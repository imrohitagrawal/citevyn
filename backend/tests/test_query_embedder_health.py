"""The query embedder's account health: noticing an empty prepaid balance.

When the embedding provider refuses the ACCOUNT (an empty prepaid balance is a
402, a bad or revoked key a 401/403), every query embedding fails, the vector
arm degrades and search drops to keyword-only while answers keep coming. That
happened in production with nothing to show for it but a log line. Each refusal
is now recorded in Redis (shared by every machine, and kept across the
scale-to-zero restarts that would empty process memory), cleared by the next
successful embedding, and reported by ``/health/index`` for the uptime probe.

Hermetic: fakeredis. Every test says which change turns it red.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.embeddings import query_health

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
KEY = "test:query_embedder_refused_since"


@pytest.fixture
async def health() -> Any:
    import fakeredis.aioredis as fake_aioredis

    client = fake_aioredis.FakeRedis(decode_responses=True)
    yield query_health.QueryEmbedderHealth(client, KEY)
    await client.aclose()


@pytest.mark.parametrize("status_code", [401, 402, 403])
async def test_account_refusal_is_recorded(
    health: query_health.QueryEmbedderHealth, status_code: int
) -> None:
    """Red when: a status is dropped from ACCOUNT_REFUSAL_STATUSES (402 is the
    empty-balance case this exists for), or record_failure stops writing."""
    await health.record_failure(status_code, now=NOW)

    assert await health.status() == {"state": "refused", "refused_since": NOW.isoformat()}


@pytest.mark.parametrize("status_code", [None, 400, 404, 408, 429, 500, 503])
async def test_transient_or_request_errors_are_not_recorded(
    health: query_health.QueryEmbedderHealth, status_code: int | None
) -> None:
    """Red when: record_failure records every failure. A timeout, a rate limit,
    a 5xx or a 400 for one odd input clears on its own or is not the account's
    fault; paging on them would make the alert noise."""
    await health.record_failure(status_code, now=NOW)

    assert await health.status() == {"state": "ok", "refused_since": None}
    # Partner: the same instance does record a refusal, so "ok" above is not a
    # store that never writes.
    await health.record_failure(402, now=NOW)
    assert (await health.status())["state"] == "refused"


async def test_refused_since_keeps_the_first_refusal(
    health: query_health.QueryEmbedderHealth,
) -> None:
    """Red when: the write overwrites (drops ``nx``). The operator needs when it
    STARTED, not the latest request."""
    await health.record_failure(402, now=NOW)
    await health.record_failure(402, now=NOW + timedelta(hours=1))

    assert (await health.status())["refused_since"] == NOW.isoformat()


async def test_a_successful_embedding_clears_the_refusal(
    health: query_health.QueryEmbedderHealth,
) -> None:
    """Red when: record_success stops deleting the key, so a topped-up balance
    keeps paging."""
    await health.record_failure(402, now=NOW)
    assert (await health.status())["state"] == "refused"

    await health.record_success()

    assert await health.status() == {"state": "ok", "refused_since": None}


class _BrokenRedis:
    """Every call fails the way an unreachable Redis does."""

    def __getattr__(self, name: str) -> Any:
        import redis.exceptions

        async def _fail(*args: Any, **kwargs: Any) -> Any:
            raise redis.exceptions.ConnectionError("redis://user:secret@host:6379 refused")

        return _fail


async def test_redis_failure_never_breaks_the_request_and_reports_unknown(caplog) -> None:
    """Red when: a Redis error escapes record_failure/record_success (it would
    turn a degraded answer into a 500), or status() claims "ok" when it could
    not read anything. The log carries the error type only: a Redis error can
    echo its URL, password included."""
    health = query_health.QueryEmbedderHealth(_BrokenRedis(), KEY)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="citevyn.embeddings"):
        await health.record_failure(402, now=NOW)
        await health.record_success()
        result = await health.status()

    assert result == {"state": "unknown", "refused_since": None}
    assert all("secret" not in str(rec.__dict__) for rec in caplog.records)
    assert any(rec.getMessage() == "query_embedder_health_unavailable" for rec in caplog.records)


async def test_a_refusal_is_logged_as_an_error(
    health: query_health.QueryEmbedderHealth, caplog
) -> None:
    """Red when: the refusal stops being logged at ERROR, or loses its status
    code. The log is where an operator sees WHICH refusal it was; the public
    health route deliberately does not say."""
    with caplog.at_level(logging.WARNING, logger="citevyn.embeddings"):
        await health.record_failure(402, now=NOW)
        await health.record_failure(503, now=NOW)

    refused = [r for r in caplog.records if r.getMessage() == "query_embedder_account_refused"]
    assert len(refused) == 1
    assert refused[0].levelno == logging.ERROR
    assert refused[0].__dict__["status_code"] == 402


def test_no_redis_configured_means_no_tracker(monkeypatch) -> None:
    """Red when: current() builds a tracker without a Redis URL (local runs and
    tests have none; it would fail on every request)."""
    from app.core.config import Settings

    monkeypatch.setattr(query_health, "get_settings", lambda: Settings(redis_url=None))

    assert query_health.current() is None


def test_a_redis_url_gives_a_tracker_under_the_configured_prefix(monkeypatch) -> None:
    """Red when: current() ignores the Redis URL (the partner of the test above)
    or stops scoping its key by redis_key_prefix, so two environments sharing
    one Redis would page for each other."""
    from app.core.config import Settings

    settings = Settings(redis_url="redis://localhost:6379/0", redis_key_prefix="env1")
    monkeypatch.setattr(query_health, "get_settings", lambda: settings)

    tracker = query_health.current()

    assert tracker is not None
    assert tracker.key == "env1:query_embedder_refused_since"


async def test_module_helpers_without_redis_are_no_ops_and_report_unknown(monkeypatch) -> None:
    """Red when: the module helpers assume a tracker exists, or report "ok"
    when nothing is tracked (that would hide a refusal behind a missing Redis)."""
    monkeypatch.setattr(query_health, "current", lambda: None)

    await query_health.record_failure(402)
    await query_health.record_success()

    assert await query_health.status() == {"state": "unknown", "refused_since": None}


async def test_module_helpers_use_the_current_tracker(
    health: query_health.QueryEmbedderHealth, monkeypatch
) -> None:
    """Red when: the module helpers stop routing to current()'s tracker."""
    monkeypatch.setattr(query_health, "current", lambda: health)

    await query_health.record_failure(402)
    assert (await query_health.status())["state"] == "refused"

    await query_health.record_success()
    assert (await query_health.status())["state"] == "ok"
