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
    # ADR-0004 (login): a session cookie value is a bearer credential, and an
    # email is personal data neither previously existed on any request path
    # this logger saw. Redacting by key name (not by entropy) is deliberate:
    # ``HIGH_ENTROPY_RE`` does not catch an Argon2 PHC string (it splits on
    # ``$`` into sub-32-char segments) and a short, common email would not
    # trip an entropy heuristic at all.
    "cookie",
    "email",
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


# Query-string parameters that ARE credentials. uvicorn's access log records
# the full request line -- path AND query string -- so without this a
# magic-link click (``GET /v1/auth/magic-link/confirm?token=<id>.<secret>``,
# ADR-0004 PR 14) would write the whole sign-in credential into the server
# log, and production runs with ``--access-log`` (infra/docker/Dockerfile.api).
# The app's own request log (``app.core.middleware``) only ever records the
# path, so this is specifically for uvicorn's logger. ``code`` is the OAuth
# authorization code on the callback URL; ``state`` is a lookup key, not a
# credential, and stays visible for debugging. Found live, not by review.
QUERY_CREDENTIAL_RE = re.compile(r"((?:^|[?&])(?:token|code)=)[^&\s\"']*")


class RedactQueryCredentialsFilter(logging.Filter):
    """Redact credential-bearing query parameters in a log record's message/args.

    uvicorn logs ``'%s - "%s %s HTTP/%s" %d'`` with the path-with-query as an
    ARG, not in ``msg``, so both are rewritten. Never drops a record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = QUERY_CREDENTIAL_RE.sub(rf"\g<1>{SECRET_VALUE}", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                QUERY_CREDENTIAL_RE.sub(rf"\g<1>{SECRET_VALUE}", arg)
                if isinstance(arg, str)
                else arg
                for arg in cast(tuple[Any, ...], record.args)
            )
        return True


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Installed on the logger uvicorn writes access lines to. Idempotent:
    # create_app() may run more than once per process (tests).
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactQueryCredentialsFilter) for f in access_logger.filters):
        access_logger.addFilter(RedactQueryCredentialsFilter())


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
