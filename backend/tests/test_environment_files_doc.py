"""docs/ENVIRONMENT_FILES.md names every env file the code reads, and holds no value.

WHY THIS FILE EXISTS
--------------------
Three local env files are read by three different launch paths (the backend run
from ``backend/``, Docker Compose, the Vite dev server), and nothing said which
was which: ``.env.example`` told operators to copy it to ``.env``, which the
backend never reads unless started from the repo root. The guide fixes that; this
file stops it going stale, and stops anyone pasting a real value into a page of a
PUBLIC repository.

The paths are derived from what the code reads, not hand-listed: the backend's
from ``Settings.model_config["env_file"]``, Compose's from every ``env_file:``
entry in ``docker-compose.yml``, and the templates from ``git ls-files``. Only
Vite's ``.env.local`` is a convention with no config line to read.

Which change turns each test red:
  * a new ``env_file:`` in compose, a renamed Settings env file, or a new
    ``*.env.example``, with no matching row in the guide;
  * a ``.gitignore`` change that stops ignoring one of the live files;
  * a ``NAME_KEY=value`` style assignment pasted into the guide.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUIDE = _REPO_ROOT / "docs" / "ENVIRONMENT_FILES.md"
_COMPOSE = _REPO_ROOT / "infra" / "docker" / "docker-compose.yml"

# KEY / SECRET / TOKEN / PASSWORD assigned something that is not empty, a
# placeholder in angle brackets, or a shell expansion.
_SECRET_ASSIGNMENT = re.compile(r"\b[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)\s*=\s*[^\s\"'$<]")


def _compose_env_files() -> set[str]:
    compose: dict[str, Any] = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    found: set[str] = set()
    services: dict[str, dict[str, Any]] = compose["services"]
    for service in services.values():
        entries: Any = service.get("env_file") or []
        for entry in [entries] if isinstance(entries, str) else entries:
            path = entry if isinstance(entry, str) else entry["path"]
            found.add((_COMPOSE.parent / path).resolve().relative_to(_REPO_ROOT).as_posix())
    return found


def _live_env_files() -> set[str]:
    backend = f"backend/{Settings.model_config.get('env_file')}"
    return {backend, "frontend/.env.local", *_compose_env_files()}


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


def test_the_derivations_find_the_files_at_all() -> None:
    # Partner to every test below: an empty set would make each of them vacuous.
    assert _compose_env_files() == {"infra/docker/.env"}
    assert "backend/.env" in _live_env_files()
    assert {
        ".env.example",
        "frontend/.env.example",
        "infra/docker/prod.env.example",
    } <= _templates()


@pytest.mark.parametrize("path", sorted(_live_env_files() | _templates()))
def test_the_guide_names_every_env_file(path: str) -> None:
    assert f"`{path}`" in _GUIDE.read_text(encoding="utf-8"), (
        f"{path} is read by the code (or is a tracked template) but docs/ENVIRONMENT_FILES.md "
        "does not say what reads it. Add a row to its table."
    )


@pytest.mark.parametrize("path", sorted(_live_env_files()))
def test_every_live_env_file_is_git_ignored(path: str) -> None:
    assert _git_ignored(path), f"{path} holds secrets and must stay git-ignored"


@pytest.mark.parametrize("path", sorted(_templates()))
def test_templates_are_not_ignored(path: str) -> None:
    # Partner: proves check-ignore can answer "no", so the test above can fail.
    assert not _git_ignored(path)


def test_the_guide_holds_no_secret_shaped_assignment() -> None:
    hits = [
        line
        for line in _GUIDE.read_text(encoding="utf-8").splitlines()
        if _SECRET_ASSIGNMENT.search(line)
    ]
    assert hits == [], f"docs/ENVIRONMENT_FILES.md looks like it holds a value: {hits}"


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("CITEVYN_OPENROUTER_API_KEY=sk-or-v1-abc", True),
        ("POSTGRES_PASSWORD=hunter2", True),
        ("grep '^CITEVYN_OPENROUTER_API_KEY=' backend/.env", False),
        ('K=$(grep "^X_KEY=" f); curl -H "Authorization: Bearer $K"', False),
        ("CITEVYN_ADMIN_API_KEY=<generate one>", False),
    ],
)
def test_the_secret_pattern_flags_values_and_not_names(line: str, flagged: bool) -> None:
    # Partner to the test above: the pattern must catch a real value, or "no hits"
    # proves nothing.
    assert bool(_SECRET_ASSIGNMENT.search(line)) is flagged
