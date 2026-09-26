"""The answer allowance (ADR-0005 §1 and §2 "Quota", Phase 4B-2).

Locked decisions pinned here:

* Free: ``access_free_trial_answers`` (25) answered questions ONCE, per VERIFIED
  account. An unverified account has none.
* Pro: ``access_pro_monthly_answers`` (1,000) answered questions per calendar UTC
  month, on top of the existing hourly limit.
* Only ANSWERED questions count: a cited answer, fresh or from the cache. A
  refusal, a no-answer, a greeting or an error never uses allowance.
* BYOK answers (``paid_by='user'``, Phase 8) never use the platform allowance.
* With ``CITEVYN_ACCESS_MODEL_ENABLED`` off nothing is enforced or recorded.

Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.access.policy import Tier
from app.access.quota import is_counted_answer, month_start, usage_for
from app.core import db as db_module
from app.core.config import Settings, get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import AnswerUsage, Base, IndexStatus, IndexVersion, Membership, User
from tests.conftest import seed_catalog

DEMO = {"Authorization": "Bearer local-demo-key"}
ON = {"CITEVYN_ACCESS_MODEL_ENABLED": "true", "CITEVYN_ACCESS_FREE_TRIAL_ANSWERS": "3"}
QUESTION = "How do I configure Claude Code permissions?"
REFUSED = "What is the best laptop to buy?"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# What counts
# ---------------------------------------------------------------------------


def _response(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "no_answer": False,
        "unsupported": False,
        "intent": "how_to",
        "citations": [{"marker": 1}],
        "cache_hit": False,
    }
    base.update(kw)
    return base


def test_a_cited_answer_counts_fresh_or_cached() -> None:
    """Turns red if: cache hits are skipped (they are real cited answers)."""
    assert is_counted_answer(_response())
    assert is_counted_answer(_response(cache_hit=True))


@pytest.mark.parametrize(
    "kw",
    [
        {"no_answer": True, "citations": []},
        {"unsupported": True, "no_answer": True, "citations": []},
        {"intent": "greeting", "citations": []},
        {"citations": []},  # an answer with no citation is not an answer we count
        {"no_answer": "false"},  # only the boolean False means answered
    ],
)
def test_refusals_greetings_and_uncited_replies_never_count(kw: dict[str, Any]) -> None:
    """Turns red if: any of these conditions is dropped from is_counted_answer."""
    assert not is_counted_answer(_response(**kw))


def test_the_month_starts_at_midnight_utc_on_the_first() -> None:
    """Turns red if: the Pro window is a rolling 30 days or a local-time month."""
    assert month_start(NOW) == datetime(2026, 10, 1, tzinfo=UTC)
    assert month_start(datetime(2026, 10, 1, 0, 0, tzinfo=UTC)) == datetime(2026, 10, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Counting (a live SQLite database)
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Any) -> Generator[Any, None, None]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'q.db'}")

    async def _init() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


ACCOUNT = "usr_" + "a" * 32
OTHER = "usr_" + "b" * 32


def _seed(make: Any, rows: list[tuple[str, datetime, str]], *, verified: bool = True) -> None:
    async def _run() -> None:
        async with make() as s:
            for user in (ACCOUNT, OTHER):
                s.add(
                    User(
                        user_id=user,
                        role="demo_user",
                        created_at=NOW,
                        email=f"{user}@x.com",
                        email_verified_at=NOW if verified else None,
                    )
                )
            await s.flush()  # the users first: answer_usage.user_id references them
            for user, when, paid_by in rows:
                s.add(
                    AnswerUsage(
                        usage_id=uuid.uuid4(),
                        user_id=user,
                        occurred_at=when,
                        paid_by=paid_by,
                        channel="chat",
                    )
                )
            await s.commit()

    asyncio.run(_run())


def _usage(make: Any, tier: Tier, settings: Settings | None = None) -> Any:
    async def _run() -> Any:
        async with make() as s:
            return await usage_for(s, ACCOUNT, tier, settings or Settings(), NOW)

    return asyncio.run(_run())


def test_free_counts_every_answer_ever_and_pro_only_this_month(db: Any) -> None:
    """Turns red if: Free is windowed (the trial would refill) or Pro is not
    (last month's answers would still count)."""
    _seed(
        db,
        [
            (ACCOUNT, NOW - timedelta(days=90), "platform"),
            (ACCOUNT, datetime(2026, 9, 30, 23, 59, tzinfo=UTC), "platform"),
            (ACCOUNT, datetime(2026, 10, 1, 0, 0, tzinfo=UTC), "platform"),
            (ACCOUNT, NOW - timedelta(hours=1), "platform"),
        ],
    )
    free = _usage(db, Tier.free)
    assert (free.used, free.limit, free.remaining, free.resets_at) == (4, 25, 21, None)
    pro = _usage(db, Tier.pro)
    assert (pro.used, pro.limit, pro.remaining) == (2, 1000, 998)
    assert pro.resets_at == datetime(2026, 11, 1, tzinfo=UTC)


def test_byok_and_other_accounts_answers_do_not_count(db: Any) -> None:
    """Turns red if: the paid_by filter or the user filter is dropped."""
    _seed(db, [(ACCOUNT, NOW, "user"), (OTHER, NOW, "platform"), (ACCOUNT, NOW, "platform")])
    assert _usage(db, Tier.free).used == 1


def test_an_unverified_free_account_has_no_trial(db: Any) -> None:
    """ADR-0005 §1: the trial is per VERIFIED account. Turns red if: the limit
    ignores email_verified_at."""
    _seed(db, [], verified=False)
    usage = _usage(db, Tier.free)
    assert (usage.limit, usage.remaining, usage.verified) == (0, 0, False)


def test_the_numbers_are_settings(db: Any) -> None:
    """Turns red if: 25 or 1,000 is hard-coded."""
    _seed(db, [])
    s = Settings(access_free_trial_answers=7, access_pro_monthly_answers=11)
    assert _usage(db, Tier.free, s).limit == 7
    assert _usage(db, Tier.pro, s).limit == 11


# ---------------------------------------------------------------------------
# Through the real answer route
# ---------------------------------------------------------------------------


@pytest.fixture
def app_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Generator[Any, None, None]:
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    get_settings.cache_clear()
    rate_limit.reset_limiter()

    async def _init() -> None:
        async with db_module.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with get_sessionmaker()() as s:
            s.add(
                IndexVersion(
                    index_version="index_v1",
                    status=IndexStatus.active,
                    source_version_hash="sha256:index-v1",
                    created_at=datetime.now(UTC),
                    promoted_at=datetime.now(UTC),
                )
            )
            await s.flush()
            await seed_catalog(s)

    asyncio.run(_init())
    yield monkeypatch
    get_settings.cache_clear()
    db_module.reset_engine()
    rate_limit.reset_limiter()


def _on(mp: pytest.MonkeyPatch) -> None:
    for k, v in ON.items():
        mp.setenv(k, v)
    get_settings.cache_clear()


def _signed_in(client: TestClient, email: str = "a@example.com", *, verified: bool = True) -> str:
    res = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct horse battery"},
        headers=DEMO,
    )
    assert res.status_code == 201
    user_id = str(res.json()["user_id"])
    if verified:
        _set_user(user_id, email_verified_at=datetime.now(UTC))
    return user_id


def _set_user(user_id: str, **fields: Any) -> None:
    async def _run() -> None:
        async with get_sessionmaker()() as s:
            user = await s.get(User, user_id)
            assert user is not None
            for k, v in fields.items():
                setattr(user, k, v)
            await s.commit()

    asyncio.run(_run())


def _make_pro(user_id: str) -> None:
    async def _run() -> None:
        async with get_sessionmaker()() as s:
            s.add(
                Membership(
                    account_id=user_id,
                    stripe_subscription_id="sub_1",
                    plan="pro",
                    status="active",
                    cancel_at_period_end=False,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            await s.commit()

    asyncio.run(_run())


def _add_usage(user_id: str, n: int, when: datetime | None = None) -> None:
    async def _run() -> None:
        async with get_sessionmaker()() as s:
            for _ in range(n):
                s.add(
                    AnswerUsage(
                        usage_id=uuid.uuid4(),
                        user_id=user_id,
                        occurred_at=when or datetime.now(UTC),
                        paid_by="platform",
                        channel="chat",
                    )
                )
            await s.commit()

    asyncio.run(_run())


def _rows(user_id: str) -> list[AnswerUsage]:
    async def _run() -> list[AnswerUsage]:
        async with get_sessionmaker()() as s:
            q = select(AnswerUsage).where(AnswerUsage.user_id == user_id)
            return list((await s.execute(q)).scalars())

    return asyncio.run(_run())


def _ask(client: TestClient, question: str = QUESTION) -> Any:
    session = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO)
    assert session.status_code == 201, session.json()
    return client.post(
        f"/v1/sessions/{session.json()['session_id']}/messages",
        json={"message": question},
        headers=DEMO,
    )


def test_an_answer_uses_one_of_the_trial_and_the_last_one_is_refused(
    app_env: pytest.MonkeyPatch,
) -> None:
    """Trial of 3 (a setting). Answers 1-3 succeed and are recorded; the 4th is
    403 plan_required, so upgrading is what helps. Turns red if: the check is
    off by one (> instead of >=), or an answer is not recorded."""
    _on(app_env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        for _ in range(3):
            assert _ask(client).status_code == 200
        assert len(_rows(user)) == 3
        refused = _ask(client)
    assert refused.status_code == 403
    body = refused.json()["error"]
    assert body["code"] == "plan_required"
    assert body["details"]["reason"] == "trial_used"
    assert body["details"]["used"] == 3 and body["details"]["limit"] == 3
    assert len(_rows(user)) == 3  # a refused request records nothing


def test_a_refusal_uses_no_allowance(app_env: pytest.MonkeyPatch) -> None:
    """Turns red if: every reply is recorded, not only answered questions."""
    _on(app_env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        res = _ask(client, REFUSED)
    assert res.status_code == 200 and res.json()["no_answer"] is True
    assert _rows(user) == []


def test_a_cached_answer_uses_allowance(app_env: pytest.MonkeyPatch) -> None:
    """A cache hit is a cited answer that made no model call; it still counts.
    The route decides from the response alone, so the orchestrator is replaced
    by one that returns a cache hit (the seeded test catalog degrades the vector
    arm, which skips cache writes). Turns red if: cache hits are not recorded."""
    from app.answer.orchestrator import Orchestrator

    async def cached(self: Orchestrator, **kw: object) -> dict[str, object]:
        del self, kw
        return {
            "request_id": "r",
            "message_id": str(uuid.uuid4()),
            "answer": "cached",
            "citations": [{"marker": 1}],
            "no_answer": False,
            "unsupported": False,
            "intent": "how_to",
            "cache_hit": True,
        }

    _on(app_env)
    app_env.setattr(Orchestrator, "ask", cached)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        res = _ask(client)
    assert res.json()["cache_hit"] is True
    [row] = _rows(user)
    assert row.message_id == uuid.UUID(res.json()["message_id"])


def test_an_unverified_account_must_verify_first(app_env: pytest.MonkeyPatch) -> None:
    """Turns red if: an unverified Free account gets the trial."""
    _on(app_env)
    with TestClient(create_app()) as client:
        _signed_in(client, verified=False)
        res = _ask(client)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "verification_required"


def test_pro_has_its_monthly_allowance_and_last_month_does_not_count(
    app_env: pytest.MonkeyPatch,
) -> None:
    """Pro with the monthly allowance set to 2. Last month's answers are ignored;
    at 2 this month the next is 429 quota_exceeded with the reset time. Turns red
    if: Pro uses the trial rule, the window is wrong, or the code is wrong."""
    _on(app_env)
    app_env.setenv("CITEVYN_ACCESS_PRO_MONTHLY_ANSWERS", "2")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        user = _signed_in(client, verified=False)  # Pro does not need the trial rule
        _make_pro(user)
        _add_usage(user, 5, when=month_start(datetime.now(UTC)) - timedelta(seconds=1))
        _add_usage(user, 1)
        assert _ask(client).status_code == 200
        res = _ask(client)
    assert res.status_code == 429
    err = res.json()["error"]
    assert err["code"] == "quota_exceeded"
    assert err["details"]["used"] == 2 and err["details"]["limit"] == 2
    assert err["details"]["resets_at"].startswith(
        (month_start(datetime.now(UTC)) + timedelta(days=32)).strftime("%Y-%m")
    )


def test_with_the_setting_off_nothing_is_enforced_or_recorded(
    app_env: pytest.MonkeyPatch,
) -> None:
    """Turns red if: the allowance runs while the access model is off."""
    app_env.setenv("CITEVYN_ACCESS_FREE_TRIAL_ANSWERS", "0")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        user = _signed_in(client, verified=False)
        assert _ask(client).status_code == 200
    assert _rows(user) == []


def test_the_membership_view_reports_the_allowance(app_env: pytest.MonkeyPatch) -> None:
    """Turns red if: the view does not report used and remaining from the rows."""
    _on(app_env)
    for k, v in {
        "CITEVYN_STRIPE_SECRET_KEY": "sk_test_x",
        "CITEVYN_STRIPE_WEBHOOK_SECRET": "whsec_x",
        "CITEVYN_STRIPE_PRICE_PRO_MONTHLY": "price_m",
        "CITEVYN_STRIPE_PRICE_PRO_YEARLY": "price_y",
    }.items():
        app_env.setenv(k, v)
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _add_usage(user, 2)
        view = client.get("/v1/billing/membership", headers=DEMO).json()
    assert view["usage"] == {
        "kind": "trial",
        "used": 2,
        "limit": 3,
        "remaining": 1,
        "resets_at": None,
        "verified": True,
    }
