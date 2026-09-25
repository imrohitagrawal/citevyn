"""Verify a Stripe webhook signature. Pure mechanism: no app imports (AGENTS.md).

Stripe's scheme (https://docs.stripe.com/webhooks#verify-manually):
``Stripe-Signature: t=<unix seconds>,v1=<hex>[,v1=<hex>...]``, where each ``v1`` is
HMAC-SHA256(endpoint secret, ``f"{t}.{raw body}"``). More than one ``v1`` appears
while a secret is being rolled; any match is enough. ``t`` must be within
``tolerance_s`` of now in BOTH directions: an old ``t`` is a replayed capture and a
far-future one is forged. (A replay inside the window is stopped separately, by
recording each event id once.)

Only ``v1`` is accepted. The raw body must be the exact bytes received: parsing
and re-serialising JSON changes them.
"""

from __future__ import annotations

import hashlib
import hmac


class SignatureError(Exception):
    """The request is not a genuine, fresh Stripe delivery."""


def verify_signature(
    payload: bytes, header: str, secret: str, *, tolerance_s: int, now: int
) -> None:
    """Return quietly for a valid signature; raise :class:`SignatureError` otherwise."""
    if not secret:
        raise SignatureError("no webhook secret configured")
    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise SignatureError("timestamp is not a number") from exc
        elif key == "v1" and value:
            candidates.append(value)
    if timestamp is None or not candidates:
        raise SignatureError("missing timestamp or v1 signature")
    if abs(now - timestamp) > tolerance_s:
        raise SignatureError("timestamp outside the tolerance window")
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise SignatureError("no signature matches")


__all__ = ["SignatureError", "verify_signature"]
