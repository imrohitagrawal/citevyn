"""One watcher run: fetch each watched page, compare with its last snapshot,
record real changes (ADR-0005 §5, Phase 7A).

* First time a page is seen: its snapshot is the baseline; no change is recorded.
* Same text (after whitespace normalisation): only ``checked_at`` moves.
* Different text: a ``doc_changes`` row with the section summary, and the
  snapshot becomes the new text.
* A fetch that fails keeps the old snapshot and records why in ``last_error``;
  a failure is never a change.

Each page is committed on its own, so one bad page cannot lose the others.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.doc_watch import DocChange, DocSnapshot
from app.watch.diff import normalize, section_changes
from app.watch.fetch import FetchError, RobotsCache, fetch_page
from app.watch.pages import WatchedPage, watched_hosts

MAX_PAGE_BYTES = 1_000_000


@dataclass
class RunStats:
    checked: int = 0
    baseline: int = 0
    unchanged: int = 0
    changed: int = 0
    failed: int = 0


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def run_watch(
    db: AsyncSession,
    client: httpx.AsyncClient,
    pages: tuple[WatchedPage, ...],
    *,
    user_agent: str,
    now: datetime,
) -> RunStats:
    stats = RunStats()
    robots = RobotsCache(client, user_agent)
    hosts = watched_hosts(pages)
    for page in pages:
        stats.checked += 1
        snap = await db.get(DocSnapshot, page.url)
        if snap is None:
            snap = DocSnapshot(
                url=page.url, product_area=page.product_area, title=page.title, checked_at=now
            )
            db.add(snap)
        snap.checked_at = now
        snap.title, snap.product_area = page.title, page.product_area
        try:
            text = normalize(
                await fetch_page(
                    client,
                    page.url,
                    allowed_hosts=hosts,
                    user_agent=user_agent,
                    max_bytes=MAX_PAGE_BYTES,
                    robots=robots,
                )
            )
        except FetchError as exc:
            snap.last_error = str(exc)[:255]
            stats.failed += 1
            await db.commit()
            continue
        sha = _sha(text)
        snap.last_error = None
        if snap.content_sha256 is None or snap.content is None:
            stats.baseline += 1
        elif snap.content_sha256 == sha:
            stats.unchanged += 1
        else:
            db.add(
                DocChange(
                    url=page.url,
                    product_area=page.product_area,
                    title=page.title,
                    detected_at=now,
                    old_sha256=snap.content_sha256,
                    new_sha256=sha,
                    summary=dict(section_changes(snap.content, text)),
                )
            )
            stats.changed += 1
        snap.content, snap.content_sha256, snap.fetched_at = text, sha, now
        await db.commit()
    return stats


__all__ = ["MAX_PAGE_BYTES", "RunStats", "run_watch"]
