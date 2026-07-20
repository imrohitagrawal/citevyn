"""Concurrency admission control for paid calls (#153 Layer 2).

There is no cap on concurrent provider calls today. That matters specifically
*because* of how the daily budget works: every in-flight call reads a spend total
that does not yet include its peers, so N simultaneous calls can each individually
see "under budget" and collectively blow past it. The budget bounds spend over
time; this bounds how far past the line a single burst can carry it.

A semaphore, not a queue with a timeout. A caller that waits is one whose answer is
still wanted; a caller that gave up has already been cancelled by the request layer,
and ``asyncio.Semaphore`` releases correctly on cancellation. Adding a wait timeout
would convert "busy" into an error at exactly the moment the system is already
degrading, for no saving.

Scope is **per process**, like the in-process rate limiter. Under multi-worker
uvicorn the effective cap is `workers × cost_max_concurrent_calls`. That is stated
rather than hidden: a cross-process cap needs the same Redis round trip the limiter
uses, and the daily budget — which IS cross-process, because it sums a table — is
the control that actually bounds total spend.
"""

from __future__ import annotations

import asyncio

from app.core.config import Settings

_semaphore: asyncio.Semaphore | None = None
_configured_limit: int | None = None


def get_semaphore(settings: Settings) -> asyncio.Semaphore:
    """Return the process-wide paid-call semaphore, rebuilding it if the cap changed.

    Rebuilt on a config change so a settings reload takes effect, mirroring
    ``app.core.rate_limit.get_limiter``. Rebuilding drops any waiters' claim on the
    old object, which is safe here: the old semaphore is still held by its current
    owners until they release, and the new one simply governs subsequent calls.
    """
    global _semaphore, _configured_limit
    if _semaphore is None or _configured_limit != settings.cost_max_concurrent_calls:
        _semaphore = asyncio.Semaphore(settings.cost_max_concurrent_calls)
        _configured_limit = settings.cost_max_concurrent_calls
    return _semaphore


def reset_semaphore() -> None:
    """Drop the process-wide semaphore (test-only)."""
    global _semaphore, _configured_limit
    _semaphore = None
    _configured_limit = None


__all__ = ["get_semaphore", "reset_semaphore"]
