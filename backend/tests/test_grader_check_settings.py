"""The grader check's switch and its parity rule (#598 Step 3).

Written before the implementation (test designer). The contract:

* ``Settings.eval_passage_grading: bool = False``, read from
  ``CITEVYN_EVAL_PASSAGE_GRADING``. Eval-only; off by default, so a run without
  it is exactly today's run.
* Parity (``tests/eval/parity.py``): the field is NOT in ``MAPPED`` (that rule
  needs production to hold ``None``). It has its own rule: production must hold
  the default (``False``); the judged run may hold either value. Any other
  setting is still compared as before.

The full contract of the grader check is in ``test_grader_check_runner.py``.
Each test names the change that turns it red.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.core.config import Settings
from tests.eval.parity import MAPPED, PREFIX, parity_failures, production_settings, settings_fields
from tests.test_eval_model_parity import CI_YML, JUDGED_JOB, KEYS

FIELD = "eval_passage_grading"
ENV_NAME = "CITEVYN_EVAL_PASSAGE_GRADING"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CITEVYN_* variable (any case) from the test process may leak in."""
    for name in list(os.environ):
        if name.upper().startswith(PREFIX):
            monkeypatch.delenv(name, raising=False)


def _judged_env() -> dict[str, Any]:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"][JUDGED_JOB]["env"]


@pytest.fixture
def production(clean_env: None) -> Settings:
    return production_settings()


@pytest.fixture
def judged(clean_env: None) -> Settings:
    fields = settings_fields(_judged_env()) | KEYS
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


# --- the setting ------------------------------------------------------------------


def test_the_switch_exists_and_is_off_by_default(clean_env: None) -> None:
    """Red when: the field is missing, or defaults to on (every run would pay for a
    second grading panel, and the experiment would run on release tags)."""
    assert FIELD in Settings.model_fields
    assert Settings(_env_file=None).model_dump()[FIELD] is False  # type: ignore[call-arg]


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("1", True), ("false", False)])
def test_the_switch_is_read_from_its_environment_name(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    """Red when: the environment name is not CITEVYN_EVAL_PASSAGE_GRADING, so CI
    could not turn the check on, or the value is not parsed as a boolean."""
    monkeypatch.setenv(ENV_NAME, value)
    assert getattr(Settings(_env_file=None), FIELD) is expected  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["", "maybe", "2"])
def test_a_malformed_switch_value_is_refused(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Red when: an empty or malformed value parses (an empty value is what an
    unset workflow expression renders, which is why CI must never put the switch
    in ``env:`` through an expression). Partner: the valid spellings above parse."""
    monkeypatch.setenv(ENV_NAME, value)
    assert FIELD in Settings.model_fields  # the refusal is the field's, not an unknown name
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


# --- parity -------------------------------------------------------------------------


def test_the_switch_is_not_an_eval_only_mapped_field() -> None:
    """Red when: the switch is put in ``MAPPED``, whose rule demands production
    hold ``None`` (a boolean default of False would then fail parity on every run)."""
    assert FIELD in Settings.model_fields
    assert FIELD not in MAPPED


@pytest.mark.parametrize("on", [True, False])
def test_the_judged_run_passes_parity_with_the_switch_in_either_position(
    production: Settings, judged: Settings, on: bool
) -> None:
    """Red when: parity compares the switch equal-by-default, so the grader-check
    run (switch on) could never start; or the field does not exist."""
    assert FIELD in Settings.model_fields
    assert getattr(production, FIELD) is False  # partner: production holds the default
    assert parity_failures(judged.model_copy(update={FIELD: on}), production) == []


def test_production_with_the_switch_on_fails_parity(production: Settings, judged: Settings) -> None:
    """Red when: production turning the experiment on goes unnoticed by parity."""
    assert FIELD in Settings.model_fields
    failures = parity_failures(judged, production.model_copy(update={FIELD: True}))
    assert any(FIELD in f or "passage" in f for f in failures), failures


def test_the_judged_jobs_env_block_does_not_set_the_switch() -> None:
    """Red when: ci.yml puts the switch in the judged job's ``env:`` block, where
    a tag run would carry it (or render it empty). It is exported by the eval
    step's script only when the ``grader_check`` input is true."""
    assert FIELD in Settings.model_fields
    assert ENV_NAME not in {str(k).upper() for k in _judged_env()}
    assert "CITEVYN_COST_HARD_DAILY_USD" not in {str(k).upper() for k in _judged_env()}
