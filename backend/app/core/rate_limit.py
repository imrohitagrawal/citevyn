"""Rate limiter for the public + admin routes.

The limiter applies a sliding-window policy per ``user_id`` (a string
the auth dependency returns — ``"demo_user"`` for the demo path,
``"admin"`` for the admin path). On every request we evict timestamps
older than ``window_seconds`` and reject if the remaining count
exceeds the per-window limit for the user's role.

Two implementations
-------------------

* **In-process** — :class:`RateLimiter` is the original Slice 8
  implementation. The bucket is an in-memory ``dict`` protected by an
  :class:`asyncio.Lock`. It is hermetic (no external dependency) and
  is the default in unit tests where there is no Redis service.
* **Redis** — :class:`RedisRateLimiter` uses an atomic Lua script
  (``EVAL``) that does ``ZREMRANGEBYSCORE`` + a conditional
  ``ZADD`` + ``EXPIRE`` in a single server-side step. The script
  only inserts the new hit **if** the post-eviction count is below
  the limit, so a flood of denied requests cannot pin the bucket
  and lock out a legitimate user.

Selection
---------

:func:`get_limiter` returns whichever implementation matches
``settings.redis_url``. Production deploys MUST set
``CITEVYN_REDIS_URL`` so the Redis path is active — leaving the
in-process path in production under-counts under multi-worker
uvicorn.

Wiring
------

The route layer enforces the limit through a FastAPI dependency
(:func:`rate_limited_demo`, :func:`rate_limited_admin`) that
chains the auth dependency and the limiter. Every authenticated
route that returns user data must add one of these — there is no
global middleware, because the auth key and the rate-limit key
must agree. Skipping the dependency silently disables the limit
on that route; the route test suite asserts the dependency is in
place to catch the regression.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import time
import uuid
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Annotated, Protocol

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.core.errors import APIErrorCode, error_response
from app.core.middleware import get_current_request_id
from app.core.security import require_admin_api_key, require_demo_api_key

if TYPE_CHECKING:
    from redis.asyncio import Redis

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


# Fallback for the shared backstop bucket when a caller constructs a limiter
# directly (tests, scripts) without naming one. Production passes
# ``settings.rate_limit_global_per_hour``. It is deliberately far above the
# per-visitor limit: this bucket exists to stop a distributed flood, not to
# throttle normal traffic, and if it ever binds during ordinary use it has
# re-created the #203 lockout.
_DEFAULT_GLOBAL_PER_WINDOW = 600


class _LimiterLike(Protocol):
    """Minimum surface the rate-limit policy needs from a limiter."""

    @property
    def window_seconds(self) -> int: ...

    def limit_for(self, *, role: str) -> int: ...

    async def check(self, *, user_id: str, role: str) -> None: ...


# ---------------------------------------------------------------------------
# In-process limiter (Slice 8)
# ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding-window rate limiter, per ``user_id``.

    Threading note: the underlying lock is :class:`asyncio.Lock`
    because FastAPI runs requests on a single event loop. Cross-thread
    use (e.g. a background task) would need a ``threading.Lock`` —
    not used in the MVP.

    Scope: **per-process**. Multi-worker uvicorn deployments
    under-count. Production deploys use :class:`RedisRateLimiter`
    instead; this class is the hermetic / single-process path.

    The limit is read from the ``limits`` dict passed at construction
    time (mapped from :attr:`Settings.rate_limit_*_per_hour`) so the
    operator-tunable env vars are honoured. Unknown roles fall back to
    the demo-user limit so a misconfigured caller cannot bypass by
    sending a bogus role string.
    """

    def __init__(
        self,
        *,
        window_seconds: int,
        demo_user_per_window: int,
        admin_per_window: int,
        global_per_window: int = _DEFAULT_GLOBAL_PER_WINDOW,
    ) -> None:
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        if demo_user_per_window < 1 or admin_per_window < 1:
            raise ValueError("per-window limits must be >= 1")
        if global_per_window < 1:
            raise ValueError("global_per_window must be >= 1")
        self._window_seconds = window_seconds
        self._limits: dict[str, int] = {
            "demo_user": demo_user_per_window,
            "admin": admin_per_window,
            # Backstop across ALL demo visitors (#203). Defaults high enough that
            # it never binds on ordinary use. Registered explicitly rather than
            # left to fall through ``limit_for``'s default, which would silently
            # apply the 30/hour DEMO limit to the shared bucket and re-create the
            # global lockout this change exists to remove.
            "global": global_per_window,
        }
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    def limit_for(self, *, role: str) -> int:
        """Return the per-window hit limit for ``role``."""
        return self._limits.get(role, self._limits["demo_user"])

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


# ---------------------------------------------------------------------------
# Redis sliding-window limiter (Slice 9a)
# ---------------------------------------------------------------------------


# Atomic sliding-window script.
#
#   KEYS[1] = sorted-set key for the user_id's bucket
#   ARGV[1] = window cutoff (now - window_seconds), float
#   ARGV[2] = unique member id
#   ARGV[3] = now (epoch seconds, float), used as the ZSET score
#   ARGV[4] = limit (per-window hit cap for this role)
#   ARGV[5] = bucket TTL in seconds (window_seconds + 1)
#
# Returns: ``{allowed:int, count:int}`` where ``allowed`` is 1 if the
# hit was recorded and 0 if it was rejected. The count returned is
# the post-decision size of the bucket (so the caller can surface
# rate-limit headers in a future slice).
#
# The script is *atomic* on the Redis server — no other client's
# commands can interleave between the ZREMRANGEBYSCORE and the
# conditional ZADD, so two uvicorn workers cannot both observe
# count == limit-1 and both succeed.
#
# The ZADD is guarded by the limit so a flood of 429-rejected
# requests does not pin the bucket: denied requests are never
# inserted, and the bucket ages out naturally.
_SLIDING_WINDOW_LUA: str = """
local key = KEYS[1]
local cutoff = tonumber(ARGV[1])
local member = ARGV[2]
local now = tonumber(ARGV[3])
local limit = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

redis.call('ZREMRANGEBYSCORE', key, 0, cutoff)
local count = redis.call('ZCARD', key)
if count >= limit then
    return {0, count}
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, ttl)
return {1, count + 1}
"""


class RedisRateLimiter:
    """Sliding-window rate limiter backed by a Redis sorted set.

    The bucket is one ``ZSET`` per ``user_id``. Members are unique
    strings (``"<epoch>:<uuid4>"``); scores are epoch seconds. The
    :data:`_SLIDING_WINDOW_LUA` script atomically evicts expired
    members, decides whether the new hit fits under the limit, and
    inserts the hit only on the success path. See the script's
    docstring for the contract.

    Limits are read from :attr:`Settings.rate_limit_demo_user_per_hour`
    and :attr:`Settings.rate_limit_admin_per_hour` at construction
    time, so operator-tunable env vars are honoured.

    The Redis client is created lazily by :mod:`app.core.redis_client`
    from :attr:`Settings.redis_url`. Production deploys MUST set that
    env var.
    """

    def __init__(
        self,
        *,
        client: Redis,
        window_seconds: int,
        demo_user_per_window: int,
        admin_per_window: int,
        key_prefix: str,
        global_per_window: int = _DEFAULT_GLOBAL_PER_WINDOW,
    ) -> None:
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        if demo_user_per_window < 1 or admin_per_window < 1:
            raise ValueError("per-window limits must be >= 1")
        if global_per_window < 1:
            raise ValueError("global_per_window must be >= 1")
        if not key_prefix:
            raise ValueError("key_prefix must be a non-empty string")
        self._client = client
        self._window_seconds = window_seconds
        self._limits: dict[str, int] = {
            "demo_user": demo_user_per_window,
            "admin": admin_per_window,
            # Backstop across ALL demo visitors (#203). Defaults high enough that
            # it never binds on ordinary use. Registered explicitly rather than
            # left to fall through ``limit_for``'s default, which would silently
            # apply the 30/hour DEMO limit to the shared bucket and re-create the
            # global lockout this change exists to remove.
            "global": global_per_window,
        }
        self._key_prefix = key_prefix.rstrip(":")
        # The script body is held as a string so we can call
        # ``client.eval()`` directly. ``register_script`` would use
        # EVALSHA first (which fakeredis does not support), so we
        # always EVAL — slightly more bytes on the wire for the
        # first call, but Lua scripts are small and the cost is
        # negligible compared to the round-trip itself.
        self._script_body = _SLIDING_WINDOW_LUA

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    def _bucket_key(self, user_id: str) -> str:
        return f"{self._key_prefix}:{user_id}"

    def limit_for(self, *, role: str) -> int:
        return self._limits.get(role, self._limits["demo_user"])

    async def check(self, *, user_id: str, role: str) -> None:
        """Record a hit atomically and raise 429 if over the limit.

        The script does the eviction + decision + insert + expire in
        a single server-side step; the client awaits the result. If
        the script returns ``allowed == 0`` the hit is rejected and
        nothing is added to the bucket — a flood of rejected
        requests cannot pin a legitimate user out.

        Failure mode: if Redis is unreachable (connection refused,
        timeout, MOVED redirection) we fail **closed** by raising
        503 ``rate_limiter_unavailable`` — a code that names the
        dependency that actually broke, so an operator reading the
        access log is not sent after the search index (#167).
        Fail-open would let a Redis outage disable rate
        limiting entirely, which is the worst possible outcome
        for a security control. Operators can re-enable traffic
        by fixing Redis; the 503s are visible in the access log
        so an outage is not silent.
        """
        import redis.exceptions  # local import — the stub backend
        # doesn't need this code path.

        limit = self.limit_for(role=role)
        now = time.time()
        cutoff = now - self._window_seconds
        member = f"{now:.6f}:{uuid.uuid4().hex}"
        key = self._bucket_key(user_id)
        try:
            allowed, _count = await self._client.eval(  # type: ignore[union-attr]
                self._script_body,
                1,  # number of KEYS
                key,
                cutoff,
                member,
                now,
                limit,
                self._window_seconds + 1,
            )
        except (redis.exceptions.RedisError, OSError) as exc:
            request_id = get_current_request_id()
            raise error_response(
                request_id=request_id,
                # NOT ``index_unavailable`` (#167): retrieval is fine, the
                # limiter is what is down. A code that names the wrong
                # dependency misdirects both operators and the UI copy.
                code=APIErrorCode.rate_limiter_unavailable,
                message="Rate limiter is temporarily unavailable.",
            ) from exc
        if not int(allowed):
            raise _too_many_requests()


# ---------------------------------------------------------------------------
# Process-wide limiter
# ---------------------------------------------------------------------------


def _too_many_requests() -> Exception:
    """Build a 429 :class:`HTTPException` carrying the standard error envelope."""
    request_id = get_current_request_id()
    return error_response(
        request_id=request_id,
        code=APIErrorCode.rate_limited,
        message=(
            "Rate limit exceeded. The demo allows a small number of "
            "queries per hour per user; try again later."
        ),
    )


# Process-wide singleton. The implementation chosen at first use
# stays until :func:`reset_limiter` is called (test-only).
_limiter: RateLimiter | RedisRateLimiter | None = None


def _build_limiter(settings: Settings) -> RateLimiter | RedisRateLimiter:
    """Construct the limiter that matches the current settings."""
    if settings.redis_url:
        from app.core.redis_client import get_redis_client

        return RedisRateLimiter(
            client=get_redis_client(),
            window_seconds=settings.rate_limit_window_seconds,
            demo_user_per_window=settings.rate_limit_demo_user_per_hour,
            admin_per_window=settings.rate_limit_admin_per_hour,
            key_prefix=settings.redis_key_prefix,
            global_per_window=_effective_global_limit(settings),
        )
    return RateLimiter(
        window_seconds=settings.rate_limit_window_seconds,
        demo_user_per_window=settings.rate_limit_demo_user_per_hour,
        admin_per_window=settings.rate_limit_admin_per_hour,
        global_per_window=_effective_global_limit(settings),
    )


def _effective_global_limit(settings: Settings) -> int:
    """The backstop limit to build the limiter with.

    ``rate_limit_global_per_hour = 0`` means "no backstop". The limiters reject a
    limit below 1, and a 0 would in any case deny EVERY request, so the disabled
    case is carried as a large sentinel and the dependency simply skips the check.
    Encoding "disabled" as 0 in the limiter itself would be a footgun: one missed
    branch and the backstop becomes a total outage.
    """
    configured = settings.rate_limit_global_per_hour
    return configured if configured > 0 else _DEFAULT_GLOBAL_PER_WINDOW


def get_limiter(settings: Settings) -> _LimiterLike:
    """Return the process-wide rate limiter, building it lazily.

    The limiter is rebuilt when the operator-tunable settings
    (window length, per-role limits, redis URL) change so a config
    reload after startup picks up the new values. The rebuild is
    cheap; the limiter holds no persistent state on the server.
    """
    global _limiter
    if _limiter is not None and _settings_match(_limiter, settings):
        return _limiter
    _limiter = _build_limiter(settings)
    return _limiter


def _settings_match(limiter: _LimiterLike, settings: Settings) -> bool:
    """Return True if the cached limiter was built from the same settings.

    We compare the operator-tunable fields the limiter's construction
    consumed: window length, per-role limits, and whether the redis
    path is active. The ``uses_redis`` check matters because the
    choice of in-process vs Redis path is driven by whether
    ``settings.redis_url`` is set — flipping it without rebuilding
    the limiter would silently leave the old path in place.
    """
    return (
        limiter.window_seconds == settings.rate_limit_window_seconds
        and limiter.limit_for(role="demo_user") == settings.rate_limit_demo_user_per_hour
        and limiter.limit_for(role="admin") == settings.rate_limit_admin_per_hour
        and limiter.limit_for(role=_GLOBAL_ROLE) == _effective_global_limit(settings)
        and isinstance(limiter, RedisRateLimiter) == bool(settings.redis_url)
    )


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
    returns the user id string) call this directly, or — preferred
    — chain the :func:`rate_limited_demo` / :func:`rate_limited_admin`
    dependency so the limit is enforced uniformly across all
    authenticated routes.

    The function raises the standard envelope on overflow; the caller
    does not need to do anything special.
    """
    if not settings.rate_limit_enabled:
        return
    limiter = get_limiter(settings)
    await limiter.check(user_id=user_id, role=role)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
#
# Chained dependencies that wrap :func:`require_demo_api_key` /
# :func:`require_admin_api_key` with the rate limit. Routes add
# ``Depends(rate_limited_demo)`` instead of ``Depends(require_demo_api_key)``
# so the limit is enforced uniformly. A new authenticated route that
# forgets to add the dependency is caught by the route test suite,
# which asserts the dependency is in place.
#
# The dependency returns the same ``user_id`` string the auth
# dependency returns, so existing route signatures are unchanged
# (they replace ``require_demo_api_key`` with ``rate_limited_demo``).


# ---------------------------------------------------------------------------
# Per-visitor identity for the DEMO bucket (#203)
# ---------------------------------------------------------------------------
#
# The demo API key is shared by construction, so ``require_demo_api_key`` returns
# a CONSTANT (``DEMO_USER_ID``). Keying the limiter on it gave every visitor on
# earth ONE bucket: 30 questions from one person denied the demo to everyone else
# for a rolling hour, and because the bucket lives in Redis a restart no longer
# cleared it.
#
# The fix separates the two identities that were conflated:
#   * AUDIT identity  — still ``DEMO_USER_ID``. Attribution, logs and the
#     orchestrator are unchanged, and the dependency still RETURNS it, so no
#     route signature changes.
#   * RATE-LIMIT key  — derived per visitor, below.
#
# What this does and does not buy: per-visitor limiting is FAIRNESS. It stops one
# visitor monopolising the demo. It does NOT cap spend — a distributed source
# still costs money, and the §9 daily budget is the control for that.

_GLOBAL_BUCKET_KEY = "demo_global"
_GLOBAL_ROLE = "global"
# Every failure path lands here rather than inventing a per-request key. Sharing
# one bucket is the OLD behaviour, so an unparseable address degrades to "no
# worse than before" instead of handing out an unlimited fresh allowance each
# request — which is what keying on something unknown would do.
_UNKNOWN_CLIENT_KEY = "demo_unknown"


def _client_address(request: Request | None, settings: Settings) -> str | None:
    """Best-effort client address: trusted header first, then the socket peer."""
    if request is None:
        return None

    header = (settings.rate_limit_client_ip_header or "").strip()
    if header:
        raw = request.headers.get(header)
        if raw:
            # X-Forwarded-For is a list, "client, proxy1, proxy2". The LEFTMOST
            # entry is the original client. Single-value headers (Fly-Client-IP,
            # CF-Connecting-IP) are unaffected by the split.
            candidate = raw.split(",")[0].strip()
            if candidate:
                return candidate

    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return host or None


def _normalise_address(raw: str) -> str | None:
    """Canonicalise an address, collapsing IPv6 to its /64.

    A single IPv6 customer is routinely handed a whole /64, so limiting per
    ADDRESS would be free to evade — one visitor could walk through billions of
    source addresses. IPv4 is used as-is.
    """
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if addr.version == 6:
        network = ipaddress.ip_network(f"{addr}/64", strict=False)
        return str(network)
    return str(addr)


def client_rate_key(request: Request | None, settings: Settings) -> str:
    """Return the demo rate-limit bucket key for this request.

    The address is HMAC'd, never stored raw: an IP is personal data, and an
    unsalted hash of an IPv4 address is reversible by brute force (2^32
    candidates). The salt falls back to the demo API key, which production
    already requires to be a strong, non-default secret.
    """
    raw = _client_address(request, settings)
    normalised = _normalise_address(raw) if raw else None
    if normalised is None:
        return _UNKNOWN_CLIENT_KEY

    salt = (settings.rate_limit_key_salt or settings.demo_api_key or "").encode()
    digest = hmac.new(salt, normalised.encode(), hashlib.sha256).hexdigest()
    return f"demo_{digest[:32]}"


async def rate_limited_demo(
    request: Request,
    user_id: Annotated[str, Depends(require_demo_api_key)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """Demo-user auth + per-visitor rate limit. Returns the demo user id.

    Returns ``user_id`` unchanged so every route signature and all downstream
    attribution stay exactly as they were; only the limiter's key differs.
    """
    await enforce_rate_limit(
        user_id=client_rate_key(request, settings),
        role="demo_user",
        settings=settings,
    )
    # Backstop across all visitors. Per-visitor limiting alone leaves a
    # distributed source unbounded; this caps total request volume. Checked
    # AFTER the per-visitor limit so an individual flood is attributed to that
    # visitor rather than burning the shared allowance.
    if settings.rate_limit_global_per_hour > 0:
        await enforce_rate_limit(user_id=_GLOBAL_BUCKET_KEY, role=_GLOBAL_ROLE, settings=settings)
    return user_id


async def rate_limited_admin(
    user_id: Annotated[str, Depends(require_admin_api_key)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """Admin auth + rate limit. Returns the admin user id."""
    await enforce_rate_limit(user_id=user_id, role="admin", settings=settings)
    return user_id


__all__ = [
    "RateLimiter",
    "RedisRateLimiter",
    "client_rate_key",
    "enforce_rate_limit",
    "get_limiter",
    "rate_limited_admin",
    "rate_limited_demo",
    "reset_limiter",
]
