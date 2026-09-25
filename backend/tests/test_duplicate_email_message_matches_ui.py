"""The register form must recognise the exact duplicate-email text the API sends (#483).

``AuthModal.tsx`` shows a "Sign in instead" button only when a register attempt
fails with a 422 whose message is the duplicate-email message. It matches the
WHOLE string with ``===`` (not a substring), because a 422 alone also means
"password too short" and must not offer sign-in.

That makes the message a contract between two files in two languages. Before this
guard, rewording it in ``backend/app/api/routes/auth.py`` would have left the
button silently gone, with every test on both sides still green: the backend tests
assert the backend's text and the frontend tests assert the frontend's copy.

WHAT THIS SEES: that the literal the frontend compares against equals the constant
the route raises, and that the route uses that constant in both of its duplicate
paths. The vitest suite (``AuthModal.test.tsx``) proves the component really
shows the button for that literal.

WHAT THIS CANNOT SEE: it reads the TSX as text. It cannot tell whether the
component's comparison is reachable or correct; that is the vitest suite's job.
It also cannot see a proxy or client that rewrites ``error.message`` in flight.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.api.routes import auth as auth_routes

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AUTH_MODAL = _REPO_ROOT / "frontend" / "src" / "components" / "AuthModal.tsx"
_AUTH_ROUTE = _REPO_ROOT / "backend" / "app" / "api" / "routes" / "auth.py"


def _frontend_literal() -> str:
    assert _AUTH_MODAL.is_file(), f"{_AUTH_MODAL} does not exist — this guard is pointed at nothing"
    text = _AUTH_MODAL.read_text(encoding="utf-8")
    match = re.search(r'^const DUPLICATE_EMAIL_MESSAGE\s*=\s*"([^"\\]*)";', text, re.MULTILINE)
    assert match is not None, "DUPLICATE_EMAIL_MESSAGE is not declared in AuthModal.tsx"
    return match.group(1)


def test_the_route_constant_is_the_published_text() -> None:
    # Asserted as a literal on purpose: reading only the constant would let both
    # sides move together silently. Rewording is meant to be a visible edit here.
    # RED WHEN: DUPLICATE_EMAIL_MESSAGE in auth.py is reworded.
    assert auth_routes.DUPLICATE_EMAIL_MESSAGE == (
        "This email is already registered. Please sign in to continue."
    )


def test_the_register_form_matches_the_text_the_api_sends() -> None:
    # RED WHEN: either DUPLICATE_EMAIL_MESSAGE (auth.py or AuthModal.tsx) changes
    # without the other.
    assert _frontend_literal() == auth_routes.DUPLICATE_EMAIL_MESSAGE


def test_both_duplicate_paths_raise_the_shared_constant() -> None:
    # The two paths (existence pre-check and the IntegrityError race branch) are
    # both exercised end-to-end in test_auth_routes.py; this is the cheap static
    # partner that names the drift if someone inlines a second copy of the text.
    # RED WHEN: either path stops using the constant (e.g. inlines its own string).
    source = _AUTH_ROUTE.read_text(encoding="utf-8")
    uses = re.findall(r"message=DUPLICATE_EMAIL_MESSAGE,", source)
    assert len(uses) == 2, f"expected both duplicate paths to use the constant, found {len(uses)}"
    assert "already registered" not in source.replace(auth_routes.DUPLICATE_EMAIL_MESSAGE, ""), (
        "a second copy of the duplicate-email text exists in auth.py"
    )
