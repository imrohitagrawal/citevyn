"""Tests for ``app.core.email_client`` and the magic-link ``Settings`` guards (ADR-0004 PR 14).

Hermetic: the Resend client is exercised through ``httpx.MockTransport``,
this codebase's established pattern for provider HTTP clients.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.api.routes.magic_link import _build_email_client
from app.core import email_client as email_client_module
from app.core.config import Settings
from app.core.email_client import (
    EmailDeliveryError,
    EmailMessage,
    FileOutboxEmailClient,
    ResendEmailClient,
)

_MESSAGE = EmailMessage(
    to_addr="someone@example.com",
    subject="Your CiteVyn sign-in link",
    text="plain body with http://localhost:8000/v1/auth/magic-link/confirm?token=abc.def",
    html='<a href="http://localhost:8000/v1/auth/magic-link/confirm?token=abc.def">x</a>',
)


def _resend(handler, **kwargs) -> ResendEmailClient:
    return ResendEmailClient(
        api_key=kwargs.pop("api_key", "re_test_key"),
        from_addr=kwargs.pop("from_addr", "CiteVyn <login@example.com>"),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# ResendEmailClient
# ---------------------------------------------------------------------------


def test_resend_posts_the_message_with_the_bearer_key() -> None:
    """RED if the endpoint, auth header or payload field names change."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "email_1"})

    asyncio.run(_resend(handler).send(_MESSAGE))

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["authorization"] == "Bearer re_test_key"
    payload = json.loads(request.content)
    assert payload == {
        "from": "CiteVyn <login@example.com>",
        "to": ["someone@example.com"],
        "subject": _MESSAGE.subject,
        "text": _MESSAGE.text,
        "html": _MESSAGE.html,
    }


def test_resend_non_2xx_raises_without_echoing_the_upstream_body() -> None:
    """Issue #50's invariant: the provider's error text never reaches the caller.
    RED if the exception message includes ``response.text``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "SECRET-UPSTREAM-DETAIL"})

    with pytest.raises(EmailDeliveryError) as excinfo:
        asyncio.run(_resend(handler).send(_MESSAGE))
    assert "SECRET-UPSTREAM-DETAIL" not in str(excinfo.value)
    assert "422" in str(excinfo.value)


def test_resend_timeout_is_a_delivery_error_not_a_raw_httpx_exception() -> None:
    """RED if the ``httpx.TimeoutException`` branch is dropped."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(EmailDeliveryError, match="timed out"):
        asyncio.run(_resend(handler).send(_MESSAGE))


def test_resend_rejects_an_empty_key_or_sender_at_construction() -> None:
    with pytest.raises(ValueError):
        ResendEmailClient(api_key="", from_addr="x@example.com")
    with pytest.raises(ValueError):
        ResendEmailClient(api_key="k", from_addr="")


# ---------------------------------------------------------------------------
# FileOutboxEmailClient
# ---------------------------------------------------------------------------


def test_outbox_writes_the_full_message_to_a_file_whose_name_carries_no_secret(
    tmp_path: Path,
) -> None:
    """RED if the file omits the text body (the local dev workflow copies the
    link out of it) or if the token leaks into the file NAME (which IS logged)."""
    outbox = tmp_path / "outbox"
    asyncio.run(FileOutboxEmailClient(outbox).send(_MESSAGE))

    files = list(outbox.iterdir())
    assert len(files) == 1
    assert "abc.def" not in files[0].name
    content = files[0].read_text(encoding="utf-8")
    assert "To: someone@example.com" in content
    assert "token=abc.def" in content


def test_email_client_module_has_only_the_tolerated_app_import() -> None:
    """The seam contract: the request-id logging helper is the ONE tolerated ``app.*`` import."""
    source = Path(email_client_module.__file__).read_text(encoding="utf-8")
    app_imports = re.findall(r"^from app\.\S+ import .*$", source, re.MULTILINE)
    assert app_imports == ["from app.core.middleware import get_current_request_id"]


# ---------------------------------------------------------------------------
# Settings guards + client selection
# ---------------------------------------------------------------------------


def _local(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "environment": "local",
        "llm_provider": "stub",
        "embedding_provider": "stub",
        "resend_api_key": None,
        "email_from": None,
        "email_outbox_dir": None,
        "magic_link_base_url": None,
    }
    kwargs.update(overrides)
    return kwargs


def _production(**overrides: object) -> dict[str, object]:
    return _local(
        environment="production",
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        demo_api_key="a-strong-demo-key-not-the-default-1234",
        admin_api_key="a-strong-admin-key-not-the-default-1234",
        **overrides,
    )


def test_resend_key_without_a_sender_is_rejected_in_any_environment() -> None:
    """RED if ``_require_email_from_with_resend_key`` is removed."""
    with pytest.raises(ValidationError, match="CITEVYN_EMAIL_FROM"):
        Settings(**_local(resend_api_key="re_x"))


def test_production_with_resend_requires_the_magic_link_base_url() -> None:
    """RED if ``_require_magic_link_base_url_in_production`` is removed --
    every emailed link would point at localhost."""
    with pytest.raises(ValidationError, match="CITEVYN_MAGIC_LINK_BASE_URL"):
        Settings(**_production(resend_api_key="re_x", email_from="a@example.com"))
    Settings(
        **_production(
            resend_api_key="re_x",
            email_from="a@example.com",
            magic_link_base_url="https://citevyn.example",
        )
    )


def test_the_file_outbox_is_refused_in_production() -> None:
    """RED if ``_reject_email_outbox_in_production`` is removed: production would
    write every sign-in link to local disk and deliver nothing."""
    with pytest.raises(ValidationError, match="CITEVYN_EMAIL_OUTBOX_DIR"):
        Settings(**_production(email_outbox_dir="/tmp/outbox"))


def test_client_selection_prefers_resend_then_outbox_then_none(tmp_path: Path) -> None:
    """RED if production-without-a-provider silently falls back to the outbox."""
    resend = _build_email_client(
        Settings(**_local(resend_api_key="re_x", email_from="a@example.com"))
    )
    assert isinstance(resend, ResendEmailClient)

    outbox = _build_email_client(Settings(**_local(email_outbox_dir=str(tmp_path))))
    assert isinstance(outbox, FileOutboxEmailClient)
    assert outbox.directory == tmp_path

    default_outbox = _build_email_client(Settings(**_local()))
    assert isinstance(default_outbox, FileOutboxEmailClient)
    assert default_outbox.directory.name == "citevyn_email_outbox"

    assert _build_email_client(Settings(**_production())) is None


def test_the_magic_link_limit_is_wired_through_the_in_process_limiter_and_settings_match() -> None:
    """RED if ``_MAGIC_LINK_ROLE`` is dropped from the in-process limiter's
    ``_limits`` or from ``_settings_match`` (a config change would then never
    rebuild the limiter). The Redis limiter is covered in
    ``test_redis_rate_limit.py``."""
    from app.core.rate_limit import RateLimiter, _settings_match, get_limiter, reset_limiter

    reset_limiter()
    try:
        first = get_limiter(Settings(**_local(rate_limit_magic_link_per_hour=5)))
        assert first.limit_for(role="magic_link") == 5
        changed = Settings(**_local(rate_limit_magic_link_per_hour=7))
        assert not _settings_match(first, changed)
        assert get_limiter(changed).limit_for(role="magic_link") == 7
    finally:
        reset_limiter()

    with pytest.raises(ValueError, match="magic_link_per_window"):
        RateLimiter(
            window_seconds=60, demo_user_per_window=1, admin_per_window=1, magic_link_per_window=0
        )
