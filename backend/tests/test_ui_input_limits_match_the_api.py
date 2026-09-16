"""The browser's input limits must equal the server's (#446, TEST_STRATEGY §8b).

Three request models declare length bounds on fields a person types into a form,
and the forms are supposed to mirror them so the browser refuses what the server
would refuse. Before this guard the mirror was held by a COMMENT --
``AuthModal.tsx`` says "Mirrors the backend's _PASSWORD_MIN_LENGTH /
_PASSWORD_MAX_LENGTH" -- and a comment cannot fail a build. Two of the four
mirrored numbers did not exist at all: ``email`` carried ``required`` and nothing
else while the server capped it at 255, and neither question composer carried any
limit while the server capped ``message`` at 4000.

What that cost the user is the reason this is a test and not a lint rule. The
composer CLEARS the box before the request goes out, so an over-length question
was typed, submitted, lost, and then reported as "TEMPORARILY UNAVAILABLE" -- a
transient-outage badge over a rejection that is permanent for that exact input.

THE BOUND IS READ FROM THE MODEL, NOT COPIED. Each test pulls the number out of
Pydantic's own field metadata and compares it to the literal in the TSX. The
literal is asserted too, deliberately: reading only the metadata would let both
sides move together silently, and these numbers are a published contract
(``docs/API_SPEC.md`` §5). Relaxing a bound is meant to be a visible edit in two
files plus this one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.api.routes.auth import LoginRequest, RegisterRequest
from app.api.routes.magic_link import MagicLinkRequest
from app.api.routes.messages import AnswerRequest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AUTH_MODAL = _REPO_ROOT / "frontend" / "src" / "components" / "AuthModal.tsx"
_COMPOSER_INPUT = _REPO_ROOT / "frontend" / "src" / "lib" / "composerInput.ts"
_HERO = _REPO_ROOT / "frontend" / "src" / "components" / "Hero.tsx"
_CHAT_VIEW = _REPO_ROOT / "frontend" / "src" / "components" / "ChatView.tsx"
_HOOK = _REPO_ROOT / "frontend" / "src" / "hooks" / "useLandingState.ts"
_API_SPEC = _REPO_ROOT / "docs" / "API_SPEC.md"


def _model_bound(model: type, field: str, kind: str) -> int:
    """Return ``min_length``/``max_length`` as the model itself declares it.

    Raises rather than returning a default: a missing bound means the server
    stopped constraining the field, and silently reporting ``None`` would turn
    this guard into one that passes when the thing it guards is gone.
    """
    info = model.model_fields[field]
    for meta in info.metadata:
        value = getattr(meta, kind, None)
        if value is not None:
            return int(value)
    raise AssertionError(f"{model.__name__}.{field} declares no {kind}")


def _read(path: Path) -> str:
    assert path.is_file(), f"{path} does not exist — this guard is pointed at nothing"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path} is empty"
    return text


def _const(text: str, name: str, path: Path) -> int:
    """Pull ``const NAME = <int>;`` out of a TS source."""
    match = re.search(rf"^const\s+{re.escape(name)}\s*=\s*(\d+);", text, re.MULTILINE)
    if match is None:
        match = re.search(rf"^export const\s+{re.escape(name)}\s*=\s*(\d+);", text, re.MULTILINE)
    assert match is not None, f"{name} is not declared in {path.name}"
    return int(match.group(1))


# ---------------------------------------------------------------------------
# The guard is pointed at real files that really contain these declarations.
# Without this, every test below could pass against a renamed or deleted file by
# failing to find anything to disagree with.
# ---------------------------------------------------------------------------


def test_the_frontend_files_this_guard_reads_all_exist_and_declare_limits() -> None:
    for path, needle in (
        (_AUTH_MODAL, "PASSWORD_MAX_LENGTH"),
        (_COMPOSER_INPUT, "MAX_QUESTION_LENGTH"),
        (_HOOK, "MAX_QUESTION_LENGTH"),
        (_HOOK, "overLengthNudge"),
    ):
        assert needle in _read(path), f"{path.name} no longer declares {needle}"


# ---------------------------------------------------------------------------
# message — both composers
# ---------------------------------------------------------------------------


def test_the_composers_carry_the_servers_message_ceiling() -> None:
    """RED if ``AnswerRequest.message``'s ``max_length`` moves without the UI."""
    server = _model_bound(AnswerRequest, "message", "max_length")
    browser = _const(_read(_COMPOSER_INPUT), "MAX_QUESTION_LENGTH", _COMPOSER_INPUT)
    # The constant has to be USED, not merely declared. Both submit paths refuse
    # on it; a constant nothing reads would satisfy the comparison below while
    # the browser enforced nothing at all.
    hook = _read(_HOOK)
    assert hook.count("> MAX_QUESTION_LENGTH") == 2, (
        "both composers must refuse over-length input on the shared constant "
        "(hero and chat); found "
        f"{hook.count('> MAX_QUESTION_LENGTH')} refusal(s)"
    )
    assert "overLengthNudge(" in hook, "the over-length refusal is not announced"
    assert browser == server, (
        f"the composers cap a question at {browser} but the server caps it at "
        f"{server} — the browser accepts what the server 422s, and the box is "
        "cleared before the request, so the user loses the text"
    )
    assert server == 4000, "the published limit in docs/API_SPEC.md §5 moved"


@pytest.mark.parametrize("path", [_HERO, _CHAT_VIEW], ids=["hero", "chat"])
def test_no_composer_reintroduces_a_maxlength_attribute(path: Path) -> None:
    """The attribute must stay OFF both boxes (#446 review).

    It was there for one round and it was a regression: ``maxLength`` makes the
    browser clamp a long paste SILENTLY, so the user sends a question cut
    mid-sentence and gets a confident answer to the fragment. The limit is
    enforced at submit instead, where it can be announced and the text kept.

    Guarded from the backend side because this is the one property whose absence
    matters: a frontend test can assert the rendered attribute is null, but only
    a source check names the mistake if someone adds it back "for safety".
    """
    text = _read(path)
    assert "maxLength" not in text, (
        f"{path.name} has a maxLength attribute again — a long paste will be "
        "truncated in silence, which is the regression #446 removed"
    )


# ---------------------------------------------------------------------------
# password and email — AuthModal
# ---------------------------------------------------------------------------


def test_the_auth_form_mirrors_the_password_bounds() -> None:
    modal = _read(_AUTH_MODAL)
    assert _const(modal, "PASSWORD_MIN_LENGTH", _AUTH_MODAL) == _model_bound(
        RegisterRequest, "password", "min_length"
    )
    assert _const(modal, "PASSWORD_MAX_LENGTH", _AUTH_MODAL) == _model_bound(
        RegisterRequest, "password", "max_length"
    )
    assert _model_bound(RegisterRequest, "password", "min_length") == 8
    assert _model_bound(RegisterRequest, "password", "max_length") == 128


def test_the_auth_form_mirrors_the_email_bounds() -> None:
    """These two numbers had no mirror at all before #446.

    ALL THREE MODELS, not just ``RegisterRequest`` (#446 review). One email
    input in ``AuthModal`` serves sign-in, registration AND the magic-link
    request, so a bound is only genuinely mirrored if every model that field can
    post to agrees with it. Pinning one of the three left the other two free to
    drift with nothing going red, which is the same single-source illusion this
    guard exists to remove.
    """
    modal = _read(_AUTH_MODAL)
    form_min = _const(modal, "EMAIL_MIN_LENGTH", _AUTH_MODAL)
    form_max = _const(modal, "EMAIL_MAX_LENGTH", _AUTH_MODAL)

    posted_to = [RegisterRequest, LoginRequest, MagicLinkRequest]
    for model in posted_to:
        assert form_min == _model_bound(model, "email", "min_length"), (
            f"the email field's minLength={form_min} disagrees with {model.__name__}"
        )
        assert form_max == _model_bound(model, "email", "max_length"), (
            f"the email field's maxLength={form_max} disagrees with {model.__name__}"
        )
    # The partner: the loop really covered three distinct models. Truncating it
    # to the first entry would otherwise be silent -- every entry is compliant.
    assert [m.__name__ for m in posted_to] == [
        "RegisterRequest",
        "LoginRequest",
        "MagicLinkRequest",
    ]
    assert form_min == 3
    assert form_max == 255


def test_login_keeps_a_lower_password_floor_than_register() -> None:
    """The one place the mirror is deliberately ASYMMETRIC, pinned so it stays so.

    ``LoginRequest.password`` is ``min_length=1`` on purpose: an account created
    before the 8-character rule must still be able to sign in. ``AuthModal``
    matches by omitting ``minLength`` in login mode only. If someone "fixes" the
    inconsistency by raising this to 8, those accounts lose their sign-in path
    and this test is what says so.
    """
    assert _model_bound(LoginRequest, "password", "min_length") == 1
    assert _model_bound(RegisterRequest, "password", "min_length") == 8
    assert 'minLength={mode === "login" ? undefined : PASSWORD_MIN_LENGTH}' in _read(_AUTH_MODAL), (
        "the login form no longer omits the register-only password floor"
    )


# ---------------------------------------------------------------------------
# The reader itself can fail. Prove it can.
# ---------------------------------------------------------------------------


def test_the_bound_reader_raises_on_a_field_with_no_bound() -> None:
    """Partner for ``_model_bound``: it must not quietly return a default.

    ``answer_style`` is a real field on the same model with no length metadata.
    If ``_model_bound`` returned ``None`` or 0 here, every comparison above could
    pass against a server that constrains nothing.
    """
    with pytest.raises(AssertionError, match="declares no max_length"):
        _model_bound(AnswerRequest, "answer_style", "max_length")


def test_the_constant_reader_raises_on_a_name_that_is_not_there() -> None:
    with pytest.raises(AssertionError, match="NOT_A_REAL_CONSTANT"):
        _const("const OTHER = 1;", "NOT_A_REAL_CONSTANT", _AUTH_MODAL)


# ---------------------------------------------------------------------------
# docs/API_SPEC.md — the published number, parsed rather than trusted (#446 review)
#
# The limits table added to §6 was a hand-written number no test read, in a repo
# that already parses this file for the error-code parity test
# (`test_errors_envelope._spec_error_codes`). A documented limit nothing checks
# is the same class of claim as a comment that says "keep these in sync".
# ---------------------------------------------------------------------------


def _spec_message_limits() -> tuple[int, int]:
    """Parse the ``| `message` | 1–4000 characters | … |`` row out of §6.

    Parses the SECTION, not the whole document: a substring search would pass on
    the number appearing anywhere, including in prose that contradicts the table.
    """
    lines = _read(_API_SPEC).splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().startswith("## 6.")), None)
    assert start is not None, "docs/API_SPEC.md has no section 6"
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|") or "`message`" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        found = re.findall(r"\d+", cells[1])
        assert len(found) == 2, f"the message row does not state two bounds: {cells[1]!r}"
        return int(found[0]), int(found[1])
    raise AssertionError("no `message` row found in docs/API_SPEC.md section 6")


def test_the_published_message_limits_match_the_model() -> None:
    """RED if the route's bounds move and §6's table is not updated with them."""
    doc_min, doc_max = _spec_message_limits()
    assert doc_min == _model_bound(AnswerRequest, "message", "min_length")
    assert doc_max == _model_bound(AnswerRequest, "message", "max_length")
    assert (doc_min, doc_max) == (1, 4000)


def test_the_published_limit_matches_what_the_browser_enforces() -> None:
    """The three numbers are one number: docs, server, and client.

    Without this the doc could agree with the server while the browser refused at
    a different length -- which is the drift the whole file exists to catch, just
    with the documentation as the third party.
    """
    _, doc_max = _spec_message_limits()
    browser = _const(_read(_COMPOSER_INPUT), "MAX_QUESTION_LENGTH", _COMPOSER_INPUT)
    assert doc_max == browser


def test_the_spec_parser_raises_rather_than_returning_a_default() -> None:
    """Partner for ``_spec_message_limits``: it must not pass on a missing row.

    A parser that quietly returned ``(1, 4000)`` when it found nothing would make
    both tests above green against a document that says nothing at all.
    """
    import unittest.mock

    empty_section = "## 6. Ask Question\n\nno table here\n"
    with (
        unittest.mock.patch.object(Path, "read_text", return_value=empty_section),
        pytest.raises(AssertionError, match="no `message` row found"),
    ):
        _spec_message_limits()
