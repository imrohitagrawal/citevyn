import logging
import re
from collections.abc import Mapping, Sized
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

# Keys whose value is an OPAQUE CORRELATION ID, not a credential, and which the
# entropy sweep would otherwise destroy. Matched on the WHOLE lowered key, not
# as a substring, so this is exactly these three names and nothing that merely
# contains them.
#
# Measured, and the reason this exists: a production request id is
# ``f"req_{uuid.uuid4().hex}"`` (``app.core.middleware``) -- 36 characters, every
# one of them inside ``HIGH_ENTROPY_RE``'s class -- so the sweep blanked it to
# ``[REDACTED]`` on EVERY line. The API hands the caller that id in
# ``ErrorEnvelope.request_id`` and an SRE greps the logs for it; the grep
# returned nothing. Same for a sha-shaped ``index_version`` and
# ``source_version_hash``. Two independent reviewers found this within minutes,
# and the test that "proved" request_id survived used the 10-character literal
# ``req_abc123``, a value the application never produces (#361).
#
# THE TRADEOFF, stated: ``request_id`` can be supplied by the caller
# (``middleware.py`` honours an inbound ``X-Request-ID`` header), so exempting it
# prints a client-controlled string. That is the client's OWN value echoed back
# -- it discloses none of our secrets -- the ``Bearer`` sweep below still runs on
# it, and the formatter's ``repr`` stops it forging a second log line. Losing
# every correlation id is the far worse trade.
#
# It IS capped, though. Before this exemption the entropy sweep collapsed a long
# value to ``[REDACTED]``, so nothing else bounded the length -- and a client
# sending a 4 kB ``X-Request-ID`` would then have written a 4 kB line into a
# metered log pipeline, on the ``build_log_event`` path where
# ``MAX_EMITTED_TEXT`` does not reach. No real id comes close to the cap: the
# longest is a 128-character ``source_version_hash``.
OPAQUE_ID_KEYS = frozenset({"request_id", "index_version", "source_version_hash"})
MAX_OPAQUE_ID = 160


# Query-string parameters that ARE credentials. uvicorn's access log records
# the full request line -- path AND query string -- so without this a
# magic-link click (``GET /v1/auth/magic-link/confirm?token=<id>.<secret>``,
# ADR-0004 PR 14) would write the whole sign-in credential into the server
# log, and production runs with ``--access-log`` (infra/docker/Dockerfile.api).
# The app's own request log (``app.core.middleware``) only ever records the
# path, so this is specifically for uvicorn's logger. ``code`` is the OAuth
# authorization code on the callback URL; ``state`` is a lookup key, not a
# credential, and stays visible for debugging. Found live, not by review.
#
# ``password``/``auth``/``secret``/``api_key`` were added for #361: redis-py
# accepts the credential as a QUERY PARAMETER
# (``redis://host:6379/0?password=…``, honoured including over ``rediss://``),
# and ``_redact_url`` in ``app.core.redis_client`` only peeled the ``user:pass@``
# userinfo form -- so that DSN shape printed the password in full on every boot
# once the formatter started emitting ``extra=`` fields. Reused here rather than
# written twice: the uvicorn access log wants exactly the same list.
QUERY_CREDENTIAL_RE = re.compile(
    r"((?:^|[?&])(?:token|code|password|passwd|auth|secret|api_key|apikey)=)[^&\s\"']*",
    re.IGNORECASE,
)


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


# The production log format, named so tests can render a record exactly the way
# an operator reading `fly logs` receives it. It renders the message and
# nothing else; the `extra=` fields are appended by ``RedactingExtraFormatter``
# below, NOT by a `%`-placeholder, because which fields exist varies per call
# site and a missing placeholder key raises inside `format()`.
LOG_FORMAT = "%(message)s"

# Every attribute the stdlib puts on a LogRecord, DERIVED from a real record on
# the running interpreter rather than hard-coded. A hard-coded list rots with
# the Python version -- 3.12 added ``taskName``, and a stale list would append a
# spurious ``taskName=None`` to every single production line (#361). ``message``
# and ``asctime`` are set by ``Formatter.format`` *after* this set is built, so
# they are added explicitly.
RESERVED_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
}

# ── Which `extra=` values may be printed VERBATIM ──────────────────────────
#
# This is an ALLOWLIST, and it is an allowlist on purpose. #361's whole trap is
# that turning the extras on converts a dropped log into a PII/secret leak:
# `redact_value` matches on the KEY NAME, so an email address nested inside an
# upstream response `body` sails straight through, and a Resend-shaped key
# (`re_` + 24 chars = 27) is under `HIGH_ENTROPY_RE`'s 32-char floor. Both were
# measured, not assumed.
#
# A denylist of secret SHAPES cannot be made complete -- every added regex is a
# guess about the next provider's key format -- so the safety here is
# STRUCTURAL instead of pattern-based:
#
#   * a bool/int/float/None is bounded by construction and is always printed;
#   * a string is printed only if its key is named below (a deliberate,
#     reviewed decision per key) and it still survives `redact_value`;
#   * every other value is replaced by a SHAPE SUMMARY -- `body=<str len=57>`.
#
# The shape summary is the point: an unlisted field is *visibly suppressed*,
# never silently dropped. An operator sees that the field exists and how big it
# was, which is strictly more than today (today the whole `extra=` dict
# vanishes), and adding a key here is then a conscious review decision rather
# than an accident. Free text from an upstream provider cannot reach the log
# line at all, whatever shape the secret inside it takes.
#
# SCOPE, because the sentence above is otherwise stronger than the evidence:
# this governs the ``extra=`` path only. ``build_log_event`` below has NO
# allowlist -- it redacts by key name and returns a dict the caller logs as the
# message -- so a caller passing free text there still prints it in full. One
# site does (``api/routes/about.py``, an OSError string over a repo-local file,
# never upstream output) and says so in its own docstring. Routing
# ``build_log_event`` through this allowlist would change the three sites that
# use it and is deliberately not done here.
EMITTED_TEXT_KEYS = frozenset(
    {
        # Correlation + routing.
        "request_id",
        "path",
        "endpoint",
        "source",
        # Configuration identity (settings values, never credentials).
        "environment",
        "provider",
        "llm_provider",
        "embedding_provider",
        "model",
        "kind",
        "call_site",
        "status",
        # Index / embedder provenance (retrieval + promotion diagnostics).
        "index_version",
        "index_stamp_status",
        "index_embedding_provider",
        "index_embedding_model",
        "configured_embedding_provider",
        "configured_embedding_model",
        # Exception CLASS names. Deliberately not `cause`/`error`/`reason`,
        # which carry `str(exc)` -- see the call sites, which now pass a
        # `_type` field alongside.
        "cause_type",
        "error_type",
        # Decimal money amounts, stringified by the call site.
        "spend_usd",
        # Already redacted before it reaches `extra=` (`_redact_url`).
        "url",
        "old_url",
    }
)

# A backstop cap on any single emitted string. It rarely fires -- every
# allowlisted key is short by construction, and `redact_value`'s entropy sweep
# collapses most long values first -- so the real volume control is the
# allowlist, not this number. It stays because a pathological provider id
# should cost one truncated field rather than a multi-kilobyte line.
MAX_EMITTED_TEXT = 200

# Anything outside this is replaced in the emitted KEY. `repr` guards the value
# from forging a second log line; the key had no such guard, and a key
# containing a newline could write a plausible fake WARNING beneath a real line.
# No call site can produce one today (keys are source literals), which is
# exactly why it is worth one cheap regex rather than an argument.
UNSAFE_KEY_CHARS_RE = re.compile(r"[^A-Za-z0-9_.-]")


def render_extra_value(key: str, value: Any) -> str:
    """Render one ``extra=`` value for the production log line.

    Fails CLOSED: anything not provably bounded becomes a shape summary.
    """
    # EXACT types, not `isinstance`. An `int` subclass can override `__str__`
    # ("class Sneaky(int): def __str__(self): return <a secret>") and would
    # otherwise be printed verbatim under any key at all. `bool` is listed
    # because it IS an int subclass and is genuinely bounded.
    if value is None or type(value) in (bool, int, float):
        return str(value)

    # The allowlist applies to STRINGS ONLY. `str()`-ing a container first threw
    # away `redact_value`'s own per-inner-key recursion, so a dict or list under
    # an innocent allowlisted key (`path={"token": ...}`) printed its full
    # contents -- the exact opposite of the fail-closed invariant this module's
    # header asserts. Unreachable at every current call site; found by review.
    if isinstance(value, str):
        if key.lower() in EMITTED_TEXT_KEYS:
            redacted = redact_value(key, value)
            rendered = redacted if isinstance(redacted, str) else str(redacted)
            if len(rendered) > MAX_EMITTED_TEXT:
                rendered = rendered[:MAX_EMITTED_TEXT] + "…<truncated>"
            # `repr`, not the bare string: it escapes newlines, so a value can
            # never forge a second log line, and it makes an empty value visible.
            return repr(rendered)
        return f"<str len={len(value)}>"

    type_name = type(value).__name__
    if isinstance(value, Sized):
        return f"<{type_name} n={len(value)}>"
    return f"<{type_name}>"


def render_extras(record: logging.LogRecord) -> str:
    """Return the ``key=value`` suffix for a record's ``extra=`` fields.

    Never raises. A ``TypeError`` escaping a formatter makes logging DROP the
    record and print ``--- Logging error ---`` to stderr, which would be a
    worse version of the very #361 symptom this exists to fix, so every value
    is rendered inside its own guard.
    """
    parts: list[str] = []
    # `list(...)`: iterating the live ``__dict__`` would raise
    # "dictionary changed size during iteration" OUTSIDE the per-field guard,
    # which is the one way this function could still drop a line.
    for key, value in list(record.__dict__.items()):
        if key in RESERVED_RECORD_ATTRS:
            continue
        safe_key = UNSAFE_KEY_CHARS_RE.sub("_", key)
        try:
            parts.append(f"{safe_key}={render_extra_value(key, value)}")
        except Exception as exc:  # noqa: BLE001 - a bad field must not drop the line
            # Name the type: a bare "<unrenderable>" tells the operator who hits
            # it precisely nothing about which object misbehaved.
            parts.append(f"{safe_key}=<unrenderable {type(exc).__name__}>")
    if not parts:
        return ""
    return " " + " ".join(parts)


class RedactingExtraFormatter(logging.Formatter):
    """``LOG_FORMAT`` plus the record's ``extra=`` fields, redacted.

    ``formatMessage`` (not ``format``) is overridden so the fields land
    directly after the message and BEFORE any exception traceback, which is
    where a reader expects them.
    """

    def formatMessage(self, record: logging.LogRecord) -> str:
        return super().formatMessage(record) + render_extras(record)


def build_log_formatter() -> logging.Formatter:
    """The exact formatter production installs. Tests render through this."""
    return RedactingExtraFormatter(LOG_FORMAT)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(build_log_formatter())
    # `handlers=` rather than `format=`: basicConfig's own formatter is a plain
    # `logging.Formatter`, which drops `extra=` entirely. Still a no-op when the
    # root logger already has handlers, exactly as before.
    logging.basicConfig(level=logging.INFO, handlers=[handler])
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
        # An opaque correlation id keeps the Bearer sweep and skips the entropy
        # sweep, which would otherwise blank the whole value -- see
        # OPAQUE_ID_KEYS for the measurement and the tradeoff.
        if key_lower in OPAQUE_ID_KEYS:
            if len(redacted) > MAX_OPAQUE_ID:
                return redacted[:MAX_OPAQUE_ID] + "…<truncated>"
            return redacted
        return HIGH_ENTROPY_RE.sub(SECRET_VALUE, redacted)

    return value


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: redact_value(key, value) for key, value in values.items()}


def build_log_event(event: str, **fields: Any) -> dict[str, Any]:
    return redact_mapping({"event": event, **fields})
