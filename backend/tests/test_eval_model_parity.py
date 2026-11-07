"""The judged eval in CI must test the models production uses (#596).

The rules live in ``tests/eval/parity.py``. Here they are applied to the judged
job's ``env:`` block (a free early warning), each rule is shown to fire, and the
runtime check (``--production-parity``, run by the CI job before any paid call
on the runner's EFFECTIVE settings) is shown to catch overrides of any spelling.

Each test names the change that turns it red.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core import config as config_mod
from app.core.config import Settings
from tests.eval import runner as runner_mod
from tests.eval.parity import (
    PREFIX,
    parity_failures,
    production_settings,
    settings_fields,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JUDGED_JOB = "answer-quality-eval"


def _judged_job() -> dict[str, Any]:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"][JUDGED_JOB]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CITEVYN_* variable (any case) from the test process may leak in."""
    for name in list(os.environ):
        if name.upper().startswith(PREFIX):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def production(clean_env: None) -> Settings:
    return production_settings()


@pytest.fixture
def judged(clean_env: None) -> Settings:
    fields = settings_fields(_judged_job()["env"])
    # The key is a secret expression in the workflow; any non-empty value parses.
    fields["openrouter_api_key"] = "placeholder-not-a-key"
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


def test_both_sides_were_read(production: Settings, judged: Settings) -> None:
    """Partner check: the files really set the providers the rules compare.

    Red when: fly.toml stops naming the gemini provider, or the judged job stops
    naming the router provider (every other rule would compare defaults).
    """
    assert production.llm_provider == "gemini"
    assert production.embedding_provider == "gemini"
    assert judged.llm_provider == "router"
    assert judged.embedding_provider == "openrouter"


def test_the_judged_job_tests_productions_models(production: Settings, judged: Settings) -> None:
    """Red when: the judged job's env drifts from production on any rule in
    ``tests/eval/parity.py`` (answer or embedding model, vector size, thinking
    level, temperature, output cap, thinking allowance, fallback model, grader
    family, prices)."""
    assert parity_failures(judged, production) == []


DRIFTS: list[tuple[str, dict[str, Any], str]] = [
    ("answer model, other family", {"openrouter_model": "openai/gpt-4o-mini"}, "answer model"),
    ("answer model, same family", {"openrouter_model": "google/gemini-2.5-flash"}, "answer model"),
    (
        "embedding model, other",
        {"embedding_model": "openai/text-embedding-3-small"},
        "embedding model",
    ),
    (
        "embedding model, same family",
        {"embedding_model": "google/text-embedding-004"},
        "embedding model",
    ),
    ("judged embeds with gemini", {"embedding_provider": "gemini"}, "embed through OpenRouter"),
    ("embedding size", {"embedding_dim": 768}, "'embedding_dim'"),
    ("thinking level low", {"openrouter_reasoning_effort": "low"}, "reasoning effort"),
    ("thinking level medium", {"openrouter_reasoning_effort": "medium"}, "reasoning effort"),
    ("no thinking level", {"openrouter_reasoning_effort": None}, "reasoning effort"),
    ("temperature", {"llm_temperature": 0.9}, "'llm_temperature'"),
    ("output cap", {"llm_max_tokens": 64}, "'llm_max_tokens'"),
    (
        "thinking allowance",
        {"gemini_thinking_headroom_tokens": 1},
        "'gemini_thinking_headroom_tokens'",
    ),
    ("endpoint", {"openrouter_api_base": "https://other.example/api/v1"}, "'openrouter_api_base'"),
    ("retrieval depth", {"retrieval_top_k": 1}, "'retrieval_top_k'"),
    ("answer policy", {"answer_when_grounded": False}, "'answer_when_grounded'"),
    ("timeout", {"openrouter_timeout_seconds": 300.0}, "'openrouter_timeout_seconds'"),
    ("fallback model", {"eval_fallback_model": "openai/gpt-4o"}, "fallback pass model"),
    ("no grader", {"eval_judge_model": None}, "grader"),
    ("grader shares a family", {"eval_judge_model": "openai/gpt-4o"}, "grader"),
    ("grader in the answer family", {"eval_judge_model": "google/gemini-2.5-flash"}, "grader"),
    ("unpriced grader", {"eval_judge_model": "mistralai/not-priced"}, "grader model"),
    ("provider", {"llm_provider": "gemini"}, "judged provider"),
]


@pytest.mark.parametrize(("label", "update", "expect"), DRIFTS, ids=[d[0] for d in DRIFTS])
def test_each_rule_fires(
    production: Settings, judged: Settings, label: str, update: dict[str, Any], expect: str
) -> None:
    """Red when: any one rule stops comparing what it names (each drift alone
    must produce a failure that names it)."""
    failures = parity_failures(judged.model_copy(update=update), production)
    assert any(expect in f for f in failures), (label, failures)


@pytest.mark.parametrize("role", ["answer", "fallback", "embedding"])
def test_an_unpriced_model_in_any_role_fires(
    production: Settings, judged: Settings, role: str
) -> None:
    """Red when: the price check skips the answer, fallback or embedding model.
    Production is moved too, so only the price rule can object."""
    field = {
        "answer": "gemini_model",
        "fallback": "openrouter_model",
        "embedding": "embedding_model",
    }[role]
    judged_field = {
        "answer": "openrouter_model",
        "fallback": "eval_fallback_model",
        "embedding": "embedding_model",
    }[role]
    unpriced = "unpriced-model-x"
    prod = production.model_copy(update={field: unpriced})
    mine = f"google/{unpriced}" if role != "fallback" else unpriced
    failures = parity_failures(judged.model_copy(update={judged_field: mine}), prod)
    assert any(f"{role} model" in f and "price book" in f for f in failures), failures


def test_the_allowlist_is_exactly_what_differs_today(
    production: Settings, judged: Settings
) -> None:
    """Partner of the equal-by-default rule: every field that differs between
    production and the judged job today is allowed by name, and nothing on the
    allowlist is a field that could not differ. Red when: a field is added to the
    allowlist (or the mapped set) that does not differ, which would let a future
    difference through unexamined, or the job starts differing on a new field."""
    from tests.eval.parity import _CREDENTIAL, FREE_TO_DIFFER, MAPPED

    differing = {n for n in Settings.model_fields if getattr(judged, n) != getattr(production, n)}
    credentials = {n for n in differing if _CREDENTIAL.search(n)}
    assert differing - credentials == set(FREE_TO_DIFFER) | set(MAPPED)


@pytest.mark.parametrize(
    "update",
    [{"retrieval_top_k": 1}, {"openrouter_api_base": "https://other.example/api/v1"}],
)
def test_drift_on_productions_side_fires(
    production: Settings, judged: Settings, update: dict[str, Any]
) -> None:
    """Red when: the comparison reads only fields the judged side set, so a
    setting added later to fly.toml (production only) passes."""
    failures = parity_failures(judged, production.model_copy(update=update))
    (name,) = update
    assert any(f"'{name}'" in f for f in failures), failures


def test_productions_gemini_timeout_must_match_the_judged_answer_timeout(
    production: Settings, judged: Settings
) -> None:
    """Red when: production's main-model timeout changes and the judged run keeps
    its own (equal fields on different paths)."""
    failures = parity_failures(
        judged, production.model_copy(update={"gemini_timeout_seconds": 30.0})
    )
    assert any("answer timeout" in f for f in failures), failures


def test_case_colliding_names_are_refused() -> None:
    """Red when: two names differing only in case are both accepted, although which
    one the app reads depends on environment order."""
    with pytest.raises(ValueError, match="different letter case"):
        settings_fields({"CITEVYN_GEMINI_MODEL": "a", "citevyn_gemini_model": "b"})


def test_production_side_rules_fire(production: Settings, judged: Settings) -> None:
    """Red when: production's fallback sending a reasoning effort, production
    leaving the gemini provider or embedder, production using a thinking budget,
    or production setting an eval-only field goes unnoticed."""
    for update, expect in (
        ({"embedding_provider": "openrouter"}, "production embeds"),
        ({"gemini_thinking_level": "use_budget"}, "thinking budget"),
        ({"eval_judge_model": "anthropic/claude-haiku-5.5"}, "eval-only"),
    ):
        failures = parity_failures(judged, production.model_copy(update=update))
        assert any(expect in f for f in failures), (update, failures)
    failures = parity_failures(
        judged, production.model_copy(update={"openrouter_reasoning_effort": "minimal"})
    )
    assert any("production's fallback" in f for f in failures)
    failures = parity_failures(judged, production.model_copy(update={"llm_provider": "router"}))
    assert any("production provider" in f for f in failures)


def test_lower_case_names_are_read_like_the_app_reads_them() -> None:
    """Red when: a lower-case ``citevyn_`` name in fly.toml or the job env is
    skipped by the comparison although pydantic-settings applies it."""
    assert settings_fields({"citevyn_openrouter_model": "x/y"}) == {"openrouter_model": "x/y"}
    assert settings_fields({"OTHER_NAME": "1", "CITEVYN_NOT_A_FIELD": "1"}) == {}


def test_production_is_not_read_from_the_checking_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red when: a variable on the machine running the check changes what
    "production" means (it must come from fly.toml and the defaults only)."""
    monkeypatch.setenv("citevyn_gemini_model", "gemini-9-local")
    assert production_settings().gemini_model != "gemini-9-local"
    assert os.environ["citevyn_gemini_model"] == "gemini-9-local"  # noqa: SIM112 - lower case on purpose


# --- the runtime check: the runner's effective settings -------------------------


def _ci_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for name, value in _judged_job()["env"].items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("CITEVYN_OPENROUTER_API_KEY", "placeholder-not-a-key")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)
    config_mod.get_settings.cache_clear()


@pytest.fixture
def no_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Records whether the eval itself started (it must not, on drift)."""
    started: list[Any] = []

    async def _never(**kwargs: Any) -> dict[str, Any]:
        started.append(kwargs)
        raise AssertionError("the eval ran")

    monkeypatch.setattr(runner_mod, "run_eval_async", _never)
    yield started
    config_mod.get_settings.cache_clear()


@pytest.mark.parametrize(
    "override",
    [
        {"CITEVYN_OPENROUTER_MODEL": "openai/gpt-4o"},
        # Any letter case reaches the app, so it must reach the check.
        {"citevyn_openrouter_model": "openai/gpt-4o"},
        {"citevyn_eval_judge_model": "google/gemini-2.5-flash"},
        {"CITEVYN_OPENROUTER_REASONING_EFFORT": "high"},
        {"CITEVYN_LLM_MAX_TOKENS": "64"},
        # Settings that change answers without naming a model.
        {"CITEVYN_OPENROUTER_API_BASE": "https://other.example/api/v1"},
        {"citevyn_retrieval_top_k": "1"},
        {"CITEVYN_ANSWER_WHEN_GROUNDED": "false"},
        {"CITEVYN_CONVERSATION_MEMORY": "false"},
    ],
)
def test_the_runtime_check_refuses_drift_before_running(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    no_run: list[Any],
    capsys: pytest.CaptureFixture[str],
    override: dict[str, str],
) -> None:
    """Red when: an override set by any means a step can use (it ends up in the
    process environment, in any letter case) passes the runtime check, or the
    eval starts before the check."""
    _ci_env(monkeypatch, **override)
    assert runner_mod.main(["--production-parity", "--quiet"]) == 2
    assert no_run == []
    assert "PRODUCTION PARITY FAILED" in capsys.readouterr().out


def test_the_runtime_check_passes_the_real_ci_env(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, no_run: list[Any]
) -> None:
    """Partner of the above: the job's own env passes, and the eval then starts."""
    _ci_env(monkeypatch)
    with pytest.raises(AssertionError, match="the eval ran"):
        runner_mod.main(["--production-parity", "--quiet"])
    assert len(no_run) == 1


def test_the_ci_job_runs_the_runtime_check() -> None:
    """Red when: the judged step stops passing ``--production-parity``, or its exit
    code stops failing the step, so drift from any override would go unchecked."""
    steps = [s for s in _judged_job()["steps"] if "tests.eval.runner" in s.get("run", "")]
    assert len(steps) == 1
    # The whole command, exactly: anything appended (``|| true``, ``; ...``) or a
    # step-level ``continue-on-error`` would turn the check's exit 2 into a pass.
    assert steps[0]["run"].split() == [
        "uv",
        "run",
        "python",
        "-m",
        "tests.eval.runner",
        "--postgres",
        "--production-parity",
        "--report",
        "../artifacts/eval_report.json",
    ]
    assert "continue-on-error" not in steps[0]
    assert "continue-on-error" not in _judged_job()
    # A custom shell (``shell: true {0}`` exits 0 without running the script) or a
    # changed condition would skip the check as surely as ``|| true``.
    assert "shell" not in steps[0]
    assert steps[0].get("if") == "steps.guard.outputs.run == 'true'"
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    assert "defaults" not in workflow
    assert "defaults" not in _judged_job()
