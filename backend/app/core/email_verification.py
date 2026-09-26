"""Mark an account's email verified (ADR-0005 §1, Phase 4B).

The free trial is granted once per VERIFIED account: one that proved it controls
its address. Two proofs count:

* redeeming a magic link, which was mailed to the account's own address;
* an OAuth sign-in or connect whose provider reports the SAME address as verified.

The stamp goes on the account that proved it, never on another account found by
email: OAuth deliberately never links by email (``oauth.py``), and a verification
looked up by address would let a provider vouch for somebody else's account.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def mark_email_verified(
    db: AsyncSession, user_id: str, proven_email: str | None, now: datetime
) -> bool:
    """Stamp ``users.email_verified_at`` if ``proven_email`` is this account's address.

    Returns whether the account is now verified. The first stamp is kept; a
    later proof does not move it. The caller commits.
    """
    # ASCII only: Python lower-cases look-alikes such as the Kelvin sign (U+212A)
    # to ASCII letters, so a different mailbox could otherwise match.
    if not proven_email or not proven_email.isascii():
        return False
    user = await db.get(User, user_id)
    if user is None or user.email is None:
        return False
    # Stored addresses are lower-cased and stripped (auth.py, oauth.py).
    if user.email != proven_email.strip().lower():
        return False
    if user.email_verified_at is None:
        user.email_verified_at = now
    return True


__all__ = ["mark_email_verified"]
