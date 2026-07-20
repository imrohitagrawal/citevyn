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

# A path token in the doc: /health, /health/index, /v1/sessions/:id/messages,
# optionally preceded by the HTTP verb it is documented under.
#
# The verb is only captured when it *immediately* precedes the path — nothing
# between them but whitespace and markdown backticks. Prose that merely names
# a verb near a path ("there is no collection ``GET`` under
# ``/v1/sessions/:id/messages``") must NOT bind, because there the verb is the
# thing being denied, not the thing being asserted. Such a path falls back to
# the shape-only check.
#
# Trailing punctuation (``.``, ``,``, backtick) is excluded so "…/health."
# does not become a path named "/health.".
_PATH_RE = re.compile(
    r"(?:\b(GET|POST|PUT|PATCH|DELETE)\b[ \t`]*\n?[ \t`]*)?(/(?:v1|health)[A-Za-z0-9_/:{}.\-]*)"
)

# Phrases that mark a *sentence* as documenting something absent.
#
# These are the deliberate "do not delete, mark it" entries: #168 asked for
# the SSE line to stay visible-but-flagged so nobody re-adds it as a live
# check, and the negative assertions ("there is no /healthz") must be
# allowed to name the route they are denying.
#
# ``~~`` is in the list because a struck-through box is the doc's own way of
# saying "this check is disabled" (Slice 7), and the strike lands on the
# sentence holding the route while the prose explanation is the next
# sentence.
_ABSENCE_MARKERS = (
    "N/A as of",
    "there is no",
    "There is no",
    "No route exposes",
    "does not exist",
    "NOT YET WRITTEN",
    "~~",
)

# The routes the doc is permitted to name *negatively*, normalised.
#
# The exemption is scoped two ways — to the disclaiming *sentence*, and to
# these tokens. The first cut of this guard exempted the whole bullet, which
# meant one "there is no …" anywhere in a bullet blinded the exists-check for
# every other route it named — including the ``/health`` box that #168 was
# filed about, so the original defect (``curl http://localhost:8000/healthz``)
# could be restored verbatim with the suite still green. Token-scoping alone
# is not enough either: ``/healthz`` is legitimately named by the disclaimer
# in that same bullet, so only sentence-scoping distinguishes "the endpoint
# you curl" from "the endpoint we are telling you not to curl".
_DISCLAIMABLE_PATHS = frozenset(
    {
        "/healthz",  # #168 item 2: never existed, the routes are /health*
        "/v1/products",  # Slice 2: the catalog is asserted at the DB, not over HTTP
        "/v1/sessions/*/messages/stream",  # Slice 7 SSE, tracked as #61
    }
)


def _checklist_text() -> str:
    return CHECKLIST.read_text(encoding="utf-8")


def _live_paths() -> set[str]:
    return set(create_app().openapi()["paths"])


def _live_operations() -> dict[str, set[str]]:
    """``{normalised path: {verb, …}}`` for the live schema."""
    return {
        _normalise(path): {verb.lower() for verb in item}
        for path, item in create_app().openapi()["paths"].items()
    }


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


def _sentences(item: str) -> list[str]:
    """Split a folded bullet into sentence-ish segments.

    Sentence granularity is what makes the absence markers usable. A bullet
    wraps across lines, so a per-line scope mis-flags a correctly disclaimed
    route; a per-bullet scope is far too coarse (see ``_DISCLAIMABLE_PATHS``).
    The disclaimer and the thing it disclaims live in the same *sentence* —
    "(There is no ``/healthz`` route — the endpoints are …)" — while the
    command you are told to run is a different one.

    Splitting only on ``. `` (period + whitespace) keeps abbreviations and
    dotted identifiers like ``app.main:app`` intact.
    """
    return re.split(r"(?<=\.)\s+", item)


def _disclaims(segment: str) -> bool:
    return any(marker in segment for marker in _ABSENCE_MARKERS)


def _documented_paths() -> list[tuple[int, str, str]]:
    """``(line_number, verb, path)`` for every *asserted* route in the doc.

    ``verb`` is ``""`` when the doc names a path without an adjacent HTTP
    method (a ``curl`` URL, a prose reference) — those get a shape-only
    check.

    A bullet carrying an absence marker does not become invisible. The
    exemption applies only to a ``_DISCLAIMABLE_PATHS`` token appearing in
    the disclaiming *sentence*, so a bullet that says "there is no
    ``/healthz``" still has its ``/health``, ``/health/dependencies`` and
    ``/health/index`` verified — and a ``/healthz`` that reappears in the
    *command* half of the same bullet is still caught.
    """
    out: list[tuple[int, str, str]] = []
    for lineno, item in _items():
        for segment in _sentences(item):
            disclaims = _disclaims(segment)
            for verb, path in _PATH_RE.findall(segment):
                if disclaims and _normalise(path) in _DISCLAIMABLE_PATHS:
                    continue
                out.append((lineno, verb, path))
    return out


# ---------------------------------------------------------------------------
# Happy path: every route the checklist tells you to hit actually exists.
# ---------------------------------------------------------------------------


def test_every_documented_route_exists_in_the_openapi_schema() -> None:
    live = _live_operations()
    bad = [
        (lineno, verb, path)
        for lineno, verb, path in _documented_paths()
        if _normalise(path) not in live
    ]
    assert not bad, (
        "docs/DEMO_CHECKLIST.md references routes that do not exist: "
        + ", ".join(f"line {n}: {v} {p}".strip() for n, v, p in bad)
        + f"\nlive routes: {sorted(live)}"
    )


def test_every_documented_route_is_documented_under_a_live_verb() -> None:
    """Shape alone is not enough — the *method* has to exist too.

    #168 item 3 was verb drift, not path drift: the doc asserted a
    collection ``GET /v1/sessions/:id/messages`` against a path that is
    ``POST``-only. A shape-only guard passes that happily, so it cannot
    catch the class of bug it was written for.
    """
    live = _live_operations()
    bad = [
        (lineno, verb, path)
        for lineno, verb, path in _documented_paths()
        if verb and verb.lower() not in live.get(_normalise(path), set())
    ]
    assert not bad, (
        "docs/DEMO_CHECKLIST.md documents routes under a method they do not support: "
        + ", ".join(
            f"line {n}: {v} {p} (live: {sorted(live.get(_normalise(p), ()))})" for n, v, p in bad
        )
    )


def test_the_parser_actually_found_routes() -> None:
    """Vacuous-pass guard.

    If the doc is restructured such that ``_PATH_RE`` stops matching, the
    tests above pass on an empty set and silently stop protecting
    anything — the exact failure mode that makes doc tests worthless.
    """
    found = _PATH_RE.findall(_checklist_text())
    assert len(found) >= 8, f"expected the checklist to name several routes, got {found}"
    assert _documented_paths(), "no route is positively asserted any more"


def test_the_health_boxes_are_actually_covered_by_the_exists_check() -> None:
    """The exemption must be per-route, not per-bullet.

    Both boxes #168 was filed about — the §1 pre-flight ``curl`` and the
    Slice 1 assertion — sit in bullets that *also* say "there is no
    ``/healthz``". The first cut of this guard therefore skipped them
    wholesale, and the original defect could be pasted straight back in
    with the suite green. These are the two entries that must survive
    the disclaimer.
    """
    asserted = {(verb, _normalise(path)) for _, verb, path in _documented_paths()}

    # §1: `curl http://localhost:8000/health` — a bare URL, no verb.
    assert ("", "/health") in asserted, "the §1 pre-flight curl box is not being checked"
    # Slice 1: `GET /health` returns 200.
    assert ("GET", "/health") in asserted, "the Slice 1 health box is not being checked"
    # The disclaimed token itself stays exempt, in both bullets.
    assert not [p for _, p in asserted if p == "/healthz"]
    # The sibling routes named in the same §1 bullet are checked too.
    assert ("", "/health/dependencies") in asserted
    assert ("", "/health/index") in asserted

    # Whole-bullet exemption collapsed this to 3; per-token keeps ~all of it.
    assert len(asserted) >= 10, f"too few routes asserted, exemption is over-broad: {asserted}"


# ---------------------------------------------------------------------------
# Regression: the three specific claims #168 filed.
# ---------------------------------------------------------------------------


def test_healthz_is_never_asserted_as_a_live_route() -> None:
    """#168 item 2. ``/healthz`` has never existed; the routes are /health*."""
    assert "/healthz" not in _live_paths()
    # Sentence-scoped, same as ``_documented_paths``: the disclaimer sentence
    # may name /healthz, the imperative half of the same bullet may not.
    offenders = [
        (lineno, segment)
        for lineno, item in _items()
        for segment in _sentences(item)
        if "/healthz" in segment and not _disclaims(segment)
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
