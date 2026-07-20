"""Tests for the Slice 9a Redis sliding-window rate limiter.

Uses :mod:`fakeredis.aioredis` so the suite remains hermetic — no
external Redis service required. Verifies the core contract: the
limiter records a hit, accepts up to ``limit`` hits in the window,
and rejects the ``limit + 1``-th hit with the standard error
envelope.
"""

from __future__ import annotations

import pytest

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
