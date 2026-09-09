import logging
import re
from collections.abc import Mapping, Sized
from typing import Any, cast

SECRET_VALUE = "[REDACTED]"
TEXT_VALUE = "[REDACTED_TEXT]"


def _length_marker(length: int) -> str:
    """The ``[REDACTED]…<N chars>`` marker, built from a COUNT and nothing else.

    Three sites emit it -- the ``MAX_SCANNED_CHARS`` guard in ``redact_value``,
    the ``MAX_MODEL_ID`` arm below it, and ``RedactQueryCredentialsFilter``'s
    per-arg bound. Each of them is claiming "this line cannot emit any character
    of its value", and taking an ``int`` makes that claim STRUCTURAL: the value
    is not in scope here, so no edit to this function can leak one. Previously it
    held only because three separate f-strings each happened to interpolate a
    ``len(...)`` rather than the string itself.

    Each CALL SITE keeps its own length expression. The distinction between the
    RAW length and the SWEPT length is load-bearing at the ``MAX_MODEL_ID`` arm
    (it counts the value the comparison actually measured, after ``BEARER_RE``),
    and hiding it inside here would erase it.
    """
    return f"{SECRET_VALUE}…<{length} chars>"


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

# THE DEFECT (#400): the boundary was defined over a DIFFERENT alphabet than the
# run it was bounding. The shipped rule was ``\b[A-Za-z0-9+/=_-]{32,}\b``, and
# ``\b`` is a transition in ``\w`` -- Unicode letters, digits and ``_``. The
# character class is neither a subset nor a superset of ``\w``, and the two
# disagree in BOTH directions, so BOTH ends of every run were bypassable.
#
# THE BYPASS IS SYMMETRIC, and #400 files only half of it. The issue reports a
# non-ASCII letter BEFORE the run and a ``+ / = -`` at the START. Measured here,
# the trailing mirror of each leaks identically. Every row below is a 64-char
# sha256 through ``HIGH_ENTROPY_RE.sub``, shipped versus this rule; ``prints``
# means the sha reached the output verbatim:
#
#                              LEADING              TRAILING
#                          shipped  fixed       shipped  fixed
#     ``é``   U+00E9        prints  redacts      prints  redacts
#     ``а``   U+0430        prints  redacts      prints  redacts
#     ``中``  U+4E2D        prints  redacts      prints  redacts
#     ``Ａ``  U+FF21        prints  redacts      prints  redacts
#     ``٠``   U+0660        prints  redacts      prints  redacts
#
# and a 32-character value (one character ON the floor) prints today whether it
# STARTS or ENDS with any of ``- + / =``; all eight redact under this rule.
#
# NOT EVERY NON-ASCII CHARACTER WAS A HOLE, and saying which is the difference
# between a fix and a superstition. Zero-width joiner U+200D, right-to-left
# override U+202E and combining acute U+0301 are NOT ``\w``, so the shipped
# ``\b`` already fired next to them and the sha already redacted -- measured,
# leading and trailing, before and after. This change does not touch them. A
# reader who assumes "unicode next to a secret leaks" would go looking for
# three bugs that never existed.
#
# WHAT ACTUALLY FIXES IT IS DROPPING ``\b``, and the lookarounds are NOT the
# fix -- which matters, because reading them as the fix sends the next reader
# after the wrong mechanism. ``\b`` was the bug; a rule with no boundary at all,
# ``[A-Za-z0-9+/=_-]{32,}``, closes every case in the table above. Measured:
# over 300,000 random strings drawn from an alphabet mixing the class, ASCII
# punctuation and the five unicode shapes above, ``.sub`` output under the
# shipped lookaround rule and under the bare-class rule differed on 0 inputs.
# That is not a coincidence and not just a sample: ``{32,}`` is greedy and
# matching is leftmost, so the leftmost position at which 32 class characters
# follow IS the start of a maximal run, and the greedy match already consumes
# it to the end. The lookarounds cannot change where a match starts or stops.
#
# THEY EARN THEIR PLACE AS A STATEMENT OF INTENT, not as behaviour: they say in
# the pattern that a match is exactly a MAXIMAL run of class characters of
# length >= 32, so a later ``.match(s, pos)`` or an anchored reuse -- the two
# calls where a mid-run start IS possible -- cannot quietly mean something else.
#
# CONSEQUENCE FOR A MUTATION ROUND, recorded so it is not read as a coverage
# hole: a mutant that DELETES BOTH LOOKAROUNDS SURVIVES BY DESIGN. No test can
# kill it while this module uses the pattern only through ``.sub``, because the
# two patterns are the same function. Do not write one; if the module ever
# calls ``.match``/``.search`` on it, that is when the mutant becomes killable.
#
# IT REDACTS A SUPERSET ON EVERY INPUT, and that is a theorem rather than a
# sample. Every ``\b``-anchored match is itself a run of >= 32 class characters,
# so it lies inside some maximal class run, and that run is therefore >= 32 and
# is matched here. (Sampled anyway, as a check on the reasoning rather than as
# the argument -- a SECOND experiment, not the one above, which compared this
# rule against the BARE-CLASS rule: 0 of 300,000 random strings over an
# alphabet mixing the class, ASCII punctuation and the five unicode shapes
# above had any character the SHIPPED rule redacted and this one did not.)
#
# IT IS ALSO FASTER ON THE ADVERSARIAL SHAPE, which is a happy accident and not
# the reason -- and, consistently with the paragraph above, the speed comes
# from dropping ``\b`` rather than from the lookarounds. ``\b`` at the end of a
# GREEDY match FAILS at the last character when the next character is a word
# character, and the engine then backtracks one character at a time down the
# whole run. Nothing here fails at that position: the lookahead SUCCEEDS (an
# ``é`` is outside the class), and the bare-class rule has no check to make at
# all. Measured on the 16,000-``A``-plus-``é`` case, across two independent
# sessions on differently-loaded machines: lookaround and bare-class came out
# equal to within the noise every time (12.13 vs 12.06 us on a quiet machine;
# 16.56 vs 16.45 and 15.06 vs 15.16 on a busier one). The win is entirely
# ``\b``'s removal. The 1.0x row in the table below is the other half of that
# argument: with no trailing character there is no boundary to fail on, and even
# the shipped rule is fast.
#
# MEASURED ON THE PATTERN, not through ``redact_value``, and the frame matters
# because a first draft of this paragraph got it wrong. Through
# ``redact_value`` a 16,001-character value is now answered by
# ``MAX_SCANNED_CHARS`` before either pattern runs, so a whole-function figure
# would be crediting this change with #401's win.
#
# METHOD, stated so the figures are reproducible rather than asserted: both
# columns in ONE process, the shipped pattern and this one compiled side by
# side, median of 7 ``timeit`` runs, Apple M4, CPython 3.12.13. ``.sub`` alone:
#
#     16,000 ``A``s + trailing ``é``    158.0 us -> 12.3 us   12.8x
#      8,000 ``A``s + trailing ``é``     79.7 us ->  6.2 us   12.8x
#      8,000 ``A``s, nothing after        6.2 us ->  6.2 us    1.0x
#     real 58-char session route          0.16 us -> 0.14 us
#     ``/health``                         0.06 us -> 0.06 us
#
# READ THE RATIO, NOT THE ABSOLUTE. Re-measured later on a busier machine the
# first row read 199.3 us -> 16.6 us and 198.2 us -> 15.1 us: every absolute
# moved by about a quarter while the ratio held at 12.0-13.2x across all three
# sessions. Only the ratio and the 1.0x row are load-bearing here, and only
# those should be treated as reproducible; an absolute that disagrees with the
# table by a quarter is measuring the machine, not this change.
#
# The third row is the one that explains the first two: with no trailing
# character the greedy match succeeds at end-of-string and ``\b`` never
# backtracks. The cost was always in the FAILING boundary, and ordinary values
# are a wash.
#
# WHAT IS NOT FIXED, stated because the fix is easy to over-read. An INTERIOR
# split still defeats the floor: any character outside the class inside a
# secret -- an ordinary ASCII ``.`` or a space, not just something exotic --
# leaves two sub-32-character halves, and both print, before this change and
# after. ``"A" * 20 + "." + "B" * 20`` is untouched by either rule. That is the
# structural limit of a length floor, and it is the same shape this file
# already records twice: the Argon2 PHC string that splits on ``$`` (see
# ``SECRET_KEY_PARTS``) and a JWT that splits on ``.``. Key-name matching, not
# entropy, is what covers those.
#
# ``re.ASCII`` IS THE TEMPTING ONE-TOKEN "SIMPLIFICATION" AND IT LEAKS. Adding
# that flag makes ``\w`` ASCII-only, which looks like it closes 8a and does the
# opposite: it makes MORE characters non-word, so more of them create the
# boundary a secret needs. ``'DhR86ª=QY0GJHlwmF0z9vfF9aipERC7sqdPW2g;'``
# redacts under the shipped rule and under this one, and prints IN FULL under
# ``\b...\b`` with ``re.ASCII`` -- the whole suite passed under that variant
# before a test was written for it, so it is pinned now.
HIGH_ENTROPY_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])")

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
# the longest is a 71-character ``source_version_hash``, ``sha256:`` plus a
# sha256 hex digest (``app/worker/cli.py:374``). (An earlier draft said 128,
# which is the ``String(128)`` COLUMN WIDTH at
# ``app/models/index_versions.py:30``, not a value this app ever produces.)
# (A JOINED value can:
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
# PRECEDENCE, since THREE key-name sets now guard the same branch
# (``MODEL_ID_KEYS`` joined for #387): ``redact_value`` tests them in the order
# ``OPAQUE_ID_KEYS``, ``MODEL_ID_KEYS``, ``PATH_KEYS``, and each arm RETURNS, so
# a key listed in two takes whichever is tested first and skips the other
# entirely -- on a collision the WEAKER rule wins, silently. All three are
# pairwise disjoint today, asserted by
# ``test_the_three_key_name_sets_are_pairwise_disjoint`` rather than only
# claimed here; keep them disjoint, or reorder the branches deliberately and
# say why.
#
# THE ``PATH_KEYS`` NAME IS NOW TESTED TWICE, in two places, and that is
# deliberate. ``MAX_PATH_SEGMENTS`` is checked ABOVE ``BEARER_RE.sub`` -- ahead
# of every other string branch -- because its arm emits no character of its
# input and everything below it is therefore dead work; the sweep is checked in
# its usual place. Both are guarded on the same key name, so the value's shape
# never selects a rule. See ``MAX_PATH_SEGMENTS`` for the measurement that
# forced the ordering.
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
# (see the SCOPE paragraph on ``EMITTED_TEXT_KEYS``). The cap restores that
# bound explicitly rather than as a side effect, using the same truncation
# shape as ``MAX_OPAQUE_ID``.
#
# The example this paragraph used to give -- ``"/seg" * 1100``, 4,400
# characters -- no longer reaches ``MAX_PATH`` at all: at 1,101 elements it is
# now answered by ``MAX_PATH_SEGMENTS`` below. ``MAX_PATH`` remains
# load-bearing for the shape it always governed and still does: a path with
# FEW segments whose swept form is long, e.g. 20 segments of 60 characters
# each, none of which matches ``HIGH_ENTROPY_RE``. That is the fixture its
# tests use.
#
# NARROWED to "the path field" on review: an earlier draft claimed the collapse
# was the only thing bounding ANY client-controlled value here, and that is
# false. ``app/core/middleware.py`` passes ``method=request.method`` on the SAME
# ``build_log_event`` call. It is equally client-controlled, and at the two
# revisions #384 spanned it was uncapped at both -- unchanged by that fix and
# not fixed by it. ``redact_value("method", "!" * 14700)`` returned all 14,700
# characters, because ``!`` is outside ``HIGH_ENTROPY_RE``'s class while still
# being a legal HTTP token character. (``"A" * 14700`` collapses to 10, which
# is presumably why nobody noticed: the obvious probe is the one shape that is
# safe.) It was LATENT, not live: ``infra/docker/Dockerfile.api``'s CMD passes
# no ``--http`` flag, so uvicorn selects httptools, and httptools 0.8.0 refuses
# an unrecognised method outright -- ``HttpParserInvalidMethodError`` for
# ``"!" * 14700`` and for ``"A" * 14700`` alike, measured. h11 accepts both and
# hands the full 14,700 characters to the app, so it would have become
# reachable the moment anything selected h11.
#
# THAT PARAGRAPH IS NOW HISTORY, and it is corrected here rather than left to
# rot: ``MAX_SCANNED_CHARS`` (#401) bounds EVERY string key, so the same probe
# now returns ``'[REDACTED]…<14700 chars>'`` -- 24 characters, measured. The
# latent hole #384 recorded and declined is closed as a side effect of bounding
# the work, not by the blanket loosening the paragraph above still rejects.
# ``method`` is bounded now; it is still not swept per segment, and it still
# should not be.
#
# THREE ``path=`` CALL SITES CARRY A FILESYSTEM PATH, NOT A URL PATH, and their
# output really did change. Two distinct VALUES, across three sites, each
# re-verified rather than inherited (an earlier version of this heading said
# TWO, counting values and not sites):
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


# The number of ``/`` SEPARATORS above which a path stops being swept and is
# replaced by a fixed marker. Separators, not ``split("/")`` elements: for any
# value beginning with ``/`` -- every value this branch sees, URL and
# filesystem alike -- the separator count IS the segment count, so the number
# the operator reads off the marker is the number they get by counting slashes.
# ``"/a/b"`` is 2, ``"/"`` is 1, ``""`` is 0.
#
# THE DEFECT (#390), found by the adversarial review of the #384 fix. The
# per-segment sweep runs one ``re.sub`` per segment, inside the asyncio event
# loop, on EVERY request, and ``MAX_PATH`` bounds the OUTPUT but not the WORK.
# A 404 matches no route, so no ``Depends(rate_limited_*)`` throttles it, and
# the redaction alone then costs many times the request it is logging. The
# probe that settled reachability is recorded in #390: a 16 kB crafted path
# returns OUR error envelope, not Fly's, so it does reach ``build_log_event``.
#
# MEASURED END TO END -- ``redact_value("path", value)``, which is what the
# process actually pays -- not one branch in isolation. Apple M4 / CPython
# 3.14.5, median of 7 independent timeit runs, three runs reproduced within 1%.
# An earlier draft of this block quoted BRANCH-ONLY figures against
# whole-function ones and overstated the win by more than tenfold; three
# reviewers caught it independently, which is why the frame is stated here.
#
#                              #384 sweep    this file
#     16,385 B all-slash          966 us       2.70 us    ~360x
#     16,384 B, 4,096 segments    379 us       2.70 us    ~140x
#     16,368 B, 496 segments      124 us       2.70 us     ~46x
#     1 MB all-slash           59,197 us        122 us    ~487x
#     real session route        1.556 us      1.615 us    +59 ns
#     ``/health``               1.040 us      1.089 us    +49 ns
#
# THE ORDERING IS THE FIX, not a layout choice. With this check placed BELOW
# ``BEARER_RE.sub`` -- where it first landed -- the same 16 kB path cost 45.6
# us rather than 2.70, because that sub is an unbounded O(len) scan the marker
# arm has no use for. See the comment at the check itself for why it uses the
# unbound ``str.count``.
#
# STILL NOT BOUNDED, and worth saying because the word is easy to overclaim:
# ``BEARER_RE.sub`` remains an unbounded scan for every OTHER key, and
# uvicorn's access-log filter (``QUERY_CREDENTIAL_RE``) scans the full request
# line for the same request at a measured cost larger than anything here. Those
# are pre-existing, in different mechanisms, and are tracked separately. What
# this constant bounds is the ``path`` field of the app's own request log.
#
# WHY THE ARM RETURNS A MARKER INSTEAD OF FALLING BACK TO THE WHOLE-STRING
# SWEEP, which is what #390 proposed. The whole-string sweep redacts a SUPERSET
# of the characters the per-segment sweep redacts -- ``/`` is non-word, so a
# ``\b`` inside a segment is a ``\b`` in the whole string, and one class run
# yields at most one greedy match containing every per-segment match inside it.
# That makes it stronger AT THE SWEEP, and #390 recorded it as "0 cases weaker
# over 300,000 samples" on exactly that basis.
#
# The rule is not the sweep, though: it is the sweep AND the ``MAX_PATH`` cut.
# Redacting a superset makes the swept string SHORTER, and a shorter string
# pulls tail material INSIDE the 160-character window that the per-segment
# order truncates away. Reproduced here, deterministically, at 65 separators --
# 64 blobs of 32 ``A``s, then a 27-character Resend-shaped literal. 65 and not
# 64: at exactly 64 the arm does not fire and the literal stays hidden, which
# an earlier draft of this paragraph got wrong while renumbering it into the
# separator unit -- the fixture in the test is right, the prose was not:
#
#     per-segment  '/[REDACTED]/[REDACTED]/...'   (cut at 160; literal absent)
#     #390's arm   '/[REDACTED]/.re_...'          (literal PRINTS IN FULL)
#
# The same flip prints the salt out of an Argon2 PHC string, which the
# ``SECRET_KEY_PARTS`` comment at the top of this file records as a shape
# ``HIGH_ENTROPY_RE`` cannot catch. Neither rule REDACTS those; the per-segment
# order hid them by volume. The marker cannot regress that way because it emits
# no input character at all: its output is a constant plus a decimal count, so
# it is bounded at ``23 + the count's digits`` -- 25 characters at the
# threshold, 28 for a 16 kB path, 31 for a 10 MB one -- and there is nothing
# for a cut to expose. Verified across 60..69 separators: the literal appears
# under #390's arm from 65 onward and never under this one.
#
# THE SAME MECHANISM FIRED AGAIN WHEN #400 STRENGTHENED ``HIGH_ENTROPY_RE``,
# and it is recorded here because this block is where the argument lives. The
# class-boundary rule redacts a SUPERSET, so the swept string is SHORTER, so
# tail material the per-segment order used to truncate away now lands inside
# the 160-character window. Reproduced deterministically -- five segments of
# ``"-" + "X" * 31`` (32 characters each, exactly ON the floor, and the leading
# ``-`` is exactly the 8b bypass, so the shipped rule matched none of them)
# followed by a 27-character Resend-shaped literal:
#
#     before #400   '/-XXX.../-XXX.../-XXX…<truncated>'   literal ABSENT
#     after  #400   '/[REDACTED]/.../[REDACTED]/re_...'   literal PRINTS
#
# This is NOT a regression to be undone by weakening the boundary. What changed
# is which under-floor bytes the cap happens to hide, and hiding by volume was
# never a redaction.
#
# THE EXPOSED MATERIAL IS NOT "SUB-32-CHARACTER", and an earlier draft of this
# paragraph said it was. That is false and measurably so: the guarantee is
# about the RUN LENGTH, not the value length, so a long value made of short
# class runs prints in full. Measured -- a 65-character cookie-shaped value
# whose longest class run is 30 (``sessionid=`` + three ``.``-separated
# stretches) reaches the line intact under exactly this fixture. The TRUE
# claim, and the one the test actually asserts, is the class-run claim: no
# ``/``-delimited segment of the swept value carries a class run of 32 or more
# characters. The BEFORE column violates that five times over and the AFTER
# column does not, so the output IS strictly more redacted by the measure this
# rule makes. It is computed by an independent maximal-run scan rather than by
# re-running the regex under test. A test asserting the literal is VISIBLE
# would lock in a defect and go red the day someone improves the cap; there is
# deliberately no such test.
#
# NOT "the largest request line uvicorn accepts". An earlier draft said that of
# 16,385 bytes. It is false: httptools 0.8.0 parses a 1,000,000-byte request
# line and uvicorn imposes no cap on that path (16,384 is h11's
# ``DEFAULT_MAX_INCOMPLETE_EVENT_SIZE``, a different implementation, and
# ``infra/docker/Dockerfile.api`` selects httptools). 16 kB is simply the size
# #390 probed. The marker stays bounded regardless, which is the point of
# stating the bound logarithmically rather than as a flat number.
#
# WHAT THE MARKER COSTS, stated. Above the threshold the route shape is gone --
# the #384 symptom, deliberately, for values that are not routes. No path this
# app serves comes close. ALL FIGURES BELOW ARE SEPARATORS, the unit this
# constant counts; an earlier draft quoted split-element counts here while the
# rest of the block used separators, so every number read one too high.
#
#     deepest declared route        6   /v1/auth/oauth/{provider}/connect/start
#     (its concrete form is 6 too -- Starlette's default converter never
#      matches ``/``, so substituting a provider slug cannot change the count)
#     container bundle path         2   /app/frontend_dist   (main.py, twice)
#     plain developer checkout      6
#     linked checkout               9
#     dev email outbox              7   (email_client.py)
#
# Three filesystem ``path=`` sites, not two -- the paragraph further up that
# says TWO predates ``email_client.py``'s and is corrected there too. Against
# the deepest value observed anywhere, 9, the threshold is a little over 7x;
# against the deepest ROUTE it is more than 10x. Nothing production emits
# changes. The outbox directory is settings-configurable
# (``settings.email_outbox_dir``) and so has no fixed bound, which is also the
# one way a ``path=`` value could arrive WITHOUT a leading ``/``: a relative
# outbox directory. There the separator count is one less than the segment
# count. It would take 65 separators to matter and a dev outbox path has 7, so
# this is stated for accuracy rather than defended against.
#
# AND THE COUNT IS CLIENT-INFLUENCED. The marker names the separator count,
# which ``'/[REDACTED]'`` did not, so an operator seeing a 404 flood learns
# more than before -- but not something they may trust as measured. A short
# crafted path forges the identical line: ``/`` + 32 ``A``s + the literal
# ``…<999999 segments>`` has one separator, takes the SWEEP, and renders as
# ``'/[REDACTED]…<999999 segments>'`` because ``…`` is outside
# ``HIGH_ENTROPY_RE``'s class and closes the match. That is the client's own
# value echoed back, disclosing none of our secrets -- the identical,
# already-accepted tradeoff documented for ``request_id`` above, and
# ``'…<truncated>'`` was forgeable the same way before this change.
#
# uvicorn's own access line still carries the full request line
# (``--access-log`` in ``infra/docker/Dockerfile.api``, query credentials
# stripped by ``QUERY_CREDENTIAL_RE``). It carries NO ``request_id``, though,
# and this module's format prints no timestamp, so recovering the path means
# correlating two lines by time -- which is precisely the burden #384 filed as
# the defect. For crafted non-routes that is an acceptable trade; "nothing is
# lost" would not be true and is not claimed.
#
# TWO ALTERNATIVES MEASURED AND REJECTED:
#
#   * Cap BEFORE sweeping: strictly WEAKER, and not retried here. The cut lands
#     mid-token and the surviving sub-32-character prefix falls under
#     ``HIGH_ENTROPY_RE``'s floor and prints. Pinned by
#     ``test_the_path_sweep_runs_BEFORE_the_truncation_so_a_cut_cannot_expose_a_secret``.
#   * Skip the ``re.sub`` for segments under 32 characters, which cannot match
#     and so is a provable no-op. On 496 segments of 32 characters it is no
#     help at all -- the segments are over the floor, so nothing is skipped.
#     It reduces the cost on one shape; it does not BOUND it, which is what
#     #390 asks for. Note it is also NOT free to adopt later: it is killed by
#     the sweep-call-count partner in ``test_log_extra_fields.py``, which pins
#     an exact call count deliberately.
#
# COUPLED TO ``MAX_PATH``, and the coupling is invisible from either constant
# alone: ``MAX_PATH``'s own boundary tests build ``"/seg" * MAX_PATH`` sliced to
# ``MAX_PATH``, which is 40 separators at 160 and crosses this threshold when
# ``MAX_PATH`` reaches 252. ``test_the_max_path_cap_fires_only_ABOVE_the_limit``
# asserts that relationship explicitly, naming BOTH constants in the failure
# message -- it did not before, and a reviewer measured the resulting failure
# at ``MAX_PATH = 300`` as an opaque string diff that never mentioned this
# constant at all.
MAX_PATH_SEGMENTS = 64


# Keys whose value is a PROVIDER MODEL SLUG -- configuration identity, not a
# credential -- which the entropy sweep would otherwise blank. Matched on the
# WHOLE lowered key, the same discipline as ``OPAQUE_ID_KEYS`` and
# ``PATH_KEYS``, and DISJOINT from both (asserted by a test, because the
# precedence note at ``PATH_KEYS`` says a key in two sets silently takes
# whichever branch is tested first).
#
# THE DEFECT (#387). A slug is one contiguous run of ``HIGH_ENTROPY_RE``'s class
# -- ``/`` and ``-`` are both in it -- so any slug of 32 characters or more
# collapses to ``[REDACTED]`` on every spend and provider line. That is the #384
# symptom on the cost path. Nothing in ``app/cost/pricing.py`` reaches 32 today
# (longest: ``openai/text-embedding-3-small``, 29), which is why this is latent
# rather than live, but ``pricing.py:177`` already NAMES
# ``openai/gpt-4o-mini-realtime-preview`` at 35 and #387 measures
# ``anthropic/claude-3-5-sonnet-20240620`` at 36 collapsing.
#
# MEASURED, AND THE MEASURE IS THE CLASS RUN, NOT THE VALUE LENGTH. Two earlier
# drafts of this paragraph got its evidence wrong in opposite directions, so the
# recipe is written out below rather than asserted. The first claimed every other
# allowlisted key "has a maximum class run of 0 characters in its real values" --
# false, and self-contradicted three paragraphs down. The second replaced that
# with four example values, and THREE OF THE FOUR were strings this repo cannot
# produce under those keys: ``call_site`` is a six-member ``StrEnum``
# (``app/cost/call_site.py:35``) whose longest value is ``alias_intent`` at 12,
# not the 22-character log EVENT name it was confused with;
# ``ServerDisconnectedError`` appears nowhere in this tree (there is no aiohttp
# dependency in ``pyproject.toml`` or ``uv.lock``); and the GitHub token URL is a
# request TARGET at ``app/api/routes/oauth.py:233``, never a logged ``url=``.
#
# THE RECIPE, which does reproduce: walk every ``extra=`` dict literal and every
# ``build_log_event`` string keyword under ``backend/app``, and scan each VALUE
# for its longest run of ``[A-Za-z0-9+/=_-]``. The largest non-exempt result is
# 10 -- ``endpoint='embeddings'`` (``app/embeddings/openrouter.py:308``).
#
# The TRUE claim, and the one that makes ``model`` the right and only place to
# spend an exemption: NO KEY IN ``EMITTED_TEXT_KEYS`` OTHER THAN THE THREE
# BELOW REACHES THE 32-CHARACTER FLOOR in its real values, except the ones
# ``OPAQUE_ID_KEYS`` / ``PATH_KEYS`` already exempt (``request_id`` at 36 --
# ``"req_"`` plus a 32-character uuid4 hex -- is the one that would).
#
# AND THE MARGIN IS THINNER THAN A LITERAL SCAN SHOWS, which is what both
# earlier drafts missed by scanning only literals. ``cause_type`` and
# ``error_type`` carry ``exc.__class__.__name__`` (``app/main.py:268``,
# ``app/llm/fallback.py:55``), which is not a literal and is bounded by nothing
# here. The longest exception class name defined in ``backend/app`` today is
# ``EmbeddingProviderNotConfigured`` (``app/embeddings/factory.py:169``) -- 30
# characters, one unbroken class run, TWO characters clear of the floor. An
# exception named two characters longer would blank its own ``cause_type`` and
# land this file back in #387 under a key that carries no slug at all. Stated
# because it is a real edge, not a hypothetical one.
#
# TWO LATENT SIBLINGS SHARE THE SLUG SPACE and are covered here rather than
# later: ``index_embedding_model`` and ``configured_embedding_model``
# (``app/retrieval/hybrid.py:532``, ``:546``, ``:549``). Both carry an embedder
# model slug -- one read off the index stamp, one off the configured identity --
# and because this set is matched on the WHOLE key neither inherits ``model``'s
# exemption. They would have failed the same way, one config change later.
#
# THE TRADEOFF, and it is sharper than #387 states. #387 calls ``model``
# "env-configurable". On the cost-meter path it is worse than that: it is
# UPSTREAM-SUPPLIED. ``app/llm/openrouter.py:119`` and
# ``app/llm/anthropic.py:116`` both read ``data.get("model", self._model)`` --
# the string comes out of the PROVIDER's HTTP response body -- and it flows to
# ``app/cost/meter.py:61`` and ``:100`` as ``extra={"model": model}``. Worse
# still, ``provider_call_unpriced`` at ``meter.py:61`` fires exactly when
# ``price_for()`` returns None, i.e. precisely when the value is one we did not
# choose.
#
# WHY IT IS STILL THE RIGHT CALL. ``model`` is ALREADY in
# ``EMITTED_TEXT_KEYS``, so a provider-chosen slug under 32 class characters
# ALREADY prints today, unchanged by this. What this exemption does is raise the
# ceiling on a provider-chosen CONTIGUOUS CLASS RUN from 31 characters to
# ``MAX_MODEL_ID``, and above that emit a marker carrying NONE of the provider's
# characters. The "31" is the run length, not the value length -- an earlier
# draft wrote it as though it capped the value, which it never did: a
# provider string of any length made of sub-32 runs printed before this change
# and prints after, bounded only by ``MAX_EMITTED_TEXT`` on the ``extra=`` path.
# The precedent is ``request_id``, which is CLIENT-controlled and exempted a few
# lines above on exactly this reasoning; a provider-chosen string is a strictly
# weaker exposure than one already accepted. ``repr`` in
# ``render_extra_value`` still stops a value forging a second log line, and
# ``MAX_EMITTED_TEXT`` still caps the emitted field on the ``extra=`` path.
#
# AND IT CUTS BOTH WAYS -- STATED, because this exemption introduces a NEW
# over-blanking case in #387's own failure direction and leaving that unsaid
# would repeat the defect #387 reports. Under a NON-exempt allowlisted key a
# long value made of short runs printed in full up to ``MAX_EMITTED_TEXT``;
# under a model key the same value is now answered by the marker. Measured, on
# a 76-character dotted slug whose longest class run is 10:
#
#     redact_value("status", <slug>)  ->  the 76 characters, verbatim
#     redact_value("model",  <slug>)  ->  '[REDACTED]…<76 chars>'
#
# So the exemption is a TRADE, not a pure widening: runs of 32..64 characters
# gain, and values over 64 characters whose runs are all short LOSE. It is
# still right, because the second shape is not a slug any provider emits (the
# longest this codebase names is 36) while the first is, and because losing a
# 65-character slug to a marker is the failure mode an operator can SEE and
# report -- the one #387 was filed from.
#
# A MARKER, NOT A TRUNCATION. Cutting an over-cap value would emit a PREFIX of
# an untrusted string, which is the same leak shape ``MAX_PATH_SEGMENTS``
# rejects two blocks above and which the cap-before-sweeping alternative was
# rejected for. The marker's only variable part is a decimal count. That count
# is provider-influenced in exactly the way the segment marker's count is
# client-influenced; read it as a hint, not a measurement.
#
# THE ``error`` HALF OF #387 IS DECLINED, deliberately, and this is the record.
# Three reasons, each verified rather than inherited:
#
#   * ``error`` is NOT in ``EMITTED_TEXT_KEYS``, so on the ``extra=`` path it
#     already renders as ``error=<str len=N>`` and the sweep never reaches a log
#     line there at all. #387's own example is the ``build_log_event`` path.
#   * That path reaches a log line at exactly ONE site,
#     ``app/api/routes/about.py:96``.
#   * It carries arbitrary ``str(exc)`` text -- which is precisely what the
#     ``EMITTED_TEXT_KEYS`` comment below says must never be exempted: it names
#     ``cause``/``error``/``reason`` as deliberately excluded on that ground.
#     Exempting ``error`` would let a secret embedded in an exception message
#     print in full.
#
# What #387 actually loses there is a filename out of an ``OSError`` string, and
# #387 itself rates that "low ... because ``source=spec.name`` still survives".
# It survives here too. (#400 shifts that value by exactly one character:
# ``'/[REDACTED].md'`` becomes ``'[REDACTED].md'``, the leading ``/`` absorbed
# into a stronger match. Strictly more redaction, not less.)
MODEL_ID_KEYS = frozenset({"model", "index_embedding_model", "configured_embedding_model"})
MAX_MODEL_ID = 64


# The number of characters above which a string value stops being SCANNED at
# all and is replaced by a fixed marker. This bounds the WORK, which is what
# #401 asks for; every other constant in this file bounds the OUTPUT.
#
# THE DEFECT (#401). ``BEARER_RE.sub`` runs over every string value with no
# ceiling, inside the asyncio event loop, on every request. #390 hoisted the
# ``path`` marker arm above it, so ``path`` no longer pays it -- but ``method``,
# ``request_id`` and every other string field still did. ``request_id`` is the
# reachable one: ``app/core/middleware.py`` honours an inbound ``X-Request-ID``
# header, and ``MAX_OPAQUE_ID`` truncates the OUTPUT afterwards while nothing
# bounded the scan.
#
# MEASURED, and the METHOD is stated because a figure without one is an
# assertion: BOTH columns in ONE process, the pre-change module loaded from the
# git blob at ``HEAD`` through ``importlib.util.spec_from_file_location`` (this
# file has zero ``app.`` imports, so it loads standalone), median of 7
# ``timeit`` runs, Apple M4, CPython 3.12.13:
#
#     redact_value("path", "A"*16000 + "é")      224.9 us ->  5.2 us
#     redact_value("request_id", "z"*16385)       51.5 us ->  1.0 us
#     redact_value("method",     "z"*16385)       63.0 us ->  1.0 us
#     redact_value("request_id", "z"*1000000)   2900.4 us ->  0.8 us
#     redact_value("path", "/"*16385)             13.3 us -> 13.3 us
#     access filter, crafted 16 kB line          157.5 us ->  2.8 us
#
#     real 58-char session route                  1.592 us -> 1.626 us
#     ``/health``                                 1.030 us -> 1.062 us
#     model slug (36 characters)                  0.944 us -> 0.869 us
#
# The ``"/"*16385`` row is UNCHANGED on purpose: #390's segment marker answers
# that value first, above this guard, so this guard never sees it. The last
# three rows are the whole tax on ordinary traffic -- one ``str.__len__``.
#
# READ THE ORDER OF MAGNITUDE, NOT THE DIGITS. Re-measured across three sessions
# at different machine loads, every absolute in the table above moved -- the
# first row read 224.9, 231.9 and 229.8 us on the left and 5.2, 4.82 and 4.83 us
# on the right; the access-filter row read 157.5 us then 208 us. What did NOT
# move is the shape of the result: O(len) before, flat after, with the crossover
# at ``MAX_SCANNED_CHARS``. The 1 MB row is the one to trust, because only an
# O(1) arm can produce it, and it reproduced at 0.78, 0.76 and 0.79 us against
# roughly 2,900 us. Treat a disagreeing absolute as machine state.
#
# THE ORDINARY-TRAFFIC ROWS ARE AT OR BELOW MEASUREMENT NOISE, which is a weaker
# and more honest claim than the "+34 ns" an earlier draft of this block implied.
# Re-measured, the real session route read 1.605 -> 1.613 us and then
# 1.559 -> 1.583 us, and the OLD column moved more between sessions than the two
# columns differ from each other. One ``str.__len__`` is too cheap to resolve
# this way; what the rows establish is the absence of a regression, not a
# quantity.
#
# A CLAIM WITHDRAWN, recorded because it was written here and was wrong. A draft
# of this block said the access-line cost "depends on the fixture, and the worst
# case is the one with NO credential in it", at 78.6 us versus 156.4 us, on the
# theory that ``QUERY_CREDENTIAL_RE``'s trailing ``[^&\s"']*`` swallows the rest
# of the line in one step once a ``token=`` matches. That asymmetry DOES NOT
# REPRODUCE: on a 16 kB path the two shapes measured 196.3 vs 208.4 us and then
# 204.3 vs 204.2 us -- equal to within noise, twice. The regex reasoning is
# plausible and the measurement does not support it, so the claim is withdrawn
# rather than repeated. #401's own 164.7 us sits in the same band as both shapes.
#
# WHY A MARKER RATHER THAN A LENGTH PRE-CHECK THAT THEN SWEEPS. The marker emits
# no character of its input, which is the one shape immune to the cut-then-sweep
# trap ``MAX_PATH_SEGMENTS`` documents: a cut leaves a sub-floor prefix that the
# later sweep no longer matches, and it prints.
#
# #401'S OWN STATEMENT OF THAT TRAP IS REFUTED FOR ``BEARER_RE``, and the
# correction matters because it is the reason the guard could not simply be
# "cut, then sweep as usual". ``BEARER_RE`` is anchored on the literal
# ``Bearer`` and has NO length floor, so a cut INSIDE the token still matches:
# measured, ``'Bearer s'`` -> ``'Bearer [REDACTED]'`` and ``'Bearer abc'`` ->
# ``'Bearer [REDACTED]'``. The trap is real for ``HIGH_ENTROPY_RE``, which any
# pre-check would also precede -- a 40-character run cut to 31 falls under the
# floor and prints in full, measured -- so the conclusion #401 draws survives
# even though the mechanism it names for it does not.
#
# AND SKIPPING THE BEARER SWEEP IS A REAL LEAK, not a redundancy: ``BEARER_RE``'s
# class contains ``.`` and ``~`` while ``HIGH_ENTROPY_RE``'s does not, so the
# entropy sweep is not a substitute for it. Measured on
# ``"Authorization: Bearer " + "a"*20 + "~" + "b"*20 + "." + "c"*20``, the Bearer
# sweep yields ``'Authorization: Bearer [REDACTED]'`` and the entropy sweep
# leaves all 62 characters of the token untouched. That is why the guard returns
# a marker instead of jumping past the sub.
#
# WHY 8192, and the first draft of this paragraph justified it with a FALSE
# claim that a reviewer refuted, so the refutation is kept here rather than the
# sentence quietly deleted.
#
# THE FALSE VERSION: "it sits above the largest fixture in the existing suite;
# the largest is ``redact_value("request_id", "z" * 4000)``." It does not.
# ``git show HEAD:backend/tests/test_log_extra_fields.py`` line 1034 defines
# ``_A_LARGE_REQUEST_LINE = 16385`` and line 1096 feeds ``"/" * 16385`` to
# ``redact_value("path", ...)`` -- twice this threshold.
#
# THE GUARD IS STILL PURELY ADDITIVE, but for a DIFFERENT reason, and it is the
# branch ORDER rather than the number: that fixture is a ``path``, and #390's
# ``MAX_PATH_SEGMENTS`` arm sits ABOVE this one, so 16,385 separators are
# answered by ``'/[REDACTED]…<16385 segments>'`` and this guard is never
# reached. Verified by running it. Among the values that DO reach this guard,
# the largest pre-existing fixture is ``redact_value("request_id", "z" * 4000)``
# (in ``test_a_pathological_client_supplied_request_id_is_capped``) and
# ``test_a_pathological_path_is_capped_by_max_path`` builds 1,344 characters --
# both re-measured, not inherited. So no pre-existing assertion changes meaning.
# Cited by TEST NAME, not by line: an earlier draft pinned line 275, which was
# correct at HEAD and which this change's own added tests pushed to 282.
#
# ROOM ABOVE REAL VALUES. The longest single real value this app produces is a
# ``source_version_hash``, and it is 71 characters, not the 128 an earlier draft
# claimed: 128 is the ``String(128)`` COLUMN WIDTH
# (``app/models/index_versions.py:30``), while ``app/worker/cli.py:374``
# produces ``f"sha256:{digest.hexdigest()}"`` -- 7 + 64. That puts this
# threshold about 115x above it, and ``MAX_OPAQUE_ID`` caps the emitted field at
# 160 regardless.
#
# WHAT IT COSTS. Above the threshold the value's shape is gone. Nothing this app
# emits is close, and every reachable over-threshold value is crafted by
# definition. Shared with ``RedactQueryCredentialsFilter`` below, which bounds
# uvicorn's access line the same way and for the same reason.
MAX_SCANNED_CHARS = 8192


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

    BOUNDED (#401), and the bound is on the ARG rather than on the line, which
    is a correctness constraint and not a preference. Verified against the
    installed uvicorn (``uvicorn/protocols/http/httptools_impl.py:485`` and
    ``h11_impl.py:482``, which log the identical call) and against
    ``uvicorn/logging.py:97-107``, whose ``formatMessage`` destructures
    ``record.args`` into exactly five names (lines 99-105) and then calls
    ``int(status_code)`` on the fifth (line 106):

      * SHORTENING a string arg is safe, and every ``str`` element is
        shortenable here. This filter rewrites ALL of them, not only ``[2]``:
        ``[0]`` client_addr, ``[1]`` method and ``[3]`` http_version go through
        ``_redact_arg`` as well, and all four are interpolated as ``%s``.
        ``[2]`` is simply the only one a client can make long enough to reach
        the bound. ``[4]`` is an ``int``, which the ``isinstance(arg, str)``
        test below skips, so its type survives for ``int(status_code)``.
      * Changing the tuple's ARITY or an element's TYPE, or touching ``[4]``,
        raises inside ``AccessFormatter.formatMessage`` and DROPS the record.
      * Rewriting ``record.msg`` while ``record.args`` is present is the worst
        of the three. ``LogRecord.getMessage`` does ``msg % self.args``, so
        replacing a five-placeholder format string with a marker raises -- and
        ``logging.Handler.handleError`` then prints the record's UNREDACTED
        ``args`` to stderr. A "fix" that bounds the line turns one redacted log
        line into a plaintext credential dump. That is the single strongest
        argument for bounding the arg.

    So ``record.msg`` is bounded ONLY when ``record.args`` is falsy, where
    ``getMessage`` performs no interpolation and the msg is therefore not a
    format string.

    THE RESIDUALS, stated rather than overclaimed. Both are records from some
    OTHER logger this filter has been attached to; ``configure_logging``
    attaches it to ``uvicorn.access`` only, which always logs the 5-tuple above.

      * When args ARE present, the unbounded ``sub`` still runs over
        ``record.msg``. On uvicorn's access logger that msg is the 23-character
        constant format string above, so the scan is O(23); the residual is a
        long format string carried alongside args.
      * A MAPPING-STYLE ``record.args`` is skipped ENTIRELY.
        ``logger.info("%(a)s", {"a": ...})`` stores the dict itself, and
        ``logging`` unwraps a single mapping argument out of the tuple before
        the record is built, so ``isinstance(record.args, tuple)`` is False and
        that branch neither bounds nor credential-redacts it. PRE-EXISTING --
        the ``isinstance(..., tuple)`` test is unchanged by #401 (see
        ``git show HEAD:backend/app/core/logging.py``) -- and unreachable for
        the reason above, so it is recorded here rather than fixed: widening
        the branch would change behaviour no current caller can produce.
    """

    @staticmethod
    def _redact_arg(arg: str) -> str:
        # ``str.__len__`` is the UNBOUND method, for the reason recorded at the
        # ``str.count`` call in ``redact_value``: this runs before any ``sub``
        # normalises a ``str`` SUBCLASS to a plain ``str``, so a subclass could
        # otherwise supply its own ``__len__`` and choose the branch.
        if str.__len__(arg) > MAX_SCANNED_CHARS:
            return _length_marker(str.__len__(arg))
        return QUERY_CREDENTIAL_RE.sub(rf"\g<1>{SECRET_VALUE}", arg)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            if record.args:
                record.msg = QUERY_CREDENTIAL_RE.sub(rf"\g<1>{SECRET_VALUE}", record.msg)
            else:
                record.msg = self._redact_arg(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._redact_arg(arg) if isinstance(arg, str) else arg
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
        # #390. This arm runs BEFORE the Bearer sweep, not after it, and the
        # ordering is the fix rather than an accident of layout. The arm emits
        # no character of its input, so everything below is dead work for the
        # values it answers -- and `BEARER_RE.sub` is an unbounded O(len) scan
        # over the same crafted path, which measured as the entire remaining
        # cost when this check sat below it. See MAX_PATH_SEGMENTS.
        #
        # `str.count(value, ...)` is the UNBOUND method, deliberately. Below
        # this point every value has been through `BEARER_RE.sub`, which
        # returns a plain `str` even when handed a `str` SUBCLASS; here it has
        # not, so a subclass could otherwise supply its own `count` and choose
        # the branch. No call site constructs one, and `render_extra_value`
        # already refuses `int` subclasses on the same reasoning; this costs
        # one attribute lookup and removes the question.
        #
        # It counts SEPARATORS, which is the number of segments for any value
        # that begins with `/` -- every value this branch sees does, URL and
        # filesystem alike. The count is what the operator is shown, so it is
        # the number they get by counting slashes, not an off-by-one they have
        # to know a convention to undo.
        if key_lower in PATH_KEYS:
            segment_count = str.count(value, "/")
            if segment_count > MAX_PATH_SEGMENTS:
                return f"/{SECRET_VALUE}…<{segment_count} segments>"

        # #401. Placed here for the same reason the arm above is: everything
        # below is an unbounded O(len) scan, and this arm emits no character of
        # its input, so for the values it answers those scans are dead work.
        # `str.__len__(value)` is the UNBOUND method, deliberately, for exactly
        # the reason given for `str.count` in the arm just ABOVE -- this runs before
        # `BEARER_RE.sub` has normalised a `str` SUBCLASS to a plain `str`, so a
        # subclass could otherwise supply its own `__len__` and choose the
        # branch. See MAX_SCANNED_CHARS for why this returns a marker rather
        # than cutting and then sweeping.
        if str.__len__(value) > MAX_SCANNED_CHARS:
            return _length_marker(str.__len__(value))

        redacted = BEARER_RE.sub(f"Bearer {SECRET_VALUE}", value)
        # An opaque correlation id keeps the Bearer sweep and skips the entropy
        # sweep, which would otherwise blank the whole value -- see
        # OPAQUE_ID_KEYS for the measurement and the tradeoff.
        if key_lower in OPAQUE_ID_KEYS:
            if len(redacted) > MAX_OPAQUE_ID:
                return redacted[:MAX_OPAQUE_ID] + "…<truncated>"
            return redacted
        # A provider model slug keeps the Bearer sweep and skips the entropy
        # sweep, which collapses any slug of 32 class characters or more -- see
        # MODEL_ID_KEYS for the measurement, the upstream-supplied tradeoff, and
        # why the `error` half of #387 is declined. Plain `len` is correct here
        # and `str.__len__` is not needed: `BEARER_RE.sub` above returns a plain
        # `str` even when handed a `str` subclass.
        if key_lower in MODEL_ID_KEYS:
            if len(redacted) > MAX_MODEL_ID:
                # A MARKER, not a cut: truncating would emit a prefix of an
                # upstream-chosen string. The count is of the SWEPT value,
                # which is what the comparison above measured -- a value whose
                # `Bearer <token>` was collapsed reports the POST-sweep length,
                # not the raw one, and the two differ whenever the sweep fired.
                return _length_marker(len(redacted))
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
