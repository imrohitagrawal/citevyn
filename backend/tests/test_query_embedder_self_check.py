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


@pytest.mark.parametrize("value", [0, 600, 3600, 86400, 604800])
def test_the_interval_accepts_off_or_ten_minutes_and_up(value: int) -> None:
    """Red when: 0 (off) or a sane interval is rejected."""
    assert _settings(value).query_embedder_self_check_interval_seconds == value


@pytest.mark.parametrize("value", [-1, 1, 599, 604801])
def test_the_interval_refuses_anything_shorter_than_ten_minutes(value: int) -> None:
    """Red when: a short interval is accepted (each check is a paid call; a
    typo like 10 must fail at startup, not spend every probe), or one longer
    than 7 days is (Redis refuses that expiry, so the check would never run)."""
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
    on a public route). It must not even try the lock: Redis refuses an
    expiry of 0, so without the guard the claim would fail every probe and log
    a misleading Redis warning."""
    embedder = _FakeEmbedder()
    claims: list[int] = []
    original = tracker.claim_self_check

    async def _spy(interval_seconds: int) -> bool:
        claims.append(interval_seconds)
        return await original(interval_seconds)

    tracker.claim_self_check = _spy  # type: ignore[method-assign]
    await _run(tracker, embedder, interval=0)

    assert embedder.calls == 0
    assert claims == []


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


# --- round-1 review: what the first version got wrong or left untested -------


async def _refused(tracker: query_health.QueryEmbedderHealth) -> str:
    since = query_health.datetime(2026, 10, 30, 9, 0, tzinfo=query_health.UTC)
    await tracker.record_failure(
        EmbedderUnavailable("x", status_code=402, account_refused=True), now=since
    )
    return since.isoformat()


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
async def test_an_unsettled_check_never_clears_a_refusal(
    tracker: query_health.QueryEmbedderHealth, outcome: BaseException
) -> None:
    """Red when: an outcome that proved nothing falls through to the success
    path. The alert would stop while the account is still refused."""
    since = await _refused(tracker)

    await _run(tracker, _FakeEmbedder(outcome))

    assert await tracker.status() == {"state": "refused", "refused_since": since}


async def test_an_embedder_that_cannot_be_built_never_fails_the_route(
    tracker: query_health.QueryEmbedderHealth, caplog
) -> None:
    """Red when: building the ping embedder can raise into the health route
    (for example a missing provider key: startup does not check it, so such a
    deploy boots and /health/index would answer 500 once per interval)."""

    def _broken(_settings: Settings) -> Any:
        raise RuntimeError("CITEVYN_GEMINI_API_KEY is required")

    with caplog.at_level("INFO", logger="citevyn.embeddings"):
        await query_health.run_self_check(tracker, settings=_settings(), build=_broken)

    assert (await tracker.status())["state"] == "ok"
    outcomes = [r for r in caplog.records if r.getMessage() == "query_embedder_self_check"]
    assert [(r.__dict__["outcome"], r.__dict__["error"]) for r in outcomes] == [
        ("unsettled", "RuntimeError")
    ]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (None, ("ok", None, None)),
        (
            EmbedderUnavailable("x", status_code=402, account_refused=True),
            ("refused", "EmbedderUnavailable", 402),
        ),
        (EmbedderUnavailable("x", status_code=429), ("unsettled", "EmbedderUnavailable", 429)),
        (EmbedderUnavailable("x"), ("unsettled", "EmbedderUnavailable", None)),
        (CostLimitReached("cap"), ("unsettled", "CostLimitReached", None)),
    ],
    ids=["ok", "refused", "rate-limited", "timeout", "daily-cap"],
)
async def test_every_check_logs_one_outcome_line(
    tracker: query_health.QueryEmbedderHealth,
    caplog,
    outcome: BaseException | None,
    expected: tuple[str, str | None, int | None],
) -> None:
    """Red when: a check that runs leaves no outcome line. A self-check that
    times out every interval would otherwise be invisible."""
    with caplog.at_level("INFO", logger="citevyn.embeddings"):
        await _run(tracker, _FakeEmbedder(outcome))

    lines = [r for r in caplog.records if r.getMessage() == "query_embedder_self_check"]
    assert [
        (r.__dict__["outcome"], r.__dict__["error"], r.__dict__["status_code"]) for r in lines
    ] == [expected]


@pytest.mark.parametrize("outcome", [None, EmbedderUnavailable("x", status_code=503)])
async def test_the_ping_client_is_closed_on_every_path(
    tracker: query_health.QueryEmbedderHealth, outcome: BaseException | None
) -> None:
    """Red when: the client is closed only on one path (success is the common
    one: a leaked pool every interval)."""
    embedder = _FakeEmbedder(outcome)
    await _run(tracker, embedder)

    assert embedder.closed


async def test_the_real_ping_embedder_is_metered_short_and_unretried() -> None:
    """Red when: the production builder builds from the live settings (15 s,
    retries), skips the meter, or leaves its HTTP client open. Every other test
    injects a fake builder, so this is the one that holds the real path."""
    from app.cost.metered import MeteredEmbedder

    live = Settings(
        embedding_provider="gemini",
        gemini_api_key="test-key-not-real",
        embedding_timeout_seconds=15.0,
        embedding_max_retries=2,
    )
    embedder = query_health._build_ping_embedder(live)
    try:
        assert isinstance(embedder, MeteredEmbedder)
        inner = embedder.inner
        assert inner._timeout_seconds == query_health.SELF_CHECK_TIMEOUT_SECONDS
        assert inner._max_retries == 0
    finally:
        await query_health._close(embedder)
    assert embedder.inner._http_client.is_closed


class _SlowSet:
    """A Redis whose first call is slow, like a cold machine's TLS handshake."""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def set(self, *args: Any, **kwargs: Any) -> bool:
        await asyncio.sleep(self._delay)
        return True


async def test_the_lock_claim_allows_for_a_cold_connection(monkeypatch) -> None:
    """Red when: the claim uses the answer path's short ceiling. On a woken
    machine it is the route's first Redis call; a slow handshake must not skip
    the check (or worse, set the lock and then skip it)."""
    monkeypatch.setattr(query_health, "REDIS_CALL_CEILING_SECONDS", 0.05)
    monkeypatch.setattr(query_health, "STATUS_CEILING_SECONDS", 1.0)
    tracker = query_health.QueryEmbedderHealth(_SlowSet(0.2), KEY, self_check_key=LOCK)

    assert await tracker.claim_self_check(10800) is True


async def test_no_free_paid_call_slot_means_no_check_and_no_lock(
    tracker: query_health.QueryEmbedderHealth, redis_client: Any, monkeypatch
) -> None:
    """Red when: the check queues behind answers for a paid-call slot on the
    health route (it could pass the probe's 45 s limit), or takes the lock
    while skipping (losing the interval)."""
    full = asyncio.Semaphore(1)
    await full.acquire()
    monkeypatch.setattr(query_health, "get_semaphore", lambda _settings: full)
    embedder = _FakeEmbedder()

    await _run(tracker, embedder)

    assert embedder.calls == 0
    assert await redis_client.get(LOCK) is None


class _HangingEmbedder(_FakeEmbedder):
    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        await asyncio.sleep(60)
        return []


async def test_a_hanging_check_is_cut_by_the_backstop(
    tracker: query_health.QueryEmbedderHealth, monkeypatch
) -> None:
    """Red when: nothing bounds the whole check. httpx's timeout is per phase
    and the slot wait has none, so the route could hang past the probe."""
    monkeypatch.setattr(query_health, "SELF_CHECK_BACKSTOP_SECONDS", 0.05)
    embedder = _HangingEmbedder()

    started = asyncio.get_running_loop().time()
    await _run(tracker, embedder)

    assert asyncio.get_running_loop().time() - started < 1.0
    assert embedder.closed
    assert (await tracker.status())["state"] == "ok"


async def test_the_route_helper_runs_the_check_before_returning(
    tracker: query_health.QueryEmbedderHealth, monkeypatch
) -> None:
    """Red when: maybe_self_check stops passing settings or the builder
    through, or defers the check (the probe would read the previous result)."""
    monkeypatch.setattr(query_health, "current", lambda: tracker)

    await query_health.maybe_self_check(
        settings=_settings(),
        build=lambda _s: _FakeEmbedder(
            EmbedderUnavailable("x", status_code=402, account_refused=True)
        ),
    )

    assert (await tracker.status())["state"] == "refused"


async def test_a_truthy_lock_reply_that_is_not_true_does_not_win() -> None:
    """Red when: any truthy reply counts as winning the lock."""

    class _OkReply:
        async def set(self, *args: Any, **kwargs: Any) -> str:
            return "OK"

    tracker = query_health.QueryEmbedderHealth(_OkReply(), KEY, self_check_key=LOCK)

    assert await tracker.claim_self_check(10800) is False


def test_the_lock_key_is_scoped_by_the_redis_prefix(monkeypatch) -> None:
    """Red when: the lock key drops the environment prefix, so two environments
    sharing one Redis would suppress each other's checks."""
    settings = Settings(redis_url="redis://localhost:6379/0", redis_key_prefix="env1")
    monkeypatch.setattr(query_health, "get_settings", lambda: settings)
    monkeypatch.setattr(query_health, "get_redis_client", lambda: object())

    tracker = query_health.current()

    assert tracker is not None
    assert tracker.self_check_key == "env1:query_embedder_self_check"


def test_the_keep_enabled_job_puts_on_every_scheduled_run_and_says_the_site_is_fine() -> None:
    """Red when: the call is not a PUT to the enable endpoint, is gated to an
    hour that GitHub's delayed midnight runs can miss, or a failure reads like
    a site outage."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    job = doc["jobs"]["keep-enabled"]
    script = "\n".join(step.get("run", "") for step in job["steps"])

    assert "--method PUT" in script
    assert "/actions/workflows/uptime.yml/enable" in script
    assert "date" not in script
    assert "the site is fine" in script


async def test_a_close_that_fails_never_fails_the_route(
    tracker: query_health.QueryEmbedderHealth,
) -> None:
    """Red when: an error from closing the ping's client reaches the route."""

    class _BadClose(_FakeEmbedder):
        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    await _run(tracker, _BadClose())

    assert (await tracker.status())["state"] == "ok"


async def test_the_embedder_is_built_only_by_the_request_that_wins_the_lock(
    tracker: query_health.QueryEmbedderHealth,
) -> None:
    """Red when: the builder runs before the claim, so every health request
    that loses the lock builds (and must close) an HTTP client for nothing."""
    builds: list[int] = []

    def _build(_settings: Settings) -> _FakeEmbedder:
        builds.append(1)
        return _FakeEmbedder()

    for _ in range(5):
        await query_health.run_self_check(tracker, settings=_settings(), build=_build)

    assert builds == [1]


async def test_a_hung_lock_claim_fails_closed_within_its_ceiling(monkeypatch) -> None:
    """Red when: the claim waits on Redis without a ceiling (a hung Redis would
    hold the health route past the probe's limit)."""
    monkeypatch.setattr(query_health, "STATUS_CEILING_SECONDS", 0.05)
    tracker = query_health.QueryEmbedderHealth(_SlowSet(60), KEY, self_check_key=LOCK)

    assert await asyncio.wait_for(tracker.claim_self_check(10800), timeout=2) is False


def test_the_stored_spend_label_is_self_check() -> None:
    """Red when: the stored value changes; docs and spend queries name it."""
    assert CallSite.self_check.value == "self_check"


def test_the_keep_enabled_job_runs_only_on_the_schedule() -> None:
    """Red when: the job's condition changes (inverted, it would run on manual
    dispatch with actions: write and never on the schedule)."""
    doc = yaml.safe_load(WORKFLOW.read_text())

    assert doc["jobs"]["keep-enabled"]["if"] == "github.event_name == 'schedule'"


async def test_an_embedder_without_a_close_method_is_fine(
    tracker: query_health.QueryEmbedderHealth,
) -> None:
    """Red when: closing assumes every embedder has ``aclose`` (the stub
    embedder used in local runs has none, and a success must still clear)."""

    class _NoClose:
        async def embed(self, text: str) -> list[float]:
            return [0.0]

    since = await _refused(tracker)
    assert since
    await query_health.run_self_check(tracker, settings=_settings(), build=lambda _s: _NoClose())

    assert (await tracker.status())["state"] == "ok"
