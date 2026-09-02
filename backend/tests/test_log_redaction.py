import logging

from app.core.logging import (
    SECRET_VALUE,
    TEXT_VALUE,
    RedactQueryCredentialsFilter,
    build_log_event,
    configure_logging,
    redact_mapping,
)


def test_redaction_masks_secret_fields() -> None:
    event = redact_mapping(
        {
            "authorization": "Bearer demo-token",
            "api_key": "sk-example",
            "password": "correct-horse-battery-staple",
            "private_key": "-----BEGIN PRIVATE KEY-----",
        }
    )

    assert event["authorization"] == SECRET_VALUE
    assert event["api_key"] == SECRET_VALUE
    assert event["password"] == SECRET_VALUE
    assert event["private_key"] == SECRET_VALUE


def test_redaction_masks_bearer_tokens_inside_strings() -> None:
    event = redact_mapping({"detail": "client sent Authorization: Bearer abc.def.ghi"})

    assert event["detail"] == f"client sent Authorization: Bearer {SECRET_VALUE}"


def test_redaction_masks_long_high_entropy_strings() -> None:
    event = redact_mapping({"detail": "value abcdefghijklmnopqrstuvwxyzABCDEF1234567890"})

    assert event["detail"] == f"value {SECRET_VALUE}"


def test_redaction_masks_raw_question_and_retrieved_chunks() -> None:
    event = build_log_event(
        "answer_attempted",
        request_id="req_123",
        question="How do I configure a token?",
        retrieved_chunks=["official doc chunk text"],
    )

    assert event["request_id"] == "req_123"
    assert event["question"] == TEXT_VALUE
    assert event["retrieved_chunks"] == TEXT_VALUE


def test_redaction_keeps_harmless_fields() -> None:
    event = build_log_event(
        "request_completed",
        request_id="req_123",
        method="GET",
        path="/health",
        status_code=200,
    )

    assert event == {
        "event": "request_completed",
        "request_id": "req_123",
        "method": "GET",
        "path": "/health",
        "status_code": 200,
    }


# ---------------------------------------------------------------------------
# uvicorn access-log redaction (ADR-0004 PR 14)
# ---------------------------------------------------------------------------


def _uvicorn_access_record(path_with_query: str) -> logging.LogRecord:
    """A record shaped exactly like uvicorn's h11/httptools access line."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1", "GET", path_with_query, "1.1", 200),
        exc_info=None,
    )


def test_access_log_filter_redacts_the_magic_link_token_from_the_request_line() -> None:
    """Found live: uvicorn logged ``GET /v1/auth/magic-link/confirm?token=<id>.<secret>``
    verbatim -- the whole sign-in credential. RED if the filter is removed or
    stops rewriting ``record.args`` (uvicorn passes the path as an ARG)."""
    secret = "a" * 32 + "." + "b" * 64
    record = _uvicorn_access_record(f"/v1/auth/magic-link/confirm?token={secret}")
    assert RedactQueryCredentialsFilter().filter(record) is True
    line = record.getMessage()
    assert secret not in line
    assert "/v1/auth/magic-link/confirm?token=[REDACTED]" in line


def test_access_log_filter_redacts_oauth_code_but_keeps_state_and_other_params() -> None:
    record = _uvicorn_access_record("/v1/auth/oauth/github/callback?code=SECRETCODE&state=nonce123")
    RedactQueryCredentialsFilter().filter(record)
    line = record.getMessage()
    assert "SECRETCODE" not in line
    assert "code=[REDACTED]" in line
    assert "state=nonce123" in line
    plain = _uvicorn_access_record("/v1/sessions?channel=chat")
    RedactQueryCredentialsFilter().filter(plain)
    assert "channel=chat" in plain.getMessage()


def test_configure_logging_installs_the_filter_on_uvicorns_access_logger_once() -> None:
    """RED if configure_logging() stops attaching the filter, or attaches a
    duplicate on every create_app() (tests build the app many times)."""
    access = logging.getLogger("uvicorn.access")
    for existing in [f for f in access.filters if isinstance(f, RedactQueryCredentialsFilter)]:
        access.removeFilter(existing)
    configure_logging()
    configure_logging()
    installed = [f for f in access.filters if isinstance(f, RedactQueryCredentialsFilter)]
    assert len(installed) == 1
    record = _uvicorn_access_record("/v1/auth/magic-link/confirm?token=abc.def")
    assert all(f.filter(record) for f in access.filters)
    assert "abc.def" not in record.getMessage()
