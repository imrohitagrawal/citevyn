"""Watch alert emails (ADR-0005 §5, Phase 7C-2).

For each current Pro account with a verified address and at least one watch:
the changes of the last 7 days that match a watch and were not alerted to it
before. One email per account per run lists them all.

* Never twice: ``alert_sends`` is unique on (account, change), claimed BEFORE
  sending; an error before the send finishes removes the claim, so the next run
  retries (at-least-once: a send that timed out but arrived can arrive again).
  Because dedupe is per change, not by time, a change the watcher commits while
  a run is in progress is simply picked up by the next run.
* Everything the loop needs is copied into plain values first (a rollback
  expires ORM objects; the digest learned this the hard way).
* One-click unsubscribe per email (RFC 8058 headers too); it removes that
  account's watches.
* At most ``alert_max_per_run`` send attempts per run (failures count), one
  every ``alert_send_interval_seconds``.
"""

from __future__ import annotations

import asyncio
import html
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.answer.comparison import product_name
from app.billing.memberships import account_is_pro, memberships_for
from app.core.config import Settings
from app.core.email_client import EmailClient, EmailDeliveryError, EmailMessage
from app.core.token_secrets import generate_token, hash_token
from app.models import AlertSend, DocChange, User, Watch
from app.services.notifications import mail_link_base
from app.watch.match import matches
from app.watch.pages import human_url

WINDOW = timedelta(days=7)
UNSUBSCRIBE_PATH = "/alerts/unsubscribe"
#: Most changes listed in one alert email; the rest are counted.
MAX_LISTED = 50


@dataclass(frozen=True)
class _Change:
    change_id: uuid.UUID
    url: str
    product_area: str
    title: str
    summary: dict[str, Any]
    lines: dict[str, Any] | None


@dataclass(frozen=True)
class _Hit:
    change: _Change
    watches: tuple[str, ...]  # what matched, for the email


@dataclass
class AlertStats:
    changes: int = 0
    sent: int = 0
    failed: int = 0
    skipped_not_pro: int = 0
    capped: bool = False
    matched_accounts: list[str] = field(default_factory=list[str])


def _label(kind: str, value: str) -> str:
    return f"the page {value}" if kind == "page" else f'"{value}"'


def alert_message(*, to_addr: str, hits: Sequence[_Hit], unsubscribe_url: str) -> EmailMessage:
    """The alert email. Every value is escaped in the HTML part."""
    text = ["Docs you watch changed", ""]
    items: list[str] = []
    shown, more = hits[:MAX_LISTED], len(hits) - MAX_LISTED
    for hit in shown:
        c = hit.change
        name = f"{product_name(c.product_area)}: {c.title}"
        url = human_url(c.url)
        why = "; ".join(hit.watches)
        raw: list[object] = list(c.summary.get("changed") or [])
        changed = [str(x) for x in raw]
        sections = ", ".join(changed[:3]) or "the page"
        maybe = (
            " (a very large change: some lines were not searched, so a match may be missing)"
            if (c.lines or {}).get("truncated")
            else ""
        )
        text.append(f"- {name}: {sections} ({why}){maybe}: {url}")
        items.append(
            f'<li><a href="{html.escape(url, quote=True)}">{html.escape(name)}</a>: '
            f"{html.escape(sections)} ({html.escape(why)}){html.escape(maybe)}</li>"
        )
    if more > 0:
        text.append(f"...and {more} more changed pages you watch.")
        items.append(f"<li>...and {more} more changed pages you watch.</li>")
    text += ["", f"Stop watch alerts with one click (this removes your watches): {unsubscribe_url}"]
    body = (
        "<p>Docs you watch changed</p>"
        f"<ul>{''.join(items)}</ul>"
        f'<p><a href="{html.escape(unsubscribe_url, quote=True)}">Stop watch alerts</a> '
        "(this removes your watches).</p>"
    )
    return EmailMessage(
        to_addr=to_addr,
        subject="Docs you watch changed",
        text="\n".join(text) + "\n",
        html=body,
        headers={
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    )


async def send_watch_alerts(
    db: AsyncSession,
    client: EmailClient,
    settings: Settings,
    *,
    now: datetime,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AlertStats:
    base = mail_link_base(settings)
    stats = AlertStats()
    changes = [
        _Change(
            c.change_id,
            c.url,
            c.product_area,
            c.title,
            dict(c.summary),
            dict(c.lines or {}) or None,
        )
        for c in (
            await db.execute(
                select(DocChange)
                .where(DocChange.detected_at >= now - WINDOW, DocChange.detected_at <= now)
                .order_by(DocChange.detected_at, DocChange.url)
            )
        ).scalars()
    ]
    stats.changes = len(changes)
    if not changes:
        return stats
    watch_rows = (
        await db.execute(
            select(Watch.user_id, Watch.kind, Watch.value, User.email)
            .join(User, User.user_id == Watch.user_id)
            .where(User.email_verified_at.is_not(None), User.email.is_not(None))
            .order_by(Watch.user_id, Watch.created_at)
        )
    ).all()
    watches: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for uid, kind, value, email in watch_rows:
        watches.setdefault(str(uid), (str(email), []))[1].append((str(kind), str(value)))
    done = {
        (str(uid), cid)
        for uid, cid in (
            await db.execute(
                select(AlertSend.user_id, AlertSend.change_id).where(
                    AlertSend.change_id.in_([c.change_id for c in changes])
                )
            )
        ).all()
    }
    for user_id, (email, theirs) in watches.items():
        hits: list[_Hit] = []
        for c in changes:
            if (user_id, c.change_id) in done:
                continue
            why = tuple(_label(k, v) for k, v in theirs if matches(k, v, url=c.url, lines=c.lines))
            if why:
                hits.append(_Hit(c, why))
        if not hits:
            continue
        stats.matched_accounts.append(user_id)
        if not account_is_pro(await memberships_for(db, user_id), now):
            stats.skipped_not_pro += 1  # a lapsed account keeps its watches, gets no alerts
            continue
        if stats.sent + stats.failed >= settings.alert_max_per_run:
            stats.capped = True
            break
        token = generate_token()
        email_id = uuid.uuid4()
        for hit in hits:
            db.add(
                AlertSend(
                    alert_id=uuid.uuid4(),
                    email_id=email_id,
                    user_id=user_id,
                    change_id=hit.change.change_id,
                    sent_at=now,
                    token_hash=hash_token(token),
                )
            )
        try:
            await db.commit()  # claim BEFORE sending: never alerted twice
        except IntegrityError:
            await db.rollback()  # an overlapping run claimed some; it will mail them
            continue
        url = f"{base}{UNSUBSCRIBE_PATH}?t={email_id}.{token}"
        try:
            await client.send(alert_message(to_addr=email, hits=hits, unsubscribe_url=url))
        except EmailDeliveryError:
            await _release(db, email_id)  # not sent: the next run retries
            stats.failed += 1
        except BaseException:
            await _release(db, email_id)  # never keep a claim for mail not sent
            raise
        else:
            stats.sent += 1
        if settings.alert_send_interval_seconds > 0:
            await sleep(settings.alert_send_interval_seconds)
    return stats


async def _release(db: AsyncSession, email_id: uuid.UUID) -> None:
    await db.execute(delete(AlertSend).where(AlertSend.email_id == email_id))
    await db.commit()


__all__ = ["UNSUBSCRIBE_PATH", "AlertStats", "alert_message", "send_watch_alerts"]
