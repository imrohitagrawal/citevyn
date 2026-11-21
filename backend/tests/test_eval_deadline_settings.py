"""The judged eval runner's deadline settings and their parity rule.

Written before the implementation (test designer). The contract for
``app/core/config.py``:

* ``eval_deadline_seconds: int | None`` (``CITEVYN_EVAL_DEADLINE_SECONDS``),
  ``ge=60, le=86400``, default ``None`` (no deadline: today's run).
* ``eval_grading_budget_seconds: int | None``
  (``CITEVYN_EVAL_GRADING_BUDGET_SECONDS``), ``ge=1, le=3600``, default ``None``.
* The deadline and the budget are set together or not at all, and the budget is
  less than the deadline. Settings checks ONLY the bounds, the pairing and
  budget < deadline: how many grader calls a panel makes is not a Settings fact
  (the panel size is read from ``CITEVYN_EVAL_JUDGE_PANEL``), so the runner, not
  Settings, refuses a budget too small for one panel
  (``test_eval_deadline.py``).
* ``eval_call_total_timeout_seconds: float | None``
  (``CITEVYN_EVAL_CALL_TOTAL_TIMEOUT_SECONDS``), ``gt=0, le=600``, default
  ``None``; the upper bound also refuses inf and nan.
* Parity (``tests/eval/parity.py``): all three are eval-only and join ``MAPPED``,
  so production must hold ``None`` for each.

Each test carries one ``RED WHEN`` line naming the change that turns it red.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from tests.eval.parity import MAPPED, PREFIX, parity_failures, production_settings

DEADLINE = "eval_deadline_seconds"
BUDGET = "eval_grading_budget_seconds"
CALL_TOTAL = "eval_call_total_timeout_seconds"
NEW_FIELDS = (DEADLINE, BUDGET, CALL_TOTAL)
DEADLINE_ENV = "CITEVYN_EVAL_DEADLINE_SECONDS"
BUDGET_ENV = "CITEVYN_EVAL_GRADING_BUDGET_SECONDS"
CALL_TOTAL_ENV = "CITEVYN_EVAL_CALL_TOTAL_TIMEOUT_SECONDS"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CITEVYN_* variable (any case) from the test process may leak in."""
    for name in list(os.environ):
        if name.upper().startswith(PREFIX):
            monkeypatch.delenv(name, raising=False)


def _settings(**fields: Any) -> Settings:
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


# --- defaults and environment names ----------------------------------------------------


def test_the_new_fields_exist_and_default_to_none(clean_env: None) -> None:
    # RED WHEN: a new field is missing or renamed, or defaults to anything but None.
    for name in NEW_FIELDS:
        assert name in Settings.model_fields, name
    loaded = _settings().model_dump()
    assert {name: loaded[name] for name in NEW_FIELDS} == dict.fromkeys(NEW_FIELDS)


def test_each_environment_name_reaches_settings(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RED WHEN: a field's CITEVYN_ environment name or alias changes.
    monkeypatch.setenv(DEADLINE_ENV, "137")
    monkeypatch.setenv(BUDGET_ENV, "61")
    monkeypatch.setenv(CALL_TOTAL_ENV, "2.5")
    loaded = _settings()
    assert getattr(loaded, DEADLINE) == 137
    assert getattr(loaded, BUDGET) == 61
    assert getattr(loaded, CALL_TOTAL) == 2.5


# --- the deadline and budget bounds --------------------------------------------------------


@pytest.mark.parametrize("value", [59, 0, -1, 86401, "", "inf", "nan", float("inf"), float("nan")])
def test_a_deadline_out_of_bounds_is_refused(clean_env: None, value: Any) -> None:
    # RED WHEN: the deadline's ge=60 / le=86400 bound or its int type is loosened.
    with pytest.raises(ValidationError):
        _settings(**{DEADLINE: value, BUDGET: 30})


@pytest.mark.parametrize("value", [0, -1, 3601, "", "inf", "nan", float("inf"), float("nan")])
def test_a_budget_out_of_bounds_is_refused(clean_env: None, value: Any) -> None:
    # RED WHEN: the budget's ge=1 / le=3600 bound or its int type is loosened.
    with pytest.raises(ValidationError):
        _settings(**{DEADLINE: 86400, BUDGET: value})


@pytest.mark.parametrize(("deadline", "budget"), [(60, 1), (60, 59), (86400, 3600), (137, 100)])
def test_bounds_and_pairs_inside_the_limits_load(
    clean_env: None, deadline: int, budget: int
) -> None:
    # RED WHEN: a bound is tightened past ge=60/le=86400 or ge=1/le=3600.
    loaded = _settings(**{DEADLINE: deadline, BUDGET: budget})
    assert (getattr(loaded, DEADLINE), getattr(loaded, BUDGET)) == (deadline, budget)


@pytest.mark.parametrize("fields", [{DEADLINE: 600}, {BUDGET: 300}], ids=["deadline", "budget"])
def test_the_deadline_and_budget_are_set_together_or_not_at_all(
    clean_env: None, fields: dict[str, Any]
) -> None:
    # RED WHEN: the both-or-neither pairing clause is deleted.
    with pytest.raises(ValidationError, match=DEADLINE_ENV) as info:
        _settings(**fields)
    assert BUDGET_ENV in str(info.value)


@pytest.mark.parametrize("budget", [600, 601, 3600])
def test_a_budget_at_or_above_the_deadline_is_refused(clean_env: None, budget: int) -> None:
    # RED WHEN: the budget < deadline check is removed or made <=.
    with pytest.raises(ValidationError):
        _settings(**{DEADLINE: 600, BUDGET: budget})


def test_a_budget_just_below_the_deadline_loads(clean_env: None) -> None:
    # RED WHEN: the budget < deadline check refuses deadline - 1 (off by one).
    assert getattr(_settings(**{DEADLINE: 600, BUDGET: 599}), BUDGET) == 599


def test_settings_does_not_check_the_budget_against_the_panel(clean_env: None) -> None:
    # RED WHEN: Settings refuses a budget below panel calls x timeout.
    loaded = _settings(
        eval_judge_model="anthropic/claude-haiku-5.5",
        eval_judge_timeout_seconds=60.0,
        **{DEADLINE: 600, BUDGET: 1},
    )
    assert getattr(loaded, BUDGET) == 1


# --- the per-call total ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [0, 0.0, -1, 600.01, "", "inf", "nan", float("inf"), float("nan"), 1e309]
)
def test_a_call_total_out_of_bounds_is_refused(clean_env: None, value: Any) -> None:
    # RED WHEN: the call total's gt=0 / le=600 bound is removed or loosened.
    with pytest.raises(ValidationError):
        _settings(**{CALL_TOTAL: value})


@pytest.mark.parametrize("value", [0.5, 120, 600])
def test_a_call_total_inside_the_bounds_loads_without_a_deadline(
    clean_env: None, value: float
) -> None:
    # RED WHEN: the call total is tied to the deadline pair or over-bounded.
    assert getattr(_settings(**{CALL_TOTAL: value}), CALL_TOTAL) == value


# --- parity -------------------------------------------------------------------------------


def test_the_new_fields_are_eval_only_in_parity() -> None:
    # RED WHEN: a new field is left out of parity's MAPPED.
    assert set(NEW_FIELDS) <= MAPPED, sorted(set(NEW_FIELDS) - MAPPED)


def test_production_holds_none_for_every_new_field(clean_env: None) -> None:
    # RED WHEN: fly.toml or a default gives production one of the new fields.
    production = production_settings()
    assert {name: getattr(production, name) for name in NEW_FIELDS} == dict.fromkeys(NEW_FIELDS)


@pytest.mark.parametrize(
    "fields",
    [{DEADLINE: 600, BUDGET: 300}, {CALL_TOTAL: 120.0}],
    ids=["deadline-pair", "call-total"],
)
def test_parity_fails_when_production_sets_a_new_field(
    clean_env: None, fields: dict[str, Any]
) -> None:
    # RED WHEN: parity's eval-only rule stops covering the new fields.
    production = production_settings()
    assert "production sets an eval-only setting" not in parity_failures(
        production, production
    )  # partner: production as it is passes this rule
    drifted = production.model_copy(update=fields)
    assert "production sets an eval-only setting" in parity_failures(production, drifted)
