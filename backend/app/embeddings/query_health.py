"""Whether the query embedder's provider is refusing this account.

Every question is turned into a search vector. When the provider refuses the
ACCOUNT rather than one request (an empty prepaid balance, a bad or revoked
key), every one of those calls fails, the vector arm degrades and search drops
to keyword-only while answers keep coming. That can go unnoticed with nothing to
show for it but a log line.

So each refusal on the query path is recorded here, the next successful
embedding clears it, and ``/health/index`` reports it for the uptime probe,
which emails the owner when it fails. No call is made to find out: only real
queries are observed, so with no traffic nothing new is learned. Which errors
are account refusals is the provider client's call
(``EmbedderUnavailable.account_refused``): the statuses differ by provider.

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
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.middleware import get_current_request_id
from app.core.redis_client import get_redis_client
from app.embeddings.errors import EmbedderUnavailable

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

_UNAVAILABLE: dict[str, str | None] = {"state": "unavailable", "refused_since": None}
_UNCONFIGURED: dict[str, str | None] = {"state": "unconfigured", "refused_since": None}


class QueryEmbedderHealth:
    """One Redis key: when the provider first refused the account, or absent."""

    def __init__(self, client: Any, key: str) -> None:
        self._client = client
        self.key = key

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


def current() -> QueryEmbedderHealth | None:
    """The tracker, or None when no Redis is configured (local runs and tests)."""
    settings = get_settings()
    if not settings.redis_url:
        return None
    return QueryEmbedderHealth(
        get_redis_client(), f"{settings.redis_key_prefix}:query_embedder_refused_since"
    )


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
