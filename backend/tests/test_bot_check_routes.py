"""The sign-up bot check and the public config (ADR-0005 Phase 5A).

* ``GET /v1/config`` tells the frontend whether the access model is on, so the
  pricing section and the usage meter render only then. Public; leaks nothing
  that a billing 404 does not already show.
* ``GET /v1/auth/challenge`` issues a proof-of-work challenge (``app.core.pow``).
* ``POST /v1/auth/register`` and ``POST /v1/auth/magic-link/request`` need a
  solved challenge in the ``X-CiteVyn-Bot-Check`` header, used once.

Everything is off, exactly as today, while ``CITEVYN_ACCESS_MODEL_ENABLED`` is
off. Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import db as db_module
from app.core.config import Settings, get_settings
from app.core.pow import solve
from app.main import create_app
from app.models import Base

DEMO = {"Authorization": "Bearer local-demo-key"}
ON = {
    "CITEVYN_ACCESS_MODEL_ENABLED": "true",
    "CITEVYN_BOT_CHECK_SECRET": "b" * 32,
    "CITEVYN_BOT_CHECK_MAX_NUMBER": "20",
}


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> Generator[pytest.MonkeyPatch, None, None]:
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'bc.db'}")
    monkeypatch.setenv("CITEVYN_EMAIL_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("CITEVYN_RESEND_API_KEY", "")
    get_settings.cache_clear()
    rate_limit.reset_limiter()

    async def _init() -> None:
        async with db_module.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield monkeypatch
    get_settings.cache_clear()
    db_module.reset_engine()
    rate_limit.reset_limiter()


def _on(mp: pytest.MonkeyPatch) -> None:
    for k, v in ON.items():
        mp.setenv(k, v)
    get_settings.cache_clear()


def _solution(client: TestClient) -> str:
    res = client.get("/v1/auth/challenge", headers=DEMO)
    assert res.status_code == 200, res.text
    challenge = res.json()["challenge"]
    payload = {**challenge, "number": solve(challenge)}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _register(client: TestClient, email: str, proof: str | None) -> Any:
    headers = {**DEMO, **({"X-CiteVyn-Bot-Check": proof} if proof is not None else {})}
    return client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct horse battery"},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# /v1/config
# ---------------------------------------------------------------------------


def test_config_reports_the_access_model_off_by_default(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the flag is reported as on, or a bot check is advertised,
    while the access model is off (the landing page must stay as it is)."""
    with TestClient(create_app()) as client:
        res = client.get("/v1/config", headers=DEMO)
    assert res.status_code == 200
    body = res.json()
    assert body["access_model"] is False and body["bot_check"] is None


def test_config_reports_the_access_model_on(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the frontend cannot learn the setting (hard-coded false)."""
    _on(env)
    with TestClient(create_app()) as client:
        body = client.get("/v1/config", headers=DEMO).json()
    assert body["access_model"] is True
    assert body["bot_check"] == {"kind": "pow"}
    assert set(body) == {"request_id", "access_model", "bot_check"}  # nothing else leaks


# ---------------------------------------------------------------------------
# The challenge and the check
# ---------------------------------------------------------------------------


def test_with_the_setting_off_there_is_no_challenge_and_no_check(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the check runs, or the challenge exists, while it is off."""
    with TestClient(create_app()) as client:
        assert client.get("/v1/auth/challenge", headers=DEMO).status_code == 404
        assert _register(client, "a@example.com", None).status_code == 201


def test_sign_up_needs_a_solved_challenge(env: pytest.MonkeyPatch) -> None:
    """Turns red if: register skips the check with the setting on."""
    _on(env)
    with TestClient(create_app()) as client:
        res = _register(client, "a@example.com", None)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "bot_check_failed"


@pytest.mark.parametrize("proof", ["", "not-base64!", base64.urlsafe_b64encode(b"[1]").decode()])
def test_a_malformed_proof_is_refused_not_a_crash(env: pytest.MonkeyPatch, proof: str) -> None:
    """Untrusted header. Turns red if: a bad value is a 500 or is accepted."""
    _on(env)
    with TestClient(create_app()) as client:
        res = _register(client, "a@example.com", proof)
    assert res.status_code == 403


def test_a_solved_challenge_signs_up_once_and_a_replay_is_refused(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a genuine solution is refused, or one solution can be
    reused for a second account (the single-use record is skipped)."""
    _on(env)
    with TestClient(create_app()) as client:
        proof = _solution(client)
        assert _register(client, "a@example.com", proof).status_code == 201
        replay = _register(TestClient(create_app()), "b@example.com", proof)
    assert replay.status_code == 403
    assert replay.json()["error"]["code"] == "bot_check_failed"


def test_one_solution_is_one_attempt_even_a_failed_one(env: pytest.MonkeyPatch) -> None:
    """Found in review: when a refused sign-up left its solution unused, one
    solve could probe "is this email registered?" without limit. Each solution
    now pays for exactly one attempt; the browser solves a new one to retry.
    Turns red if: a refused request leaves the solution usable."""
    _on(env)
    with TestClient(create_app()) as client:
        proof = _solution(client)
        weak = client.post(
            "/v1/auth/register",
            json={"email": "a@example.com", "password": "x"},
            headers={**DEMO, "X-CiteVyn-Bot-Check": proof},
        )
        assert weak.status_code == 422
        again = _register(client, "a@example.com", proof)
        assert again.status_code == 403
        assert _register(client, "a@example.com", _solution(client)).status_code == 201


@pytest.mark.parametrize(("offset", "expected"), [(-0.5, 201), (0.0, 403), (0.5, 403)])
def test_a_used_solution_cannot_be_replayed_at_its_expiry(
    env: pytest.MonkeyPatch, offset: float, expected: int
) -> None:
    """Found in review: the expiry check used whole seconds and the clean-up the
    exact time, so in the second after expiry each replay deleted the "used" row
    and passed. Before expiry a FRESH solution still works (the partner case).
    Turns red if: the expiry check and the clean-up disagree again."""
    import datetime as real_datetime

    import app.access.bot_check as bot_check

    _on(env)
    with TestClient(create_app()) as client:
        proof = _solution(client)
        payload = json.loads(base64.urlsafe_b64decode(proof))
        expires = int(str(payload["salt"]).rpartition("?expires=")[2])
        if expected == 403:
            assert _register(client, "a@example.com", proof).status_code == 201

        class Frozen(real_datetime.datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Any:  # noqa: ARG003
                return real_datetime.datetime.fromtimestamp(expires + offset, real_datetime.UTC)

        env.setattr(bot_check, "datetime", Frozen)
        res = _register(TestClient(create_app()), "b@example.com", proof)
    assert res.status_code == expected


def test_a_magic_link_request_needs_a_solved_challenge_too(env: pytest.MonkeyPatch) -> None:
    """Asking for a link sends an email, which costs money and can be abused.
    Turns red if: the magic-link request skips the check."""
    _on(env)
    with TestClient(create_app()) as client:
        bare = client.post(
            "/v1/auth/magic-link/request", json={"email": "a@example.com"}, headers=DEMO
        )
        proof = _solution(client)
        solved = client.post(
            "/v1/auth/magic-link/request",
            json={"email": "a@example.com"},
            headers={**DEMO, "X-CiteVyn-Bot-Check": proof},
        )
    assert bare.status_code == 403
    assert solved.status_code == 202


def test_production_with_the_access_model_on_needs_a_bot_check_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turns red if: production can start with the model on and no secret, so
    every challenge would be refused (or signed with a blank key)."""
    from pydantic import ValidationError

    monkeypatch.delenv("CITEVYN_BOT_CHECK_SECRET", raising=False)  # conftest's test key

    kw: dict[str, Any] = {
        "environment": "production",
        "llm_provider": "gemini",
        "gemini_api_key": "gk",
        "admin_api_key": "a-strong-admin-secret",
        "public_client_token": "a-strong-demo-secret",
        "access_model_enabled": True,
        "stripe_secret_key": "sk_live_x",
        "stripe_webhook_secret": "whsec_x",
        "stripe_price_pro_monthly": "price_m",
        "stripe_price_pro_yearly": "price_y",
        "_env_file": None,
    }
    with pytest.raises(ValidationError, match="CITEVYN_BOT_CHECK_SECRET"):
        Settings(**kw)
    kw["bot_check_secret"] = "a" * 31  # a short key makes challenges guessable
    with pytest.raises(ValidationError, match="CITEVYN_BOT_CHECK_SECRET"):
        Settings(**kw)
    kw["bot_check_secret"] = "a" * 32
    assert Settings(**kw).bot_check_secret == "a" * 32  # partner: set, it starts


def test_expired_uses_are_cleaned_up_on_the_next_check(env: pytest.MonkeyPatch) -> None:
    """The table must not grow forever. Turns red if: expired rows are kept."""
    from datetime import UTC, datetime, timedelta

    from app.core.db import get_sessionmaker
    from app.models import BotCheckUse

    async def _rows() -> list[str]:
        async with get_sessionmaker()() as s:
            from sqlalchemy import select

            return [r.challenge for r in (await s.execute(select(BotCheckUse))).scalars()]

    async def _old() -> None:
        async with get_sessionmaker()() as s:
            long_ago = datetime.now(UTC) - timedelta(days=1)
            s.add(BotCheckUse(challenge="0" * 64, used_at=long_ago, expires_at=long_ago))
            await s.commit()

    _on(env)
    asyncio.run(_old())
    with TestClient(create_app()) as client:
        assert _register(client, "a@example.com", _solution(client)).status_code == 201
    rows = asyncio.run(_rows())
    assert "0" * 64 not in rows and len(rows) == 1  # the old one gone, the new one kept


def test_a_fresh_solution_is_refused_from_the_instant_it_expires(
    env: pytest.MonkeyPatch,
) -> None:
    """Expiry uses the exact time, not whole seconds. Turns red if: a solution
    is still accepted at its expiry instant (the rounding the review found)."""
    import datetime as real_datetime

    import app.access.bot_check as bot_check

    _on(env)
    with TestClient(create_app()) as client:
        proof = _solution(client)
        payload = json.loads(base64.urlsafe_b64decode(proof))
        expires = int(str(payload["salt"]).rpartition("?expires=")[2])

        class AtExpiry(real_datetime.datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Any:  # noqa: ARG003
                return real_datetime.datetime.fromtimestamp(expires, real_datetime.UTC)

        env.setattr(bot_check, "datetime", AtExpiry)
        res = _register(client, "a@example.com", proof)
    assert res.status_code == 403
