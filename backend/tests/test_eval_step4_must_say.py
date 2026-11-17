"""``must_say`` oracles for the judged eval (#598 Step 4), reported and never gating.

Written before the implementation (test designer). The contract the builder writes to:

* ``EvalCase.must_say``: a tuple of entries, each a ``|``-separated list of
  alternatives (any one present satisfies the entry). Optional; ``()`` when absent.
  The loader rejects a refusal case that sets it, an empty list, a value that is not
  a list, and an entry that is not a non-empty string.
* It is matched by the groundedness matcher (``tests.eval.groundedness``: casefold,
  whitespace runs collapsed, word boundaries), whose ``normalize()`` is unchanged.
  So a contraction with a curly apostrophe needs its own alternative.
* ``JudgedCase.must_say_missing``: ``None`` when the case declares no ``must_say``,
  otherwise the entries (raw form, in order) the answer does not satisfy.
* Each pass's ``judge`` section carries ``must_say``:
  ``{"declared": <cases in the pass declaring it>, "missing": [{"case_id", "entries"}]}``,
  and ``pass_report_lines`` prints it with the words "not gating until #602 (Step 5)".
* ``must_say`` never gates: ``gate_failures``, the leak rule, ``expected_facts`` and
  the groundedness population are unchanged by it.
* It is a presence check, not semantic correctness (the negative controls below).

Free and hermetic: the golden file, the shipped sources and the SQLite catalog with
scripted clients. Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from app.core.config import Settings
from tests.eval import grader_check
from tests.eval import runner as runner_mod
from tests.eval.cases import EvalCase
from tests.eval.groundedness import fact_covered, missing_facts
from tests.eval.paths import REPO_ROOT
from tests.eval.shipped import shipped_chunk_texts
from tests.test_eval_report_fields import EXPECT, _golden, _Judge, _Model

NOT_GATING = "not gating until #602 (Step 5)"
REFUSAL_COPY = (
    "I couldn't find a grounded answer for that question. Try rephrasing with more specific "
    "terms, or ask about Claude, Claude Code, Codex, or the Gemini API."
)

# The rows that carry ``must_say`` (the plan's golden edit map, Revision 4 item 5):
# six with a gist edit and two with ``must_say`` only.
MUST_SAY_IDS = frozenset(
    {
        "claude_api_lit_envvar",
        "claude_api_par_raisecap",
        "followup_claude_api_raise",
        "followup_gemini_api_altkey",
        "multihop_auth_compare",
        "codex_par_engine",
        "codex_unprompted_evidence_phrase",
        "followup_codex_otherflags",
    }
)

# Step 3's recorded answers (run 44052833740), verbatim. Each must FAIL its row's
# ``must_say``: these are the wrong or incomplete answers the oracle exists for.
RECORDED_WRONG: dict[str, list[str]] = {
    "claude_api_lit_envvar": [
        "The environment variable that overrides the Claude API rate limit is "
        "`CLAUDE_API_RATE_LIMIT` [1].",
    ],
    "claude_api_par_raisecap": [
        "The `CLAUDE_API_RATE_LIMIT` environment variable allows self-serve customers to "
        "override the default rate limit of 50 requests per minute for the Claude API [1].",
    ],
    "followup_claude_api_raise": [
        "Self-serve customers can raise the rate limit for the Claude API by overriding the "
        "`CLAUDE_API_RATE_LIMIT` environment variable [1]. The default rate limit enforced by "
        "the API is 50 requests per minute [1].",
    ],
    "followup_gemini_api_altkey": [
        "No evidence in the provided source material shows another way to provide the Gemini "
        "API key for the Gemini API besides using the `x-goog-api-key` header [1]. The Gemini "
        "CLI accepts the key in a credentials file [1].",
    ],
    "multihop_auth_compare": [
        "The Gemini API requires passing an API key in the `x-goog-api-key` header on every "
        "request, and the Gemini CLI accepts the key in a credentials file [2]. The sources I "
        "found do not contain information about how the Claude API handles authentication.",
        REFUSAL_COPY,
    ],
    "codex_par_engine": [
        "Codex is powered by a model selected using the `--model` flag for code generation [1].",
        REFUSAL_COPY,
    ],
    "codex_unprompted_evidence_phrase": [
        "Codex supports selecting models for code generation using the `--model` flag. For a "
        "full list of models, you can run `codex --help` [1].",
        REFUSAL_COPY,
        grader_check.WRONG_ANSWER,
    ],
    "followup_codex_otherflags": [
        "The sources I found do not contain information on how to view the full list of "
        "available models in Codex [1].",
    ],
}

# At least two paraphrased CORRECT answers per row, with straight and curly
# apostrophes and contractions; each must PASS its row's ``must_say``.
CORRECT: dict[str, list[str]] = {
    "claude_api_lit_envvar": [
        "CLAUDE_API_RATE_LIMIT overrides only the SDK's client-side ceiling; the server-side "
        "organization limit still applies [1].",
        "Set CLAUDE_API_RATE_LIMIT: it changes the SDK’s client side ceiling, but it doesn’t "
        "lift the organization limit on the server side [1].",
    ],
    "claude_api_par_raisecap": [
        "The CLAUDE_API_RATE_LIMIT variable raises the client-side ceiling the SDK attempts "
        "(default 50 requests per minute), but the server-side organization limit still "
        "applies [1].",
        "You can't lift the organization limit yourself: CLAUDE_API_RATE_LIMIT only moves the "
        "SDK’s client side cap above the default 50 requests per minute [1].",
    ],
    "followup_claude_api_raise": [
        "Set CLAUDE_API_RATE_LIMIT to override the SDK's client-side ceiling; the server-side "
        "organization limit still applies [1].",
        "CLAUDE_API_RATE_LIMIT lifts the client side ceiling only — it doesn’t change the "
        "server side limit for your organization [1].",
    ],
    "followup_gemini_api_altkey": [
        "Yes. Besides the x-goog-api-key header, the Gemini CLI reads the key from a "
        "credentials file or the GEMINI_API_KEY environment variable [1].",
        "You can also set `GEMINI_API_KEY` in your environment, and the CLI’ll pick it up [1].",
    ],
    "multihop_auth_compare": [
        "The Gemini API takes the key in the x-goog-api-key header, while the Claude API "
        "expects it in the x-api-key header [1][2].",
        "Claude’s API uses x-api-key (the SDKs fall back to ANTHROPIC_API_KEY); Gemini's uses "
        "x-goog-api-key [1][2].",
    ],
    "codex_par_engine": [
        "Codex is powered by a large language model (LLM); the --model flag picks which "
        "model it uses [1].",
        "It’s driven by an LLM, and you choose the model with --model [1].",
    ],
    "codex_unprompted_evidence_phrase": [
        grader_check.CORRECT_ANSWER,
        "The Codex docs don’t list the supported models; the --model flag selects the model, "
        "and `codex --help` shows every flag.",
        "The documentation doesn't list which models Codex supports. Use --model to pick one.",
        "The Codex docs don't list models by name; you choose one with --model.",
    ],
    "followup_codex_otherflags": [
        "Run `codex --help` to see the full list of flags [1].",
        "Use codex --help — it prints every option Codex accepts, and that’s where the other "
        "flags are [1].",
    ],
}

# An entry that cannot be found in the shipped source is allowed only here: a gap
# phrase on a case whose shipped gold has no answer (``shipped_gold == []``). Keyed by
# case id, then the entry's index in that case's ``must_say``.
GROUNDABILITY_EXEMPT: dict[str, dict[int, str]] = {
    "codex_unprompted_evidence_phrase": {
        1: "a gap phrase: the shipped Codex text has no model list, so a correct answer "
        "says the documentation does not list them; no shipped sentence can say that",
    },
}


def _cases() -> dict[str, EvalCase]:
    return _golden()


def _must_say(case: EvalCase) -> tuple[str, ...]:
    assert "must_say" in {f.name for f in dataclasses.fields(EvalCase)}, (
        "EvalCase has no must_say field"
    )
    return tuple(case.must_say)  # type: ignore[attr-defined]


def _with_must_say(case: EvalCase, entries: tuple[str, ...], **changes: Any) -> EvalCase:
    _must_say(case)  # the field must exist first
    return dataclasses.replace(case, must_say=entries, **changes)  # type: ignore[call-arg]


def _base_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "x",
        "area": "codex",
        "kind": "literal",
        "question": "q?",
        "expected_source": "codex",
        "expected_gist": "g",
        "expect_no_answer": False,
    }
    row.update(overrides)
    return row


def _source_text(case: EvalCase) -> str:
    """The case's shipped source: its shipped gold passages, or every passage of its
    expected source(s) when its gold is empty."""
    texts = shipped_chunk_texts()
    gold = case.shipped_gold or ()
    if gold:
        keys = list(gold)
    else:
        sources = set(case.expected_sources or ()) | {case.expected_source or ""}
        keys = [k for k in texts if k.split("#", 1)[0] in sources]
    assert keys, f"{case.id}: no shipped text to check against"
    return " ".join(texts[k] for k in keys)


# --- the loader --------------------------------------------------------------------------


def test_the_loader_accepts_a_list_of_entries() -> None:
    """Partner of the refusals below. Red when: a valid ``must_say`` is not parsed into
    the case (or is parsed as something other than its entries, in order)."""
    case = EvalCase.from_dict(_base_row(must_say=["codex --help", "flag|flags"]), origin="t")
    assert _must_say(case) == ("codex --help", "flag|flags")
    assert _must_say(EvalCase.from_dict(_base_row(), origin="t")) == ()


@pytest.mark.parametrize(
    "value",
    [[], [""], ["   "], ["ok", ""], "codex --help", ["ok", 3], [["nested"]], {"a": 1}],
    ids=[
        "empty-list",
        "empty-string",
        "blank-string",
        "one-empty-entry",
        "not-a-list",
        "non-string-entry",
        "nested-list",
        "a-dict",
    ],
)
def test_the_loader_rejects_a_malformed_must_say(value: Any) -> None:
    """Red when: the loader stops validating ``must_say`` (an empty entry would be
    satisfied by every answer; a bare string would be split into letters)."""
    with pytest.raises(ValueError, match="must_say"):
        EvalCase.from_dict(_base_row(must_say=value), origin="t")


def test_the_loader_rejects_must_say_on_a_refusal() -> None:
    """Red when: a refusal case may declare ``must_say`` (there is no correct answer
    whose words it could require)."""
    row = _base_row(
        kind="refusal",
        expected_source=None,
        expect_no_answer=True,
        must_say=["anything"],
    )
    with pytest.raises(ValueError, match="must_say"):
        EvalCase.from_dict(row, origin="t")
    del row["must_say"]
    assert EvalCase.from_dict(row, origin="t").is_refusal  # partner: the row is otherwise valid


# --- the golden rows ---------------------------------------------------------------------


def test_exactly_the_planned_rows_declare_must_say() -> None:
    """Red when: a planned ``must_say`` row is missing (its oracle is silently gone),
    or a row gains one outside the reviewed edit map."""
    declared = {c.id for c in _cases().values() if _must_say(c)}
    assert declared == MUST_SAY_IDS


@pytest.mark.parametrize(
    ("case_id", "text"),
    [(cid, text) for cid, texts in RECORDED_WRONG.items() for text in texts],
    ids=[f"{cid}-{n}" for cid, texts in RECORDED_WRONG.items() for n in range(len(texts))],
)
def test_each_must_say_fails_on_the_recorded_wrong_answer(case_id: str, text: str) -> None:
    """Red when: an alternative is widened until Step 3's recorded wrong (or
    incomplete) answer satisfies it, so the oracle stops catching the case it was
    written for."""
    case = _cases()[case_id]
    entries = _must_say(case)
    assert entries, f"{case_id} declares no must_say"
    assert missing_facts(text, entries), f"{case_id}: the recorded wrong answer passes"


@pytest.mark.parametrize(
    ("case_id", "text"),
    [(cid, text) for cid, texts in CORRECT.items() for text in texts],
    ids=[f"{cid}-{n}" for cid, texts in CORRECT.items() for n in range(len(texts))],
)
def test_each_must_say_passes_a_paraphrased_correct_answer(case_id: str, text: str) -> None:
    """Red when: an entry is too narrow for a correct answer (a contraction, a curly
    apostrophe or a hyphen-free spelling the alternatives do not cover)."""
    case = _cases()[case_id]
    entries = _must_say(case)
    assert entries, f"{case_id} declares no must_say"
    assert missing_facts(text, entries) == [], f"{case_id}: a correct answer fails"


def test_every_must_say_row_has_two_correct_answers_and_a_recorded_wrong_one() -> None:
    """Partner of the two tests above. Red when: a ``must_say`` row has no recorded
    wrong answer or fewer than two correct answers here, so its oracle is unproven."""
    assert set(RECORDED_WRONG) == MUST_SAY_IDS
    assert set(CORRECT) == MUST_SAY_IDS
    assert all(len(texts) >= 2 for texts in CORRECT.values())
    apostrophes = "".join(t for texts in CORRECT.values() for t in texts)
    assert "’" in apostrophes and "'" in apostrophes


def test_x_goog_api_key_does_not_satisfy_x_api_key() -> None:
    """Red when: the Claude-header entry is matched as a substring, so the Gemini
    header alone satisfies it."""
    entries = _must_say(_cases()["multihop_auth_compare"])
    assert "x-api-key" in entries  # partner: the entry this checks exists
    assert missing_facts("Both use x-goog-api-key [1].", entries) == ["x-api-key"]


# Codex's negative controls (plan Revision 3 item 7): must_say checks presence, not
# meaning. Both of these are wrong answers and both PASS; the docs must say so.


def test_negative_control_a_wrong_probe_with_a_true_sentence_appended_passes() -> None:
    """Pins the blind spot. Red when: this wrong answer stops passing, which means the
    matcher became semantic and the docs' "presence check" wording is out of date."""
    entries = _must_say(_cases()["codex_unprompted_evidence_phrase"])
    assert missing_facts(grader_check.WRONG_ANSWER, entries)  # partner: wrong alone fails
    padded = grader_check.WRONG_ANSWER + " The documentation does not list the models."
    assert missing_facts(padded, entries) == []


def test_negative_control_swapped_headers_pass() -> None:
    """Pins the blind spot. Red when: swapped headers stop passing (see above)."""
    entries = _must_say(_cases()["multihop_auth_compare"])
    swapped = "Gemini uses the x-api-key header and Claude uses x-goog-api-key [1][2]."
    assert missing_facts(swapped, entries) == []


def test_the_docs_call_must_say_a_presence_check() -> None:
    """Red when: the eval README documents ``must_say`` without saying it is a presence
    check that gates nothing until Step 5, so a reader takes a pass for correctness."""
    readme = (REPO_ROOT / "tests" / "eval" / "README.md").read_text(encoding="utf-8")
    assert "must_say" in readme
    section = readme[readme.index("must_say") :]
    assert "presence check" in section
    assert "#602" in section


# --- groundability -------------------------------------------------------------------------


def test_every_must_say_entry_is_groundable_in_the_shipped_text() -> None:
    """Red when: an entry names something the shipped source cannot support (a correct
    answer made from the shipped passages could never satisfy it), outside the pinned
    gap-phrase exemptions."""
    checked = 0
    for case in _cases().values():
        for index, entry in enumerate(_must_say(case)):
            if index in GROUNDABILITY_EXEMPT.get(case.id, {}):
                continue
            checked += 1
            assert fact_covered(_source_text(case), entry), (case.id, entry)
    assert checked >= len(MUST_SAY_IDS)  # partner: the entries really were checked


def test_each_groundability_exemption_is_live() -> None:
    """Red when: an exemption goes stale: its case leaves the golden set, stops
    declaring ``must_say``, gains an answering shipped gold, or its entry becomes
    groundable (then it needs no exemption)."""
    cases = _cases()
    assert GROUNDABILITY_EXEMPT  # partner: there is an exemption to check
    for case_id, entries in GROUNDABILITY_EXEMPT.items():
        assert case_id in cases, case_id
        case = cases[case_id]
        declared = _must_say(case)
        assert declared, case_id
        assert case.shipped_gold == (), f"{case_id}: has an answering shipped gold"
        for index, reason in entries.items():
            assert reason.strip()
            assert index < len(declared), (case_id, index)
            assert not fact_covered(_source_text(case), declared[index]), (case_id, index)


# --- the runner: matcher, field, summary, print, and no gate -------------------------------


def _judge(
    monkeypatch: pytest.MonkeyPatch, cases: list[EvalCase], reply: Any
) -> list[runner_mod.JudgedCase]:
    """The REAL ``_judge_cases`` on the hermetic catalog, switch off."""
    monkeypatch.delenv("CITEVYN_EVAL_JUDGE_PANEL", raising=False)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: _Judge())
    return asyncio.run(
        runner_mod._judge_cases(
            cases,
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            answer_llm=runner_mod.AttributingLLM(_Model(reply)),  # type: ignore[arg-type]
            judge_llm=_Judge(),  # type: ignore[arg-type]
            expect_answerer=EXPECT,
        )
    )


def _missing(case: runner_mod.JudgedCase) -> Any:
    assert hasattr(case, "must_say_missing"), "JudgedCase has no must_say_missing"
    value = case.must_say_missing  # type: ignore[attr-defined]
    return None if value is None else list(value)


# Texts where a substring match, a case-sensitive match or a whitespace-sensitive match
# would differ from the groundedness matcher.
_MATCHER_PROBES = [
    ("50 requests", "The limit is 150 requests per minute [1]."),
    ("50 requests", "The limit is 50 requests per minute [1]."),
    ("x-api-key", "Gemini takes x-goog-api-key [1]."),
    ("large language model|LLM", "Codex is an llm-based agent [1]."),
    ("codex --help", "Run codex\n  --help for the flags [1]."),
    ("CLAUDE_API_RATE_LIMIT", "Set CLAUDE_API_RATE_LIMIT_V2 to raise it [1]."),
]


@pytest.mark.parametrize(("entry", "text"), _MATCHER_PROBES)
def test_the_runner_matches_must_say_with_the_groundedness_matcher(
    monkeypatch: pytest.MonkeyPatch, entry: str, text: str
) -> None:
    """Red when: ``must_say_missing`` is computed with any matcher other than the
    groundedness one (substring, case-sensitive, whitespace-sensitive), e.g. '150
    requests' satisfying '50 requests'."""
    base = _cases()["claude_api_lit_ratelimit"]
    case = _with_must_say(base, (entry,))
    (judged,) = _judge(monkeypatch, [case], text)
    assert judged.answer == text  # partner: the answer was served as written
    assert _missing(judged) == missing_facts(text, (entry,))


def test_150_requests_does_not_satisfy_50_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the runner's must_say match lets a digit-prefixed wrong number through."""
    case = _with_must_say(_cases()["claude_api_lit_ratelimit"], ("50 requests",))
    (wrong,) = _judge(monkeypatch, [case], "The limit is 150 requests per minute [1].")
    (right,) = _judge(monkeypatch, [case], "The limit is 50 requests per minute [1].")
    assert _missing(wrong) == ["50 requests"]
    assert _missing(right) == []


def test_a_case_without_must_say_reports_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: a case that declares nothing reports an empty list (it would read as
    "checked, nothing missing") instead of ``None``."""
    case = _with_must_say(_cases()["claude_api_lit_ratelimit"], ())
    (judged,) = _judge(monkeypatch, [case], "The limit is 50 requests per minute [1].")
    assert judged.score == 4  # partner: the case ran and was graded
    assert _missing(judged) is None


_ANSWERS = {
    "claude_api_lit_ratelimit": "The limit is 50 requests per minute [1].",
    "claude_api_lit_envvar": "Set CLAUDE_API_RATE_LIMIT to override the limit [1].",
    "codex_lit_modelflag": "The --model flag selects the model [1].",
}


def _reply(prompt: str) -> str:
    cases = _cases()
    for case_id, answer in _ANSWERS.items():
        if cases[case_id].question in prompt:
            return answer
    return "No marker."


def _isolation_cases(with_must_say: bool) -> list[EvalCase]:
    cases = _cases()
    entries = ("server-side|server side|organization limit", "client-side|client side")
    out = []
    for case_id in _ANSWERS:
        case = cases[case_id]
        out.append(_with_must_say(case, entries if with_must_say else ()))
    return out


def _gated_summary(cases: list[EvalCase], judged: list[Any]) -> dict[str, Any]:
    retrieval = {
        "answerable_total": len(cases),
        "hit_rate_by_kind": {"literal": 1.0},
        "overall_hit_rate": 1.0,
        "refusal_leaks": 0,
    }
    summary = runner_mod._summarize(cases, retrieval, judged, judge_available=True, judgeable=cases)
    # Groundedness gates on the live-retrieval run only; gate it here too.
    summary["embedder"] = {"mode": "postgres"}
    return summary


def test_must_say_misses_move_no_gate_and_no_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: ``must_say`` gates (a miss reaches ``gate_failures``), or it changes the
    leak rule, the groundedness population or the judge figures."""
    plain = _isolation_cases(False)
    marked = _isolation_cases(True)
    before = _gated_summary(plain, _judge(monkeypatch, plain, _reply))
    after = _gated_summary(marked, _judge(monkeypatch, marked, _reply))
    must_say = after["judge"].get("must_say")
    assert must_say, "the pass has no must_say summary"
    # Partner: every case really missed its must_say.
    assert {m["case_id"] for m in must_say["missing"]} == set(_ANSWERS)
    assert runner_mod.gate_failures(after) == runner_mod.gate_failures(before)
    for section in ("injection", "groundedness", "multi_turn"):
        assert after[section] == before[section], section
    for key in ("mean_score", "scored", "judged", "refusal_leaks_judged", "harness_errors"):
        assert after["judge"][key] == before["judge"][key], key


def test_each_pass_reports_must_say_declared_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the per-pass summary loses ``declared`` (a silent oracle and a passing
    one would look alike) or names a case that did not miss, or drops the entries."""
    entries = ("50 requests", "CLAUDE_API_RATE_LIMIT")
    cases = [_with_must_say(c, entries) for c in _isolation_cases(False)]
    judged = _judge(monkeypatch, cases, _reply)
    summary = runner_mod._summarize(cases, {}, judged, judge_available=True, judgeable=cases)
    must_say = summary["judge"]["must_say"]
    assert must_say["declared"] == 3
    missing = {m["case_id"]: list(m["entries"]) for m in must_say["missing"]}
    assert missing == {
        "claude_api_lit_ratelimit": ["CLAUDE_API_RATE_LIMIT"],
        "claude_api_lit_envvar": ["50 requests"],
        "codex_lit_modelflag": ["50 requests", "CLAUDE_API_RATE_LIMIT"],
    }


def test_the_pass_lines_print_must_say_as_not_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red when: the log does not show must_say, or shows it without saying it gates
    nothing until Step 5, or leaves out a case that missed."""
    cases = _isolation_cases(True)
    judged = _judge(monkeypatch, cases, _reply)
    summary = runner_mod._summarize(cases, {}, judged, judge_available=True, judgeable=cases)
    lines = runner_mod.pass_report_lines(summary["judge"])
    noted = [line for line in lines if "must_say" in line]
    assert noted, lines
    assert all(NOT_GATING in line for line in noted), noted
    for case_id in _ANSWERS:
        assert any(case_id in line for line in noted), case_id
