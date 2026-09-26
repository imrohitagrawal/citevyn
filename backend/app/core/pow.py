"""A self-hosted proof-of-work challenge. Pure mechanism: no app imports (AGENTS.md).

The sign-up bot check (ADR-0005 Phase 5). The server picks a secret number,
publishes ``SHA-256(salt + number)`` and an HMAC signature over it; the browser
tries numbers from 0 upward until the hash matches, which takes about
``maxnumber / 2`` hashes; the server checks the signature, the work and the
expiry. The same scheme as ALTCHA, written here because it is ~60 lines of
``hashlib``/``hmac`` and a vendor widget would need a third-party script, a
change to the Content-Security-Policy and sending visitor data elsewhere.

It raises the cost of scripting sign-ups; it does not prove a human. The ADR
relies on it together with per-IP limits, email verification and the spend cap.
Single use is the caller's job: this module is stateless.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any


class PowError(Exception):
    """The solution is missing, malformed, forged, wrong or expired."""


def _hash(salt: str, number: int) -> str:
    return hashlib.sha256(f"{salt}{number}".encode()).hexdigest()


def _sign(secret: str, salt: str, challenge: str) -> str:
    return hmac.new(secret.encode(), f"{salt}|{challenge}".encode(), hashlib.sha256).hexdigest()


def make_challenge(secret: str, *, max_number: int, ttl_s: int, now: int) -> dict[str, Any]:
    """A new challenge for the browser. The number itself is never sent."""
    if not secret:
        raise ValueError("an empty secret would make every signature forgeable")
    salt = f"{secrets.token_hex(12)}?expires={now + ttl_s}"
    challenge = _hash(salt, secrets.randbelow(max_number + 1))
    return {
        "algorithm": "SHA-256",
        "salt": salt,
        "challenge": challenge,
        "maxnumber": max_number,
        "signature": _sign(secret, salt, challenge),
    }


def solve(challenge: dict[str, Any]) -> int:
    """Find the number (what the browser does). Used by tests and tooling."""
    salt, target, top = (
        str(challenge["salt"]),
        str(challenge["challenge"]),
        int(challenge["maxnumber"]),
    )
    for n in range(top + 1):
        if _hash(salt, n) == target:
            return n
    raise PowError("no solution in range")


def verify_solution(secret: str, payload: dict[str, Any], *, now: int) -> str:
    """Check a solved challenge; return its ``challenge`` (the single-use id)."""
    salt, number = payload.get("salt"), payload.get("number")
    challenge, signature = payload.get("challenge"), payload.get("signature")
    if not (
        isinstance(salt, str)
        and isinstance(challenge, str)
        and isinstance(signature, str)
        and isinstance(number, int)
        and not isinstance(number, bool)
    ):
        raise PowError("malformed solution")
    if not hmac.compare_digest(_sign(secret, salt, challenge), signature):
        raise PowError("not a challenge we issued")
    if not hmac.compare_digest(_hash(salt, number), challenge):
        raise PowError("wrong answer")
    _, _, expires = salt.rpartition("?expires=")
    if not expires.isdigit() or int(expires) < now:
        raise PowError("expired")
    return challenge


__all__ = ["PowError", "make_challenge", "solve", "verify_solution"]
