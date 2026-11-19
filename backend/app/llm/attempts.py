"""Why a primary LLM call failed, for a caller that asks (#576).

:class:`~app.llm.fallback.FallbackLLMClient` answers a failed primary call with
its secondary, so nothing above it can see the failure. When a collector is
installed (:func:`collect_primary_failures`, the judged eval does this around
each call), the fallback client notes a :class:`PrimaryFailure` there before it
calls the secondary. With no collector installed, noting does nothing, so
production behaviour is unchanged.

A record is bounded by construction: it has no message, cause or body field, and
:meth:`PrimaryFailure.from_error` is the one place every value is held to a
fixed shape. A provider string that is not a short identifier is recorded as
:data:`UNRECOGNIZED`, never as given.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import re
from collections.abc import Generator, Mapping
from contextvars import ContextVar
from typing import Any

# The failure kinds a raise site may tag. A plain set of strings, not an enum: the
# set may grow. Anything else is recorded as ``unknown``.
KINDS = frozenset(
    {
        "http_status",
        "timeout",
        "transport",
        "non_json",
        "malformed_response",
        "blocked",
        "empty_answer",
        "unknown",
    }
)
UNRECOGNIZED = "UNRECOGNIZED"
# A short identifier: Gemini's finish and block reasons and error codes
# (``MAX_TOKENS``, ``RESOURCE_EXHAUSTED``) and httpx class names (``ReadTimeout``).
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z_]{0,39}")


def _identifier(value: object) -> str | None:
    """``None`` stays ``None``; a short identifier is kept; anything else (another
    type, markup, a newline, too long) is :data:`UNRECOGNIZED`."""
    if value is None:
        return None
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
        return value
    return UNRECOGNIZED


def _count(value: object) -> int | None:
    """A token count: a non-negative int (not a bool), else ``None``."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _seconds(value: object) -> float | None:
    """A duration: a finite, non-negative number (not a bool), else ``None`` (not
    measured), never a made-up 0.0."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0.0:
            return seconds
    return None


def _label(value: object) -> str | None:
    """Our own provider or model label: a string, else ``None``."""
    return value if isinstance(value, str) else None


@dataclasses.dataclass(frozen=True)
class PrimaryFailure:
    """One failed primary call: who failed, how, and how long it took.

    Deliberately without a message, cause or body field."""

    provider: str | None
    model: str | None
    error_type: str
    kind: str
    status_code: int | None
    provider_status: str | None
    reason: str | None
    duration_seconds: float | None
    thoughts_tokens: int | None = None
    answer_tokens: int | None = None
    max_output_tokens: int | None = None

    @classmethod
    def from_error(
        cls, exc: BaseException, *, provider: object, model: object, duration_seconds: object
    ) -> PrimaryFailure:
        """The bounded record of ``exc``. Never raises: every field is read with a
        default and normalised here."""
        kind = getattr(exc, "kind", "unknown")
        status = getattr(exc, "status_code", None)
        return cls(
            provider=_label(provider),
            model=_label(model),
            error_type=type(exc).__name__,
            kind=kind if isinstance(kind, str) and kind in KINDS else "unknown",
            status_code=status
            if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599
            else None,
            provider_status=_identifier(getattr(exc, "provider_status", None)),
            reason=_identifier(getattr(exc, "reason", None)),
            duration_seconds=_seconds(duration_seconds),
            thoughts_tokens=_count(getattr(exc, "thoughts_tokens", None)),
            answer_tokens=_count(getattr(exc, "answer_tokens", None)),
            max_output_tokens=_count(getattr(exc, "max_output_tokens", None)),
        )

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def cause_key(fields: Mapping[str, Any]) -> str:
    """One cause as a short key: provider, kind, then whichever of the status,
    the provider's error code and the reason are present, space-joined
    (``gemini http_status 429 RESOURCE_EXHAUSTED``)."""
    parts = [fields.get("provider") or "unknown", fields.get("kind") or "unknown"]
    parts += [
        fields[key]
        for key in ("status_code", "provider_status", "reason")
        if fields.get(key) is not None
    ]
    return " ".join(str(p) for p in parts)


_failures: ContextVar[list[PrimaryFailure] | None] = ContextVar(
    "citevyn_primary_failures", default=None
)


@contextlib.contextmanager
def collect_primary_failures() -> Generator[list[PrimaryFailure]]:
    """Collect the primary failures noted inside this block, in order.

    The list is only appended to, so a note made in a task started inside the
    block (which runs on a copy of the context) still lands here. The previous
    collector is restored on exit, including on an exception."""
    notes: list[PrimaryFailure] = []
    token = _failures.set(notes)
    try:
        yield notes
    finally:
        _failures.reset(token)


def note_primary_failure(failure: PrimaryFailure) -> None:
    """Record ``failure`` in the installed collector; a no-op when there is none."""
    notes = _failures.get()
    if notes is not None:
        notes.append(failure)
