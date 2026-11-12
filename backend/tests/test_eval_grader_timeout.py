"""The judged eval's grader gets its own timeout (#598 Step 2, Part A).

``CITEVYN_EVAL_JUDGE_TIMEOUT_SECONDS`` (``Settings.eval_judge_timeout_seconds``)
reaches ONLY the grader's OpenRouter client. Production's clients, and the judged
run's answer clients (main pass and fallback pass), keep production's 15 s. The
grader model and the grader timeout are set together or not at all: a grader
without its own timeout would silently inherit 15 s, and a timeout without a
grader is never used.

Written before the implementation (test designer). Until it lands these fail on
the missing field, the missing validator, the missing parity rule, the missing
report key and the missing CI env line. Everything here is offline: clients are
built, never called.

Each test names the change that turns it red.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.core import config as config_mod
from app.core.config import Settings
from app.cost.metered import MeteredLLMClient
from app.llm.factory import build_llm_client
from app.llm.fallback import FallbackLLMClient
from app.llm.openrouter import OpenRouterLLMClient
from tests.eval import runner as runner_mod
from tests.eval.parity import MAPPED, PREFIX, parity_failures, production_settings, settings_fields

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
WAIT_SCRIPT = REPO_ROOT / "scripts" / "wait_for_judged_eval.sh"
JUDGED_JOB = "answer-quality-eval"
ENV_NAME = "CITEVYN_EVAL_JUDGE_TIMEOUT_SECONDS"
FIELD = "eval_judge_timeout_seconds"

ANSWER = "gemini-3.6-flash"
FALLBACK = "openai/gpt-4o-mini"
JUDGE = "anthropic/claude-haiku-5.5"
GRADER_TIMEOUT = 60.0
PRODUCTION_TIMEOUT = 15.0
KEYS = {"gemini_api_key": "placeholder-not-a-key", "openrouter_api_key": "placeholder-not-a-key"}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CITEVYN_* variable (any case) from the test process may leak in."""
    for name in list(os.environ):
        if name.upper().startswith(PREFIX):
            monkeypatch.delenv(name, raising=False)


def _settings(**overrides: Any) -> Settings:
    """The CI judged job's shape: production's Gemini client, both keys, a grader."""
    base: dict[str, Any] = {
        "llm_provider": "gemini",
        **KEYS,
        "gemini_model": ANSWER,
        "eval_fallback_model": FALLBACK,
        "eval_judge_model": JUDGE,
        FIELD: GRADER_TIMEOUT,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _judged_job() -> dict[str, Any]:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"][JUDGED_JOB]


# --- the setting: refusals, invalid values, bounds ------------------------------


def test_a_grader_model_without_a_grader_timeout_is_refused(clean_env: None) -> None:
    """Red when: a grader model is accepted without its own timeout (the grader
    would silently inherit production's 15 s, the cause of #598's 22 timeouts)."""
    with pytest.raises(ValidationError, match=FIELD.upper() + "|" + FIELD + "|timeout"):
        _settings(**{FIELD: None})


def test_a_grader_timeout_without_a_grader_model_is_refused(clean_env: None) -> None:
    """Red when: a grader timeout is accepted with no grader model (the value is
    never used, so a typo in the model's name would go unnoticed)."""
    with pytest.raises(ValidationError, match=FIELD.upper() + "|" + FIELD + "|timeout"):
        _settings(eval_judge_model=None)


def test_both_set_and_both_unset_are_accepted(clean_env: None) -> None:
    """Partner of the two refusals. Red when: the pairing rule refuses a valid
    pair, or the field does not exist (AttributeError until it is added)."""
    both = _settings()
    assert both.eval_judge_model == JUDGE
    assert getattr(both, FIELD) == GRADER_TIMEOUT
    neither = _settings(eval_judge_model=None, **{FIELD: None})
    assert neither.eval_judge_model is None
    assert getattr(neither, FIELD) is None


def test_the_field_has_no_code_default(clean_env: None) -> None:
    """Red when: the field is missing, or gains a default (a default would hide a
    run that forgot to set it, which the pairing rule exists to catch)."""
    assert FIELD in Settings.model_fields
    assert Settings(_env_file=None).model_dump()[FIELD] is None  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["", "abc", "0", "-1", "nan", "inf", "1e309", "300.01"])
def test_an_invalid_grader_timeout_from_the_environment_is_refused(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Red when: an empty, non-numeric, zero, negative, NaN, infinite, overflowing
    or over-300 s value parses (the existing ``gt=0`` timeouts accept inf and
    1e309; this one must not). Set through the environment, as CI sets it."""
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_MODEL", JUDGE)
    monkeypatch.setenv(ENV_NAME, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(("value", "expected"), [("0.5", 0.5), ("60", 60.0), ("300", 300.0)])
def test_grader_timeouts_inside_the_bounds_are_accepted(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str, expected: float
) -> None:
    """Partner of the invalid values: the bounds are (0, 300], inclusive at 300.
    Red when: the upper bound is exclusive, too low, or the env name is wrong."""
    monkeypatch.setenv("CITEVYN_EVAL_JUDGE_MODEL", JUDGE)
    monkeypatch.setenv(ENV_NAME, value)
    assert getattr(Settings(_env_file=None), FIELD) == expected  # type: ignore[call-arg]


# --- where the value goes: only the grader --------------------------------------


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Build the run's REAL clients (nothing is called) and record each pass."""
    state: dict[str, Any] = {"calls": []}
    real_build = runner_mod.build_metered_llm_client

    async def _spy(
        cases, *, settings, postgres=False, answer_llm=None, judge_llm=None, expect_answerer=None
    ):  # type: ignore[no-untyped-def]
        state["calls"].append({"answer_llm": answer_llm, "judge_llm": judge_llm})
        return []

    monkeypatch.setattr(runner_mod, "build_metered_llm_client", real_build)
    monkeypatch.setattr(runner_mod, "_judge_cases", _spy)
    monkeypatch.setattr(runner_mod, "get_llm_client", lambda settings=None: object())
    return state


def _unwrap(client: Any) -> Any:
    """Through ``AttributingLLM`` and ``MeteredLLMClient`` to the provider client."""
    if isinstance(client, runner_mod.AttributingLLM):
        client = client._inner
    assert isinstance(client, MeteredLLMClient), type(client)
    return client.inner


def test_the_grader_timeout_reaches_the_real_openrouter_grader_client(
    clean_env: None, built: dict[str, Any]
) -> None:
    """Red when: ``_grader_settings`` is not used where the grader is built, so
    the real OpenRouter grader client keeps 15 s; or the grader gets another model."""
    asyncio.run(runner_mod.run_eval_async(settings=_settings()))
    grader = _unwrap(built["calls"][0]["judge_llm"])
    assert isinstance(grader, OpenRouterLLMClient)
    assert grader._model == JUDGE
    assert grader._timeout_seconds == GRADER_TIMEOUT


def test_the_answer_clients_keep_productions_timeouts(
    clean_env: None, built: dict[str, Any]
) -> None:
    """Red when: the grader timeout leaks into an answer client (the fallback pass
    or the main pass's Gemini + OpenRouter chain), so the answers are no longer
    produced under production's 15 s."""
    asyncio.run(runner_mod.run_eval_async(settings=_settings()))
    main_pass, fallback_pass = built["calls"]
    main = _unwrap(main_pass["answer_llm"])
    assert isinstance(main, FallbackLLMClient)
    assert main.primary._timeout_seconds == PRODUCTION_TIMEOUT  # type: ignore[attr-defined]
    assert main.secondary._timeout_seconds == PRODUCTION_TIMEOUT  # type: ignore[attr-defined]
    fallback = _unwrap(fallback_pass["answer_llm"])
    assert isinstance(fallback, OpenRouterLLMClient)
    assert fallback._model == FALLBACK
    assert fallback._timeout_seconds == PRODUCTION_TIMEOUT


def test_productions_fallback_client_is_unchanged_by_the_eval_setting(clean_env: None) -> None:
    """Red when: the eval-only setting is read by the production factory (the
    OpenRouter backstop behind Gemini would wait 60 s in production)."""
    client = build_llm_client(_settings())
    assert isinstance(client, FallbackLLMClient)
    assert client.secondary._timeout_seconds == PRODUCTION_TIMEOUT  # type: ignore[attr-defined]
    assert client.primary._timeout_seconds == PRODUCTION_TIMEOUT  # type: ignore[attr-defined]


def test_the_report_records_the_grader_timeout(clean_env: None, built: dict[str, Any]) -> None:
    """Red when: the report does not say how long the grader was allowed, so a
    run's timeouts cannot be read against the limit it ran under."""
    summary = asyncio.run(runner_mod.run_eval_async(settings=_settings()))
    assert summary["grader"]["timeout_seconds"] == GRADER_TIMEOUT
    assert summary["models"]["judge"] == JUDGE  # partner: the grader really ran


# --- parity: the rule, both sides -----------------------------------------------


@pytest.fixture
def production(clean_env: None) -> Settings:
    return production_settings()


@pytest.fixture
def judged(clean_env: None) -> Settings:
    fields = settings_fields(_judged_job()["env"]) | KEYS
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


def test_the_grader_timeout_is_an_eval_only_mapped_field() -> None:
    """Red when: the field is compared equal-by-default (the judged run, which
    must set it, could never pass parity) instead of under its own rule."""
    assert FIELD in MAPPED


def test_the_judged_job_sets_the_grader_timeout(judged: Settings) -> None:
    """Red when: ci.yml's judged job stops setting the grader timeout to 60, so the
    real run would refuse to start (or grade under 15 s)."""
    assert _judged_job()["env"][ENV_NAME] == "60"
    assert getattr(judged, FIELD) == GRADER_TIMEOUT


def test_production_setting_the_grader_timeout_fails_parity(
    production: Settings, judged: Settings
) -> None:
    """Red when: production setting the eval-only grader timeout goes unnoticed."""
    failures = parity_failures(judged, production.model_copy(update={FIELD: GRADER_TIMEOUT}))
    assert any("eval-only" in f for f in failures), failures


def test_a_judged_run_with_a_grader_but_no_grader_timeout_fails_parity(
    production: Settings, judged: Settings
) -> None:
    """Red when: parity lets a judged run with a grader go without its timeout
    (``model_copy`` skips validators, so only the parity rule can catch this)."""
    failures = parity_failures(judged.model_copy(update={FIELD: None}), production)
    assert any("timeout" in f for f in failures), failures


def test_the_real_ci_env_passes_parity_with_the_grader_timeout(
    production: Settings, judged: Settings
) -> None:
    """Partner of the two parity refusals: the job's own env, timeout included,
    passes. Red when: the new field is compared equal-by-default."""
    assert getattr(judged, FIELD) == GRADER_TIMEOUT
    assert parity_failures(judged, production) == []


# --- CI: the job limit and the release wait --------------------------------------


def test_the_judged_jobs_limit_fits_inside_the_release_wait() -> None:
    """Red when: ``timeout-minutes`` is raised so far that the release wait gives
    up first (it would report "no result" for a run that is still going).

    The 6 is the ``test`` job that runs before this one (5.2 min measured), rounded
    up. Lower bound, kept as an estimate and not pinned (REVISION 3): on the
    measured run the eval step took 25.0 min; with the grader at T = 60 s the
    worst case adds 22 x (T - 15) s for the calls that timed out and 22 x T s for
    the adversarial calls they skipped, so 25.0 + 16.5 + 22.0 = 63.5 min plus
    about 0.6 min overhead: ``timeout-minutes: 66``. Raising either again needs
    the wait script's ``TOTAL_TIMEOUT_SECONDS`` raised too.
    """
    limit = _judged_job()["timeout-minutes"]
    match = re.search(r"TOTAL_TIMEOUT_SECONDS-(\d+)", WAIT_SCRIPT.read_text(encoding="utf-8"))
    assert match, "the wait script no longer defaults TOTAL_TIMEOUT_SECONDS"
    wait_minutes = int(match.group(1)) / 60
    assert isinstance(limit, int) and limit > 0  # partner: the limit was really read
    assert limit + 6 <= wait_minutes, (limit, wait_minutes)


# --- a misconfigured run stops before anything is spent --------------------------


def _ci_env(monkeypatch: pytest.MonkeyPatch, *, drop: tuple[str, ...] = (), **extra: str) -> None:
    for name, value in _judged_job()["env"].items():
        monkeypatch.setenv(name, str(value))
    for field, value in KEYS.items():
        monkeypatch.setenv(f"{PREFIX}{field.upper()}", value)
    for name in drop:
        monkeypatch.delenv(name, raising=False)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)
    config_mod.get_settings.cache_clear()


@pytest.fixture
def no_run(monkeypatch: pytest.MonkeyPatch) -> Any:
    started: list[Any] = []

    async def _never(**kwargs: Any) -> dict[str, Any]:
        started.append(kwargs)
        raise AssertionError("the eval ran")

    monkeypatch.setattr(runner_mod, "run_eval_async", _never)
    yield started
    config_mod.get_settings.cache_clear()


@pytest.mark.parametrize(
    ("drop", "extra"),
    [
        ((ENV_NAME,), {}),  # a grader model, no grader timeout
        (("CITEVYN_EVAL_JUDGE_MODEL",), {ENV_NAME: "60"}),  # a timeout, no grader
        ((), {ENV_NAME: "0"}),  # an invalid timeout
    ],
    ids=["model-without-timeout", "timeout-without-model", "zero-timeout"],
)
def test_a_misconfigured_judged_run_stops_at_startup_before_the_paid_path(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    no_run: list[Any],
    drop: tuple[str, ...],
    extra: dict[str, str],
) -> None:
    """Red when: the CI command with a misconfigured grader timeout gets past
    settings loading (the eval would start and pay for answers before failing,
    or grade under the wrong limit). It must stop with a Settings ValidationError."""
    _ci_env(monkeypatch, drop=drop, **extra)
    with pytest.raises(ValidationError):
        runner_mod.main(["--production-parity", "--quiet"])
    assert no_run == []
