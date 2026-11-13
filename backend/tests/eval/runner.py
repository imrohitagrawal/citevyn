"""Eval runner + CLI (Phase 0, issue #96).

Composes the two metrics into one report and a pass/fail exit code:

* **retrieval hit-rate** (:mod:`.retrieval`) — always run, fully hermetic.
* **answer quality** (:mod:`.judge`) — run only when a real LLM provider is
  configured, and refused before any paid call unless a separate grader is set
  (``CITEVYN_EVAL_JUDGE_MODEL``, its ``CITEVYN_EVAL_JUDGE_TIMEOUT_SECONDS`` and the
  OpenRouter key); each answerable case is driven end-to-end through
  :class:`~app.answer.orchestrator.Orchestrator` against the seeded corpus and
  the produced answer is judged 1–5 vs its expected gist. Under the stub the
  section is marked ``unavailable`` (never faked).

Usage::

    python -m tests.eval.runner                 # retrieval + judge (provider + grader set)
    python -m tests.eval.runner --no-judge       # retrieval only
    python -m tests.eval.runner --report out.json --quiet

Exit code is non-zero when the retrieval gate regresses, or when the judge ran
and its mean score fell below the floor — so ``make eval`` gates locally too.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import os
import pathlib
import re
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.answer.generate import _truncate_snippet
from app.cache.answer_cache import CachedAnswer
from app.cache.factory import build_answer_cache_store
from app.core.config import Settings, get_settings
from app.cost.call_site import CallSite, get_call_site
from app.llm.errors import LLMUnavailable
from app.llm.factory import build_metered_llm_client, get_llm_client
from app.llm.protocol import LLMClient
from app.llm.stub import StubLLMClient
from app.llm.types import LLMResult
from app.models import (
    AuditAction,
    AuditEvent,
    Chunk,
    Document,
    Message,
    MessageRole,
    RetrievedEvidence,
)

# Bare session ids per case: the harness wants the rows made for it (#511).
from ..orchestrator_double import Orchestrator
from . import judge as judge_module
from .cases import EvalCase, filter_cases, load_cases
from .groundedness import fact_coverage, forbidden_present, missing_facts
from .judge import JudgeParseError, parse_verdict, score_answer_panel_async
from .parity import _CREDENTIAL
from .paths import DEFAULT_REPORT_PATH, GOLDEN_PATH
from .probe import Probe, probe_retrieval
from .retrieval import evaluate_retrieval, postgres_session, seeded_session
from .subset import select_judge_subset
from .thresholds import (
    MAX_REFUSAL_LEAKS,
    MIN_FOLLOWUP_HIT_RATE,
    MIN_JUDGE_COVERAGE,
    MIN_LITERAL_HIT_RATE,
    MIN_MEAN_JUDGE,
    MIN_MRR,
    MIN_MULTIHOP_HIT_RATE,
    MIN_OVERALL_HIT_RATE,
    MIN_PRECISION_AT_1,
)


@dataclasses.dataclass
class JudgedCase:
    """One case driven end-to-end and (optionally) judged.

    ``score`` is the ROBUST panel score — ``min(standard_median, adversarial)`` — so
    a skeptic that catches a plausible-but-wrong answer vetoes an over-scored median.
    ``fact_coverage`` is the judge-independent deterministic groundedness signal
    (``None`` when the case declares no ``expected_facts``).
    """

    case_id: str
    kind: str
    answer: str
    # ``None`` when no response was seen (the case crashed first): it was never
    # observed to decline or to answer (#598 Step 2b).
    no_answer: bool | None
    score: int | None = None
    rationale: str = ""
    error: str | None = None
    standard_scores: tuple[int, ...] = ()
    standard_median: int | None = None
    adversarial_score: int | None = None
    spread: int | None = None
    contested: bool = False
    fact_coverage: float | None = None
    missing_facts: tuple[str, ...] = ()
    # Prompt-injection resistance (Item 2): forbidden substrings the answer emitted (a
    # non-empty tuple = the model complied with an injection → a leak). ``None`` when
    # the case declares no ``must_not_contain``.
    injection_hits: tuple[str, ...] | None = None
    # Multi-turn echo oracle (#169). ``True`` when this ``followup`` case's answer came
    # back BYTE-IDENTICAL to the answer of the turn before it — the signature of a
    # follow-up that was never actually answered. ``None`` for a single-turn case (no
    # prior turn to compare against). See ``_judge_cases`` for why this exists.
    echoed_prior: bool | None = None
    # Why the case came out as it did (#598 Step 2). All describe the FINAL turn.
    question: str = ""
    # The answer could not be graded as the expected model's: an answer call failed,
    # or another model wrote an answer (an answer call, or the cache entry served),
    # on any turn (owner rule, 2026-11-12). It fails the refusal, groundedness or
    # multi-turn gate that applies to it (D1), through that gate's own "errored"
    # line, instead of dropping out of it. Nothing else sets it (#598 Step 2b).
    answer_errored: bool = False
    # Everything else that went wrong for this case: ``None``, or a list of
    # ``{"stage", "type", "message"}`` with ``stage`` ``answer``, ``report`` or
    # ``grade`` (#598 Step 2b). The case is still graded when its answer exists
    # and its writer is known; every entry fails the separate harness-integrity
    # check, never an answer gate.
    harness_error: list[dict[str, Any]] | None = None
    # On a final-turn cache hit: the case and turn that wrote the cached answer and
    # the passages that turn answered from (#598 Step 2b). The hit's own
    # retrieval fields stay empty: no retrieval ran for it.
    cache_origin: dict[str, Any] | None = None
    # The run's own audit row: ``outcome`` (answer, no_answer, unsupported,
    # citation_validation_failed, cache_hit) and its ``reason`` for a decline.
    outcome: str | None = None
    decline_reason: str | None = None
    # The answer model's own text (before the product rewrites or declines it),
    # and the #208 retry's text, which is served only when ``retry_kept``.
    raw_reply: str | None = None
    retry_reply: str | None = None
    retry_kept: bool = False
    # The query of the final turn's first retrieval (a follow-up's carries its
    # antecedent), whether it was the unscoped path, and the global confidence
    # gate's numbers (they fill only on Postgres; on SQLite the vector arm returns
    # before the gate).
    resolved_query: str | None = None
    retrieval_global: bool | None = None
    gate_top_score: float | None = None
    gate_margin: float | None = None
    gate_passed: bool | None = None
    # The passages the answer was made from, in rank order, as the model saw them.
    evidence: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # One entry per grader call: score, rationale, duration_seconds, and the
    # provider and model that served it (kept for a malformed verdict too).
    grader_calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # One entry per answer-client call of every turn: turn (history or final),
    # call site, provider, model, whether it was the expected model, and whether
    # it ``failed`` (with its ``error_type``). A failed answer call errors the
    # case; a helper call (not ``answer``) that failed or was made by another
    # model is listed, never gated.
    model_calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@contextlib.asynccontextmanager
async def _judge_session(settings: Settings, *, postgres: bool):
    """Yield the DB session the judge drives the orchestrator against.

    Hermetic SQLite by default; the real Postgres+pgvector catalog (with a real
    embedder, rolled back for zero CATALOG residue) when ``postgres=True`` so the
    judged answers reflect the live semantic retrieval path.

    The rollback covers seeded catalog/session rows only. ``provider_calls`` rows
    written by the cost meter (#153 Layer 1) are committed on a separate session and
    intentionally OUTLIVE the run — spend is a fact about money that already left the
    account, so our own teardown must not erase it. See :func:`postgres_session` and
    ``docs/COST_CONTROLS.md`` §1.
    """
    if postgres:
        async with postgres_session(settings) as (session, _embedder):
            yield session
    else:
        async with seeded_session() as session:
            yield session


class AttributingLLM:
    """An answer client that records which provider and model produced each call.

    The judged run's main client is production's Gemini client with its OpenRouter
    fallback, so a Gemini failure is answered, silently, by the fallback model.
    Without this record such an answer would be graded and reported as Gemini's
    (#596). ``_judge_cases`` reads ``records`` per case.

    A call that raises is recorded too, as failed, and the error re-raised
    (#598 Step 2b): the orchestrator swallows some failures (the #208 retry,
    condense, the alias check), and without this record they would vanish.
    """

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        # One record per call, in call order: a call that returned and one that
        # raised alike, so no pairing between two lists can drift.
        self.records: list[_CallRecord] = []

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        site = get_call_site()
        try:
            result = await self._inner.complete(
                system=system, user=user, max_tokens=max_tokens, temperature=temperature
            )
            record = _CallRecord(site, result.provider, result.model, text=result.text)
        except BaseException as exc:
            # Who served a failed call is unknown; the client may name its own.
            self.records.append(
                _CallRecord(
                    site,
                    getattr(self._inner, "provider", None),
                    getattr(self._inner, "model", None),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    cause=exc,
                )
            )
            raise
        self.records.append(record)
        return result

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclasses.dataclass(frozen=True)
class _CallRecord:
    """One answer-client call: its site, who served it, and its text or failure."""

    site: CallSite
    provider: str | None
    model: str | None
    text: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    # The failed call's own exception, so the runner can tell it escaping ``ask``
    # from an unrelated failure later in the turn. Not part of the report.
    cause: BaseException | None = dataclasses.field(default=None, compare=False, repr=False)

    @property
    def failed(self) -> bool:
        return self.error_type is not None


class _RecordingGrader:
    """A grader client that keeps each call's score, rationale, duration and the
    provider and model that served it (#598), so a stalled, disagreeing or
    self-graded call can be found in the report."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        start = time.perf_counter()
        entry: dict[str, Any] = {
            "score": None,
            "rationale": "",
            "duration_seconds": 0.0,
            "provider": None,
            "model": None,
        }
        self.calls.append(entry)
        try:
            result = await self._inner.complete(
                system=system, user=user, max_tokens=max_tokens, temperature=temperature
            )
        except Exception as exc:
            entry["rationale"] = f"call failed: {type(exc).__name__}: {exc}"
            raise
        finally:
            entry["duration_seconds"] = time.perf_counter() - start
        # Stored before the verdict is read, so a malformed verdict keeps them.
        entry["provider"], entry["model"] = result.provider, result.model
        try:
            verdict = parse_verdict(result.text)
        except JudgeParseError as exc:
            entry["rationale"] = f"judge_parse: {exc}"
        except Exception as exc:
            # Recorded only; the panel parses again and its error is the case's
            # ``grade`` harness error (e.g. a score too large for a float).
            entry["rationale"] = f"parse failed: {type(exc).__name__}: {exc}"
        else:
            entry["score"], entry["rationale"] = verdict.score, verdict.rationale
        return result

    async def aclose(self) -> None:
        await self._inner.aclose()


def _reply_fields(replies: list[tuple[CallSite, str]]) -> dict[str, Any]:
    """The answer model's own first reply and the #208 retry's, from the calls that
    returned (a failed retry is not a retry reply)."""
    answers = [text for site, text in replies if site is CallSite.answer]
    return {
        "raw_reply": answers[0] if answers else None,
        "retry_reply": answers[1] if len(answers) > 1 else None,
    }


def _probe_fields(probe: Probe, retrievals_from: int, gates_from: int) -> dict[str, Any]:
    """The final turn's first retrieval and global confidence gate, from the probe."""
    fields: dict[str, Any] = {}
    first = next((r for r in probe.retrievals[retrievals_from:] if r.tag == "first"), None)
    fields["resolved_query"] = first.query if first else None
    fields["retrieval_global"] = first.is_global if first else None
    gate = next(iter(probe.gates[gates_from:]), None)
    if gate is not None:
        fields["gate_top_score"] = gate.top_score
        fields["gate_margin"] = gate.margin
        fields["gate_passed"] = gate.passed
    return fields


async def _final_turn(
    session: AsyncSession,
    response: Any,
    session_id: uuid.UUID,
    replies: list[tuple[CallSite, str]],
    probe: Probe,
    retrievals_from: int,
    gates_from: int,
    *,
    into: dict[str, Any] | None = None,
    errors: list[BaseException] | None = None,
) -> dict[str, Any]:
    """The report fields of one case's FINAL turn (#598 Step 2).

    ``replies`` and the probe's records from the given indexes are the final
    turn's only, so a follow-up's history turn cannot leak in. The decline reason
    is read from the turn's own audit row; the evidence from the rows the
    orchestrator persisted for the turn's USER message, which hold what the
    answer was made from (the #208 retry's passages when it was kept).

    Fields are written into ``into`` one by one as they are read, so a later
    failure keeps every field already read. With ``errors``, each database read
    runs in its own savepoint and a failed read is appended there instead of
    raised, so a failed audit read still leaves the evidence read, and the other
    way round (#598 Step 2b).
    """
    fields = into if into is not None else {}
    fields.update(_reply_fields(replies))
    fields.update(_probe_fields(probe, retrievals_from, gates_from))

    async def _audit() -> None:
        message_id = str(response.get("message_id"))
        meta = (
            await session.execute(
                select(AuditEvent.metadata_).where(
                    AuditEvent.action == AuditAction.ask_question,
                    AuditEvent.resource_id == message_id,
                )
            )
        ).scalar_one_or_none()
        if meta is None:
            raise LookupError(f"no ask_question audit row for message {message_id}")
        fields["outcome"] = meta.get("outcome")
        fields["decline_reason"] = meta.get("reason")
        # The retry runs only after an ungrounded first reply, so a turn that still
        # answered was answered by the retry.
        fields["retry_kept"] = (
            fields.get("retry_reply") is not None and fields["outcome"] == "answer"
        )

    async def _evidence() -> None:
        fields["evidence"] = await _turn_evidence(session, session_id)

    for read in (_audit, _evidence):
        if errors is None:
            await read()
            continue
        try:
            async with session.begin_nested():
                await read()
        except Exception as exc:
            errors.append(exc)
    return fields


async def _turn_evidence(session: AsyncSession, session_id: uuid.UUID) -> list[dict[str, Any]]:
    """The passages the session's latest turn answered from, in rank order, as
    the model saw them: the rows persisted for that turn's USER message."""
    user_message = (
        await session.execute(
            select(Message.message_id)
            .where(Message.session_id == session_id, Message.role == MessageRole.user)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    rows = (
        await session.execute(
            select(RetrievedEvidence, Chunk, Document)
            .join(Chunk, RetrievedEvidence.chunk_id == Chunk.chunk_id)
            .join(Document, Chunk.document_id == Document.document_id)
            .where(RetrievedEvidence.message_id == user_message)
            .order_by(RetrievedEvidence.rank)
        )
    ).all()
    evidence: list[dict[str, Any]] = []
    for row, chunk, document in rows:
        # The same cut the answer prompt makes, so the text is what the model saw.
        text = _truncate_snippet(chunk.chunk_text)
        evidence.append(
            {
                "source": document.source_name,
                "heading": chunk.heading,
                "rank": row.rank,
                "score": row.score,
                "type": row.retrieval_type.value,
                "text": text,
                "truncated": text != chunk.chunk_text,
            }
        )
    return evidence


def _wrong_answerers(
    calls: list[tuple[str, str]], expect: tuple[str, str]
) -> list[tuple[str, str]]:
    """Calls not made by the expected provider and model. OpenRouter reports the
    model it routed to, which may be the id with a date suffix
    (``openai/gpt-4o-mini-2024-07-18``); anything longer is another model
    (``openai/gpt-4o-mini-search-preview``)."""
    provider, model = expect
    same = re.compile(rf"{re.escape(model)}(-\d{{4}}-\d{{2}}-\d{{2}})?")
    return [c for c in calls if c[0] != provider or not same.fullmatch(c[1])]


_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_VERSION_SUFFIX = re.compile(r"^(?P<base>.+)-(?P<version>\d{3})$")


def _model_key(model: str) -> tuple[str, str | None]:
    """A model id without what does not change the model: the ``vendor/`` prefix,
    an OpenRouter ``:<tag>`` (``:free``) and a trailing ``-YYYY-MM-DD`` date. A
    trailing ``-NNN`` version is split off and returned on its own."""
    name = model.split("/", 1)[1] if "/" in model else model
    name = name.split(":", 1)[0]
    name = _DATE_SUFFIX.sub("", name)
    versioned = _VERSION_SUFFIX.fullmatch(name)
    if versioned is None:
        return name, None
    return versioned["base"], versioned["version"]


def _same_model(served: str, model: str) -> bool:
    """Whether two ids name one model, whichever side carries the decorations
    (#598 Step 2b): prefixes, tags and dates are stripped from BOTH sides and the
    rest must be equal. A trailing version is ignored only when one side has none
    (the base id and a pinned one); two different versions are two models. Used by
    the start-up check and the per-call self-grading check alike."""
    (served_name, served_version), (name, version) = _model_key(served), _model_key(model)
    if served_name != name:
        return False
    return served_version is None or version is None or served_version == version


# A harness error's message is cut here: a database error can carry a whole statement.
_MESSAGE_LIMIT = 2000


def _harness_entry(
    stage: str, exc: BaseException, secrets: list[str], *, where: str | None = None
) -> dict[str, Any]:
    """One harness error: its stage, the exception's type, and its redacted message."""
    message = f"{where}: {exc}" if where else str(exc)
    return {
        "stage": stage,
        "type": type(exc).__name__,
        "message": redact(message, secrets)[:_MESSAGE_LIMIT],
    }


def _harness_entries(value: Any) -> list[dict[str, Any]]:
    """A case's harness errors as a list: ``None``, one entry, or a list of them."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]  # type: ignore[arg-type]


def _add_error(entry: JudgedCase, text: str) -> None:
    """Append to the case's ``error``; an error already recorded is never replaced."""
    entry.error = text if entry.error is None else f"{entry.error}; {text}"


def _explained_by(exc: BaseException, causes: list[BaseException]) -> bool:
    """Whether ``exc`` is one of ``causes`` or was raised from one (its cause or
    context chain): a failed answer call's own error escaping ``ask``."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if any(current is cause for cause in causes):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _helper_fallbacks(case: JudgedCase) -> list[dict[str, Any]]:
    """The case's non-answer calls made by another model."""
    return [
        c
        for c in case.model_calls
        if not c["expected"] and not c.get("failed") and c["site"] != CallSite.answer.value
    ]


def _helper_failures(case: JudgedCase) -> list[dict[str, Any]]:
    """The case's non-answer calls that failed (the orchestrator swallowed them)."""
    return [c for c in case.model_calls if c.get("failed") and c["site"] != CallSite.answer.value]


def _describe(call: dict[str, Any]) -> str:
    if call.get("failed"):
        return f"{call['site']} failed with {call['error_type']} ({call['turn']} turn)"
    return f"{call['site']} by {call['provider']} {call['model']} ({call['turn']} turn)"


class _CacheLedger:
    """The pass's answer cache, recording who wrote each key (#598 Step 2b).

    One per pass, handed to every orchestrator with ``cache=``. Each pass starts
    with an empty cache (a fresh catalog or index version), so every hit was
    written by an earlier turn of THIS pass. ``writers`` maps a key to the turn
    that last wrote it (last write wins): whether the expected model wrote the
    answer, its case and turn, and the passages it answered from. A hit is
    attributed by its own key, never by its text.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.hits: list[str] = []
        self.puts: list[str] = []
        self.writers: dict[str, dict[str, Any]] = {}

    async def get(self, *, cache_key: str) -> CachedAnswer | None:
        value = await self._inner.get(cache_key=cache_key)
        if value is not None:
            self.hits.append(cache_key)
        return value

    async def put(self, *, cache_key: str, value: CachedAnswer) -> None:
        await self._inner.put(cache_key=cache_key, value=value)
        self.puts.append(cache_key)


@dataclasses.dataclass
class _CaseState:
    """Bookkeeping for one case that is not part of its report. Everything the
    report holds is written straight onto the case's one ``JudgedCase``."""

    stage: str = "answer"
    harness: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # The errors of the case's failed answer calls: one of them escaping ``ask``
    # is that call's failure, not another harness error.
    failed_causes: list[BaseException] = dataclasses.field(default_factory=list)
    # A cache hit whose writer is unknown: the answer cannot be graded as anyone's.
    unattributed: bool = False
    final_puts: list[str] = dataclasses.field(default_factory=list)
    final_replies: list[tuple[CallSite, str]] = dataclasses.field(default_factory=list)


_T = TypeVar("_T")


async def _judge_cases(
    cases: list[EvalCase],
    *,
    settings: Settings,
    postgres: bool = False,
    answer_llm: LLMClient | None = None,
    judge_llm: LLMClient | None = None,
    expect_answerer: tuple[str, str] | None = None,
    answer_models: tuple[str, ...] = (),
    on_case: Callable[[list[JudgedCase]], bool] | None = None,
    run_errors: list[dict[str, Any]] | None = None,
) -> list[JudgedCase]:
    """Drive each case through the orchestrator and judge the produced answer.

    Returns one :class:`JudgedCase` per case driven. Each case runs inside its own
    savepoint (``session.begin_nested()``) and its own ``except Exception``, so a
    failure records what happened on that case, rolls back only that case's rows,
    and the pass goes on. A case's record only grows: each fact is written onto
    its one ``JudgedCase`` the moment it is known, and a later failure is appended
    (``harness_error``, ``error``), never allowed to reset a field already set.
    Returns an empty list when no real provider is configured.

    ``answer_llm`` answers instead of the process-wide client (the fallback pass,
    #596); ``judge_llm`` grades instead of it (the separate grader). ``None`` keeps
    the process-wide client for that role. ``expect_answerer`` (provider, model):
    when ``answer_llm`` is an :class:`AttributingLLM`, a case in which an answer
    call failed, or another model wrote an answer, on any turn, is recorded as an
    answer error, not graded (#596; owner rule 2026-11-12).

    ``answer_models``: every answer model of the run; a grader call served by one
    of them (or by ``expect_answerer``'s model) is a ``grade`` harness error and
    its score is never used. ``on_case`` is called with the cases finished so far
    after each one, so the caller can save them before anything is torn down;
    when it returns ``False`` the pass stops before any further paid call. A
    failure closing the pass's session or probe is appended to ``run_errors``
    (raised when it is ``None``), so it cannot drop the pass's results.
    """
    if isinstance(get_llm_client(settings), StubLLMClient):
        return []
    # Exclude postgres-only cases from a hermetic judged run (Item 2) — they need the
    # live vector arm; on SQLite they would misfire and pollute the judged metrics.
    cases = [c for c in cases if postgres or not c.postgres_only]
    judged: list[JudgedCase] = []
    attributing = expect_answerer is not None and isinstance(answer_llm, AttributingLLM)
    secrets = secret_values(settings)
    # The models whose grading of this run's answers would be self-grading.
    own_models = {*answer_models, *([expect_answerer[1]] if expect_answerer else [])}

    # The grader, recorded per call, resolved exactly as the panel resolves it. A
    # stub is never wrapped: the panel's own stub check must still see it.
    grader = judge_llm or judge_module.get_llm_client(settings)
    recording_grader = None if isinstance(grader, StubLLMClient) else _RecordingGrader(grader)

    stack = contextlib.AsyncExitStack()
    try:
        # The probe is open only while this pass runs (#598).
        probe = stack.enter_context(probe_retrieval())
        session = await stack.enter_async_context(_judge_session(settings, postgres=postgres))
        ledger = _CacheLedger(build_answer_cache_store(settings, session))

        async def _guarded(
            state: _CaseState, stage: str, read: Callable[[], Awaitable[_T]]
        ) -> _T | None:
            """Run one report read in its own savepoint. A failure is a harness
            error on the case, and the case's transaction stays usable: on
            Postgres a failed statement would otherwise abort every later one."""
            try:
                async with session.begin_nested():
                    return await read()
            except Exception as exc:
                state.harness.append(_harness_entry(stage, exc, secrets))
                return None

        def _mark_answer_error(entry: JudgedCase, text: str) -> None:
            entry.answer_errored = True
            _add_error(entry, f"answer: {text}")

        async def _ask_attributed(
            case: EvalCase,
            entry: JudgedCase,
            state: _CaseState,
            question: str,
            request_id: str,
            session_id: uuid.UUID,
            turn: str,
        ) -> Any:
            """Ask one turn and record, in a ``finally`` so a turn that raises keeps
            them: which model made each call (by call site), the replies, every
            answer call that failed, and every answer another model wrote. A case's
            answer error is decided here, from the calls, whatever happens after."""
            start = len(answer_llm.records) if isinstance(answer_llm, AttributingLLM) else 0
            hits_from, puts_from = len(ledger.hits), len(ledger.puts)
            calls: list[dict[str, Any]] = []
            try:
                response = await Orchestrator(settings, session, llm=answer_llm, cache=ledger).ask(
                    question=question, request_id=request_id, session_id=session_id
                )
            finally:
                records = (
                    answer_llm.records[start:] if isinstance(answer_llm, AttributingLLM) else []
                )
                if turn == "final":
                    # Only calls that returned: a failed retry is not a retry reply.
                    state.final_replies = [(r.site, r.text or "") for r in records if not r.failed]
                    for key, value in _reply_fields(state.final_replies).items():
                        setattr(entry, key, value)
                if attributing:
                    assert expect_answerer is not None
                    for record in records:
                        served = (record.provider or "", record.model or "")
                        calls.append(
                            {
                                "turn": turn,
                                "site": record.site.value,
                                "provider": record.provider,
                                "model": record.model,
                                "expected": not record.failed
                                and not _wrong_answerers([served], expect_answerer),
                                "failed": record.failed,
                                "error_type": record.error_type,
                            }
                        )
                    entry.model_calls.extend(calls)
                    failed = [r for r in records if r.failed and r.site is CallSite.answer]
                    state.failed_causes.extend(r.cause for r in failed if r.cause is not None)
                    if failed:
                        _mark_answer_error(
                            entry,
                            "answer call failed ("
                            + "; ".join(
                                f"{turn}, {r.site.value}: {r.error_type}: "
                                f"{redact(r.error_message or '', secrets)[:_MESSAGE_LIMIT]}"
                                for r in failed
                            )
                            + ")",
                        )
                    # Owner decision (2026-11-12): only an ANSWER call by another model,
                    # on any turn, makes the case errored. A history turn's answer shapes
                    # the follow-up's context, so it counts too. Helper calls (condense,
                    # alias check) by another model, or that failed, are reported in
                    # ``model_calls``, not gated.
                    wrong = [
                        c
                        for c in calls
                        if not c["expected"]
                        and not c["failed"]
                        and c["site"] == CallSite.answer.value
                    ]
                    if wrong:
                        _mark_answer_error(
                            entry,
                            f"answered by [{'; '.join(_describe(c) for c in wrong)}], "
                            f"not {expect_answerer}; not graded as that model's answer",
                        )
            if turn == "final":
                # Recorded at once, before any cache bookkeeping or report read.
                entry.answer = str(response.get("answer", ""))
                entry.no_answer = bool(response.get("no_answer") or response.get("unsupported"))
            if not attributing:
                return response
            wrong_writer = any(
                not c["expected"] and not c["failed"] and c["site"] == CallSite.answer.value
                for c in calls
            )
            if response.get("cache_hit"):
                # No model call was made for this turn; the answer is whatever an earlier
                # turn wrote under this key. Graded only if that turn was the expected
                # model's; a key no turn of this pass wrote is a harness error.
                hit = ledger.hits[hits_from:]
                writer = ledger.writers.get(hit[-1]) if hit else None
                if writer is None:
                    state.unattributed = True
                    state.harness.append(
                        {
                            "stage": "answer",
                            "type": "UnknownCacheWriter",
                            "message": f"the {turn} turn was served a cache entry no turn "
                            "of this pass wrote, so who wrote it is unknown; not graded",
                        }
                    )
                else:
                    if turn == "final":
                        entry.cache_origin = {
                            "case_id": writer["case_id"],
                            "turn": writer["turn"],
                            "evidence": writer["evidence"],
                        }
                    if not writer["expected"]:
                        fault = {
                            "turn": turn,
                            "site": "cache",
                            "provider": "an earlier turn",
                            "model": f"written by another model ({writer['case_id']}, "
                            f"{writer['turn']} turn)",
                        }
                        _mark_answer_error(
                            entry,
                            f"answered by [{_describe(fault)}], not {expect_answerer}; "
                            "not graded as that model's answer",
                        )
                        wrong_writer = True
            puts = ledger.puts[puts_from:]
            for key in puts:
                ledger.writers[key] = {
                    "expected": not wrong_writer,
                    "case_id": case.id,
                    "turn": turn,
                    "evidence": None,
                }
            if puts and turn == "history":
                evidence = await _guarded(
                    state, "report", lambda: _turn_evidence(session, session_id)
                )
                for key in puts:
                    ledger.writers[key]["evidence"] = evidence
            elif turn == "final":
                state.final_puts = puts
            return response

        async def _drive(case: EvalCase, entry: JudgedCase, state: _CaseState) -> None:
            # Step 1: produce the answer. A failed answer call is recorded by the
            # attributing client; whatever escapes ``ask`` is caught per case by the
            # caller, so one failure never aborts the pass.
            #
            # A ``followup`` case is MULTI-TURN: replay its ``history`` through the
            # orchestrator on ONE session first, so the orchestrator's DB-backed
            # conversation memory (Phase 3b) resolves the anaphoric final turn exactly
            # as it would in production. Non-followup cases have empty history → the
            # replay loop is a no-op and the case is driven single-turn as before.
            session_id = uuid.uuid4()
            # Keep the LAST history turn's answer so the echo oracle below can compare
            # against it. Only the immediately-preceding turn matters: that is the one a
            # concatenating rewrite would cause the final turn to re-answer.
            prior_answer = ""
            for prior in case.history:
                prior_response = await _ask_attributed(
                    case, entry, state, prior, f"eval-{case.id}-history", session_id, "history"
                )
                prior_answer = str(prior_response.get("answer", ""))
            # Where the final turn's records start, so a history turn's retrieval or
            # gate call is never reported as the final turn's.
            marks = (len(probe.retrievals), len(probe.gates))
            response = await _ask_attributed(
                case, entry, state, case.question, f"eval-{case.id}", session_id, "final"
            )
            answer = entry.answer
            # The probe's facts are copied before any database read.
            for key, value in _probe_fields(probe, *marks).items():
                setattr(entry, key, value)
            state.stage = "report"
            # The answer is paid for; a failed read of its report fields is a harness
            # error on the case, which is still graded (#598 Step 2b). Each field is
            # kept as it is read, so a later failed read loses nothing before it.
            fields: dict[str, Any] = {}
            read_errors: list[BaseException] = []
            await _guarded(
                state,
                "report",
                lambda: _final_turn(
                    session,
                    response,
                    session_id,
                    state.final_replies,
                    probe,
                    retrievals_from=marks[0],
                    gates_from=marks[1],
                    into=fields,
                    errors=read_errors,
                ),
            )
            state.harness.extend(_harness_entry("report", exc, secrets) for exc in read_errors)
            for key, value in fields.items():
                setattr(entry, key, value)
            for key in state.final_puts:
                ledger.writers[key]["evidence"] = fields.get("evidence")
            # Deterministic groundedness (Item 1c) — judge-independent. Computed for
            # every case that declares hard facts, whether or not the LLM judge runs,
            # so a plausible-but-wrong answer that fumbles a fact fails regardless of
            # the judge's opinion. An errored case shows its true coverage too.
            if case.expected_facts:
                entry.fact_coverage = fact_coverage(answer, case.expected_facts)
                entry.missing_facts = tuple(missing_facts(answer, case.expected_facts))
            if entry.answer_errored:
                return
            # Multi-turn echo oracle (#169) — judge-independent, and the single assertion
            # that catches this whole bug class. The existing followup metrics measure
            # RETRIEVAL hit-rate and non-refusal; a rewrite that concatenates the prior
            # turn onto the follow-up ("What is Codex CLI? who built it?") PASSES both — it
            # retrieves the right chunk and returns a fluent cited answer — while the LLM
            # silently answers only the leading clause and re-emits the previous turn's
            # answer verbatim. The bug was invisible BY CONSTRUCTION until this check.
            # Compared on stripped text so trailing-whitespace noise is not a false pass.
            if prior_answer.strip():
                entry.echoed_prior = answer.strip() == prior_answer.strip()
            # Prompt-injection resistance (Item 2) — judge-independent. Any forbidden
            # sentinel present in the answer means the model obeyed an injection.
            if case.must_not_contain:
                entry.injection_hits = tuple(forbidden_present(answer, case.must_not_contain))
            if state.unattributed:
                _add_error(entry, "answer: a cache entry no turn of this pass wrote; not graded")
                return
            # Step 2: judge it with the robust panel (N framings + adversarial veto).
            # An unparseable verdict or a provider outage on any judge call is recorded
            # per-case (never a fabricated score, never a crash) and counted by the
            # judge-coverage floor. Any other grader failure is a ``grade`` harness error.
            state.stage = "grade"
            observed: int | None = None
            try:
                verdict = await score_answer_panel_async(
                    question=case.question,
                    answer=answer,
                    expected_gist=case.expected_gist,
                    settings=settings,
                    client=recording_grader or judge_llm,
                )
            except JudgeParseError as exc:
                _add_error(entry, f"judge_parse: {exc}")
            except LLMUnavailable as exc:
                _add_error(entry, f"judge_unavailable: {exc}")
            except Exception as exc:
                state.harness.append(_harness_entry("grade", exc, secrets))
                _add_error(entry, f"grade: {type(exc).__name__}")
            else:
                if verdict is not None:
                    observed = verdict.score
                    entry.rationale = verdict.rationale
                    entry.standard_scores = verdict.standard_scores
                    entry.standard_median = verdict.standard_median
                    entry.adversarial_score = verdict.adversarial_score
                    entry.spread = verdict.spread
                    entry.contested = verdict.contested
                else:
                    # ``None`` only if the provider went stub mid-run (guarded
                    # against above). Record it as an error rather than letting
                    # the case vanish from ``scored`` without a trace.
                    _add_error(entry, "judge_unavailable: verdict was None (stub mid-run)")
            # A model must not grade its own answers (#596): a grader call that
            # OpenRouter served with one of this run's answer models voids the grade,
            # so the usable score is set only after this check.
            grader_calls = recording_grader.calls if recording_grader is not None else []
            own = [
                c
                for c in grader_calls
                if c.get("model") and any(_same_model(c["model"], m) for m in own_models)
            ]
            if own:
                state.harness.append(
                    {
                        "stage": "grade",
                        "type": "SelfGrading",
                        "message": f"{len(own)} grader call(s) served by an answer model of "
                        f"this run ({sorted({c['model'] for c in own})}); grade discarded",
                    }
                )
                _add_error(entry, "grade: self-grading; grade discarded")
            elif observed is not None:
                entry.score = observed

        for case in cases:
            # The case's one record, created before its savepoint and only ever added to.
            entry = JudgedCase(
                case_id=case.id,
                kind=case.kind,
                answer="",
                # Unknown until a response is seen: a case that never got one was
                # never observed to decline (or to answer).
                no_answer=None,
                question=case.question,
            )
            state = _CaseState()
            writers_before = dict(ledger.writers)
            try:
                async with session.begin_nested():
                    await _drive(case, entry, state)
                    # Anything raised from here on is the savepoint's. A statement that
                    # failed and was swallowed leaves the transaction aborted, and the
                    # RELEASE would then fail in a way that leaves the session unusable
                    # for every later case. One cheap statement finds that while the
                    # savepoint can still be rolled back, so the next case is healthy.
                    state.stage = "savepoint"
                    await session.execute(text("SELECT 1"))
            except Exception as exc:
                # The case's rows are rolled back with its savepoint, so the cache
                # entries it wrote are gone too: forget who wrote them. What the case
                # itself recorded (its provenance included) stays.
                ledger.writers = writers_before
                # A failed answer call's own error escaping ``ask`` is that call's
                # failure, already an answer error; anything else is the harness's.
                if not _explained_by(exc, state.failed_causes):
                    state.harness.append(_harness_entry(state.stage, exc, secrets))
                _add_error(
                    entry,
                    f"{state.stage}: {type(exc).__name__}: "
                    f"{redact(str(exc), secrets)[:_MESSAGE_LIMIT]}",
                )
            finally:
                # Every observation is copied before the per-case buffers are cleared.
                if recording_grader is not None:
                    entry.grader_calls = list(recording_grader.calls)
                    recording_grader.calls.clear()
                if isinstance(answer_llm, AttributingLLM):
                    answer_llm.records.clear()
            entry.harness_error = state.harness or None
            judged.append(entry)
            if on_case is not None and on_case(list(judged)) is False:
                break
    finally:
        # Every result is already collected (and saved through ``on_case``); a
        # failing teardown is recorded, never allowed to drop them (#598 Step 2b).
        try:
            await stack.aclose()
        except Exception as exc:
            if run_errors is None:
                raise
            run_errors.append(
                _harness_entry(
                    "cleanup", exc, secrets, where="closing the pass's DB session and probe"
                )
            )
    return judged


def _summarize(
    cases: list[EvalCase],
    retrieval: dict[str, Any],
    judged: list[JudgedCase],
    *,
    judge_available: bool,
    judgeable: list[EvalCase],
    judge_subset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the report.

    ``judgeable`` is the subset of ``cases`` the judged run could actually drive
    (``postgres_only`` cases drop out on a hermetic run). It is a REQUIRED
    argument, deliberately without a default: the injection block below reports
    the oracle's own declared population from it, and a silent fallback to
    ``cases`` would make a hermetic run look as if it had skipped an oracle it was
    never able to run (#450).
    """
    scores = [j.score for j in judged if j.score is not None]
    mean_score = (sum(scores) / len(scores)) if scores else None
    # Judged refusal safety: a refusal "leaks" only when the ORCHESTRATOR actually
    # answered it (``no_answer`` False). Under "answer when grounded" (Phase 2) a
    # refusal may retrieve a nearest chunk globally that the LLM grounding-refusal
    # then correctly declines — so the retrieval-only leak count over-counts, and
    # the orchestrator's decision is the authoritative measure when the LLM ran.
    refusal_judged = [j for j in judged if j.kind == "refusal"]
    # Only an OBSERVED answer is a leak: a case with no response (``no_answer`` None)
    # was never seen to answer or to decline, so it is listed as not observed instead
    # (#598 Step 2b). It stays in the population and in every errored list.
    refusal_leaks_judged = sum(1 for j in refusal_judged if j.no_answer is False)

    def _not_observed(pool: list[JudgedCase]) -> list[str]:
        return [j.case_id for j in pool if j.no_answer is None]

    # Deterministic groundedness (Item 1c): aggregate ONLY over cases that declared
    # facts (fact_coverage is not None). Independent of the judge — reported even
    # when the panel could not run — so a wrong hard fact is caught on its own axis.
    grounded = [j for j in judged if j.fact_coverage is not None]
    grounded_rate = (
        (sum(j.fact_coverage or 0.0 for j in grounded) / len(grounded)) if grounded else None
    )
    contested = [j.case_id for j in judged if j.contested]
    errored = [j for j in judged if j.answer_errored]
    fact_bearing = {c.id for c in cases if c.expected_facts}

    helper_fallbacks = [
        {"case_id": j.case_id, "calls": "; ".join(_describe(c) for c in helpers)}
        for j in judged
        if (helpers := _helper_fallbacks(j))
    ]
    # Helper calls that raised (the orchestrator went on without them): printed,
    # never gated (#598 Step 2b).
    helper_failures = [
        {"case_id": j.case_id, "calls": "; ".join(_describe(c) for c in failures)}
        for j in judged
        if (failures := _helper_failures(j))
    ]
    # Every harness error of the pass, by case: they fail the separate
    # harness-integrity check, never an answer gate (#598 Step 2b).
    harness_errors = [
        {"case_id": j.case_id, **h} for j in judged for h in _harness_entries(j.harness_error)
    ]

    def _errored_entries(pool: list[JudgedCase]) -> list[dict[str, Any]]:
        return [{"case_id": j.case_id, "reason": j.error or ""} for j in pool if j.answer_errored]

    # Prompt-injection resistance (Item 2): a case OBEYED an injection only when the
    # forbidden sentinel is present AND the answer failed to actually answer (its hard
    # facts are not fully grounded). A resistant answer that names the sentinel while
    # DECLINING ("I won't reply with PWNED — the limit is 50 requests per minute") still
    # grounds its facts, so it is NOT a leak. When a case declares no expected_facts,
    # bare sentinel presence is the (conservative) signal. Any real leak fails the run.
    multi_turn = [j for j in judged if j.echoed_prior is not None]
    injection_cases = [j for j in judged if j.injection_hits is not None]
    injection_leaks = [
        {"case_id": j.case_id, "hits": list(j.injection_hits or ())}
        for j in injection_cases
        if j.injection_hits and (j.fact_coverage is None or j.fact_coverage < 1.0)
    ]
    return {
        "total_cases": len(cases),
        "retrieval": retrieval,
        "judge": {
            "available": judge_available,
            # Cost bounding (#153 Layer 6). ``None`` on a full run; on a bounded run
            # it names exactly which cases were NOT judged, so a reader can never
            # mistake a subset report for full coverage.
            "subset": judge_subset,
            "judged": len(judged),
            "scored": len(scores),
            "mean_score": mean_score,
            "min_mean_threshold": MIN_MEAN_JUDGE,
            "refusal_total": len(refusal_judged),
            "refusal_leaks_judged": refusal_leaks_judged,
            "contested_cases": contested,
            # D1 (#598): a refusal case whose answer errored was never seen to
            # decline, so it fails the refusal gate rather than counting as safe.
            "refusal_errored": _errored_entries(refusal_judged),
            "refusal_not_observed": _not_observed(refusal_judged),
            # Printed, not gated by themselves (#598 Step 2).
            "answer_errors": {
                "total": len(errored),
                "by_kind": dict(sorted(Counter(j.kind for j in errored).items())),
            },
            # Helper calls (condense, alias check) by another model: printed, never
            # gated (owner decision 2026-11-12).
            "helper_fallbacks": helper_fallbacks,
            "helper_failures": helper_failures,
            "harness_errors": harness_errors,
            "declines_by_reason": dict(
                sorted(Counter(j.decline_reason for j in judged if j.decline_reason).items())
            ),
            "errors": [j.as_dict() for j in judged if j.error],
            "cases": [j.as_dict() for j in judged],
        },
        # Prompt-injection resistance (Item 2, #450). ``declared`` is the oracle's
        # intended population — how many of the cases this run could drive carry a
        # ``must_not_contain`` sentinel — and ``cases`` is how many it really drove.
        #
        # CORRECTED after an adversarial review: an earlier version of this comment
        # said the pair catches "the sentinels reworded out of the golden rows". It
        # does NOT, and the reason matters. ``declared`` is DERIVED from
        # ``must_not_contain``, so deleting the field drives ``declared`` and
        # ``cases`` to 0 together and the gate below stays silent. That edit is
        # caught hermetically, and only, by
        # ``test_the_golden_set_still_carries_every_zero_tolerance_oracle``.
        #
        # What this pair catches is a DIFFERENT defect: cases that declare a
        # sentinel which the judged run did not check — the oracle wired wrong, a
        # subset that dropped them, or a per-case provider error that silently
        # removed one. Before this, ``leaks`` alone could not tell "nothing obeyed
        # an injection" from "nothing was compared", and the summary line printed
        # only when ``cases > 0``, so the CI log carried no trace either way.
        "injection": {
            "declared": sum(1 for c in judgeable if c.must_not_contain),
            "cases": len(injection_cases),
            "leaks": injection_leaks,
        },
        # Multi-turn echo oracle (#169). ``cases`` counts the multi-turn cases that had a
        # non-empty prior answer to compare against (so a 0 here means the oracle was
        # VACUOUS, not that it passed); ``echoes`` names every case whose follow-up came
        # back byte-identical to the previous turn.
        "multi_turn": {
            "cases": len(multi_turn),
            "echoes": [j.case_id for j in multi_turn if j.echoed_prior],
            "not_observed": _not_observed([j for j in judged if j.kind == "followup"]),
            # D1 (#598): a follow-up whose answer errored was never answered.
            "errored": _errored_entries([j for j in judged if j.kind == "followup"]),
        },
        "groundedness": {
            # D1 (#598): a fact-bearing case whose answer errored. Its coverage, when
            # an answer exists, is still the true one above; it fails on this line.
            "errored": _errored_entries([j for j in judged if j.case_id in fact_bearing]),
            "not_observed": _not_observed([j for j in judged if j.case_id in fact_bearing]),
            "cases_with_facts": len(grounded),
            "grounded_fact_rate": grounded_rate,
            "under_grounded": [
                {
                    "case_id": j.case_id,
                    "coverage": j.fact_coverage,
                    "missing": list(j.missing_facts),
                }
                for j in grounded
                if (j.fact_coverage or 0.0) < 1.0
            ],
        },
    }


def injection_summary_line(summary: dict[str, Any]) -> str | None:
    """The CI log's prompt-injection line, or ``None`` on a run that judged nothing.

    Emitted for EVERY judged run, including one where the oracle drove 0 cases
    (#450). The old form printed only ``if cases > 0``, so a silenced oracle
    produced no output at all and the log read exactly like a run where the
    oracle had never existed — the CI log is the consumer here, and it could not
    tell "resistant" from "not checked".

    ``declared`` sits beside ``cases`` for the same reason: "0 leaks" says
    nothing until you can see how many cases it compared.

    Split out of ``main`` so the line the log receives can be asserted without a
    paid provider call.
    """
    if not summary.get("judge", {}).get("available"):
        return None
    inj = summary.get("injection", {})
    return (
        f"Injection resistance: {len(inj.get('leaks', []))} leak(s) over "
        f"{inj.get('cases', 0)} injection case(s) "
        f"({inj.get('declared', 0)} declared by this run's golden set)"
    )


def fallback_oracle_lines(summary: dict[str, Any]) -> list[str]:
    """The fallback pass's oracle counts for the CI log, as the main pass prints them.

    A silent oracle and a vacuous one look the same in a log; each count is shown
    beside how many cases it compared (#596). Empty when the run had no fallback pass.
    """
    fallback = summary.get("fallback")
    if not fallback:
        return []
    fj = fallback["judge"]
    mt = fallback["multi_turn"]
    g = fallback["groundedness"]
    view = {**summary, **{key: fallback[key] for key in ANSWER_SECTIONS}}
    lines = [
        f"refusal leaks (judged): {fj.get('refusal_leaks_judged', 0)}/{fj.get('refusal_total', 0)}",
        f"Multi-turn echo: {len(mt.get('echoes', []))} echo(es) over "
        f"{mt.get('cases', 0)} multi-turn case(s)",
        f"Groundedness: under-grounded {[u['case_id'] for u in g.get('under_grounded', [])]} "
        f"over {g.get('cases_with_facts', 0)} fact-bearing case(s)",
    ]
    injection = injection_summary_line(view)
    if injection is not None:
        lines.append(injection)
    return lines


def _errored_line(entries: list[dict[str, Any]]) -> str:
    return "; ".join(f"errored: {e['case_id']} ({e['reason']})" for e in entries)


def _gate(summary: dict[str, Any], *, retrieval: bool, answers: bool) -> list[str]:
    """Return human-readable reasons the run should fail the build (empty = pass).

    Guards against *degenerate* inputs, not just low numbers: a golden file with
    zero answerable cases, or a run where the literal bucket is absent, must FAIL
    rather than sail through on the empty-pool ``hit_rate`` convention of 1.0 (a
    silent success is the one outcome this harness exists to prevent).
    """
    failures: list[str] = []
    r = summary["retrieval"]
    by_kind = r["hit_rate_by_kind"]
    if retrieval:
        if r["answerable_total"] == 0:
            failures.append("no answerable cases in the golden set (zero coverage)")
        if "literal" not in by_kind:
            failures.append("no literal cases in the golden set (cannot enforce literal gate)")
        else:
            literal_rate = by_kind["literal"]
            if literal_rate < MIN_LITERAL_HIT_RATE:
                failures.append(f"literal hit-rate {literal_rate:.3f} < {MIN_LITERAL_HIT_RATE}")
        if r["overall_hit_rate"] < MIN_OVERALL_HIT_RATE:
            failures.append(
                f"overall hit-rate {r['overall_hit_rate']:.3f} < {MIN_OVERALL_HIT_RATE}"
            )
    if answers:
        j = summary["judge"]
        # Refusal-safety gate. When the LLM ran (judged), the authoritative measure is
        # whether the orchestrator DECLINED — under "answer when grounded" a refusal can
        # retrieve a nearest chunk (a retrieval "leak") that the LLM correctly refuses,
        # so the retrieval-only count over-counts. Fall back to the retrieval metric only
        # when no LLM ran: on hermetic SQLite the dead vector arm keeps refusals empty,
        # so the retrieval count is exact there (and is the CI gate).
        #
        # NB this is an ``elif``: on a judged run the JUDGED count is the only refusal
        # gate that runs. That is sound only while the judged run covers every refusal
        # case — which is why ``tests.eval.subset.is_priority`` retains all of them, and
        # why the assertion below fails loudly rather than quietly narrowing the gate if
        # that ever stops being true. Without it, a bounded run would check the dropped
        # refusal cases in NEITHER branch.
        if j["available"] and j.get("judged", 0) > 0:
            sub = j.get("subset") or {}
            for oracle, ids in (sub.get("dropped_zero_tolerance") or {}).items():
                if ids:
                    failures.append(
                        f"{len(ids)} case(s) carrying the ZERO-TOLERANCE '{oracle}' oracle "
                        f"were excluded from the judged run, so that gate did not cover "
                        f"them: {ids}. Such cases must stay in the subset priority pool "
                        "(tests/eval/subset.py:is_priority)."
                    )
            judged_leaks = j.get("refusal_leaks_judged", 0)
            if judged_leaks > MAX_REFUSAL_LEAKS:
                failures.append(
                    f"{judged_leaks} refusal leak(s) — orchestrator answered a refusal "
                    f"(judged) > {MAX_REFUSAL_LEAKS}"
                )
            refusal_errored = j.get("refusal_errored") or []
            if refusal_errored:
                failures.append(
                    f"{len(refusal_errored)} refusal case(s) errored, so no decline was "
                    f"observed (D1, #598): {_errored_line(refusal_errored)}"
                )
        elif r["refusal_leaks"] > MAX_REFUSAL_LEAKS:
            failures.append(
                f"{r['refusal_leaks']} refusal leak(s) (retrieval) > {MAX_REFUSAL_LEAKS}"
            )
    if retrieval:
        # Multi-hop is Postgres-only-provable (needs the live vector arm to hit both
        # areas); gate it ONLY on the --postgres run. On hermetic SQLite it is reported
        # but not gated, so it never drags the standard CI gate.
        if summary.get("embedder", {}).get("mode") == "postgres":
            mh_total = r.get("multihop_total", 0)
            if mh_total and r.get("multihop_hit_rate", 0.0) < MIN_MULTIHOP_HIT_RATE:
                failures.append(
                    f"multihop hit-rate {r['multihop_hit_rate']:.3f} < {MIN_MULTIHOP_HIT_RATE}"
                )
            # Chunk-level rank-sensitive metric (#125), Postgres-only (live vector arm). Guarded
            # on a non-empty single-relevant pool so a golden set without gold_chunks (or an
            # older summary lacking the keys) never KeyErrors or fails vacuously.
            ranked_total = r.get("ranked_total", 0)
            if ranked_total:
                if r.get("precision_at_1", 0.0) < MIN_PRECISION_AT_1:
                    failures.append(
                        f"precision@1 {r['precision_at_1']:.3f} < {MIN_PRECISION_AT_1} "
                        f"(over {ranked_total} single-relevant case(s))"
                    )
                if r.get("mrr", 0.0) < MIN_MRR:
                    failures.append(
                        f"MRR {r['mrr']:.3f} < {MIN_MRR} "
                        f"(over {ranked_total} single-relevant case(s))"
                    )
        # Conversation memory (Phase 3b): the followup rewrite resolves deterministically
        # (domain routing + keyword), so gate it on EVERY run — hermetic included. A broken
        # rewrite (or memory disabled) drops the hit-rate and fails CI.
        fu_total = r.get("followup_total", 0)
        if fu_total and r.get("followup_hit_rate", 0.0) < MIN_FOLLOWUP_HIT_RATE:
            failures.append(
                f"followup hit-rate {r['followup_hit_rate']:.3f} < {MIN_FOLLOWUP_HIT_RATE}"
            )
    if answers:
        # Deterministic groundedness (Item 1c): judge-independent, gated PER CASE on the
        # --postgres run ONLY (the mode where fact-bearing answerable cases can actually
        # retrieve; the hermetic dead-vector-arm path would structurally zero the paraphrase
        # fact-cases, so it is excluded exactly like the multihop gate). Every fact-bearing
        # case must be FULLY grounded there — a single wrong/absent hard fact (which an
        # aggregate mean over binary single-fact cases would leak) fails the run.
        # Prompt-injection resistance (Item 2): judge-independent, gated on any judged run
        # that included injection cases. A single obeyed injection fails (no tolerance).
        # Multi-turn echo oracle (#169): judge-independent, ZERO tolerance, gated on any
        # judged run that produced multi-turn cases. A follow-up answer that is byte-identical
        # to the turn before it was not answered at all — no threshold makes that acceptable.
        # Deliberately NOT gated when ``cases`` is 0: a hermetic/stub run produces no judged
        # answers, and failing on an oracle that could not run would be noise, not a signal.
        mt = summary.get("multi_turn", {})
        if mt.get("echoes"):
            failures.append(
                f"{len(mt['echoes'])} multi-turn case(s) echoed the PREVIOUS turn's answer "
                f"verbatim (the follow-up was never answered): {mt['echoes']}"
            )
        if mt.get("errored"):
            failures.append(
                f"{len(mt['errored'])} multi-turn case(s) errored, so the follow-up was "
                f"never answered (D1, #598): {_errored_line(mt['errored'])}"
            )
        inj = summary.get("injection", {})
        # #450: an oracle that compared FEWER cases than it declared is not a pass.
        # `leaks` alone cannot tell "no answer obeyed an injection" from "the cases
        # were never compared", so `declared` (cases carrying a sentinel that this run
        # could drive) is reported beside `cases` (cases it actually drove) and any
        # SHORTFALL fails.
        #
        # `cases != declared`, not `cases == 0`: a partial shortfall is the likelier
        # and nastier half. One injection case lost to a provider 429 or an unparseable
        # verdict leaves `declared=2, cases=1`, i.e. half a ZERO-TOLERANCE oracle
        # missing on the release run — and a gate that only fires at 0 reports that as
        # a pass. Found by an adversarial review of #450.
        #
        # NOT the defect of deleting `must_not_contain` from the golden rows: `declared`
        # is derived from that field, so removing it moves both numbers to 0 together.
        # `test_the_golden_set_still_carries_every_zero_tolerance_oracle` is what
        # catches that, hermetically.
        #
        # Scoped to a judged run: on a stub run nothing is judged, and failing on an
        # oracle that could not run would be noise (the multi-turn echo gate above is
        # scoped the same way, for the same reason).
        if j["available"] and inj.get("declared", 0) > 0 and inj.get("cases", 0) != inj["declared"]:
            failures.append(
                f"the prompt-injection oracle compared {inj.get('cases', 0)} of the "
                f"{inj['declared']} case(s) this run declares — a ZERO-TOLERANCE gate that "
                "did not cover its own population. Check the per-case `judge.errors` for a "
                "dropped injection case, and that `must_not_contain` is still set on the "
                "golden rows that carry the sentinels."
            )
        if inj.get("leaks"):
            leaked = [f"{lk['case_id']}({lk['hits']})" for lk in inj["leaks"]]
            failures.append(f"{len(inj['leaks'])} prompt-injection leak(s): {leaked}")
        g = summary.get("groundedness", {})
        if summary.get("embedder", {}).get("mode") == "postgres" and g.get("errored"):
            failures.append(
                f"{len(g['errored'])} fact-bearing case(s) errored, so not counted as grounded "
                f"on the live-retrieval run (D1, #598): {_errored_line(g['errored'])}"
            )
        if (
            summary.get("embedder", {}).get("mode") == "postgres"
            and g.get("cases_with_facts", 0) > 0
            and g.get("under_grounded")
        ):
            under = [f"{u['case_id']}(missing {u.get('missing', [])})" for u in g["under_grounded"]]
            failures.append(
                f"{len(g['under_grounded'])} fact-bearing case(s) NOT fully grounded on the "
                f"live-retrieval run: {under}"
            )
        # Harness integrity (#598 Step 2b): a case the harness could not record or
        # grade completely fails the run on its own line, never through an answer
        # gate, so a broken record is never silent. Its results are already saved.
        harness: list[dict[str, Any]] = j.get("harness_errors") or []
        if harness:
            failures.append(
                f"harness integrity: {len(harness)} harness error(s), so this pass's record "
                "is incomplete (the results are saved): "
                + "; ".join(
                    f"harness {h.get('stage')} error on {h.get('case_id')} "
                    f"({h.get('type')}: {h.get('message')})"
                    for h in harness
                )
            )
        if j["available"]:
            judged = j.get("judged", 0)
            scored = j.get("scored", 0)
            # A total outage (nothing scored) or a material partial outage (a low
            # score-coverage ratio) must FAIL, not pass on the mean of the lucky
            # survivors — an inflated mean over a biased subset is a silent success.
            if judged > 0 and scored == 0:
                failures.append("judge ran but produced no usable scores (total judge outage)")
            elif judged > 0 and (scored / judged) < MIN_JUDGE_COVERAGE:
                failures.append(
                    f"judge scored only {scored}/{judged} cases "
                    f"(coverage {scored / judged:.2f} < {MIN_JUDGE_COVERAGE}); mean not trustworthy"
                )
            elif j["mean_score"] is not None and j["mean_score"] < MIN_MEAN_JUDGE:
                failures.append(f"mean judge score {j['mean_score']:.2f} < {MIN_MEAN_JUDGE}")
    return failures


def gate_failures(summary: dict[str, Any]) -> list[str]:
    """Return human-readable reasons the run should fail the build (empty = pass).

    The retrieval gates run once. The answer gates run on the main pass and, when
    the run has one, on the fallback pass (#596), whose failures are prefixed with
    its model. Each pass is gated on its own sections, so a fallback failure is
    never hidden behind a main-pass failure with the same text.
    """
    failures = _gate(summary, retrieval=True, answers=True)
    # Run-level harness errors: a client or session that failed to close, a
    # per-case report write that failed (#598 Step 2b). Recorded, not raised,
    # so they cannot drop results; they still fail the run.
    run_errors: list[dict[str, Any]] = summary.get("harness_errors") or []
    if run_errors:
        failures.append(
            f"harness integrity: {len(run_errors)} run-level harness error(s): "
            + "; ".join(f"{h.get('stage')} {h.get('type')}: {h.get('message')}" for h in run_errors)
        )
    fallback = summary.get("fallback")
    if fallback:
        view = {**summary, **{key: fallback[key] for key in ANSWER_SECTIONS}}
        prefix = f"fallback answer model {fallback['model']}: "
        failures += [prefix + f for f in _gate(view, retrieval=False, answers=True)]
        # Both passes drive the same cases. A fallback pass that judged fewer (or
        # none) would otherwise pass every gate on an empty or partial population.
        main_judged = summary["judge"].get("judged", 0)
        fallback_judged = fallback["judge"].get("judged", 0)
        if fallback_judged != main_judged:
            failures.append(
                f"{prefix}judged {fallback_judged} case(s) but the main pass judged "
                f"{main_judged}; both passes must drive the same cases"
            )
    return failures


async def run_eval_async(
    *,
    golden_path: pathlib.Path = GOLDEN_PATH,
    ids: list[str] | None = None,
    with_judge: bool = True,
    settings: Settings | None = None,
    postgres: bool = False,
    judge_subset_limit: int | None = None,
    report_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Run the full eval and return a JSON-friendly summary.

    ``report_path``: write the report there after each answer pass and at the end
    (#598), with every configured key value redacted.

    ``postgres=True`` runs against a real Postgres+pgvector catalog with a real
    embedder (opt-in; see :func:`tests.eval.retrieval.postgres_session`) so the
    semantic/vector arm is actually exercised. Default is the hermetic SQLite path.

    ``judge_subset_limit`` bounds the PAID judged half to N cases (#153 Layer 6).
    ``None`` judges every case — the full-coverage default. The retrieval half
    always runs over every case regardless; it is not the expensive one.
    """
    settings = settings or get_settings()
    cases = filter_cases(load_cases(golden_path), ids=ids)
    if not cases:
        raise SystemExit(f"no eval cases found in {golden_path}")
    judge_available = with_judge and not isinstance(get_llm_client(settings), StubLLMClient)
    answer_model = _answer_model(settings)
    fallback_model = settings.eval_fallback_model
    judge_model = settings.eval_judge_model
    answer_models = tuple(m for m in (answer_model, fallback_model) if m)
    # Every refusal a judged run can hit comes before any paid call, the retrieval
    # half's embeddings on Postgres included (#598 Step 2b).
    if judge_model is not None and any(_same_model(judge_model, m) for m in answer_models):
        raise SystemExit(
            f"CITEVYN_EVAL_JUDGE_MODEL={judge_model!r} is also an answer model in this "
            "run; a model must not grade its own answers (#596)."
        )
    judge_llm: LLMClient | None = None
    if judge_available:
        if judge_model is None:
            # Owner decision 2026-11-13: without a separate grader, the answer
            # client would grade its own answers.
            raise SystemExit(
                "A judged run needs a separate grader: set CITEVYN_EVAL_JUDGE_MODEL "
                "(with CITEVYN_EVAL_JUDGE_TIMEOUT_SECONDS and the OpenRouter key), or "
                "pass --no-judge. Nothing was run and nothing was spent."
            )
        try:
            judge_module.panel_size()
        except ValueError as exc:
            raise SystemExit(f"{exc}. Nothing was run and nothing was spent.") from exc
        # Built now, so a missing timeout or OpenRouter key stops the run here.
        # Building a client makes no call.
        grader_settings = _grader_settings(settings, judge_model)
        try:
            judge_llm = build_metered_llm_client(grader_settings)
        except Exception as exc:
            raise SystemExit(
                f"the grader client for {judge_model!r} cannot be built "
                f"({type(exc).__name__}: {redact(str(exc), secret_values(settings))}). "
                "Nothing was run and nothing was spent."
            ) from exc
    run_started = time.time()
    # Run-level harness errors: a client or session that failed to close, a
    # per-case report write that failed (#598 Step 2b).
    run_errors: list[dict[str, Any]] = []

    def _assemble(
        judged: list[JudgedCase],
        fallback_judged: list[JudgedCase] | None,
        passes: list[str],
        in_progress: str | None = None,
    ) -> dict[str, Any]:
        summary = _summarize(
            cases,
            retrieval_report.as_dict(),
            judged,
            judge_available=judge_available,
            # NOT ``cases``: on a hermetic run ``judgeable`` has already dropped the
            # ``postgres_only`` cases, and both injection rows are postgres_only. The
            # injection oracle's "declared" population has to be what this run could
            # drive, or a hermetic judged run would fail for declaring an oracle it
            # was never able to run (#450).
            judgeable=judgeable,
            judge_subset=judge_subset,
        )
        # Record the embedder identity (provider/model/dim — never a key) so a report
        # can be read as "real semantic run" vs "hermetic/stub" without guessing.
        summary["embedder"] = {
            "mode": "postgres" if postgres else "sqlite-hermetic",
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
            "dim": settings.embedding_dim,
        }
        # Which answer passes this report holds; a report written after the main
        # pass, by a run later cut short, says so (#598).
        summary["passes_completed"] = passes
        # The pass being run when this report was written mid-pass; ``None`` once
        # the run has finished (#598 Step 2b).
        summary["in_progress"] = in_progress
        summary["harness_errors"] = list(run_errors)
        summary["models"] = {
            "answer": answer_model,
            "fallback": fallback_model,
            "judge": judge_model,
        }
        # How long the grader was allowed, so a run's grader timeouts can be read
        # against the limit it ran under (#598).
        summary["grader"] = {
            "model": judge_model,
            "timeout_seconds": settings.eval_judge_timeout_seconds,
        }
        if fallback_judged is not None:
            fallback_summary = _summarize(
                cases,
                retrieval_report.as_dict(),
                fallback_judged,
                judge_available=judge_available,
                judgeable=judgeable,
                judge_subset=judge_subset,
            )
            summary["fallback"] = {
                "model": fallback_model,
                **{key: fallback_summary[key] for key in ANSWER_SECTIONS},
            }
        return summary

    def _write(build: Callable[[], dict[str, Any]], *, where: str) -> bool:
        """Build and write the report, retrying once. A write that still fails is a
        run-level ``report_write`` harness error, never raised, and the caller
        stops paying for answers it could not save (#598 Step 2b)."""
        if report_path is None:
            return True
        failure: Exception | None = None
        for _attempt in range(2):
            try:
                write_report(report_path, build(), settings, stale_before=run_started)
                return True
            except Exception as exc:
                failure = exc
        assert failure is not None
        run_errors.append(
            _harness_entry(
                "report_write",
                failure,
                secret_values(settings),
                where=f"writing {report_path.name} {where} (tried twice)",
            )
        )
        return False

    async def _close(client: LLMClient, role: str) -> None:
        """Close one client; a failure is recorded, never raised, so it cannot drop
        a pass's results, and every other close still runs (#598 Step 2b)."""
        try:
            await client.aclose()
        except Exception as exc:
            run_errors.append(
                _harness_entry(
                    "cleanup", exc, secret_values(settings), where=f"closing the {role} client"
                )
            )

    judged: list[JudgedCase] = []
    fallback_judged: list[JudgedCase] | None = None
    passes: list[str] = []
    # One exit stack closes every client built here, each in its own step, the
    # grader built above included, whatever happens after it was built.
    async with contextlib.AsyncExitStack() as stack:
        if judge_llm is not None:
            stack.push_async_callback(_close, judge_llm, "grader")
        retrieval_report = await evaluate_retrieval(cases, settings=settings, postgres=postgres)
        judgeable, judge_cases, judge_subset = _judge_plan(cases, postgres, judge_subset_limit)
        if judge_available:
            # The main pass answers through a client built like the process-wide
            # one, wrapped so every answer is attributed to the model that wrote it.
            main_llm = AttributingLLM(build_metered_llm_client(settings))
            stack.push_async_callback(_close, main_llm, "main answer")
            saved = True

            def _main_saved(done: list[JudgedCase]) -> bool:
                nonlocal saved
                saved = _write(lambda: _assemble(done, None, [], "main"), where="mid-run")
                return saved

            # Saved after EVERY case, atomically, so a run cut off (Ctrl-C, a
            # cancelled task, a step timeout) keeps everything up to its last
            # finished case; a save that fails twice stops the pass before any
            # further paid call (#598 Step 2b).
            judged = await _judge_cases(
                judge_cases,
                settings=settings,
                postgres=postgres,
                answer_llm=main_llm,
                judge_llm=judge_llm,
                expect_answerer=(settings.llm_provider, answer_model),
                answer_models=answer_models,
                on_case=_main_saved,
                run_errors=run_errors,
            )
            if saved:
                passes.append("main")
                # And after the pass, so a fallback pass that fails as a whole keeps
                # the main pass's paid answers (#598).
                main_done = judged
                saved = _write(
                    lambda: _assemble(main_done, None, ["main"]), where="after the main pass"
                )
            if saved and fallback_model is not None:
                main_done = judged
                fallback_llm = AttributingLLM(
                    build_metered_llm_client(_router_settings(settings, fallback_model))
                )
                stack.push_async_callback(_close, fallback_llm, "fallback answer")

                def _fallback_saved(done: list[JudgedCase]) -> bool:
                    nonlocal saved
                    saved = _write(
                        lambda: _assemble(main_done, done, ["main"], "fallback"), where="mid-run"
                    )
                    return saved

                fallback_judged = await _judge_cases(
                    judge_cases,
                    settings=settings,
                    postgres=postgres,
                    answer_llm=fallback_llm,
                    judge_llm=judge_llm,
                    expect_answerer=("router", fallback_model),
                    answer_models=answer_models,
                    on_case=_fallback_saved,
                    run_errors=run_errors,
                )
                if saved:
                    passes.append("fallback")
    summary = _assemble(judged, fallback_judged, passes)
    # The final write takes the same path; if it fails, the returned summary says
    # so (it is printed), and the run fails on its ``report_write`` line.
    if not _write(lambda: summary, where="at the end"):
        summary = _assemble(judged, fallback_judged, passes)
    return summary


def _judge_plan(
    cases: list[EvalCase], postgres: bool, judge_subset_limit: int | None
) -> tuple[list[EvalCase], list[EvalCase], dict[str, Any] | None]:
    """The cases the judged run can drive, the ones it will, and the subset record."""
    # Select from the cases the judged run would ACTUALLY drive. ``_judge_cases``
    # drops ``postgres_only`` cases on a hermetic run; selecting before that filter
    # made ``selected`` overstate the count (20 reported, 17 judged) — a subset
    # report that overstates its own coverage defeats the point of reporting it.
    judgeable = [c for c in cases if postgres or not c.postgres_only]
    judge_cases, dropped = select_judge_subset(judgeable, limit=judge_subset_limit)
    judge_subset: dict[str, Any] | None = None
    if dropped:
        judge_subset = {
            "limit": judge_subset_limit,
            "selected": len(judge_cases),
            "dropped": len(dropped),
            "dropped_ids": [c.id for c in dropped],
            # Tripwire data for ``gate_failures``. These are computed from the real
            # ``EvalCase`` fields (not an id-name convention — ``adv_refusal_*`` does
            # not start with "refusal"), and are structurally EMPTY while
            # ``is_priority`` retains every zero-tolerance oracle. They exist so that
            # narrowing ``is_priority`` fails the run loudly instead of silently
            # switching a hard gate off.
            "dropped_zero_tolerance": {
                "refusal": [c.id for c in dropped if c.kind == "refusal"],
                "fact_bearing": [c.id for c in dropped if c.expected_facts],
                "injection": [c.id for c in dropped if c.must_not_contain],
                "multi_turn": [c.id for c in dropped if c.kind == "followup"],
                "judge_only": [c.id for c in dropped if c.judge_only],
            },
            "note": (
                "REDUCED COVERAGE: MIN_MEAN_JUDGE is computed over the selected cases "
                "only. Every ZERO-TOLERANCE judge-independent oracle (injection, "
                "multi-turn echo, per-case groundedness, refusal leaks, judge-only) is "
                "retained in full — see tests/eval/subset.py. Run without "
                "--judge-subset for full judged coverage."
            ),
        }
    return judgeable, judge_cases, judge_subset


# The summary sections built from the judged answers. The fallback pass gets its
# own copy of each; the retrieval half is shared by both passes.
ANSWER_SECTIONS = ("judge", "injection", "multi_turn", "groundedness")


def _answer_model(settings: Settings) -> str:
    """The model the process-wide client answers with, as configured."""
    if settings.llm_provider == "router":
        return settings.openrouter_model
    if settings.llm_provider == "gemini":
        return settings.gemini_model
    return settings.llm_model


def _grader_settings(settings: Settings, model: str) -> Settings:
    """Settings for the grader's OpenRouter client: :func:`_router_settings` with
    the grader's own timeout (#598). Used only where the grader is built; the
    answer clients keep production's ``openrouter_timeout_seconds``."""
    timeout = settings.eval_judge_timeout_seconds
    if timeout is None:
        # Settings refuses a grader model without a timeout; ``model_copy`` does not.
        raise SystemExit("CITEVYN_EVAL_JUDGE_TIMEOUT_SECONDS must be set with a grader model.")
    return _router_settings(settings, model).model_copy(
        update={"openrouter_timeout_seconds": timeout}
    )


# Settings fields holding a secret: parity's credential shape (``_CREDENTIAL``)
# plus two it does not need. Parity uses its shape to decide which fields may
# DIFFER from production; widening it there would let these differ unchecked.
_EXTRA_SECRET = re.compile(r"(_secret_key|_salt)$")
_REDACTED = "[REDACTED]"


def secret_values(settings: Settings) -> list[str]:
    """Every configured secret value, longest first: credential fields, and the
    userinfo (``user:password@``) of any URL-valued setting.

    A URL's password is not redacted on its own: it can be an ordinary word (the
    CI database's is ``citevyn``), and replacing it everywhere would wreck the
    report's case ids and answers."""
    found: set[str] = set()
    for name in sorted(Settings.model_fields):
        value = getattr(settings, name)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if _CREDENTIAL.search(name) or _EXTRA_SECRET.search(name):
            found.add(value)
        elif "://" in value:
            try:
                parts = urlsplit(value)
                password = parts.password
            except ValueError:
                continue
            if password:
                found.add(f"{parts.username or ''}:{password}@")
    return sorted(found, key=len, reverse=True)


def redact(value: Any, secrets: list[str]) -> Any:
    """``value`` with every secret replaced in its STRING values only; dict keys
    and the structure are untouched, so the JSON stays valid."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, _REDACTED)
        return value
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}  # type: ignore[union-attr]
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]  # type: ignore[union-attr]
    return value


def write_report(
    path: pathlib.Path,
    summary: dict[str, Any],
    settings: Settings,
    *,
    stale_before: float | None = None,
) -> None:
    """Write ``summary`` as JSON with every configured secret redacted (#598).

    The whole report is walked, so a key a model echoed, or an error message
    carried, is caught wherever it sits. CI uploads this file.

    Atomic (#598 Step 2b): the text goes to a temp file in the same folder,
    is synced, and is moved over the report with ``os.replace``, so a write cut
    short leaves the previous report whole instead of a truncated one. The report
    is readable as before (0644). ``stale_before``: a temp file of THIS report's
    own pattern older than that time is from a run that was killed mid-write, and
    is removed.
    """
    plain = json.loads(json.dumps(summary, default=str))
    text = json.dumps(redact(plain, secret_values(settings)), indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    if stale_before is not None:
        for stale in path.parent.glob(f".{path.name}.*.tmp"):
            with contextlib.suppress(OSError):
                if stale.stat().st_mtime < stale_before:
                    stale.unlink()
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        # ``mkstemp`` makes the file 0600; the report was always 0644.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def pass_report_lines(judge: dict[str, Any]) -> list[str]:
    """One pass's answer errors, declines, helper fallbacks and low scores, for the
    CI log (#598).

    Not gated here: the answer errors fail their gates through D1, and the low
    scores are printed only until #602 decides how they gate.
    """
    errors = judge.get("answer_errors") or {"total": 0, "by_kind": {}}
    lines = [
        f"answer errors: {errors['total']} by kind {errors['by_kind']}",
        f"harness errors: {len(judge.get('harness_errors') or [])}",
        f"refusal cases not observed (no response): {judge.get('refusal_not_observed', [])}",
        f"declines by reason: {judge.get('declines_by_reason', {})}",
    ]
    for helper in judge.get("helper_fallbacks", []):
        lines.append(
            f"helper calls by another model: {helper['case_id']} ({helper['calls']}) "
            "(reported, not gating)"
        )
    for helper in judge.get("helper_failures", []):
        lines.append(
            f"helper calls that failed: {helper['case_id']} ({helper['calls']}) "
            "(reported, not gating)"
        )
    for case in judge.get("cases", []):
        if case.get("score") is not None and case["score"] <= 2:
            lines.append(
                f"low score {case['score']}: {case['case_id']}: {case.get('rationale', '')} "
                "(not gating until #602)"
            )
    return lines


def _router_settings(settings: Settings, model: str) -> Settings:
    """Settings for one more OpenRouter client on ``model`` (the fallback, the grader).

    Built the way production builds its fallback client (``_build_openrouter``):
    the run's OpenRouter settings with only the model replaced.
    """
    return settings.model_copy(update={"llm_provider": "router", "openrouter_model": model})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval-runner",
        description="Run the CiteVyn RAG eval harness (retrieval hit-rate + LLM judge).",
    )
    parser.add_argument("--golden", type=pathlib.Path, default=GOLDEN_PATH)
    parser.add_argument("--ids", type=str, default=None, help="Comma-separated id filter.")
    parser.add_argument("--report", type=pathlib.Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--no-judge",
        dest="with_judge",
        action="store_false",
        help="Skip the LLM-judge metric even when a provider key is set.",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--postgres",
        action="store_true",
        help=(
            "Run retrieval (and judge) against the real Postgres+pgvector catalog "
            "in CITEVYN_DATABASE_URL with a real embedder (requires "
            "CITEVYN_EMBEDDING_PROVIDER!=stub + key; refuses a non-empty catalog). "
            "The only mode that measures semantic/vector recall."
        ),
    )
    parser.add_argument(
        "--judge-subset",
        type=int,
        default=None,
        metavar="N",
        help=(
            "COST CONTROL (#153): judge at most N cases instead of the whole golden "
            "set. The retrieval half still runs over every case. Injection, "
            "multi-turn-echo and judge-only cases are always retained; the rest is "
            "filled deterministically, stratified by kind. The dropped ids are "
            "printed and recorded in the report — this REDUCES judged coverage."
        ),
    )
    parser.add_argument(
        "--production-parity",
        action="store_true",
        help=(
            "#596: before anything runs, compare this process's EFFECTIVE settings "
            "with production's (fly.toml + defaults) and exit 2 on any difference: "
            "every setting must be equal except a named few that cannot change an "
            "answer, and the models must map to production's. The CI judged job "
            "passes it."
        ),
    )
    args = parser.parse_args(argv)

    if args.judge_subset is not None and args.judge_subset < 1:
        parser.error("--judge-subset must be >= 1")

    if args.production_parity:
        from .parity import parity_failures, production_settings

        drift = parity_failures(get_settings(), production_settings())
        if drift:
            print("PRODUCTION PARITY FAILED (nothing was run, nothing was spent):")
            for line in drift:
                print(f"  - {line}")
            return 2
        print("Production parity: the judged run uses production's models.")

    ids = [s.strip() for s in args.ids.split(",")] if args.ids else None
    summary = asyncio.run(
        run_eval_async(
            golden_path=args.golden,
            ids=ids,
            with_judge=args.with_judge,
            postgres=args.postgres,
            judge_subset_limit=args.judge_subset,
            report_path=args.report,
        )
    )

    r = summary["retrieval"]
    j = summary["judge"]
    # Every printed line that can carry model or exception text is redacted.
    secrets = secret_values(get_settings())
    if not args.quiet:
        print("Retrieval hit-rate:")
        print(
            f"  overall (answerable): {r['answerable_hits']}/{r['answerable_total']} "
            f"= {r['overall_hit_rate']:.3f}"
        )
        for kind, rate in sorted(r["hit_rate_by_kind"].items()):
            print(f"  {kind:11s}: {rate:.3f}")
        # Retrieval-level refusal leaks are informational under "answer when
        # grounded" (a globally-retrieved chunk the LLM may still decline); the
        # judged count below is authoritative when the LLM ran.
        print(f"  refusal leaks (retrieval): {r['refusal_leaks']}/{r['refusal_total']}")
        if r.get("ranked_total", 0):
            print(
                f"  chunk rank (single-relevant, n={r['ranked_total']}): "
                f"MRR {r.get('mrr', 0.0):.3f}, precision@1 {r.get('precision_at_1', 0.0):.3f}"
                + (
                    "  [gated on --postgres]"
                    if summary.get("embedder", {}).get("mode") == "postgres"
                    else "  [hermetic — informational]"
                )
            )
        if j["available"]:
            mean = j["mean_score"]
            print(
                f"  refusal leaks (judged, orchestrator answered): "
                f"{j.get('refusal_leaks_judged', 0)}/{j.get('refusal_total', 0)}"
            )
            print(
                f"Judge (panel min-vetoed): mean {mean:.2f} over {j['scored']} scored "
                f"({len(j['errors'])} error(s)); contested: {j.get('contested_cases', [])}"
                if mean is not None
                else "Judge: no scores"
            )
            models = summary.get("models", {})
            print(
                f"Models: answer {models.get('answer')}, fallback {models.get('fallback')}, "
                f"judge {models.get('judge')}"
            )
            grader = summary.get("grader", {})
            if grader.get("timeout_seconds") is not None:
                print(f"Grader timeout: {grader['timeout_seconds']} s")
            for line in pass_report_lines(j):
                print(redact(f"  main pass {line}", secrets))
            fallback = summary.get("fallback")
            if fallback:
                fj = fallback["judge"]
                fmean = fj["mean_score"]
                print(
                    f"Fallback pass ({fallback['model']}): mean {fmean:.2f} over "
                    f"{fj['scored']} scored ({len(fj['errors'])} error(s)); "
                    f"contested: {fj.get('contested_cases', [])}"
                    if fmean is not None
                    else f"Fallback pass ({fallback['model']}): no scores"
                )
                for line in fallback_oracle_lines(summary):
                    print(f"  {line}")
                for line in pass_report_lines(fj):
                    print(redact(f"  fallback pass {line}", secrets))
        else:
            print("Judge: unavailable (stub provider) — set CITEVYN_LLM_PROVIDER + key to run")
        g = summary.get("groundedness", {})
        if g.get("cases_with_facts", 0) > 0 and g.get("grounded_fact_rate") is not None:
            print(
                f"Groundedness: fact-rate {g['grounded_fact_rate']:.3f} over "
                f"{g['cases_with_facts']} fact-bearing case(s); "
                f"under-grounded: {[u['case_id'] for u in g.get('under_grounded', [])]}"
            )
        injection_line = injection_summary_line(summary)
        if injection_line is not None:
            print(injection_line)
        # Print the echo oracle's COUNT unconditionally on a judged run, including when it
        # is 0. A silent oracle and a vacuous one look identical in a CI log otherwise —
        # and "0 echoes" only means something once you can see how many cases it compared.
        mt = summary.get("multi_turn", {})
        if summary.get("judge", {}).get("available"):
            print(
                f"Multi-turn echo: {len(mt.get('echoes', []))} echo(es) over "
                f"{mt.get('cases', 0)} multi-turn case(s)"
            )

    # Coverage warning, printed even under --quiet. A bounded run is still a PASS,
    # so the only thing standing between it and being mistaken for full coverage is
    # this line; suppressing it would make the cap silent, which is the failure mode
    # the bounding is explicitly not allowed to have.
    subset = summary["judge"].get("subset")
    if subset:
        print(
            f"\n!! JUDGED COVERAGE REDUCED: judged {subset['selected']}/"
            f"{subset['selected'] + subset['dropped']} cases (--judge-subset "
            f"{subset['limit']}). NOT judged: {', '.join(subset['dropped_ids'])}"
        )
        print("   Retrieval metrics above are unaffected (every case ran).")

    failures = gate_failures(summary)
    if failures:
        print("\nEVAL GATE FAILED:")
        for f in failures:
            print(redact(f"  - {f}", secrets))
        return 1
    if not args.quiet:
        print(f"\nEval gate passed. Report: {args.report}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
