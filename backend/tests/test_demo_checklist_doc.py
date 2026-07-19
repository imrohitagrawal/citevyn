"""Guard ``docs/DEMO_CHECKLIST.md`` against route/port drift (#168).

The checklist is a *runbook*: a human types the commands in it, in order,
on demo day. That makes every route and port in it executable content, and
executable content rots exactly like code — #168 shipped a checklist whose
FIRST step (``curl``/browser at 5173) and whose first functional assertion
(``GET /healthz``) both pointed at things that do not exist. A checklist
that fails on its own first step trains people to skip it, which is worse
than having none.

So this module treats the doc as an artefact under test:

* every ``/v1/...`` and ``/health...`` path mentioned in a checklist item
  must exist in the live OpenAPI schema, *unless* the line explicitly
  marks it as not-yet-implemented (Slice 7 SSE, tracked as #61);
* the frontend port must agree with ``frontend/vite.config.ts``;
* the ports/headers/limits it quotes must agree with ``Settings``.

The parser is deliberately dumb (regex over the raw markdown). A smarter
one would need the doc to carry machine-readable annotations, and the
moment the doc has to be written for the test rather than for the human
reading it at 9am, the test has broken the thing it protects.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.core.config import Settings
from app.main import create_app

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECKLIST = REPO_ROOT / "docs" / "DEMO_CHECKLIST.md"
VITE_CONFIG = REPO_ROOT / "frontend" / "vite.config.ts"

# A path token in the doc: /health, /health/index, /v1/sessions/:id/messages.
# Trailing punctuation (``.``, ``,``, backtick) is excluded so "…/health."
# does not become a path named "/health.".
_PATH_RE = re.compile(r"/(?:v1|health)[A-Za-z0-9_/:{}.\-]*")

# Lines that document a route as *absent* rather than asserting it works.
# These are the deliberate "do not delete, mark it" entries: #168 asked for
# the SSE line to stay visible-but-flagged so nobody re-adds it as a live
# check, and the negative assertions ("there is no /healthz") must be
# allowed to name the route they are denying.
_ABSENCE_MARKERS = (
    "N/A as of",
    "there is no",
    "There is no",
    "No route exposes",
    "does not exist",
    "NOT YET WRITTEN",
)


def _checklist_text() -> str:
    return CHECKLIST.read_text(encoding="utf-8")


def _live_paths() -> set[str]:
    return set(create_app().openapi()["paths"])


def _normalise(path: str) -> str:
    """Collapse the doc's ``:id`` style onto OpenAPI's ``{param}`` style.

    The doc writes ``/v1/sessions/:id/messages`` because that is what a
    human reads; FastAPI publishes ``/v1/sessions/{session_id}/messages``.
    Both collapse to ``/v1/sessions/*/messages`` for comparison — we are
    checking that the *shape* of the route exists, not the parameter name.
    """
    path = path.rstrip("./,`")
    path = re.sub(r"\{[^}]*\}", "*", path)
    path = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "*", path)
    return path


def _items() -> list[tuple[int, str]]:
    """Split the doc into ``(start_line, text)`` checklist items.

    An absence marker ("there is no …") routinely lands on a *different*
    physical line from the route it disclaims, because the bullet wraps.
    Scoping the marker to one line therefore mis-flags a correctly
    disclaimed route, so we fold each bullet — a ``- [ ]`` line plus its
    indented continuations — into a single unit and judge that.

    The split is deliberately over-eager: any leading ``-`` opens a new
    item, so a shell continuation like ``-c "select …"`` inside a fenced
    block splits the bullet. That errs *strict* (a marker's scope stays
    narrow, so fewer routes are exempted from the exists-check) which is
    the safe direction for a guard.
    """
    items: list[tuple[int, list[str]]] = []
    for lineno, line in enumerate(_checklist_text().splitlines(), start=1):
        starts_item = line.lstrip().startswith(("- [", "* [", "-", "#"))
        if starts_item or not items:
            items.append((lineno, [line]))
        else:
            items[-1][1].append(line)
    return [(n, "\n".join(ls)) for n, ls in items]


def _documented_paths() -> list[tuple[int, str]]:
    """Return ``(line_number, path)`` for every *asserted* path in the doc."""
    out: list[tuple[int, str]] = []
    for lineno, item in _items():
        if any(marker in item for marker in _ABSENCE_MARKERS):
            continue
        for match in _PATH_RE.findall(item):
            out.append((lineno, match))
    return out


# ---------------------------------------------------------------------------
# Happy path: every route the checklist tells you to hit actually exists.
# ---------------------------------------------------------------------------


def test_every_documented_route_exists_in_the_openapi_schema() -> None:
    live = {_normalise(p) for p in _live_paths()}
    bad = [(lineno, path) for lineno, path in _documented_paths() if _normalise(path) not in live]
    assert not bad, (
        "docs/DEMO_CHECKLIST.md references routes that do not exist: "
        + ", ".join(f"line {n}: {p}" for n, p in bad)
        + f"\nlive routes: {sorted(live)}"
    )


def test_the_parser_actually_found_routes() -> None:
    """Vacuous-pass guard.

    If the doc is restructured such that ``_PATH_RE`` stops matching, the
    test above passes on an empty set and silently stops protecting
    anything — the exact failure mode that makes doc tests worthless.

    Counted over the raw text, *including* disclaimed bullets: the risk
    being guarded is the regex going blind, not the marker logic. (A
    bullet that disclaims a route — "there is no ``/healthz``" — is
    deliberately exempt from the exists-check, so the asserted subset is
    legitimately small and would make a poor canary.)
    """
    found = _PATH_RE.findall(_checklist_text())
    assert len(found) >= 8, f"expected the checklist to name several routes, got {found}"
    assert _documented_paths(), "no route is positively asserted any more"


# ---------------------------------------------------------------------------
# Regression: the three specific claims #168 filed.
# ---------------------------------------------------------------------------


def test_healthz_is_never_asserted_as_a_live_route() -> None:
    """#168 item 2. ``/healthz`` has never existed; the routes are /health*."""
    assert "/healthz" not in _live_paths()
    offenders = [
        (lineno, item)
        for lineno, item in _items()
        if "/healthz" in item and not any(m in item for m in _ABSENCE_MARKERS)
    ]
    assert not offenders, f"checklist asserts /healthz as live: {offenders}"


def test_frontend_port_matches_the_vite_dev_server() -> None:
    """#168 item 1. The doc said 5173; Vite binds 3000."""
    match = re.search(r"server:\s*\{[^}]*?port:\s*(\d+)", VITE_CONFIG.read_text(), re.S)
    assert match, "could not read the dev-server port out of frontend/vite.config.ts"
    port = match.group(1)

    text = _checklist_text()
    assert f"http://localhost:{port}" in text, (
        f"checklist should point the browser at the real dev port {port}"
    )
    # 5173 is Vite's *default*, which is precisely why the wrong number was
    # plausible enough to survive review. Keep it out entirely.
    assert "5173" not in text, "checklist still references Vite's default port 5173"


def test_sse_route_is_marked_not_implemented_rather_than_asserted() -> None:
    """#168 item 3. The streaming route is tracked as #61, not shipped."""
    live = {_normalise(p) for p in _live_paths()}
    assert "/v1/sessions/*/messages/stream" not in live, (
        "SSE shipped — re-enable the Slice 7 checklist box and delete this test"
    )
    text = _checklist_text()
    assert "messages/stream" in text, "the SSE line was deleted instead of flagged"
    assert "N/A as of" in text and "issues/61" in text


# ---------------------------------------------------------------------------
# Edge cases: numbers the doc quotes from Settings.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quoted", "actual"),
    [
        ("X-Request-ID", "request_id_header"),
    ],
)
def test_quoted_settings_values_match(quoted: str, actual: str) -> None:
    settings = Settings()
    assert getattr(settings, actual) == quoted
    assert quoted in _checklist_text()


def test_quoted_rate_limits_match_settings() -> None:
    settings = Settings()
    text = _checklist_text()
    # The doc says "the 31st request" — i.e. limit + 1.
    assert f"{settings.rate_limit_demo_user_per_hour + 1}st" in text
    assert str(settings.rate_limit_demo_user_per_hour) in text
    assert str(settings.rate_limit_admin_per_hour) in text
