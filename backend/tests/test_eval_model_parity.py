"""The judged eval in CI must run production's configuration (#596).

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
    _CREDENTIAL,
    FREE_TO_DIFFER,
    MAPPED,
    PREFIX,
    parity_failures,
    production_settings,
    settings_fields,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JUDGED_JOB = "answer-quality-eval"
# The job reads both keys from secrets; any non-empty value stands in for them.
KEYS = {"gemini_api_key": "placeholder-not-a-key", "openrouter_api_key": "placeholder-not-a-key"}


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
    fields = settings_fields(_judged_job()["env"]) | KEYS
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


def test_both_sides_were_read(production: Settings, judged: Settings) -> None:
    """Partner check: the files really set what the rules compare.

    Red when: fly.toml stops naming the gemini provider, or the judged job stops
    naming it too, or stops setting the eval-only fields (every other rule would
    then compare defaults with defaults).
    """
    assert production.llm_provider == "gemini"
    assert production.embedding_provider == "gemini"
    assert judged.llm_provider == "gemini"
    assert judged.embedding_provider == "gemini"
    assert judged.eval_fallback_model
    assert judged.eval_judge_model


def test_the_job_reads_both_keys_from_secrets() -> None:
    """Red when: the judged job stops passing either key, so its client would not
    be production's Gemini client with the OpenRouter fallback."""
    env = _judged_job()["env"]
    assert env["CITEVYN_GEMINI_API_KEY"] == "${{ secrets.CITEVYN_GEMINI_API_KEY }}"
    assert env["CITEVYN_OPENROUTER_API_KEY"] == "${{ secrets.CITEVYN_OPENROUTER_API_KEY }}"


def test_the_judged_job_runs_productions_configuration(
    production: Settings, judged: Settings
) -> None:
    """Red when: the judged job's env drifts from production on any rule in
    ``tests/eval/parity.py``."""
    assert parity_failures(judged, production) == []


DRIFTS: list[tuple[str, dict[str, Any], str]] = [
    ("answer model", {"gemini_model": "gemini-2.5-flash"}, "'gemini_model'"),
    ("provider", {"llm_provider": "router"}, "'llm_provider'"),
    ("embedding provider", {"embedding_provider": "openrouter"}, "'embedding_provider'"),
    ("embedding model", {"embedding_model": "text-embedding-004"}, "'embedding_model'"),
    ("embedding size", {"embedding_dim": 768}, "'embedding_dim'"),
    ("thinking level", {"gemini_thinking_level": "low"}, "'gemini_thinking_level'"),
    (
        "thinking allowance",
        {"gemini_thinking_headroom_tokens": 1},
        "'gemini_thinking_headroom_tokens'",
    ),
    ("temperature", {"llm_temperature": 0.9}, "'llm_temperature'"),
    ("output cap", {"llm_max_tokens": 64}, "'llm_max_tokens'"),
    ("gemini endpoint", {"gemini_api_base": "https://other.example"}, "'gemini_api_base'"),
    (
        "fallback endpoint",
        {"openrouter_api_base": "https://other.example/v1"},
        "'openrouter_api_base'",
    ),
    ("gemini timeout", {"gemini_timeout_seconds": 30.0}, "'gemini_timeout_seconds'"),
    ("fallback timeout", {"openrouter_timeout_seconds": 30.0}, "'openrouter_timeout_seconds'"),
    ("production's fallback model", {"openrouter_model": "openai/gpt-4o"}, "'openrouter_model'"),
    ("retrieval depth", {"retrieval_top_k": 1}, "'retrieval_top_k'"),
    ("answer policy", {"answer_when_grounded": False}, "'answer_when_grounded'"),
    ("no Gemini key", {"gemini_api_key": None}, "no Gemini key"),
    ("no OpenRouter key", {"openrouter_api_key": None}, "no OpenRouter key"),
    ("fallback pass model", {"eval_fallback_model": "openai/gpt-4o"}, "fallback pass model"),
    ("no grader", {"eval_judge_model": None}, "grader"),
    ("grader in the fallback's family", {"eval_judge_model": "openai/gpt-4o"}, "grader"),
    ("grader in the answer's family", {"eval_judge_model": "google/gemini-2.5-flash"}, "grader"),
    ("unpriced grader", {"eval_judge_model": "mistralai/not-priced"}, "grader model"),
]


@pytest.mark.parametrize(("label", "update", "expect"), DRIFTS, ids=[d[0] for d in DRIFTS])
def test_each_rule_fires(
    production: Settings, judged: Settings, label: str, update: dict[str, Any], expect: str
) -> None:
    """Red when: any one rule stops comparing what it names (each drift alone
    must produce a failure that names it)."""
    failures = parity_failures(judged.model_copy(update=update), production)
    assert any(expect in f for f in failures), (label, failures)


def test_an_unpriced_fallback_model_fires(production: Settings, judged: Settings) -> None:
    """Red when: the price check skips the fallback model. Production is moved too,
    so only the price rule can object."""
    prod = production.model_copy(update={"openrouter_model": "unpriced/model-x"})
    mine = judged.model_copy(
        update={"openrouter_model": "unpriced/model-x", "eval_fallback_model": "unpriced/model-x"}
    )
    failures = parity_failures(mine, prod)
    assert any("fallback model" in f and "price book" in f for f in failures), failures


def test_the_allowlist_is_exactly_what_differs_today(
    production: Settings, judged: Settings
) -> None:
    """Partner of the equal-by-default rule. Red when: a field is put on the
    allowlist (or the eval-only set) that does not differ today, which would let a
    future difference through unexamined, or the job starts differing on a new
    field."""
    differing = {n for n in Settings.model_fields if getattr(judged, n) != getattr(production, n)}
    credentials = {n for n in differing if _CREDENTIAL.search(n)}
    assert differing - credentials == set(FREE_TO_DIFFER) | set(MAPPED)


@pytest.mark.parametrize(
    "update",
    [
        {"retrieval_top_k": 1},
        {"openrouter_api_base": "https://other.example/api/v1"},
        {"gemini_timeout_seconds": 30.0},
    ],
)
def test_drift_on_productions_side_fires(
    production: Settings, judged: Settings, update: dict[str, Any]
) -> None:
    """Red when: the comparison reads only fields the judged side set, so a
    setting added later to fly.toml (production only) passes."""
    failures = parity_failures(judged, production.model_copy(update=update))
    (name,) = update
    assert any(f"'{name}'" in f for f in failures), failures


def test_production_side_rules_fire(production: Settings, judged: Settings) -> None:
    """Red when: production leaving the gemini provider, or setting an eval-only
    field, goes unnoticed."""
    for update, expect in (
        ({"llm_provider": "router"}, "production provider"),
        ({"eval_judge_model": "anthropic/claude-haiku-5.5"}, "eval-only"),
    ):
        failures = parity_failures(judged, production.model_copy(update=update))
        assert any(expect in f for f in failures), (update, failures)


def test_case_colliding_names_are_refused() -> None:
    """Red when: two names differing only in case are both accepted, although which
    one the app reads depends on environment order."""
    with pytest.raises(ValueError, match="different letter case"):
        settings_fields({"CITEVYN_GEMINI_MODEL": "a", "citevyn_gemini_model": "b"})


def test_lower_case_names_are_read_like_the_app_reads_them() -> None:
    """Red when: a lower-case ``citevyn_`` name in fly.toml or the job env is
    skipped by the comparison although pydantic-settings applies it."""
    assert settings_fields({"citevyn_gemini_model": "x"}) == {"gemini_model": "x"}
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


def _ci_env(monkeypatch: pytest.MonkeyPatch, *, drop: tuple[str, ...] = (), **extra: str) -> None:
    for name, value in _judged_job()["env"].items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("CITEVYN_GEMINI_API_KEY", "placeholder-not-a-key")
    monkeypatch.setenv("CITEVYN_OPENROUTER_API_KEY", "placeholder-not-a-key")
    for name in drop:
        monkeypatch.delenv(name, raising=False)
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
    ("override", "drop"),
    [
        ({"CITEVYN_GEMINI_MODEL": "gemini-2.5-flash"}, ()),
        # Any letter case reaches the app, so it must reach the check.
        ({"citevyn_gemini_model": "gemini-2.5-flash"}, ()),
        ({"citevyn_eval_judge_model": "google/gemini-2.5-flash"}, ()),
        ({"CITEVYN_GEMINI_THINKING_LEVEL": "high"}, ()),
        ({"CITEVYN_LLM_TEMPERATURE": "0.9"}, ()),
        ({"CITEVYN_LLM_MAX_TOKENS": "64"}, ()),
        ({"CITEVYN_OPENROUTER_API_BASE": "https://other.example/api/v1"}, ()),
        ({"citevyn_retrieval_top_k": "1"}, ()),
        ({"CITEVYN_ANSWER_WHEN_GROUNDED": "false"}, ()),
        # A missing Gemini secret would make the factory answer with OpenRouter alone.
        ({}, ("CITEVYN_GEMINI_API_KEY",)),
    ],
)
def test_the_runtime_check_refuses_drift_before_running(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    no_run: list[Any],
    capsys: pytest.CaptureFixture[str],
    override: dict[str, str],
    drop: tuple[str, ...],
) -> None:
    """Red when: an override set by any means a step can use (it ends up in the
    process environment, in any letter case), or a missing key, passes the
    runtime check, or the eval starts before the check."""
    _ci_env(monkeypatch, drop=drop, **override)
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
