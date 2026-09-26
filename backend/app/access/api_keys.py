"""Per-user API keys: make one, and authenticate one (ADR-0005 §4, Phase 6A).

The key the user holds is ``cvk_<key_id>_<secret>``: 32 hex characters that
find the row, then the 64-hex secret whose SHA-256 is the only thing stored
(``app.core.token_secrets``, the design the session cookie and magic links
already use). Compared in constant time; a revoked key never authenticates.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.token_secrets import generate_token, hash_token, verify_token
from app.models import ApiKey

PREFIX = "cvk_"
_SHAPE = re.compile(r"cvk_([0-9a-f]{32})_([0-9a-f]{64})")
#: How much of a key is shown in lists: enough to tell keys apart, useless alone.
DISPLAY_CHARS = len(PREFIX) + 8


@dataclass(frozen=True)
class NewKey:
    row: ApiKey
    raw: str  # shown to the user once, never stored


def make_key(user_id: str, name: str, now: datetime) -> NewKey:
    """A new key for ``user_id``. The caller adds ``row`` and commits."""
    key_id = secrets.token_hex(16)
    secret = generate_token()
    row = ApiKey(
        key_id=key_id,
        user_id=user_id,
        name=name,
        secret_hash=hash_token(secret),
        scope="mcp",
        created_at=now,
    )
    return NewKey(row=row, raw=f"{PREFIX}{key_id}_{secret}")


async def authenticate_api_key(db: AsyncSession, raw: str) -> ApiKey | None:
    """The live key ``raw`` names, or None. Records when it was last used; the
    caller commits."""
    match = _SHAPE.fullmatch(raw.strip())
    if match is None:
        return None
    key_id, secret = match.groups()
    row = await db.get(ApiKey, key_id)
    if row is None or row.revoked_at is not None:
        return None
    if not verify_token(secret, row.secret_hash):
        return None
    row.last_used_at = datetime.now(UTC)
    return row


__all__ = ["DISPLAY_CHARS", "PREFIX", "NewKey", "authenticate_api_key", "make_key"]
