"""The README's HTTP API table must describe routes that exist (#217).

The README shipped `POST /v1/ask` and `GET /metrics` — neither of which has ever been
registered — plus a `/ready` that belongs to no app and a quickstart `curl` using the
wrong auth header, the wrong body field and the wrong URL. It is the most-read document
in the repo, so following it verbatim produced three consecutive 404s.

This is the third document caught carrying invented paths (`docs/DEMO_CHECKLIST.md` in
#168, `docs/API_SPEC.md` §13 in #212), which is what makes a check cheaper than a fourth
discovery. The approach mirrors ``test_demo_checklist_doc.py``: parse the doc, extract
route-shaped tokens, and assert each against the live ``app.openapi()`` schema — so the
application, not a second hand-maintained list, is the source of truth.

Scope is deliberately the §8 table only. Prose elsewhere in the README legitimately says
things like "there is no ``/metrics`` endpoint", and a whole-file scan would have to
re-derive the disclaimer machinery ``test_demo_checklist_doc.py`` already carries. The
table is the part a reader copies from.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.main import create_app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"

# A markdown table row: ``| `VERB  /path` | auth | purpose |``. The path may carry an
# optional ``[/{suffix}]`` group, the README's shorthand for "and its by-id variant".
_ROW_RE = re.compile(r"^\|\s*`(GET|POST|PUT|PATCH|DELETE)\s+(/[^`]*)`\s*\|", re.MULTILINE)


def _api_table() -> str:
    """The body of the §8 HTTP API table."""
    start = README.read_text(encoding="utf-8").index("## 8. HTTP API")
    section = README.read_text(encoding="utf-8")[start:]
    # Stop at the next top-level heading so §9 rows can never leak in.
    end = section.index("\n## ", 1)
    return section[:end]


def _expand(path: str) -> list[str]:
    """Expand the ``[/{suffix}]`` shorthand into the concrete paths it stands for."""
    match = re.search(r"\[(/[^\]]+)\]$", path)
    if not match:
        return [path]
    base = path[: match.start()]
    return [base, base + match.group(1)]


def _normalise(path: str) -> str:
    """Collapse path parameters so ``{index_version}`` matches ``{foo}``."""
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/")) or "/"


@pytest.fixture(scope="module")
def documented() -> list[tuple[str, str]]:
    """``(verb, path)`` pairs claimed by the README table."""
    return [
        (verb.lower(), path)
        for verb, raw in _ROW_RE.findall(_api_table())
        for path in _expand(raw.strip())
    ]


@pytest.fixture(scope="module")
def live() -> dict[str, set[str]]:
    """``{normalised path: {verbs}}`` actually registered by the app."""
    schema = create_app().openapi()["paths"]
    return {_normalise(path): set(ops) for path, ops in schema.items()}


def test_the_table_documents_exactly_the_operations_the_app_serves(
    documented: list[tuple[str, str]], live: dict[str, set[str]]
) -> None:
    """The table's completeness IS the invariant — asserted both directions.

    This subsumes the vacuous-pass guard: a regex that silently stopped matching rows
    fails here immediately, rather than only once coverage fell past some magic floor.
    A floor of "at least N" would have let most of the table drop out unnoticed, which
    is precisely the silent-loss-of-coverage failure this module exists to prevent.

    It also closes the direction the original check could not see: a NEW route added to
    the app without a README row now fails too, so the table cannot rot by omission the
    way it previously rotted by invention.
    """
    claimed = {(verb, _normalise(path)) for verb, path in documented}
    serves = {(verb, path) for path, verbs in live.items() for verb in verbs}
    undocumented = sorted(f"{v.upper()} {p}" for v, p in serves - claimed)
    invented = sorted(f"{v.upper()} {p}" for v, p in claimed - serves)
    assert not undocumented, f"routes the app serves but README §8 omits: {undocumented}"
    assert not invented, f"routes README §8 claims but the app does not serve: {invented}"


def test_every_route_in_the_readme_table_exists(
    documented: list[tuple[str, str]], live: dict[str, set[str]]
) -> None:
    """THE #217 regression: the table listed POST /v1/ask and GET /metrics."""
    missing = sorted({path for _, path in documented if _normalise(path) not in live})
    assert not missing, (
        f"README §8 documents paths that are not registered: {missing}. "
        "Remove them or implement them — do not let the README invent routes."
    )


def test_every_documented_route_uses_a_verb_the_app_serves(
    documented: list[tuple[str, str]], live: dict[str, set[str]]
) -> None:
    """A right path under a wrong verb is still a 405 for anyone following the docs."""
    wrong = sorted(
        f"{verb.upper()} {path}"
        for verb, path in documented
        if _normalise(path) in live and verb not in live[_normalise(path)]
    )
    assert not wrong, f"README §8 documents routes under verbs the app does not serve: {wrong}"


def test_the_phantom_paths_that_regressed_are_gone(documented: list[tuple[str, str]]) -> None:
    """Pin the specific instances so a parser refactor cannot quietly stop covering them.

    Asserts against the parsed ROWS, not the raw section text: §8 deliberately states in
    prose that these two paths do not exist, and a substring scan would fire on the very
    sentence that documents their absence.
    """
    claimed = {path for _, path in documented}
    for phantom in ("/v1/ask", "/metrics"):
        assert phantom not in claimed, f"README §8 still lists the phantom path {phantom}"


def test_the_ask_route_is_documented(
    documented: list[tuple[str, str]], live: dict[str, set[str]]
) -> None:
    """Removing the phantom must not leave the README with no way to ask a question.

    Deleting ``POST /v1/ask`` and stopping there would have satisfied every assertion
    above while making the API table *less* useful than the version that was wrong.
    """
    assert ("post", "/v1/sessions/{session_id}/messages") in documented
    assert "post" in live["/v1/sessions/{}/messages"]
