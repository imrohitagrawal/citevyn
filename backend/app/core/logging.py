import logging
import re
from collections.abc import Mapping
from typing import Any, cast

SECRET_VALUE = "[REDACTED]"
TEXT_VALUE = "[REDACTED_TEXT]"

SECRET_KEY_PARTS = (
    "authorization",
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "private_key",
)
RAW_TEXT_KEYS = (
    "question",
    "message",
    "content",
    "chunk",
    "chunk_text",
    "retrieved_chunk",
    "retrieved_chunks",
    "context",
)

BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
HIGH_ENTROPY_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{32,}\b")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def redact_value(key: str, value: Any) -> Any:
    key_lower = key.lower()

    if any(part in key_lower for part in RAW_TEXT_KEYS):
        return TEXT_VALUE

    if any(part in key_lower for part in SECRET_KEY_PARTS):
        return SECRET_VALUE

    if isinstance(value, Mapping):
        return redact_mapping(cast(Mapping[str, Any], value))

    if isinstance(value, list):
        return [redact_value(key, item) for item in cast(list[Any], value)]

    if isinstance(value, str):
        redacted = BEARER_RE.sub(f"Bearer {SECRET_VALUE}", value)
        return HIGH_ENTROPY_RE.sub(SECRET_VALUE, redacted)

    return value


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: redact_value(key, value) for key, value in values.items()}


def build_log_event(event: str, **fields: Any) -> dict[str, Any]:
    return redact_mapping({"event": event, **fields})
