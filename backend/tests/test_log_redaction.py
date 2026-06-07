from app.core.logging import SECRET_VALUE, TEXT_VALUE, build_log_event, redact_mapping


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
