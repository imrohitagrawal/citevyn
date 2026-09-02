"""Transactional email delivery seam (ADR-0004 PR 14).

A minimal :class:`EmailClient` protocol with two implementations:

* :class:`ResendEmailClient` -- the production path (``POST
  https://api.resend.com/emails``). Mirrors ``app.core.oauth_http``'s error
  taxonomy: timeout -> clear error, non-2xx -> clear error, the upstream body
  logged SERVER-SIDE only and never placed in the exception message (issue
  #50's invariant, again). ``httpx`` is already a dependency; nothing new.
* :class:`FileOutboxEmailClient` -- the local/dev path. Writes each rendered
  message to a file under a directory so a magic link can be obtained
  without any provider account. It is refused in production by a
  ``Settings`` validator (``app.core.config``) and the choice between the
  two is made by the route, not here.

The provider (Resend vs. SES vs. Postmark) is a genuinely live decision --
unlike OAuth's fixed provider set -- which is what earns the ``Protocol`` seam
its near-zero cost now. The only ``app.*`` import is the request-id logging
helper, the same single tolerated exception ``oauth_http`` carries; check with
``grep "^from app\\." backend/app/core/email_client.py``.

**Never log a message body.** A magic-link email's body IS the credential.
The outbox client logs the file path it wrote (which carries no secret); the
Resend client logs the upstream status and error body on failure, which
Resend's API does not echo the request body into.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from app.core.middleware import get_current_request_id

_logger = logging.getLogger("citevyn.email")

# Cap the upstream body kept in the SERVER log -- mirrors
# ``app.core.oauth_http._ERROR_BODY_LOG_LIMIT``.
_ERROR_BODY_LOG_LIMIT = 500

_RESEND_API_BASE = "https://api.resend.com"


class EmailDeliveryError(RuntimeError):
    """Raised when a send fails (transport, timeout, or non-2xx).

    Safe to log; never placed in an HTTP response. The magic-link route
    delivers in a background task and only logs this.
    """


@dataclass(frozen=True)
class EmailMessage:
    """One outbound message. ``text`` is the plain-text alternative to ``html``."""

    to_addr: str
    subject: str
    text: str
    html: str


class EmailClient(Protocol):
    """Minimum surface the magic-link route needs from a delivery backend."""

    async def send(self, message: EmailMessage) -> None: ...


class ResendEmailClient:
    """Deliver via Resend's HTTP API.

    ``transport`` is a test seam: an ``httpx.MockTransport`` keeps the unit
    tests hermetic (this codebase's established pattern for the LLM and
    OAuth clients). One short-lived ``httpx.AsyncClient`` per send -- the
    magic-link route sends at most one message per request, so a pooled
    client would buy nothing.
    """

    def __init__(
        self,
        *,
        api_key: str,
        from_addr: str,
        base_url: str = _RESEND_API_BASE,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be a non-empty string")
        if not from_addr:
            raise ValueError("from_addr must be a non-empty string")
        self._api_key = api_key
        self._from_addr = from_addr
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def send(self, message: EmailMessage) -> None:
        payload = {
            "from": self._from_addr,
            "to": [message.to_addr],
            "subject": message.subject,
            "text": message.text,
            "html": message.html,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(transport=self._transport) as client:
                response = await client.post(
                    f"{self._base_url}/emails",
                    json=payload,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
        except httpx.TimeoutException as exc:
            raise EmailDeliveryError(
                f"resend request timed out after {self._timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise EmailDeliveryError(f"resend transport error: {exc.__class__.__name__}") from exc

        if response.status_code >= 400:
            # Upstream error text is logged server-side only (never the
            # request headers, which hold the API key) and kept out of the
            # exception message so it cannot leak to a caller.
            _logger.warning(
                "resend_send_error",
                extra={
                    "request_id": get_current_request_id(),
                    "status_code": response.status_code,
                    "body": response.text[:_ERROR_BODY_LOG_LIMIT],
                },
            )
            raise EmailDeliveryError(f"resend returned {response.status_code}")


class FileOutboxEmailClient:
    """Write each message to ``directory`` instead of sending it (dev only).

    The file name is a timestamp plus random suffix -- it carries no part of
    the message, so the INFO log line naming the path leaks nothing. The file
    itself holds the full rendered message (headers, text, html) so a local
    developer can copy the magic link out of it.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory

    async def send(self, message: EmailMessage) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = self._directory / f"{stamp}-{secrets.token_hex(4)}.eml"
        body = (
            f"To: {message.to_addr}\n"
            f"Subject: {message.subject}\n"
            f"Date: {datetime.now(UTC).isoformat()}\n"
            "\n"
            f"{message.text}\n"
            "\n"
            "--- html ---\n"
            f"{message.html}\n"
        )
        path.write_text(body, encoding="utf-8")
        _logger.info(
            "email_outbox_written",
            extra={"request_id": get_current_request_id(), "path": str(path)},
        )


__all__ = [
    "EmailClient",
    "EmailDeliveryError",
    "EmailMessage",
    "FileOutboxEmailClient",
    "ResendEmailClient",
]
