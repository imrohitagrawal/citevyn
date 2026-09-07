"""Tests for ``app.core.email_client`` and the magic-link ``Settings`` guards (ADR-0004 PR 14).

Hermetic: the Resend client is exercised through ``httpx.MockTransport``,
this codebase's established pattern for provider HTTP clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
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
from app.core.logging import build_log_formatter

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


# ---------------------------------------------------------------------------
# What production actually PRINTS on a delivery failure (#296)
# ---------------------------------------------------------------------------


def _rendered_email_log(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record, rendered the way production renders it.

    ALL of them, joined -- not ``records[0]``, and NOT filtered by logger name.

    Two review findings, one after the other. First, reading ``records[0]`` let
    a decoy warning emitted just before the real one satisfy the PII assertion
    while the real line printed the recipient's address -- a "second mechanism
    supplying the observation", one of this repo's five recorded ways a test
    passes for the wrong reason.

    Then the fix for that kept an ``r.name == "citevyn.email"`` filter, and the
    next round leaked the same body on ``citevyn.http`` instead: the address
    reached a production log line and all 1889 backend tests stayed green. The
    upstream body is PII-bearing whichever logger emits it, so this looks at
    every record the test captured. The "did it log anything at all" partner
    still checks the email logger specifically.

    Rendered with ``build_log_formatter()``, the formatter object production
    installs -- not ``logging.Formatter(LOG_FORMAT)``. Since #361 those differ:
    production's subclass APPENDS the ``extra=`` fields, so a plain
    ``logging.Formatter`` would silently drop exactly the ``body`` field these
    assertions exist to police, and the PII test below would pass by asserting
    against output nobody receives.
    """
    formatter = build_log_formatter()
    return "\n".join(formatter.format(r) for r in caplog.records)


def test_a_resend_failure_logs_the_status_code_in_the_message_itself(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The status code must be in the MESSAGE STRING, not only in ``extra=``.

    Asserted on ``record.getMessage()``, deliberately -- NOT on the rendered
    line. Since #361 made the formatter emit ``extra=`` fields, the rendered
    line carries the status code TWICE, so an assertion on it is satisfied by
    either mechanism and pins neither. Review demonstrated exactly that: with
    ``status_code=%s`` deleted from the message string the whole file stayed
    green, and the docstring's old "RED if the status code lives only in
    ``extra=``" was therefore false. The message-string copy is #296's fix and
    the deliberate second mechanism; this is what holds it in place.

    RED if ``status_code`` is removed from the message-string arguments. The
    ``extra=`` copy has its own assertion in the PII test below.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "The domain is not verified."})

    with (
        caplog.at_level(logging.WARNING, logger="citevyn.email"),
        pytest.raises(EmailDeliveryError),
    ):
        asyncio.run(_resend(handler).send(_MESSAGE))

    email_records = [r for r in caplog.records if r.name == "citevyn.email"]
    assert email_records, "the failure logged nothing on the email logger at all"
    # `getMessage()` is the `%`-interpolated MESSAGE only; `extra=` never
    # reaches it, so this isolates the mechanism under test.
    messages = "\n".join(r.getMessage() for r in email_records)
    assert "resend_send_error" in messages
    assert "422" in messages, f"the message string itself carries no status code: {messages!r}"


def test_the_resend_failure_line_never_carries_the_upstream_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Resend echoes the RECIPIENT'S EMAIL ADDRESS in some 4xx bodies.

    Measured: ``redact_value("body", ...)`` returns an email address unchanged
    (``body`` matches no entry in ``RAW_TEXT_KEYS`` or ``SECRET_KEY_PARTS``, and
    the 32-char entropy sweep does not fire on an address). So the body must not
    be promoted into the message string -- the status code is the actionable
    part. RED if a future edit folds ``response.text`` into the rendered line.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "You can only send testing emails to victim@example.com."},
        )

    with (
        caplog.at_level(logging.WARNING, logger="citevyn.email"),
        pytest.raises(EmailDeliveryError),
    ):
        asyncio.run(_resend(handler).send(_MESSAGE))

    rendered = _rendered_email_log(caplog)
    assert rendered, "the failure logged nothing at all"
    assert "victim@example.com" not in rendered, f"PII leaked into the log line: {rendered!r}"
    assert "403" in rendered
    # PARTNER, and the fix for a vacuity review found here: without this the
    # "address is absent" assertion holds trivially if the `extra=` dict is
    # deleted from the call site altogether -- proved by deleting it and
    # watching all 1999 backend tests stay green. This asserts the body IS
    # passed and IS suppressed, which is the actual claim.
    assert "body=<str len=" in rendered, (
        f"the call site no longer passes the upstream body, so the redaction "
        f"assertion above proves nothing: {rendered!r}"
    )


_FORMATTER_PROBE = """
import json, sys
sys.path.insert(0, %r)
import logging
from app.core.logging import LOG_FORMAT, build_log_formatter, configure_logging
configure_logging()
installed = [h.formatter for h in logging.getLogger().handlers if h.formatter]
# The record an operator would actually receive: one bounded field, one
# PII-bearing upstream body, rendered by the INSTALLED formatter object.
record = logging.LogRecord(
    "citevyn.email", logging.WARNING, "email_client.py", 1, "resend_send_error", None, None
)
record.status_code = 403
record.body = "You can only send testing emails to victim@example.com."
print(json.dumps({
    "log_format": LOG_FORMAT,
    "fmts": [f._fmt for f in installed],
    "classes": [type(f).__name__ for f in installed],
    "expected_class": type(build_log_formatter()).__name__,
    "lines": [f.format(record) for f in installed],
}))
"""


def test_configure_logging_installs_log_format_on_the_root_handler() -> None:
    """Partner: the two tests above are meaningless if LOG_FORMAT drifts.

    They render with ``LOG_FORMAT``; production renders with whatever
    ``configure_logging`` actually installs. If those stop being the same
    string, the assertions above would describe a formatter nobody uses.

    This EXECUTES ``configure_logging`` and inspects the emitted formatter
    rather than grepping ``logging.py`` for ``format=LOG_FORMAT``. The grep
    version was this repo's recorded "guards that check strings" defect, and
    review demonstrated two bypasses: keeping the literal in a ``# was:``
    comment above a changed line, and parking it in an unused function. Both
    left the partner green while production emitted a different format -- in
    the comment case, one that dropped the message entirely, a worse version of
    the very #296 symptom this file exists for.

    A subprocess because ``logging.basicConfig`` is a no-op once the root
    logger has handlers, and pytest has already given it several -- so an
    in-process call would assert against pytest's formatter, not production's.

    Since #361 the format STRING is no longer sufficient on its own: the extras
    are appended by a Formatter SUBCLASS, not by a ``%``-placeholder, so a
    matching ``_fmt`` on a plain ``logging.Formatter`` would drop every
    ``extra=`` field while this assertion stayed green. So the probe also
    renders a real record THROUGH THE INSTALLED OBJECT and asserts on the
    resulting line.

    RED if ``configure_logging`` goes back to ``basicConfig(format=...)``, if
    the installed class stops being ``build_log_formatter()``'s, or if the
    ``body`` field's content reaches the emitted line.
    """
    backend = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-c", _FORMATTER_PROBE % str(backend)],
        capture_output=True,
        text=True,
        cwd=str(backend),
        check=True,
    )
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["fmts"], "configure_logging() installed no root handler with a formatter"
    assert data["fmts"] == [data["log_format"]], (
        f"configure_logging installs {data['fmts']!r} but this file renders with "
        f"{data['log_format']!r}; the rendered-line assertions above no longer "
        f"describe production output."
    )
    assert data["classes"] == [data["expected_class"]], (
        f"configure_logging installs {data['classes']!r}, but this file renders through "
        f"{data['expected_class']!r}. A plain logging.Formatter with the same _fmt drops "
        f"every extra= field, which is exactly the #361 defect."
    )
    (line,) = data["lines"]
    # Positive half: the operator gets the diagnosis.
    assert "resend_send_error" in line and "status_code=403" in line, (
        f"the installed formatter dropped the extra= fields: {line!r}"
    )
    # Negative half: the upstream body's CONTENT never reaches the line.
    assert "victim@example.com" not in line, f"PII leaked into production output: {line!r}"
    assert "body=<str len=" in line, (
        f"the suppressed body must still be visibly ACCOUNTED FOR, not silently "
        f"dropped -- got {line!r}"
    )
