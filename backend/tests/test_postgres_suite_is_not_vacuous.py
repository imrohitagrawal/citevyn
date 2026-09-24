"""#449: ``pytest -m postgres`` must not report green having executed nothing.

THE DEFECT. ``ci.yml``'s ``postgres-migrations`` job — status-check context
``alembic + postgres integration tests``, a REQUIRED context on ``main`` — ends
with ``uv run pytest -m postgres -v``. There is no report and no outcome check.
Every test in ``tests/test_pg_integration.py`` sits under a module-level
``skipif`` on ``CITEVYN_PG_TEST_URL``, so deleting or renaming that one key on
``jobs.postgres-migrations.env`` turns the whole suite into skips and the job
still exits 0. Measured out of tree at ``d4763d6``::

    $ env -u CITEVYN_PG_TEST_URL .venv/bin/python -m pytest -m postgres -q
    15 skipped, 2556 deselected
    $ echo $?
    0

That takes every pgvector assertion with it, including the #370 keyword-arm
scenario and the ``NULLS LAST`` tiebreaker pin.

WHY NOT THE MECHANISM THE ISSUE PROPOSED. #449 suggested "a session-scoped
autouse check under the postgres marker". That cannot work, and the reason is
worth recording rather than re-discovering: a module-level ``skipif`` is
evaluated at COLLECTION time, and a skipped item never runs its fixtures —
autouse or otherwise, session-scoped or otherwise. Measured with a two-test
probe module carrying ``pytestmark = [pytest.mark.skipif(True, ...)]`` and a
session-scoped autouse fixture whose body raises: ``2 skipped in 0.05s``, the
fixture never ran.

WHAT IS HERE INSTEAD. A test that carries the ``postgres`` marker itself, so it
is inside the job's own ``-m postgres`` selection and cannot be skipped by
another module's marks. Under CI with no database it FAILS; on a laptop with no
database it passes, because ``make test-pg`` there is a developer asking what
would run, not a gate claiming anything.

THE JOB IS ALSO EXEMPT FROM THE DEFUSING RULE, as a whole job, in
``test_gating_workflows.py``'s ``_NOT_HELD_TO_THE_DEFUSING_RULE`` — so
``continue-on-error: true`` on its steps is uncaught there too. This file does
not close that; it closes the zero-tests hole. The YAML side (the env key must
stay on the job) is pinned by
``test_ci_workflow_conditions.py::test_the_postgres_job_hands_pytest_a_real_database``,
which fails in the FAST ``pytest + lint`` job on the PR that makes the edit.

WHAT THIS FILE CANNOT SEE, stated before the claim rather than after it. An
earlier version of this docstring said the test-only form makes the job "unable
to execute zero tests". That is too strong, and an adversarial review of #449
produced three edits that each give a green
``alembic + postgres integration tests`` context over zero executed Postgres
tests with every assertion here passing:

* the skip moved onto each of the 15 test FUNCTIONS instead of the module, or
  into a conftest as ``collect_ignore`` / a ``pytest_collection_modifyitems``
  that skips ``postgres`` items. The probe below reads the MODULE's
  ``pytestmark`` and is blind to all three;
* ``run: uv run pytest -m postgres -v || true``, or ``--ignore=`` /
  ``--deselect`` on that command. Pinned against in
  ``test_ci_workflow_conditions.py``, which is a YAML pin and so sees the file
  rather than the job's real command;
* ``continue-on-error: true`` on that job's OTHER steps. ``ci.yml:postgres-migrations``
  is exempt from the defusing rule as a WHOLE JOB in
  ``test_gating_workflows.py``'s ``_NOT_HELD_TO_THE_DEFUSING_RULE``; the pytest
  step specifically is now pinned in ``test_ci_workflow_conditions.py``, the
  job's other steps are not;
* a ``skipif`` TUNED TO THE PROBE. The probe below drives the module with a
  sentinel URL, so a condition keyed on that sentinel — say ``skipif("probe" not
  in os.environ.get("CITEVYN_PG_TEST_URL", ""))`` — resolves ``True`` unset and
  ``False`` with the probe's own URL, satisfying both of its assertions, while
  real CI (a URL with no ``probe`` in it) skips all 15 tests. Found by a
  verification round. Not closable by picking a different sentinel: any FIXED
  value can be keyed on. What would close it is the run-outcome check below.

What this file DOES close is the hole #449 filed: the job can no longer report
green because the DATABASE is absent, because the guard is a test the
``-m postgres`` selection contains. The mechanism that would close the rest is
the one the live Playwright job already has — read the RUN's own outcome
(``--junit-xml`` plus a step that asserts on it). That is a workflow edit, and
``AGENTS.md`` requires the owner's approval for one.

Also not this guard's job: a URL that is set but points at something broken.
Those tests ERROR rather than skip, which is loud.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import get_settings

BACKEND = Path(__file__).resolve().parents[1]

# The env key ``tests/test_pg_integration.py``'s ``skipif`` reads, and the key
# ``ci.yml`` sets on ``jobs.postgres-migrations.env``.
PG_URL_ENV = "CITEVYN_PG_TEST_URL"

# The module whose entire test population the skip above governs.
PG_SUITE = "tests.test_pg_integration"

# That population AS MEASURED: 15 test functions at ``d4763d6``, and
# ``pytest -m postgres`` reports exactly 15 skipped with the URL unset.
#
# Compared as a BASELINE that must not shrink, not as a round floor. An earlier
# version used 10, which meant a third of the suite could be deleted with the
# guard green — the same "a partner that barely partners" shape the coverage gate
# avoids by comparing `missed` against a recorded number rather than a
# percentage. Adding a Postgres test never reddens it; deliberately removing one
# is a one-line update here in the same change.
MEASURED_PG_TESTS = 15

# The marks ``tests/test_pg_integration.py`` may carry at MODULE level, by NAME.
# ``skip`` is deliberately NOT here: ``pytest.mark.skip`` skips all 15 tests on a
# fully provisioned CI job, and the earlier probe filtered the mark list to
# ``skipif`` only — so it was invisible, while this file claimed to catch "an
# unconditional skip mark". Found by an adversarial review of #449.
_ALLOWED_MODULE_MARKS = frozenset({"postgres", "skipif"})


def missing_database_is_a_failure(*, on_ci: bool, url_configured: bool) -> bool:
    """Should an absent Postgres URL FAIL the run rather than skip it?

    Only in CI. There the ``postgres-migrations`` job exists SOLELY to stand up
    that database, so every ``postgres``-marked test skipping means a required
    status context went green having executed nothing (#449). On a developer
    machine the same absence is ordinary — ``make test-pg`` without a database is
    a question, not a gate — so it stays a skip.

    Kept as a pure function of two booleans so the decision can be driven
    directly in both directions (``test_the_missing_database_rule_bites_only_in_ci``)
    rather than only observed in whichever environment happens to run the suite.
    """
    return on_ci and not url_configured


def _on_ci() -> bool:
    """Is this a CI runner?

    GitHub Actions sets ``CI=true`` by DEFAULT on every runner and every event — a
    default, not a guarantee. An ``env:`` block at workflow, job or step level can
    override it, and ``CI: "false"`` on ``jobs.postgres-migrations`` would turn the
    guard below into a permanent no-op. Nothing here can see that, and the YAML pin
    in ``test_ci_workflow_conditions.py`` does not read ``CI`` either.
    """
    return os.environ.get("CI", "").strip().lower() == "true"


def _url_configured() -> bool:
    """The same two sources ``test_pg_integration.py``'s ``skipif`` consults.

    ``is not None``, not truthiness, so ``CITEVYN_PG_TEST_URL=""`` counts as
    configured — matching ``test_pg_integration.py`` exactly, where an empty value
    lets the 15 tests RUN and fail loudly on connect rather than skip. The two
    sides agree because both spell it this way; if either moves to a truthiness
    check they desynchronise silently, and nothing asserts the pair.

    ``get_settings`` is resolved through this module's globals (imported at the
    top, not inside the body) so a test can substitute it. Without that, the
    "no URL anywhere" case would be decided by whether the checkout happens to
    carry a ``backend/.env`` setting ``pg_test_url`` — an environment reason, for
    nobody's mistake.
    """
    return os.environ.get(PG_URL_ENV) is not None or get_settings().pg_test_url is not None


@pytest.mark.parametrize(
    ("on_ci", "url_configured", "expected"),
    [
        # FIRST, and compliant: a laptop with no database. The absence is
        # ordinary here and must stay a skip, so a rule that simply returned
        # True would be caught by this case rather than looking correct.
        (False, False, False),
        (False, True, False),
        (True, True, False),
        # THE load-bearing cell: CI, no database. This is the silent-green run.
        (True, False, True),
    ],
)
def test_the_missing_database_rule_bites_only_in_ci(
    on_ci: bool, url_configured: bool, expected: bool
) -> None:
    """Drive the decision directly, in all four combinations.

    Turns red if: ``missing_database_is_a_failure`` starts ignoring either input
    — ``return False`` fails the (True, False) case, ``return not
    url_configured`` fails the (False, False) case, and ``return on_ci`` fails
    the (True, True) case.
    """
    assert missing_database_is_a_failure(on_ci=on_ci, url_configured=url_configured) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # FIRST is the compliant laptop case, so a reader that simply returned
        # True is caught here rather than looking right.
        (None, False),
        ("", False),
        ("false", False),
        ("0", False),
        ("true", True),
        ("TRUE", True),
        (" true ", True),
    ],
)
def test_the_ci_reader_reads_the_environment(
    value: str | None, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision function is pure; the thing that FEEDS it is not, and it had no test.

    This is the hole an adversarial review of #449 found, and it is the worst kind.
    Mutate ``_on_ci`` to ``return False`` and the four-cell truth table above still
    passes, the whole hermetic suite still passes, AND ``pytest -m postgres`` under
    ``CI=true`` with no database goes back to ``1 passed, 15 skipped`` — the guard
    is permanently vacuous and NO environment reveals it. Reproduced exactly that
    way before this test was written. Coverage is not assertion: ``_on_ci`` was
    executed by the guard on every run and asserted by nothing.

    Turns red if: ``_on_ci`` stops reading ``CI`` (``return False`` / ``return
    True`` each fail a case), or stops normalising it — dropping ``.lower()`` fails
    ``TRUE``, dropping ``.strip()`` fails ``" true "``.
    """
    if value is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", value)
    assert _on_ci() is expected


def test_the_url_reader_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same hole, other input: ``_url_configured`` had no test either.

    ``return True`` there makes ``missing_database_is_a_failure`` permanently
    False, so the guard passes in CI with no database — invisibly, exactly as with
    ``_on_ci``.

    Only the ENVIRONMENT half is driven here. ``Settings.pg_test_url`` is the other
    source and is read through a cached ``get_settings()``, so flipping it inside
    one process is not reliable; the subprocess probe below is what exercises that
    path, through ``test_pg_integration.py``'s own ``skipif`` over the same two
    sources.

    Turns red if: ``_url_configured`` stops reading ``CITEVYN_PG_TEST_URL`` —
    ``return True`` fails the unset case, ``return False`` fails the set case — or
    starts treating an empty value as unconfigured.
    """
    monkeypatch.delenv(PG_URL_ENV, raising=False)
    # Substituted, not trusted: `raising=True` (the default) means a rename of the
    # module-level import fails this test loudly instead of patching nothing and
    # letting a real `backend/.env` decide the outcome.
    monkeypatch.setattr(
        "tests.test_postgres_suite_is_not_vacuous.get_settings",
        lambda: SimpleNamespace(pg_test_url=None),
    )
    assert _url_configured() is False, (
        "with the env var unset and Settings.pg_test_url None, no URL is configured"
    )

    monkeypatch.setenv(PG_URL_ENV, "postgresql+psycopg://u:p@127.0.0.1:5432/never_contacted")
    assert _url_configured() is True

    # The EMPTY value is deliberately "configured" — see `_url_configured`'s
    # docstring. This is the pair's agreement with `test_pg_integration.py`,
    # asserted rather than assumed.
    monkeypatch.setenv(PG_URL_ENV, "")
    assert _url_configured() is True, (
        "an empty CITEVYN_PG_TEST_URL must count as configured, matching "
        f"{PG_SUITE}'s `is not None` skipif — there the 15 tests then RUN and fail "
        "loudly on connect, which is the outcome this guard wants"
    )


@pytest.mark.postgres
def test_the_postgres_job_is_not_running_zero_tests() -> None:
    """Carries the ``postgres`` marker, so the job's own selection contains it.

    OFF CI this assertion is satisfied by the benign cell, so it proves nothing
    on a laptop — ``test_the_missing_database_rule_bites_only_in_ci`` is what
    holds the rule itself, everywhere. This one reads the live environment, and
    the live environment is the half a laptop cannot supply.

    Turns red if: ``CITEVYN_PG_TEST_URL`` is dropped or renamed on ``ci.yml``'s
    ``jobs.postgres-migrations.env`` — the 15 tests in
    ``tests/test_pg_integration.py`` then all skip and, without this, ``pytest -m
    postgres`` exits 0 having asserted nothing.
    """
    assert not missing_database_is_a_failure(on_ci=_on_ci(), url_configured=_url_configured()), (
        f"CI=true but no Postgres URL is configured ({PG_URL_ENV} unset and "
        "Settings.pg_test_url is None), so every `postgres`-marked test in "
        f"{PG_SUITE} just SKIPPED and this job — the required status context "
        "`alembic + postgres integration tests` — would otherwise have reported "
        f"green having executed nothing. Restore {PG_URL_ENV} on ci.yml's "
        "jobs.postgres-migrations.env."
    )


def _resolved_module_marks(*, with_url: bool, cwd: Path) -> list[dict[str, object]]:
    """What the INTERPRETER resolved for ``test_pg_integration``'s marks.

    Imports the module in a SUBPROCESS and prints the evaluated ``skipif``
    conditions. A subprocess, not ``importlib.reload``, for three reasons: the
    condition is computed at import time and this session has already imported
    the module; ``get_settings`` is cached; and reloading a module pytest is
    holding collected items from is a side effect no assertion needs.

    ``cwd`` is the caller's ``tmp_path``. ``Settings`` declares ``env_file=".env"``
    and resolves it against the CWD, and running from the repo ROOT was not enough:
    ``.gitignore`` ignores ``.env`` at any level, so a dev box can legitimately
    carry a repo-root ``.env``, and if it set ``CITEVYN_PG_TEST_URL`` this test
    would red for nobody's mistake. A fresh ``tmp_path`` has no ``.env`` by
    construction rather than by assumption.

    Returns EVERY mark, not only the ``skipif`` ones — filtering to ``skipif`` is
    what made ``pytest.mark.skip`` invisible.
    """
    probe = (
        "import importlib, json\n"
        f"m = importlib.import_module({PG_SUITE!r})\n"
        "print(json.dumps([\n"
        "    {'name': mk.name,\n"
        "     'condition': bool(mk.args[0]) if mk.args else None}\n"
        "    for mk in m.pytestmark\n"
        "]))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != PG_URL_ENV}
    env["PYTHONPATH"] = str(BACKEND)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if with_url:
        env[PG_URL_ENV] = "postgresql+psycopg://probe:probe@127.0.0.1:5432/probe_not_contacted"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, (
        f"the import probe failed (with_url={with_url}). This is ALSO how a "
        "module-scope `pytest.skip(..., allow_module_level=True)` shows up — it "
        "raises on import, so the probe exits non-zero rather than reporting a "
        "mark. That form skips all 15 tests too, so a failure here is a real "
        f"signal even when the traceback is not about marks:\n{result.stdout}\n{result.stderr}"
    )
    marks: list[dict[str, object]] = json.loads(result.stdout.strip().splitlines()[-1])
    assert any(m["name"] == "postgres" for m in marks), (
        f"{PG_SUITE} no longer carries `pytest.mark.postgres`, so `pytest -m "
        f"postgres` selects none of it: {marks}"
    )
    return marks


def test_the_postgres_suite_skips_for_the_env_and_for_nothing_else(tmp_path: Path) -> None:
    """``skipif(True)`` skips identically to ``skipif(no URL)`` and reads the same.

    The guard above only sees a MISSING url. A ``skipif`` condition that stopped
    depending on the environment would skip all 15 tests with the URL present and
    correct, and that guard would pass. So assert the condition FLIPS — the only
    thing that distinguishes the two — and assert the module carries NO OTHER mark
    that can skip.

    The second half exists because the first was not enough. An adversarial review
    of #449 added ``pytest.mark.skip(reason="flaky")`` beside the ``skipif``: all 15
    tests skip on a fully provisioned CI job, the interpreter loading
    ``[('postgres', None), ('skip', None), ('skipif', False)]``, and the earlier
    version of this test filtered that list to ``skipif`` and never saw it — while
    claiming, in this very docstring, to catch "an unconditional skip mark". The
    mark NAMES are now compared against ``_ALLOWED_MODULE_MARKS`` as a set.

    This records what the interpreter LOADED (it imports the module in a subprocess
    and prints the evaluated marks), not what the file says.

    Turns red if: the ``skipif`` condition stops reading ``CITEVYN_PG_TEST_URL`` /
    ``Settings.pg_test_url``; the module gains ANY module-level mark other than
    ``postgres`` and ``skipif`` — ``skip``, ``xfail``, ``usefixtures`` included; or
    it gains a SECOND ``skipif``, even a legitimately conditional one on platform or
    Python version, which has to be argued for here because a second condition is
    also a second way for the whole suite to vanish.
    """
    without_marks = _resolved_module_marks(with_url=False, cwd=tmp_path)
    with_url_marks = _resolved_module_marks(with_url=True, cwd=tmp_path)

    for label, marks in (("unset", without_marks), ("set", with_url_marks)):
        unexpected = sorted({str(m["name"]) for m in marks} - _ALLOWED_MODULE_MARKS)
        assert not unexpected, (
            f"with {PG_URL_ENV} {label}, {PG_SUITE} carries module-level mark(s) "
            f"{unexpected} beyond {sorted(_ALLOWED_MODULE_MARKS)}. An unconditional "
            "`skip` (or `xfail`) skips all 15 tests on a fully provisioned CI job, "
            "and the environment guard above cannot see it. Full mark list: "
            f"{marks}"
        )

    without = [m["condition"] for m in without_marks if m["name"] == "skipif"]
    with_url = [m["condition"] for m in with_url_marks if m["name"] == "skipif"]

    assert without == [True], (
        f"with {PG_URL_ENV} unset, {PG_SUITE}'s skipif conditions resolved to "
        f"{without}; exactly one condition, True, is what makes the suite skip "
        "for the documented reason"
    )
    assert with_url == [False], (
        f"with {PG_URL_ENV} set, {PG_SUITE}'s skipif conditions resolved to "
        f"{with_url}. A condition that stays True with a database configured "
        "skips all of the suite while the job looks correctly provisioned — the "
        "#449 hole one level deeper."
    )


def test_the_postgres_suite_has_tests_to_skip_in_the_first_place() -> None:
    """Partner: the guards above are about a population, so it must exist.

    "15 skipped" is only alarming because there are 15 tests. If
    ``test_pg_integration.py`` were emptied, ``pytest -m postgres`` would report
    zero tests, ``missing_database_is_a_failure`` would still be satisfied by a
    provisioned CI job, and nothing would be measuring anything.

    It counts DECLARATIONS in the source, so it cannot see a test renamed to a
    non-``test_`` helper still called from a survivor, nor a per-function
    ``@pytest.mark.skip`` — both recorded in the module docstring above.

    Turns red if: ``tests/test_pg_integration.py`` declares fewer than
    ``MEASURED_PG_TESTS`` test functions.
    """
    source = (BACKEND / "tests" / "test_pg_integration.py").read_text(encoding="utf-8")
    declared = [
        line for line in source.splitlines() if line.startswith(("def test_", "async def test_"))
    ]
    assert len(declared) >= MEASURED_PG_TESTS, (
        f"tests/test_pg_integration.py declares {len(declared)} test function(s), "
        f"below the {MEASURED_PG_TESTS} measured at d4763d6. The guards in this "
        "file assert that a populated suite really ran; over an empty suite they "
        "assert nothing."
    )
