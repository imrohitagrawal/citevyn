"""Delete an account on request (#511). Operator only: there is no route.

The privacy page promises to delete an account and its data on request. The
operator runs ``python -m app.worker.cli delete-account`` (``docs/RUNBOOK.md``
§3.8, "Delete an account on request"), which calls :func:`plan_deletion` (the
dry run) and then :func:`delete_account`.

What a deletion does, in ONE transaction:

1. Finds the account by address, normalised the way sign-up stores it
   (``strip().lower()``), and locks its ``users`` row (``FOR UPDATE`` on
   Postgres) so no answer, webhook or sign-in can add a row under it meanwhile.
2. Refuses (:class:`SubscriptionStillLive`) unless every subscription of the
   account has ENDED. ``past_due`` refuses even after our 7-day grace, because
   the payment provider keeps retrying the card; a status we do not know also
   refuses. Deleting the account would otherwise leave someone being billed by
   an account we can no longer find.
3. Counts the trial answers the account used, exactly as the Free allowance
   counts them (``app.access.quota.counted_rows(user_id, None)``).
4. Adds that count to the address's tombstone (``deleted_accounts``, keyed by
   the SHA-256 of the address, never the address), so the same address cannot
   start a fresh free trial (``docs/ACCESS_POLICY.md``).
5. Deletes the ``users`` row. Every table that references it is either deleted
   with it (``ON DELETE CASCADE``) or keeps its rows with the account link
   cleared (``audit_events``, ``ON DELETE SET NULL``). ``provider_calls`` and
   ``stripe_events`` have no foreign key and are kept: spend already incurred
   and the payment-event log.

Anonymous ``anon_*`` accounts have no address and are out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column, Table, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.quota import counted_rows
from app.models import Base, DeletedAccount, Membership, User
from app.models.deleted_accounts import email_hash

# Subscription statuses that can no longer bill. Anything else, including a
# status the provider adds later, blocks the deletion: fail closed.
ENDED_STATUSES = frozenset({"canceled", "incomplete_expired"})


class AccountNotFound(Exception):
    """No account has this address."""


class AmbiguousAddress(Exception):
    """More than one account has this address once case and spaces are
    ignored (rows stored before addresses were normalised)."""

    def __init__(self, matches: int) -> None:
        super().__init__(f"{matches} accounts match")
        self.matches = matches


class SubscriptionStillLive(Exception):
    """The account has a subscription that can still bill."""

    def __init__(self, statuses: list[str]) -> None:
        super().__init__(f"subscription still live: {', '.join(statuses)}")
        self.statuses = statuses


@dataclass(frozen=True)
class DeletionPlan:
    """What :func:`delete_account` would do. ``rows`` is rows deleted per
    table; ``kept`` is rows kept with the account link cleared."""

    user_id: str
    trial_answers_used: int
    carried_over: int  # already on the tombstone from an earlier deletion
    rows: dict[str, int]
    kept: dict[str, int]
    live_subscriptions: list[str]


@dataclass(frozen=True)
class DeletionReport:
    """What :func:`delete_account` did."""

    user_id: str
    trial_answers_used: int
    carried_over: int
    tombstone_total: int
    rows: dict[str, int]
    kept: dict[str, int]


def _user_references() -> list[tuple[Table, Column[str], bool]]:
    """Every ``(table, column, set_null)`` with a foreign key onto
    ``users.user_id``, read from the models so a new table is never missed."""
    found: list[tuple[Table, Column[str], bool]] = []
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name == "users" and fk.column.name == "user_id":
                set_null = (fk.ondelete or "").upper() == "SET NULL"
                found.append((table, fk.parent, set_null))
    return sorted(found, key=lambda ref: ref[0].name)


async def _find(db: AsyncSession, email: str, *, lock: bool) -> User:
    # Case and spaces are ignored on the STORED side too: provider addresses
    # were stored verbatim before ADR-0004 PR 14, and nothing lower-cased them.
    # Operator-only and rare, so a scan of ``users`` is fine here.
    stored = func.lower(func.trim(User.email))
    query = select(User).where(stored == email.strip().lower())
    if lock:
        query = query.with_for_update()
    users = (await db.execute(query)).scalars().all()
    if not users:
        raise AccountNotFound(email)
    if len(users) > 1:
        raise AmbiguousAddress(len(users))
    return users[0]


async def _plan(db: AsyncSession, user: User) -> DeletionPlan:
    rows: dict[str, int] = {}
    kept: dict[str, int] = {}
    for table, column, set_null in _user_references():
        query = select(func.count()).select_from(table).where(column == user.user_id)
        (kept if set_null else rows)[table.name] = int(await db.scalar(query) or 0)
    statuses = await db.scalars(
        select(Membership.status).where(Membership.account_id == user.user_id)
    )
    # Exactly what the Free allowance counts (``usage_for``, Allowance.once).
    used = int(await db.scalar(select(func.count()).where(*counted_rows(user.user_id, None))) or 0)
    stone = await db.get(DeletedAccount, email_hash(user.email or ""))
    return DeletionPlan(
        user_id=user.user_id,
        trial_answers_used=used,
        carried_over=stone.trial_answers_used if stone is not None else 0,
        rows=rows,
        kept=kept,
        live_subscriptions=sorted(s for s in statuses if s not in ENDED_STATUSES),
    )


async def plan_deletion(db: AsyncSession, email: str) -> DeletionPlan:
    """The dry run: what :func:`delete_account` would delete. Writes nothing."""
    return await _plan(db, await _find(db, email, lock=False))


async def delete_account(db: AsyncSession, email: str, now: datetime) -> DeletionReport:
    """Delete the account with this address, keeping its tombstone. Commits.

    Raises :class:`AccountNotFound` or :class:`SubscriptionStillLive` before
    writing anything; any other failure rolls the whole deletion back."""
    try:
        user = await _find(db, email, lock=True)
        plan = await _plan(db, user)
        if plan.live_subscriptions:
            raise SubscriptionStillLive(plan.live_subscriptions)
        key = email_hash(user.email or "")
        stone = await db.get(DeletedAccount, key, with_for_update=True)
        if stone is None:
            stone = DeletedAccount(email_hash=key, trial_answers_used=0, deleted_at=now)
            db.add(stone)
        stone.trial_answers_used += plan.trial_answers_used
        stone.deleted_at = now
        total = stone.trial_answers_used
        await db.flush()
        # A bulk DELETE, so the database's own ON DELETE rules run (the models
        # declare no ORM relationships to cascade through).
        await db.execute(delete(User).where(User.user_id == user.user_id))
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return DeletionReport(
        user_id=plan.user_id,
        trial_answers_used=plan.trial_answers_used,
        carried_over=plan.carried_over,
        tombstone_total=total,
        rows=plan.rows,
        kept=plan.kept,
    )


__all__ = [
    "ENDED_STATUSES",
    "AccountNotFound",
    "AmbiguousAddress",
    "DeletionPlan",
    "DeletionReport",
    "SubscriptionStillLive",
    "delete_account",
    "email_hash",
    "plan_deletion",
]
