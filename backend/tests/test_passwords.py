"""Tests for :mod:`app.core.passwords` (ADR-0004 PR 4). No routes exist yet.

Covers the plan's verify criteria: round trip; ``needs_rehash`` on lowered
parameters; observed max in-flight hashes <= 2 under N concurrent callers;
dummy-verify on an unknown account.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type

from app.core import passwords


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """Each test gets its own semaphore bound to its own event loop."""
    passwords.reset_semaphore()
    yield
    passwords.reset_semaphore()


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


async def test_hash_then_verify_round_trips() -> None:
    hashed = await passwords.hash_password("correct-horse-battery-staple")
    assert await passwords.verify_password("correct-horse-battery-staple", hashed) is True


async def test_verify_rejects_the_wrong_password() -> None:
    hashed = await passwords.hash_password("correct-horse-battery-staple")
    assert await passwords.verify_password("wrong-password", hashed) is False


async def test_verify_rejects_a_malformed_hash_instead_of_raising() -> None:
    assert await passwords.verify_password("anything", "not-a-real-argon2-hash") is False


def test_hash_uses_the_pinned_owasp_parameters() -> None:
    """Assert the LITERAL parameters, not just "hashing works" — a silent
    parameter downgrade (e.g. Semaphore(2) -> 40, or m=19456 -> a library
    default) is invisible to every functional test above; only an assertion
    on the literal catches it."""
    assert passwords._TIME_COST == 2
    assert passwords._MEMORY_COST_KIB == 19456
    assert passwords._PARALLELISM == 1
    assert passwords._MAX_CONCURRENT_HASHES == 2


# ---------------------------------------------------------------------------
# needs_rehash
# ---------------------------------------------------------------------------


async def test_needs_rehash_is_false_for_a_hash_from_the_current_hasher() -> None:
    hashed = await passwords.hash_password("correct-horse-battery-staple")
    assert passwords.needs_rehash(hashed) is False


def test_needs_rehash_is_true_for_a_hash_with_lowered_parameters() -> None:
    """A hash produced under weaker (pre-bump) parameters must be flagged so
    the caller can re-hash it on the next successful login."""
    weak_hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID)
    weak_hash = weak_hasher.hash("correct-horse-battery-staple")
    assert passwords.needs_rehash(weak_hash) is True


# ---------------------------------------------------------------------------
# Concurrency bound — the actual control, not the hash parameters
# ---------------------------------------------------------------------------


async def test_observed_max_in_flight_hashes_never_exceeds_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N=6 concurrent ``hash_password`` calls; the semaphore must keep the
    OBSERVED number of simultaneously-executing hashes at <= 2. Patches
    ``_HASHER.hash`` to a slow stand-in (not a real ~25ms Argon2 hash) so
    overlap is deterministic under CI scheduling noise, while still
    exercising the real semaphore + ``asyncio.to_thread`` wiring — only the
    CPU-bound hash body is replaced, not the concurrency machinery around
    it.
    """
    in_flight = 0
    max_observed = 0

    class _SlowHasher:
        def hash(self, password: str) -> str:
            nonlocal in_flight, max_observed
            # Runs in a worker thread (asyncio.to_thread). A real data race
            # here would only ever UNDERcount max_observed, never invent an
            # overcount, so it cannot manufacture a false failure of the
            # assertion below -- it can only mask a genuine one.
            in_flight += 1
            max_observed = max(max_observed, in_flight)
            time.sleep(0.05)
            in_flight -= 1
            return "fake-hash"

    # PasswordHasher.hash is read-only on the instance (argon2-cffi defines
    # it via a base-class slot), so the module-level singleton itself is
    # swapped rather than patching one of its attributes.
    monkeypatch.setattr(passwords, "_HASHER", _SlowHasher())

    await asyncio.gather(*(passwords.hash_password(f"password-{i}") for i in range(6)))

    assert max_observed <= 2, f"observed {max_observed} concurrent hashes, cap is 2"
    assert max_observed >= 1, "the stand-in never ran at all -- test is vacuous"


# ---------------------------------------------------------------------------
# Dummy-verify: unknown account takes the same path cost as a real one
# ---------------------------------------------------------------------------


async def test_verify_or_dummy_returns_false_for_an_unknown_account() -> None:
    assert await passwords.verify_password_or_dummy("any-password", None) is False


async def test_verify_or_dummy_still_verifies_a_real_hash() -> None:
    hashed = await passwords.hash_password("correct-horse-battery-staple")
    assert await passwords.verify_password_or_dummy("correct-horse-battery-staple", hashed) is True
    assert await passwords.verify_password_or_dummy("wrong-password", hashed) is False


async def test_dummy_path_spends_comparable_time_to_a_real_verify() -> None:
    """Not a tight timing equality (CI jitter would make that flaky) -- just
    proves the dummy path actually runs a real Argon2 verify rather than
    short-circuiting to an instant ``return False``, which would turn
    "unknown email" into a fast, distinguishable response and defeat the
    whole point of this function.
    """
    hashed = await passwords.hash_password("correct-horse-battery-staple")

    t0 = time.perf_counter()
    await passwords.verify_password(hashed=hashed, password="wrong-password")
    real_verify_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    await passwords.verify_password_or_dummy("any-password", None)
    dummy_elapsed = time.perf_counter() - t0

    # Same order of magnitude: the dummy path must cost at least a third of
    # a real verify. A near-zero dummy_elapsed (a short-circuit regression)
    # fails this; ordinary machine-speed variance does not.
    assert dummy_elapsed >= real_verify_elapsed / 3
