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

WHAT THIS FILE CANNOT SEE. A database URL that is set but points at something
broken: those tests ERROR rather than skip, which is loud, so it is not this
guard's job. And the run's own pytest outcome — the sibling guard for the live
Playwright job reads the run's JSON ``stats``; the equivalent here would be a
new ``--junit-xml`` step in ``ci.yml`` plus a step that reads it, and a workflow
edit needs the owner's approval (``AGENTS.md``). The test-only form above is
what makes the job unable to execute zero tests, because the guard IS a test the
selection contains.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

# The env key ``tests/test_pg_integration.py``'s ``skipif`` reads, and the key
# ``ci.yml`` sets on ``jobs.postgres-migrations.env``.
PG_URL_ENV = "CITEVYN_PG_TEST_URL"

# The module whose entire test population the skip above governs.
PG_SUITE = "tests.test_pg_integration"

# The floor for that population. Measured at ``d4763d6``: 15 test functions, and
# ``pytest -m postgres`` reports exactly 15 skipped with the URL unset. A FLOOR,
# not a pin — pinning 15 would redden on every legitimate new Postgres test,
# which is a guard that punishes the thing it wants.
MIN_PG_TESTS = 10


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
    """GitHub Actions sets ``CI=true`` on every runner, for every event."""
    return os.environ.get("CI", "").strip().lower() == "true"


def _url_configured() -> bool:
    """The same two sources ``test_pg_integration.py``'s ``skipif`` consults."""
    from app.core.config import get_settings

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


def _resolved_skip_conditions(*, with_url: bool) -> list[bool | None]:
    """What the INTERPRETER resolved for ``test_pg_integration``'s marks.

    Imports the module in a SUBPROCESS and prints the evaluated ``skipif``
    conditions. A subprocess, not ``importlib.reload``, for three reasons: the
    condition is computed at import time and this session has already imported
    the module; ``get_settings`` is cached; and reloading a module pytest is
    holding collected items from is a side effect no assertion needs.

    ``cwd`` is a directory with no ``.env`` in it, because ``Settings`` declares
    ``env_file=".env"`` and resolves that against the CWD — a checkout carrying
    ``backend/.env`` would otherwise decide this test's outcome.
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
        cwd=str(BACKEND.parent),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, (
        f"the import probe failed (with_url={with_url}):\n{result.stdout}\n{result.stderr}"
    )
    marks = json.loads(result.stdout.strip().splitlines()[-1])
    assert any(m["name"] == "postgres" for m in marks), (
        f"{PG_SUITE} no longer carries `pytest.mark.postgres`, so `pytest -m "
        f"postgres` selects none of it: {marks}"
    )
    return [m["condition"] for m in marks if m["name"] == "skipif"]


def test_the_postgres_suite_skips_for_the_env_and_for_nothing_else() -> None:
    """``skipif(True)`` skips identically to ``skipif(no URL)`` and reads the same.

    The guard above only sees a MISSING url. A ``skipif`` condition that stopped
    depending on the environment — ``skipif(True)``, or a second unconditional
    mark added beside it — would skip all 15 tests with the URL present and
    correct, and the guard above would pass. So assert the condition FLIPS, which
    is the only thing that distinguishes the two.

    This records what the interpreter LOADED (it imports the module and prints
    the evaluated condition), not what the file says.

    Turns red if: the ``skipif`` condition stops reading ``CITEVYN_PG_TEST_URL``
    / ``Settings.pg_test_url``, or an unconditional skip mark is added to the
    module.
    """
    without = _resolved_skip_conditions(with_url=False)
    with_url = _resolved_skip_conditions(with_url=True)

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

    Turns red if: ``tests/test_pg_integration.py`` declares fewer than
    ``MIN_PG_TESTS`` test functions.
    """
    source = (BACKEND / "tests" / "test_pg_integration.py").read_text(encoding="utf-8")
    declared = [
        line for line in source.splitlines() if line.startswith(("def test_", "async def test_"))
    ]
    assert len(declared) >= MIN_PG_TESTS, (
        f"tests/test_pg_integration.py declares {len(declared)} test function(s), "
        f"below the floor of {MIN_PG_TESTS} (15 at d4763d6). The guards in this "
        "file assert that a populated suite really ran; over an empty suite they "
        "assert nothing."
    )
