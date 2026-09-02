"""Transactional email for the auth routes (ADR-0004 PR 14 / PR 15).

One place that (a) picks the delivery backend from ``Settings`` and (b)
renders the three messages the auth flows send: the magic link itself, the
"you signed in with a link" notice, and the "your password was set/changed"
notice. The last two are #293's second guardrail: a stolen link or a
hijacked session becomes a race the inbox owner can see and win ("request a
sign-in link now and set a new password"), instead of a silent takeover.

Delivery always happens in a background task after the response is sent
(``deliver``), and a failure is logged with the provider's status and a
redacted reason -- never raised into the request, never with the message
body or the address (the address is personal data; the magic-link body IS
a credential).
"""

from __future__ import annotations

import html
import logging
import tempfile
from datetime import datetime
from pathlib import Path

from app.core.config import Settings
from app.core.email_client import (
    EmailClient,
    EmailDeliveryError,
    EmailMessage,
    FileOutboxEmailClient,
    ResendEmailClient,
)

_logger = logging.getLogger("citevyn.magic_link")

# Local-dev fallback for the emailed link's origin -- same default the OAuth
# redirect URI uses (``app.api.routes.oauth._redirect_uri``).
LOCAL_BASE_URL = "http://localhost:8000"


def build_email_client(settings: Settings) -> EmailClient | None:
    """Pick the delivery backend, or ``None`` when email is unavailable.

    A configured ``resend_api_key`` wins everywhere. Otherwise the file
    outbox is used outside production only (a ``Settings`` validator refuses
    it IN production), and production without a provider returns ``None``
    -- the magic-link request route then 404s, the same "not configured ->
    quiet 404" convention an unconfigured OAuth provider follows, and the
    notices are simply not sent.
    """
    if settings.resend_api_key:
        return ResendEmailClient(
            api_key=settings.resend_api_key, from_addr=settings.email_from or ""
        )
    if settings.environment != "production":
        directory = (
            Path(settings.email_outbox_dir)
            if settings.email_outbox_dir
            else Path(tempfile.gettempdir()) / "citevyn_email_outbox"
        )
        return FileOutboxEmailClient(directory)
    return None


def site_base_url(settings: Settings) -> str:
    return (settings.magic_link_base_url or LOCAL_BASE_URL).rstrip("/")


def _when(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M UTC")


def magic_link_message(*, to_addr: str, link: str, ttl_seconds: int) -> EmailMessage:
    minutes = max(1, ttl_seconds // 60)
    plural = "s" if minutes != 1 else ""
    text = (
        "Sign in to CiteVyn\n"
        "\n"
        f"Open this link to sign in: {link}\n"
        "\n"
        f"It works once and expires in {minutes} minute{plural}. If you didn't ask for it, "
        "ignore this email -- nothing happens unless the link is used.\n"
    )
    safe_link = html.escape(link, quote=True)
    body_html = (
        "<p>Sign in to CiteVyn</p>"
        f'<p><a href="{safe_link}">Open this link to sign in</a></p>'
        f"<p>It works once and expires in {minutes} minute{plural}. If you didn't ask for it, "
        "ignore this email &mdash; nothing happens unless the link is used.</p>"
    )
    return EmailMessage(
        to_addr=to_addr, subject="Your CiteVyn sign-in link", text=text, html=body_html
    )


def _if_not_you(base_url: str) -> tuple[str, str]:
    text = (
        f'If this wasn\'t you, go to {base_url}/ now, choose "Email me a sign-in link", '
        "and set a new password from your account menu. Setting a password signs out "
        "every other device."
    )
    safe = html.escape(base_url, quote=True)
    body_html = (
        f'If this wasn\'t you, go to <a href="{safe}/">{safe}/</a> now, choose '
        "&ldquo;Email me a sign-in link&rdquo;, and set a new password from your account "
        "menu. Setting a password signs out every other device."
    )
    return text, body_html


def magic_link_signin_message(*, to_addr: str, at: datetime, base_url: str) -> EmailMessage:
    text_tail, html_tail = _if_not_you(base_url)
    text = f"You signed in to CiteVyn with an email link at {_when(at)}.\n\n{text_tail}\n"
    body_html = (
        f"<p>You signed in to CiteVyn with an email link at {_when(at)}.</p><p>{html_tail}</p>"
    )
    return EmailMessage(
        to_addr=to_addr, subject="New sign-in to CiteVyn", text=text, html=body_html
    )


def password_changed_message(
    *, to_addr: str, at: datetime, first_time: bool, base_url: str
) -> EmailMessage:
    verb = "set" if first_time else "changed"
    text_tail, html_tail = _if_not_you(base_url)
    text = f"Your CiteVyn password was {verb} at {_when(at)}.\n\n{text_tail}\n"
    body_html = f"<p>Your CiteVyn password was {verb} at {_when(at)}.</p><p>{html_tail}</p>"
    return EmailMessage(
        to_addr=to_addr, subject=f"Your CiteVyn password was {verb}", text=text, html=body_html
    )


async def deliver(client: EmailClient, message: EmailMessage, request_id: str) -> None:
    """Background send. Failures are logged with status + reason, never raised."""
    try:
        await client.send(message)
    except EmailDeliveryError as exc:
        # The reason is the provider's status line ("resend returned 422"),
        # never its body (logged separately, capped) and never the address.
        _logger.warning(
            f"magic_link_email_failed: {exc}",
            extra={"request_id": request_id, "reason": str(exc)},
        )


async def deliver_nothing() -> None:
    """The no-match branch's background task -- explicit, so both branches register one."""
    return None


__all__ = [
    "LOCAL_BASE_URL",
    "build_email_client",
    "deliver",
    "deliver_nothing",
    "magic_link_message",
    "magic_link_signin_message",
    "password_changed_message",
    "site_base_url",
]
