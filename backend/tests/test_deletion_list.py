"""Deletions survive a restore (#546).

A restore from a backup brings a deleted account back, and its tombstone goes
with it, because both live in the database. So every deletion is also written
to a list OUTSIDE the database (Redis: account id and date, never the address),
and the app re-applies that list on its own: at startup, every hour, and at
every sign-in and session lookup a listed account is refused.

Hermetic: fakeredis and a file-backed SQLite with foreign keys on, as in
``test_account_deletion.py``. Every test says which change turns it red.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core import deletion_list
from app.core.deletion_list import KEEP, DeletionList, DeletionListUnavailable
from app.models import (
    AnswerUsage,
    AuthSession,
    Base,
    DeletedAccount,
    Membership,
    User,
    UserRole,
)
from app.services import account_deletion
from app.services.account_deletion import (
    SubscriptionStillLive,
    delete_account,
    email_hash,
    reapply_deletions,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
EMAIL = "person@example.com"
KEY = "test:deleted_accounts"

Maker = async_sessionmaker[AsyncSession]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def maker(tmp_path: Any) -> AsyncGenerator[Maker]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'list.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def fake() -> AsyncGenerator[Any]:
    import fakeredis.aioredis as fake_aioredis

    client = fake_aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def listed(fake: Any, monkeypatch: pytest.MonkeyPatch) -> DeletionList:
    """The deletion list every caller gets, backed by fakeredis."""
    the_list = DeletionList(fake, KEY)
    monkeypatch.setattr(deletion_list, "current", lambda: the_list)
    return the_list


class Broken:
    """A Redis client whose every call fails, as when Upstash is unreachable."""

    def __getattr__(self, _name: str) -> Any:
        import redis.exceptions

        async def _fail(*_a: Any, **_k: Any) -> Any:
            raise redis.exceptions.ConnectionError("unreachable")

        return _fail


async def add_user(
    make: Maker, email: str | None = EMAIL, *, answers: int = 0, statuses: tuple[str, ...] = ()
) -> str:
    user_id = "usr_" + uuid.uuid4().hex
    async with make() as s:
        s.add(
            User(
                user_id=user_id,
                role=UserRole.demo_user,
                created_at=NOW,
                email=email,
                email_verified_at=NOW,
            )
        )
        await s.flush()
        for _ in range(answers):
            s.add(
                AnswerUsage(
                    user_id=user_id,
                    occurred_at=NOW - timedelta(days=3),
                    paid_by="platform",
                    channel="chat",
                    tier="free",
                )
            )
        for status in statuses:
            s.add(
                Membership(
                    account_id=user_id,
                    status=status,
                    stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        await s.commit()
    return user_id


async def restore(make: Maker, user_id: str, *, answers: int = 2, status: str = "active") -> None:
    """What a restore from an older backup does: the account is back, with a
    membership that was live when the backup was taken, and the tombstone the
    deletion wrote is gone."""
    async with make() as s:
        s.add(
            User(
                user_id=user_id,
                role=UserRole.demo_user,
                created_at=NOW,
                email=EMAIL,
                email_verified_at=NOW,
            )
        )
        await s.flush()
        for _ in range(answers):
            s.add(
                AnswerUsage(
                    user_id=user_id,
                    occurred_at=NOW - timedelta(days=3),
                    paid_by="platform",
                    channel="chat",
                    tier="free",
                )
            )
        s.add(
            Membership(
                account_id=user_id,
                status=status,
                stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        stone = await s.get(DeletedAccount, email_hash(EMAIL))
        if stone is not None:
            await s.delete(stone)
        await s.commit()


async def users(make: Maker, user_id: str) -> int:
    async with make() as s:
        query = select(func.count()).select_from(User).where(User.user_id == user_id)
        return int((await s.execute(query)).scalar_one())


async def stone(make: Maker) -> DeletedAccount | None:
    async with make() as s:
        return await s.get(DeletedAccount, email_hash(EMAIL))


# ---------------------------------------------------------------------------
# The list itself
# ---------------------------------------------------------------------------


async def test_record_keeps_the_id_and_date_and_never_the_address(fake: Any) -> None:
    """Turns red if: the list stores the address, or loses the date."""
    the_list = DeletionList(fake, KEY)
    await the_list.record("usr_abc", NOW)
    assert await the_list.entries(NOW) == {"usr_abc": NOW}
    dumped = [k for k in await fake.keys("*")] + [
        m for m, _ in await fake.zrange(KEY, 0, -1, withscores=True)
    ]
    assert not any("@" in item for item in dumped)
    # The key never expires as a whole: entries are dropped one by one.
    assert await fake.ttl(KEY) == -1


async def test_entries_are_kept_60_days_then_dropped(fake: Any) -> None:
    """Turns red if: KEEP drops below 60 days, or old entries are never dropped."""
    assert timedelta(days=60) <= KEEP
    the_list = DeletionList(fake, KEY)
    await the_list.record("usr_old", NOW - KEEP - timedelta(seconds=1))
    await the_list.record("usr_kept", NOW - timedelta(days=59))
    assert set(await the_list.entries(NOW)) == {"usr_kept"}
    # Dropped from Redis itself, not just filtered from the answer.
    assert await fake.zscore(KEY, "usr_old") is None


async def test_contains_answers_for_listed_ids_only(fake: Any) -> None:
    """Turns red if: contains() is always true or always false."""
    the_list = DeletionList(fake, KEY)
    await the_list.record("usr_gone", NOW)
    assert await the_list.contains("usr_gone") is True
    assert await the_list.contains("usr_other") is False


async def test_an_unreachable_redis_is_reported_as_unavailable() -> None:
    """Turns red if: a Redis error escapes as a raw library error."""
    the_list = DeletionList(Broken(), KEY)
    with pytest.raises(DeletionListUnavailable):
        await the_list.record("usr_x", NOW)
    with pytest.raises(DeletionListUnavailable):
        await the_list.entries(NOW)
    with pytest.raises(DeletionListUnavailable):
        await the_list.contains("usr_x")


def test_the_gate_s_prefix_is_the_registered_account_prefix() -> None:
    """Turns red if: the two copies of the prefix drift apart (the gate would
    skip every real account)."""
    from app.core.auth_sessions import REGISTERED_USER_PREFIX

    assert deletion_list._REGISTERED_PREFIX == REGISTERED_USER_PREFIX


def test_no_list_without_a_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turns red if: current() builds a list (and opens Redis) with no URL set."""
    from app.core.config import Settings

    monkeypatch.setattr("app.core.deletion_list.get_settings", lambda: Settings(redis_url=None))
    assert deletion_list.current() is None


def test_the_list_key_is_namespaced_by_the_redis_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turns red if: the key ignores CITEVYN_REDIS_KEY_PREFIX (two environments
    sharing one Redis would share one list)."""
    from app.core.config import Settings

    settings = Settings(redis_url="redis://localhost:6379/0", redis_key_prefix="env1")
    monkeypatch.setattr("app.core.deletion_list.get_settings", lambda: settings)
    monkeypatch.setattr("app.core.deletion_list.get_redis_client", lambda: object())
    the_list = deletion_list.current()
    assert the_list is not None
    assert the_list.key == "env1:deleted_accounts"


# ---------------------------------------------------------------------------
# A deletion writes the list
# ---------------------------------------------------------------------------


async def test_a_deletion_records_the_account_id(maker: Maker, listed: DeletionList) -> None:
    """Turns red if: delete_account stops writing the outside list."""
    user_id = await add_user(maker, answers=1)
    async with maker() as s:
        await delete_account(s, EMAIL, NOW)
    assert await listed.entries(NOW) == {user_id: NOW}


async def test_a_refused_deletion_records_nothing(maker: Maker, listed: DeletionList) -> None:
    """Turns red if: the list is written before the billing check."""
    await add_user(maker, statuses=("active",))
    async with maker() as s:
        with pytest.raises(SubscriptionStillLive):
            await delete_account(s, EMAIL, NOW)
    assert await listed.entries(NOW) == {}


async def test_redis_down_refuses_the_deletion_and_changes_nothing(
    maker: Maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: a deletion the list cannot record is not made, so it can be
    retried, rather than made and silently lost on the next restore.
    Turns red if: the database delete commits when the list write failed."""
    monkeypatch.setattr(deletion_list, "current", lambda: DeletionList(Broken(), KEY))
    user_id = await add_user(maker, answers=1)
    async with maker() as s:
        with pytest.raises(DeletionListUnavailable):
            await delete_account(s, EMAIL, NOW)
    assert await users(maker, user_id) == 1
    assert await stone(maker) is None


class LandsThenTimesOut:
    """Redis stores the write, then the reply times out."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        import redis.exceptions

        method = getattr(self._real, name)

        async def _call(*a: Any, **k: Any) -> Any:
            result = await method(*a, **k)
            if name == "zadd":
                raise redis.exceptions.TimeoutError("reply lost")
            return result

        return _call


async def test_a_list_write_that_lands_but_times_out_is_undone(
    maker: Maker, fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding (all three reviewers): the command says "Nothing was
    changed", so the entry must not stay behind for the hourly run to act on.
    Turns red if: ``listed`` is set only after record() returns."""
    the_list = DeletionList(LandsThenTimesOut(fake), KEY)
    monkeypatch.setattr(deletion_list, "current", lambda: the_list)
    user_id = await add_user(maker)
    async with maker() as s:
        with pytest.raises(DeletionListUnavailable):
            await delete_account(s, EMAIL, NOW)
    assert await users(maker, user_id) == 1
    assert await fake.zscore(KEY, user_id) is None


async def test_without_a_redis_url_the_deletion_still_works(
    maker: Maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local work and tests have no Redis. Turns red if: no list means no deletion."""
    monkeypatch.setattr(deletion_list, "current", lambda: None)
    user_id = await add_user(maker)
    async with maker() as s:
        await delete_account(s, EMAIL, NOW)
    assert await users(maker, user_id) == 0


async def test_a_failed_commit_unlists_an_account_that_still_exists(
    maker: Maker, listed: DeletionList, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turns red if: a commit that failed leaves the account listed, so the
    hourly re-apply would delete an account whose deletion was reported failed."""
    user_id = await add_user(maker)

    async def fail(self: AsyncSession) -> None:
        raise RuntimeError("connection dropped")

    async with maker() as s:
        monkeypatch.setattr(AsyncSession, "commit", fail)
        with pytest.raises(RuntimeError):
            await delete_account(s, EMAIL, NOW)
        monkeypatch.undo()
    assert await users(maker, user_id) == 1
    assert await listed.entries(NOW) == {}


async def test_a_commit_that_landed_but_reported_failure_stays_listed(
    maker: Maker, listed: DeletionList, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit reached the database but the reply was lost. Turns red if:
    the account is unlisted although it is gone (a restore would bring it back
    for good)."""
    user_id = await add_user(maker)
    real_commit = AsyncSession.commit

    async def landed_then_fail(self: AsyncSession) -> None:
        await real_commit(self)
        raise RuntimeError("reply lost")

    async with maker() as s:
        monkeypatch.setattr(AsyncSession, "commit", landed_then_fail)
        with pytest.raises(RuntimeError):
            await delete_account(s, EMAIL, NOW)
        monkeypatch.undo()
    assert await users(maker, user_id) == 0
    assert set(await listed.entries(NOW)) == {user_id}


# ---------------------------------------------------------------------------
# Re-applying the list
# ---------------------------------------------------------------------------


async def test_a_restored_account_is_deleted_again_with_its_tombstone(
    maker: Maker, listed: DeletionList
) -> None:
    """Turns red if: re-apply skips a restored account, or forgets to write
    its tombstone back (the address would get a fresh free trial)."""
    user_id = await add_user(maker, answers=2)
    async with maker() as s:
        await delete_account(s, EMAIL, NOW)
    await restore(maker, user_id, answers=2)
    assert await users(maker, user_id) == 1 and await stone(maker) is None

    report = await reapply_deletions(maker, listed, NOW + timedelta(hours=1))
    assert (report.listed, report.deleted_again) == (1, 1)
    assert await users(maker, user_id) == 0
    restored_stone = await stone(maker)
    assert restored_stone is not None
    assert restored_stone.trial_answers_used == 2


@pytest.mark.parametrize("status", ["active", "past_due", "trialing", "something_new"])
async def test_a_live_membership_in_the_restored_copy_does_not_block(
    maker: Maker, listed: DeletionList, status: str
) -> None:
    """The original deletion already checked billing; the restored copy shows
    the membership as it was before that. Turns red if: re-apply runs the
    billing refusal."""
    user_id = await add_user(maker)
    async with maker() as s:
        await delete_account(s, EMAIL, NOW)
    await restore(maker, user_id, status=status)
    report = await reapply_deletions(maker, listed, NOW)
    assert report.deleted_again == 1
    assert await users(maker, user_id) == 0


async def test_re_apply_touches_only_listed_accounts(maker: Maker, listed: DeletionList) -> None:
    """Turns red if: re-apply deletes an account that is not on the list."""
    keep = await add_user(maker, "keep@example.com")
    await listed.record("usr_" + uuid.uuid4().hex, NOW)  # deleted, not restored
    report = await reapply_deletions(maker, listed, NOW)
    assert (report.listed, report.deleted_again) == (1, 0)
    assert await users(maker, keep) == 1


async def test_re_apply_is_idempotent(maker: Maker, listed: DeletionList) -> None:
    """Turns red if: a second run (another machine, the next hour) adds the
    trial count to the tombstone again."""
    user_id = await add_user(maker, answers=2)
    async with maker() as s:
        await delete_account(s, EMAIL, NOW)
    await restore(maker, user_id, answers=2)
    await reapply_deletions(maker, listed, NOW)
    second = await reapply_deletions(maker, listed, NOW)
    assert second.deleted_again == 0
    restored_stone = await stone(maker)
    assert restored_stone is not None and restored_stone.trial_answers_used == 2


async def test_one_failing_account_does_not_stop_the_rest(
    maker: Maker, listed: DeletionList, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding. Turns red if: re-apply stops at the first account that
    fails, or swallows the failure (the caller must retry soon)."""
    first = await add_user(maker, "a@example.com")
    second = await add_user(maker, "b@example.com")
    for user_id in (first, second):
        await listed.record(user_id, NOW)
    bad = min(first, second)  # re-apply goes in sorted order: fail the first
    real_remove = account_deletion._remove

    async def remove(db: AsyncSession, user: User, *a: Any) -> Any:
        if user.user_id == bad:
            raise RuntimeError("foreign key")
        return await real_remove(db, user, *a)

    monkeypatch.setattr(account_deletion, "_remove", remove)
    with pytest.raises(RuntimeError):
        await reapply_deletions(maker, listed, NOW)
    assert await users(maker, bad) == 1
    assert await users(maker, max(first, second)) == 0


async def test_re_apply_logs_counts_never_ids_or_addresses(
    maker: Maker, listed: DeletionList, caplog: pytest.LogCaptureFixture
) -> None:
    """Turns red if: a log line carries the account id or the address."""
    user_id = await add_user(maker)
    async with maker() as s:
        await delete_account(s, EMAIL, NOW)
    await restore(maker, user_id)
    with caplog.at_level(logging.INFO):
        await reapply_deletions(maker, listed, NOW)
    lines = [r for r in caplog.records if r.name == "citevyn.account_deletion"]
    assert lines, "re-apply logged nothing"
    for record in lines:
        text = record.getMessage() + repr(record.__dict__)
        assert user_id not in text and EMAIL not in text
    assert any(getattr(r, "deleted_again", None) == 1 for r in lines)


async def test_a_list_shorter_than_the_tombstones_is_reported(
    maker: Maker, listed: DeletionList, caplog: pytest.LogCaptureFixture
) -> None:
    """The list must not vanish silently (Upstash eviction, a lost database,
    a deletion made where no Redis was configured). Every deletion of the last
    60 days that kept a tombstone also wrote a list entry, so more recent
    tombstones than entries means entries are missing.
    Turns red if: the check is removed or compares the wrong way."""
    await add_user(maker)
    async with maker() as s:
        await delete_account(s, EMAIL, NOW)
    with caplog.at_level(logging.INFO):
        await reapply_deletions(maker, listed, NOW)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    await listed._client.delete(KEY)  # the whole list is gone
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await reapply_deletions(maker, listed, NOW)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == ["deletion_list_missing_entries"]


async def test_re_apply_with_redis_down_raises_unavailable(maker: Maker) -> None:
    """Turns red if: an unreadable list is treated as an empty one (nothing
    re-applied, and nothing says so)."""
    with pytest.raises(DeletionListUnavailable):
        await reapply_deletions(maker, DeletionList(Broken(), KEY), NOW)


# ---------------------------------------------------------------------------
# The gate: a listed account cannot be used in between
# ---------------------------------------------------------------------------


async def test_is_listed(listed: DeletionList) -> None:
    """Turns red if: the gate misses a listed account or blocks others."""
    await listed.record("usr_gone", NOW)
    assert await deletion_list.is_listed("usr_gone") is True
    assert await deletion_list.is_listed("usr_here") is False
    # Anonymous visitors and shared rows are never listed; no Redis call needed.
    assert await deletion_list.is_listed("anon_" + "0" * 32) is False


async def test_is_listed_fails_closed_with_a_503_when_redis_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turns red if: a Redis outage lets a possibly-deleted account through, or
    answers as if signed out (the caller would mint a new cookie over the
    person's own)."""
    monkeypatch.setattr(deletion_list, "current", lambda: DeletionList(Broken(), KEY))
    with pytest.raises(HTTPException) as raised:
        await deletion_list.is_listed("usr_any")
    assert raised.value.status_code == 503
    assert await deletion_list.is_listed("anon_" + "0" * 32) is False


async def test_a_redis_blip_on_the_cookie_path_is_a_503_not_a_sign_out(
    maker: Maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding: one failed ZSCORE made resolve_principal mint an
    anonymous cookie over a signed-in user's. Turns red if: the session lookup
    answers None when the list cannot be read."""
    from app.core.auth_sessions import _lookup_auth_session

    user_id = await add_user(maker)
    cookie = await _session_for(maker, user_id)
    monkeypatch.setattr(deletion_list, "current", lambda: DeletionList(Broken(), KEY))
    async with maker() as s:
        with pytest.raises(HTTPException) as raised:
            await _lookup_auth_session(s, cookie)
    assert raised.value.status_code == 503


async def test_is_listed_is_false_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turns red if: local work with no Redis cannot sign in."""
    monkeypatch.setattr(deletion_list, "current", lambda: None)
    assert await deletion_list.is_listed("usr_any") is False


async def _session_for(make: Maker, user_id: str) -> str:
    """A live sign-in session for ``user_id``; returns its cookie value."""
    from app.core.auth_sessions import _hash_secret

    secret = uuid.uuid4().hex
    row_id = uuid.uuid4()
    async with make() as s:
        s.add(
            AuthSession(
                auth_session_id=row_id,
                user_id=user_id,
                secret_hash=_hash_secret(secret),
                created_at=NOW.replace(tzinfo=None),
                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1),
            )
        )
        await s.commit()
    return f"{row_id.hex}.{secret}"


async def test_a_listed_account_s_session_does_not_resolve(
    maker: Maker, listed: DeletionList
) -> None:
    """Turns red if: the session lookup stops consulting the list."""
    from app.core.auth_sessions import _lookup_auth_session

    user_id = await add_user(maker)
    cookie = await _session_for(maker, user_id)
    async with maker() as s:
        assert await _lookup_auth_session(s, cookie) is not None  # partner: it resolves
    await listed.record(user_id, NOW)
    async with maker() as s:
        assert await _lookup_auth_session(s, cookie) is None


async def test_a_listed_account_s_session_id_does_not_resolve(
    maker: Maker, listed: DeletionList
) -> None:
    """The account-linking callback maps a session id to its owner. Turns red
    if: that lookup stops consulting the list."""
    from app.core.auth_sessions import resolve_principal_by_auth_session_id

    user_id = await add_user(maker)
    row_id = uuid.UUID(hex=(await _session_for(maker, user_id)).partition(".")[0])
    async with maker() as s:
        assert await resolve_principal_by_auth_session_id(s, row_id) == user_id  # partner
    await listed.record(user_id, NOW)
    async with maker() as s:
        assert await resolve_principal_by_auth_session_id(s, row_id) is None


async def test_a_listed_account_s_api_key_does_not_authenticate(
    maker: Maker, listed: DeletionList
) -> None:
    """Turns red if: API-key authentication stops consulting the list."""
    from app.access.api_keys import authenticate_api_key, make_key

    user_id = await add_user(maker)
    new = make_key(user_id, "agent", NOW)
    async with maker() as s:
        s.add(new.row)
        await s.commit()
    async with maker() as s:
        assert await authenticate_api_key(s, new.raw) is not None  # partner
    await listed.record(user_id, NOW)
    async with maker() as s:
        assert await authenticate_api_key(s, new.raw) is None


async def test_a_listed_account_cannot_sign_in(maker: Maker, listed: DeletionList) -> None:
    """Turns red if: claim_and_login issues a session to a listed account."""
    from starlette.requests import Request

    from app.core.auth_sessions import claim_and_login
    from app.core.config import Settings

    user_id = await add_user(maker)
    await listed.record(user_id, NOW)
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    async with maker() as s:
        with pytest.raises(HTTPException) as raised:
            await claim_and_login(request, Response(), s, Settings(), user_id=user_id)
        assert raised.value.status_code == 401
    async with maker() as s:
        query = select(func.count()).select_from(AuthSession)
        assert (await s.execute(query)).scalar_one() == 0


# ---------------------------------------------------------------------------
# When it runs: at startup and every hour
# ---------------------------------------------------------------------------


async def test_startup_re_applies_before_serving(
    maker: Maker, listed: DeletionList, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turns red if: the app lifespan stops re-applying the list at startup."""
    from app.main import _lifespan, create_app

    user_id = await add_user(maker)
    async with maker() as s:
        await delete_account(s, EMAIL, NOW)
    await restore(maker, user_id)
    monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: maker)
    async with _lifespan(create_app()):
        assert await users(maker, user_id) == 0


@pytest.mark.parametrize(("redis_up", "first_delay"), [(True, 3600), (False, 60)])
async def test_startup_serves_anyway_and_retries_soon_after_a_failure(
    maker: Maker,
    fake: Any,
    monkeypatch: pytest.MonkeyPatch,
    redis_up: bool,
    first_delay: float,
) -> None:
    """The gate fails closed per request, so a failed startup re-apply must not
    keep the public pages down; and the next try must not wait an hour.
    Turns red if: the lifespan raises, the retry delay is an hour, or the
    background task is left running after shutdown."""
    from app.main import _lifespan, create_app

    client = fake if redis_up else Broken()
    monkeypatch.setattr(deletion_list, "current", lambda: DeletionList(client, KEY))
    monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: maker)
    seen: dict[str, Any] = {}
    real_loop = account_deletion.keep_reapplying

    async def loop(*a: Any, **k: Any) -> None:
        seen["first_delay"] = k.get("first_delay")
        seen["task"] = __import__("asyncio").current_task()
        await real_loop(*a, **k)

    monkeypatch.setattr(account_deletion, "keep_reapplying", loop)
    async with _lifespan(create_app()):
        await __import__("asyncio").sleep(0)  # let the task start
    assert seen["first_delay"] == first_delay
    assert seen["task"].done()


@pytest.mark.parametrize(("environment", "logged"), [("production", True), ("local", False)])
async def test_production_without_redis_says_so(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    environment: str,
    logged: bool,
) -> None:
    """Turns red if: a production app with no deletion list starts silently."""
    from types import SimpleNamespace

    from app.main import _start_reapplying_deletions

    # Only the environment is read; a full production Settings needs real keys.
    monkeypatch.setattr(deletion_list, "current", lambda: None)
    monkeypatch.setattr("app.main.get_settings", lambda: SimpleNamespace(environment=environment))
    with caplog.at_level(logging.ERROR):
        assert await _start_reapplying_deletions() is None
    found = [r for r in caplog.records if r.getMessage() == "deletion_list_not_configured"]
    assert bool(found) is logged


async def test_the_hourly_loop_re_applies_and_retries_after_a_failure(
    maker: Maker, listed: DeletionList, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turns red if: the loop runs once and stops, waits the wrong time, or
    dies on the first failure."""
    import asyncio

    waits: list[float] = []
    calls: list[int] = []

    async def fake_reapply(*_a: Any, **_k: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise DeletionListUnavailable("down")
        return account_deletion.ReapplyReport(listed=0, deleted_again=0)

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(account_deletion, "reapply_deletions", fake_reapply)
    monkeypatch.setattr(account_deletion, "_sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await account_deletion.keep_reapplying(maker, listed)
    # First wait is the full hour (startup just ran it); after the failure,
    # the retry is sooner; after a success, an hour again.
    assert waits == [3600, 60, 3600]
    assert len(calls) == 2


async def test_a_restored_account_s_shared_answer_is_not_served(
    maker: Maker, listed: DeletionList
) -> None:
    """Breaker finding: the public share page is reached without any session.
    Turns red if: /s/{id} stops consulting the list."""
    from app.api.routes import shares
    from app.models import SharedAnswer

    user_id = await add_user(maker)
    share_id = uuid.uuid4().hex
    async with maker() as s:
        s.add(
            SharedAnswer(
                share_id=share_id,
                user_id=user_id,
                question="q",
                answer="a",
                citations=[],
                docs_as_of=None,
                answered_at=NOW,
                created_at=NOW,
            )
        )
        await s.commit()
    from starlette.requests import Request

    request = Request(
        {"type": "http", "headers": [], "method": "GET", "path": "/", "state": {"request_id": "t"}}
    )
    async with maker() as s:
        page = await shares.shared_page(request, share_id, None, s)  # partner
        assert page.status_code == 200
    await listed.record(user_id, NOW)
    async with maker() as s:
        with pytest.raises(HTTPException) as raised:
            await shares.shared_page(request, share_id, None, s)
    assert raised.value.status_code == 404


# ---------------------------------------------------------------------------
# The operator's command
# ---------------------------------------------------------------------------


def _cli_user(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Maker, str]:
    """A file-backed database the command will use, with one account in it.
    Synchronous: the command runs its own event loop."""
    import asyncio

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
    make = async_sessionmaker(engine, expire_on_commit=False)

    async def _init() -> str:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return await add_user(make)

    user_id = asyncio.run(_init())
    monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: make)
    return make, user_id


def test_the_command_refuses_when_redis_is_down(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Turns red if: the command reports a Redis failure as a generic error,
    or deletes anyway."""
    import asyncio

    from app.worker import cli

    make, user_id = _cli_user(tmp_path, monkeypatch)
    monkeypatch.setattr(deletion_list, "current", lambda: DeletionList(Broken(), KEY))
    assert cli.main(["delete-account", "--email", EMAIL, "--yes"]) == 3
    out = capsys.readouterr().out
    assert "deletion list" in out and "Nothing was changed" in out
    assert asyncio.run(users(make, user_id)) == 1


@pytest.mark.parametrize("yes", [False, True])
def test_the_command_warns_when_no_redis_is_configured(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any, yes: bool
) -> None:
    """Run from a laptop against the production database, the deletion would
    not be listed. Turns red if: the warning is dropped."""
    from app.worker import cli

    _cli_user(tmp_path, monkeypatch)
    monkeypatch.setattr(deletion_list, "current", lambda: None)
    argv = ["delete-account", "--email", EMAIL] + (["--yes"] if yes else [])
    assert cli.main(argv) == 0
    assert "CITEVYN_REDIS_URL is not set" in capsys.readouterr().out


def test_the_command_does_not_warn_when_the_list_is_configured(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Partner of the warning test. Turns red if: the warning prints always."""
    from app.worker import cli

    _cli_user(tmp_path, monkeypatch)
    monkeypatch.setattr(deletion_list, "current", lambda: DeletionList(Broken(), KEY))
    assert cli.main(["delete-account", "--email", EMAIL]) == 0  # dry run: no write
    assert "CITEVYN_REDIS_URL is not set" not in capsys.readouterr().out


@pytest.mark.parametrize("job", ["_digest", "_alerts"])
def test_email_jobs_re_apply_deletions_first(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, job: str
) -> None:
    """Breaker finding: the digest and alert jobs are separate processes, so
    the app's startup and hourly runs do not cover them. Turns red if: a job
    sends without re-applying first."""
    import asyncio

    from app.worker import cli

    make, user_id = _cli_user(tmp_path, monkeypatch)
    order: list[str] = []

    async def fake_reapply(*_a: Any, **_k: Any) -> Any:
        order.append("reapply")
        return account_deletion.ReapplyReport(listed=0, deleted_again=0)

    async def fake_send(*_a: Any, **_k: Any) -> Any:
        order.append("send")
        from types import SimpleNamespace

        return SimpleNamespace(
            changes=0, sent=0, failed=0, skipped_already_sent=0, skipped_not_pro=0, capped=False
        )

    monkeypatch.setattr(deletion_list, "current", lambda: DeletionList(Broken(), KEY))
    monkeypatch.setattr(account_deletion, "reapply_deletions", fake_reapply)
    monkeypatch.setattr("app.services.digest.send_weekly_digest", fake_send)
    monkeypatch.setattr("app.services.alerts.send_watch_alerts", fake_send)
    assert asyncio.run(getattr(cli, job)(object(), object())) == 0
    assert order == ["reapply", "send"]


@pytest.mark.parametrize("job", ["_digest", "_alerts"])
def test_email_jobs_send_nothing_when_re_apply_fails(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any, job: str
) -> None:
    """Turns red if: a job emails accounts after the re-apply failed."""
    import asyncio

    from app.worker import cli

    _cli_user(tmp_path, monkeypatch)
    sent: list[int] = []

    async def fake_send(*_a: Any, **_k: Any) -> Any:
        sent.append(1)

    monkeypatch.setattr(deletion_list, "current", lambda: DeletionList(Broken(), KEY))
    monkeypatch.setattr("app.services.digest.send_weekly_digest", fake_send)
    monkeypatch.setattr("app.services.alerts.send_watch_alerts", fake_send)
    assert asyncio.run(getattr(cli, job)(object(), object())) == 4  # not 3: "some sends failed"
    assert sent == []
    assert "nothing was sent" in capsys.readouterr().out


@pytest.mark.parametrize("job", ["_digest", "_alerts"])
def test_email_jobs_warn_when_no_redis_is_configured(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any, job: str
) -> None:
    """Round-2 finding. Turns red if: a job skips the re-apply silently."""
    import asyncio
    from types import SimpleNamespace

    from app.worker import cli

    _cli_user(tmp_path, monkeypatch)

    async def fake_send(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(
            changes=0, sent=0, failed=0, skipped_already_sent=0, skipped_not_pro=0, capped=False
        )

    monkeypatch.setattr(deletion_list, "current", lambda: None)
    monkeypatch.setattr("app.services.digest.send_weekly_digest", fake_send)
    monkeypatch.setattr("app.services.alerts.send_watch_alerts", fake_send)
    assert asyncio.run(getattr(cli, job)(object(), object())) == 0
    assert "CITEVYN_REDIS_URL is not set" in capsys.readouterr().out
