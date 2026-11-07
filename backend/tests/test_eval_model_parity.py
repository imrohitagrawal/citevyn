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
    ("answer model", {"openrouter_model": "openai/gpt-4o-mini"}, "answer model"),
    ("embedding model", {"embedding_model": "openai/text-embedding-3-small"}, "embedding model"),
    ("embedding size", {"embedding_dim": 768}, "embedding size"),
    ("thinking level", {"openrouter_reasoning_effort": "low"}, "reasoning effort"),
    ("no thinking level", {"openrouter_reasoning_effort": None}, "reasoning effort"),
    ("temperature", {"llm_temperature": 0.9}, "temperature"),
    ("output cap", {"llm_max_tokens": 64}, "output cap"),
    ("thinking allowance", {"gemini_thinking_headroom_tokens": 1}, "thinking allowance"),
    ("fallback model", {"eval_fallback_model": "openai/gpt-4o"}, "fallback pass model"),
    ("no grader", {"eval_judge_model": None}, "grader"),
    ("grader shares a family", {"eval_judge_model": "openai/gpt-4o"}, "grader"),
    ("grader in the answer family", {"eval_judge_model": "google/gemini-2.5-flash"}, "grader"),
    ("unpriced grader", {"eval_judge_model": "mistralai/not-priced"}, "price book"),
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


def test_production_side_rules_fire(production: Settings, judged: Settings) -> None:
    """Red when: production's fallback sending a reasoning effort, or production
    leaving the gemini provider, goes unnoticed."""
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
    """Red when: the judged step stops passing ``--production-parity``, so drift
    from a step-level or ``$GITHUB_ENV`` override would go unchecked."""
    runs = [s.get("run", "") for s in _judged_job()["steps"]]
    eval_runs = [r for r in runs if "tests.eval.runner" in r]
    assert len(eval_runs) == 1
    assert "--production-parity" in eval_runs[0].split()
