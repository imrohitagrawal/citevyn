"""Does a judged run test the models production uses? (#596)

One set of rules, used twice:

* ``tests/test_eval_model_parity.py`` applies them to the judged CI job's
  ``env:`` block, a free early warning on every push;
* ``python -m tests.eval.runner --production-parity`` applies them to the
  runner's EFFECTIVE settings, before any paid call. That is the check that
  holds: it reads the environment the way the app does (pydantic-settings, any
  letter case, whatever a step or ``$GITHUB_ENV`` set), so no spelling of an
  override can get past it. A text check over the workflow could only list
  spellings, and each list had a hole.

Every Settings field must be equal, except the few in ``FREE_TO_DIFFER`` and
credentials (they cannot change an answer) and the ``MAPPED`` fields, each
compared by its own rule. So a setting added later is compared by default.

Production's configuration is ``fly.toml``'s ``[env]`` plus the Settings
defaults. The Fly secrets hold the keys and connection settings (Redis, OAuth);
none of those feeds an answer today, but a setting moved into a secret is
invisible here. ``environment`` is not compared: production runs as
"production", whose startup guards demand the secrets; no answer path reads it.

Production calls Google directly; the
judged run reaches the same models through OpenRouter, whose ids are the Google
ids under ``google/``.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.cost.pricing import price_for

REPO_ROOT = Path(__file__).resolve().parents[3]
FLY_TOML = REPO_ROOT / "fly.toml"
PREFIX = "CITEVYN_"


def settings_fields(env: dict[str, Any]) -> dict[str, Any]:
    """Map env names to Settings fields, ignoring letter case as pydantic-settings
    does, so a lower-case ``citevyn_x`` is read here exactly as the app reads it."""
    out: dict[str, Any] = {}
    for name, value in env.items():
        upper = str(name).upper()
        if not upper.startswith(PREFIX):
            continue
        field = upper[len(PREFIX) :].lower()
        if field in Settings.model_fields:
            if field in out:
                # Two names differing only in case: which one the app reads depends
                # on environment order, so neither side can be known. Refuse it.
                raise ValueError(f"{PREFIX}{field.upper()} is set twice, in different letter case")
            out[field] = value
    return out


def production_settings() -> Settings:
    """Production's model choices: ``fly.toml``'s ``[env]`` over the defaults.

    Built without the process environment or an env file, so nothing on the
    machine running the check can change what "production" means.
    """
    with FLY_TOML.open("rb") as handle:
        fields = settings_fields(tomllib.load(handle)["env"])
    # The production validators demand the secrets, which live in Fly. No model
    # field depends on the environment name.
    fields.pop("environment", None)
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.upper().startswith(PREFIX)}
    try:
        return Settings(_env_file=None, **fields)  # type: ignore[call-arg]
    finally:
        os.environ.update(saved)


def _family(model: str) -> str:
    return model.split("/", 1)[0]


# Fields that may differ between production and the judged run because they do
# not change an answer: where the data lives, who may call, how much may be spent,
# and credentials. Measured 2026-11-07: these and the mapped fields below were the
# only fields that differed. Every other Settings field must be EQUAL, so a new
# setting is compared by default, not forgotten.
FREE_TO_DIFFER = frozenset(
    {
        "database_url",
        "database_pool_size",
        "cors_allowed_origins",
        "cost_soft_daily_usd",
        "cost_hard_daily_usd",
    }
)
# Credentials are free to differ: a key selects an account, not a model.
_CREDENTIAL = re.compile(r"(_api_key|_secret|_token|_password)$")
# Fields compared by an explicit rule below instead of by equality.
MAPPED = frozenset(
    {
        "llm_provider",
        "embedding_provider",
        "openrouter_model",
        "embedding_model",
        "openrouter_reasoning_effort",
        "eval_fallback_model",
        "eval_judge_model",
    }
)


def _equal_by_default(name: str) -> bool:
    return name not in FREE_TO_DIFFER and name not in MAPPED and not _CREDENTIAL.search(name)


def parity_failures(judged: Settings, production: Settings) -> list[str]:
    """Every way ``judged`` would not test production's configuration. Empty = parity."""
    failures: list[str] = []

    def need(ok: bool, message: str) -> None:
        if not ok:
            failures.append(message)

    for name in sorted(Settings.model_fields):
        if _equal_by_default(name):
            mine, theirs = getattr(judged, name), getattr(production, name)
            need(mine == theirs, f"setting {name!r} is {mine!r}, production's is {theirs!r}")

    # The mapped fields: production calls Google directly; the judged run reaches
    # the same models through OpenRouter, whose ids are Google's under ``google/``.
    need(
        production.llm_provider == "gemini",
        f"production provider is {production.llm_provider!r}, not 'gemini'",
    )
    need(production.embedding_provider == "gemini", "production embeds without the gemini provider")
    need(
        judged.llm_provider == "router", f"judged provider is {judged.llm_provider!r}, not 'router'"
    )
    need(judged.embedding_provider == "openrouter", "judged run does not embed through OpenRouter")
    need(
        judged.openrouter_model == f"google/{production.gemini_model}",
        f"answer model {judged.openrouter_model!r} is not production's "
        f"'google/{production.gemini_model}'",
    )
    need(
        judged.embedding_model == f"google/{production.embedding_model}",
        f"embedding model {judged.embedding_model!r} is not production's "
        f"'google/{production.embedding_model}'",
    )
    need(
        production.gemini_thinking_level != "use_budget",
        "production uses a thinking budget, not a level",
    )
    # One judged field, ``openrouter_timeout_seconds``, serves the judged MAIN pass
    # (production's Gemini timeout) and the fallback pass (production's OpenRouter
    # timeout, compared by equality above). Production must use one value for both,
    # or the judged run cannot reproduce when production falls back.
    need(
        judged.openrouter_timeout_seconds == production.gemini_timeout_seconds,
        f"answer timeout {judged.openrouter_timeout_seconds!r} is not production's Gemini "
        f"timeout {production.gemini_timeout_seconds!r}",
    )
    need(
        judged.openrouter_reasoning_effort == production.gemini_thinking_level,
        f"reasoning effort {judged.openrouter_reasoning_effort!r} is not production's "
        f"thinking level {production.gemini_thinking_level!r}",
    )
    need(
        judged.eval_fallback_model == production.openrouter_model,
        f"fallback pass model {judged.eval_fallback_model!r} is not production's "
        f"fallback {production.openrouter_model!r}",
    )
    need(
        production.openrouter_reasoning_effort is None,
        "production's fallback would send a reasoning effort",
    )
    need(
        production.eval_fallback_model is None and production.eval_judge_model is None,
        "production sets an eval-only setting",
    )
    judge = judged.eval_judge_model
    answer_families = {_family(judged.openrouter_model)}
    if judged.eval_fallback_model:
        answer_families.add(_family(judged.eval_fallback_model))
    need(
        judge is not None and _family(judge) not in answer_families,
        f"grader {judge!r} is unset or shares a family with an answer model",
    )
    for role, model in (
        ("answer", judged.openrouter_model),
        ("fallback", judged.eval_fallback_model),
        ("grader", judge),
        ("embedding", judged.embedding_model),
    ):
        need(
            model is not None and price_for(provider="router", model=model) is not None,
            f"{role} model {model!r} is not in the price book",
        )
    return failures
