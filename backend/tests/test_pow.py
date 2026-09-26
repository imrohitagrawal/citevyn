"""The proof-of-work challenge used as the sign-up bot check (ADR-0005 Phase 5).

Pure mechanism (``app.core.pow``, no app imports). The server signs a challenge;
the browser searches for the number that produces it; the server checks the
signature, the work and the expiry. Each test says which change turns it red.
"""

from __future__ import annotations

import hashlib

import pytest

from app.core.pow import PowError, make_challenge, solve, verify_solution

SECRET = "s" * 32
NOW = 1_800_000_000


def _solved(max_number: int = 50, now: int = NOW, ttl_s: int = 300) -> dict[str, object]:
    challenge = make_challenge(SECRET, max_number=max_number, ttl_s=ttl_s, now=now)
    return {**challenge, "number": solve(challenge)}


def test_a_correct_solution_verifies_and_returns_the_challenge_id() -> None:
    """Turns red if: a genuine solution is rejected."""
    payload = _solved()
    assert verify_solution(SECRET, payload, now=NOW + 10) == payload["challenge"]


def test_the_challenge_is_the_hash_of_salt_and_a_secret_number() -> None:
    """The work is real: the number is not in the challenge. Turns red if: the
    number is exposed or the hash is computed differently from the client."""
    c = make_challenge(SECRET, max_number=50, ttl_s=300, now=NOW)
    assert "number" not in c
    n = solve(c)
    assert hashlib.sha256(f"{c['salt']}{n}".encode()).hexdigest() == c["challenge"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("number", -1),  # the wrong answer
        ("signature", "0" * 64),  # a challenge we never issued
        ("salt", "forged?expires=9999999999"),  # extend the expiry by editing the salt
        ("challenge", "0" * 64),  # swap in another challenge
    ],
)
def test_a_tampered_solution_is_rejected(field: str, value: object) -> None:
    """Turns red if: any of the signature, the work check or the salt binding is
    dropped."""
    payload = {**_solved(), field: value}
    with pytest.raises(PowError):
        verify_solution(SECRET, payload, now=NOW + 10)


def test_an_expired_challenge_is_rejected() -> None:
    """Turns red if: the expiry in the signed salt is not enforced."""
    payload = _solved(ttl_s=300)
    verify_solution(SECRET, payload, now=NOW + 299)  # partner: still valid just before
    with pytest.raises(PowError):
        verify_solution(SECRET, payload, now=NOW + 301)


def test_a_challenge_signed_with_another_secret_is_rejected() -> None:
    """Turns red if: the HMAC key is not the server's secret."""
    payload = _solved()
    with pytest.raises(PowError):
        verify_solution("t" * 32, payload, now=NOW + 10)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"salt": 1, "number": 1, "challenge": "x", "signature": "y"},
        {"salt": "a", "number": "1", "challenge": "x", "signature": "y"},
        {"salt": "a", "number": True, "challenge": "x", "signature": "y"},
        {"salt": "no-expiry", "number": 1, "challenge": "x", "signature": "y"},
    ],
)
def test_a_malformed_payload_is_rejected_not_a_crash(payload: dict[str, object]) -> None:
    """Untrusted input. Turns red if: a bad shape raises anything but PowError."""
    with pytest.raises(PowError):
        verify_solution(SECRET, payload, now=NOW)


def test_the_number_is_within_the_advertised_range() -> None:
    """The client searches 0..maxnumber; a number outside it would make the
    work unbounded. Turns red if: the range is not honoured."""
    for _ in range(20):
        c = make_challenge(SECRET, max_number=5, ttl_s=300, now=NOW)
        assert c["maxnumber"] == 5
        assert 0 <= solve(c) <= 5


def test_an_empty_secret_is_refused() -> None:
    """A blank key would make every signature forgeable. Turns red if: an empty
    secret is accepted."""
    with pytest.raises(ValueError, match="secret"):
        make_challenge("", max_number=5, ttl_s=300, now=NOW)


def test_solve_reports_a_challenge_with_no_answer_in_range() -> None:
    """Turns red if: solve loops past maxnumber or returns a wrong number."""
    c = make_challenge(SECRET, max_number=5, ttl_s=300, now=NOW)
    with pytest.raises(PowError):
        solve({**c, "challenge": "0" * 64})
