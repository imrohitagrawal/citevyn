"""The Makefile loads ``backend/.env`` for the development commands, and only those.

``Settings`` reads no file (``test_settings_reads_no_env_file.py``), so a command
gets ``backend/.env`` only when the Makefile hands it over through
``scripts/with_env.py``. ``DEV_ENV_FILE`` (default ``backend/.env``) picks the file.
The default may be missing (CI and a fresh clone have none; ``make ci-smoke``
runs ``migrate`` in CI); a file named explicitly must exist. ``make test`` and
coverage load nothing, as in CI, and pass ``--no-env-file`` so a ``UV_ENV_FILE``
in the shell cannot slip one in.

These tests read ``make -n`` (what make would run), run the ``migrate`` recipe for
real against stand-in ``uv`` and ``alembic`` programs, and run ``scripts/smoke.sh``
only as far as its env-file check.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_WRAPPER = "scripts/with_env.py"

# Each development target, and the command(s) in it that must run through the
# wrapper. A target missing here is a target nobody checks.
_DEV_TARGETS: dict[str, list[str]] = {
    "api": ["uvicorn app.main:app"],
    "worker": ["citevyn-worker"],
    "eval": ["tests.eval.runner"],
    "golden": ["tests.golden.runner"],
    "golden-smoke": ["tests.golden.runner"],
    "migrate": ["alembic -c db/alembic.ini upgrade head"],
    "seed": ["db.seed.seed_users", "db.seed.seed_catalog"],
    "demo": [
        "alembic -c db/alembic.ini upgrade head",
        "db.seed.seed_users",
        "db.seed.seed_catalog",
    ],
    "test-pg": ["pytest -m postgres"],
}


def _make(
    *args: str, cwd: Path = _REPO, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    make = shutil.which("make")
    assert make, "these tests need GNU make on PATH"
    base = {
        k: v for k, v in os.environ.items() if k not in {"DEV_ENV_FILE", "MAKEFLAGS", "MAKELEVEL"}
    }
    return subprocess.run(
        [make, "--no-print-directory", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**base, **(env or {})},
        check=False,
        timeout=60,
    )


def _dry_run(target: str, *extra: str) -> str:
    result = _make("-n", "-f", str(_REPO / "Makefile"), target, *extra)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("target", sorted(_DEV_TARGETS))
def test_each_dev_target_runs_through_the_wrapper(target: str) -> None:
    # Red when: a development target stops going through scripts/with_env.py (it would
    # then read no file at all), or calls `uv run` without --no-env-file.
    lines = _dry_run(target).splitlines()
    for command in _DEV_TARGETS[target]:
        hits = [line for line in lines if command in line]
        assert hits, f"`make -n {target}` never runs {command!r}"
        for line in hits:
            assert _WRAPPER in line, f"`make {target}` runs {command!r} without the wrapper"
            assert "--no-env-file" in line, f"`make {target}` lets uv load an env file"


@pytest.mark.parametrize("target", ["test", "coverage"])
def test_the_test_suite_loads_no_env_file(target: str) -> None:
    # Red when: `make test`/`coverage` start loading backend/.env (the wrapper) or drop
    # --no-env-file, so local runs stop matching CI, which has no file.
    # `make -n` also prints the recipe's `@#` comment lines; only commands count.
    lines = [
        line for line in _dry_run(target).splitlines() if "uv run" in line and "pytest" in line
    ]
    assert lines, f"`make -n {target}` runs no pytest"
    for line in lines:
        assert _WRAPPER not in line
        assert "uv run --no-env-file pytest" in line


def test_an_explicit_file_that_is_missing_stops_make() -> None:
    # Red when: DEV_ENV_FILE=<missing> is quietly treated like the optional default.
    for target in ("migrate", "smoke"):
        result = _make("-n", target, "DEV_ENV_FILE=/nonexistent/wpe.env")
        assert result.returncode != 0, f"`make -n {target}` accepted a missing DEV_ENV_FILE"
        assert "DEV_ENV_FILE" in result.stderr


def test_an_explicit_file_is_passed_as_required(tmp_path: Path) -> None:
    # Red when: an explicit file is marked optional (a later deletion would go unnoticed).
    env_file = tmp_path / "chosen.env"
    env_file.write_text("A=1\n", encoding="utf-8")
    line = next(
        line
        for line in _dry_run("migrate", f"DEV_ENV_FILE={env_file}").splitlines()
        if _WRAPPER in line
    )
    assert str(env_file) in line
    assert "--optional" not in line


def test_the_default_file_is_optional_and_absolute(tmp_path: Path) -> None:
    # Red when: the default stops being optional (CI's `make ci-smoke` would fail on a
    # runner with no backend/.env), or is passed relative (targets that `cd backend`
    # would then look for backend/backend/.env).
    result = _make("-n", "-f", str(_REPO / "Makefile"), "migrate", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    line = next(line for line in result.stdout.splitlines() if _WRAPPER in line)
    assert f"--optional '{tmp_path.resolve() / 'backend' / '.env'}'" in line


def _stand_in_tools(root: Path) -> dict[str, str]:
    """A sandbox where the real migrate recipe runs without uv, a venv or a database.

    ``uv`` drops its own options and runs the rest with this interpreter (which has
    python-dotenv); ``alembic`` prints what it was given. The real wrapper is linked
    in, because the recipe names it relative to the folder make runs in.
    """
    (root / "scripts").mkdir()
    (root / "scripts" / "with_env.py").symlink_to(_REPO / _WRAPPER)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'while [ "$#" -gt 0 ] && [ "$1" != python ]; do shift; done\n'
        "shift\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    alembic = bin_dir / "alembic"
    alembic.write_text(
        "#!/bin/sh\n"
        'echo "alembic-ran db=$CITEVYN_DATABASE_URL from_file=${WPE_FROM_FILE:-unset}"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    alembic.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def test_migrate_runs_with_no_default_file(tmp_path: Path) -> None:
    # Red when: `make migrate` (and so CI's `make ci-smoke`) fails because backend/.env
    # is absent.
    env = _stand_in_tools(tmp_path)
    result = _make("-f", str(_REPO / "Makefile"), "migrate", cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "alembic-ran" in result.stdout
    assert "from_file=unset" in result.stdout
    assert "backend/.env" in result.stderr  # the one-line notice


def test_migrate_reads_the_file_and_its_own_db_url_wins(tmp_path: Path) -> None:
    # Red when: `make migrate` stops loading backend/.env, or the file's database URL
    # beats the one the Makefile sets inline (migrations would hit the wrong database).
    env = _stand_in_tools(tmp_path)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / ".env").write_text(
        "CITEVYN_DATABASE_URL=sqlite:///from-the-file\nWPE_FROM_FILE=loaded\n", encoding="utf-8"
    )
    result = _make(
        "-f",
        str(_REPO / "Makefile"),
        "migrate",
        "DB_URL=sqlite:///from-make",
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "db=sqlite:///from-make" in result.stdout
    assert "from_file=loaded" in result.stdout


def _smoke_uv_lines() -> list[str]:
    text = (_REPO / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    joined = text.replace("\\\n", " ")
    return [
        line
        for line in joined.splitlines()
        if "uv run" in line and not line.lstrip().startswith("#")
    ]


def test_smoke_runs_every_backend_step_through_the_wrapper() -> None:
    # Red when: a smoke.sh step (migrate, either seed, the server) runs `uv run`
    # without the wrapper, bringing back the "seed with one file, serve with another" mix.
    text = (_REPO / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    assert 'WITH_ENV="$REPO_ROOT/scripts/with_env.py"' in text
    lines = _smoke_uv_lines()
    assert len(lines) >= 4, lines  # migrate, seed users, seed catalog, uvicorn
    for line in lines:
        assert 'python "$WITH_ENV"' in line, line
        assert "--no-env-file" in line, line


def test_smoke_stops_on_an_explicit_file_that_is_missing() -> None:
    # Red when: smoke.sh runs (and starts docker) with an explicitly named file missing.
    result = subprocess.run(
        ["bash", str(_REPO / "scripts" / "smoke.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "DEV_ENV_FILE": "/nonexistent/wpe.env", "PATH": "/usr/bin:/bin"},
        check=False,
        timeout=60,
    )
    assert result.returncode != 0
    assert "/nonexistent/wpe.env" in result.stderr
    assert "missing required command" not in result.stderr
