"""``GET /about`` — the page every CiteVyn self-citation points at (#84 item 6).

``app.worker.allowlist`` gives two sources the ``source_url`` ``/about``: the
About-CiteVyn product description and the AI-concepts glossary. That URL is
persisted onto ``Document.source_url`` at ingest time and shipped to the browser
in every ``citations[].url``, where the frontend renders it as a real
``<a href="/about">``. Until this route existed the link answered the API's JSON
404 envelope — reproduced against production on 2026-09-06.

Serving the URL rather than changing it is deliberate. The URL lives in the
database, so changing ``allowlist.py`` would fix nothing already indexed without
a production re-ingest and index promote (owner-gated ops). Serving ``/about``
needs only a deploy and repairs every citation already stored.

Registration order is load-bearing. ``app.main._mount_frontend`` mounts
``StaticFiles`` at ``/`` — a catch-all — as the last thing ``create_app`` does,
so this router must be included before it. Getting that wrong does not raise:
the mount answers ``/about`` with a 307 to ``/about/`` and the page silently
never renders. ``tests/test_about_page.py`` pins the ordering directly.

``/about/`` (trailing slash) is deliberately NOT served. The mount at ``/``
suppresses Starlette's ``redirect_slashes``, so it answers the JSON 404
envelope. No citation emits that form — every one is exactly ``/about`` — so
adding a second route for it is unpaid-for surface. Recorded here rather than
left as a silent gap.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.logging import build_log_event
from app.services.about_page import render_about_page
from app.worker.allowlist import MVP_SOURCES, SourceSpec
from app.worker.fetchers import FetchError, build_fetcher

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pages"])

#: The exact URL stamped onto every self-referential citation. Named rather than
#: repeated so the route, the tests and the source selection cannot drift apart.
ABOUT_PATH = "/about"


def about_sources() -> list[SourceSpec]:
    """Every allowlisted source whose citations point at :data:`ABOUT_PATH`.

    Derived from ``MVP_SOURCES`` rather than hard-coded: a future source that
    describes CiteVyn itself and reuses this citation URL then appears on the
    page automatically, instead of quietly citing a page that omits it.
    """
    return [spec for spec in MVP_SOURCES if spec.source_url == ABOUT_PATH]


def _load_documents() -> list[tuple[str, str]]:
    """``(title, markdown)`` for each self-citing source, unreadable ones skipped.

    A source that cannot be read is logged and dropped rather than raised. The
    corpus docs ship inside the API image beside this module, so an unreadable
    one means a broken build — but answering a 500 on a public page would turn a
    packaging fault into an outage of the page, and the remaining sections are
    still worth serving.

    ``OSError`` is caught alongside ``FetchError`` because ``LocalFetcher``
    only converts "missing" and "not utf-8"; a permissions or I/O error from
    ``read_text`` escapes it and would have 500'd this page — the exact reader
    experience #84 exists to remove.

    Logged through :func:`build_log_event`, not ``extra=``: the app's log
    format is ``%(message)s`` (``app/core/logging.py``), so an ``extra`` dict is
    DROPPED and the line would have read ``about_page_source_unreadable`` with
    no indication of which source failed or why. Same trap already recorded
    against the email-failure log line in #296.
    """
    documents: list[tuple[str, str]] = []
    for spec in about_sources():
        try:
            documents.append((spec.title, build_fetcher(spec).fetch(spec)))
        except (FetchError, OSError) as exc:
            logger.warning(
                build_log_event(
                    "about_page_source_unreadable",
                    source=spec.name,
                    error=str(exc),
                )
            )
    return documents


@router.get(
    ABOUT_PATH,
    response_class=HTMLResponse,
    summary="The About-CiteVyn page that self-referential citations link to.",
    description=(
        "Renders the allowlisted corpus documents whose citation URL is `/about` "
        "-- the product description and the AI-concepts glossary -- so a citation "
        "resolves to the source text the answer was drawn from rather than a "
        "paraphrase of it. Public, unauthenticated, no query parameters."
    ),
)
async def about_page() -> HTMLResponse:
    return HTMLResponse(render_about_page(_load_documents()))


__all__ = ["ABOUT_PATH", "about_page", "about_sources", "router"]
