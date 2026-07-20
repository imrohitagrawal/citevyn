"""Per-visitor rate-limit identity (#203).

The demo API key is shared by construction, so ``require_demo_api_key`` returns a
CONSTANT. Keying the limiter on it gave every visitor on earth ONE bucket: 30
questions from one person denied the demo to everyone else for a rolling hour,
and because the bucket lives in Redis a restart no longer cleared it.

These tests pin the properties that make the fix real, rather than merely present:

  * two visitors get INDEPENDENT quotas (the whole point);
  * one visitor is still limited (we did not just disable the limiter);
  * a whole IPv6 /64 is ONE visitor (per-address keying is free to evade — a
    single customer is routinely handed billions of addresses);
  * raw IP addresses never reach the bucket key (an IP is personal data, and an
    unsalted hash of an IPv4 address is reversible by brute force);
  * an untrusted/garbled address degrades to the OLD shared bucket rather than
    minting a fresh unlimited allowance per request — failing closed, not open;
  * the header is only honoured when configured, so it cannot be spoofed when the
    deployment does not sit behind a trusted proxy.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.core.rate_limit import (
    _GLOBAL_BUCKET_KEY,
    _UNKNOWN_CLIENT_KEY,
    RateLimiter,
    client_rate_key,
)


class _FakeClient:
    def __init__(self, host: str | None) -> None:
        self.host = host


class _FakeRequest:
    """The two attributes ``client_rate_key`` reads: headers and the peer."""

    def __init__(self, headers: dict[str, str] | None = None, peer: str | None = None) -> None:
        self.headers = headers or {}
        self.client = _FakeClient(peer) if peer is not None else None


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "rate_limit_client_ip_header": "Fly-Client-IP",
        "rate_limit_key_salt": "test-salt-not-a-real-secret",
    }
    base.update(overrides)
    return Settings(**base)


def _key(headers: dict[str, str] | None = None, peer: str | None = None, **s: Any) -> str:
    return client_rate_key(_FakeRequest(headers, peer), _settings(**s))


# --- identity -------------------------------------------------------------


def test_distinct_addresses_get_distinct_keys() -> None:
    """The property the whole change exists for."""
    a = _key({"Fly-Client-IP": "203.0.113.7"})
    b = _key({"Fly-Client-IP": "203.0.113.8"})
    assert a != b


def test_same_address_is_stable_across_requests() -> None:
    """A visitor must land in the SAME bucket every request, or they are unlimited."""
    assert _key({"Fly-Client-IP": "203.0.113.7"}) == _key({"Fly-Client-IP": "203.0.113.7"})


def test_ipv6_addresses_in_one_slash_64_share_a_bucket() -> None:
    """A /64 is one customer. Per-address keying would be free to evade."""
    a = _key({"Fly-Client-IP": "2001:db8:abcd:1234::1"})
    b = _key({"Fly-Client-IP": "2001:db8:abcd:1234:ffff:ffff:ffff:ffff"})
    assert a == b


def test_different_ipv6_slash_64s_do_not_share_a_bucket() -> None:
    """...but the collapse must not be so coarse that everyone shares one key."""
    a = _key({"Fly-Client-IP": "2001:db8:abcd:1234::1"})
    b = _key({"Fly-Client-IP": "2001:db8:abcd:9999::1"})
    assert a != b


# --- privacy --------------------------------------------------------------


def test_key_does_not_contain_the_raw_address() -> None:
    """A raw IP must never reach Redis — it is personal data."""
    key = _key({"Fly-Client-IP": "203.0.113.7"})
    assert "203.0.113.7" not in key


def test_salt_changes_the_key() -> None:
    """Unsalted, an IPv4 hash is reversible by brute force (2^32 candidates)."""
    a = _key({"Fly-Client-IP": "203.0.113.7"}, rate_limit_key_salt="salt-one")
    b = _key({"Fly-Client-IP": "203.0.113.7"}, rate_limit_key_salt="salt-two")
    assert a != b


def test_salt_falls_back_to_the_demo_api_key_when_unset() -> None:
    """Production already requires the demo key to be a strong, non-default secret."""
    a = _key(
        {"Fly-Client-IP": "203.0.113.7"},
        rate_limit_key_salt="",
        demo_api_key="first-strong-demo-key",
    )
    b = _key(
        {"Fly-Client-IP": "203.0.113.7"},
        rate_limit_key_salt="",
        demo_api_key="second-strong-demo-key",
    )
    assert a != b


# --- trust / failure modes -------------------------------------------------


def test_header_is_ignored_when_no_header_is_configured() -> None:
    """Trusting a header is only safe behind a proxy. Empty config = trust nothing.

    Otherwise anyone could send ``Fly-Client-IP: <random>`` per request and mint an
    unlimited series of fresh buckets.
    """
    key = _key(
        {"Fly-Client-IP": "203.0.113.7"},
        peer="198.51.100.2",
        rate_limit_client_ip_header="",
    )
    assert key == _key(peer="198.51.100.2", rate_limit_client_ip_header="")


def test_falls_back_to_the_socket_peer_when_the_header_is_absent() -> None:
    key = _key({}, peer="198.51.100.2")
    assert key not in (_UNKNOWN_CLIENT_KEY, "")


def test_forwarded_for_uses_the_leftmost_entry() -> None:
    """X-Forwarded-For is "client, proxy1, proxy2" — the client is leftmost."""
    key = _key(
        {"X-Forwarded-For": "203.0.113.7, 70.41.3.18, 150.172.238.178"},
        rate_limit_client_ip_header="X-Forwarded-For",
    )
    assert key == _key(
        {"X-Forwarded-For": "203.0.113.7"},
        rate_limit_client_ip_header="X-Forwarded-For",
    )


@pytest.mark.parametrize(
    "bad",
    ["not-an-ip", "", "   ", "999.999.999.999", "203.0.113.7:443", "<script>"],
)
def test_unparseable_addresses_share_the_known_fallback_bucket(bad: str) -> None:
    """Fail CLOSED to the old shared bucket.

    Minting a per-request key from something unparseable would hand out a fresh
    unlimited allowance on every request — strictly worse than the bug being fixed.
    """
    assert _key({"Fly-Client-IP": bad}) == _UNKNOWN_CLIENT_KEY


def test_no_request_at_all_falls_back_safely() -> None:
    assert client_rate_key(None, _settings()) == _UNKNOWN_CLIENT_KEY


def test_request_without_a_peer_falls_back_safely() -> None:
    assert client_rate_key(_FakeRequest({}, peer=None), _settings()) == (_UNKNOWN_CLIENT_KEY)


# --- the limiter actually honours the separation ---------------------------


async def test_two_visitors_do_not_consume_each_others_quota() -> None:
    """End-to-end over the real limiter: the #203 regression itself.

    Before the fix both visitors keyed on the constant ``demo_user``, so visitor B
    was locked out by visitor A's traffic.
    """
    limiter = RateLimiter(window_seconds=60, demo_user_per_window=3, admin_per_window=10)
    a = _key({"Fly-Client-IP": "203.0.113.7"})
    b = _key({"Fly-Client-IP": "203.0.113.8"})

    for _ in range(3):
        await limiter.check(user_id=a, role="demo_user")
    with pytest.raises(HTTPException):
        await limiter.check(user_id=a, role="demo_user")

    # B is untouched by A exhausting their quota.
    await limiter.check(user_id=b, role="demo_user")


async def test_one_visitor_is_still_limited() -> None:
    """Non-vacuity: we separated the buckets, we did not disable the limiter."""
    limiter = RateLimiter(window_seconds=60, demo_user_per_window=2, admin_per_window=10)
    key = _key({"Fly-Client-IP": "203.0.113.7"})
    for _ in range(2):
        await limiter.check(user_id=key, role="demo_user")
    with pytest.raises(HTTPException):
        await limiter.check(user_id=key, role="demo_user")


async def test_global_backstop_has_its_own_far_higher_limit() -> None:
    """The shared bucket must NOT inherit the 30/hour demo limit.

    ``limit_for`` falls back to the demo limit for unknown roles, so a missing
    "global" registration would silently re-create the #203 lockout on the shared
    bucket. This is that guard.
    """
    limiter = RateLimiter(
        window_seconds=60,
        demo_user_per_window=2,
        admin_per_window=10,
        global_per_window=50,
    )
    assert limiter.limit_for(role="global") == 50
    assert limiter.limit_for(role="global") > limiter.limit_for(role="demo_user")

    # And it is reachable: the shared key survives well past the demo limit.
    for _ in range(10):
        await limiter.check(user_id=_GLOBAL_BUCKET_KEY, role="global")


def test_global_limit_must_be_positive() -> None:
    """A zero would deny every request — "disabled" is expressed by the caller."""
    with pytest.raises(ValueError):
        RateLimiter(
            window_seconds=60,
            demo_user_per_window=2,
            admin_per_window=10,
            global_per_window=0,
        )
