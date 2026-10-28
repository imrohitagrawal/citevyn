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
6. Records the account id on the deletion list OUTSIDE the database
   (``app.core.deletion_list``, #546), just before committing, so a restore
   from an older backup cannot quietly bring the account back. If the list
   cannot be written, nothing is deleted.

:func:`reapply_deletions` deletes again every listed account a restore brought
back. The app runs it at startup and every hour (:func:`keep_reapplying`).

Anonymous ``anon_*`` visitors and the shared ``demo_user``/``admin`` rows are
out of scope: only ``usr_`` accounts can be named.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Column, Table, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.access.quota import counted_rows

# Subscription statuses that can no longer bill (one definition, shared with
# the webhook's warning). Anything else blocks the deletion: fail closed.
from app.billing.memberships import ENDED_STATUSES
from app.core import deletion_list
from app.core.auth_sessions import is_registered_principal
from app.core.deletion_list import KEEP, DeletionList
from app.models import Base, DeletedAccount, Membership, Message, Session, User
from app.models.deleted_accounts import email_hash

_logger = logging.getLogger("citevyn.account_deletion")

#: How often a running app re-applies the list, and how soon it tries again
#: after a failed run.
REAPPLY_EVERY_SECONDS = 3600
REAPPLY_RETRY_SECONDS = 60
_sleep = asyncio.sleep  # replaced in tests


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


async def _remove(db: AsyncSession, user: User, plan: DeletionPlan, when: datetime) -> int | None:
    """Write the tombstone and delete the ``users`` row. Does not commit.
    Returns the tombstone's new total, or None for an account with no address."""
    total: int | None = None
    if user.email:
        # An account with no address never had a trial (it cannot be
        # verified) and has nothing to key a tombstone by.
        key = email_hash(user.email)
        stone = await db.get(DeletedAccount, key, with_for_update=True)
        if stone is None:
            stone = DeletedAccount(email_hash=key, trial_answers_used=0, deleted_at=when)
            db.add(stone)
        stone.trial_answers_used += plan.trial_answers_used
        stone.deleted_at = when
        total = stone.trial_answers_used
        await db.flush()
    # A bulk DELETE, so the database's own ON DELETE rules run (the models
    # declare no ORM relationships to cascade through).
    await db.execute(delete(User).where(User.user_id == user.user_id))
    return total


async def delete_account(
    db: AsyncSession, email: str | None, now: datetime, *, user_id: str | None = None
) -> DeletionReport:
    """Delete the account named by address or ``user_id`` (exactly one),
    keeping its tombstone when it has an address, and record it on the
    deletion list outside the database. Commits.

    Raises :class:`AccountNotFound`, :class:`AmbiguousAddress`,
    :class:`SubscriptionStillLive` or
    :class:`~app.core.deletion_list.DeletionListUnavailable` before anything
    is committed; any other failure rolls the whole deletion back."""
    outside = deletion_list.current()
    listed: str | None = None
    try:
        user = await _find(db, email, user_id, lock=True)
        plan = await _plan(db, user)
        if plan.live_subscriptions:
            raise SubscriptionStillLive(plan.live_subscriptions)
        total = await _remove(db, user, plan, now)
        if outside is not None:
            # Last before the commit: a list we cannot write means no deletion.
            # ``listed`` is set FIRST: a write that reached Redis but whose
            # reply timed out must be undone too (review finding).
            listed = user.user_id
            await outside.record(user.user_id, now)
        await db.commit()
    except BaseException:
        await db.rollback()
        if listed is not None:
            await _unlist_if_still_there(db, outside, listed)
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


async def _unlist_if_still_there(
    db: AsyncSession, outside: DeletionList | None, user_id: str
) -> None:
    """The commit failed after the account was listed. If the account is still
    there, the deletion did not happen: unlist it, or the hourly re-apply would
    delete an account whose deletion was reported as failed. If it is gone (the
    commit landed and only the reply was lost), or we cannot tell, keep it
    listed: the person asked for the deletion, and a restore must not undo it."""
    assert outside is not None
    try:
        still_there = await db.scalar(select(User.user_id).where(User.user_id == user_id))
        if still_there is not None:
            await outside.forget(user_id)
    except Exception:  # noqa: BLE001 - best effort; the original error is re-raised
        _logger.warning("deletion_unlist_failed")


@dataclass(frozen=True)
class ReapplyReport:
    """What one :func:`reapply_deletions` run did (counts only)."""

    listed: int
    deleted_again: int


async def reapply_deletions(
    make: async_sessionmaker[AsyncSession], outside: DeletionList, now: datetime
) -> ReapplyReport:
    """Delete again every listed account that is in the database: only a
    restore from an older backup can bring one back (account ids are never
    reused). Each goes the way the first deletion went (tombstone written
    back, every referencing row with it), WITHOUT the subscription check: that
    check passed when the account was first deleted, and the restored copy
    shows the membership as it was before. Each account in its own
    transaction, its row locked, so two machines running this at once delete
    it once.

    Raises :class:`~app.core.deletion_list.DeletionListUnavailable` when the
    list cannot be read. Logs counts only, never an id or an address."""
    entries = await outside.entries(now)
    async with make() as db:
        present = list(
            await db.scalars(select(User.user_id).where(User.user_id.in_(list(entries))))
        )
        # Every deletion in the last KEEP that kept a tombstone also listed the
        # account, so more recent tombstones than entries means entries went
        # missing (evicted, or a deletion made where no Redis was configured).
        recent = int(
            await db.scalar(
                select(func.count())
                .select_from(DeletedAccount)
                .where(DeletedAccount.deleted_at > now - KEEP)
            )
            or 0
        )
    if recent > len(entries):
        _logger.error(
            "deletion_list_missing_entries",
            extra={"listed": len(entries), "recent_tombstones": recent},
        )
    deleted_again = 0
    failures = 0
    failed: Exception | None = None
    for user_id in sorted(present):
        async with make() as db:
            try:
                user = await _find(db, None, user_id, lock=True)
                await _remove(db, user, await _plan(db, user), entries[user_id])
                await db.commit()
            except AccountNotFound:
                continue  # another machine got there first
            except Exception as exc:  # noqa: BLE001 - one account must not stop the rest
                await db.rollback()
                failures += 1
                failed = failed or exc
                continue
        deleted_again += 1
    _logger.info(
        "deletions_reapplied",
        extra={
            "listed": len(entries),
            "deleted_again": deleted_again,
            "failed": failures,
        },
    )
    if failed is not None:
        # Every other account was still handled; the caller retries soon.
        raise failed
    return ReapplyReport(listed=len(entries), deleted_again=deleted_again)


async def keep_reapplying(
    make: async_sessionmaker[AsyncSession],
    outside: DeletionList,
    *,
    first_delay: float = REAPPLY_EVERY_SECONDS,
) -> None:
    """Run :func:`reapply_deletions` every hour, for as long as the app runs
    (the caller cancels it at shutdown). The first run is ``first_delay`` in:
    an hour after a startup run that worked, sooner after one that failed. A
    failed run is logged and tried again sooner."""
    delay = first_delay
    while True:
        await _sleep(delay)
        try:
            await reapply_deletions(make, outside, datetime.now(UTC))
            delay = REAPPLY_EVERY_SECONDS
        except Exception as exc:  # noqa: BLE001 - logged; the loop must survive
            _logger.error("deletions_reapply_failed", extra={"error": type(exc).__name__})
            delay = REAPPLY_RETRY_SECONDS


__all__ = [
    "ENDED_STATUSES",
    "AccountNotFound",
    "AmbiguousAddress",
    "DeletionPlan",
    "DeletionReport",
    "ReapplyReport",
    "SubscriptionStillLive",
    "delete_account",
    "email_hash",
    "keep_reapplying",
    "plan_deletion",
    "reapply_deletions",
]
