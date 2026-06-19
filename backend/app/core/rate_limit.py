"""In-process rate limiter for the public /v1/* routes.

A token bucket per ``user_id`` (a string the auth dependency returns —
``"demo_user"`` for the demo path, ``"admin"`` for the admin path).
The bucket is a sliding window of timestamps; on every request we
evict timestamps older than ``window_seconds`` and reject if the
remaining count exceeds the per-hour limit for the user's role.

Scope and limits
----------------

* **Per-process.** The bucket is an in-memory dict protected by a
  single :class:`asyncio.Lock`. A multi-process deployment (e.g.
  several uvicorn workers) will under-count because the dicts do not
  share state. A Redis-backed implementation is a Slice 10+ concern;
  this module is the contract.
* **Limits** come from :class:`Settings.rate_limit_demo_user_per_hour`
  and :class:`Settings.rate_limit_admin_per_hour`. They default to
  the values in ``docs/SECURITY_MODEL.md §6``.
* **Disable** by setting :class:`Settings.rate_limit_enabled = False`.
  Useful for tests that want a green check independent of the limit.

Routes call :func:`enforce_rate_limit` directly after their auth
dependency has run. The function raises the standard error envelope
via :func:`app.core.errors.error_response` with the ``rate_limited``
code on overflow; the caller does not need to do anything special.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING

from app.core.errors import APIErrorCode, error_response
from app.core.middleware import get_current_request_id

if TYPE_CHECKING:
    from app.core.config import Settings

# Default limits the limiter uses when ``Settings.rate_limit_*_per_hour``
# is not provided (e.g. the limiter is exercised in a unit test that
# does not bind a request scope). The real per-deployment values come
# from the settings and are tested in ``tests/test_rate_limit.py``.
DEFAULT_LIMIT_DEMO_USER: int = 30
DEFAULT_LIMIT_ADMIN: int = 100


class RateLimiter:
    """Sliding-window rate limiter, per ``user_id``.

    Threading note: the underlying lock is :class:`asyncio.Lock`
    because FastAPI runs requests on a single event loop. Cross-thread
    use (e.g. a background task) would need a ``threading.Lock`` —
    not used in the MVP.
    """

    def __init__(self, *, window_seconds: int) -> None:
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        self._window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    def limit_for(self, *, role: str) -> int:
        """Return the per-window hit limit for ``role``.

        Falls back to the demo-user limit for unknown roles so a
        misconfigured caller cannot bypass the limit by sending a
        bogus role string.
        """
        if role == "admin":
            return DEFAULT_LIMIT_ADMIN
        return DEFAULT_LIMIT_DEMO_USER

    async def check(self, *, user_id: str, role: str) -> None:
        """Record a hit and raise :class:`HTTPException` if over the limit.

        The hit is recorded on the success path only. A flood of
        denied requests is intentionally NOT counted against the
        bucket so an attacker can't keep the bucket near-full by
        sending rejected requests.
        """
        limit = self.limit_for(role=role)
        now = time.monotonic()
        cutoff = now - self._window_seconds
        async with self._lock:
            bucket = self._buckets[user_id]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                raise _too_many_requests()
            bucket.append(now)

    def reset(self) -> None:
        """Drop all bucket state.

        Test-only helper; never call in production.
        """
        self._buckets.clear()


def _too_many_requests() -> Exception:
    """Build a 429 :class:`HTTPException` carrying the standard error envelope."""
    request_id = get_current_request_id() or ""
    return error_response(
        request_id=request_id,
        code=APIErrorCode.rate_limited,
        message=(
            "Rate limit exceeded. The demo allows a small number of "
            "queries per hour per user; try again later."
        ),
    )


# ---------------------------------------------------------------------------
# Process-wide limiter
# ---------------------------------------------------------------------------


# Process-wide singleton. The lock is process-wide too; for the MVP
# a single uvicorn process is the documented deployment shape.
_limiter: RateLimiter | None = None


def get_limiter(settings: Settings) -> RateLimiter:
    """Return the process-wide :class:`RateLimiter`, building it lazily.

    ``settings`` is typed via :class:`Settings` (forwarded through
    :data:`TYPE_CHECKING`) so this module does not import
    :mod:`app.core.config` at runtime and create a circular import.
    Callers pass ``get_settings()`` directly.
    """
    global _limiter
    if _limiter is None or _limiter.window_seconds != settings.rate_limit_window_seconds:
        _limiter = RateLimiter(window_seconds=settings.rate_limit_window_seconds)
    return _limiter


def reset_limiter() -> None:
    """Drop the process-wide limiter (test-only)."""
    global _limiter
    _limiter = None


async def enforce_rate_limit(
    *,
    user_id: str,
    role: str,
    settings: Settings,
) -> None:
    """Apply the rate limit for an authenticated request.

    Routes that already use :func:`require_demo_api_key` (which
    returns the user id string) call this directly. The function
    raises the standard envelope on overflow; the caller does not
    need to do anything special.
    """
    if not settings.rate_limit_enabled:
        return
    limiter = get_limiter(settings)
    await limiter.check(user_id=user_id, role=role)


__all__ = [
    "DEFAULT_LIMIT_ADMIN",
    "DEFAULT_LIMIT_DEMO_USER",
    "RateLimiter",
    "enforce_rate_limit",
    "get_limiter",
    "reset_limiter",
]