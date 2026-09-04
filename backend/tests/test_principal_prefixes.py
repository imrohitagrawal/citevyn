"""The registered-vs-anonymous predicate, and its fail-closed property (#288).

WHY THIS FILE EXISTS
--------------------
`is_registered_principal` carries a docstring saying it is deliberately a
POSITIVE test on the registered prefix rather than `not startswith("anon_")`,
so that an unrecognised future principal shape reads as NOT registered.

A security review checked that claim by replacing the body with exactly the
rejected form and running the whole suite: **1762 passed**. The stated design
property was documentation, not a guard -- the "coverage is not assertion"
shape, on a predicate that decides the higher rate tier.

Not exploitable today, because only `usr_`, `anon_` and the never-sessioned
`demo_user` / `admin` shapes exist. That is precisely why it needs a test now
rather than after a third prefix arrives.
"""

from __future__ import annotations

import pytest

from app.core.auth_sessions import (
    ANONYMOUS_USER_PREFIX,
    REGISTERED_USER_PREFIX,
    is_registered_principal,
)


def test_a_registered_principal_is_registered() -> None:
    assert is_registered_principal(f"{REGISTERED_USER_PREFIX}abc123") is True


def test_an_anonymous_principal_is_not_registered() -> None:
    assert is_registered_principal(f"{ANONYMOUS_USER_PREFIX}abc123") is False


@pytest.mark.parametrize(
    "user_id",
    [
        "demo_user",  # the seeded demo principal -- real, and carries neither prefix
        "admin",  # likewise
        "svc_future",  # a hypothetical third shape
        "",  # degenerate
        "usr",  # the prefix without its underscore
        "x_usr_abc",  # the prefix present but not at the start
        "USR_abc",  # right letters, wrong case
    ],
)
def test_an_unrecognised_principal_shape_fails_CLOSED(user_id: str) -> None:
    """This is the one the docstring promises and nothing asserted.

    RED if the body becomes `not user_id.startswith(ANONYMOUS_USER_PREFIX)` --
    the exact rejected form -- because every id here would then read as
    REGISTERED and silently earn the higher rate tier
    (`rate_limit.py::_apply_per_visitor_rate_limit`).
    """
    assert is_registered_principal(user_id) is False


def test_the_two_prefixes_are_distinct_and_non_empty() -> None:
    """Partner: an empty prefix would make `startswith` true for everything."""
    assert REGISTERED_USER_PREFIX
    assert ANONYMOUS_USER_PREFIX
    assert REGISTERED_USER_PREFIX != ANONYMOUS_USER_PREFIX
    assert not REGISTERED_USER_PREFIX.startswith(ANONYMOUS_USER_PREFIX)
