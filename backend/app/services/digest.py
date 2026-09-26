"""The weekly "what changed" digest (ADR-0005 §5, Phase 7B).

Accounts that opted in (``users.digest_opt_in_at``) with a verified address get
one email a week listing the watched vendor pages that changed in the last 7
days (``doc_changes``, from the docs watcher). No changes, no email.

* One email per account per week: ``digest_sends`` is unique on (account,
  week), so a re-run in the same week mails no one twice. A failed send removes
  its row, so the next run retries that account.
* Every email carries a one-click unsubscribe link (RFC 8058 headers too); its
  token is random, stored only as a hash on the send row.
* At most ``digest_max_per_run`` emails per run, one every
  ``digest_send_interval_seconds``.
"""

from __future__ import annotations

import asyncio
import html
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.answer.comparison import product_name
from app.core.config import Settings
from app.core.email_client import EmailClient, EmailDeliveryError, EmailMessage
from app.core.token_secrets import generate_token, hash_token
from app.models import DigestSend, DocChange, User
from app.services.notifications import site_base_url
from app.watch.pages import human_url

WINDOW = timedelta(days=7)
UNSUBSCRIBE_PATH = "/digest/unsubscribe"


def week_start(now: datetime) -> date:
    """The Monday (UTC) of ``now``'s week: the digest's once-a-week key."""
    day = now.date()
    return day - timedelta(days=day.weekday())


def _describe(change: DocChange) -> str:
    s = change.summary
    parts: list[str] = []
    for key, word in (("changed", "changed"), ("added", "new"), ("removed", "removed")):
        names = list(s.get(key) or [])
        if names:
            shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
            parts.append(f"{word}: {shown}")
    return "; ".join(parts) or "updated"


def digest_message(
    *, to_addr: str, changes: Sequence[DocChange], unsubscribe_url: str
) -> EmailMessage:
    """The digest email. Every value is escaped in the HTML part."""
    by_product: dict[str, list[DocChange]] = {}
    for c in changes:
        by_product.setdefault(c.product_area, []).append(c)
    text_lines = ["What changed in the AI docs this week", ""]
    html_parts = ["<p>What changed in the AI docs this week</p>"]
    for area in sorted(by_product):
        name = product_name(area)
        heading = name[:1].upper() + name[1:]
        text_lines.append(heading)
        items: list[str] = []
        for c in by_product[area]:
            url = human_url(c.url)
            text_lines.append(f"- {c.title} ({_describe(c)}): {url}")
            items.append(
                f'<li><a href="{html.escape(url, quote=True)}">{html.escape(c.title)}</a> '
                f"({html.escape(_describe(c))})</li>"
            )
        text_lines.append("")
        html_parts.append(f"<p>{html.escape(heading)}</p><ul>{''.join(items)}</ul>")
    footer = (
        "You get this because you asked for the weekly CiteVyn digest. "
        f"Stop it with one click: {unsubscribe_url}"
    )
    text_lines.append(footer)
    html_parts.append(
        "<p>You get this because you asked for the weekly CiteVyn digest. "
        f'<a href="{html.escape(unsubscribe_url, quote=True)}">Stop it with one click</a>.</p>'
    )
    return EmailMessage(
        to_addr=to_addr,
        subject="What changed in the AI docs this week",
        text="\n".join(text_lines) + "\n",
        html="".join(html_parts),
        headers={
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    )


@dataclass
class DigestStats:
    changes: int = 0
    sent: int = 0
    skipped_already_sent: int = 0
    failed: int = 0
    capped: bool = False


async def send_weekly_digest(
    db: AsyncSession,
    client: EmailClient,
    settings: Settings,
    *,
    now: datetime,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DigestStats:
    stats = DigestStats()
    changes = list(
        (
            await db.execute(
                select(DocChange)
                .where(DocChange.detected_at >= now - WINDOW, DocChange.detected_at <= now)
                .order_by(DocChange.product_area, DocChange.title, DocChange.detected_at)
            )
        ).scalars()
    )
    stats.changes = len(changes)
    if not changes:
        return stats
    week = week_start(now)
    recipients = (
        await db.execute(
            select(User.user_id, User.email)
            .where(
                User.digest_opt_in_at.is_not(None),
                User.email_verified_at.is_not(None),
                User.email.is_not(None),
            )
            .order_by(User.user_id)
        )
    ).all()
    base = site_base_url(settings)
    for user_id, email in recipients:
        if stats.sent >= settings.digest_max_per_run:
            stats.capped = True
            break
        token = generate_token()
        row = DigestSend(
            send_id=uuid.uuid4(),
            user_id=user_id,
            week_start=week,
            sent_at=now,
            token_hash=hash_token(token),
        )
        db.add(row)
        try:
            await db.commit()  # claim (account, week) BEFORE sending: no double mail
        except IntegrityError:
            await db.rollback()
            stats.skipped_already_sent += 1
            continue
        url = f"{base}{UNSUBSCRIBE_PATH}?t={row.send_id}.{token}"
        try:
            await client.send(digest_message(to_addr=email, changes=changes, unsubscribe_url=url))
        except EmailDeliveryError:
            await db.delete(row)  # not sent: let the next run retry this account
            await db.commit()
            stats.failed += 1
        else:
            stats.sent += 1
        if settings.digest_send_interval_seconds > 0:
            await sleep(settings.digest_send_interval_seconds)
    return stats


__all__ = ["UNSUBSCRIBE_PATH", "DigestStats", "digest_message", "send_weekly_digest", "week_start"]
