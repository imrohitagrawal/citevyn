"""``GET /legal/{slug}`` — the legal pages (ADR-0005 Phase 5C).

Terms, Privacy, Refunds and No-training. The owner approves legal text.

* A PUBLISHED page (owner-approved) is served whatever the access model
  setting says, as final text: no notice, indexable. A payment provider's site
  review needs Terms, Privacy and Refunds public before billing can start.
* Any other page is a DRAFT: 404 while ``CITEVYN_ACCESS_MODEL_ENABLED`` is off,
  so the live site never shows unapproved terms, and otherwise a visible
  "DRAFT, not in effect" notice and ``X-Robots-Tag: noindex``.
* A published page may not contain an ``[OWNER: ...]`` note
  (``tests/test_legal_pages.py``).

The text is Markdown in ``app/legal/`` (it ships inside the API image) and is
rendered by the same code as ``/about``. The slug picks a file from a fixed
table; nothing from the request is used as a path.

Registration order: included before ``app.main._mount_frontend``'s catch-all
static mount, like ``/about`` (``tests/test_legal_pages.py`` pins it).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.access.deps import requires
from app.access.policy import Capability
from app.core.config import Settings, get_settings
from app.core.errors import APIErrorCode, error_response
from app.services.about_page import render_about_page

router = APIRouter(tags=["pages"])

_DIR = Path(__file__).resolve().parents[2] / "legal"

#: slug -> (page title, source file). The owner approves each page's text.
LEGAL_PAGES: dict[str, tuple[str, str]] = {
    "terms": ("Terms of Service", "terms.md"),
    "privacy": ("Privacy Policy", "privacy.md"),
    "refunds": ("Refund Policy", "refunds.md"),
    "no-training": ("We Don't Train on Your Questions", "no-training.md"),
}

#: Owner-approved 2026-10-28. No-training waits on two owner checks (#492).
PUBLISHED: frozenset[str] = frozenset({"terms", "privacy", "refunds"})

#: Change this date whenever a published page's text changes.
_PUBLISHED_LEAD = "Last updated 28 October 2026."

# Any bracket (ASCII or full-width), then anything but letters, then "owner" as
# a word: "[OWNER: x]", "[ OWNER : x]", "[Owner note: x]", "\uff3bOWNER\uff1ax\uff3d", "[OWNER]".
_OWNER_NOTE = re.compile(r"[\[\uff3b][^a-z\]\uff3d]*owner\b", re.IGNORECASE)


def has_owner_note(text: str) -> bool:
    """True if ``text`` still holds an unanswered ``[OWNER: ...]`` note."""
    return _OWNER_NOTE.search(text) is not None


_DRAFT_NOTICE = (
    "DRAFT, not in effect. This text is awaiting the owner's review and approval; "
    "it does not bind anyone and may change."
)


@router.get(
    "/legal/{slug}",
    dependencies=[Depends(requires(Capability.public))],
    response_class=HTMLResponse,
    summary="A legal page (a draft 404s unless the access model is on).",
)
async def legal_page(
    slug: str,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    page = LEGAL_PAGES.get(slug)
    published = slug in PUBLISHED
    if page is None or not (published or settings.access_model_enabled):
        raise error_response(
            request_id=str(request.state.request_id),
            code=APIErrorCode.not_found,
            message="Not found.",
        )
    title, filename = page
    markdown = (_DIR / filename).read_text(encoding="utf-8")
    if published:
        return HTMLResponse(
            render_about_page(
                [(title, markdown)],
                page_title=title,
                description=f"CiteVyn {title}.",
                lead=_PUBLISHED_LEAD,
            )
        )
    html = render_about_page(
        [(title, markdown)],
        page_title=title,
        description=f"CiteVyn {title} (draft).",
        lead=_DRAFT_NOTICE,
        lead_class="legal-draft",
    )
    return HTMLResponse(html, headers={"X-Robots-Tag": "noindex"})


__all__ = ["LEGAL_PAGES", "PUBLISHED", "has_owner_note", "router"]
