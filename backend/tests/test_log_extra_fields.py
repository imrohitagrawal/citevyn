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
    OPAQUE_ID_KEYS,
    RESERVED_RECORD_ATTRS,
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

    Dots separate the segments on purpose, and the key is ``model``, not
    ``path``. ``HIGH_ENTROPY_RE``'s character class includes ``/``, so under any
    key OUTSIDE ``PATH_KEYS`` a long slash-separated value is one contiguous
    32+-char run and gets blanked to ``[REDACTED]`` BEFORE truncation is
    reached -- the test would then pass on the redactor's behaviour rather than
    on the truncation it names. (Under ``path`` the per-segment sweep of #384
    applies instead and the value survives to be truncated by ``MAX_PATH``,
    which is pinned by ``test_a_pathological_path_is_capped_by_max_path``
    below.)
    """
    long_value = "seg." * 400
    assert len(long_value) > MAX_EMITTED_TEXT * 5
    line = render("req", model=long_value)
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
    assert redact_value("reason", session_path) == "/[REDACTED]"
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
# The bound is far above any value the constant could sanely take -- raising the
# threshold to 256 or 1024 is a legitimate product decision and must not fail
# here -- and exists only so an absurd value fails FAST and LOUDLY.
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
    characters, 1,101 split elements -- which now exceeds ``MAX_PATH_SEGMENTS``
    and is answered by the marker arm, never reaching ``MAX_PATH`` at all. It
    would have passed for the wrong reason under a mutant that deleted the cap,
    because the marker bounds that input too. The fixture is now built AT the
    largest element count the sweep still handles, so it exercises the cap on
    the only branch that has one.

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
    pathological = ("/" + segment) * (MAX_PATH_SEGMENTS - 1)
    assert len(pathological.split("/")) == MAX_PATH_SEGMENTS, (
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

    marker = "…<truncated>"

    at_limit = ("/seg" * MAX_PATH)[:MAX_PATH]
    over_limit = ("/seg" * MAX_PATH)[: MAX_PATH + 1]
    just_under = ("/seg" * MAX_PATH)[: MAX_PATH - 1]
    assert len(at_limit) == MAX_PATH
    assert len(over_limit) == MAX_PATH + 1

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
    assert "…<truncated>" not in str(redact_value("path", session_path))


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
_SEGMENT_MARKER_RE = re.compile(r"/\[REDACTED\]…<\d+ segments>")

# The largest request line uvicorn's httptools parser accepts, per #390.
_LARGEST_ACCEPTED_REQUEST_LINE = 16385


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

    crafted = "/" * _LARGEST_ACCEPTED_REQUEST_LINE
    out = redact_value("path", crafted)
    assert isinstance(out, str)
    assert _SEGMENT_MARKER_RE.fullmatch(out), out
    # Bounded by construction, not by ``MAX_PATH``: the marker never reaches
    # the cap, so the cap is not what is holding this line down.
    assert len(out) <= 28, (len(out), out)
    assert len(out) < MAX_PATH
    # Positive partner: the count is REAL, so this is not a fixed string that
    # would look identical for any input.
    assert f"<{_LARGEST_ACCEPTED_REQUEST_LINE + 1} segments>" in out, out


def test_the_segment_marker_fires_only_ABOVE_the_limit() -> None:
    """The threshold is a security boundary, so both sides are pinned and so is
    the exact element count at which behaviour flips. Off-by-one here is not a
    cosmetic bug: it decides which of two different redaction rules runs.

    ``MAX_PATH_SEGMENTS`` counts ``split("/")`` ELEMENTS, not separators, so a
    leading ``/`` means the limit is one fewer real segment than the number
    reads. That is asserted, not assumed.

    RED if the comparison is loosened to ``>=`` (the at-limit half fails, the
    sweep's output replaced by a marker) or tightened to ``+ 1`` (the
    one-over half fails). Also RED if the count is taken as ``count("/")``
    without the ``+ 1`` -- the flip point moves by one element.
    """
    from app.core.logging import MAX_PATH, MAX_PATH_SEGMENTS, redact_value

    _refuse_to_materialise(MAX_PATH_SEGMENTS)
    # One-character segments, for two independent reasons: no segment can match
    # ``HIGH_ENTROPY_RE``'s 32-character floor, AND the whole value stays under
    # ``MAX_PATH``. Both matter -- on the sweep side the output is then exactly
    # the input, so any difference is attributable to the BRANCH and not to
    # redaction or to truncation. (A 3-character segment overflows the cap at
    # 63 segments and this test measured the cap instead. Found by running it.)
    at_limit = "/a" * (MAX_PATH_SEGMENTS - 1)
    one_over = "/a" * MAX_PATH_SEGMENTS
    assert len(one_over) <= MAX_PATH, (
        f"MAX_PATH={MAX_PATH} can no longer hold {MAX_PATH_SEGMENTS} one-character segments "
        f"({len(one_over)} characters), so this test would measure the cap, not the branch."
    )
    assert len(at_limit.split("/")) == MAX_PATH_SEGMENTS
    assert len(one_over.split("/")) == MAX_PATH_SEGMENTS + 1
    # The element count is one MORE than the number of real segments, because
    # a leading "/" produces an empty leading element.
    assert at_limit.split("/")[0] == ""
    assert at_limit.count("/") == MAX_PATH_SEGMENTS - 1

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
    """THE "NEVER WEAKER" PROOF, per secret shape, and it is a proof by shape
    of the OUTPUT rather than a sampling argument: the marker arm's result is a
    fixed string plus a decimal count, so no input character can reach the log
    line whatever the input contains.

    Asserted twice on purpose -- the full match is the general statement, the
    per-shape absence is the concrete one an operator would recognise. The
    first shape in ``FAKE_SECRET_SHAPES`` is one the sweep itself redacts, so a
    rewrite of that list cannot make this pass vacuously.

    RED if the marker arm is replaced by anything that echoes input, including
    #390's whole-string fallback.
    """
    from app.core.logging import MAX_PATH_SEGMENTS, redact_value

    _refuse_to_materialise(MAX_PATH_SEGMENTS)
    crafted = "/" + "/".join(["z"] * MAX_PATH_SEGMENTS) + "/" + value
    assert len(crafted.split("/")) > MAX_PATH_SEGMENTS
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
    fake_key = "re_" + "M5jEIbOReG1FaJDGhnseqgg4"
    assert len(fake_key) == 27

    crafted = "/" + "/".join(["A" * 32] * (MAX_PATH_SEGMENTS - 1)) + "/." + fake_key
    assert len(crafted.split("/")) == MAX_PATH_SEGMENTS + 1

    out = str(redact_value("path", crafted))
    assert _SEGMENT_MARKER_RE.fullmatch(out), out
    assert fake_key not in out, out


class _CountingPattern:
    """A ``re.Pattern`` stand-in that records how many times ``sub`` ran.

    Only the two methods ``redact_value`` actually calls are implemented, so a
    future call to a third method fails loudly here rather than silently
    reporting a count that means something else.
    """

    def __init__(self, inner: re.Pattern[str]) -> None:
        self._inner = inner
        self.sub_calls = 0

    def sub(self, repl: str, string: str) -> str:
        self.sub_calls += 1
        return self._inner.sub(repl, string)

    def search(self, string: str) -> re.Match[str] | None:
        return self._inner.search(string)


def test_the_marker_arm_runs_INSTEAD_OF_the_sweep_not_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#390 IS A COST DEFECT, so the only assertion that actually measures it is
    one about WORK, not about output.

    Moving the threshold check to AFTER the sweep produces a byte-identical log
    line for every input, and every other test in this file passes -- measured:
    that mutant survived the whole affected suite (96 passed) while doing the
    full 1,702 microseconds of per-segment work the issue exists to remove. It
    would have bounded the output, which was never the problem, and shipped the
    regression under a green suite.

    The sub COUNT is asserted rather than a wall-clock time, so this is
    deterministic and cannot flake under load.

    RED if the ``MAX_PATH_SEGMENTS`` check is moved below the sweep, or if the
    arm stops returning early.
    """
    from app.core import logging as logging_module

    _refuse_to_materialise(logging_module.MAX_PATH_SEGMENTS)
    counting = _CountingPattern(logging_module.HIGH_ENTROPY_RE)
    monkeypatch.setattr(logging_module, "HIGH_ENTROPY_RE", counting)

    crafted = "/a" * (logging_module.MAX_PATH_SEGMENTS + 5)
    assert len(crafted.split("/")) > logging_module.MAX_PATH_SEGMENTS
    out = logging_module.redact_value("path", crafted)
    assert _SEGMENT_MARKER_RE.fullmatch(str(out)), out
    assert counting.sub_calls == 0, (
        f"the entropy sweep ran {counting.sub_calls} times on a path the marker arm "
        f"answers -- the work is not bounded, only the output is"
    )

    # PARTNER, because a count of zero on its own would also be satisfied by a
    # `redact_value` that never sweeps anything: a real route DOES sweep, once
    # per segment.
    counting.sub_calls = 0
    session_path = "/v1/sessions/0f0d1a2b-3c4d-5e6f-7a8b-9c0d1e2f3a4b/messages"
    swept = logging_module.redact_value("path", session_path)
    assert swept == "/v1/sessions/[REDACTED]/messages", swept
    assert counting.sub_calls == len(session_path.split("/")), counting.sub_calls


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

    fake_key = "re_" + "M5jEIbOReG1FaJDGhnseqgg4"
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
