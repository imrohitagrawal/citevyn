"""Which part of the system spent the money (#153 Layer 1).

``LLMResult`` says *what* was called and how many tokens it cost, but not *why*.
Without that, a spend total answers "we spent $4 today" and not "we spent $4 today
and 70% of it was the alias-intent check", which is the form an operator can act on.

The site is carried in a :class:`~contextvars.ContextVar` rather than threaded
through every signature. Two reasons:

* The metering seam is a *decorator* around the shared client, so it sits below the
  call sites and cannot see their arguments. A parameter would have to be added to
  ``LLMClient.complete`` — changing the provider protocol for a bookkeeping concern.
* A new call site that forgets to set the site still gets metered, just labelled
  ``unknown``. The failure mode is a vague row, not a missing one. A required
  parameter would instead be a compile-time nag that someone satisfies with a
  copy-pasted wrong value.

``contextvars`` is asyncio-native: each task gets its own copy, so concurrent
requests cannot read each other's label the way a module global would allow.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum


class CallSite(StrEnum):
    """The paid operations this system performs, as spend-attribution labels."""

    answer = "answer"
    condense = "condense"
    alias_intent = "alias_intent"
    ingest = "ingest"
    eval = "eval"
    # The default. A row labelled ``unknown`` is not an error, but a persistent
    # stream of them means a call site was added without a label — worth finding,
    # since unattributed spend is exactly what makes a bill hard to act on.
    unknown = "unknown"


_current_call_site: ContextVar[CallSite] = ContextVar("citevyn_call_site", default=CallSite.unknown)


def get_call_site() -> CallSite:
    """Return the call site in force for the current task."""
    return _current_call_site.get()


@contextlib.contextmanager
def call_site(site: CallSite) -> Generator[None]:
    """Label every paid call made inside this block.

    Restores the previous value on exit — including on an exception — so a failed
    call cannot leak its label onto whatever runs next in the same task. Nesting is
    supported: an inner block wins for its duration, then the outer label resumes.
    """
    token = _current_call_site.set(site)
    try:
        yield
    finally:
        _current_call_site.reset(token)


class PaidBy(StrEnum):
    """Whose key paid for a call (ADR-0005 §2, Quota). Stored as a plain string."""

    platform = "platform"
    # BYOK (Phase 8): the user's own key. Never counts against the plan allowance.
    user = "user"


@dataclass(frozen=True)
class Billing:
    """The principal a paid call was made for, and who paid. ``user_id`` is None
    outside a user request (ingest, eval)."""

    user_id: str | None = None
    paid_by: PaidBy = PaidBy.platform


# Frozen, so sharing one default instance across tasks is safe.
_NOBODY = Billing()
_current_billing: ContextVar[Billing] = ContextVar("citevyn_billing", default=_NOBODY)


def get_billing() -> Billing:
    """Return who the current task's paid calls are for."""
    return _current_billing.get()


@contextlib.contextmanager
def billed_to(user_id: str | None, paid_by: PaidBy = PaidBy.platform) -> Generator[None]:
    """Attribute every paid call made inside this block to ``user_id``.

    A ContextVar for the same reasons as :func:`call_site`: the meter sits below
    the call sites, and each asyncio task gets its own copy. Restored on exit,
    including on an exception.
    """
    token = _current_billing.set(Billing(user_id=user_id, paid_by=paid_by))
    try:
        yield
    finally:
        _current_billing.reset(token)


__all__ = [
    "Billing",
    "CallSite",
    "PaidBy",
    "billed_to",
    "call_site",
    "get_billing",
    "get_call_site",
]
