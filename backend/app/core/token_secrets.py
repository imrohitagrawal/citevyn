"""Pure bearer-token primitives for emailed credentials (ADR-0004 PR 14).

Three stdlib-only functions -- generate, hash, verify -- and nothing else.
This module has **no database, no settings and no ``app.*`` imports** on
purpose, the same boundary ``app.core.oauth_http`` draws for itself: the
mechanism (a random secret whose SHA-256 digest is the only thing stored)
transfers to any stack unchanged, while single-use, expiry and storage
policy are the caller's business (``app.api.routes.magic_link``) and must
never be bolted on here.

The shape mirrors ``AuthSession``'s own cookie credential
(``app.core.auth_sessions``): 32 random bytes, hex-encoded, compared in
constant time against a stored digest. A magic-link token IS a bearer
credential exactly the way the session cookie is -- possession alone logs
you in -- so it deliberately reuses that proven design rather than inventing
a new token format.
"""

from __future__ import annotations

import hashlib
import secrets

# 32 random bytes -> 64 hex chars; the same entropy the session cookie carries.
_SECRET_BYTES = 32


def generate_token() -> str:
    """Return a fresh random secret (hex, 64 chars). Never stored as-is."""
    return secrets.token_hex(_SECRET_BYTES)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of ``token`` -- the only form ever persisted.

    A leaked database row therefore cannot be replayed as a credential, the
    same property ``AuthSession.secret_hash`` gives the session cookie.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, hashed: str) -> bool:
    """Constant-time check of ``token`` against a stored digest.

    ``secrets.compare_digest`` (not ``==``): a byte-at-a-time comparison would
    let response timing narrow the digest down one byte at a time -- the same
    class of bug ``app.core.security`` fixed for the demo bearer.
    """
    return secrets.compare_digest(hash_token(token), hashed)


__all__ = ["generate_token", "hash_token", "verify_token"]
