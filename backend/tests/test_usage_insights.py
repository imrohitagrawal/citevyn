"""Usage insights (ADR-0005 §4, Phase 6D): "a page showing the user's own use
against their allowance".

``GET /v1/me/usage`` is Pro (capability ``usage_insights``) and 404 unless
``CITEVYN_ACCESS_MODEL_ENABLED`` is on. It returns the allowance position (the
same numbers the meter shows) and one row per day of the current period, split
by channel. The days count EXACTLY what the allowance counts, so they always add
up to ``usage.used``.

Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.access.quota import month_start
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import AnswerUsage, Membership
from tests.test_answer_allowance import (  # noqa: F401 (app_env is a fixture)
    DEMO,
    _make_pro,
    _on,
    _signed_in,
    app_env,
)


@pytest.fixture
def env(app_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:  # noqa: F811 (the imported fixture)
    """The answer-allowance test's app: a seeded catalog on a fresh SQLite file."""
    return app_env


def _add(
    user_id: str,
    when: datetime,
    *,
    channel: str = "chat",
    tier: str = "pro",
    paid_by: str = "platform",
    n: int = 1,
) -> None:
    async def _run() -> None:
        async with get_sessionmaker()() as s:
            for _ in range(n):
                s.add(
                    AnswerUsage(
                        usage_id=uuid.uuid4(),
                        user_id=user_id,
                        occurred_at=when,
                        paid_by=paid_by,
                        channel=channel,
                        tier=tier,
                    )
                )
            await s.commit()

    asyncio.run(_run())


def _lapse(user_id: str) -> None:
    from sqlalchemy import select

    async def _run() -> None:
        async with get_sessionmaker()() as s:
            for m in (await s.execute(select(Membership))).scalars():
                if m.account_id == user_id:
                    m.status = "canceled"
            await s.commit()

    asyncio.run(_run())


def _get(client: TestClient) -> object:
    return client.get("/v1/me/usage", headers=DEMO)


def test_with_the_setting_off_there_is_no_usage_page(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the route answers while the access model is off."""
    with TestClient(create_app()) as client:
        _signed_in(client)
        assert client.get("/v1/me/usage", headers=DEMO).status_code == 404


def test_a_free_account_is_told_it_is_pro(env: pytest.MonkeyPatch) -> None:
    """Turns red if: usage_insights is granted to Free."""
    _on(env)
    with TestClient(create_app()) as client:
        _signed_in(client)
        res = client.get("/v1/me/usage", headers=DEMO)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "plan_required"


def test_a_lapsed_pro_account_loses_the_page(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the capability check reads a stale tier."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        assert client.get("/v1/me/usage", headers=DEMO).status_code == 200
        _lapse(user)
        assert client.get("/v1/me/usage", headers=DEMO).status_code == 403


def test_days_count_exactly_what_the_allowance_counts(env: pytest.MonkeyPatch) -> None:
    """Every day of the period, zero-filled, split by channel; rows the allowance
    does not count (Free-tier, last month, paid by the user) are left out, so the
    days add up to usage.used. Turns red if: a filter is dropped, a day is
    missing, or chat and MCP are mixed up."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        now = datetime.now(UTC)
        start = month_start(now)
        _add(user, now, channel="chat", n=2)
        _add(user, start + timedelta(minutes=1), channel="mcp")
        _add(user, now, tier="free")  # a trial answer earlier this month
        _add(user, start - timedelta(minutes=1))  # last month
        _add(user, now, paid_by="user")  # BYOK: not the platform's allowance
        other = _signed_in(TestClient(create_app()), "b@example.com")
        _add(other, now, n=5)  # someone else

        body = client.get("/v1/me/usage", headers=DEMO).json()

    days = body["days"]
    assert [d["date"] for d in days] == [
        (start + timedelta(days=i)).date().isoformat() for i in range((now - start).days + 1)
    ]
    by_day = {d["date"]: (d["chat"], d["mcp"]) for d in days}
    today, first = now.date().isoformat(), start.date().isoformat()
    if today == first:
        assert by_day[today] == (2, 1)
    else:
        assert by_day[today] == (2, 0)
        assert by_day[first] == (0, 1)
    assert body["totals"] == {"chat": 2, "mcp": 1, "total": 3}
    assert body["usage"]["used"] == 3 == sum(d["chat"] + d["mcp"] for d in days)
    assert body["usage"]["kind"] == "monthly"
    assert body["period_start"] == start.isoformat()


def test_a_new_pro_account_sees_an_empty_month(env: pytest.MonkeyPatch) -> None:
    """Turns red if: no use gives an error or no days instead of zeros."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        body = client.get("/v1/me/usage", headers=DEMO).json()
    assert body["totals"] == {"chat": 0, "mcp": 0, "total": 0}
    assert body["days"] and all(d["chat"] == d["mcp"] == 0 for d in body["days"])
    assert body["usage"]["remaining"] == body["usage"]["limit"] > 0
