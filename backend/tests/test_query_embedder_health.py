"""The query embedder's account health: noticing an empty prepaid balance.

When the embedding provider refuses the ACCOUNT (an empty prepaid balance is a
402; a bad or revoked key is a 401/403, or on Gemini a 400 naming the key), every
query embedding fails, the vector arm degrades and search drops to keyword-only
while answers keep coming, with nothing to show for it but a log line. Each
refusal is now recorded in Redis (shared by every machine, and kept across the
scale-to-zero restarts that would empty process memory), cleared by the next
successful embedding, and reported by ``/health/index`` for the uptime probe.

Whether an error is an account refusal is the provider client's call
(``EmbedderUnavailable.account_refused``); the tracker only reads it.

Hermetic: fakeredis. Every test says which change turns it red.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.embeddings import EmbedderUnavailable, query_health

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
KEY = "test:query_embedder_refused_since"


def _refused(status_code: int = 402) -> EmbedderUnavailable:
    return EmbedderUnavailable("returned", status_code=status_code, account_refused=True)


def _transient(status_code: int | None = 503) -> EmbedderUnavailable:
    return EmbedderUnavailable("returned", status_code=status_code)


@pytest.fixture
async def redis_client() -> Any:
    import fakeredis.aioredis as fake_aioredis

    client = fake_aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def health(redis_client: Any) -> query_health.QueryEmbedderHealth:
    return query_health.QueryEmbedderHealth(redis_client, KEY)


async def test_an_account_refusal_is_recorded(
    health: query_health.QueryEmbedderHealth, redis_client: Any
) -> None:
    """Red when: record_failure stops reading ``account_refused`` or stops
    writing. The key has no expiry: a refusal must not clear itself while the
    balance is still empty (red if ``ex=`` is added)."""
    await health.record_failure(_refused(), now=NOW)

    assert await health.status() == {"state": "refused", "refused_since": NOW.isoformat()}
    assert await redis_client.ttl(KEY) == -1


@pytest.mark.parametrize("status_code", [None, 400, 401, 402, 403, 429, 503])
async def test_an_error_not_marked_as_a_refusal_is_not_recorded(
    health: query_health.QueryEmbedderHealth, status_code: int | None
) -> None:
    """Red when: the tracker decides by status code instead of the provider's
    ``account_refused`` verdict. A 403 from OpenRouter can be one flagged input
    and a 400 one odd question; only the provider knows which statuses mean the
    account. A timeout, a 429 or a 5xx clears on its own."""
    await health.record_failure(_transient(status_code), now=NOW)

    assert await health.status() == {"state": "ok", "refused_since": None}
    # Partner: the same store does record a refusal, so "ok" above is not a
    # store that never writes.
    await health.record_failure(_refused(), now=NOW)
    assert (await health.status())["state"] == "refused"


async def test_refused_since_keeps_the_first_refusal(
    health: query_health.QueryEmbedderHealth,
) -> None:
    """Red when: the write overwrites (drops ``nx``). The operator needs when it
    STARTED, not the latest request."""
    await health.record_failure(_refused(), now=NOW)
    await health.record_failure(_refused(), now=NOW + timedelta(hours=1))

    assert (await health.status())["refused_since"] == NOW.isoformat()


async def test_a_successful_embedding_clears_the_refusal(
    health: query_health.QueryEmbedderHealth,
) -> None:
    """Red when: record_success stops deleting the key, so a topped-up balance
    keeps paging."""
    await health.record_failure(_refused(), now=NOW)
    assert (await health.status())["state"] == "refused"

    await health.record_success()

    assert await health.status() == {"state": "ok", "refused_since": None}


class _BrokenRedis:
    """Every call fails with ``exc``, the way an unreachable Redis does."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __getattr__(self, name: str) -> Any:
        async def _fail(*args: Any, **kwargs: Any) -> Any:
            raise self._exc

        return _fail


def _redis_failures() -> list[BaseException]:
    import redis.exceptions

    return [
        redis.exceptions.ConnectionError("redis://user:secret@host:6379 refused"),
        redis.exceptions.TimeoutError("redis://user:secret@host:6379 timed out"),
        OSError("redis://user:secret@host:6379 unreachable"),
        # A non-UTF-8 value under the key fails to decode on read.
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "redis://user:secret@host bad byte"),
        # Not a Redis error at all (an event-loop mix-up, a client bug): still
        # telemetry, so still never allowed to fail the request.
        RuntimeError("redis://user:secret@host attached to a different loop"),
    ]


@pytest.mark.parametrize("exc", _redis_failures(), ids=lambda e: type(e).__name__)
async def test_a_redis_failure_never_breaks_the_request_and_reports_unavailable(
    exc: BaseException, caplog
) -> None:
    """Red when: an error escapes record_failure/record_success/status (it would
    turn a degraded answer, or the health route, into a 500), or status() claims
    "ok" when it could not read anything. Each failed call is logged by its
    TYPE only: a Redis error can echo its URL, password included."""
    health = query_health.QueryEmbedderHealth(_BrokenRedis(exc), KEY)

    with caplog.at_level(logging.WARNING, logger="citevyn.embeddings"):
        await health.record_failure(_refused(), now=NOW)
        await health.record_success()
        result = await health.status()

    assert result == {"state": "unavailable", "refused_since": None}
    assert all("secret" not in str(rec.__dict__) for rec in caplog.records)
    unavailable = [
        r for r in caplog.records if r.getMessage() == "query_embedder_health_unavailable"
    ]
    assert [r.__dict__["error"] for r in unavailable] == [type(exc).__name__] * 3


class _SlowRedis:
    """Accepts every call and never answers, like a stalled Redis."""

    def __getattr__(self, name: str) -> Any:
        async def _hang(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(60)

        return _hang


async def test_a_stalled_redis_costs_at_most_the_call_ceiling(monkeypatch) -> None:
    """Red when: a tracker call waits on Redis without its own ceiling. Every
    successful query now touches Redis, so a stalled Redis would otherwise add
    the client's 5 s socket timeout to every answer."""
    monkeypatch.setattr(query_health, "REDIS_CALL_CEILING_SECONDS", 0.05)
    health = query_health.QueryEmbedderHealth(_SlowRedis(), KEY)

    started = asyncio.get_running_loop().time()
    await health.record_success()
    result = await health.status()
    elapsed = asyncio.get_running_loop().time() - started

    assert result["state"] == "unavailable"
    assert elapsed < 1.0


def test_the_call_ceiling_is_short() -> None:
    """Red when: the ceiling is raised toward the client's own 5 s timeout,
    which would make the ceiling pointless on the answer path."""
    assert 0 < query_health.REDIS_CALL_CEILING_SECONDS <= 0.5


async def test_a_malformed_refusal_time_is_not_passed_on(
    health: query_health.QueryEmbedderHealth, redis_client: Any
) -> None:
    """Red when: status() passes on whatever string sits under the key. The
    uptime probe prints refused_since into a GitHub Actions log, where a line
    starting ``::`` is a workflow command."""
    await redis_client.set(KEY, "::error::forged")

    assert await health.status() == {"state": "refused", "refused_since": None}


async def test_a_refusal_is_logged_as_an_error_with_its_status(
    health: query_health.QueryEmbedderHealth, caplog
) -> None:
    """Red when: the refusal stops being logged at ERROR, or loses its status
    code. The log is where an operator sees WHICH refusal it was; the public
    health route deliberately does not say."""
    with caplog.at_level(logging.WARNING, logger="citevyn.embeddings"):
        await health.record_failure(_refused(402), now=NOW)
        await health.record_failure(_transient(503), now=NOW)

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
    shared_client = object()
    monkeypatch.setattr(query_health, "get_redis_client", lambda: shared_client)

    tracker = query_health.current()

    assert tracker is not None
    assert tracker.key == "env1:query_embedder_refused_since"
    # The process-wide client, not a new connection per request.
    assert tracker._client is shared_client


async def test_without_redis_the_helpers_are_no_ops_and_report_unconfigured(
    monkeypatch,
) -> None:
    """Red when: the module helpers assume a tracker exists, or report "ok" when
    nothing is tracked. Production always sets Redis, so the probe pages on
    "unconfigured": a lost Redis URL must not switch this alert off silently."""
    monkeypatch.setattr(query_health, "current", lambda: None)

    await query_health.record_failure(_refused())
    await query_health.record_success()

    assert await query_health.status() == {"state": "unconfigured", "refused_since": None}


async def test_the_module_helpers_use_the_current_tracker(
    health: query_health.QueryEmbedderHealth, monkeypatch
) -> None:
    """Red when: the module helpers stop routing to current()'s tracker."""
    monkeypatch.setattr(query_health, "current", lambda: health)

    await query_health.record_failure(_refused())
    assert (await query_health.status())["state"] == "refused"

    await query_health.record_success()
    assert (await query_health.status())["state"] == "ok"
