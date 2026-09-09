"""What an operator reading ``fly logs`` actually receives (#361).

``LOG_FORMAT`` is ``"%(message)s"``, so before this every field passed as
``extra=`` was discarded and 31 call sites across 18 files printed a bare event
name. Turning the fields on is not a one-line change: two shapes were MEASURED
to survive ``redact_value`` and would have converted a dropped log into a PII
leak, which ``code_review.md`` blocks shipping for.

    redact_value("body", '{"to":"someone@example.com",...}')  -> unchanged
    redact_value("body", "re_" + "A"*24)   # 27 chars          -> unchanged

The first survives because ``redact_value`` matches on the KEY NAME and ``body``
is in neither ``SECRET_KEY_PARTS`` nor ``RAW_TEXT_KEYS``; the second because
``HIGH_ENTROPY_RE`` has a 32-character floor.

Every BEHAVIOURAL assertion in this file renders a real ``LogRecord`` through
``build_log_formatter()`` -- the object ``configure_logging`` installs -- and
reads the OUTPUT STRING, never ``record.__dict__``, a substring of
``LOG_FORMAT``, or "was ``redact_value`` called": this repo has repeatedly
shipped guards that pinned a constant while the emitted artifact went
unchecked.

Three tests are deliberately NOT behavioural and say so in their own docstrings
-- they are PINS on `redact_value`'s measured behaviour and on the derived
reserved-attribute set, and they exist to keep the behavioural tests honest.
An earlier version of this paragraph claimed *every* assertion was behavioural,
which was simply false; the repo's "claims stronger than evidence" scar applies
to test-file prose too.

Each negative assertion ("the secret is absent") has a POSITIVE partner in the
same test proving the line was produced and carries the operational content, so
a formatter that redacted everything -- or emitted nothing at all -- fails.
"""

import hashlib
import logging
import re
import uuid

import pytest

from app.core.logging import (
    EMITTED_TEXT_KEYS,
    MAX_EMITTED_TEXT,
    MAX_MODEL_ID,
    MAX_SCANNED_CHARS,
    MODEL_ID_KEYS,
    OPAQUE_ID_KEYS,
    PATH_KEYS,
    QUERY_CREDENTIAL_RE,
    RAW_TEXT_KEYS,
    RESERVED_RECORD_ATTRS,
    SECRET_KEY_PARTS,
    SECRET_VALUE,
    build_log_event,
    build_log_formatter,
)

# Obviously synthetic. Never a real key, and never a real address.
FAKE_RESEND_KEY = "re_" + "A" * 24  # 27 chars -- under HIGH_ENTROPY_RE's floor
FAKE_ADDRESS = "victim@example.com"

# Built the way `app.core.middleware` builds one, NOT a short literal. The first
# version of this file used `"req_abc123"` (10 chars) and every request_id
# assertion passed while production emitted `request_id='[REDACTED]'` on every
# single line -- 36 chars of `[a-z0-9_]` is exactly what `HIGH_ENTROPY_RE`
# matches. Two independent reviewers found it; the test did not, because it was
# measuring a value the application never produces.
REAL_REQUEST_ID = f"req_{uuid.uuid4().hex}"


def render(event: str = "some_event", **fields: object) -> str:
    """The line production prints for ``logger.warning(event, extra=fields)``."""
    record = logging.LogRecord("citevyn.test", logging.WARNING, __file__, 1, event, None, None)
    for key, value in fields.items():
        setattr(record, key, value)
    return build_log_formatter().format(record)


# ---------------------------------------------------------------------------
# 1. The defect itself: extras must reach the operator.
# ---------------------------------------------------------------------------


def test_a_bounded_extra_field_reaches_the_emitted_line() -> None:
    """THE #361 REGRESSION.

    RED if ``build_log_formatter()`` returns a plain ``logging.Formatter``,
    which drops ``extra=`` entirely. (The other half -- that
    ``configure_logging`` INSTALLS this formatter and not a plain one -- is
    pinned by ``test_email_client.py::
    test_configure_logging_installs_log_format_on_the_root_handler``, which runs
    a clean subprocess. An earlier docstring here claimed that bite for itself;
    review demonstrated it did not have it.)
    """
    line = render("resend_send_error", status_code=422)
    assert line == "resend_send_error status_code=422", line


def test_a_record_with_no_extras_is_unchanged() -> None:
    """Non-regression for the ~everything else that logs no extras -- including
    every uvicorn access line, which is redacted by a FILTER and must not
    acquire a suffix. RED if the formatter appends a stray separator or leaks a
    standard LogRecord attribute (``taskName``, ``module``, ...) into the line.
    """
    assert render("app_shutdown") == "app_shutdown"


def test_every_standard_logrecord_attribute_is_suppressed() -> None:
    """Partner for the test above: it would pass vacuously if the reserved set
    were somehow EVERY key. This proves the set is derived from a real record
    and contains the attributes that would otherwise be printed."""
    assert {"taskName", "module", "lineno", "levelname", "msg"} <= RESERVED_RECORD_ATTRS
    assert "status_code" not in RESERVED_RECORD_ATTRS


def test_exception_text_still_follows_the_extra_fields() -> None:
    """``formatMessage`` is overridden, not ``format``, so the fields land
    before the traceback. RED if the override moves to ``format``, which would
    append them after the stack trace."""
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "citevyn.test", logging.ERROR, __file__, 1, "unhandled", None, None
        )
        import sys

        record.exc_info = sys.exc_info()
        record.request_id = "req_1"
    line = build_log_formatter().format(record)
    head, _, tail = line.partition("\n")
    assert head == "unhandled request_id='req_1'", head
    assert "ValueError: boom" in tail


# ---------------------------------------------------------------------------
# 2. The two shapes that were MEASURED to defeat redact_value.
# ---------------------------------------------------------------------------


def test_an_email_address_inside_an_upstream_body_never_reaches_the_line() -> None:
    """SHAPE 1. ``redact_value("body", "...victim@example.com...")`` returns the
    address UNCHANGED -- key-name matching, and an address trips no entropy
    rule. The emitted line must still not carry it.

    RED if ``body`` is added to ``EMITTED_TEXT_KEYS``, or if the fail-closed
    default in ``render_extra_value`` is changed to print unknown strings.
    """
    body = f'{{"message":"You can only send testing emails to {FAKE_ADDRESS}."}}'
    line = render("resend_send_error", status_code=403, body=body)

    assert FAKE_ADDRESS not in line, f"PII reached the log line: {line!r}"
    assert "example.com" not in line, f"a domain fragment reached the log line: {line!r}"
    # PARTNER (non-vacuity): the line exists and is still diagnostic, and the
    # suppressed field is visibly accounted for rather than silently dropped.
    assert "resend_send_error" in line
    assert "status_code=403" in line, f"the actionable field was lost too: {line!r}"
    assert f"body=<str len={len(body)}>" in line, line


def test_a_short_prefixed_api_key_inside_a_body_never_reaches_the_line() -> None:
    """SHAPE 2. ``re_`` + 24 chars is 27 characters, UNDER ``HIGH_ENTROPY_RE``'s
    32-char floor, so ``redact_value("body", key)`` returns it unchanged. The
    emitted line must still not carry it.

    RED under the same changes as the test above. This is the shape a
    shape-based denylist misses, which is why the design is an allowlist.
    """
    assert len(FAKE_RESEND_KEY) == 27, "the fixture must stay under the 32-char entropy floor"
    line = render("resend_send_error", status_code=401, body=f"invalid key {FAKE_RESEND_KEY}")

    assert FAKE_RESEND_KEY not in line, f"a credential reached the log line: {line!r}"
    assert "re_AAAA" not in line, f"a credential PREFIX reached the log line: {line!r}"
    # PARTNER (non-vacuity).
    assert "status_code=401" in line, f"the actionable field was lost too: {line!r}"


def test_redact_value_still_leaves_both_shapes_intact() -> None:
    """The two tests above claim to defeat shapes ``redact_value`` cannot catch.
    If ``redact_value`` were quietly hardened to catch them, those tests would
    still pass but would no longer be testing what they say they test.

    This PINS the measured gap. If it goes red, ``redact_value`` gained new
    coverage -- good news, but the two tests above must then be re-justified
    rather than assumed to still bite.
    """
    from app.core.logging import redact_value

    assert redact_value("body", f"to {FAKE_ADDRESS}") == f"to {FAKE_ADDRESS}"
    assert redact_value("body", FAKE_RESEND_KEY) == FAKE_RESEND_KEY


# ---------------------------------------------------------------------------
# 3. Non-vacuity: the allowlist is not "redact everything".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("status_code", 500, "status_code=500"),
        ("active_count", 2, "active_count=2"),
        ("pass_rate", 0.92, "pass_rate=0.92"),
        ("force", True, "force=True"),
        ("threshold", None, "threshold=None"),
        ("provider", "openrouter", "provider='openrouter'"),
        ("model", "google/gemini-2.5-flash", "model='google/gemini-2.5-flash'"),
        ("request_id", REAL_REQUEST_ID, f"request_id={REAL_REQUEST_ID!r}"),
        ("path", "/health", "path='/health'"),
        ("cause_type", "LLMUnavailable", "cause_type='LLMUnavailable'"),
        ("spend_usd", "1.9400", "spend_usd='1.9400'"),
    ],
)
def test_operationally_necessary_fields_survive_intact(
    key: str, value: object, expected: str
) -> None:
    """PARTNER for every negative assertion in this file. A formatter that
    redacted or suppressed everything would satisfy each "the secret is absent"
    check above; it fails here.

    RED if a key is removed from ``EMITTED_TEXT_KEYS`` or if the scalar branch
    stops printing numbers.
    """
    assert render("probe", **{key: value}) == f"probe {expected}"


def test_a_real_production_request_id_survives_the_entropy_sweep() -> None:
    """The blocker two reviewers found, pinned against the REAL id shape.

    ``f"req_{uuid.uuid4().hex}"`` is 36 characters of ``[a-z0-9_]`` -- entirely
    inside ``HIGH_ENTROPY_RE``'s class -- so before ``OPAQUE_ID_KEYS`` the sweep
    blanked it and the emitted line read ``request_id='[REDACTED]'``. The API
    hands that id to the caller in ``ErrorEnvelope.request_id``; grepping the
    logs for it returned nothing, which is the one thing the whole #361 change
    exists to make possible.

    RED if ``request_id`` is removed from ``OPAQUE_ID_KEYS``.
    """
    assert len(REAL_REQUEST_ID) == 36, "the fixture must match middleware's real shape"
    line = render("orchestrator_error", request_id=REAL_REQUEST_ID, path="/health")
    assert REAL_REQUEST_ID in line, f"the correlation id was destroyed: {line!r}"
    assert "[REDACTED]" not in line, line


def test_the_per_request_line_carries_the_real_request_id_too() -> None:
    """Same defect, other emitter. ``app.core.middleware`` logs through
    ``build_log_event``, not ``extra=``, so it needed the fix in
    ``redact_value`` rather than in the formatter -- and it was already broken
    on ``main`` before #361 touched anything.

    RED if the ``OPAQUE_ID_KEYS`` branch is removed from ``redact_value``.
    """
    event = build_log_event("request_completed", request_id=REAL_REQUEST_ID, status_code=200)
    assert event["request_id"] == REAL_REQUEST_ID


def test_an_opaque_id_still_loses_a_bearer_token() -> None:
    """PARTNER: exempting these keys from the ENTROPY sweep must not exempt them
    from redaction altogether. ``request_id`` is caller-supplied (middleware
    honours an inbound ``X-Request-ID``), so the Bearer sweep is the part that
    still has to bite.

    RED if the ``OPAQUE_ID_KEYS`` branch returns ``value`` instead of the
    Bearer-swept ``redacted``.
    """
    line = render("req", request_id="Bearer abcdefghijklmnop.qrstuvwx")
    assert "abcdefghijklmnop" not in line, line
    assert "Bearer [REDACTED]" in line, line


def test_a_pathological_client_supplied_request_id_is_capped() -> None:
    """``RequestIDMiddleware`` honours an inbound ``X-Request-ID`` with no length
    validation. Before ``OPAQUE_ID_KEYS`` the entropy sweep collapsed a long one
    to ``[REDACTED]``, so exempting these keys removed the only bound — and on
    the ``build_log_event`` path ``MAX_EMITTED_TEXT`` does not reach, so a 4 kB
    header would have written a 4 kB line into a metered pipeline.

    RED if ``MAX_OPAQUE_ID`` stops being applied in ``redact_value``.
    """
    from app.core.logging import MAX_OPAQUE_ID, redact_value

    rendered = redact_value("request_id", "z" * 4000)
    assert len(rendered) < MAX_OPAQUE_ID + 40, len(rendered)
    assert rendered.endswith("…<truncated>")
    # PARTNER: a real id is nowhere near the cap and survives whole, so this is
    # not "ids are always truncated".
    assert redact_value("request_id", REAL_REQUEST_ID) == REAL_REQUEST_ID


def test_the_opaque_id_exemption_is_exact_not_substring() -> None:
    """PARTNER: ``SECRET_KEY_PARTS`` and ``RAW_TEXT_KEYS`` match as SUBSTRINGS,
    which is this module's recorded footgun. ``OPAQUE_ID_KEYS`` deliberately
    does not, so a future ``customer_request_id_token`` cannot buy itself an
    entropy exemption by containing ``request_id``.

    RED if the membership test becomes a substring scan.
    """
    from app.core.logging import redact_value

    long_secret = "z" * 40
    assert redact_value("request_id", long_secret) == long_secret  # exact key: exempt
    assert redact_value("outer_request_id", long_secret) == "[REDACTED]"  # not exempt
    # Byte-exact, so every addition is a deliberate, reviewed edit. ``index_versions``
    # (plural) joined #352; it is the same data as ``index_version`` and, because the
    # membership test is on the WHOLE key, it did NOT inherit that key's exemption.
    assert set(OPAQUE_ID_KEYS) == {
        "request_id",
        "index_version",
        "index_versions",
        "source_version_hash",
    }


def test_a_container_under_an_allowlisted_key_never_prints_its_contents() -> None:
    """Found by adversarial review. ``str()``-ing the value before consulting
    the allowlist threw away ``redact_value``'s per-inner-key recursion, so
    ``path={"token": ...}`` printed the whole dict -- the exact opposite of the
    fail-closed invariant this module's header asserts. No call site does this
    today; the invariant is the point.

    RED if the allowlist branch stops being ``isinstance(value, str)``-only.
    """
    for value, expected in (
        ({"token": "abc", "email": FAKE_ADDRESS}, "<dict n=2>"),
        ([FAKE_ADDRESS, FAKE_RESEND_KEY], "<list n=2>"),
        (b"secret-bytes", "<bytes n=12>"),
    ):
        line = render("evt", path=value)
        assert FAKE_ADDRESS not in line and FAKE_RESEND_KEY not in line, line
        assert "token" not in line, line
        assert f"path={expected}" in line, line
    # PARTNER: a plain string under the SAME key is still printed, so this is
    # not "path is always suppressed".
    assert render("evt", path="/health") == "evt path='/health'"


def test_an_int_subclass_cannot_smuggle_a_secret_through_the_scalar_branch() -> None:
    """Found by adversarial review. ``isinstance(value, int)`` accepted any int
    SUBCLASS, whose ``__str__`` can return anything at all, and the scalar branch
    prints it verbatim under any key -- allowlisted or not.

    RED if the scalar test goes back to ``isinstance`` from ``type(value) in``.
    """

    class Sneaky(int):
        def __str__(self) -> str:
            return f"apikey={FAKE_RESEND_KEY} {FAKE_ADDRESS}"

    line = render("evt", status_code=Sneaky(1))
    assert FAKE_RESEND_KEY not in line and FAKE_ADDRESS not in line, line
    assert "status_code=<Sneaky>" in line, line
    # PARTNER: a genuine int under the same key is still printed.
    assert render("evt", status_code=500) == "evt status_code=500"


def test_a_newline_in_an_extra_KEY_cannot_forge_a_second_line() -> None:
    """``repr`` guards the VALUE; the key had no guard, so a key containing a
    newline could write a plausible fake WARNING beneath a real line. Keys are
    source literals today, which is why this costs one regex instead of an
    argument.

    RED if ``UNSAFE_KEY_CHARS_RE`` stops being applied in ``render_extras``.
    """
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, "evt", None, None)
    setattr(record, "ok\nWARNING citevyn.auth: admin_login_success user", "root")
    line = build_log_formatter().format(record)
    assert "\n" not in line, f"an extra KEY forged a second log line: {line!r}"
    # PARTNER: an ordinary key is untouched, so this is not "all keys mangled".
    assert render("evt", status_code=1) == "evt status_code=1"


def test_the_allowlist_is_consulted_with_the_RAW_key_not_the_sanitised_one() -> None:
    """The sanitiser is for DISPLAY only. If the sanitised name were used for the
    allowlist lookup too, a key like ``"request id"`` (a space) would sanitise
    into ``request_id`` and buy itself verbatim printing it was never granted.

    Review found this direction correct but unpinned — swapping the two
    arguments survived the entire suite.

    RED if ``render_extras`` passes ``safe_key`` to ``render_extra_value``.
    """
    secret = "would-be-printed-verbatim"
    line = render("evt", **{"request id": secret})
    assert f"request_id=<str len={len(secret)}>" in line, line
    assert secret not in line, line
    # PARTNER: the genuinely-named key IS printed verbatim, so this is not
    # "nothing is ever allowlisted".
    assert render("evt", request_id="abc") == "evt request_id='abc'"


def test_a_record_dict_that_grows_during_formatting_does_not_drop_the_line() -> None:
    """``render_extras`` snapshots ``record.__dict__`` with ``list(...)`` because
    iterating it live raises ``RuntimeError: dictionary changed size during
    iteration`` OUTSIDE the per-field guard — the one remaining way this
    function could drop a line, which its docstring says it cannot.

    RED if the ``list(...)`` snapshot is removed. (The mutation-tester's report
    that this survived is why the test exists.)
    """

    class GrowsOnStr:
        def __init__(self, record: logging.LogRecord) -> None:
            self._record = record

        def __str__(self) -> str:
            self._record.__dict__[f"added_{len(self._record.__dict__)}"] = 1
            return "grew"

    record = logging.LogRecord("t", logging.WARNING, __file__, 1, "evt", None, None)
    record.provider = GrowsOnStr(record)
    line = build_log_formatter().format(record)
    assert line.startswith("evt "), line
    assert "provider=" in line, line


def test_an_unlisted_string_field_is_suppressed_but_visible() -> None:
    """Fail-closed default, stated as behaviour. A new call site's new string
    key is NOT printed (nobody reviewed whether it can carry a secret) but it IS
    accounted for, so the operator can see a field exists and ask for it.

    RED if the default branch starts printing unknown strings verbatim, or if it
    drops them silently the way ``LOG_FORMAT`` used to.
    """
    line = render("something_failed", brand_new_field="a secret nobody reviewed")
    assert "a secret nobody reviewed" not in line
    assert "brand_new_field=<str len=24>" in line, line


def test_collections_are_summarised_by_element_count_not_content() -> None:
    """``worker/cli.py`` passes ``ingested=[...source names...]``. RED if the
    default branch starts rendering container CONTENTS."""
    line = render("partial_run", index_version="v9", ingested=["codex", "claude", "cursor"])
    assert "codex" not in line
    assert "ingested=<list n=3>" in line, line
    assert "index_version='v9'" in line, line  # partner: the bounded field survives


# ---------------------------------------------------------------------------
# 4. Robustness: the formatter must never drop a line.
# ---------------------------------------------------------------------------


def test_a_value_whose_str_raises_does_not_drop_the_record() -> None:
    """A ``TypeError`` escaping ``format()`` makes logging DISCARD the record and
    print ``--- Logging error ---`` to stderr -- a worse version of the very
    symptom #361 fixes. RED if the per-field guard in ``render_extras`` is
    removed."""

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("nope")

        def __len__(self) -> int:
            raise RuntimeError("nope")

    line = render("boom", provider=Hostile(), status_code=500)
    assert "boom" in line
    assert "status_code=500" in line, f"one bad field killed the good ones: {line!r}"
    # The exception TYPE is named: a bare "<unrenderable>" tells whoever hits
    # this in production nothing at all.
    assert "provider=<unrenderable RuntimeError>" in line, line


def test_a_redis_dsn_with_a_query_password_is_redacted_before_it_is_logged() -> None:
    """Found by adversarial review, and NEW: on ``main`` this line was dropped
    entirely, so making the formatter emit extras created the leak.

    ``redis_client.py`` logs ``url=_redact_url(url)`` at INFO on every boot.
    ``_redact_url`` only peeled the ``user:pass@host`` userinfo form, but
    redis-py also honours ``redis://host:6379/0?password=…`` -- which has no
    ``@``, so the DSN came back untouched, and a password under 32 characters
    also slips ``HIGH_ENTROPY_RE``.

    RED if ``_redact_url`` stops applying ``QUERY_CREDENTIAL_RE``, or if
    ``password`` is dropped from that pattern's alternation.
    """
    from app.core.redis_client import _redact_url

    dsn = "redis://10.0.0.5:6379/0?password=hunter2"
    line = render("redis_client_initialized", url=_redact_url(dsn))
    assert "hunter2" not in line, f"a credential reached the log line: {line!r}"
    assert "password=[REDACTED]" in line, line
    # PARTNER 1: the userinfo form still works (this is not "the whole DSN is
    # blanked", which would lose the host an operator needs).
    assert "***@host.upstash.io" in _redact_url("rediss://default:SECRET@host.upstash.io:6379")
    # PARTNER 2: a DSN with no credential at all survives intact, so the
    # redaction is not simply eating every URL.
    assert _redact_url("redis://localhost:6379/0") == "redis://localhost:6379/0"


def test_a_newline_in_an_allowlisted_value_cannot_forge_a_second_line() -> None:
    """Log injection. ``repr`` is what contains it. RED if the allowlisted
    branch returns the bare string instead of ``repr``."""
    line = render("req", path="/a\nWARNING fake_admin_login_succeeded")
    assert "\n" not in line, f"a field forged a second log line: {line!r}"
    assert "fake_admin_login_succeeded" in line  # partner: it IS present, escaped


def test_a_very_long_allowlisted_value_is_truncated() -> None:
    """Volume control: one runaway field costs a truncated field, not a
    multi-kilobyte line. RED if ``MAX_EMITTED_TEXT`` stops being applied.

    Dots separate the segments on purpose, and the key is neither ``path`` nor
    a ``MODEL_ID_KEYS`` member. ``HIGH_ENTROPY_RE``'s character class includes
    ``/``, so under any key OUTSIDE ``PATH_KEYS`` a long slash-separated value
    is one contiguous 32+-char run and gets blanked to ``[REDACTED]`` BEFORE
    truncation is reached -- the test would then pass on the redactor's
    behaviour rather than on the truncation it names. (Under ``path`` the
    per-segment sweep of #384 applies instead and the value survives to be
    truncated by ``MAX_PATH``, which is pinned by
    ``test_a_pathological_path_is_capped_by_max_path`` below.)

    THE KEY MOVED FROM ``model`` TO ``call_site`` WITH #387, and the reason is
    worth recording because ``model`` is the obvious choice and is now the one
    wrong one: ``model`` joined ``MODEL_ID_KEYS``, whose over-cap arm returns
    ``'[REDACTED]…<N chars>'`` at 64 characters, so this fixture would be
    answered by ``MAX_MODEL_ID`` and never reach ``MAX_EMITTED_TEXT`` at all.
    Measured -- it went red on exactly the ``"[REDACTED]" not in line``
    assertion below. ``call_site`` is on ``EMITTED_TEXT_KEYS`` and in none of
    the three key-name exemption sets, which is the property this test needs.
    """
    long_value = "seg." * 400
    assert len(long_value) > MAX_EMITTED_TEXT * 5
    # The premise the docstring rests on, asserted rather than assumed: this
    # value is under the WORK guard, so it really does reach the sweep.
    assert len(long_value) < MAX_SCANNED_CHARS
    line = render("req", call_site=long_value)
    assert "[REDACTED]" not in line, "the entropy sweep fired; this is not testing truncation"
    assert "…<truncated>" in line, line
    assert len(line) < MAX_EMITTED_TEXT + 100, len(line)


def test_a_long_slash_path_keeps_its_route_shape() -> None:
    """#384. ``HIGH_ENTROPY_RE``'s class contains ``/``, so a path over 32
    characters used to collapse to ``'/[REDACTED]'`` -- observed in the Fly v17
    production logs on every session route.

    The two justifications the previous version of this test gave for accepting
    that were both MEASURED FALSE and are corrected here, not copied:

    * "every path this app routes (``/v1/ask``, ``/health``) is short" --
      ``/v1/ask`` is not a route. ``grep -rn '/v1/ask' backend/app`` hits only
      two comment lines in ``app/main.py``. The real answer route is
      ``POST /v1/sessions/{uuid}/messages``, 58 characters, and it blanked.
    * "the entropy sweep is what strips a Bearer token from a callback path" --
      it is not. ``BEARER_RE`` runs FIRST and does all of it; measured, the
      entropy sweep alone leaves ``Bearer abcdefghij.klmnopqrst`` untouched
      (21 chars, and the ``.`` breaks the class anyway). That is pinned
      separately by ``test_bearer_in_a_path_is_still_stripped``.

    RED if ``PATH_KEYS`` no longer contains ``"path"`` -- the whole-string sweep
    returns and this goes back to ``'/[REDACTED]'``.
    """
    line = render("req", path="/v1/auth/oauth/github/callback/extra/segments/here")
    assert line == "req path='/v1/auth/oauth/github/callback/extra/segments/here'", line
    # Partner: a high-entropy SEGMENT still blanks, so this is not "paths are
    # never redacted".
    assert render("req", path="/v1/x/" + "A" * 44) == "req path='/v1/x/[REDACTED]'"


def test_a_key_on_the_allowlist_is_still_run_through_redact_value() -> None:
    """Defence in depth: allowlisting a key buys the VALUE no exemption from the
    Bearer / high-entropy sweeps. RED if ``render_extra_value`` returns the raw
    string for an allowlisted key."""
    line = render("req", path="/cb?h=Bearer abcdefghij.klmnopqrst")
    assert "abcdefghij.klmnopqrst" not in line, line
    assert "Bearer [REDACTED]" in line, line


# ---------------------------------------------------------------------------
# 4b. #384 -- the PER-SEGMENT path sweep. Scope, secret shapes, and the cap.
#     The single most important test for this change is NOT here: it is
#     ``test_messages_routes.py::
#     test_the_request_log_line_names_the_session_route_it_served``, which
#     drives a real request through the app and reads the line the middleware
#     actually emitted. These are the unit-level partners around it.
# ---------------------------------------------------------------------------


# Obviously-fake constructed secret shapes. None is a real key; each is built
# from a repeated character or an obvious placeholder so no scanner and no
# reader can mistake it for one. The FIRST entry is deliberately one that IS
# redacted, so a `break`-vs-`continue` mistake in a future rewrite of this list
# cannot make the whole parametrization pass vacuously.
FAKE_SECRET_SHAPES = [
    ("b64_44_with_slash", "AAAAAAAAAAAAAAAAAAAA/BBBBBBBBBBBBBBBBBBBBBBB"),
    ("b64_40_with_slash", "AAAAAAAAAAAAAAAA/BBBBBBBBBBBBBBBBBBBBBBB"),
    ("hex_40", "a" * 40),
    ("sk_prefixed", "sk-" + "A" * 48),
    ("token_hex_32", "0" * 64),
    ("token_urlsafe_64", "x" * 80 + "-_"),
    ("aiza_39", "AIza" + "B" * 35),
    ("jwt_shaped", "A" * 36 + "." + "B" * 40 + "." + "C" * 43),
]


@pytest.mark.parametrize(
    ("shape", "value"), FAKE_SECRET_SHAPES, ids=[s for s, _ in FAKE_SECRET_SHAPES]
)
def test_a_secret_shape_under_a_non_path_key_is_still_blanked(shape: str, value: str) -> None:
    """The DEFAULT branch is unchanged by #384: under any key that is not in
    ``PATH_KEYS`` the whole-string entropy sweep still runs, so every one of
    these shapes blanks.

    RED if the per-segment sweep is moved above the key check and applied to
    every string -- the rejected fix (A). Measured: that change is
    INDISTINGUISHABLE from deleting ``/`` from ``HIGH_ENTROPY_RE``'s class
    (0 differing inputs over 400,000 sampled strings), and it would print
    28.35% of 44-character base64 values in full.
    """
    from app.core.logging import redact_value

    redacted = redact_value("detail", value)
    assert "[REDACTED]" in str(redacted), f"{shape} survived the entropy sweep: {redacted!r}"
    # Partner: the value really was long enough to be a credential shape, so
    # this is not passing on an empty/short input.
    assert len(value) >= 32, shape


def test_the_path_rule_is_keyed_on_the_key_name_not_the_value_shape() -> None:
    """A value that LOOKS like a path gets no per-segment treatment under a key
    that is not ``path``. This is the whole security argument for #384: the
    trigger must be a name chosen in our source, never a shape an attacker can
    choose. Measured: 1.58% of random base64 values begin with ``/``, so
    ``value.startswith("/")`` would let a secret select its own weaker rule.

    RED if the rule is re-keyed on the value's shape instead of ``PATH_KEYS``.
    """
    from app.core.logging import redact_value

    session_path = "/v1/sessions/0f0d1a2b-3c4d-5e6f-7a8b-9c0d1e2f3a4b/messages"
    # CHANGED BY #400, deliberately, and it is ABSORPTION rather than weakening:
    # this used to read ``"/[REDACTED]"``. The old boundary was ``\b``, which
    # cannot start a match on ``/`` (in the class, not a word character), so the
    # leading slash survived. The new boundary is defined over the class itself,
    # so the slash is part of the maximal run and is redacted with it. One
    # character MORE is blanked, never less.
    assert redact_value("reason", session_path) == "[REDACTED]"
    # Partner: the very same value under `path` DOES keep its shape, so the
    # assertion above is about the key, not about the value being unredactable.
    assert redact_value("path", session_path) == "/v1/sessions/[REDACTED]/messages"


def test_a_secret_inside_one_path_segment_is_still_blanked() -> None:
    """A 44-character base64 value that lands WHOLE inside one path segment is
    caught, because the segment is still swept.

    THE HONEST RESIDUAL, stated so nobody reads this as a stronger guarantee
    than it is: a base64 value whose own ``/`` characters fall inside the value
    splits into short segments and is NOT caught. Measured (200,000 samples,
    seed 20260908): 28.35% of 44-character base64 values and 32.12% of
    40-character ones would survive that way. That is why the rule is scoped to
    ``path`` alone -- there it is the CLIENT's own value in a 404 URL, echoed
    back and bounded by ``MAX_PATH``, disclosing none of our secrets, which is
    the identical tradeoff already accepted for ``request_id``.

    RED if the per-segment sweep drops ``HIGH_ENTROPY_RE`` (e.g. returns the
    path verbatim once the key matches).
    """
    from app.core.logging import redact_value

    contained = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # 44 chars, no slash
    assert len(contained) == 44
    assert redact_value("path", f"/v1/x/{contained}/y") == "/v1/x/[REDACTED]/y"
    # The residual, EXECUTED rather than only described: the same length of
    # value, split by its own slashes, survives.
    split_by_own_slashes = "AAAAAAAAAAAAAAAAAAAA/BBBBBBBBBBBBBBBBBBBBBBB"
    assert len(split_by_own_slashes) == 44
    assert redact_value("path", f"/v1/x/{split_by_own_slashes}/y") == (
        "/v1/x/AAAAAAAAAAAAAAAAAAAA/BBBBBBBBBBBBBBBBBBBBBBB/y"
    )


# A TEST-HARNESS BOUND on ``MAX_PATH_SEGMENTS``, the twin of the
# ``MATERIALISATION_LIMIT`` inside ``test_the_max_path_cap_fires_only_ABOVE_the_limit``
# and written for the same reason. Every fixture below builds a string by
# repeating a segment ``MAX_PATH_SEGMENTS`` times, so an absurd value for that
# constant allocates gigabytes and HANGS the suite instead of failing it.
#
# Found by the #390 mutation sweep, which is the point of writing it down: the
# ``MAX_PATH_SEGMENTS = 10**9`` mutant was recorded as KILLED, and it was not --
# the run died on allocation, not on an assertion. A mutant that hangs looks
# exactly like a mutant that fails, and this repo has already shipped one
# survivor wearing a timeout.
#
# The bound is far above any value the constant could sanely take, and exists
# only so an absurd value fails FAST and LOUDLY rather than allocating.
#
# It does NOT promise that any value under it is acceptable to the suite. An
# earlier version said raising the threshold to 256 or 1024 "must not fail
# here", which is true of this guard and misleading about the file: other
# tests bracket the constant much more tightly, and a reviewer measured the
# suite going red well below 256. This guard is about allocation, nothing more.
_SEGMENTS_MATERIALISATION_LIMIT = 100_000


def _refuse_to_materialise(max_path_segments: int) -> None:
    """Fail fast rather than allocate when ``MAX_PATH_SEGMENTS`` is absurd."""
    assert max_path_segments <= _SEGMENTS_MATERIALISATION_LIMIT, (
        f"MAX_PATH_SEGMENTS={max_path_segments}: this test refuses to build a fixture of "
        f"{max_path_segments} segments. This is the harness declining to allocate, not a "
        f"statement that {max_path_segments} is an invalid threshold."
    )


def test_a_pathological_path_is_capped_by_max_path() -> None:
    """``MAX_PATH`` restores a bound the whole-string entropy collapse used to
    provide by accident. Uncapped, the per-segment rule would emit the whole
    path into a metered log pipeline, on the ``build_log_event`` path where
    ``MAX_EMITTED_TEXT`` does not reach.

    THE FIXTURE CHANGED WITH #390, and the old one is worth naming because it
    is the obvious thing to write. This test used ``"/seg" * 1100`` -- 4,400
    characters, 1,100 separators -- which now exceeds ``MAX_PATH_SEGMENTS`` and
    is answered by the marker arm, never reaching ``MAX_PATH`` at all.

    An earlier version of this paragraph, and of the commit message that
    shipped it, said that fixture "would have passed for the wrong reason under
    a mutant that deleted the cap". That is FALSE, and two reviewers
    reproduced it independently: against the shipped code the old assertion
    ``capped.endswith("…<truncated>")`` is simply False, because the value is
    now ``'/[REDACTED]…<1100 segments>'``. The old test went RED and FORCED
    this rewrite; it did not pass quietly. Recorded rather than silently
    corrected, because overstating what evidence showed is this repo's most
    expensive recurring mistake.

    The fixture is now built AT the largest separator count the sweep still
    handles, so it exercises the cap on the only branch that has one.

    RED if ``MAX_PATH`` stops being applied (set it to a huge number and this
    fails). Also RED, loudly and with a message, if ``MAX_PATH`` is ever raised
    above the length this fixture can reach -- the two constants are coupled
    and the assertions below say so rather than assume it.
    """
    from app.core.logging import MAX_PATH, MAX_PATH_SEGMENTS, redact_value

    _refuse_to_materialise(MAX_PATH_SEGMENTS)
    # 20 characters is under ``HIGH_ENTROPY_RE``'s 32-character floor, so no
    # segment matches and the sweep is the identity -- the cap is then the only
    # thing that can shorten this value.
    segment = "q" * 20
    pathological = ("/" + segment) * MAX_PATH_SEGMENTS
    assert pathological.count("/") == MAX_PATH_SEGMENTS, (
        "the fixture must sit ON the sweep's boundary, not past it"
    )
    assert len(pathological) > MAX_PATH, (
        f"MAX_PATH={MAX_PATH} now exceeds the longest value {MAX_PATH_SEGMENTS} segments of "
        f"{len(segment)} characters can build ({len(pathological)}). Raise the segment length "
        f"or lower the cap -- this test can no longer reach the boundary it exists to check."
    )

    capped = redact_value("path", pathological)
    assert isinstance(capped, str)
    assert capped.endswith("…<truncated>"), capped
    assert len(capped) == MAX_PATH + len("…<truncated>"), len(capped)
    # Positive partner: the cap TRUNCATED real content rather than firing on a
    # value the sweep had already collapsed to nothing.
    assert capped.startswith("/" + segment), capped


def test_the_max_path_cap_fires_only_ABOVE_the_limit() -> None:
    """PARTNER for the cap test above, pinning the COMPARISON rather than the
    cap. ``len(swept) > MAX_PATH`` and ``len(swept) >= MAX_PATH`` both truncate
    everything the cap test feeds them, so that test passes under either -- an
    independent review round applied the ``>=`` mutant and the whole suite
    stayed green. Under ``>=`` a path whose swept form is EXACTLY ``MAX_PATH``
    characters gets ``…<truncated>`` appended to content that was never
    truncated: a log line that lies about itself, and the operator reading it
    goes looking for a longer path that does not exist.

    The swept length is established, not assumed equal to the input length. It
    is established TWICE, because the cheap version of this check is wrong: the
    module's ``HIGH_ENTROPY_RE`` matches a slash path as ONE run -- that IS
    #384 -- so searching the whole value proves nothing about a rule that
    sweeps SEGMENTS. What matters is that no individual segment matches, and
    then the sweep is the identity and swept length IS input length. That is
    asserted against the real regex here, and measured end-to-end through
    ``redact_value`` itself at ``MAX_PATH - 1``, a length where the correct
    comparison and the ``>=`` mutant necessarily agree so the measurement
    cannot be contaminated by the thing under test.

    RED if the comparison is loosened to ``>=`` (the exactly-``MAX_PATH`` half
    fails) or tightened to a larger threshold (the ``MAX_PATH + 1`` half fails).

    NOT fixed here: the pre-existing ``MAX_OPAQUE_ID`` branch a few lines above
    carries the identical ``>`` comparison with no boundary test of its own.
    That is pre-existing debt, out of scope for #384, and deliberately left
    alone rather than fixed in a change whose subject is the ``path`` key.
    """
    from app.core.logging import HIGH_ENTROPY_RE, MAX_PATH, redact_value

    # A TEST-HARNESS BOUND, not a policy on the constant. This test builds
    # ``"/seg" * MAX_PATH`` -- four times ``MAX_PATH`` characters -- so it
    # states the largest string it is willing to materialise. Found by the
    # mutation sweep for this very change: under the ``MAX_PATH = 10**9``
    # mutant an earlier draft allocated four GB and HUNG the suite rather than
    # failing it, which is a survivor wearing a timeout. The bound is set far
    # above any value the constant could sanely take -- raising ``MAX_PATH``
    # to 4096 or 8192 is a legitimate product decision and must not fail here
    # -- and exists only so an absurd value fails FAST and LOUDLY.
    MATERIALISATION_LIMIT = 1_000_000
    assert MAX_PATH <= MATERIALISATION_LIMIT, (
        f"MAX_PATH={MAX_PATH}: this test refuses to materialise a "
        f"{4 * MAX_PATH}-character string. This is the harness declining to "
        f"allocate, not a statement that {MAX_PATH} is an invalid cap."
    )

    # THE COUPLING TO ``MAX_PATH_SEGMENTS``, asserted rather than assumed.
    # Every fixture below is a ``"/seg"`` repeat sliced to about ``MAX_PATH``,
    # so it carries roughly ``MAX_PATH / 4`` separators. Past ``MAX_PATH = 252``
    # that crosses the segment threshold, the marker arm answers these values
    # instead of the sweep, and this test fails -- with a bare diff of a
    # 300-character string that never names the other constant. A reviewer
    # measured exactly that at ``MAX_PATH = 300`` and called it opaque rather
    # than loud. This assertion is what makes it loud.
    from app.core.logging import MAX_PATH_SEGMENTS

    marker = "…<truncated>"

    at_limit = ("/seg" * MAX_PATH)[:MAX_PATH]
    over_limit = ("/seg" * MAX_PATH)[: MAX_PATH + 1]
    just_under = ("/seg" * MAX_PATH)[: MAX_PATH - 1]
    assert len(at_limit) == MAX_PATH
    assert len(over_limit) == MAX_PATH + 1

    # Asserted on the FIXTURE's own separator count, not on arithmetic over
    # ``MAX_PATH``. The first version of this guard computed
    # ``(MAX_PATH + 1) // 4`` and was off by one: it permitted
    # ``MAX_PATH_SEGMENTS = 40``, where ``over_limit`` already carries 41
    # separators and the test fails anyway -- opaquely, which is the exact
    # thing this guard exists to prevent. Measured, not reasoned: the real
    # requirement is 41. Deriving the number from the fixture cannot drift.
    widest = max(value.count("/") for value in (at_limit, over_limit, just_under))
    assert widest <= MAX_PATH_SEGMENTS, (
        f"MAX_PATH={MAX_PATH} and MAX_PATH_SEGMENTS={MAX_PATH_SEGMENTS} have drifted apart: "
        f"this test's fixtures carry up to {widest} separators, which now exceeds the segment "
        f"threshold, so they take the MARKER arm and stop exercising the cap at all. Raise "
        f"MAX_PATH_SEGMENTS alongside MAX_PATH, or rebuild these fixtures with longer segments."
    )

    # No SEGMENT matches the entropy regex, so the per-segment sweep is the
    # identity on this material and swept length == len(). (Searching the
    # whole value instead would match -- "/" is in the class; that is #384.)
    for value in (at_limit, over_limit, just_under):
        assert all(HIGH_ENTROPY_RE.search(seg) is None for seg in value.split("/")), value
    # And measured end-to-end, one character below the boundary, where the
    # correct comparison and the ">=" mutant give the same answer.
    assert redact_value("path", just_under) == just_under

    exactly = redact_value("path", at_limit)
    assert exactly == at_limit, exactly
    assert not str(exactly).endswith(marker), exactly

    one_over = redact_value("path", over_limit)
    assert str(one_over).endswith(marker), one_over
    assert len(str(one_over)) == MAX_PATH + len(marker), len(str(one_over))


# ── The two operations inside the ``path`` branch, pinned individually ─────
#
# ``redact_value``'s path branch does exactly two things: SWEEP the value one
# ``/``-delimited segment at a time, then TRUNCATE at ``MAX_PATH``. The three
# tests above pin the sweep's OUTPUT and the cap's THRESHOLD. Neither pins how
# the two compose, and a second review round measured two mutants of that
# composition surviving the whole suite (2075 passed, 24 skipped -- byte
# identical to baseline). One of them LEAKS. The two tests below close them.

# An OBVIOUSLY-FAKE constructed token. Every character is inside
# ``HIGH_ENTROPY_RE``'s class and it is long enough to match, so the sweep
# blanks it -- which is precisely what a truncation landing inside it undoes.
FAKE_PATH_SECRET = "NOTAREALSECRET" + "0123456789" + "abcdefghij" + "QRSTUV"


def _fake_secret_path(cut_keeps: int) -> str:
    """A path built so a raw cut at ``MAX_PATH`` lands ``cut_keeps`` characters
    into ``FAKE_PATH_SECRET``, leaving a prefix too short for the sweep to see.

    Derived from ``MAX_PATH`` rather than hard-coded, so the fixture keeps its
    meaning if the cap is retuned.
    """
    from app.core.logging import MAX_PATH

    filler = "q" * (MAX_PATH - cut_keeps - len("/v1/") - len("/"))
    return "/v1/" + filler + "/" + FAKE_PATH_SECRET + "/tail"


def test_the_path_sweep_runs_BEFORE_the_truncation_so_a_cut_cannot_expose_a_secret() -> None:
    """The sweep-then-truncate ORDER is load-bearing, and nothing pinned it.

    Swapping the two -- cap the raw value first, then sweep the pieces --
    leaves the whole suite green while LEAKING. The cut lands mid-token, and
    the surviving prefix is under ``HIGH_ENTROPY_RE``'s 32-character floor, so
    the sweep that runs afterwards no longer matches it and prints it verbatim.
    Measured on this fixture, with ``MAX_PATH = 160`` and a 28-character cut::

        SHIPPED: '/v1/[REDACTED]/[REDACTED]/tail'
        MUTANT : '/v1/[REDACTED]/NOTAREALSECRET0123456789abcd…<truncated>'

    RED if the ``path`` branch truncates before it sweeps. Asserted on the
    SECRET's own characters, not on the exact output string, so it stays true
    if ``MAX_PATH``, the truncation marker or the redaction placeholder change.

    Its non-vacuity partner is
    ``test_the_fake_path_secret_really_is_present_and_really_is_swept``, which
    proves this fixture actually carries the token and that only the sweep
    removes it.
    """
    from app.core.logging import redact_value

    value = _fake_secret_path(cut_keeps=28)
    out = redact_value("path", value)
    assert isinstance(out, str)

    # No 16-character window of the token survives anywhere in the output --
    # strictly stronger than "the leaked prefix is absent", and independent of
    # exactly where a cut would land.
    window = 16
    leaked = [
        FAKE_PATH_SECRET[i : i + window]
        for i in range(len(FAKE_PATH_SECRET) - window + 1)
        if FAKE_PATH_SECRET[i : i + window] in out
    ]
    assert leaked == [], (leaked, out)


def test_the_fake_path_secret_really_is_present_and_really_is_swept() -> None:
    """NON-VACUITY PARTNER for the ordering test above, which on its own
    asserts only an ABSENCE and would pass just as happily on a fixture that
    never contained the token at all.

    Three things, each measured against the real module rather than assumed:

    1. the token IS in the input;
    2. a RAW cut at ``MAX_PATH`` preserves a 28-character prefix of it -- so
       truncation alone does not remove the leak material, and whatever does
       remove it must be the sweep;
    3. that surviving prefix does NOT match ``HIGH_ENTROPY_RE`` while the whole
       token DOES -- the exact mechanism by which a truncate-first order goes
       blind. And the sweep, on its own, really does blank the whole token.
    """
    from app.core.logging import HIGH_ENTROPY_RE, MAX_PATH, redact_value

    cut_keeps = 28
    value = _fake_secret_path(cut_keeps=cut_keeps)

    assert FAKE_PATH_SECRET in value
    assert len(value) > MAX_PATH, len(value)

    survives_a_raw_cut = value[:MAX_PATH]
    assert survives_a_raw_cut.endswith(FAKE_PATH_SECRET[:cut_keeps]), survives_a_raw_cut

    assert HIGH_ENTROPY_RE.search(FAKE_PATH_SECRET) is not None
    assert HIGH_ENTROPY_RE.search(FAKE_PATH_SECRET[:cut_keeps]) is None, (
        "the fixture no longer straddles the 32-character floor, so the "
        "ordering test above would pass for the wrong reason"
    )

    # And the sweep, unaided, blanks the whole token.
    assert redact_value("path", "/" + FAKE_PATH_SECRET) == "/[REDACTED]"


def test_the_max_path_cap_measures_the_SWEPT_length_not_the_raw_length() -> None:
    """The cap's OPERAND, which its sibling boundary test cannot see.

    ``len(swept) > MAX_PATH`` mutated to ``len(redacted) > MAX_PATH`` -- the
    cap keyed on the UNSWEPT length -- also survives the whole suite. It
    appends ``…<truncated>`` to output that was never truncated::

        input  : '/v1/' + 'B'*40 + '/' + 'c'*130      (len 175, swept len 25)
        SHIPPED: '/v1/[REDACTED]/[REDACTED]'
        MUTANT : '/v1/[REDACTED]/[REDACTED]…<truncated>'

    WHY ``test_the_max_path_cap_fires_only_ABOVE_the_limit`` IS BLIND TO IT, BY
    CONSTRUCTION. Every fixture there is a ``"/seg"`` repeat, chosen so that no
    segment matches ``HIGH_ENTROPY_RE`` and the sweep is therefore the
    identity. That is exactly the property its docstring establishes to make
    its OPERATOR check (``>`` versus ``>=``) sound -- and it forces
    ``len(redacted) == len(swept)`` on every one of its inputs, so the two
    operands are indistinguishable there. The property that makes the operator
    check sound is the property that makes the operand check vacuous. Only a
    value the sweep SHORTENS can separate them.

    RED if the cap is measured against the pre-sweep length.
    """
    from app.core.logging import MAX_PATH, redact_value

    marker = "…<truncated>"
    entropy_segment = "B" * 40
    long_segment = "c" * (MAX_PATH + 15 - len("/v1/") - len(entropy_segment) - len("/"))
    value = "/v1/" + entropy_segment + "/" + long_segment

    # Both segments are over the 32-character floor, so both collapse and the
    # sweep genuinely SHORTENS -- the one shape the sibling test never feeds.
    assert len(entropy_segment) >= 32 and len(long_segment) >= 32
    # The RAW length straddles the cap: a cap keyed on it fires, a cap keyed on
    # the swept length does not. Without this the test would prove nothing.
    assert len(value) > MAX_PATH, len(value)

    out = redact_value("path", value)
    assert isinstance(out, str)
    assert not out.endswith(marker), out
    assert len(out) <= MAX_PATH, (len(out), out)
    # Positive partner: the sweep really did fire on both segments, so the
    # shortening is real and not an empty-input artefact.
    assert out.count("[REDACTED]") == 2, out
    assert entropy_segment not in out and long_segment not in out


def test_a_real_session_path_is_not_truncated() -> None:
    """PARTNER for the cap test above, which on its own counts nothing: prove
    the thing being bounded is a value the cap does NOT reach. The longest path
    this app actually routes is a session message POST, 58 characters.

    RED if ``MAX_PATH`` is lowered below the real route length.
    """
    from app.core.logging import redact_value

    session_path = "/v1/sessions/0f0d1a2b-3c4d-5e6f-7a8b-9c0d1e2f3a4b/messages"
    assert len(session_path) == 58
    # POSITIVE, not merely 'the truncation marker is absent'. #390 added a
    # SECOND mechanism that satisfies that absence: under
    # ``MAX_PATH_SEGMENTS = 4`` this route renders as
    # ``'/[REDACTED]…<5 segments>'`` -- total loss of the route shape this
    # test exists to protect -- and the absence assertion still passed.
    # Measured by a reviewer. Assert the value instead.
    assert redact_value("path", session_path) == "/v1/sessions/[REDACTED]/messages"


# ── #390: the MAX_PATH_SEGMENTS marker arm ─────────────────────────────────
#
# The per-segment sweep costs one ``re.sub`` per segment, in the event loop, on
# every request; ``MAX_PATH`` bounded the output and not the work. Above
# ``MAX_PATH_SEGMENTS`` the value is replaced by a marker.
#
# #390 proposed falling back to the pre-#384 WHOLE-STRING sweep instead, on the
# ground that it redacts a superset of what the per-segment sweep redacts. That
# is true of the sweep and false of the RULE: redacting a superset makes the
# output shorter, and a shorter output pulls tail material inside the
# ``MAX_PATH`` window that the per-segment order cuts away. Both halves are
# executed below -- the marker arm is pinned by the shape of its own output,
# and the counterexample that killed the whole-string fallback is pinned as a
# regression so nobody re-derives it.

# The marker's ONLY variable part is a decimal count, so its whole output is a
# constant modulo digits. That is the security property, and it is asserted as
# a full match rather than described.
# ANCHORED with \A and \Z, not merely relied on being used with `fullmatch`.
# Unanchored, the pattern accepts every leak under a stray `search`:
# `//[REDACTED]…<1 segments>`, `/[REDACTED]…<1 segments>/leak` and
# `x/[REDACTED]…<1 segments>` all match. Every use here is `fullmatch` today;
# the anchors mean a future `search` cannot silently weaken it.
_SEGMENT_MARKER_RE = re.compile(r"\A/\[REDACTED\]…<\d+ segments>\Z")

# A large crafted request line. NOT "the largest uvicorn accepts" -- an earlier
# name said that and it is false: httptools 0.8.0 parses a 1,000,000-byte
# request line and uvicorn caps neither (16,384 is h11's
# DEFAULT_MAX_INCOMPLETE_EVENT_SIZE, a different implementation). This is
# simply the size #390 probed.
_A_LARGE_REQUEST_LINE = 16385

# The sub-floor literal the #390 counterexample turns on: 27 characters, under
# HIGH_ENTROPY_RE's 32-character floor, so NO sweep in the module removes it.
# ONE constant, imported by every site that needs it. It was written out three
# times in three tests, so changing one copy to a shape the sweep DOES redact
# would have left the regression test passing for the wrong reason while its
# non-vacuity partner stayed green -- the exact failure that partner exists to
# prevent. Obviously synthetic; never a real key.
FAKE_SUB_FLOOR_KEY = "re_" + "M5jEIbOReG1FaJDGhnseqgg4"


def test_the_marker_reaches_the_line_through_the_extra_route_too() -> None:
    """THE OTHER EMISSION ROUTE, which nothing else covers.

    ``path`` reaches the log by two different mechanisms and this change
    affects both. ``app/core/middleware.py`` passes it to ``build_log_event``,
    whose dict becomes the record's MESSAGE -- that route is driven end to end
    by ``test_messages_routes.py``. But ``app/main.py`` emits ``path`` as an
    ``extra=`` field at TWO sites (``orchestrator_error`` at :267 and
    ``unhandled_exception`` at :348), and that route goes through
    ``render_extra_value``'s allowlist, ``MAX_EMITTED_TEXT`` and ``repr``
    instead. Two of the three URL-path sites are therefore the untested one.

    Found by an adversarial reviewer, who checked the behaviour by hand and
    noted that nothing pinned it.

    RED if ``path`` is dropped from ``EMITTED_TEXT_KEYS`` (the field renders as
    a shape summary), or if the marker grows past ``MAX_EMITTED_TEXT`` and is
    truncated a second time.
    """
    from app.core.logging import MAX_EMITTED_TEXT, MAX_PATH_SEGMENTS

    crafted = "/a" * (MAX_PATH_SEGMENTS + 1)
    line = render("orchestrator_error", path=crafted)

    expected = f"/[REDACTED]…<{MAX_PATH_SEGMENTS + 1} segments>"
    assert line == f"orchestrator_error path={expected!r}", line
    # The `extra=` route applies its OWN cap after `redact_value`. The marker
    # must sit under it, or the field would carry two truncation markers.
    assert len(expected) < MAX_EMITTED_TEXT
    assert "…<truncated>" not in line, line
    # Positive partner: a short route still renders in full on this same
    # route, so the assertion above is about the marker, not about `path`
    # being suppressed here generally.
    assert render("orchestrator_error", path="/health") == "orchestrator_error path='/health'"


def test_a_path_with_too_many_segments_becomes_a_bounded_marker() -> None:
    """#390. Above ``MAX_PATH_SEGMENTS`` the sweep is skipped entirely.

    The marker states the element count, which ``'/[REDACTED]'`` never did, so
    an operator reading a 404 flood learns its SHAPE. uvicorn's own access line
    still carries the full request line (``--access-log``), so the path itself
    is not lost from the system.

    RED if ``MAX_PATH_SEGMENTS`` is raised above this fixture's element count,
    or if the marker arm is removed (the sweep would emit 160 characters of
    slashes plus ``…<truncated>``).
    """
    from app.core.logging import MAX_PATH, redact_value

    crafted = "/" * _A_LARGE_REQUEST_LINE
    out = redact_value("path", crafted)
    assert isinstance(out, str)
    assert _SEGMENT_MARKER_RE.fullmatch(out), out
    # Bounded by construction, not by ``MAX_PATH``: the marker never reaches
    # the cap, so the cap is not what is holding this line down.
    assert len(out) <= 28, (len(out), out)
    assert len(out) < MAX_PATH
    # Positive partner: the count is REAL, so this is not a fixed string that
    # would look identical for any input.
    assert f"<{_A_LARGE_REQUEST_LINE} segments>" in out, out


def test_the_segment_marker_fires_only_ABOVE_the_limit() -> None:
    """The threshold is a security boundary, so both sides are pinned and so is
    the exact element count at which behaviour flips. Off-by-one here is not a
    cosmetic bug: it decides which of two different redaction rules runs.

    ``MAX_PATH_SEGMENTS`` counts SEPARATORS, which for a value beginning with
    ``/`` is the segment count an operator gets by counting slashes. That
    property is asserted here, not assumed.

    RED if the comparison is loosened to ``>=`` (the at-limit half fails, the
    sweep's output replaced by a marker) or tightened to ``+ 1`` (the
    one-over half fails).

    A previous version of this line listed a third mutant, "the count is
    taken without the ``+ 1``". Under the element-counting rule that was the
    SAME mutant as ``> MAX_PATH_SEGMENTS + 1`` -- both move the flip point one
    step -- so it was coverage counted twice, not coverage. The rule now
    counts separators and there is no ``+ 1`` to drop.
    """
    from app.core.logging import MAX_PATH, MAX_PATH_SEGMENTS, redact_value

    _refuse_to_materialise(MAX_PATH_SEGMENTS)
    # One-character segments, for two independent reasons: no segment can match
    # ``HIGH_ENTROPY_RE``'s 32-character floor, AND the whole value stays under
    # ``MAX_PATH``. Both matter -- on the sweep side the output is then exactly
    # the input, so any difference is attributable to the BRANCH and not to
    # redaction or to truncation. (A 3-character segment overflows the cap at
    # 63 segments and this test measured the cap instead. Found by running it.)
    at_limit = "/a" * MAX_PATH_SEGMENTS
    one_over = "/a" * (MAX_PATH_SEGMENTS + 1)
    assert len(one_over) <= MAX_PATH, (
        f"MAX_PATH={MAX_PATH} can no longer hold {MAX_PATH_SEGMENTS + 1} one-character "
        f"segments ({len(one_over)} characters), so this test would measure the cap, not "
        f"the branch."
    )
    # SEPARATORS, which is what the rule counts and what the marker prints.
    assert at_limit.count("/") == MAX_PATH_SEGMENTS
    assert one_over.count("/") == MAX_PATH_SEGMENTS + 1
    # And the separator count really is the segment count here, because the
    # value begins with "/" -- the property the rule relies on.
    assert at_limit.startswith("/")
    assert len([s for s in at_limit.split("/") if s]) == MAX_PATH_SEGMENTS

    swept = redact_value("path", at_limit)
    assert swept == at_limit, swept
    assert not _SEGMENT_MARKER_RE.fullmatch(str(swept)), swept

    marked = redact_value("path", one_over)
    assert _SEGMENT_MARKER_RE.fullmatch(str(marked)), marked
    assert f"<{MAX_PATH_SEGMENTS + 1} segments>" in str(marked), marked


@pytest.mark.parametrize(
    ("shape", "value"), FAKE_SECRET_SHAPES, ids=[s for s, _ in FAKE_SECRET_SHAPES]
)
def test_the_marker_arm_emits_no_character_of_its_input(shape: str, value: str) -> None:
    """THE "NEVER WEAKER" PROOF: the marker arm's result is a fixed string plus
    a decimal count, so no input character can reach the log line whatever the
    input contains. That is a proof by the shape of the OUTPUT, not a sampling
    argument, and ``_SEGMENT_MARKER_RE.fullmatch`` is the assertion carrying
    it.

    WHAT THIS PARAMETRIZATION IS AND IS NOT, because an earlier docstring
    called it a per-shape proof and a reviewer showed it is not. Once the
    fullmatch passes, ``value not in out`` and the empty-window check are
    entailed for EVERY shape -- they cannot fail while the fullmatch
    succeeds. So the eight cases have no marginal kills over the first, and
    the kill set of this test is a strict subset of
    ``test_a_path_with_too_many_segments_becomes_a_bounded_marker``'s.

    It is kept as a DOCUMENTATION pin -- the concrete, per-shape statement an
    operator or auditor would want to see executed rather than argued -- in
    the same spirit as the three declared non-behavioural pins named in this
    module's docstring. It is not load-bearing, and this paragraph exists so
    nobody counts it as coverage.

    RED if the marker arm is replaced by anything that echoes input, including
    #390's whole-string fallback.
    """
    from app.core.logging import MAX_PATH_SEGMENTS, redact_value

    _refuse_to_materialise(MAX_PATH_SEGMENTS)
    crafted = "/" + "/".join(["z"] * MAX_PATH_SEGMENTS) + "/" + value
    assert crafted.count("/") > MAX_PATH_SEGMENTS
    out = str(redact_value("path", crafted))

    assert _SEGMENT_MARKER_RE.fullmatch(out), (shape, out)
    assert value not in out, (shape, out)
    # Non-vacuity: no 8-character window of the shape survives either, so this
    # is not passing because the value happens to be short.
    assert len(value) >= 32, shape
    windows = [value[i : i + 8] for i in range(len(value) - 7)]
    assert [w for w in windows if w in out] == [], (shape, out)


def test_a_many_segment_path_cannot_print_what_the_per_segment_order_truncated() -> None:
    """THE REGRESSION FOR THE FIX THAT WAS NOT TAKEN, executed rather than
    described, because #390 records the opposite conclusion and a future reader
    will otherwise re-derive it.

    #390's remedy was to fall back to the pre-#384 whole-string sweep above the
    threshold, on the measured ground that the whole-string sweep redacts a
    superset of the per-segment sweep's characters. The superset claim is true.
    The conclusion drawn from it -- "strictly stronger, 0 cases weaker over
    300,000 samples" -- is false, because the rule is the sweep AND the
    ``MAX_PATH`` cut. Redacting a superset SHORTENS the output, and a shorter
    output pulls tail material inside the 160-character window the per-segment
    order truncates away.

    Reproduced deterministically, 65 elements, ``MAX_PATH = 160``::

        per-segment    '/[REDACTED]/[REDACTED]/…<truncated>'   literal absent
        whole-string   '/[REDACTED]/.re_…'                     literal PRINTS
        marker         '/[REDACTED]…<65 segments>'             literal absent

    The literal is a shape NEITHER sweep redacts -- 27 characters, under
    ``HIGH_ENTROPY_RE``'s floor -- so the per-segment order hid it by volume,
    not by redaction. The marker cannot regress this way at all.

    RED if the marker arm is replaced by ``HIGH_ENTROPY_RE.sub(...)`` over the
    whole string, which is exactly the change #390 asks for.
    """
    from app.core.logging import MAX_PATH_SEGMENTS, redact_value

    # Obviously fake: a repeated character plus a placeholder body, 27
    # characters, deliberately UNDER the 32-character floor.
    _refuse_to_materialise(MAX_PATH_SEGMENTS)
    fake_key = FAKE_SUB_FLOOR_KEY
    assert len(fake_key) == 27

    crafted = "/" + "/".join(["A" * 32] * MAX_PATH_SEGMENTS) + "/." + fake_key
    assert crafted.count("/") == MAX_PATH_SEGMENTS + 1

    out = str(redact_value("path", crafted))
    assert _SEGMENT_MARKER_RE.fullmatch(out), out
    assert fake_key not in out, out


class _CountingPattern:
    """A ``re.Pattern`` stand-in that records every call ``redact_value`` makes.

    An earlier version implemented ``sub`` (counted) and ``search``
    (UNCOUNTED), which left the hole this class exists to close: a mutant that
    restored the per-segment work as ``HIGH_ENTROPY_RE.search(seg)`` inside the
    marker arm produced byte-identical output, cost 1,040 of the 1,592
    microseconds the sweep costs, and kept the whole suite green. A reviewer
    measured it. Only the counter was blind -- no output-level test anywhere
    can see that mutant, because there is no output difference to see.

    So ``sub`` is counted and EVERY other attribute raises. That also makes
    good on the promise the old docstring made and did not keep.

    THE LIMIT, stated because it is a real surviving mutant and not a
    hypothetical: this intercepts a module GLOBAL by name. Code that calls
    ``re.sub(<the pattern source>, ...)`` directly, or compiles its own copy,
    does the same work and is invisible here -- and to every other test in the
    repo, since the output is unchanged. That mutant survives, deliberately
    recorded rather than papered over. Nothing but review catches it; a probe
    on a name cannot see a call that never uses the name.

    Used for ``HIGH_ENTROPY_RE`` and ``BEARER_RE`` both -- the marker arm must
    run before EITHER of them.
    """

    def __init__(self, inner: re.Pattern[str], name: str) -> None:
        self._inner = inner
        # The pattern's own name, because this class wraps TWO of them and a
        # hardcoded one made a stray `BEARER_RE` call report as
        # `HIGH_ENTROPY_RE` -- a misdiagnosis in the one probe whose whole job
        # is saying which pattern ran.
        self._name = name
        self.calls = 0
        self.sub_calls = 0

    def sub(self, repl: str, string: str) -> str:
        self.calls += 1
        self.sub_calls += 1
        return self._inner.sub(repl, string)

    def __getattr__(self, name: str) -> object:
        # Deliberately AssertionError, not AttributeError: this must fail the
        # test rather than let a `hasattr` probe quietly answer False. The
        # cost, named because it is real: `copy.copy` and `hasattr` on this
        # object raise. Nothing in these tests does either.
        raise AssertionError(
            f"the path branch reached {object.__getattribute__(self, '_name')}"
            f".{name}: this probe counts `sub` only, so any other use is "
            f"unmeasured work and must be either counted here or removed"
        )


def test_the_marker_arm_runs_INSTEAD_OF_the_sweep_not_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#390 IS A COST DEFECT, so the only assertion that actually measures it is
    one about WORK, not about output.

    Moving the threshold check to AFTER the sweep produces a byte-identical log
    line for every input, and every other test in this file passes. Measured:
    that mutant survived ``pytest tests/test_log_extra_fields.py
    tests/test_log_redaction.py tests/test_messages_routes.py`` -- the exact
    selection, because a bare count means nothing without one -- while doing
    the full per-segment work the issue exists to remove (966 microseconds on
    the 16 kB all-slash path, end to end through ``redact_value``). It
    would have bounded the output, which was never the problem, and shipped the
    regression under a green suite.

    The sub COUNT is asserted rather than a wall-clock time, so this is
    deterministic and cannot flake under load.

    RED if the ``MAX_PATH_SEGMENTS`` check is moved below the sweep, or if the
    arm stops returning early.
    """
    from app.core import logging as logging_module

    _refuse_to_materialise(logging_module.MAX_PATH_SEGMENTS)
    counting = _CountingPattern(logging_module.HIGH_ENTROPY_RE, "HIGH_ENTROPY_RE")
    monkeypatch.setattr(logging_module, "HIGH_ENTROPY_RE", counting)

    crafted = "/a" * (logging_module.MAX_PATH_SEGMENTS + 5)
    assert crafted.count("/") > logging_module.MAX_PATH_SEGMENTS
    out = logging_module.redact_value("path", crafted)
    assert _SEGMENT_MARKER_RE.fullmatch(str(out)), out
    # `calls`, not `sub_calls`: ANY use of the pattern is unmeasured work, and
    # `__getattr__` turns a non-`sub` use into an error rather than a silent
    # zero. Asserting only `sub_calls` left a mutant alive that restored 65% of
    # the cost through `search`.
    assert counting.calls == 0, (
        f"the entropy sweep ran {counting.calls} times on a path the marker arm "
        f"answers -- the work is not bounded, only the output is"
    )

    # PARTNER, because a count of zero on its own would also be satisfied by a
    # `redact_value` that never sweeps anything: a real route DOES sweep.
    counting.calls = 0
    counting.sub_calls = 0
    session_path = "/v1/sessions/0f0d1a2b-3c4d-5e6f-7a8b-9c0d1e2f3a4b/messages"
    swept = logging_module.redact_value("path", session_path)
    assert swept == "/v1/sessions/[REDACTED]/messages", swept
    # ONE sub per split element, pinned EXACTLY rather than as `> 0`. The
    # exactness is load-bearing and it has a cost worth naming: it forbids the
    # "skip segments under 32 characters" optimisation, which is a provable
    # no-op on output and which `MAX_PATH_SEGMENTS`'s comment records as
    # measured-and-rejected. Anyone adopting it must change this line and say
    # why -- which is the point. A bare `> 0` would let the sweep degenerate to
    # a single call and still pass.
    assert counting.sub_calls == len(session_path.split("/")), counting.sub_calls


def test_no_regex_at_all_runs_on_a_path_the_marker_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker arm must run BEFORE ``BEARER_RE``, not merely before the
    entropy sweep -- and that ordering had nothing pinning it.

    ``BEARER_RE.sub`` is an unbounded O(len) scan over the same crafted path,
    and it runs on every string value. With the segment check placed below it,
    output is byte-identical for every input and the entire suite stays green
    while the 16 kB path costs **45.6 us instead of 2.70** -- measured, both
    orders, on the same interpreter. A reviewer's mutant proved the gap by
    surviving; the entropy-sweep counter next door cannot see it, because the
    wasted work is in a different pattern object.

    RED if the ``MAX_PATH_SEGMENTS`` check is moved below
    ``redacted = BEARER_RE.sub(...)``.
    """
    from app.core import logging as logging_module

    _refuse_to_materialise(logging_module.MAX_PATH_SEGMENTS)
    bearer = _CountingPattern(logging_module.BEARER_RE, "BEARER_RE")
    entropy = _CountingPattern(logging_module.HIGH_ENTROPY_RE, "HIGH_ENTROPY_RE")
    monkeypatch.setattr(logging_module, "BEARER_RE", bearer)
    monkeypatch.setattr(logging_module, "HIGH_ENTROPY_RE", entropy)

    crafted = "/a" * (logging_module.MAX_PATH_SEGMENTS + 5)
    out = logging_module.redact_value("path", crafted)
    assert _SEGMENT_MARKER_RE.fullmatch(str(out)), out
    assert bearer.calls == 0, (
        f"BEARER_RE ran {bearer.calls} times on a path the marker arm answers: the "
        f"arm is below the Bearer sweep, so the value is scanned in full before "
        f"being discarded"
    )
    assert entropy.calls == 0, entropy.calls

    # PARTNER: on a value the arm does NOT answer, both patterns really do run,
    # so the zeros above measure the ordering and not a broken probe.
    bearer.calls = entropy.calls = 0
    assert logging_module.redact_value("path", "/v1/x") == "/v1/x"
    assert bearer.calls == 1, bearer.calls
    assert entropy.calls > 0, entropy.calls


def test_the_marker_arm_is_selected_by_the_key_name_not_the_value_shape() -> None:
    """#384's central invariant, extended to the arm #390 adds.

    ``test_the_path_rule_is_keyed_on_the_key_name_not_the_value_shape`` pins
    this for the SWEEP. It did not cover the marker arm: a reviewer's mutant
    replacing the arm's ``if key_lower in PATH_KEYS`` with an unconditional
    test survived the whole suite. That mutant leaks nothing -- the marker is
    strictly more redacted than the sweep -- but it hands the branch choice to
    the VALUE, which is the one thing the #384 comment block spends a hundred
    lines establishing must never happen.

    RED if the arm's key-name guard is dropped or widened to all keys.
    """
    from app.core.logging import MAX_PATH_SEGMENTS, redact_value

    _refuse_to_materialise(MAX_PATH_SEGMENTS)
    many_slashes = "/a" * (MAX_PATH_SEGMENTS + 5)

    # Under a NON-path key the arm must not fire: the default branch runs and
    # collapses this to the whole-string sweep's output instead.
    under_other_key = str(redact_value("reason", many_slashes))
    assert not _SEGMENT_MARKER_RE.fullmatch(under_other_key), under_other_key
    # Partner: the identical value under `path` DOES take the arm, so the
    # assertion above is about the key and not about the value being immune.
    assert _SEGMENT_MARKER_RE.fullmatch(str(redact_value("path", many_slashes)))


def test_a_str_subclass_cannot_choose_its_own_branch() -> None:
    """The arm reads ``str.count(value, "/")`` -- the UNBOUND method -- because
    it runs before ``BEARER_RE.sub``, which is what normalises a ``str``
    subclass to a plain ``str`` everywhere else in this function.

    With the bound ``value.count("/")`` a subclass supplies its own count and
    picks the branch, which is the same class of hole ``render_extra_value``
    already refuses ``int`` subclasses for. No call site constructs one today;
    a reviewer's mutant showed nothing would notice if one did.

    RED if the arm is changed to ``value.count("/")``.
    """
    from app.core.logging import MAX_PATH_SEGMENTS, redact_value

    _refuse_to_materialise(MAX_PATH_SEGMENTS)

    class LyingStr(str):
        def count(self, *args: object, **kwargs: object) -> int:  # type: ignore[override]
            return 0  # "I have no separators, please sweep me instead"

    crafted = LyingStr("/a" * (MAX_PATH_SEGMENTS + 5))
    # The lie is real: the object reports 0 while the true count is over the
    # threshold. Without this the test could pass for the wrong reason.
    assert crafted.count("/") == 0
    assert str.count(crafted, "/") == MAX_PATH_SEGMENTS + 5

    out = str(redact_value("path", crafted))
    assert _SEGMENT_MARKER_RE.fullmatch(out), (
        f"a str subclass talked its way out of the marker arm: {out!r}"
    )


def test_the_threshold_clears_every_route_this_app_declares() -> None:
    """THE DESIGN MARGIN, made executable. Nothing else pins it.

    ``MAX_PATH_SEGMENTS``'s comment justifies 64 by saying the deepest declared
    route is 7 separators and the deepest observed filesystem ``path=`` value
    is 10. That argument lived only in prose: a reviewer enumerated the values
    the constant may take without any test noticing and found the whole window
    **42 through 80** unguarded. At 42, any 43-separator value would silently
    lose its route shape and no test would say so.

    This asserts the margin against the REAL route table, in the separator unit
    the constant counts.

    WHAT IT UNIQUELY CATCHES, stated honestly because the first draft of this
    docstring claimed the whole window and a skeptic measured otherwise. On the
    LOWERING direction it contributes nothing: the coupling assertion inside
    ``test_the_max_path_cap_fires_only_ABOVE_the_limit`` requires
    ``MAX_PATH_SEGMENTS >= 41`` (measured, by running it) and fires first at
    every value below that. What
    only this test catches is the OPPOSITE direction -- a route being ADDED
    that is deep enough to approach the threshold, which no other test looks
    at, and which is the failure the constant's comment would otherwise be the
    only record of.

    The 4x factor is the margin the comment claims, not a tight bound; it is
    stated as a factor so the test does not have to be edited every time a
    route is added.

    RED if a route is added deeper than a quarter of the threshold (17
    separators or more today), or -- redundantly with the coupling assertion --
    if ``MAX_PATH_SEGMENTS`` drops to 23 or below. The deepest declared route
    is 6 separators today, so the guard reads ``6 * 4 <= 64``.
    """
    from app.core.logging import MAX_PATH_SEGMENTS
    from app.main import create_app

    # From the OpenAPI schema, not from ``app.routes``: the API routers are
    # attached through include wrappers, so the top-level ``routes`` list
    # yields only ``/docs``, ``/openapi.json`` and friends. The partner below
    # is what caught that -- the first draft of this test enumerated 4 paths
    # and would have "passed" against a maximum depth of 2.
    paths = list(create_app().openapi()["paths"])
    assert len(paths) >= 20, (len(paths), sorted(paths))
    assert "/health" in paths, sorted(paths)[:10]
    assert "/v1/sessions/{session_id}/messages" in paths, sorted(paths)[:10]

    deepest = max(path.count("/") for path in paths)
    deepest_path = max(paths, key=lambda p: p.count("/"))
    assert deepest * 4 <= MAX_PATH_SEGMENTS, (
        f"the deepest declared route is {deepest} separators ({deepest_path!r}) and "
        f"MAX_PATH_SEGMENTS is {MAX_PATH_SEGMENTS}: the margin the constant's comment "
        f"claims is gone, so a real route is close to being logged as a marker."
    )


def test_the_fake_under_floor_key_really_does_survive_both_sweeps() -> None:
    """NON-VACUITY PARTNER for the regression above, which asserts an ABSENCE
    and would pass on a fixture whose literal both sweeps redact anyway.

    Three things, measured against the real module rather than assumed:

    1. the literal does NOT match ``HIGH_ENTROPY_RE``, so no sweep removes it;
    2. the per-segment sweep alone, on a SHORT path carrying it, prints it in
       full -- so it is genuinely reachable material, not something the rule
       blanks for other reasons;
    3. the whole-string sweep prints it too -- so the marker, not the sweep, is
       what removes it above the threshold.
    """
    from app.core.logging import HIGH_ENTROPY_RE, SECRET_VALUE, redact_value

    fake_key = FAKE_SUB_FLOOR_KEY
    assert HIGH_ENTROPY_RE.search(fake_key) is None, (
        "the fixture is now over the 32-character floor, so the regression "
        "test above would pass for the wrong reason"
    )
    short = "/v1/x/." + fake_key
    assert redact_value("path", short) == short
    assert fake_key in HIGH_ENTROPY_RE.sub(SECRET_VALUE, short)


def test_bearer_in_a_path_is_still_stripped() -> None:
    """``BEARER_RE`` runs BEFORE the new per-segment branch, so a ``Bearer``
    token in a path value is still stripped. Measured, correcting the claim the
    old test made: the entropy sweep alone does NOT do this -- it leaves
    ``Bearer abcdefghij.klmnopqrst`` untouched.

    RED if the ``BEARER_RE`` substitution is removed, or if the ``path`` branch
    is moved above it.
    """
    from app.core.logging import redact_value

    redacted = str(redact_value("path", "/cb/h/Bearer abcdefghij.klmnopqrst"))
    assert "abcdefghij.klmnopqrst" not in redacted, redacted
    assert "Bearer [REDACTED]" in redacted, redacted
    # Partner: the route shape around it survived, so this did not pass by the
    # whole value being blanked.
    assert redacted.startswith("/cb/h/"), redacted


# ---------------------------------------------------------------------------
# 5. The CALL SITES. The tests above prove the allowlist contains `cause_type`
#    and `error_type`; they prove nothing about any call site passing them.
#    Review deleted all three additions and all 1999 backend tests stayed green
#    -- the line silently degrades to `cause=<str len=57>`, which is the #361
#    symptom in miniature.
# ---------------------------------------------------------------------------


def test_the_llm_fallback_line_names_the_failure_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED if ``llm/fallback.py`` renames ``cause_type`` back to ``cause``."""
    import asyncio

    from app.llm.errors import LLMUnavailable
    from app.llm.fallback import FallbackLLMClient
    from app.llm.types import LLMResult

    class Dead:
        async def complete(self, **kwargs: object) -> LLMResult:
            raise LLMUnavailable("gemini returned 503")

        async def aclose(self) -> None: ...

    class Alive:
        async def complete(self, **kwargs: object) -> LLMResult:
            return LLMResult(
                text="ok", input_tokens=1, output_tokens=1, model="m", provider="router"
            )

        async def aclose(self) -> None: ...

    client = FallbackLLMClient(primary=Dead(), secondary=Alive())  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="citevyn.llm"):
        asyncio.run(client.complete(system="s", user="u", max_tokens=8, temperature=0.0))

    line = "\n".join(build_log_formatter().format(r) for r in caplog.records)
    assert "llm_primary_unavailable_falling_back" in line, line
    assert "cause_type='LLMUnavailable'" in line, (
        f"the operator cannot tell WHY the primary failed: {line!r}"
    )


def test_the_unfetchable_source_line_names_the_failure_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED if ``worker/cli.py`` drops the ``error_type`` field."""
    from app.worker.allowlist import SourceSpec
    from app.worker.cli import content_version_hash
    from app.worker.fetchers import FetchError

    spec = SourceSpec(name="codex", product_area="codex", title="T", fetcher="local", location="x")

    def boom(_: SourceSpec) -> str:
        raise FetchError(f"cannot read /home/{FAKE_ADDRESS}/doc.md")

    with caplog.at_level(logging.WARNING):
        content_version_hash([spec], fetch=boom)

    line = "\n".join(build_log_formatter().format(r) for r in caplog.records)
    assert "source_version_hash_source_unfetchable" in line, line
    assert "error_type='FetchError'" in line, line
    assert "source='codex'" in line, line
    # PARTNER: the free-text `error` is still suppressed, so `error_type` is
    # not just the same text under a new name.
    assert FAKE_ADDRESS not in line, line
    assert "error=<str len=" in line, line


def test_the_allowlist_contains_no_key_that_redact_value_would_blank() -> None:
    """``redact_value`` matches SECRET_KEY_PARTS/RAW_TEXT_KEYS as SUBSTRINGS of
    the key name, so allowlisting e.g. ``message_id`` (contains ``message``) or
    ``emails_sent`` (contains ``email``) would emit a permanent
    ``[REDACTED_TEXT]`` and look like a broken field rather than a policy. Both
    collisions are real -- measured on the current ``redact_value``.

    RED if someone adds such a key to ``EMITTED_TEXT_KEYS``.
    """
    from app.core.logging import SECRET_VALUE, TEXT_VALUE, redact_value

    # Partner: the collision mechanism this guards against genuinely exists.
    assert redact_value("message_id", "abc") == TEXT_VALUE
    assert redact_value("emails_sent", "abc") == SECRET_VALUE

    blanked = {k for k in EMITTED_TEXT_KEYS if redact_value(k, "sentinel") != "sentinel"}
    assert not blanked, f"allowlisted keys that redact_value blanks anyway: {sorted(blanked)}"


# ---------------------------------------------------------------------------
# #352: the span-of-indexes WARN payload.
# ---------------------------------------------------------------------------

# A REAL index version can be sha-shaped -- ``index_versions.index_version`` is a
# 64-character string PK and this module's own OPAQUE_ID_KEYS comment already names
# "a sha-shaped ``index_version``" as the thing the entropy sweep destroyed. #361's
# recorded lesson is that a test proving a field survives must use the shape the
# application can actually produce, not a friendly literal like ``v1``.
_SHA_VERSION_A = "a" + hashlib.sha256(b"index-a").hexdigest()[:63]
_SHA_VERSION_B = "b" + hashlib.sha256(b"index-b").hexdigest()[:63]


def test_the_spanned_index_versions_reach_the_operator_verbatim() -> None:
    """#352's observability field must PRINT, not render as a shape summary.

    Two independent mechanisms have to hold at once: ``index_versions`` on
    ``EMITTED_TEXT_KEYS`` (a string under an unlisted key becomes ``<str len=N>``)
    AND on ``OPAQUE_ID_KEYS`` (a sha-shaped version is 64 characters inside
    ``HIGH_ENTROPY_RE``'s class, so the sweep would blank it).

    RED if ``index_versions`` is removed from EITHER set.
    """
    assert len(_SHA_VERSION_A) == 64, "the fixture must match the column's real width"
    line = render(
        "retrieval_evidence_spans_multiple_indexes",
        request_id=REAL_REQUEST_ID,
        index_versions=f"{_SHA_VERSION_A},{_SHA_VERSION_B}",
        index_version_count=2,
    )
    assert _SHA_VERSION_A in line, f"the first index version was destroyed: {line!r}"
    assert _SHA_VERSION_B in line, f"the second index version was destroyed: {line!r}"
    assert "[REDACTED]" not in line, line
    assert "<str len=" not in line, line
    # The int rides the scalar branch and needs no allowlist entry at all.
    assert "index_version_count=2" in line, line


def test_the_singular_index_version_key_still_prints_too() -> None:
    """PARTNER: adding the plural must not have been a rename. Both keys are live
    -- ``retrieval_multiple_active_indexes`` and the promotion diagnostics still
    emit the singular.

    RED if ``index_version`` is dropped from ``EMITTED_TEXT_KEYS`` or
    ``OPAQUE_ID_KEYS`` while wiring up the plural.
    """
    line = render("promote", index_version=_SHA_VERSION_A)
    assert _SHA_VERSION_A in line, line
    assert "[REDACTED]" not in line, line


def test_a_plural_index_versions_LIST_is_still_summarised() -> None:
    """PARTNER, and the trap this field was one line away from: the allowlist
    governs STRINGS ONLY. Allowlisting the key does NOT make a list print, so the
    call sites join before logging -- had they passed the list, the operator would
    have received ``index_versions=<list n=2>`` and the field would have looked
    live while carrying nothing.

    RED if ``render_extra_value`` starts stringifying containers under an
    allowlisted key.
    """
    line = render("probe", index_versions=[_SHA_VERSION_A, _SHA_VERSION_B])
    assert "index_versions=<list n=2>" in line, line
    assert _SHA_VERSION_A not in line, line


# ---------------------------------------------------------------------------
# #400: the entropy rule's BOUNDARY. ``\b`` is a transition in ``\w``; the
#       character class is not ``\w``, and the two disagree in BOTH directions,
#       so both ends of every run were bypassable. The boundary is now defined
#       over the class itself.
# ---------------------------------------------------------------------------

# A 64-character sha256, the shape #400 reproduces with. Every character is
# inside ``HIGH_ENTROPY_RE``'s class, so the ONLY thing that can decide whether
# it redacts is the boundary.
_SHA256_SHAPED = hashlib.sha256(b"citevyn-400-fixture").hexdigest()

# Characters that ARE ``\w`` to Python's ``re`` but are NOT in the character
# class. Each one therefore removed the word/non-word transition ``\b`` needed,
# and one of them next to a secret defeated the sweep completely. #400 files the
# LEADING case only; the trailing mirror leaks identically and is covered here.
_WORD_BUT_NOT_IN_CLASS = [
    ("latin_small_e_acute", "\u00e9"),
    ("cyrillic_small_a", "\u0430"),
    ("cjk_zhong", "\u4e2d"),
    ("fullwidth_capital_a", "\uff21"),
    ("arabic_indic_zero", "\u0660"),
]

# Characters that are IN the character class but are NOT ``\w``. A ``\b``-anchored
# match could not START (or END) on one, so a value sitting exactly on the
# 32-character floor lost a character of protection and fell under it.
_IN_CLASS_BUT_NOT_WORD = ["-", "+", "/", "="]

# NOT bypasses, before or after. They are not ``\w``, so the shipped ``\b``
# already fired next to them. Listed so the suite does not claim credit for
# three bugs that never existed.
_ALREADY_SAFE_NEIGHBOURS = [
    ("zero_width_joiner", "\u200d"),
    ("rtl_override", "\u202e"),
    ("combining_acute", "\u0301"),
]


def _longest_class_run(text: str) -> int:
    """The maximal run of ``HIGH_ENTROPY_RE``-class characters, scanned
    INDEPENDENTLY of that regex.

    Deliberately not implemented by re-running the rule under test: a check
    keyed on the rule passes under every mutation of the rule.
    """
    best = run = 0
    for char in text:
        run = run + 1 if re.fullmatch(r"[A-Za-z0-9+/=_-]", char) else 0
        best = max(best, run)
    return best


@pytest.mark.parametrize("position", ["leading", "trailing"])
@pytest.mark.parametrize(
    ("name", "neighbour"), _WORD_BUT_NOT_IN_CLASS, ids=[n for n, _ in _WORD_BUT_NOT_IN_CLASS]
)
def test_a_word_character_outside_the_class_no_longer_defeats_the_entropy_sweep(
    name: str, neighbour: str, position: str
) -> None:
    """#400 8a, BOTH ENDS. ``é`` is a word character that is not in the
    character class, so where the run began there was no word/non-word
    transition, ``\\b`` failed, and a 64-character sha printed VERBATIM. It is
    reachable through a URL: uvicorn percent-decodes the path before the app
    sees it, so ``%C3%A9`` arrives as ``é``.

    The issue reports only the LEADING case. Measured here, the TRAILING mirror
    leaks identically for all five shapes -- ``\\b`` fails at the END of the run
    for exactly the same reason -- which is why this is parametrized over both.

    RED if ``HIGH_ENTROPY_RE``'s lookaround boundary is reverted to ``\\b``.
    Verified by making that change: all ten cases fail.
    """
    from app.core.logging import redact_value

    value = (neighbour + _SHA256_SHAPED) if position == "leading" else (_SHA256_SHAPED + neighbour)
    out = str(redact_value("detail", value))

    assert _SHA256_SHAPED not in out, f"{name}/{position} still bypasses the sweep: {out!r}"
    assert SECRET_VALUE in out, out
    # NON-VACUITY: the neighbour really is a ``\w`` character outside the class,
    # which is the whole mechanism. Without this the fixture could drift to some
    # character the sweep never had trouble with and the test would still pass.
    assert re.match(r"\w", neighbour), f"{name} is not a word character; the fixture is wrong"
    assert not re.fullmatch(r"[A-Za-z0-9+/=_-]", neighbour), f"{name} is inside the class"
    # And the neighbour itself survives: this is a boundary fix, not "blank the
    # whole field".
    assert neighbour in out, out


@pytest.mark.parametrize("position", ["leading", "trailing"])
@pytest.mark.parametrize("edge_char", _IN_CLASS_BUT_NOT_WORD)
def test_a_secret_sitting_on_the_floor_is_caught_whichever_class_character_it_ends_with(
    edge_char: str, position: str
) -> None:
    """#400 8b, BOTH ENDS. ``- + / =`` are in the character class but are not
    word characters, so a ``\\b``-anchored match could not begin (or end) on
    one: a 32-character value starting with ``-`` matched only its last 31
    characters, fell under the floor, and printed in full.

    #400 files the leading half and calls it narrow. It is exactly as narrow at
    the other end and exactly as complete a leak there -- measured, all eight
    combinations print today and redact after.

    RED if the lookaround boundary is reverted to ``\\b``. Verified by making
    that change: all eight cases fail.
    """
    from app.core.logging import redact_value

    body = "X" * 31
    value = (edge_char + body) if position == "leading" else (body + edge_char)
    # ON the floor, not over it: at 33 characters the shipped rule simply starts
    # one character in and catches the rest, so only this length shows the bug.
    assert len(value) == 32

    assert redact_value("detail", value) == SECRET_VALUE, (
        f"a 32-character secret {position} on {edge_char!r} printed in full"
    )


@pytest.mark.parametrize("position", ["leading", "trailing"])
@pytest.mark.parametrize(
    ("name", "neighbour"), _ALREADY_SAFE_NEIGHBOURS, ids=[n for n, _ in _ALREADY_SAFE_NEIGHBOURS]
)
def test_zero_width_and_combining_neighbours_were_never_a_bypass(
    name: str, neighbour: str, position: str
) -> None:
    """SCOPE PARTNER for the two tests above, and DELIBERATELY NOT LOAD-BEARING
    -- declared here in the same spirit as the non-behavioural pins named in
    this module's docstring.

    Its job is to stop a reader concluding "unicode next to a secret leaks".
    Zero-width joiner, right-to-left override and a combining acute are NOT
    ``\\w``, so the shipped ``\\b`` already fired beside them and these shapes
    redacted before this change as well as after. Measured, leading and
    trailing, on both rules. Without this, the fix's comment block would be
    claiming credit for three bugs that never existed.

    RED if the entropy sweep stops firing at all, or if its floor is raised
    above 64. It does NOT bite the boundary change -- that is the point.
    """
    from app.core.logging import redact_value

    value = (neighbour + _SHA256_SHAPED) if position == "leading" else (_SHA256_SHAPED + neighbour)
    assert not re.match(r"\w", neighbour), f"{name} IS a word character; the premise is wrong"
    out = str(redact_value("detail", value))
    assert _SHA256_SHAPED not in out, out
    # THE TWO PARTNERS ITS SIBLINGS CARRY AND THIS ONE WAS MISSING. Absence of
    # the sha alone is satisfied by a ``redact_value`` returning ``""`` -- or
    # returning anything at all -- so it is not evidence that the sweep fired.
    # These say what the output IS: the sweep replaced the run, and the
    # neighbour itself survived rather than being swallowed with it.
    assert SECRET_VALUE in out, out
    assert neighbour in out, out


def test_the_entropy_rule_is_not_ascii_only() -> None:
    """THE FALSIFIER. ``re.ASCII`` is the tempting one-token "simplification" of
    this regex and it LEAKS -- and nothing in the suite could see it.

    Measured before this test existed: the ENTIRE backend suite passed under
    ``re.compile(r"\\b[A-Za-z0-9+/=_-]{32,}\\b", re.ASCII)``, a variant that
    prints the value below in full. The flag makes ``\\w`` ASCII-only, so MORE
    characters count as non-word and MORE of them manufacture the boundary a
    secret needs -- the opposite of what it looks like it does.

    RED if the rule is rewritten as ``\\b...\\b`` WITH ``re.ASCII``. That is the
    truthful statement and it is narrower than the obvious one: measured,
    ``re.ASCII`` added to the SHIPPED lookaround pattern alone is a semantic
    no-op and this test stays green under it, because that pattern contains no
    ``\\w``, ``\\b``, ``\\d`` or ``\\s`` for the flag to reach. Which is itself
    part of what the boundary change buys -- after it, the flag cannot do harm
    without a boundary rewrite alongside. Verified by applying both mutants: the
    combination kills this test, the flag alone does not.
    """
    from app.core.logging import redact_value

    # Obviously synthetic. The load-bearing character is ``ª`` (U+00AA), a
    # Unicode word character that is not ASCII: under ``re.ASCII`` it stops
    # being ``\w``, the ``=`` beside it is not ``\w`` either, and the boundary
    # the match needs never exists.
    leaky = "DhR86\u00aa=QY0GJHlwmF0z9vfF9aipERC7sqdPW2g;"
    # NON-VACUITY: the value really does carry a 32-character run of the class,
    # so it is material the rule is supposed to catch and not a short string.
    assert _longest_class_run(leaky) >= 32, leaky

    assert redact_value("detail", leaky) == f"DhR86\u00aa{SECRET_VALUE};"


def test_no_path_segment_keeps_a_32_character_class_run_after_the_sweep() -> None:
    """THE SECURITY PROPERTY BEHIND THE ``MAX_PATH`` TRUNCATION FLIP.

    #400 makes the sweep redact a SUPERSET, so the swept path is SHORTER, so
    tail material the per-segment order used to truncate away now lands inside
    the 160-character ``MAX_PATH`` window. That is the same mechanism
    ``MAX_PATH_SEGMENTS``'s comment block records for #390's rejected arm, and
    it fires here for the same reason.

    It is NOT a regression, and this test is the executable form of that
    argument. The exposed material is sub-32-character by construction -- no
    rule in the module redacts a 27-character literal under any key -- so what
    changed is which under-floor bytes the cap happened to hide, and hiding by
    volume was never redaction. What the output must satisfy is that no
    ``/``-delimited SEGMENT still carries a run of 32 or more class characters.
    The fixture's five segments violate that before the fix (32 characters
    each, every one opening on the 8b bypass -- exactly ON the floor, which is
    the only length at which that bypass bites) and satisfy it after.

    PER SEGMENT, not over the whole string, and that is not a weakening: ``/``
    is itself inside the character class, so any path at all is one long run
    when read whole -- that IS #384, and the per-segment sweep is the rule.

    There is deliberately NO test asserting the 27-character literal is
    VISIBLE. That would lock in a defect and go red the day someone improves
    the cap.

    RED if the lookaround boundary is reverted to ``\\b``. Verified by making
    that change: the scan reports segments at a run of 32.
    """
    from app.core.logging import MAX_PATH, MAX_PATH_SEGMENTS, redact_value

    # Each segment is 32 characters of the class, opening on ``-`` -- the exact
    # 8b bypass, so the shipped rule matched none of them and the whole value
    # overflowed ``MAX_PATH``. The tail literal is this file's own sub-floor
    # fixture, 27 characters, which NO sweep in the module removes.
    segment = "-" + "X" * 31
    assert len(segment) == 32
    crafted = "".join("/" + segment for _ in range(5)) + "/" + FAKE_SUB_FLOOR_KEY
    assert crafted.count("/") <= MAX_PATH_SEGMENTS, (
        "the fixture must take the sweep, not the marker arm"
    )
    assert len(crafted) > MAX_PATH, (
        f"the fixture must overflow MAX_PATH={MAX_PATH} so the truncation is in play"
    )

    out = str(redact_value("path", crafted))
    runs = [(seg, _longest_class_run(seg)) for seg in out.split("/")]
    offenders = [(seg, run) for seg, run in runs if run >= 32]
    assert offenders == [], (offenders, out)
    # NON-VACUITY, two ways. The scan is not blind -- it reports 32 on the RAW
    # segment, so an empty result above means the sweep acted...
    assert _longest_class_run(segment) == 32
    # ...and the sweep really did fire on all five, so this is not passing on an
    # output the cap emptied.
    assert out.count(SECRET_VALUE) == 5, out


def test_the_whole_string_sweep_leaves_no_32_character_class_run_either() -> None:
    """The same invariant on the DEFAULT branch, where it applies to the whole
    value because there are no segments: after ``redact_value`` under a
    non-path key, no run of 32 or more class characters survives. This is the
    maximal-run property the new boundary buys, stated where it holds without
    qualification.

    RED if the lookaround boundary is reverted to ``\\b`` (the leading-``é``
    probe leaves a 64-character run standing).
    """
    from app.core.logging import redact_value

    probes = [
        "\u00e9" + _SHA256_SHAPED,
        _SHA256_SHAPED + "\u00e9",
        "-" + "X" * 31,
        "X" * 31 + "-",
        "/v1/sessions/0f0d1a2b-3c4d-5e6f-7a8b-9c0d1e2f3a4b/messages",
    ]
    for probe in probes:
        # NON-VACUITY: each probe really does carry the run being tested for.
        assert _longest_class_run(probe) >= 32, probe
        assert _longest_class_run(str(redact_value("detail", probe))) < 32, probe


# ---------------------------------------------------------------------------
# #387: legitimate long PROVIDER MODEL SLUGS must reach the operator.
#       The ``error`` half is DECLINED -- see MODEL_ID_KEYS and the last test
#       in this section.
# ---------------------------------------------------------------------------

# Provider slugs this repo or #387 NAMES, both measured collapsing to
# ``[REDACTED]`` before this change because every character is inside
# ``HIGH_ENTROPY_RE``'s class and there is no separator to break the run.
#
# ONLY TWO, and the shortfall is deliberate rather than an oversight. A third
# candidate, ``google/gemini-2.5-flash-lite:thinking`` (37 characters), is NOT a
# fixture for this test: its ``.`` and ``:`` are outside the class, so its
# longest run is 15 and it never over-blanked at all. It is a headroom data
# point, not a defect shape, and using it here would have made this test pass
# for the wrong reason.
_OVER_BLANKED_SLUGS = [
    # Named at ``app/cost/pricing.py:177`` as a model that WOULD have been
    # mis-billed -- i.e. a slug this codebase already anticipates seeing.
    "openai/gpt-4o-mini-realtime-preview",
    # #387's own measured example.
    "anthropic/claude-3-5-sonnet-20240620",
]

# THE KEY LIST IS A LITERAL, NOT ``sorted(MODEL_ID_KEYS)``, and that is the
# whole difference between a guard and a tautology. Parametrizing over the set
# under test makes the parametrization SHRINK when a key is removed: the case
# for that key is simply never generated, the suite stays green, and the
# coverage vanishes silently. Measured -- dropping ``index_embedding_model`` or
# ``configured_embedding_model`` from ``MODEL_ID_KEYS`` SURVIVED the whole
# selection until this list was written out by hand.
_MODEL_KEYS_UNDER_TEST = [
    "model",
    "index_embedding_model",
    "configured_embedding_model",
]


def test_the_model_key_set_is_exactly_the_set_under_test() -> None:
    """The partner that makes the parametrization below honest: it pins the set
    BYTE-EXACTLY, so removing a key reddens here even though the parametrized
    cases for it stop being generated, and ADDING one reddens here rather than
    slipping in untested.

    RED if any key is added to or removed from ``MODEL_ID_KEYS``. Verified by
    removing each of the three in turn and by adding a fourth.
    """
    assert set(MODEL_ID_KEYS) == set(_MODEL_KEYS_UNDER_TEST)
    assert len(_MODEL_KEYS_UNDER_TEST) == len(set(_MODEL_KEYS_UNDER_TEST))


@pytest.mark.parametrize("key", _MODEL_KEYS_UNDER_TEST)
@pytest.mark.parametrize("slug", _OVER_BLANKED_SLUGS)
def test_a_long_provider_slug_reaches_the_operator_under_every_model_key(
    key: str, slug: str
) -> None:
    """#387. A slug of 32 class characters or more is one contiguous run, so the
    whole-string sweep collapsed it and every spend and provider line read
    ``model=[REDACTED]`` -- the #384 symptom on the cost path.

    All THREE keys are covered, not just ``model``. ``index_embedding_model``
    and ``configured_embedding_model`` (``app/retrieval/hybrid.py:532``,
    ``:546``, ``:549``) carry the same slug space, and because the membership
    test is on the WHOLE key neither inherits ``model``'s exemption. They would
    have failed identically, one config change later.

    RED if a key is removed from ``MODEL_ID_KEYS``. Verified by removing each of
    the three in turn: that key's two cases fail with ``'[REDACTED]'``.
    """
    from app.core.logging import redact_value

    # NON-VACUITY: the slug really is over the floor and really is one unbroken
    # class run, so this is the shape that used to blank.
    assert len(slug) >= 32, slug
    assert re.fullmatch(r"[A-Za-z0-9+/=_-]+", slug), slug
    assert len(slug) <= MAX_MODEL_ID, "the fixture must sit under the cap, not over it"

    assert redact_value(key, slug) == slug
    # And it reaches the EMITTED LINE, not just ``redact_value``'s return: the
    # key has to be on ``EMITTED_TEXT_KEYS`` too, which is a separate mechanism.
    assert render("provider_call_unpriced", **{key: slug}) == (
        f"provider_call_unpriced {key}={slug!r}"
    )


def test_an_over_cap_model_value_emits_a_marker_carrying_none_of_its_input() -> None:
    """THE EXEMPTION'S CEILING, and why it is a MARKER rather than a truncation.

    ``model`` is not merely env-configurable as #387 says: on the cost-meter
    path it is UPSTREAM-SUPPLIED. ``app/llm/openrouter.py:119`` and
    ``app/llm/anthropic.py:116`` both read ``data.get("model", self._model)``
    out of the provider's HTTP response body, and it reaches
    ``app/cost/meter.py:61`` as ``extra={"model": model}`` -- from
    ``provider_call_unpriced``, which fires exactly when the value is one we did
    not choose. Truncating would emit a PREFIX of that string; the marker emits
    none of it.

    RED if the over-cap arm returns a truncation (``redacted[:MAX_MODEL_ID] +
    ...``) instead of the marker. Verified by making that change: the
    eight-character window check fails.
    """
    from app.core.logging import redact_value

    hostile = "sk-live-" + "Zq7" * 60
    assert len(hostile) > MAX_MODEL_ID

    out = str(redact_value("model", hostile))
    assert out == f"{SECRET_VALUE}…<{len(hostile)} chars>", out
    # No eight-character window of the input survives -- stronger than "the
    # value is absent" and independent of where a cut would have landed.
    windows = [hostile[i : i + 8] for i in range(len(hostile) - 7)]
    assert [w for w in windows if w in out] == [], out

    # PARTNER (non-vacuity), because the assertions above are satisfied by a
    # ``redact_value`` that blanks every model: a real slug still prints whole.
    #
    # THE PARTNER MUST BE OVER THE 32-CHARACTER FLOOR, and the first version of
    # it was not. It used ``google/gemini-2.5-flash`` -- 23 characters, longest
    # class run 15 -- which the entropy sweep never touched in the first place,
    # so it passed with ``MODEL_ID_KEYS`` emptied ENTIRELY and proved nothing
    # about the exemption. Measured; that is the exact vacuity this partner
    # exists to rule out. This slug is 36 characters in one unbroken class run,
    # so it prints ONLY because the exemption is live.
    live_slug = "anthropic/claude-3-5-sonnet-20240620"
    assert _longest_class_run(live_slug) >= 32, live_slug
    assert redact_value("model", live_slug) == live_slug


def test_the_model_cap_fires_only_ABOVE_the_limit() -> None:
    """The comparison, not the cap. ``len(redacted) > MAX_MODEL_ID`` and
    ``>= MAX_MODEL_ID`` both answer every over-cap fixture, so the test above
    passes under either -- and a ``>=``-versus-``>`` mutant has survived a whole
    review round in this file's history. Under ``>=`` a slug of exactly
    ``MAX_MODEL_ID`` characters is replaced by a marker claiming it was over the
    cap: a log line that lies about itself.

    RED if the comparison is loosened to ``>=`` (the at-limit half fails) or
    raised to ``MAX_MODEL_ID + 1`` (the one-over half fails). Verified by making
    both changes.
    """
    from app.core.logging import redact_value

    at_limit = "m" * MAX_MODEL_ID
    one_over = "m" * (MAX_MODEL_ID + 1)

    assert redact_value("model", at_limit) == at_limit
    assert redact_value("model", one_over) == f"{SECRET_VALUE}…<{MAX_MODEL_ID + 1} chars>"


@pytest.mark.parametrize("key", _MODEL_KEYS_UNDER_TEST)
def test_a_model_key_keeps_the_bearer_sweep_it_only_skips_the_entropy_one(key: str) -> None:
    """THE EXEMPTION IS FROM ``HIGH_ENTROPY_RE`` ONLY, AND NOTHING TESTED THAT.

    ``model`` is UPSTREAM-SUPPLIED -- ``app/llm/openrouter.py:119`` and
    ``app/llm/anthropic.py:116`` read it out of the provider's HTTP response
    body -- so a provider string carrying ``Bearer <token>`` is not a
    hypothetical, and under this key the value is printed rather than summarised.
    The arm is written to return the SWEPT string for exactly that reason, and
    no fixture in this file put a Bearer token under a model key.

    RED if the arm returns ``value`` instead of ``redacted`` (i.e. skips
    ``BEARER_RE.sub`` as well). Verified by making that change on the real
    module: all three cases fail with the token printed in full.
    """
    from app.core.logging import redact_value

    token = "NOTAREALTOKEN" + "9" * 17
    value = f"m Bearer {token}"
    # NON-VACUITY: the token really is present, and the value really does take
    # the under-cap branch rather than the marker, so this measures the sweep.
    assert token in value
    assert len(value) <= MAX_MODEL_ID

    out = str(redact_value(key, value))
    assert out == f"m Bearer {SECRET_VALUE}", out
    assert token not in out, out


def test_the_model_exemption_is_exact_not_substring() -> None:
    """The membership test is ``key_lower in MODEL_ID_KEYS`` -- WHOLE KEY -- and
    the comment at ``MODEL_ID_KEYS`` says so, but nothing measured it.

    Same shape as ``test_the_opaque_id_exemption_is_exact_not_substring`` above,
    and for the same reason: ``any(part in key_lower for part in ...)`` is the
    idiom used three lines earlier in ``redact_value`` for ``RAW_TEXT_KEYS`` and
    ``SECRET_KEY_PARTS``, so copying it here is a one-token slip that hands the
    exemption to every key CONTAINING ``model`` -- ``outer_model``,
    ``model_secret_note``, anything.

    RED if the whole-key test is loosened to a substring test. Verified by
    making that change: ``outer_model`` then prints the slug.
    """
    from app.core.logging import redact_value

    slug = "anthropic/claude-3-5-sonnet-20240620"
    # NON-VACUITY: the value is over the floor in ONE class run, so the entropy
    # sweep is what decides, and the key really does contain a real member.
    assert _longest_class_run(slug) >= 32, slug
    assert "model" in "outer_model" and "outer_model" not in MODEL_ID_KEYS

    assert redact_value("outer_model", slug) == SECRET_VALUE
    # PARTNER: the exemption really is live for the exact key, so the assertion
    # above is about the MATCHING RULE and not about a dead exemption.
    assert redact_value("model", slug) == slug


def test_the_over_cap_model_marker_counts_the_SWEPT_value_not_the_raw_one() -> None:
    """The count in the marker is ``len(redacted)``, and the comment says so.
    Every other over-cap fixture in this file has no Bearer token in it, so the
    swept and raw lengths are EQUAL and ``len(value)`` passes them all.

    It matters twice over. The comparison that chose this branch measured the
    swept length, so a raw count describes a different string than the one the
    branch was taken for; and the count is the one thing an operator can act on
    -- a marker that overstates by the length of a collapsed token invites
    "the provider sent 148 characters" when it sent 98 of them post-redaction.

    RED if the marker is built from ``len(value)``. Verified by making that
    change: the expected/actual counts differ by 50.
    """
    from app.core.logging import redact_value

    raw = "Bearer " + "T" * 60 + " " + "x" * 80
    swept = "Bearer " + SECRET_VALUE + " " + "x" * 80
    # NON-VACUITY, all three legs: both lengths clear the cap (so the marker is
    # what fires, not the passthrough), and they DIFFER (so the two
    # implementations are distinguishable at all).
    assert len(raw) > MAX_MODEL_ID and len(swept) > MAX_MODEL_ID
    assert len(raw) != len(swept), (len(raw), len(swept))

    assert redact_value("model", raw) == f"{SECRET_VALUE}…<{len(swept)} chars>"


def test_the_three_key_name_sets_are_pairwise_disjoint() -> None:
    """``redact_value`` tests ``OPAQUE_ID_KEYS``, then ``MODEL_ID_KEYS``, then
    ``PATH_KEYS``, and every arm RETURNS. A key in two sets therefore takes
    whichever is tested first and skips the other silently -- on a collision the
    WEAKER rule wins, which is what ``PATH_KEYS``'s precedence note says must
    never happen. That was prose only until this test.

    THE PAIR LIST IS MULTI-ITEM AND ITS FIRST ENTRY IS COMPLIANT, deliberately:
    a ``break``-versus-``continue`` mistake, or an early ``return``, has
    silently disabled a guard in this repo before, and a single-item or
    first-item-offending fixture cannot see it.

    RED if a key is added to two of the three sets. Verified by adding
    ``"path"`` to ``MODEL_ID_KEYS``.
    """
    pairs: list[tuple[str, frozenset[str], str, frozenset[str]]] = [
        ("OPAQUE_ID_KEYS", OPAQUE_ID_KEYS, "MODEL_ID_KEYS", MODEL_ID_KEYS),
        ("OPAQUE_ID_KEYS", OPAQUE_ID_KEYS, "PATH_KEYS", PATH_KEYS),
        ("MODEL_ID_KEYS", MODEL_ID_KEYS, "PATH_KEYS", PATH_KEYS),
    ]
    assert len(pairs) == 3

    def overlaps(candidates: list[tuple[str, frozenset[str], str, frozenset[str]]]) -> list[str]:
        found: list[str] = []
        for name_a, set_a, name_b, set_b in candidates:
            shared = set_a & set_b
            if shared:
                found.append(f"{name_a} & {name_b} = {sorted(shared)}")
        return found

    assert overlaps(pairs) == [], (
        "two key-name sets share a key, so one of the two rules is now dead for "
        "it and the branch order decides which -- see PATH_KEYS's precedence note"
    )
    # SELF-CHECK: the walk really does reach every pair and really can report, so
    # the empty result above is a measurement rather than a broken loop. The
    # offender is planted LAST, behind three compliant pairs.
    planted = [*pairs, ("planted", frozenset({"model"}), "planted_twin", frozenset({"model"}))]
    assert overlaps(planted) == ["planted & planted_twin = ['model']"]

    # THE FOURTH AND FIFTH SETS, WHICH THE PAIRWISE WALK ABOVE CANNOT SEE.
    # ``RAW_TEXT_KEYS`` and ``SECRET_KEY_PARTS`` are tested FIRST in
    # ``redact_value``, BEFORE any of the three above, and they match on
    # SUBSTRING rather than on the whole key. So they shadow by containment, not
    # by equality, and set intersection finds nothing: a future exempt key named
    # ``embedding_model_token`` is disjoint from all three sets above and would
    # still be answered by ``SECRET_KEY_PARTS``'s ``token`` before its own arm
    # is reached -- the exemption silently dead, this test still green.
    shadowed = [
        f"{set_name}:{key!r} is shadowed by the earlier substring rule {part!r}"
        for set_name, keys in (
            ("OPAQUE_ID_KEYS", OPAQUE_ID_KEYS),
            ("MODEL_ID_KEYS", MODEL_ID_KEYS),
            ("PATH_KEYS", PATH_KEYS),
        )
        for key in keys
        for part in (*SECRET_KEY_PARTS, *RAW_TEXT_KEYS)
        if part in key.lower()
    ]
    assert shadowed == [], (
        "a whole-key exemption contains a SECRET_KEY_PARTS/RAW_TEXT_KEYS "
        "substring, so redact_value answers it before the exemption's own arm "
        "and the exemption is dead -- see the precedence note at PATH_KEYS"
    )
    # SELF-CHECK, same discipline as the plant above: the comprehension really
    # can report. ``token`` is a real ``SECRET_KEY_PARTS`` member and this key
    # really does contain it.
    assert [
        f"planted:{key!r} shadowed by {part!r}"
        for key in ("embedding_model_token",)
        for part in SECRET_KEY_PARTS
        if part in key
    ] == ["planted:'embedding_model_token' shadowed by 'token'"]


def test_the_error_key_is_deliberately_not_exempted() -> None:
    """#387's OTHER HALF, DECLINED, and this is the executable record of that
    decision rather than a comment nobody re-checks.

    ``error`` carries arbitrary ``str(exc)`` text. The ``EMITTED_TEXT_KEYS``
    comment names ``cause``/``error``/``reason`` as deliberately excluded on
    exactly that ground, so exempting ``error`` from the sweep would let a
    secret embedded in an exception message print. What #387 loses instead is a
    filename out of an ``OSError`` string, which #387 itself rates low because
    ``source=spec.name`` survives -- and it does.

    RED if ``error`` is ever added to ``EMITTED_TEXT_KEYS`` or to
    ``MODEL_ID_KEYS``. Verified by adding it to each.
    """
    assert "error" not in EMITTED_TEXT_KEYS
    assert "error" not in MODEL_ID_KEYS
    # On the ``extra=`` path the value never reaches the line at all, so the
    # sweep's behaviour under this key is not what an operator sees there.
    line = render("boom", error="[Errno 13] Permission den", source="codex")
    assert "error=<str len=25>" in line, line
    # PARTNER: the line stays diagnostic, which is why the loss is acceptable.
    assert "source='codex'" in line, line
    # On the ``build_log_event`` path it DOES reach the line, and a
    # secret-shaped run inside it must still blank.
    event = build_log_event("source_unfetchable", error=f"cannot read {_SHA256_SHAPED}")
    assert _SHA256_SHAPED not in str(event["error"]), event
    assert "cannot read" in str(event["error"]), event


# ---------------------------------------------------------------------------
# #401: the WORK guard. Every other constant in the module bounds the OUTPUT;
#       this one bounds the SCAN. Above MAX_SCANNED_CHARS a string value is
#       answered by a marker and no regex runs on it at all.
# ---------------------------------------------------------------------------


class _RecordingPattern:
    """A ``re.Pattern`` stand-in that records EVERY attribute the module uses.

    Written fresh rather than reusing ``_CountingPattern`` above, which RAISES on
    any non-``sub`` attribute: that is the right probe for the marker arm and the
    wrong one here, because this test wants to REPORT what ran rather than fail
    on the first non-``sub`` use. The recorder still sees ``search``, ``match``
    and friends, so a mutant restoring work through a different method is
    visible instead of invisible.
    """

    def __init__(self, inner: re.Pattern[str], name: str) -> None:
        self._inner = inner
        self._name = name
        self.uses: list[str] = []

    def sub(self, repl: str, string: str) -> str:
        self.uses.append("sub")
        return self._inner.sub(repl, string)

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "uses").append(name)
        return getattr(object.__getattribute__(self, "_inner"), name)


def test_an_over_length_value_runs_no_regex_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """#401 IS A COST DEFECT, so the assertion that measures it is one about
    WORK. ``BEARER_RE.sub`` is an unbounded O(len) scan that ran over every
    string value; #390 hoisted the ``path`` marker arm above it, so ``path``
    stopped paying it while ``method``, ``request_id`` and every other key still
    did. ``request_id`` is the reachable one -- ``app/core/middleware.py``
    honours an inbound ``X-Request-ID`` -- and ``MAX_OPAQUE_ID`` bounded its
    OUTPUT while nothing bounded the scan.

    Asserting the output alone cannot see this: moving the guard BELOW
    ``BEARER_RE.sub`` produces a byte-identical result for every input.

    RED if the ``MAX_SCANNED_CHARS`` guard is moved below ``BEARER_RE.sub``, or
    if it stops returning early. Verified by moving it: ``bearer.uses`` becomes
    ``['sub']`` on every key.
    """
    from app.core import logging as logging_module

    bearer = _RecordingPattern(logging_module.BEARER_RE, "BEARER_RE")
    entropy = _RecordingPattern(logging_module.HIGH_ENTROPY_RE, "HIGH_ENTROPY_RE")
    monkeypatch.setattr(logging_module, "BEARER_RE", bearer)
    monkeypatch.setattr(logging_module, "HIGH_ENTROPY_RE", entropy)

    crafted = "z" * (MAX_SCANNED_CHARS + 1)
    for key in ("request_id", "method", "path", "model", "detail"):
        bearer.uses.clear()
        entropy.uses.clear()
        out = logging_module.redact_value(key, crafted)
        assert out == f"{SECRET_VALUE}…<{len(crafted)} chars>", (key, out)
        assert bearer.uses == [], (key, bearer.uses)
        assert entropy.uses == [], (key, entropy.uses)

    # PARTNER, because zeros would also be produced by a ``redact_value`` that
    # never scans anything: on a value UNDER the guard both patterns really run.
    bearer.uses.clear()
    entropy.uses.clear()
    assert logging_module.redact_value("detail", "hello") == "hello"
    assert bearer.uses == ["sub"], bearer.uses
    assert entropy.uses == ["sub"], entropy.uses


def test_the_work_guard_emits_no_window_of_its_input() -> None:
    """THE REASON IT IS A MARKER AND NOT A LENGTH PRE-CHECK THAT THEN SWEEPS.

    #401 suggests a pre-check and names the trap: cutting before sweeping leaves
    a sub-floor prefix the later sweep no longer matches, and it prints. That
    trap is REAL for ``HIGH_ENTROPY_RE`` -- a 40-character run cut to 31 falls
    under the floor -- and the marker is immune to it because it emits no
    character of its input.

    (The trap as #401 states it, for ``BEARER_RE``, is REFUTED: that pattern is
    anchored on the literal ``Bearer`` and has no length floor, so a cut inside
    the token still matches. Measured, and recorded at ``MAX_SCANNED_CHARS``.)

    RED if the guard returns a truncation of ``value`` instead of the marker.
    Verified by making that change.
    """
    from app.core.logging import redact_value

    secret = "NOTAREALSECRET" + "0123456789abcdefghij" * 3
    crafted = secret + "q" * (MAX_SCANNED_CHARS + 1 - len(secret))
    assert len(crafted) == MAX_SCANNED_CHARS + 1

    out = str(redact_value("detail", crafted))
    windows = [secret[i : i + 8] for i in range(len(secret) - 7)]
    assert [w for w in windows if w in out] == [], out
    assert out == f"{SECRET_VALUE}…<{len(crafted)} chars>", out


def test_the_work_guard_fires_only_ABOVE_the_limit() -> None:
    """The COMPARISON, pinned separately. ``> MAX_SCANNED_CHARS`` and
    ``>= MAX_SCANNED_CHARS`` both answer every over-limit fixture, so the tests
    above pass under either; a ``>=``-versus-``>`` mutant survived a whole review
    round in this file's history.

    The material is chosen so the sweep is the IDENTITY on it -- alternating
    ``q.`` gives a maximum class run of one -- so at the limit the output is the
    input exactly and any difference is attributable to the BRANCH rather than
    to redaction or truncation.

    RED if the comparison is loosened to ``>=`` (the at-limit half fails) or
    raised to ``+ 1`` (the one-over half fails). Verified by making both
    changes.
    """
    from app.core.logging import redact_value

    at_limit = "q." * (MAX_SCANNED_CHARS // 2)
    one_over = at_limit + "q"
    assert len(at_limit) == MAX_SCANNED_CHARS
    assert _longest_class_run(at_limit) == 1, "the sweep must be the identity on this fixture"

    assert redact_value("detail", at_limit) == at_limit
    assert redact_value("detail", one_over) == f"{SECRET_VALUE}…<{MAX_SCANNED_CHARS + 1} chars>"


def test_a_str_subclass_cannot_talk_its_way_past_the_work_guard() -> None:
    """The guard reads ``str.__len__(value)`` -- the UNBOUND method -- for the
    same reason the ``MAX_PATH_SEGMENTS`` arm reads ``str.count``: it runs
    BEFORE ``BEARER_RE.sub``, which is what normalises a ``str`` subclass to a
    plain ``str`` everywhere else in this function. With the bound
    ``len(value)`` a subclass supplies its own length and picks the branch.

    RED if the guard is changed to ``len(value)``. Verified by making that
    change.
    """
    from app.core.logging import redact_value

    class LyingStr(str):
        def __len__(self) -> int:
            return 1  # "I am tiny, please scan me in full"

    crafted = LyingStr("z" * (MAX_SCANNED_CHARS + 1))
    # The lie is real, so this cannot pass for the wrong reason.
    assert len(crafted) == 1
    assert str.__len__(crafted) == MAX_SCANNED_CHARS + 1

    assert redact_value("detail", crafted) == f"{SECRET_VALUE}…<{MAX_SCANNED_CHARS + 1} chars>"


def test_every_declared_route_sits_far_below_the_work_guard() -> None:
    """THE DESIGN MARGIN, made executable against REALITY rather than against a
    constant. ``MAX_SCANNED_CHARS``'s comment says no value this app emits comes
    near it; that argument lived only in prose.

    Read off the OpenAPI schema for the same reason
    ``test_the_threshold_clears_every_route_this_app_declares`` does: the API
    routers are attached through include wrappers, so ``app.routes`` yields only
    ``/docs`` and friends.

    RED if a route is declared longer than a twentieth of the guard (410
    characters today), or if ``MAX_SCANNED_CHARS`` is lowered under twenty times
    the longest route. The longest declared route is 48 characters, so the guard
    reads ``48 * 20 <= 8192``.
    """
    from app.main import create_app

    paths = list(create_app().openapi()["paths"])
    # PARTNER: the enumeration really found the API, not just the docs routes.
    assert len(paths) >= 20, (len(paths), sorted(paths))
    assert "/v1/sessions/{session_id}/messages" in paths, sorted(paths)[:10]

    longest = max(paths, key=len)
    assert len(longest) * 20 <= MAX_SCANNED_CHARS, (
        f"the longest declared route is {len(longest)} characters ({longest!r}) and "
        f"MAX_SCANNED_CHARS is {MAX_SCANNED_CHARS}: the margin the constant's comment "
        f"claims is gone, so a real route is approaching the work guard."
    )


# ── #401, the other half: uvicorn's access line ────────────────────────────


def _uvicorn_access_record(path_with_query: str) -> logging.LogRecord:
    """A record shaped exactly like uvicorn's h11/httptools access line.

    The 5-tuple and the format string are verified against the INSTALLED
    uvicorn: ``protocols/http/httptools_impl.py:485`` and ``h11_impl.py:482``
    both log ``'%s - "%s %s HTTP/%s" %d'`` with
    ``(client_addr, method, path_with_query, http_version, status_code)``.
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1", "GET", path_with_query, "1.1", 200),
        exc_info=None,
    )


def test_the_access_log_filter_bounds_an_over_long_arg_and_keeps_the_record_renderable() -> None:
    """#401's second half. ``QUERY_CREDENTIAL_RE.sub`` scanned uvicorn's full
    access line with no ceiling, and after #390 that was the dominant
    per-request cost of a crafted request.

    THE BOUND IS ON THE ARG, NOT ON THE LINE, and that is a correctness
    constraint. Verified against ``uvicorn/logging.py:97-107``, whose
    ``formatMessage`` destructures ``record.args`` into exactly five names
    (lines 99-105) and then calls ``int(status_code)`` (line 106): shortening a
    string arg is safe, but changing the tuple's ARITY or an element's TYPE
    raises inside the formatter and DROPS the record.

    RED, AND THE MECHANISM STATED HONESTLY, because the first version of this
    line named one that is false. Removing the bound does NOT make the
    credential reappear: ``QUERY_CREDENTIAL_RE`` still matches the ``token=``
    parameter at any length and still redacts it, so the ``"S3cr3t" not in
    line`` assertion below stays GREEN under that mutant. What actually reddens
    is the MARKER assertion -- unbounded, the line carries
    ``?token=[REDACTED]`` and no ``…<N chars>`` at all. Verified by removing the
    bound from ``_redact_arg``: exactly that one assertion fails.

    RED also if ``_redact_arg`` changes the tuple's shape (the arity and type
    assertions fail). Verified by making that change.
    """
    from app.core.logging import RedactQueryCredentialsFilter

    secret = "S3cr3t" * 4000
    crafted = "/v1/auth/magic-link/confirm?token=" + secret
    assert len(crafted) > MAX_SCANNED_CHARS

    record = _uvicorn_access_record(crafted)
    assert RedactQueryCredentialsFilter().filter(record) is True

    args = record.args
    assert isinstance(args, tuple)
    # ARITY and TYPES intact, which is what keeps the record renderable.
    assert len(args) == 5, args
    assert args[4] == 200 and type(args[4]) is int, args[4]
    assert all(isinstance(a, str) for a in args[:4]), args

    line = record.getMessage()
    # THE LOAD-BEARING ASSERTION. This is the only one the bound moves.
    assert f"{SECRET_VALUE}…<{len(crafted)} chars>" in line, line
    # ABSENCE, kept only because it has a PARTNER proving the thing counted
    # exists: the same 6-character run really is present in the input, in bulk,
    # so "not in the output" is a measurement rather than a vacuous truth. On
    # its own this assertion proves nothing here -- see the RED note above.
    assert crafted.count("S3cr3t") == 4000, "the fixture lost its material"
    assert "S3cr3t" not in line, line
    # PARTNER: the record is still a usable access line, not a blank.
    assert "GET" in line and "200" in line and "HTTP/1.1" in line, line


@pytest.mark.parametrize("args", [None, ()], ids=["args_none", "args_empty_tuple"])
def test_the_args_FALSY_branch_bounds_the_msg_itself(args: tuple[object, ...] | None) -> None:
    """THE OTHER HALF OF THE ``if record.args`` SPLIT, and the reason the split
    exists at all -- and nothing in this file reached it. Every other filter
    test builds a record with uvicorn's truthy 5-tuple, so they all take the
    args-PRESENT branch, and a mutant that deleted the bound from the else arm
    survived the whole selection.

    Both falsy shapes are covered because they arrive differently: ``args=None``
    is a bare ``logger.info(msg)``, ``args=()`` is a caller that passed an empty
    tuple. ``LogRecord.getMessage`` skips interpolation for both, so ``msg`` is
    NOT a format string and bounding it cannot break rendering -- which is the
    whole argument for treating it differently from the args-present case.

    RED if the else arm stops bounding (e.g. reverts to a bare
    ``QUERY_CREDENTIAL_RE.sub``). Verified by making that change: the marker
    assertion fails and the 8,200-character msg is emitted whole.
    """
    from app.core.logging import RedactQueryCredentialsFilter

    token = "NOTAREALTOKEN" + "7" * 30
    msg = "GET /cb?token=" + token + " " + "z" * MAX_SCANNED_CHARS
    assert len(msg) > MAX_SCANNED_CHARS

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=args,
        exc_info=None,
    )
    assert RedactQueryCredentialsFilter().filter(record) is True

    # The msg was replaced by the marker, carrying none of its input.
    assert record.msg == f"{SECRET_VALUE}…<{len(msg)} chars>", record.msg
    # AND THE RECORD IS STILL RENDERABLE -- the point of the falsy-args split.
    assert record.getMessage() == f"{SECRET_VALUE}…<{len(msg)} chars>"
    # PARTNER, so this is not "the filter blanks everything": a SHORT msg with
    # falsy args still takes the ordinary substitution and keeps its shape.
    short = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="GET /cb?token=" + token,
        args=args,
        exc_info=None,
    )
    RedactQueryCredentialsFilter().filter(short)
    assert short.getMessage() == f"GET /cb?token={SECRET_VALUE}"
    assert token not in short.getMessage()


def test_a_str_subclass_cannot_talk_its_way_past_the_access_log_bound() -> None:
    """``_redact_arg`` reads ``str.__len__(arg)`` -- the UNBOUND method -- and its
    comment says why, but the only test of that discipline went through
    ``redact_value``, never through the FILTER. They are separate call sites and
    a mutant only has to be applied to one of them.

    It is reachable in principle for the same reason it is in ``redact_value``:
    the filter runs before any ``sub`` normalises a subclass to a plain ``str``,
    so with the bound ``len(arg)`` an object supplies its own length and picks
    its own branch.

    RED if ``_redact_arg`` uses ``len(arg)``. Verified by making that change:
    the marker assertion fails and the record renders the full value.
    """
    from app.core.logging import RedactQueryCredentialsFilter

    class LyingStr(str):
        def __len__(self) -> int:
            return 1  # "I am tiny, please scan me in full"

    crafted = LyingStr("/cb?token=" + "z" * MAX_SCANNED_CHARS)
    # The lie is real, so this cannot pass for the wrong reason.
    assert len(crafted) == 1
    assert str.__len__(crafted) > MAX_SCANNED_CHARS

    record = _uvicorn_access_record(crafted)
    assert RedactQueryCredentialsFilter().filter(record) is True

    args = record.args
    assert isinstance(args, tuple)
    assert args[2] == f"{SECRET_VALUE}…<{str.__len__(crafted)} chars>", args[2]
    # AND THE CONSUMER SEES IT -- the emitted line, not just the tuple.
    assert f"{SECRET_VALUE}…<{str.__len__(crafted)} chars>" in record.getMessage()


def test_the_access_log_arg_bound_fires_only_ABOVE_the_limit() -> None:
    """THE THIRD MARKER SITE'S COMPARISON, pinned like the other two.

    ``redact_value``'s guard and the ``MAX_MODEL_ID`` cap each have an at-limit
    partner; ``_redact_arg`` did not, and a ``>``-to-``>=`` mutant on it survived
    the FULL backend suite while the identical mutant at the other two sites is
    killed. Found by the skeptic round on the fix round.

    No leak either way -- ``>=`` redacts strictly more -- but at the limit it
    emits a line claiming the value exceeded a bound it did not exceed, and an
    operator reading the marker is told a false thing about a value we still
    hold in full.

    The fixture carries no ``token=``/``code=`` marker, so
    ``QUERY_CREDENTIAL_RE`` is the identity on it and any difference at the
    boundary is attributable to the BRANCH rather than to redaction.

    RED if the comparison is loosened to ``>=`` (the at-limit half fails) or
    raised to ``+ 1`` (the one-over half fails). Verified by making both changes.
    """
    from app.core.logging import RedactQueryCredentialsFilter

    at_limit = "q." * (MAX_SCANNED_CHARS // 2)
    one_over = at_limit + "q"
    assert len(at_limit) == MAX_SCANNED_CHARS
    assert QUERY_CREDENTIAL_RE.sub("x", at_limit) == at_limit, "the sub must be the identity here"

    filt = RedactQueryCredentialsFilter()

    record = _uvicorn_access_record(at_limit)
    assert filt.filter(record) is True
    args = record.args
    assert isinstance(args, tuple)
    assert args[2] == at_limit, "at exactly the limit the value must pass through untouched"

    record = _uvicorn_access_record(one_over)
    assert filt.filter(record) is True
    args = record.args
    assert isinstance(args, tuple)
    assert args[2] == f"{SECRET_VALUE}…<{MAX_SCANNED_CHARS + 1} chars>", args[2]


def test_the_access_log_filter_still_redacts_a_short_crafted_line() -> None:
    """NON-VACUITY PARTNER for the bound above, which would be satisfied by a
    filter that replaced every line with a marker. A normal-length line still
    takes the ordinary substitution and still loses its credential.

    RED if ``QUERY_CREDENTIAL_RE`` stops being applied under the threshold --
    e.g. if ``_redact_arg`` returns ``arg`` unchanged there.
    """
    from app.core.logging import RedactQueryCredentialsFilter

    secret = "a" * 32 + "." + "b" * 64
    record = _uvicorn_access_record(f"/v1/auth/magic-link/confirm?token={secret}")
    RedactQueryCredentialsFilter().filter(record)

    line = record.getMessage()
    assert secret not in line, line
    assert f"/v1/auth/magic-link/confirm?token={SECRET_VALUE}" in line, line
    assert "…<" not in line, "a short line must not be marked as bounded"


def test_the_bounded_record_survives_uvicorns_real_access_formatter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """THE CONSUMER, not the filter's return value. This repo has shipped a guard
    that pinned a constant while the emitted artifact went unchecked, and the
    failure mode here is specifically invisible from inside the filter.

    If a bound were applied to ``record.msg`` while ``record.args`` were present,
    ``LogRecord.getMessage``'s ``msg % self.args`` would raise,
    ``logging.Handler.handleError`` would catch it, and it would then print the
    record's UNREDACTED ``args`` to stderr -- turning one redacted log line into
    a plaintext credential dump. So the record is rendered through uvicorn's OWN
    ``AccessFormatter`` and stderr is captured and asserted EMPTY.

    RED if the filter rewrites ``record.msg`` while args are present (stderr
    fills with ``--- Logging error ---`` and the raw token, and the stream is
    empty). Verified by making that change.
    """
    import io

    from uvicorn.logging import AccessFormatter

    from app.core.logging import RedactQueryCredentialsFilter

    secret = "S3cr3t" * 4000
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(AccessFormatter(use_colors=False))

    logger = logging.getLogger("citevyn.test.access.401")
    logger.handlers = [handler]
    logger.filters = [RedactQueryCredentialsFilter()]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1",
            "GET",
            "/v1/auth/magic-link/confirm?token=" + secret,
            "1.1",
            200,
        )
    finally:
        logger.handlers = []
        logger.filters = []

    emitted = stream.getvalue()
    # THE RECORD WAS NOT DROPPED.
    assert emitted.strip(), "uvicorn's own formatter dropped the record"
    assert "GET" in emitted and "200" in emitted, emitted
    assert "S3cr3t" not in emitted, emitted
    assert f"{SECRET_VALUE}…<" in emitted, emitted

    # AND STDERR IS CLEAN: no ``--- Logging error ---``, no raw token.
    captured = capsys.readouterr()
    assert captured.err == "", captured.err


def test_a_long_format_string_with_args_is_left_formattable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """THE ARGS-PRESENT BRANCH, which is where bounding ``record.msg`` turns a
    redacted line into a plaintext credential dump.

    The test above cannot see this: uvicorn's own ``msg`` is a 23-character
    constant, so bounding it unconditionally is a NO-OP on every record uvicorn
    emits -- measured, that mutant survived the whole selection. Only a record
    whose FORMAT STRING is over the threshold AND which carries args separates
    the two implementations, so this builds one.

    Under the correct rule the msg is left alone (it is a format string; the
    unbounded scan over it is the residual the filter's docstring states) and
    the ARG is bounded. Under an unconditional bound the msg becomes a marker
    with no ``%s`` left, ``msg % self.args`` raises ``TypeError``,
    ``logging.Handler.handleError`` catches it, and the handler then prints the
    record's UNREDACTED ``args`` -- including the token -- to stderr.

    RED if the ``record.args`` guard is dropped from the ``record.msg`` branch.
    Verified by dropping it: stderr fills with ``--- Logging error ---`` and the
    raw token.
    """
    import io

    from app.core.logging import RedactQueryCredentialsFilter

    secret = "NOTAREALTOKEN-" + "9" * 40
    long_format = "x" * (MAX_SCANNED_CHARS + 1) + " %s"
    assert len(long_format) > MAX_SCANNED_CHARS

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(build_log_formatter())

    logger = logging.getLogger("citevyn.test.access.401.longmsg")
    logger.handlers = [handler]
    logger.filters = [RedactQueryCredentialsFilter()]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.info(long_format, f"/cb?token={secret}")
    finally:
        logger.handlers = []
        logger.filters = []

    emitted = stream.getvalue()
    assert emitted.strip(), "the record was dropped: the format string is no longer formattable"
    assert secret not in emitted, emitted
    assert f"?token={SECRET_VALUE}" in emitted, emitted[-200:]
    # THE ACTUAL PAYLOAD OF THIS TEST: nothing reached stderr. A dropped record
    # here does not merely lose a line -- it PRINTS the unredacted args.
    captured = capsys.readouterr()
    assert "--- Logging error ---" not in captured.err, captured.err
    assert secret not in captured.err, "the credential was dumped to stderr"
    assert captured.err == "", captured.err
