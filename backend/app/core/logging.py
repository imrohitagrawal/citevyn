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
# as a substring, so this is exactly these four names and nothing that merely
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
# ``MAX_EMITTED_TEXT`` does not reach. No real SINGLE id comes close to the cap:
# the longest is a 128-character ``source_version_hash``. (A JOINED value can:
# see ``index_versions`` below, where two 64-character versions already make 129
# and three overflow the cap.)
#
# ``index_versions`` (plural, #352) is the comma-joined set of index versions one
# answer's evidence spanned. It is the same data as ``index_version`` under a
# different cardinality, so it needs the same exemption -- and because this set is
# matched on the WHOLE key it does not inherit one. The joined value is bounded by
# ``MAX_OPAQUE_ID`` like the others; on a database with enough simultaneously-active
# sha-shaped indexes to overflow 160 characters the value truncates and the
# ``index_version_count`` int emitted alongside it stays exact.
OPAQUE_ID_KEYS = frozenset({"request_id", "index_version", "index_versions", "source_version_hash"})
MAX_OPAQUE_ID = 160


# Keys whose value is a URL PATH, swept ONE ``/``-DELIMITED SEGMENT AT A TIME
# instead of as one string. Matched on the WHOLE lowered key, the same
# discipline as ``OPAQUE_ID_KEYS`` above.
#
# PRECEDENCE, since two key-name sets now guard the same branch:
# ``redact_value`` tests ``OPAQUE_ID_KEYS`` FIRST, so a key listed in both
# would take the opaque-id branch and skip the segment sweep entirely -- on a
# collision the WEAKER rule wins, silently. The two sets are disjoint today
# (``PATH_KEYS & OPAQUE_ID_KEYS == frozenset()``, verified); keep them
# disjoint, or reorder the branches deliberately and say why.
#
# THE DEFECT (#384), seen in the Fly v17 production logs: ``HIGH_ENTROPY_RE``'s
# character class contains ``/``, so a path is one contiguous run and the whole
# thing collapses. Every session route printed as
#
#     request_completed ... 'path': '/[REDACTED]'
#
# An operator reading ``fly logs`` could not tell an answer POST from a session
# DELETE from a 404 -- the app's own request log became useless for exactly the
# routes that carry the product. ``/health`` (7 characters) still printed, which
# is why this survived review: the short paths look fine.
#
# WHY THE SCOPE IS A KEY NAME AND NOT THE VALUE'S SHAPE. The obvious cheap
# trigger is ``value.startswith("/")``. Rejected, and measured: that trigger is
# ATTACKER-INFLUENCED. Roughly one in sixty-five random base64 values begins
# with ``/`` -- independent 200,000-sample draws gave 1.58%, 1.54% and 1.52% --
# so a secret could select its own weaker redaction rule by its first
# character. A key name is chosen in our source; a value is not.
#
# WHY THE SCOPE IS NARROW, measured. Applying the per-segment sweep to EVERY
# string is not a mild loosening -- it is a hole. About HALF of random
# 44-character base64 values (``b64encode`` of 32 bytes) contain a ``/``, and
# of those, every ``/``-delimited segment is under 32 characters often enough
# that ABOUT THREE IN TEN of all such values would print IN FULL. At 40
# characters it is about a third. A concrete leaker:
# ``AKIAxxxxxxxxx/yyyyyyyyyyyyy/zzzzzzzzzzzz``.
#
# Those three figures are rounded ON PURPOSE, because they do not reproduce to
# two decimal places: independent 200,000-sample draws gave 48.51% / 48.32% /
# 48.45% for "contains a ``/``", 28.35% / 29.19% / 29.51% / 29.34% for "prints
# in full at 44 characters", and 32.12% / 32.04% / 32.31% / 32.15% at 40. A
# reader re-deriving them should expect the first decimal to move.
#
# ONE figure here is exact and it is the load-bearing one. The per-segment
# sweep applied to every string is not merely similar to deleting ``/`` from
# ``HIGH_ENTROPY_RE``'s class -- it is INDISTINGUISHABLE from it: 0 differing
# inputs over 400,000 sampled strings, reproduced EXACTLY, at full precision,
# by an independent agent on an independent sample. So the key-name scope is
# not a stylistic preference; it is the ONLY thing separating this fix from
# the rejected one.
#
# ``endpoint`` is deliberately NOT in this set even though it is allowlisted
# alongside ``path``. Its values are source literals (``"embeddings"``,
# ``"embedContent"``, ``"batchEmbedContents"``) that the entropy sweep never
# touches, so adding it would widen the attack surface above for zero benefit.
#
# WHY ``MAX_PATH`` EXISTS. The whole-string entropy collapse was, by accident,
# the only thing bounding the PATH FIELD on the ``build_log_event`` path --
# ``MAX_EMITTED_TEXT`` governs the ``extra=`` path only and does not reach it
# (see the SCOPE paragraph on ``EMITTED_TEXT_KEYS``). A 4,400-character path of
# short segments emits 11 characters today and would emit 4,400 under an
# uncapped per-segment rule. The cap restores that bound explicitly rather than
# as a side effect, using the same truncation shape as ``MAX_OPAQUE_ID``.
#
# NARROWED to "the path field" on review: an earlier draft claimed the collapse
# was the only thing bounding ANY client-controlled value here, and that is
# false. ``app/core/middleware.py`` passes ``method=request.method`` on the SAME
# ``build_log_event`` call. It is equally client-controlled and it is uncapped
# at BOTH revisions -- unchanged by this fix, and not fixed by it.
# ``redact_value("method", "!" * 14700)`` returns all 14,700 characters, before
# and after, because ``!`` is outside ``HIGH_ENTROPY_RE``'s class while still
# being a legal HTTP token character. (``"A" * 14700`` collapses to 10, which
# is presumably why nobody noticed: the obvious probe is the one shape that is
# safe.) It is LATENT, not live. ``infra/docker/Dockerfile.api``'s CMD passes
# no ``--http`` flag, so uvicorn selects httptools, and httptools 0.8.0 refuses
# an unrecognised method outright -- ``HttpParserInvalidMethodError`` for
# ``"!" * 14700`` and for ``"A" * 14700`` alike, measured. h11 accepts both and
# hands the full 14,700 characters to the app, so this becomes reachable the
# moment anything selects h11. Recorded, not fixed: ``method`` is not a path,
# and widening this change to cover it would be the blanket loosening the
# paragraph above rejects.
#
# TWO ``path=`` CALL SITES CARRY A FILESYSTEM PATH, NOT A URL PATH, and their
# output really did change. Both re-verified rather than inherited:
#
#   * ``app/main.py:182`` / ``:185`` (``frontend_bundle_absent`` /
#     ``frontend_bundle_mounted``, ``str(FRONTEND_DIST)``). In the container
#     this is ``/app/frontend_dist`` -- every segment short, so it printed in
#     full BEFORE this change too and production output is unchanged. Only a
#     developer checkout moved, from ``/[REDACTED]`` to its full absolute path.
#   * ``app/core/email_client.py:199`` (``email_outbox_written``, the dev file
#     outbox). ``/var/folders/.../citevyn_email_outbox/<stamp>-<hex>.eml`` went
#     from ``/[REDACTED].eml`` to the full path. The ``.eml`` stem measures 31
#     characters -- ONE under the 32-character floor -- so the filename itself
#     now prints too.
#
# Both are acceptable. The first is a build-time constant.
# ``FileOutboxEmailClient`` is dev-only: ``app/services/notifications.py:61``
# constructs it only when ``settings.environment != "production"``, and a
# ``Settings`` validator refuses it in production outright. Neither filename
# carries message content -- the outbox name is a timestamp plus
# ``secrets.token_hex(4)``, as its own docstring says. What DID change is that
# a developer's home-directory layout now appears in local logs; worth naming,
# and a privacy cost of zero in the deployment this app has.
#
# WHY A PATH SEGMENT IS SAFE TO PRINT. No route carries a credential in a path
# segment: all 11 parameterized routes take a UUID, a sha-shaped
# ``index_version``, or a provider slug. The magic-link token and the OAuth
# ``code``/``state`` are QUERY parameters -- ``URL.path`` never contains the
# query string, and uvicorn's access line, which does, is covered by
# ``QUERY_CREDENTIAL_RE`` above. And a session UUID is not a capability token:
# ``api/routes/messages.py`` and ``api/routes/sessions.py`` both filter on
# ``Session.user_id == user_id`` where ``user_id`` comes from
# ``Depends(resolve_principal)`` -- a cookie-derived ownership principal, NOT
# the constant ``DEMO_USER_ID`` (``app/core/auth_sessions.py``). Knowing the id
# buys nothing without the cookie.
#
# THE RESIDUAL, stated. A client can put a base64-with-``/`` string of its OWN
# into a path segment (it 404s) and see it echoed back into our log, since the
# three-in-ten figure above applies to any value the segment split breaks up.
# That is the client's own value disclosing none of our secrets, bounded by
# ``MAX_PATH``, and it is the identical, already-accepted tradeoff documented
# for ``request_id`` at the ``OPAQUE_ID_KEYS`` comment above.
PATH_KEYS = frozenset({"path"})
MAX_PATH = 160


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
        # The PLURAL of the line above: the comma-joined set of index versions one
        # answer's evidence actually came from (#352). Same data, same disclosure
        # profile -- an admin-chosen `index_versions.index_version` primary key,
        # never a credential -- so it is allowlisted on the same grounds. It is
        # ALSO in `OPAQUE_ID_KEYS`: the exemption there is matched on the WHOLE
        # key, so without it the entropy sweep would blank a sha-shaped version to
        # `[REDACTED]` and this field would be one letter away from the exact #361
        # defect that motivated that set.
        "index_versions",
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
        # A URL path is swept one `/`-delimited SEGMENT at a time, so the route
        # shape survives and only a high-entropy segment blanks -- see
        # PATH_KEYS for the defect (#384), the measurement, and why this is
        # scoped to a key name rather than to the value's shape.
        if key_lower in PATH_KEYS:
            swept = "/".join(
                HIGH_ENTROPY_RE.sub(SECRET_VALUE, segment) for segment in redacted.split("/")
            )
            if len(swept) > MAX_PATH:
                return swept[:MAX_PATH] + "…<truncated>"
            return swept
        return HIGH_ENTROPY_RE.sub(SECRET_VALUE, redacted)

    return value


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: redact_value(key, value) for key, value in values.items()}


def build_log_event(event: str, **fields: Any) -> dict[str, Any]:
    return redact_mapping({"event": event, **fields})
