"""Tests for ``app.core.token_secrets`` (ADR-0004 PR 14) -- the pure credential seam."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from app.core import token_secrets
from app.core.token_secrets import generate_token, hash_token, verify_token


def test_generate_token_is_64_hex_chars_and_unique() -> None:
    """RED if the entropy is cut (``_SECRET_BYTES``) or the generator repeats."""
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(re.fullmatch(r"[0-9a-f]{64}", t) for t in tokens)


def test_hash_token_is_the_sha256_hex_digest() -> None:
    """Pins the digest so a stored ``secret_hash`` stays comparable across deploys."""
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


def test_verify_token_accepts_the_right_secret_and_rejects_a_wrong_one() -> None:
    """RED if ``verify_token`` compares the raw token instead of its digest."""
    secret = generate_token()
    hashed = hash_token(secret)
    assert verify_token(secret, hashed)
    assert not verify_token(secret + "0", hashed)
    assert not verify_token("", hashed)


def test_module_has_no_app_imports() -> None:
    """The seam contract from the plan: zero ``app.*`` coupling -- portable as-is."""
    source = Path(token_secrets.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^from app\.", source, re.MULTILINE)
    assert not re.search(r"^import app", source, re.MULTILINE)
