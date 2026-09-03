"""Tests for the Slice 9a Redis sliding-window rate limiter.

Uses :mod:`fakeredis.aioredis` so the suite remains hermetic — no
external Redis service required. Verifies the core contract: the
limiter records a hit, accepts up to ``limit`` hits in the window,
and rejects the ``limit + 1``-th hit with the standard error
envelope.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core import rate_limit


@pytest.fixture
def fake_redis():
    """Yield a fakeredis async client and reset the rate-limit singletons."""
    import fakeredis.aioredis as fake_aioredis

    rate_limit.reset_limiter()
    client = fake_aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        rate_limit.reset_limiter()
        import asyncio

        asyncio.run(client.aclose())


async def test_redis_limiter_accepts_hits_under_limit(fake_redis) -> None:
    """A new user is allowed to make ``limit`` requests in a row."""
    limiter = rate_limit.RedisRateLimiter(
        client=fake_redis,
        window_seconds=60,
        demo_user_per_window=3,
        admin_per_window=10,
        key_prefix="citevyn:rl:test",
    )
    for _ in range(3):
        await limiter.check(user_id="alice", role="demo_user")


async def test_redis_limiter_rejects_overflow(fake_redis) -> None:
    """The ``limit + 1``-th hit raises the standard 429 envelope."""
    from fastapi import HTTPException

    from app.core.errors import APIErrorCode

    class _LowLimit(rate_limit.RedisRateLimiter):
        def limit_for(self, *, role: str) -> int:  # type: ignore[override]
            return 2

    limiter = _LowLimit(
        client=fake_redis,
        window_seconds=60,
        demo_user_per_window=2,
        admin_per_window=10,
        key_prefix="citevyn:rl:test",
    )
    await limiter.check(user_id="alice", role="demo_user")
    await limiter.check(user_id="alice", role="demo_user")
    with pytest.raises(HTTPException) as exc_info:
        await limiter.check(user_id="alice", role="demo_user")
    assert exc_info.value.status_code == 429
    assert APIErrorCode.rate_limited.value in str(exc_info.value.detail)


async def test_redis_limiter_isolates_users(fake_redis) -> None:
    """A second user has its own bucket and is not affected by the first user's overflow."""
    from fastapi import HTTPException

    class _LowLimit(rate_limit.RedisRateLimiter):
        def limit_for(self, *, role: str) -> int:  # type: ignore[override]
            return 1

    limiter = _LowLimit(
        client=fake_redis,
        window_seconds=60,
        demo_user_per_window=1,
        admin_per_window=10,
        key_prefix="citevyn:rl:test",
    )
    await limiter.check(user_id="alice", role="demo_user")
    with pytest.raises(HTTPException):
        await limiter.check(user_id="alice", role="demo_user")
    # Bob is untouched.
    await limiter.check(user_id="bob", role="demo_user")


async def test_get_limiter_returns_redis_when_url_set(fake_redis, monkeypatch) -> None:
    """``get_limiter`` returns the Redis implementation when ``redis_url`` is set."""
    from app.core import redis_client
    from app.core.config import Settings

    # Redirect the redis_client factory to return the fakeredis client
    # instead of opening a real connection. The factory is now
    # parameterless (URL is read from settings).
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: fake_redis)
    rate_limit.reset_limiter()
    settings = Settings(redis_url="redis://localhost:6379/0")
    limiter = rate_limit.get_limiter(settings)
    assert isinstance(limiter, rate_limit.RedisRateLimiter)


async def test_get_limiter_returns_in_process_without_url() -> None:
    """Without ``redis_url`` the in-process :class:`RateLimiter` is returned."""
    from app.core.config import Settings

    rate_limit.reset_limiter()
    settings = Settings()
    limiter = rate_limit.get_limiter(settings)
    assert isinstance(limiter, rate_limit.RateLimiter)


def test_enforce_rate_limit_disabled_skips_check() -> None:
    """``rate_limit_enabled=False`` short-circuits before any check."""
    from app.core.config import Settings

    rate_limit.reset_limiter()
    settings = Settings(rate_limit_enabled=False)
    import asyncio

    # Should not raise even with a tight limit.
    asyncio.run(rate_limit.enforce_rate_limit(user_id="alice", role="demo_user", settings=settings))


def test_redis_limiter_rejects_empty_prefix() -> None:
    """A blank ``key_prefix`` raises — the prefix is a safety net for shared Redis."""
    import fakeredis.aioredis as fake_aioredis

    client = fake_aioredis.FakeRedis(decode_responses=True)
    import asyncio

    asyncio.run(client.aclose())
    with pytest.raises(ValueError, match="key_prefix"):
        rate_limit.RedisRateLimiter(
            client=client,
            window_seconds=60,
            demo_user_per_window=3,
            admin_per_window=10,
            key_prefix="",
        )


async def test_redis_limiter_fails_closed_on_redis_outage(monkeypatch) -> None:
    """When the Redis EVAL raises, the limiter must fail closed (503), not open.

    A fail-open limiter would let a Redis outage silently disable
    the rate-limit control. The contract under test is
    documented in :class:`rate_limit.RedisRateLimiter.check`.
    """
    from fastapi import HTTPException

    from app.core.errors import APIErrorCode

    class _BrokenClient:
        async def eval(self, *args, **kwargs):  # noqa: ANN001
            import redis.exceptions

            raise redis.exceptions.ConnectionError("simulated outage")

    broken_limiter = rate_limit.RedisRateLimiter(
        client=_BrokenClient(),  # type: ignore[arg-type]
        window_seconds=60,
        demo_user_per_window=3,
        admin_per_window=10,
        key_prefix="citevyn:rl:test",
    )
    with pytest.raises(HTTPException) as exc_info:
        await broken_limiter.check(user_id="alice", role="demo_user")
    assert exc_info.value.status_code == 503
    # Regression (#167): the code must name the limiter, not the search index.
    # Asserting on the parsed envelope (not a substring of ``str(detail)``)
    # means a mutation back to ``index_unavailable`` cannot pass by accident.
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"]["code"] == APIErrorCode.rate_limiter_unavailable.value
    assert detail["error"]["code"] != APIErrorCode.index_unavailable.value
    assert APIErrorCode.index_unavailable.value not in str(detail)


async def test_redis_limiter_outage_code_is_not_index_unavailable_on_admin_role() -> None:
    """Edge case: the accurate code is role-independent (#167).

    The bug was found on an ADMIN promote call, so pin the admin path too —
    a fix applied only to the demo branch would be invisible otherwise.
    """
    from fastapi import HTTPException

    from app.core.errors import APIErrorCode, status_code_for

    class _BrokenClient:
        async def eval(self, *args, **kwargs):  # noqa: ANN001
            raise OSError("socket closed")

    limiter = rate_limit.RedisRateLimiter(
        client=_BrokenClient(),  # type: ignore[arg-type]
        window_seconds=60,
        demo_user_per_window=3,
        admin_per_window=10,
        key_prefix="citevyn:rl:test",
    )
    with pytest.raises(HTTPException) as exc_info:
        await limiter.check(user_id="root", role="admin")
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"]["code"] == APIErrorCode.rate_limiter_unavailable.value
    # The status stays 503 — this change is code-only, not a behaviour change
    # for clients that branch on status.
    assert exc_info.value.status_code == status_code_for(APIErrorCode.rate_limiter_unavailable)
    assert exc_info.value.status_code == 503


async def test_redis_limiter_outage_message_matches_the_code(fake_redis) -> None:
    """The human message and the machine code must agree (#167).

    The original bug was exactly this disagreement: a ``Rate limiter is
    temporarily unavailable.`` message carrying an ``index_unavailable`` code.
    """
    from fastapi import HTTPException

    from app.core.errors import APIErrorCode

    class _BrokenClient:
        async def eval(self, *args, **kwargs):  # noqa: ANN001
            import redis.exceptions

            raise redis.exceptions.TimeoutError("simulated timeout")

    limiter = rate_limit.RedisRateLimiter(
        client=_BrokenClient(),  # type: ignore[arg-type]
        window_seconds=60,
        demo_user_per_window=3,
        admin_per_window=10,
        key_prefix="citevyn:rl:test",
    )
    with pytest.raises(HTTPException) as exc_info:
        await limiter.check(user_id="alice", role="demo_user")
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert "rate limiter" in detail["error"]["message"].lower()
    assert detail["error"]["code"] == APIErrorCode.rate_limiter_unavailable.value


async def test_healthy_redis_never_yields_the_outage_code(fake_redis) -> None:
    """Happy path: a working limiter raises 429 (not the outage code) at the cap."""
    from fastapi import HTTPException

    from app.core.errors import APIErrorCode

    limiter = rate_limit.RedisRateLimiter(
        client=fake_redis,
        window_seconds=60,
        demo_user_per_window=1,
        admin_per_window=10,
        key_prefix="citevyn:rl:test",
    )
    await limiter.check(user_id="alice", role="demo_user")
    with pytest.raises(HTTPException) as exc_info:
        await limiter.check(user_id="alice", role="demo_user")
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"]["code"] == APIErrorCode.rate_limited.value


async def test_redis_limiter_has_its_own_magic_link_bucket(fake_redis) -> None:
    """ADR-0004 PR 14 on the PRODUCTION limiter: RED if ``_MAGIC_LINK_ROLE`` is
    dropped from ``RedisRateLimiter._limits`` (``limit_for`` would silently
    fall back to the 30/hour demo limit) or if the magic-link and auth-login
    roles share a bucket key."""
    from app.core.config import Settings

    limiter = rate_limit.RedisRateLimiter(
        client=fake_redis,
        window_seconds=60,
        demo_user_per_window=30,
        admin_per_window=10,
        key_prefix="citevyn:rl:test",
        magic_link_per_window=2,
    )
    assert limiter.limit_for(role="magic_link") == 2
    await limiter.check(user_id="magiclink_abc", role="magic_link")
    await limiter.check(user_id="magiclink_abc", role="magic_link")
    with pytest.raises(HTTPException) as excinfo:
        await limiter.check(user_id="magiclink_abc", role="magic_link")
    assert excinfo.value.status_code == 429
    # A different bucket key (the auth_login role uses its own prefix) is untouched.
    await limiter.check(user_id="authlogin_abc", role="auth_login")

    matching = Settings(
        redis_url="redis://localhost:6379/0",
        rate_limit_window_seconds=60,
        rate_limit_demo_user_per_hour=30,
        rate_limit_admin_per_hour=10,
        rate_limit_magic_link_per_hour=2,
    )
    assert rate_limit._settings_match(limiter, matching)
    changed = matching.model_copy(update={"rate_limit_magic_link_per_hour": 3})
    assert not rate_limit._settings_match(limiter, changed)


# ---------------------------------------------------------------------------
# Per-role window on the REDIS path (#301)
# ---------------------------------------------------------------------------
#
# This is the limiter PRODUCTION runs (Upstash on Fly). A mutation survived
# without this test: replacing ``window = self.window_for(role=role)`` with
# ``self._window_seconds`` in the Redis ``check`` left all 12 Redis tests green,
# because none of them exercised a role whose window differs from the
# limiter-wide one. The in-process twin of this test does not cover it — the two
# limiters have separate ``check`` implementations, and only one of them ships.


async def test_magic_link_interval_counts_over_its_own_window_on_redis(
    fake_redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interval role must count over its own window on the Redis path too.

    The Lua script receives the cutoff and the TTL as arguments, so a per-role
    window flows through without touching the script — but only if ``check``
    actually passes the per-role value. That is what this pins.

    Drives a controlled clock (the Redis limiter uses ``time.time``) rather than
    sleeping. RED if ``check`` passes ``self._window_seconds``.
    """
    now = [1_000_000.0]
    monkeypatch.setattr("app.core.rate_limit.time.time", lambda: now[0])

    limiter = rate_limit.RedisRateLimiter(
        client=fake_redis,
        window_seconds=3600,  # the hour every other role uses
        demo_user_per_window=30,
        admin_per_window=100,
        key_prefix="citevyn:rl:test",
        magic_link_interval_seconds=60,
    )
    role = rate_limit._MAGIC_LINK_INTERVAL_ROLE
    key = "mlinterval_probe"

    await limiter.check(user_id=key, role=role)  # accepted

    now[0] += 30  # inside the floor
    with pytest.raises(HTTPException) as refused:
        await limiter.check(user_id=key, role=role)
    assert refused.value.status_code == 429

    now[0] += 31  # 61s after the first — the floor has elapsed
    await limiter.check(user_id=key, role=role)  # accepted again

    # Partner assertion: an ordinary role is still counted over the full hour on
    # this path, so the change cannot have shortened everyone else's window.
    await limiter.check(user_id="demo_probe", role="demo_user")
    now[0] += 61
    with_hits = await fake_redis.zcard("citevyn:rl:test:demo_probe")
    assert with_hits == 1, "an ordinary role's hit aged out of the hour far too early"


async def test_redis_window_for_falls_back_to_the_limiter_window(fake_redis) -> None:
    """Only the interval role overrides the window; everything else inherits it."""
    limiter = rate_limit.RedisRateLimiter(
        client=fake_redis,
        window_seconds=3600,
        demo_user_per_window=30,
        admin_per_window=100,
        key_prefix="citevyn:rl:test",
        magic_link_interval_seconds=45,
    )
    assert limiter.window_for(role=rate_limit._MAGIC_LINK_INTERVAL_ROLE) == 45
    for role in ("demo_user", "admin", "global", "auth_login", "magic_link"):
        assert limiter.window_for(role=role) == 3600


async def test_redis_bucket_ttl_follows_the_per_role_window(fake_redis) -> None:
    """The bucket's Redis TTL must be the ROLE's window, not the limiter-wide one.

    The Lua script takes the TTL as an argument, so passing the wrong one is a one-token
    edit with no visible symptom in any other test: a 60s interval bucket would be kept
    alive for 3601s. That wastes nothing functionally — the sliding window still evicts
    by score — but it pins a key per address for an hour instead of a minute, and on a
    metered Upstash plan that is real memory for every address anyone ever probes.

    RED if ``check`` passes ``self._window_seconds + 1``. Verified: that mutation left
    all 14 Redis tests green before this test existed.
    """
    limiter = rate_limit.RedisRateLimiter(
        client=fake_redis,
        window_seconds=3600,
        demo_user_per_window=30,
        admin_per_window=100,
        key_prefix="citevyn:rl:test",
        magic_link_interval_seconds=60,
    )

    await limiter.check(user_id="ttl_probe", role=rate_limit._MAGIC_LINK_INTERVAL_ROLE)
    interval_ttl = await fake_redis.ttl("citevyn:rl:test:ttl_probe")
    assert 0 < interval_ttl <= 61, (
        f"interval bucket TTL should track its 60s window, got {interval_ttl}"
    )

    # Partner assertion: an ordinary role still gets the limiter-wide hour, so this
    # cannot have shortened everyone else's TTL.
    await limiter.check(user_id="hour_probe", role="demo_user")
    hourly_ttl = await fake_redis.ttl("citevyn:rl:test:hour_probe")
    assert hourly_ttl > 3000, (
        f"an ordinary role's TTL should still track the hour, got {hourly_ttl}"
    )
