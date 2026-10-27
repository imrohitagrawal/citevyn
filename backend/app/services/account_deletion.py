"""Delete an account on request (#511). Operator only: there is no route.

The privacy page promises to delete an account and its data on request. The
operator runs ``python -m app.worker.cli delete-account`` (``docs/RUNBOOK.md``
§3.8, "Delete an account on request"), which calls :func:`plan_deletion` (the
dry run) and then :func:`delete_account`.

What a deletion does, in ONE transaction:

1. Finds the account by address, ignoring case and surrounding spaces, or by
   its ``usr_`` id (for an account with no stored address: OAuth stores none
   for an unverified, non-ASCII or already-taken provider address), and locks
   its ``users`` row (``FOR UPDATE`` on Postgres) so no answer, webhook or
   sign-in can add a row under it meanwhile.
2. Refuses (:class:`SubscriptionStillLive`) unless every subscription of the
   account has ENDED. ``past_due`` refuses even after our 7-day grace, because
   the payment provider keeps retrying the card; a status we do not know also
   refuses. Deleting the account would otherwise leave someone being billed by
   an account we can no longer find.
3. Counts the trial answers the account used, exactly as the Free allowance
   counts them (``app.access.quota.counted_rows(user_id, None)``).
4. Adds that count to the address's tombstone (``deleted_accounts``, keyed by
   the SHA-256 of the address, never the address), so the same address cannot
   start a fresh free trial (``docs/ACCESS_POLICY.md``). Skipped for an
   account with no address: it could not be verified, so it never had a trial.
5. Deletes the ``users`` row. Every table that references it is either deleted
   with it (``ON DELETE CASCADE``) or keeps its rows with the account link
   cleared (``audit_events``, ``ON DELETE SET NULL``). ``provider_calls`` and
   ``stripe_events`` have no foreign key and are kept: spend already incurred
   and the payment-event log.

Anonymous ``anon_*`` visitors and the shared ``demo_user``/``admin`` rows are
out of scope: only ``usr_`` accounts can be named.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column, Table, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.quota import counted_rows

# Subscription statuses that can no longer bill (one definition, shared with
# the webhook's warning). Anything else blocks the deletion: fail closed.
from app.billing.memberships import ENDED_STATUSES
from app.core.auth_sessions import is_registered_principal
from app.models import Base, DeletedAccount, Membership, Message, Session, User
from app.models.deleted_accounts import email_hash


class AccountNotFound(Exception):
    """No registered account has this address or id."""


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
    table that references the account; ``kept`` is rows kept with the account
    link cleared; ``indirect`` is rows deleted through those tables (chat
    messages go with their session)."""

    user_id: str
    has_address: bool
    trial_answers_used: int
    carried_over: int  # already on the tombstone from an earlier deletion
    rows: dict[str, int]
    kept: dict[str, int]
    indirect: dict[str, int]
    live_subscriptions: list[str]


@dataclass(frozen=True)
class DeletionReport:
    """What :func:`delete_account` did. ``tombstone_total`` is None when the
    account had no address (nothing to key a tombstone by)."""

    user_id: str
    trial_answers_used: int
    carried_over: int
    tombstone_total: int | None
    rows: dict[str, int]
    kept: dict[str, int]
    indirect: dict[str, int]


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


async def _find(db: AsyncSession, email: str | None, user_id: str | None, *, lock: bool) -> User:
    if (email is None) == (user_id is None):
        raise ValueError("give exactly one of an address or an account id")
    if user_id is not None:
        # Registered accounts only: ``demo_user`` holds every bearer-token
        # visitor's chats, and ``admin``/``anon_*`` are not a person's account.
        if not is_registered_principal(user_id):
            raise AccountNotFound(user_id)
        query = select(User).where(User.user_id == user_id)
    else:
        assert email is not None
        # Case and spaces are ignored on the STORED side too: provider
        # addresses were stored verbatim before ADR-0004 PR 14, and nothing
        # lower-cased them. Operator-only and rare, so a scan is fine here.
        stored = func.lower(func.trim(User.email))
        query = select(User).where(stored == email.strip().lower())
    if lock:
        query = query.with_for_update()
    users = (await db.execute(query)).scalars().all()
    if not users:
        raise AccountNotFound(email or user_id)
    if len(users) > 1:
        raise AmbiguousAddress(len(users))
    return users[0]


async def _plan(db: AsyncSession, user: User) -> DeletionPlan:
    rows: dict[str, int] = {}
    kept: dict[str, int] = {}
    for table, column, set_null in _user_references():
        query = select(func.count()).select_from(table).where(column == user.user_id)
        (kept if set_null else rows)[table.name] = int(await db.scalar(query) or 0)
    messages = (
        select(func.count())
        .select_from(Message)
        .join(Session, Message.session_id == Session.session_id)
        .where(Session.user_id == user.user_id)
    )
    statuses = await db.scalars(
        select(Membership.status).where(Membership.account_id == user.user_id)
    )
    # Exactly what the Free allowance counts (``usage_for``, Allowance.once).
    used = int(await db.scalar(select(func.count()).where(*counted_rows(user.user_id, None))) or 0)
    stone = await db.get(DeletedAccount, email_hash(user.email)) if user.email else None
    return DeletionPlan(
        user_id=user.user_id,
        has_address=bool(user.email),
        trial_answers_used=used,
        carried_over=stone.trial_answers_used if stone is not None else 0,
        rows=rows,
        kept=kept,
        indirect={"messages": int(await db.scalar(messages) or 0)},
        live_subscriptions=sorted(s for s in statuses if s not in ENDED_STATUSES),
    )


async def plan_deletion(
    db: AsyncSession, email: str | None = None, *, user_id: str | None = None
) -> DeletionPlan:
    """The dry run: what :func:`delete_account` would delete. Writes nothing.
    Name the account by address or by ``user_id`` (exactly one)."""
    return await _plan(db, await _find(db, email, user_id, lock=False))


async def delete_account(
    db: AsyncSession, email: str | None, now: datetime, *, user_id: str | None = None
) -> DeletionReport:
    """Delete the account named by address or ``user_id`` (exactly one),
    keeping its tombstone when it has an address. Commits.

    Raises :class:`AccountNotFound`, :class:`AmbiguousAddress` or
    :class:`SubscriptionStillLive` before writing anything; any other failure
    rolls the whole deletion back."""
    try:
        user = await _find(db, email, user_id, lock=True)
        plan = await _plan(db, user)
        if plan.live_subscriptions:
            raise SubscriptionStillLive(plan.live_subscriptions)
        total: int | None = None
        if user.email:
            # An account with no address never had a trial (it cannot be
            # verified) and has nothing to key a tombstone by.
            key = email_hash(user.email)
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
        indirect=plan.indirect,
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
