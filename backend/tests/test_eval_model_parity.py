"""The judged eval in CI must test the models production uses (#596).

Production's models come from ``fly.toml``'s ``[env]`` plus the Settings defaults
(the Fly secrets hold only keys). The judged eval's come from the
``answer-quality-eval`` job's ``env`` in ``ci.yml``. Production calls Google
directly; CI reaches the same models through OpenRouter, whose ids are the Google
ids under ``google/``.

Each test names the drift that turns it red.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import Settings
from app.cost.pricing import price_for

REPO_ROOT = Path(__file__).resolve().parents[2]
FLY_TOML = REPO_ROOT / "fly.toml"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JUDGED_JOB = "answer-quality-eval"
PREFIX = "CITEVYN_"


def _fields(env: dict[str, Any]) -> dict[str, Any]:
    """Map ``CITEVYN_X`` env names to Settings field names; drop non-Settings names."""
    out: dict[str, Any] = {}
    for name, value in env.items():
        if not name.startswith(PREFIX):
            continue
        field = name[len(PREFIX) :].lower()
        if field in Settings.model_fields:
            out[field] = value
    return out


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CITEVYN_* variable from the test process may leak into either side."""
    import os

    for name in list(os.environ):
        if name.startswith(PREFIX):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def production(clean_env: None) -> Settings:
    with FLY_TOML.open("rb") as handle:
        env = tomllib.load(handle)["env"]
    fields = _fields(env)
    # The production validators demand the secrets, which live in Fly, not here.
    # No model field depends on the environment name, so building it as
    # "development" reads the same model choices.
    fields.pop("environment", None)
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


@pytest.fixture
def judged(clean_env: None) -> Settings:
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    env = workflow["jobs"][JUDGED_JOB]["env"]
    fields = _fields(env)
    # The key is a secret expression in the workflow; any non-empty value parses.
    fields["openrouter_api_key"] = "placeholder-not-a-key"
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


def test_both_sides_were_read(production: Settings, judged: Settings) -> None:
    """Partner check: the files really set the providers this file compares.

    Red when: fly.toml stops naming the gemini provider, or the judged job stops
    naming the router provider (every other test here would compare defaults).
    """
    assert production.llm_provider == "gemini"
    assert production.embedding_provider == "gemini"
    assert judged.llm_provider == "router"
    assert judged.embedding_provider == "openrouter"


def test_judged_answer_model_is_productions(production: Settings, judged: Settings) -> None:
    """Red when: CI answers with any model other than production's Gemini model."""
    assert judged.openrouter_model == f"google/{production.gemini_model}"


def test_judged_embedding_model_is_productions(production: Settings, judged: Settings) -> None:
    """Red when: CI embeds with another model or another vector size than production."""
    assert judged.embedding_model == f"google/{production.embedding_model}"
    assert judged.embedding_dim == production.embedding_dim


def test_judged_thinking_matches_production(production: Settings, judged: Settings) -> None:
    """Red when: CI lets Gemini think at a different level than production does.

    Production sends Gemini a thinking level; through OpenRouter the same control is
    the reasoning effort, and the extra output allowance for thinking is shared.
    """
    assert production.gemini_thinking_level != "use_budget"
    assert judged.openrouter_reasoning_effort == production.gemini_thinking_level


def test_judged_generation_limits_match_production(production: Settings, judged: Settings) -> None:
    """Red when: CI answers with a different temperature or output cap."""
    assert judged.llm_temperature == production.llm_temperature
    assert judged.llm_max_tokens == production.llm_max_tokens
    assert judged.gemini_thinking_headroom_tokens == production.gemini_thinking_headroom_tokens


def test_judged_fallback_pass_uses_productions_fallback(
    production: Settings, judged: Settings
) -> None:
    """Red when: CI's second pass tests another model than production's backup, or
    production's backup starts receiving a reasoning setting the pass does not send."""
    assert judged.eval_fallback_model == production.openrouter_model
    assert production.openrouter_reasoning_effort is None


def test_judge_is_a_third_family(judged: Settings) -> None:
    """Red when: the grader is unset, or shares a model family with an answer model,
    so a model would grade its own answers."""
    assert judged.eval_judge_model
    assert judged.eval_fallback_model
    family = {m.split("/", 1)[0] for m in (judged.openrouter_model, judged.eval_fallback_model)}
    assert judged.eval_judge_model.split("/", 1)[0] not in family


def test_every_judged_model_is_priced(judged: Settings) -> None:
    """Red when: a model the judged run calls is missing from the price book, so its
    spend would be recorded as unpriced."""
    for model in (
        judged.openrouter_model,
        judged.eval_fallback_model,
        judged.eval_judge_model,
        judged.embedding_model,
    ):
        assert model
        assert price_for(provider="router", model=model) is not None, model
