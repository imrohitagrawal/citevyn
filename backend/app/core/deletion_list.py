"""The deletion list: which accounts were deleted, kept OUTSIDE the database (#546).

A deletion lives in the database: the ``users`` row goes and a tombstone is
written. A restore from an older backup brings both back as they were, so the
account returns and its tombstone is lost. The privacy page promises that if we
ever restore from a backup we delete the data again straight away, so every
deletion is ALSO written here, in Redis, which a database restore does not
touch, and the app re-applies it on its own
(``app.services.account_deletion.reapply_deletions``):

* at startup, before serving: the machine sleeps when idle, and a restore made
  while it sleeps is caught when it wakes;
* every hour while it runs: a Neon restore goes live in seconds, machine awake
  or not;
* before every digest or alert email run (``app.worker.cli``: those run as
  separate processes);
* and in between, :func:`is_listed` refuses a listed account at every sign-in,
  session lookup, API-key check and shared-answer page, so a restored account
  cannot be used before the re-apply removes it.

What is stored: one sorted set, ``<CITEVYN_REDIS_KEY_PREFIX>:deleted_accounts``.
Each member is an account id (``usr_…``, random, never the address); its score
is when it was deleted (Unix seconds). Entries are kept :data:`KEEP` (60 days:
backups are kept 30 days, so 60 covers every backup copy with room to spare)
and then dropped one by one. The key itself has no expiry. Upstash does not
evict keys unless eviction is switched on for the database, and even then it
prefers keys with an expiry (the rate limiter's), so the list does not vanish
under normal operation; if it ever does, the re-apply logs an error (see
``reapply_deletions``).

Redis down:

* when a deletion is made: the deletion is REFUSED and nothing changes
  (:class:`DeletionListUnavailable`). A deletion that cannot be recorded would
  be undone for good by the next restore, silently; a refused one is visible
  and can be retried. The rate limiter also fails closed on a Redis outage, so
  the app is unavailable then anyway.
* when the gate asks: the request is refused with a 503 (fail closed), as
  the rate limiter does in that state, never answered as if signed out. This
  includes the public shared-answer page, which has no rate limiter.

No Redis configured (local work, tests): there is no list, deletions work as
before, and the gate lets everyone through. Production always sets
``CITEVYN_REDIS_URL``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import get_settings
from app.core.errors import APIErrorCode, error_response
from app.core.middleware import get_current_request_id
from app.core.redis_client import get_redis_client

_logger = logging.getLogger("citevyn.deletion_list")

#: How long an entry is kept. At least 60 days: the privacy page's promise
#: (30 days to delete, then backups kept 30 days, RUNBOOK §4.1).
KEEP = timedelta(days=60)

#: Every account a person can own starts with this (``auth_sessions``
#: ``REGISTERED_USER_PREFIX``; repeated here to avoid an import cycle, and
#: pinned equal by a test). Nothing else can be deleted, so nothing else is
#: looked up.
_REGISTERED_PREFIX = "usr_"


class DeletionListUnavailable(Exception):
    """Redis could not be read or written."""


class DeletionList:
    """The outside list in one Redis sorted set (see the module docstring)."""

    def __init__(self, client: Any, key: str) -> None:
        self._client = client
        self.key = key

    async def record(self, user_id: str, when: datetime) -> None:
        """Add ``user_id``, deleted at ``when``."""
        await self._call("zadd", self.key, {user_id: when.timestamp()})

    async def forget(self, user_id: str) -> None:
        """Remove ``user_id`` (a deletion that did not happen after all)."""
        await self._call("zrem", self.key, user_id)

    async def contains(self, user_id: str) -> bool:
        return await self._call("zscore", self.key, user_id) is not None

    async def entries(self, now: datetime) -> dict[str, datetime]:
        """Every entry younger than :data:`KEEP`, by account id. Drops the
        older ones from Redis first."""
        await self._call("zremrangebyscore", self.key, "-inf", (now - KEEP).timestamp())
        rows = await self._call("zrange", self.key, 0, -1, withscores=True)
        return {member: datetime.fromtimestamp(score, UTC) for member, score in rows}

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        import redis.exceptions

        try:
            return await getattr(self._client, name)(*args, **kwargs)
        except (redis.exceptions.RedisError, OSError) as exc:
            # The type only: a Redis error can echo the URL.
            raise DeletionListUnavailable(type(exc).__name__) from exc


def current() -> DeletionList | None:
    """The deletion list, or None when no Redis is configured."""
    settings = get_settings()
    if not settings.redis_url:
        return None
    return DeletionList(get_redis_client(), f"{settings.redis_key_prefix}:deleted_accounts")


async def is_listed(user_id: str) -> bool:
    """Whether ``user_id`` was deleted (a restore may have brought it back),
    so must be refused. For request paths only.

    When the list cannot be read this raises a 503 rather than answering:
    answering True would look like "no session" to the caller, which then
    mints a fresh anonymous cookie over the person's real one and signs them
    out for good on one Redis timeout (review finding). The 503 carries
    ``rate_limiter_unavailable``, the code for "Redis is unreachable" that
    the client already handles: the list lives in the same Redis."""
    if not user_id.startswith(_REGISTERED_PREFIX):
        return False
    the_list = current()
    if the_list is None:
        return False
    try:
        return await the_list.contains(user_id)
    except DeletionListUnavailable as exc:
        _logger.warning("deletion_list_unreadable", extra={"error": str(exc)})
        raise error_response(
            request_id=get_current_request_id(),
            code=APIErrorCode.rate_limiter_unavailable,
            message="Temporarily unavailable. Try again shortly.",
        ) from exc


__all__ = [
    "KEEP",
    "DeletionList",
    "DeletionListUnavailable",
    "current",
    "is_listed",
]
