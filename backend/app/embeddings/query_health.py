"""Whether the query embedder's provider is refusing this account.

Every question is turned into a search vector. When the provider refuses the
ACCOUNT rather than one request (an empty prepaid balance is a 402, a bad or
revoked key a 401 or 403), every one of those calls fails, the vector arm
degrades and search drops to keyword-only while answers keep coming. That
happened in production with nothing to show for it but a log line.

So each refusal on the query path is recorded here, the next successful
embedding clears it, and ``/health/index`` reports it for the uptime probe,
which emails the owner when it fails. No call is made to find out: only real
queries are observed, so with no traffic nothing new is learned.

The record lives in Redis, not in process memory: production scales to zero
and runs more than one machine, so a probe usually wakes a machine that has
never seen the failure. A Redis error never reaches the request (the answer is
already degraded; failing it outright would be worse) and is logged by its type
only, since a Redis error can echo its URL.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import redis.exceptions

from app.core.config import get_settings
from app.core.middleware import get_current_request_id
from app.core.redis_client import get_redis_client

_logger = logging.getLogger("citevyn.embeddings")

# Statuses that mean the provider refuses THIS ACCOUNT, not one request. They
# do not clear by retrying, so they are worth waking someone for. A timeout, a
# rate limit (429), a 5xx or a 400 for one odd input is not recorded.
ACCOUNT_REFUSAL_STATUSES: frozenset[int] = frozenset({401, 402, 403})

_UNKNOWN: dict[str, str | None] = {"state": "unknown", "refused_since": None}

_REDIS_ERRORS = (redis.exceptions.RedisError, OSError)


class QueryEmbedderHealth:
    """One Redis key: when the provider first refused the account, or absent."""

    def __init__(self, client: Any, key: str) -> None:
        self._client = client
        self.key = key

    async def record_failure(self, status_code: int | None, *, now: datetime) -> None:
        """Record an account refusal; ignore every other failure."""
        if status_code not in ACCOUNT_REFUSAL_STATUSES:
            return
        # The status code goes to the server log only; the public health route
        # says THAT the account is refused, not why.
        _logger.error(
            "query_embedder_account_refused",
            extra={"request_id": get_current_request_id(), "status_code": status_code},
        )
        # ``nx``: keep the FIRST refusal, which is what the operator needs.
        await self._call("set", self.key, now.isoformat(), nx=True)

    async def record_success(self) -> None:
        """A vector came back, so the account is not refused."""
        await self._call("delete", self.key)

    async def status(self) -> dict[str, str | None]:
        """``ok``, ``refused`` (with when it started) or ``unknown`` (Redis unreadable)."""
        try:
            since = await self._client.get(self.key)
        except _REDIS_ERRORS as exc:  # never fail the health route over this
            _log_unavailable(exc)
            return dict(_UNKNOWN)
        if since is None:
            return {"state": "ok", "refused_since": None}
        return {"state": "refused", "refused_since": since}

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> None:
        try:
            await getattr(self._client, name)(*args, **kwargs)
        except _REDIS_ERRORS as exc:  # never fail the request over this
            _log_unavailable(exc)


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


async def record_failure(status_code: int | None) -> None:
    tracker = current()
    if tracker is not None:
        await tracker.record_failure(status_code, now=datetime.now(UTC))


async def record_success() -> None:
    tracker = current()
    if tracker is not None:
        await tracker.record_success()


async def status() -> dict[str, str | None]:
    """The ``query_embedder`` block of ``/health/index``."""
    tracker = current()
    if tracker is None:
        return dict(_UNKNOWN)
    return await tracker.status()
