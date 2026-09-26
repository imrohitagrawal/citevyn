"""``GET /legal/{slug}`` — the DRAFT legal pages (ADR-0005 Phase 5C).

Terms, Privacy, Refunds and No-training, drafted for the owner to review. The
owner approves legal text; until then each page:

* answers 404 while ``CITEVYN_ACCESS_MODEL_ENABLED`` is off, so the live site
  never shows unapproved terms;
* carries a visible "DRAFT, not in effect" notice;
* is sent with ``X-Robots-Tag: noindex``.

The text is Markdown in ``app/legal/`` (it ships inside the API image) and is
rendered by the same code as ``/about``. The slug picks a file from a fixed
table; nothing from the request is used as a path.

Registration order: included before ``app.main._mount_frontend``'s catch-all
static mount, like ``/about`` (``tests/test_legal_pages.py`` pins it).
"""

from __future__ import annotations

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

_DRAFT_NOTICE = (
    "DRAFT, not in effect. This text is awaiting the owner's review and approval; "
    "it does not bind anyone and may change."
)


@router.get(
    "/legal/{slug}",
    dependencies=[Depends(requires(Capability.public))],
    response_class=HTMLResponse,
    summary="A draft legal page (404 unless the access model is on).",
)
async def legal_page(
    slug: str,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    page = LEGAL_PAGES.get(slug)
    if not settings.access_model_enabled or page is None:
        raise error_response(
            request_id=str(request.state.request_id),
            code=APIErrorCode.not_found,
            message="Not found.",
        )
    title, filename = page
    markdown = (_DIR / filename).read_text(encoding="utf-8")
    html = render_about_page(
        [(title, markdown)],
        page_title=title,
        description=f"CiteVyn {title} (draft).",
        lead=_DRAFT_NOTICE,
        lead_class="legal-draft",
    )
    return HTMLResponse(html, headers={"X-Robots-Tag": "noindex"})


__all__ = ["LEGAL_PAGES", "router"]
