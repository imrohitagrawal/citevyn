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
from app.api.routes.messages import AnswerRequest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AUTH_MODAL = _REPO_ROOT / "frontend" / "src" / "components" / "AuthModal.tsx"
_COMPOSER_INPUT = _REPO_ROOT / "frontend" / "src" / "lib" / "composerInput.ts"
_HERO = _REPO_ROOT / "frontend" / "src" / "components" / "Hero.tsx"
_CHAT_VIEW = _REPO_ROOT / "frontend" / "src" / "components" / "ChatView.tsx"


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
        (_HERO, "maxLength={MAX_QUESTION_LENGTH}"),
        (_CHAT_VIEW, "maxLength={MAX_QUESTION_LENGTH}"),
    ):
        assert needle in _read(path), f"{path.name} no longer declares {needle}"


# ---------------------------------------------------------------------------
# message — both composers
# ---------------------------------------------------------------------------


def test_the_composers_carry_the_servers_message_ceiling() -> None:
    """RED if ``AnswerRequest.message``'s ``max_length`` moves without the UI."""
    server = _model_bound(AnswerRequest, "message", "max_length")
    browser = _const(_read(_COMPOSER_INPUT), "MAX_QUESTION_LENGTH", _COMPOSER_INPUT)
    assert browser == server, (
        f"the composers cap a question at {browser} but the server caps it at "
        f"{server} — the browser accepts what the server 422s, and the box is "
        "cleared before the request, so the user loses the text"
    )
    assert server == 4000, "the published limit in docs/API_SPEC.md §5 moved"


@pytest.mark.parametrize("path", [_HERO, _CHAT_VIEW], ids=["hero", "chat"])
def test_both_composers_use_the_shared_constant_not_a_copy(path: Path) -> None:
    """A second literal is how the two composers drift apart again.

    Asserted as "the attribute is bound to the CONSTANT", not "the attribute is
    4000": a hard-coded 4000 in one composer would satisfy a value check and
    still be the exact duplication #445 and #446 both exist to remove.
    """
    text = _read(path)
    assert "maxLength={MAX_QUESTION_LENGTH}" in text, (
        f"{path.name} does not bind its maxLength to the shared constant"
    )
    assert not re.search(r"maxLength=\{?\s*4000", text), (
        f"{path.name} hard-codes the ceiling instead of importing it"
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
    """These two numbers had no mirror at all before #446."""
    modal = _read(_AUTH_MODAL)
    assert _const(modal, "EMAIL_MIN_LENGTH", _AUTH_MODAL) == _model_bound(
        RegisterRequest, "email", "min_length"
    )
    assert _const(modal, "EMAIL_MAX_LENGTH", _AUTH_MODAL) == _model_bound(
        RegisterRequest, "email", "max_length"
    )
    assert _model_bound(RegisterRequest, "email", "min_length") == 3
    assert _model_bound(RegisterRequest, "email", "max_length") == 255


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
