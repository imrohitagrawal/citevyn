"""The query embedder's self-check: noticing a refused account with no visitors.

The refusal tracker (``app.embeddings.query_health``) learns only from real
queries that miss the answer cache, so with no traffic an empty prepaid balance
is never noticed, and after a top-up the alert keeps paging until a new
question gets through. So ``/health/index`` (which the uptime probe already
calls, and which therefore already wakes the machine) makes ONE tiny embedding
call at most once per interval app-wide, guarded by a Redis lock, and feeds the
result to the same tracker.

Hermetic: fakeredis and fake embedders; no provider is called. Every test says
which change turns it red.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.core.config import Settings
from app.cost.call_site import CallSite, get_call_site
from app.embeddings import EmbedderUnavailable, query_health
from app.llm.errors import CostLimitReached

KEY = "test:query_embedder_refused_since"
LOCK = "test:query_embedder_self_check"
WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "uptime.yml"


@pytest.fixture
async def redis_client() -> Any:
    import fakeredis.aioredis as fake_aioredis

    client = fake_aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def app_with_session(session: Any) -> Any:
    """The app with ``get_session`` bound to the migrated test session."""
    from app.core import db as db_module
    from app.main import create_app

    app = create_app()

    async def _override() -> Any:
        yield session

    app.dependency_overrides[db_module.get_session] = _override
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def tracker(redis_client: Any) -> query_health.QueryEmbedderHealth:
    return query_health.QueryEmbedderHealth(redis_client, KEY, self_check_key=LOCK)


class _FakeEmbedder:
    """Counts calls, records the spend label each ran under, then acts."""

    def __init__(self, outcome: BaseException | None = None) -> None:
        self.calls = 0
        self.sites: list[CallSite] = []
        self.closed = False
        self._outcome = outcome

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        self.sites.append(get_call_site())
        if self._outcome is not None:
            raise self._outcome
        return [0.0, 1.0]

    async def aclose(self) -> None:
        self.closed = True


def _settings(interval: int = 10800) -> Settings:
    return Settings(redis_url=None, query_embedder_self_check_interval_seconds=interval)


async def _run(
    tracker: query_health.QueryEmbedderHealth, embedder: _FakeEmbedder, interval: int = 10800
) -> None:
    await query_health.run_self_check(
        tracker, settings=_settings(interval), build=lambda _settings: embedder
    )


# --- the setting -------------------------------------------------------------


def test_the_interval_defaults_to_three_hours() -> None:
    """Red when: the default drifts from the owner's decision (every 3 hours)."""
    assert Settings().query_embedder_self_check_interval_seconds == 10800


@pytest.mark.parametrize("value", [0, 600, 3600, 86400])
def test_the_interval_accepts_off_or_ten_minutes_and_up(value: int) -> None:
    """Red when: 0 (off) or a sane interval is rejected."""
    assert _settings(value).query_embedder_self_check_interval_seconds == value


@pytest.mark.parametrize("value", [-1, 1, 599])
def test_the_interval_refuses_anything_shorter_than_ten_minutes(value: int) -> None:
    """Red when: a short interval is accepted. Each check is a paid call; a
    typo like 10 must fail at startup, not spend every probe."""
    with pytest.raises(ValidationError):
        _settings(value)


def test_the_interval_is_read_from_the_environment(monkeypatch) -> None:
    """Red when: the field is renamed and the documented variable stops
    reaching it (the setting would silently stay at its default)."""
    monkeypatch.setenv("CITEVYN_QUERY_EMBEDDER_SELF_CHECK_INTERVAL_SECONDS", "3600")
    assert Settings().query_embedder_self_check_interval_seconds == 3600


# --- the lock ----------------------------------------------------------------


async def test_one_check_per_interval_however_often_the_route_is_called(
    tracker: query_health.QueryEmbedderHealth, redis_client: Any
) -> None:
    """Red when: the lock stops guarding the call. The route is public, so
    anyone could otherwise trigger a paid call per request."""
    embedder = _FakeEmbedder()
    for _ in range(5):
        await _run(tracker, embedder)

    assert embedder.calls == 1
    # The interval reaches the lock's expiry.
    assert 10790 <= await redis_client.ttl(LOCK) <= 10800


async def test_a_new_interval_allows_a_new_check(
    tracker: query_health.QueryEmbedderHealth, redis_client: Any
) -> None:
    """Red when: the lock never expires (one check per Redis lifetime). Partner
    of the test above."""
    embedder = _FakeEmbedder()
    await _run(tracker, embedder)
    await redis_client.delete(LOCK)
    await _run(tracker, embedder)

    assert embedder.calls == 2


async def test_interval_zero_switches_it_off(tracker: query_health.QueryEmbedderHealth) -> None:
    """Red when: 0 does not disable the check (the off switch for a paid call
    on a public route)."""
    embedder = _FakeEmbedder()
    await _run(tracker, embedder, interval=0)

    assert embedder.calls == 0


async def test_without_redis_there_is_no_check(monkeypatch) -> None:
    """Red when: the check runs with no Redis, where there is no lock to bound
    it and no tracker to record into."""
    embedder = _FakeEmbedder()
    monkeypatch.setattr(query_health, "current", lambda: None)

    await query_health.maybe_self_check(settings=_settings(), build=lambda _s: embedder)

    assert embedder.calls == 0


class _BrokenRedis:
    def __getattr__(self, name: str) -> Any:
        async def _fail(*args: Any, **kwargs: Any) -> Any:
            import redis.exceptions

            raise redis.exceptions.ConnectionError("down")

        return _fail


async def test_an_unwritable_lock_means_no_check() -> None:
    """Red when: a lock that could not be written is treated as won. On spend,
    fail closed: no lock, no paid call."""
    embedder = _FakeEmbedder()
    tracker = query_health.QueryEmbedderHealth(_BrokenRedis(), KEY, self_check_key=LOCK)

    await _run(tracker, embedder)

    assert embedder.calls == 0


# --- the outcome -------------------------------------------------------------


async def test_a_successful_check_clears_a_recorded_refusal(
    tracker: query_health.QueryEmbedderHealth,
) -> None:
    """Red when: the check's success is not passed to the tracker, so a
    topped-up balance keeps paging until a visitor asks a new question."""
    refused = EmbedderUnavailable("x", status_code=402, account_refused=True)
    await tracker.record_failure(refused, now=query_health.datetime.now(query_health.UTC))
    assert (await tracker.status())["state"] == "refused"

    await _run(tracker, _FakeEmbedder())

    assert (await tracker.status())["state"] == "ok"


async def test_a_refused_check_is_recorded(tracker: query_health.QueryEmbedderHealth) -> None:
    """Red when: the check's refusal is not recorded, so an empty balance with
    no visitors is never noticed."""
    await _run(
        tracker, _FakeEmbedder(EmbedderUnavailable("x", status_code=402, account_refused=True))
    )

    assert (await tracker.status())["state"] == "refused"


@pytest.mark.parametrize(
    "outcome",
    [
        EmbedderUnavailable("x", status_code=503),
        EmbedderUnavailable("x"),
        CostLimitReached("daily cap"),
        RuntimeError("anything else"),
    ],
    ids=["transient", "timeout", "daily-cap", "other"],
)
async def test_an_unsettled_check_records_nothing_and_never_raises(
    tracker: query_health.QueryEmbedderHealth, outcome: BaseException
) -> None:
    """Red when: a check that proved nothing changes the state, or raises into
    the health route. The daily spending cap blocking the check is one such
    outcome: it says nothing about the provider account."""
    await _run(tracker, _FakeEmbedder(outcome))

    assert (await tracker.status())["state"] == "ok"


async def test_the_check_is_labelled_as_self_check_spend(
    tracker: query_health.QueryEmbedderHealth,
) -> None:
    """Red when: the call runs without its own spend label and is recorded as
    an answer (the metered embedder's default for a query)."""
    embedder = _FakeEmbedder()
    await _run(tracker, embedder)

    assert embedder.sites == [CallSite.self_check]


async def test_the_check_closes_the_embedder_it_built(
    tracker: query_health.QueryEmbedderHealth,
) -> None:
    """Red when: the ping's own HTTP client is left open (one leaked pool every
    interval)."""
    embedder = _FakeEmbedder(EmbedderUnavailable("x", status_code=503))
    await _run(tracker, embedder)

    assert embedder.closed


def test_the_ping_has_a_short_timeout_and_no_retries() -> None:
    """Red when: the ping inherits the live embedder's 15 s timeout and its
    retries. Its worst case must stay inside the uptime probe's 45 s curl
    limit, and it is bounded at the provider request, so a slow cost write is
    never cut off after the provider has answered (and billed)."""
    ping = query_health.ping_settings(Settings(embedding_timeout_seconds=15.0))

    assert ping.embedding_timeout_seconds == query_health.SELF_CHECK_TIMEOUT_SECONDS
    assert ping.embedding_max_retries == 0
    assert 0 < query_health.SELF_CHECK_TIMEOUT_SECONDS <= 10


async def test_the_check_starts_no_background_work(
    tracker: query_health.QueryEmbedderHealth,
) -> None:
    """Red when: the check leaves a task or timer running. It must only ride a
    request that is already happening: anything scheduled would keep the
    scale-to-zero machine awake (a cost the owner ruled out)."""
    before = asyncio.all_tasks()
    await _run(tracker, _FakeEmbedder())
    await asyncio.sleep(0)

    assert asyncio.all_tasks() == before


# --- the route ---------------------------------------------------------------


def test_health_index_checks_before_it_reports(app_with_session, monkeypatch) -> None:
    """Red when: the route reports the state before (or without) running the
    check, so the probe that triggers a check sees the previous result."""
    order: list[str] = []

    async def _check(**_kwargs: Any) -> None:
        order.append("check")

    async def _status() -> dict[str, str | None]:
        order.append("status")
        return {"state": "ok", "refused_since": None}

    monkeypatch.setattr(query_health, "maybe_self_check", _check)
    monkeypatch.setattr(query_health, "status", _status)
    from fastapi.testclient import TestClient

    with TestClient(app_with_session) as client:
        assert client.get("/health/index").status_code == 200

    assert order == ["check", "status"]


# --- the workflow keeps itself enabled ---------------------------------------


def test_the_uptime_workflow_keeps_itself_enabled() -> None:
    """Red when: the keep-enabled job is removed or loses its permission.
    GitHub disables a public repository's scheduled workflows after 60 days
    with no activity, which would stop this alert without a word."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    jobs = doc["jobs"]
    keep = [j for j in jobs.values() if "actions/workflows/uptime.yml/enable" in str(j)]
    assert len(keep) == 1
    job = keep[0]
    assert job.get("permissions") == {"actions": "write"}
    assert "schedule" in job.get("if", "")
    # Partner: the probe jobs keep read-only permissions.
    others = [j for j in jobs.values() if j is not job]
    assert others
    assert all(j.get("permissions") is None for j in others)
    assert doc["permissions"] == {"contents": "read"}
