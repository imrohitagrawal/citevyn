"""Stripe webhook signature verification (ADR-0005 §2, entitlement).

Stripe signs each webhook: ``Stripe-Signature: t=<unix>,v1=<hex>[,v1=<hex>...]``,
where each ``v1`` is HMAC-SHA256(secret, f"{t}.{raw_body}"). Several ``v1`` values
appear while a secret is being rolled. A signature is valid if ANY ``v1`` matches
and ``t`` is within the tolerance of now, in either direction: an old ``t`` is a
replayed capture, a far-future one is forged.

Each test says which change turns it red.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.billing.stripe_signature import SignatureError, verify_signature

SECRET = "whsec_test_secret"
BODY = b'{"id":"evt_1","type":"customer.subscription.updated"}'
NOW = 1_800_000_000


def _sig(body: bytes, t: int, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def _header(t: int, *sigs: str) -> str:
    return ",".join([f"t={t}", *(f"v1={s}" for s in sigs)])


def test_a_valid_signature_passes() -> None:
    """Turns red if: a genuine Stripe delivery is refused."""
    verify_signature(BODY, _header(NOW, _sig(BODY, NOW)), SECRET, tolerance_s=300, now=NOW)


def test_any_of_several_v1_values_may_match() -> None:
    """During a secret roll Stripe sends two v1 values. Turns red if: only the
    first is checked."""
    verify_signature(
        BODY, _header(NOW, "00" * 32, _sig(BODY, NOW)), SECRET, tolerance_s=300, now=NOW
    )


@pytest.mark.parametrize(
    ("header", "why"),
    [
        (_header(NOW, _sig(BODY, NOW, "whsec_other")), "signed with another secret"),
        (_header(NOW, _sig(b'{"id":"evt_2"}', NOW)), "signature of a different body"),
        (_header(NOW, "zz"), "not hex"),
        (f"t={NOW}", "no v1"),
        (f"v1={_sig(BODY, NOW)}", "no timestamp"),
        ("", "empty header"),
        (f"t=abc,v1={_sig(BODY, NOW)}", "timestamp not a number"),
        (
            _header(NOW, _sig(BODY, NOW)).replace("v1=", "v0="),
            "only a v0 (test-mode legacy) scheme",
        ),
    ],
)
def test_a_forged_or_malformed_signature_is_refused(header: str, why: str) -> None:
    """Turns red if: any of these is accepted."""
    with pytest.raises(SignatureError):
        verify_signature(BODY, header, SECRET, tolerance_s=300, now=NOW)


def test_the_timestamp_is_part_of_what_is_signed() -> None:
    """Moving t to a fresh value without re-signing must fail, or an attacker could
    replay an old body by editing only t. Turns red if: the signature ignores t."""
    old = NOW - 3600
    with pytest.raises(SignatureError):
        verify_signature(BODY, _header(NOW, _sig(BODY, old)), SECRET, tolerance_s=300, now=NOW)


@pytest.mark.parametrize("offset", [-301, 301])
def test_a_timestamp_outside_the_tolerance_is_refused(offset: int) -> None:
    """Old = a replayed capture; far future = forged. Turns red if: either
    direction is unchecked, or the window is off by one."""
    t = NOW + offset
    with pytest.raises(SignatureError):
        verify_signature(BODY, _header(t, _sig(BODY, t)), SECRET, tolerance_s=300, now=NOW)


@pytest.mark.parametrize("offset", [-300, 300])
def test_the_tolerance_edge_is_inside(offset: int) -> None:
    """Partner: exactly at the edge still passes."""
    t = NOW + offset
    verify_signature(BODY, _header(t, _sig(BODY, t)), SECRET, tolerance_s=300, now=NOW)


def test_an_empty_secret_refuses_everything() -> None:
    """A missing configuration must not accept an unsigned or empty-key HMAC.
    Turns red if: an empty secret verifies a signature made with an empty key."""
    with pytest.raises(SignatureError):
        verify_signature(BODY, _header(NOW, _sig(BODY, NOW, "")), "", tolerance_s=300, now=NOW)


def test_the_module_imports_nothing_from_the_app() -> None:
    """AGENTS.md: protocol code stays app-independent. Turns red if: it grows an
    ``from app.`` import."""
    import inspect

    import app.billing.stripe_signature as mod

    assert "from app." not in inspect.getsource(mod)
