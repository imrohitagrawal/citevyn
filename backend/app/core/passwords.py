"""Password hashing (ADR-0004 PR 4). No routes yet — this module has no callers.

Argon2id via ``argon2-cffi`` (not ``passlib``, unmaintained since 2020), pinned
to OWASP's recommended parameters: ``m=19456 KiB, t=2, p=1``. ``p=1`` because
the production machine is Fly's ``shared-cpu-1x`` — one vCPU, so parallelism
above 1 buys nothing and only inflates memory use.

**Tuning the parameters alone does not bound the worst case.** At Fly's
connection ``hard_limit = 40``, even 19 MiB per hash running concurrently
exceeds the 512 MB machine (``docs/ADR/0004-user-accounts.md``). The actual
control is ``_MAX_CONCURRENT_HASHES`` below, a module-level semaphore
acquired BEFORE dispatching to the thread pool — Starlette's 40-thread
default pool is not the bound, and this is not tunable via ``Settings``
because the plan pins the exact value, not a knob an operator should widen.
Mirrors the ``app.cost.admission`` singleton-semaphore idiom.

Hashing is genuinely CPU-bound (unlike an HTTP call), so it runs via
``asyncio.to_thread`` rather than being awaited directly — awaiting it inline
would block the event loop for the whole hash duration, starving every other
in-flight request on this worker.
"""

from __future__ import annotations

import asyncio
import contextlib

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError
from argon2.low_level import Type

# OWASP-recommended Argon2id parameters, see the module docstring for why
# these are fixed rather than settings-driven.
_TIME_COST = 2
_MEMORY_COST_KIB = 19456
_PARALLELISM = 1

_HASHER = PasswordHasher(
    time_cost=_TIME_COST,
    memory_cost=_MEMORY_COST_KIB,
    parallelism=_PARALLELISM,
    type=Type.ID,
)

# Bounds concurrent in-flight hashes to keep peak memory well under the
# 512 MB production machine even at Fly's connection hard_limit=40. See the
# module docstring — this is the actual control, not the hash parameters.
_MAX_CONCURRENT_HASHES = 2
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """Return the process-wide hashing semaphore, building it lazily.

    Lazy, not module-scope-constructed, because ``asyncio.Semaphore()``
    binds to the running event loop at construction in older Python
    versions; building it on first use inside a coroutine avoids a
    "semaphore bound to a different event loop" failure if this module is
    ever imported before an event loop exists (e.g. at process startup).
    """
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT_HASHES)
    return _semaphore


def reset_semaphore() -> None:
    """Drop the process-wide semaphore (test-only), mirroring
    ``app.cost.admission.reset_semaphore``."""
    global _semaphore
    _semaphore = None


# Precomputed hash of a fixed, non-secret placeholder — never a real
# password, never checked against anything meaningful. Verifying against
# this (instead of returning False immediately) when an email is unknown is
# what makes "no such account" and "wrong password" take the same time; a
# short-circuit would be a free account-enumeration oracle over response
# latency. Hardcoded (not computed at import time) so module import stays
# cheap — computing it costs the same ~19 MiB / few-ms hash as a real
# request, which has no business happening at process startup.
_DUMMY_HASH = (
    "$argon2id$v=19$m=19456,t=2,p=1"
    "$MNYnrr9dPNp56KqXLSUgHw$L8EM4JCi503jpU0sGMxKYNLDUE+RT4B0LeCJRvL//wU"
)


async def hash_password(password: str) -> str:
    """Hash ``password`` with Argon2id, bounded by the concurrency semaphore."""
    async with _get_semaphore():
        return await asyncio.to_thread(_HASHER.hash, password)


async def verify_password(password: str, hashed: str) -> bool:
    """Verify ``password`` against a REAL stored hash. Never raises."""
    async with _get_semaphore():
        try:
            return await asyncio.to_thread(_HASHER.verify, hashed, password)
        except (VerifyMismatchError, VerificationError, InvalidHash):
            return False


async def verify_password_or_dummy(password: str, hashed: str | None) -> bool:
    """Verify ``password``, or burn the same cost against a dummy hash.

    Call this — never ``verify_password`` directly — on any login path where
    ``hashed`` may be ``None`` because the account does not exist. Always
    returns ``False`` when ``hashed`` is ``None``; the point is not the
    return value, it is spending the same CPU time as a real verification so
    "unknown email" and "wrong password" are indistinguishable from response
    latency alone.
    """
    if hashed is None:
        async with _get_semaphore():
            await asyncio.to_thread(_safe_verify_dummy, password)
        return False
    return await verify_password(password, hashed)


def _safe_verify_dummy(password: str) -> None:
    """Run a verify against ``_DUMMY_HASH`` and discard the (always-False) result."""
    with contextlib.suppress(VerifyMismatchError, VerificationError, InvalidHash):
        _HASHER.verify(_DUMMY_HASH, password)


def needs_rehash(hashed: str) -> bool:
    """Return whether ``hashed`` was produced with different parameters than
    the current ``_HASHER`` — a stored hash from before a parameter bump.

    Synchronous and cheap: this parses the PHC-format prefix and compares
    parameters, it does not recompute a hash, so it does not need the
    semaphore or a thread.
    """
    return _HASHER.check_needs_rehash(hashed)


__all__ = [
    "hash_password",
    "needs_rehash",
    "reset_semaphore",
    "verify_password",
    "verify_password_or_dummy",
]
