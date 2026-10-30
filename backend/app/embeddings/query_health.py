"""Whether the query embedder's provider is refusing this account.

Every question is turned into a search vector. When the provider refuses the
ACCOUNT rather than one request (an empty prepaid balance, a bad or revoked
key), every one of those calls fails, the vector arm degrades and search drops
to keyword-only while answers keep coming. That can go unnoticed with nothing to
show for it but a log line.

So each refusal on the query path is recorded here, the next successful
embedding clears it, and ``/health/index`` reports it for the uptime probe,
which emails the owner when it fails. Which errors are account refusals is the
provider client's call (``EmbedderUnavailable.account_refused``): the statuses
differ by provider.

Real queries alone learn nothing while nobody asks, and after a top-up only a
new question clears the alert. So ``/health/index`` also runs a SELF-CHECK: one
tiny embedding call, at most once per
``query_embedder_self_check_interval_seconds`` app-wide (a Redis lock), fed to
the same record. It only rides a request that is already happening (the probe
already wakes the scale-to-zero machine): nothing here schedules a timer or a
task, which would keep the machine up.

The record lives in Redis, not in process memory: production scales to zero
and runs more than one machine, so a probe usually wakes a machine that has
never seen the failure. This is telemetry: no failure of it may reach the
request (the answer is already degraded; failing it outright would be worse),
each Redis call has a short ceiling of its own, and a failure is logged by its
type only, since a Redis error can echo its URL.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.core.middleware import get_current_request_id
from app.core.redis_client import get_redis_client
from app.cost.admission import get_semaphore
from app.cost.call_site import CallSite, call_site
from app.embeddings.errors import EmbedderUnavailable
from app.embeddings.factory import build_embedder, metered_embedder

_logger = logging.getLogger("citevyn.embeddings")

#: The longest one tracker call on the ANSWER path may wait on Redis. A safety
#: ceiling, not a tuning knob: every successful query now makes a call, and the
#: client's own socket timeout (5 s) would otherwise be added to every answer
#: while Redis is stalled. Tested to stay well below that timeout.
REDIS_CALL_CEILING_SECONDS = 0.5

#: The same ceiling for the health route's read. Longer, because the uptime
#: probe usually wakes a stopped machine whose first Redis call also opens the
#: connection (TLS included), and a slow handshake must not page as "Redis
#: unreadable". Still well inside the client's 5 s timeout and the probe's 45 s.
STATUS_CEILING_SECONDS = 2.0

#: The self-check's provider timeout, with no retries. A safety ceiling: the
#: live embedder's worst case (15 s x 3 attempts + backoff) would outlast the
#: uptime probe's 45 s curl limit. It bounds the PROVIDER request only, never
#: the metering around it, so a slow cost write is not cut off after the
#: provider has answered (and billed).
SELF_CHECK_TIMEOUT_SECONDS = 8.0

#: A backstop on the whole self-check (budget read, the wait for a paid-call
#: slot, the provider request, the cost write). httpx's timeout above applies
#: per phase, and the slot wait has none, so without this a busy machine could
#: hold the health route past the probe's 45 s limit and page a false outage.
#: Hitting it is rare; if it cuts a call after the provider answered, that one
#: call goes unmetered (logged as unsettled).
SELF_CHECK_BACKSTOP_SECONDS = 20.0

#: The text the self-check embeds: a few tokens.
SELF_CHECK_TEXT = "ping"

_UNAVAILABLE: dict[str, str | None] = {"state": "unavailable", "refused_since": None}
_UNCONFIGURED: dict[str, str | None] = {"state": "unconfigured", "refused_since": None}


class QueryEmbedderHealth:
    """One Redis key: when the provider first refused the account, or absent."""

    def __init__(self, client: Any, key: str, *, self_check_key: str | None = None) -> None:
        self._client = client
        self.key = key
        self.self_check_key = self_check_key or f"{key}:self_check"

    async def record_failure(self, error: EmbedderUnavailable, *, now: datetime) -> None:
        """Record an account refusal; ignore every other failure."""
        if not error.account_refused:
            return
        # The status code goes to the server log only; the public health route
        # says THAT the account is refused, not why.
        _logger.error(
            "query_embedder_account_refused",
            extra={"request_id": get_current_request_id(), "status_code": error.status_code},
        )
        # ``nx``: keep the FIRST refusal, which is what the operator needs. No
        # expiry: it must not clear itself while the account is still refused.
        await self._call("set", self.key, now.isoformat(), nx=True)

    async def record_success(self) -> None:
        """A vector came back, so the account is not refused."""
        await self._call("delete", self.key)

    async def status(self) -> dict[str, str | None]:
        """``ok``, ``refused`` (with when it started) or ``unavailable``."""
        try:
            since = await asyncio.wait_for(
                self._client.get(self.key), timeout=STATUS_CEILING_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never fails the health route
            _log_unavailable(exc)
            return dict(_UNAVAILABLE)
        if since is None:
            return {"state": "ok", "refused_since": None}
        return {"state": "refused", "refused_since": _valid_time(since)}

    async def claim_self_check(self, interval_seconds: int) -> bool:
        """Whether this request may run the self-check: True only for the one
        request per interval that sets the lock. Anything else (the lock is
        held, or Redis failed) is False: on spend, fail closed."""
        try:
            # The health route's ceiling: on a woken machine this is the first
            # Redis call, which also opens the connection.
            won = await asyncio.wait_for(
                self._client.set(self.self_check_key, "1", nx=True, ex=interval_seconds),
                timeout=STATUS_CEILING_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — no lock, no paid call
            _log_unavailable(exc)
            return False
        # redis-py answers None (not False) when NX finds the key set.
        return won is True

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> None:
        try:
            await asyncio.wait_for(
                getattr(self._client, name)(*args, **kwargs), timeout=REDIS_CALL_CEILING_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never fails the request
            _log_unavailable(exc)


def _valid_time(value: Any) -> str | None:
    """``value`` if it is exactly a timestamp this module writes, else None.

    Only this module writes the key, but the value is printed into the uptime
    probe's GitHub Actions log, where a line starting ``::`` is a workflow
    command. ``fromisoformat`` alone is not enough: it accepts any character
    between date and time, a newline included, so the value must also read
    back unchanged."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return value if parsed.isoformat() == value else None


def _log_unavailable(exc: Exception) -> None:
    # The type only: a Redis error can echo the URL, password included.
    _logger.warning("query_embedder_health_unavailable", extra={"error": type(exc).__name__})


def ping_settings(settings: Settings) -> Settings:
    """The live settings with the self-check's own timeout and no retries."""
    return settings.model_copy(
        update={
            "embedding_timeout_seconds": SELF_CHECK_TIMEOUT_SECONDS,
            "embedding_max_retries": 0,
        }
    )


def _build_ping_embedder(settings: Settings) -> Any:
    """A metered embedder of its own (so its spend is recorded), built from the
    ping settings."""
    ping = ping_settings(settings)
    return metered_embedder(build_embedder(ping), ping)


async def run_self_check(
    tracker: QueryEmbedderHealth,
    *,
    settings: Settings,
    build: Callable[[Settings], Any] = _build_ping_embedder,
) -> None:
    """At most once per interval app-wide, embed one word and record the result.

    Never raises: it runs inside the health route. Only a clean vector (clears)
    and a provider's account refusal (records) change the state; anything else
    (a timeout, a transient error, the daily spending cap, an embedder that
    cannot be built) proved nothing and changes nothing. Every check that runs
    logs one ``query_embedder_self_check`` line with its outcome, so a check
    that never settles is visible. An unsettled check still holds the lock, so
    the next try is one interval later.
    """
    interval = settings.query_embedder_self_check_interval_seconds
    if interval == 0:
        return
    # No free paid-call slot: answers are using them all. Skip without taking
    # the lock rather than queue behind them on the health route.
    if get_semaphore(settings).locked():
        return
    if not await tracker.claim_self_check(interval):
        return
    embedder: Any = None
    try:
        embedder = build(settings)
        with call_site(CallSite.self_check):
            await asyncio.wait_for(
                embedder.embed(SELF_CHECK_TEXT), timeout=SELF_CHECK_BACKSTOP_SECONDS
            )
    except EmbedderUnavailable as exc:
        outcome = "refused" if exc.account_refused else "unsettled"
        _log_outcome(outcome, error=exc, status_code=exc.status_code)
        await tracker.record_failure(exc, now=datetime.now(UTC))
        return
    except Exception as exc:  # noqa: BLE001 — proved nothing; never fail the route
        _log_outcome("unsettled", error=exc, status_code=None)
        return
    finally:
        if embedder is not None:
            await _close(embedder)
    _log_outcome("ok", error=None, status_code=None)
    await tracker.record_success()


def _log_outcome(outcome: str, *, error: Exception | None, status_code: int | None) -> None:
    # The error's type only: provider and Redis errors can echo URLs or bodies.
    _logger.log(
        logging.INFO if outcome == "ok" else logging.WARNING,
        "query_embedder_self_check",
        extra={
            "outcome": outcome,
            "error": type(error).__name__ if error is not None else None,
            "status_code": status_code,
        },
    )


async def _close(embedder: Any) -> None:
    """Close the ping's own HTTP client (the metered wrapper passes it on)."""
    close = getattr(embedder, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception as exc:  # noqa: BLE001 — closing must not fail the route
        _log_unavailable(exc)


def current() -> QueryEmbedderHealth | None:
    """The tracker, or None when no Redis is configured (local runs and tests)."""
    settings = get_settings()
    if not settings.redis_url:
        return None
    return QueryEmbedderHealth(
        get_redis_client(),
        f"{settings.redis_key_prefix}:query_embedder_refused_since",
        self_check_key=f"{settings.redis_key_prefix}:query_embedder_self_check",
    )


async def maybe_self_check(
    *,
    settings: Settings | None = None,
    build: Callable[[Settings], Any] = _build_ping_embedder,
) -> None:
    """The self-check behind ``/health/index``; nothing without Redis."""
    tracker = current()
    if tracker is not None:
        await run_self_check(tracker, settings=settings or get_settings(), build=build)


async def record_failure(error: EmbedderUnavailable) -> None:
    tracker = current()
    if tracker is not None:
        await tracker.record_failure(error, now=datetime.now(UTC))


async def record_success() -> None:
    tracker = current()
    if tracker is not None:
        await tracker.record_success()


async def status() -> dict[str, str | None]:
    """The ``query_embedder`` block of ``/health/index``.

    ``unconfigured`` when no Redis is set: production always sets one, so the
    uptime probe pages on it rather than let a lost setting switch this off."""
    tracker = current()
    if tracker is None:
        return dict(_UNCONFIGURED)
    return await tracker.status()
