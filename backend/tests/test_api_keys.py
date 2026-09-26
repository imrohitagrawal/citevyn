"""Per-user API keys (ADR-0005 §4, Phase 6A).

"Shown once, stored only as a hash, scoped, revocable." A Pro feature, and off
(404) unless ``CITEVYN_ACCESS_MODEL_ENABLED`` is on. The key authenticates the
MCP server (Phase 6B) through ``app.access.api_keys.authenticate_api_key``.

Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.access.api_keys import authenticate_api_key
from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.token_secrets import hash_token
from app.main import create_app
from app.models import ApiKey, Base, Membership
from tests.conftest import bot_check_headers

DEMO = {"Authorization": "Bearer local-demo-key"}


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> Generator[pytest.MonkeyPatch, None, None]:
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'k.db'}")
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
    mp.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "true")
    get_settings.cache_clear()


def _signed_in(client: TestClient, email: str = "a@example.com") -> str:
    res = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct horse battery"},
        headers={**DEMO, **bot_check_headers(client)},
    )
    assert res.status_code == 201, res.text
    return str(res.json()["user_id"])


def _make_pro(user_id: str, sub: str = "sub_1") -> None:
    async def _run() -> None:
        async with get_sessionmaker()() as s:
            s.add(
                Membership(
                    account_id=user_id,
                    stripe_subscription_id=sub,
                    plan="pro",
                    status="active",
                    cancel_at_period_end=False,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            await s.commit()

    asyncio.run(_run())


def _auth(raw: str) -> ApiKey | None:
    async def _run() -> ApiKey | None:
        async with get_sessionmaker()() as s:
            key = await authenticate_api_key(s, raw)
            await s.commit()
            return key

    return asyncio.run(_run())


def _rows() -> list[ApiKey]:
    async def _run() -> list[ApiKey]:
        async with get_sessionmaker()() as s:
            return list((await s.execute(select(ApiKey))).scalars())

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Off and not Pro
# ---------------------------------------------------------------------------


def test_with_the_setting_off_there_are_no_api_keys(env: pytest.MonkeyPatch) -> None:
    """Turns red if: keys can be made while the access model is off."""
    with TestClient(create_app()) as client:
        _signed_in(client)
        assert (
            client.post("/v1/me/api-keys", json={"name": "laptop"}, headers=DEMO).status_code == 404
        )
        assert client.get("/v1/me/api-keys", headers=DEMO).status_code == 404


def test_a_free_account_cannot_make_keys(env: pytest.MonkeyPatch) -> None:
    """API keys are Pro (ADR-0005 §1). Turns red if: the capability is missing."""
    _on(env)
    with TestClient(create_app()) as client:
        _signed_in(client)
        res = client.post("/v1/me/api-keys", json={"name": "laptop"}, headers=DEMO)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "plan_required"


# ---------------------------------------------------------------------------
# Create, list, revoke
# ---------------------------------------------------------------------------


def test_a_key_is_shown_once_and_stored_only_as_a_hash(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the secret is stored, returned again, or the hash is not
    the SHA-256 of the secret."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        created = client.post("/v1/me/api-keys", json={"name": " laptop "}, headers=DEMO)
        listed = client.get("/v1/me/api-keys", headers=DEMO).json()
    assert created.status_code == 201
    body = created.json()
    raw = body["key"]
    assert raw.startswith("cvk_") and body["name"] == "laptop"
    [row] = _rows()
    secret = raw.rsplit("_", 1)[1]
    assert row.secret_hash == hash_token(secret) and secret not in str(vars(row))
    [item] = listed["keys"]
    assert "key" not in item and raw not in str(listed)
    assert item["prefix"] == raw[: len(item["prefix"])] and len(item["prefix"]) < len(raw)


def test_the_key_authenticates_its_owner_until_revoked(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a valid key is refused, a revoked one accepted, or
    last_used_at is not recorded."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        raw = client.post("/v1/me/api-keys", json={"name": "ci"}, headers=DEMO).json()["key"]
        key = _auth(raw)
        assert key is not None and key.user_id == user
        assert _rows()[0].last_used_at is not None
        key_id = client.get("/v1/me/api-keys", headers=DEMO).json()["keys"][0]["key_id"]
        assert client.delete(f"/v1/me/api-keys/{key_id}", headers=DEMO).status_code == 204
    assert _auth(raw) is None


@pytest.mark.parametrize(
    "mangle",
    [
        lambda k: k[:-1] + ("0" if k[-1] != "0" else "1"),  # wrong secret
        lambda k: "cvk_" + "0" * 32 + "_" + k.rsplit("_", 1)[1],  # unknown key id
        lambda k: k.replace("cvk_", "xyz_"),  # wrong prefix
        lambda k: "",
        lambda k: "cvk_",
        lambda k: "cvk_a_b_c",
        lambda k: "junk" + k,  # a valid key inside other text is not a key
        lambda k: k + "0",
    ],
)
def test_a_wrong_or_malformed_key_is_refused(env: pytest.MonkeyPatch, mangle: Any) -> None:
    """Turns red if: anything but the exact key (surrounding whitespace aside,
    see below) authenticates, or a bad shape raises."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        raw = client.post("/v1/me/api-keys", json={"name": "x"}, headers=DEMO).json()["key"]
    assert _auth(mangle(raw)) is None


def test_you_cannot_see_or_revoke_someone_elses_key(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the list or the revoke is not scoped to the caller."""
    _on(env)
    alice_client = TestClient(create_app())
    bob_client = TestClient(create_app())
    alice = _signed_in(alice_client, "alice@example.com")
    bob = _signed_in(bob_client, "bob@example.com")
    _make_pro(alice, "sub_a")
    _make_pro(bob, "sub_b")
    raw = alice_client.post("/v1/me/api-keys", json={"name": "a"}, headers=DEMO).json()["key"]
    alice_key = alice_client.get("/v1/me/api-keys", headers=DEMO).json()["keys"][0]["key_id"]
    assert bob_client.get("/v1/me/api-keys", headers=DEMO).json()["keys"] == []
    assert bob_client.delete(f"/v1/me/api-keys/{alice_key}", headers=DEMO).status_code == 404
    assert _auth(raw) is not None  # still valid


def test_at_most_ten_live_keys(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the cap is missing, or revoked keys still count."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        for i in range(10):
            assert (
                client.post("/v1/me/api-keys", json={"name": f"k{i}"}, headers=DEMO).status_code
                == 201
            )
        over = client.post("/v1/me/api-keys", json={"name": "k10"}, headers=DEMO)
        assert over.status_code == 409
        first = client.get("/v1/me/api-keys", headers=DEMO).json()["keys"][0]["key_id"]
        client.delete(f"/v1/me/api-keys/{first}", headers=DEMO)
        again = client.post("/v1/me/api-keys", json={"name": "k10"}, headers=DEMO)
    assert again.status_code == 201


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
def test_a_key_needs_a_short_name(env: pytest.MonkeyPatch, name: str) -> None:
    """Turns red if: an empty or over-long name is accepted."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        assert client.post("/v1/me/api-keys", json={"name": name}, headers=DEMO).status_code == 422


def test_surrounding_whitespace_from_a_paste_is_ignored(env: pytest.MonkeyPatch) -> None:
    """Deliberate: a key pasted with a trailing newline still works; the secret
    is unchanged, so nothing is weakened. Turns red if: the strip is dropped."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        raw = client.post("/v1/me/api-keys", json={"name": "x"}, headers=DEMO).json()["key"]
    assert _auth(f"  {raw}\n") is not None


def test_a_lapsed_account_can_still_see_and_revoke_its_keys(env: pytest.MonkeyPatch) -> None:
    """Found in review: after Pro lapsed, list and revoke were Pro-only (403)
    while the keys still authenticated, so a leaked key could not be killed.
    Turns red if: list or revoke needs Pro. Creating a key still does."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        raw = client.post("/v1/me/api-keys", json={"name": "x"}, headers=DEMO).json()["key"]

        async def lapse() -> None:
            async with get_sessionmaker()() as s:
                for m in (await s.execute(select(Membership))).scalars():
                    m.status = "canceled"
                await s.commit()

        asyncio.run(lapse())
        assert client.post("/v1/me/api-keys", json={"name": "y"}, headers=DEMO).status_code == 403
        listed = client.get("/v1/me/api-keys", headers=DEMO)
        assert listed.status_code == 200
        key_id = listed.json()["keys"][0]["key_id"]
        assert client.delete(f"/v1/me/api-keys/{key_id}", headers=DEMO).status_code == 204
    assert _auth(raw) is None


@pytest.mark.parametrize(
    "name", ["bad\u0007bell", "flip\u202eme", "zero\u200bwidth", "tab\there", "line\u2028break"]
)
def test_a_key_name_has_no_control_or_direction_characters(
    env: pytest.MonkeyPatch, name: str
) -> None:
    """A right-to-left override can make a name look like another in any list
    that shows it. Turns red if: such characters are accepted."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        assert client.post("/v1/me/api-keys", json={"name": name}, headers=DEMO).status_code == 422


@pytest.mark.parametrize(
    "name",
    ["\U0001f469\u200d\U0001f4bb laptop", "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"],
)
def test_joiners_that_real_names_use_are_allowed(env: pytest.MonkeyPatch, name: str) -> None:
    """The zero-width joiner builds emoji like a person at a laptop, and the
    non-joiner is part of Persian spelling. Turns red if: they are rejected
    along with the other formatting characters."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        assert client.post("/v1/me/api-keys", json={"name": name}, headers=DEMO).status_code == 201
