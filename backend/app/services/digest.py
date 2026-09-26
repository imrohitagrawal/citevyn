"""The weekly "what changed" digest (ADR-0005 §5, Phase 7B).

Accounts that opted in (``users.digest_opt_in_at``) with a verified address get
one email a week listing the watched vendor pages that changed in the last 7
days (``doc_changes``, from the docs watcher). No changes, no email.

* One email per account per week: ``digest_sends`` is unique on (account,
  week), so a re-run in the same week mails no one twice. A failed send (or
  any error before the send finishes) removes its row, so the next run
  retries that account. So delivery is at-least-once: a send that timed out
  but did arrive can arrive again on the retry.
* Each account gets the changes since its own last digest (at most 7 days),
  so runs either side of Monday never repeat a change.
* Unsubscribe links do not expire: any old digest's link still turns the
  digest off for the account it was sent to (and only that account).
* Every email carries a one-click unsubscribe link (RFC 8058 headers too); its
  token is random, stored only as a hash on the send row.
* At most ``digest_max_per_run`` send ATTEMPTS per run (failures count), one
  every ``digest_send_interval_seconds``.
"""

from __future__ import annotations

import asyncio
import html
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import delete, func, select
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


@dataclass(frozen=True)
class ChangeItem:
    """A ``doc_changes`` row as plain values (safe after any rollback)."""

    url: str
    product_area: str
    title: str
    detected_at: datetime
    summary: dict[str, Any]


class _Change(Protocol):
    @property
    def url(self) -> str: ...
    @property
    def product_area(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def summary(self) -> dict[str, Any]: ...


def _describe(change: _Change) -> str:
    s = change.summary
    parts: list[str] = []
    for key, word in (("changed", "changed"), ("added", "new"), ("removed", "removed")):
        names = list(s.get(key) or [])
        if names:
            shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
            parts.append(f"{word}: {shown}")
    return "; ".join(parts) or "updated"


def digest_message(
    *, to_addr: str, changes: Sequence[_Change], unsubscribe_url: str
) -> EmailMessage:
    """The digest email. Every value is escaped in the HTML part."""
    by_product: dict[str, list[_Change]] = {}
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
    skipped_nothing_new: int = 0
    failed: int = 0
    capped: bool = False


def _aware(at: datetime) -> datetime:
    """SQLite returns naive datetimes; they are UTC."""
    return at if at.tzinfo else at.replace(tzinfo=UTC)


async def send_weekly_digest(
    db: AsyncSession,
    client: EmailClient,
    settings: Settings,
    *,
    now: datetime,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DigestStats:
    """Send this week's digest. Everything the loop needs is copied into plain
    values FIRST: a rollback expires every ORM object in the session, and an
    expired row read later in an async session crashes (a re-run in the same
    week did exactly that, after committing the next account's claim)."""
    base = site_base_url(settings)
    if "\r" in base or "\n" in base:
        raise ValueError("the site URL contains a line break; refusing to build mail headers")
    stats = DigestStats()
    changes = [
        ChangeItem(
            url=c.url,
            product_area=c.product_area,
            title=c.title,
            detected_at=_aware(c.detected_at),
            summary=dict(c.summary),
        )
        for c in (
            await db.execute(
                select(DocChange)
                .where(DocChange.detected_at >= now - WINDOW, DocChange.detected_at <= now)
                .order_by(DocChange.product_area, DocChange.title, DocChange.detected_at)
            )
        ).scalars()
    ]
    stats.changes = len(changes)
    if not changes:
        return stats
    week = week_start(now)
    recipients: list[tuple[str, str]] = [
        (str(uid), str(email))
        for uid, email in (
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
    ]
    claimed = {
        str(uid)
        for (uid,) in (
            await db.execute(select(DigestSend.user_id).where(DigestSend.week_start == week))
        ).all()
    }
    # Each account gets what changed since ITS last digest (at most 7 days), so
    # runs either side of Monday never send the same change twice.
    last_sent = {
        str(uid): _aware(at)
        for uid, at in (
            await db.execute(
                select(DigestSend.user_id, func.max(DigestSend.sent_at)).group_by(
                    DigestSend.user_id
                )
            )
        ).all()
    }
    for user_id, email in recipients:
        if user_id in claimed:
            stats.skipped_already_sent += 1
            continue
        since = last_sent.get(user_id)
        mine = [c for c in changes if since is None or c.detected_at > since]
        if not mine:
            stats.skipped_nothing_new += 1
            continue
        if stats.sent + stats.failed >= settings.digest_max_per_run:
            stats.capped = True
            break
        token = generate_token()
        send_id = uuid.uuid4()
        db.add(
            DigestSend(
                send_id=send_id,
                user_id=user_id,
                week_start=week,
                sent_at=now,
                token_hash=hash_token(token),
            )
        )
        try:
            await db.commit()  # claim (account, week) BEFORE sending: no double mail
        except IntegrityError:
            await db.rollback()  # another run claimed it; only plain values are used below
            stats.skipped_already_sent += 1
            continue
        url = f"{base}{UNSUBSCRIBE_PATH}?t={send_id}.{token}"
        try:
            await client.send(digest_message(to_addr=email, changes=mine, unsubscribe_url=url))
        except EmailDeliveryError:
            await _release(db, send_id)  # not sent: the next run retries this account
            stats.failed += 1
        except BaseException:
            await _release(db, send_id)  # never leave a claim for mail that was not sent
            raise
        else:
            stats.sent += 1
        if settings.digest_send_interval_seconds > 0:
            await sleep(settings.digest_send_interval_seconds)
    return stats


async def _release(db: AsyncSession, send_id: uuid.UUID) -> None:
    await db.execute(delete(DigestSend).where(DigestSend.send_id == send_id))
    await db.commit()


__all__ = ["UNSUBSCRIBE_PATH", "DigestStats", "digest_message", "send_weekly_digest", "week_start"]
