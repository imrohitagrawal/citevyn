"""docs/ENVIRONMENT_FILES.md has a row for every env file the code reads, and holds no value.

WHY THIS FILE EXISTS
--------------------
Several local env files are read by different commands (the Makefile's
development commands, Docker Compose, Vite), and nothing used to say which was
which: ``.env.example`` told operators to copy it to ``.env`` without saying which
``.env``. The guide fixes that; this file stops it going stale, and stops anyone
pasting a real value into a page of a PUBLIC repository.

The paths are derived from what the code reads where a config line exists: the
backend file from the Makefile's ``DEV_ENV_FILE_DEFAULT`` (``Settings`` reads
no file; the Makefile loads this one through ``scripts/with_env.py``), Compose's
from every ``env_file:`` in ``docker-compose.yml``, and the templates from
``git ls-files``. Only Vite's ``.env.local`` is a convention with no config line
to read.

Which change turns each test red:
  * a new ``env_file:`` in compose, a new ``DEV_ENV_FILE_DEFAULT`` in the Makefile,
    or a new ``*.env.example``, with no table row in the guide that starts with it;
  * a ``.gitignore`` change that stops ignoring one of the live files, one of the
    backup-style names (``.env~``, ``.env-old``, ``.envrc``), or the retired root
    ``.env``;
  * a ``NAME_KEY=value`` / ``NAME_KEY: "value"`` assignment, or a URL carrying a
    password, pasted into the guide.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUIDE = _REPO_ROOT / "docs" / "ENVIRONMENT_FILES.md"
_COMPOSE = _REPO_ROOT / "infra" / "docker" / "docker-compose.yml"
_MAKEFILE = _REPO_ROOT / "Makefile"

# KEY / SECRET / TOKEN / PASSWORD given a value with ``=`` or ``:``, quoted or
# not. Not flagged: nothing after it, an ``<angle placeholder>``, a ``$VAR``
# expansion, or a closing quote/bracket (``grep '^NAME='`` names, it does not set).
_SECRET_ASSIGNMENT = re.compile(
    r"\b[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)\s*[=:]\s*[\"']?[^\s\"'$<)]"
)
# scheme://user:password@host
_URL_CREDENTIAL = re.compile(r"://[^/\s:@]+:[^@\s/]+@")

# Names a backup or editor leaves behind, and the repo-root ``.env`` that
# ``make migrate``/``seed``/``demo`` used to read (retired, but an old checkout
# may still hold one). Each must be ignored like a live file.
_BACKUP_STYLE = ("backend/.env~", "backend/.env-old", "infra/docker/.env.bak", ".envrc", ".env")


def _compose_env_files() -> set[str]:
    compose: dict[str, Any] = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    services: dict[str, dict[str, Any]] = compose["services"]
    found: set[str] = set()
    for service in services.values():
        entries: Any = service.get("env_file") or []
        for entry in [entries] if isinstance(entries, str) else entries:
            path = entry if isinstance(entry, str) else entry["path"]
            found.add((_COMPOSE.parent / path).resolve().relative_to(_REPO_ROOT).as_posix())
    return found


def _dev_env_file() -> str:
    """The Makefile's ``DEV_ENV_FILE_DEFAULT := <path>``: what the development commands load."""
    found = re.findall(
        r"^DEV_ENV_FILE_DEFAULT\s*:=\s*(\S+)\s*$", _MAKEFILE.read_text(encoding="utf-8"), re.M
    )
    assert len(found) == 1, f"expected one `DEV_ENV_FILE_DEFAULT :=` line in the Makefile: {found}"
    return found[0]


def _live_env_files() -> set[str]:
    return {_dev_env_file(), "frontend/.env.local", *_compose_env_files()}


def _templates() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.env.example", "**/*.env.example", ".env.example"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return set(out)


def _git_ignored(path: str) -> bool:
    return (
        subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", path],
            cwd=_REPO_ROOT,
            check=False,
        ).returncode
        == 0
    )


def _table_rows() -> set[str]:
    """The first cell of every table row in the guide, without its backticks."""
    rows: set[str] = set()
    for line in _GUIDE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| `([^`]+)`", line)
        if m:
            rows.add(m.group(1).split(" ")[0])
    return rows


def test_the_derivations_find_the_files_at_all() -> None:
    # Partner to every test below: an empty set would make each of them vacuous.
    assert _compose_env_files() == {"infra/docker/.env"}
    assert {"backend/.env", "frontend/.env.local"} <= _live_env_files()
    assert {
        ".env.example",
        "frontend/.env.example",
        "infra/docker/prod.env.example",
    } <= _templates()
    assert len(_table_rows()) >= 7


@pytest.mark.parametrize("path", sorted(_live_env_files() | _templates()))
def test_the_guide_has_a_table_row_for_every_env_file(path: str) -> None:
    assert path in _table_rows(), (
        f"{path} is read by the code (or is a tracked template) but no row of the table in "
        "docs/ENVIRONMENT_FILES.md starts with it. Add one saying what reads it."
    )


@pytest.mark.parametrize("path", sorted(_live_env_files() | set(_BACKUP_STYLE)))
def test_every_live_or_backup_env_file_is_git_ignored(path: str) -> None:
    assert _git_ignored(path), f"{path} holds secrets and must stay git-ignored"


@pytest.mark.parametrize("path", sorted(_templates()))
def test_templates_are_not_ignored(path: str) -> None:
    # Partner: proves check-ignore can answer "no", so the test above can fail.
    assert not _git_ignored(path)


def test_the_guide_holds_no_value() -> None:
    hits = [
        line
        for line in _GUIDE.read_text(encoding="utf-8").splitlines()
        if _SECRET_ASSIGNMENT.search(line) or _URL_CREDENTIAL.search(line)
    ]
    assert hits == [], f"docs/ENVIRONMENT_FILES.md looks like it holds a value: {hits}"


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("CITEVYN_OPENROUTER_API_KEY=sk-or-v1-abc", True),
        ('CITEVYN_OPENROUTER_API_KEY="sk-or-v1-abc"', True),
        ("CITEVYN_OPENROUTER_API_KEY='sk-or-v1-abc'", True),
        ("CITEVYN_OPENROUTER_API_KEY: sk-or-v1-abc", True),
        ("POSTGRES_PASSWORD=hunter2", True),
        ("CITEVYN_DATABASE_URL=postgresql+psycopg://citevyn:Xy7pass@db.example/citevyn", True),
        ("rediss://default:AbCd123@eu1.example.com:6379", True),
        ("grep '^CITEVYN_OPENROUTER_API_KEY=' backend/.env", False),
        ('K=$(grep "^X_KEY=" f); curl -H "Authorization: Bearer $K"', False),
        ("CITEVYN_ADMIN_API_KEY=<generate one>", False),
        ("CITEVYN_ADMIN_API_KEY=", False),
        ("https://citevyn.example/legal/privacy", False),
    ],
)
def test_the_value_patterns_flag_values_and_not_names(line: str, flagged: bool) -> None:
    # Partner to the test above: the patterns must catch a real value, or "no
    # hits" proves nothing; and must pass the doc's own name-only commands.
    found = bool(_SECRET_ASSIGNMENT.search(line) or _URL_CREDENTIAL.search(line))
    assert found is flagged
