"""Tests for the magic-link password step-up (ADR-0004 PR 15, #293).

The waiver of ``current_password`` is decided from the CALLER'S OWN session
row (``magic_link_verified_at`` within the window), never from the body, and
is one shot. Every docstring names the change that turns the test red.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import AuditEvent, AuthSession, Base, User

DEMO_BEARER = "Bearer local-demo-key"
EMAIL = "stepup@example.com"
OLD = "forgotten passphrase"
NEW = "brand new passphrase"
_TOKEN_RE = re.compile(r"/v1/auth/magic-link/confirm\?token=([0-9a-f]{32}\.[0-9a-f]{64})")


_ENV_KEYS = (
    "CITEVYN_DATABASE_URL",
    "CITEVYN_EMAIL_OUTBOX_DIR",
    "CITEVYN_RESEND_API_KEY",
    "CITEVYN_EMAIL_FROM",
    "CITEVYN_MAGIC_LINK_BASE_URL",
    "CITEVYN_RATE_LIMIT_MAGIC_LINK_PER_HOUR",
    "CITEVYN_RATE_LIMIT_PASSWORD_CHANGE_PER_HOUR",
)


@pytest.fixture
def step_up_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Generator[Callable[..., Path], None, None]:
    import app.core.rate_limit as rate_limit

    def _build(**env: str) -> Path:
        db_module.reset_engine()
        get_settings.cache_clear()
        outbox = tmp_path / "outbox"
        monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'su.db'}")
        monkeypatch.setenv("CITEVYN_EMAIL_OUTBOX_DIR", str(outbox))
        monkeypatch.setenv("CITEVYN_RESEND_API_KEY", "")
        monkeypatch.setenv("CITEVYN_EMAIL_FROM", "")
        monkeypatch.setenv("CITEVYN_MAGIC_LINK_BASE_URL", "")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        rate_limit.reset_limiter()
        engine = db_module.get_engine()

        async def _init_schema() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(_init_schema())
        return outbox

    try:
        yield _build
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()
        rate_limit.reset_limiter()
        for var in _ENV_KEYS:
            monkeypatch.delenv(var, raising=False)


@pytest.fixture
def step_up_app(step_up_factory: Callable[..., Path]) -> Path:
    return step_up_factory()


def _client() -> TestClient:
    return TestClient(create_app())


def _register(client: TestClient) -> str:
    r = client.post(
        "/v1/auth/register",
        json={"email": EMAIL, "password": OLD},
        headers={"Authorization": DEMO_BEARER},
    )
    assert r.status_code == 201, r.text
    return r.json()["user_id"]


def _login(client: TestClient, password: str) -> httpx.Response:
    return client.post(
        "/v1/auth/login",
        json={"email": EMAIL, "password": password},
        headers={"Authorization": DEMO_BEARER},
    )


def _me(client: TestClient) -> httpx.Response:
    return client.get("/v1/auth/me", headers={"Authorization": DEMO_BEARER})


def _update(client: TestClient, **body: str) -> httpx.Response:
    return client.post("/v1/auth/me/password", json=body, headers={"Authorization": DEMO_BEARER})


def _latest_token(outbox: Path) -> str:
    tokens = []
    for path in sorted(outbox.iterdir()):
        m = _TOKEN_RE.search(path.read_text(encoding="utf-8"))
        if m:
            tokens.append(m.group(1))
    assert tokens
    return tokens[-1]


def _subjects(outbox: Path) -> list[str]:
    out = []
    for path in sorted(outbox.iterdir()):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("Subject: "):
                out.append(line.removeprefix("Subject: "))
                break
    return out


def _sign_in_with_link(outbox: Path) -> TestClient:
    """A fresh browser that requests a link for EMAIL and redeems it."""
    browser = _client()
    r = browser.post(
        "/v1/auth/magic-link/request", json={"email": EMAIL}, headers={"Authorization": DEMO_BEARER}
    )
    assert r.status_code == 202
    r = browser.post(
        "/v1/auth/magic-link/confirm",
        content=f"token={_latest_token(outbox)}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/?auth=ok"
    return browser


def _run(coro):
    return asyncio.run(coro)


def _age_stamps(seconds: int) -> None:
    """Back-date every stamped session by ``seconds`` (models time passing)."""

    async def _go() -> None:
        factory = get_sessionmaker()
        async with factory() as session:
            await session.execute(
                update(AuthSession)
                .where(AuthSession.magic_link_verified_at.is_not(None))
                .values(magic_link_verified_at=datetime.now(UTC) - timedelta(seconds=seconds))
            )
            await session.commit()

    _run(_go())


def _password_hash() -> str | None:
    async def _go() -> str | None:
        factory = get_sessionmaker()
        async with factory() as session:
            return (
                (await session.execute(select(User).where(User.email == EMAIL)))
                .scalar_one()
                .password_hash
            )

    return _run(_go())


def _audit_metadata(event: str) -> list[dict]:
    async def _go() -> list[dict]:
        factory = get_sessionmaker()
        async with factory() as session:
            rows = (await session.execute(select(AuditEvent))).scalars().all()
            return [dict(r.metadata_) for r in rows if r.metadata_.get("event") == event]

    return _run(_go())


# ---------------------------------------------------------------------------


def test_magic_link_session_reports_step_up_and_a_password_session_does_not(
    step_up_app: Path,
) -> None:
    """``/me`` exposes the waiver so the UI can drop the current-password
    field. RED if ``password_step_up`` is derived from anything but the
    caller's own session stamp (a password login must report false)."""
    _register(_client())
    by_link = _sign_in_with_link(step_up_app)
    assert _me(by_link).json()["password_step_up"] is True
    by_password = _client()
    assert _login(by_password, OLD).status_code == 200
    assert _me(by_password).json()["password_step_up"] is False
    assert _login(_client(), OLD).json()["password_step_up"] is False


def test_forgotten_password_can_be_replaced_after_a_magic_link_sign_in(step_up_app: Path) -> None:
    """THE #293 scenario. RED if ``update_password`` ignores the session
    stamp: the forgotten-password user would get the 422 forever."""
    _register(_client())
    hash_before = _password_hash()
    browser = _sign_in_with_link(step_up_app)

    response = _update(browser, new_password=NEW)
    assert response.status_code == 200, response.text
    assert response.json()["has_password"] is True
    assert _password_hash() != hash_before
    assert _login(_client(), NEW).status_code == 200
    assert _login(_client(), OLD).status_code == 401
    # The registering browser's session is the one revoked; the audit row
    # records that the waiver was used.
    assert _audit_metadata("password_changed") == [
        {"event": "password_changed", "sessions_revoked": 1, "step_up": "magic_link"}
    ]


def test_step_up_is_one_shot(step_up_app: Path) -> None:
    """RED if the stamp is not cleared on use: a second change on the same
    session would again need no current password."""
    _register(_client())
    browser = _sign_in_with_link(step_up_app)
    assert _update(browser, new_password=NEW).status_code == 200
    assert _me(browser).json()["password_step_up"] is False
    again = _update(browser, new_password="yet another passphrase")
    assert again.status_code == 422
    assert (
        _update(browser, current_password=NEW, new_password="yet another passphrase").status_code
        == 200
    )


def test_step_up_expires_with_the_window(step_up_app: Path) -> None:
    """RED if ``step_up_active`` ignores ``password_step_up_window_seconds``."""
    _register(_client())
    browser = _sign_in_with_link(step_up_app)
    _age_stamps(get_settings().password_step_up_window_seconds + 1)
    assert _me(browser).json()["password_step_up"] is False
    assert _update(browser, new_password=NEW).status_code == 422
    assert _login(_client(), OLD).status_code == 200, "nothing changed"


def test_step_up_never_leaks_to_another_session_of_the_same_account(step_up_app: Path) -> None:
    """The hijack shape: a second live session (an attacker's, or just another
    device) must not inherit the waiver from the owner's link click. RED if
    the decision keys on the USER (e.g. a recent audit event) instead of the
    caller's own session row."""
    _register(_client())
    other = _client()
    assert _login(other, OLD).status_code == 200
    _sign_in_with_link(step_up_app)  # the owner's phone
    assert _me(other).json()["password_step_up"] is False
    assert _update(other, new_password=NEW).status_code == 422
    assert _login(_client(), OLD).status_code == 200


def test_step_up_cannot_be_asserted_by_the_body(step_up_app: Path) -> None:
    """A password-login session sending extra fields gains nothing. RED if
    any body field is consulted for the waiver."""
    _register(_client())
    browser = _client()
    assert _login(browser, OLD).status_code == 200
    r = browser.post(
        "/v1/auth/me/password",
        json={"new_password": NEW, "password_step_up": True, "magic_link_verified_at": "now"},
        headers={"Authorization": DEMO_BEARER},
    )
    assert r.status_code == 422


def test_password_set_and_change_email_the_account(step_up_app: Path) -> None:
    """Guardrail 2 of #293: every set/change is announced to the inbox owner.
    RED if the background task is dropped or the notice carries a token."""
    _register(_client())
    browser = _sign_in_with_link(step_up_app)
    assert _update(browser, new_password=NEW).status_code == 200
    subjects = _subjects(step_up_app)
    assert subjects[-1] == "Your CiteVyn password was changed", subjects
    notice = sorted(step_up_app.iterdir())[-1].read_text(encoding="utf-8")
    assert "To: stepup@example.com" in notice
    assert "Email me a sign-in link" in notice
    assert _TOKEN_RE.search(notice) is None


def _null_password() -> None:
    async def _go() -> None:
        factory = get_sessionmaker()
        async with factory() as session:
            await session.execute(
                update(User).where(User.email == EMAIL).values(password_hash=None)
            )
            await session.commit()

    _run(_go())


def test_passwordless_account_on_a_link_session_does_a_plain_first_time_set(
    step_up_app: Path,
) -> None:
    """An OAuth-created (passwordless) account that signed in by link: the
    set is a first-time set (``password_set``, no ``step_up`` key) even though
    the session is stamped. RED if the audit metadata or ``has_password``
    conflate the two paths."""
    _register(_client())
    _null_password()
    browser = _sign_in_with_link(step_up_app)
    me = _me(browser).json()
    assert me["has_password"] is False and me["password_step_up"] is True
    assert _update(browser, new_password=NEW).status_code == 200
    assert _audit_metadata("password_set") == [{"event": "password_set", "sessions_revoked": 1}]
    assert _audit_metadata("password_changed") == []
    assert _subjects(step_up_app)[-1] == "Your CiteVyn password was set"


def test_stamp_does_not_survive_a_session_rotation(step_up_app: Path) -> None:
    """A password login on the stepped-up browser rotates the session
    (``claim_and_login`` deletes the old row) and the NEW row carries no
    stamp. RED if the stamp were copied across the rotation."""
    _register(_client())
    browser = _sign_in_with_link(step_up_app)
    assert _me(browser).json()["password_step_up"] is True
    assert _login(browser, OLD).status_code == 200
    assert _me(browser).json()["password_step_up"] is False
    assert _update(browser, new_password=NEW).status_code == 422


def test_password_change_notices_are_capped_per_address_but_changes_are_not(
    step_up_factory: Callable[..., Path],
) -> None:
    """The mail-cannon finding: registration never verifies an address, so an
    attacker who registers a victim's email must not be able to send it
    unlimited "password changed" notices. The NOTICE is throttled at the
    per-address magic-link limit; the change itself is not. RED if
    ``email_notice_allowed`` is bypassed."""
    outbox = step_up_factory(
        CITEVYN_RATE_LIMIT_MAGIC_LINK_PER_HOUR="2",
        CITEVYN_RATE_LIMIT_PASSWORD_CHANGE_PER_HOUR="100",
    )
    client = _client()
    _register(client)
    current = OLD
    statuses = []
    for i in range(4):
        new = f"rotated passphrase {i}"
        statuses.append(_update(client, current_password=current, new_password=new).status_code)
        current = new
    assert statuses == [200, 200, 200, 200]
    assert _subjects(outbox).count("Your CiteVyn password was changed") == 2


def test_current_password_changes_are_capped_per_user_but_the_recovery_set_is_exempt(
    step_up_app: Path,
) -> None:
    """The camping finding: an intruder who learned the password cannot loop
    changes (each revoking the owner's sessions) beyond the per-user cap,
    while the owner's stepped-up recovery set still works with that bucket
    full. RED if the cap is removed or applied to the stepped-up set."""
    client = _client()
    _register(client)
    current = OLD
    statuses = []
    for i in range(4):
        new = f"camped passphrase {i}"
        r = _update(client, current_password=current, new_password=new)
        statuses.append(r.status_code)
        if r.status_code == 200:
            current = new
    assert statuses == [200, 200, 200, 429], statuses
    assert "password changes" in r.json()["error"]["message"]
    # Bucket full for this user: the owner's recovery via a fresh link still lands.
    owner = _sign_in_with_link(step_up_app)
    assert _update(owner, new_password=NEW).status_code == 200
    assert _login(_client(), NEW).status_code == 200


def test_notice_bucket_is_separate_from_the_link_request_bucket(
    step_up_factory: Callable[..., Path],
) -> None:
    """A thief holding a stolen link must not be able to silence the sign-in
    notice by draining the link-REQUEST bucket first. RED if the notice key
    shares the ``magiclink`` prefix (both skeptics found no test for this)."""
    outbox = step_up_factory(CITEVYN_RATE_LIMIT_MAGIC_LINK_PER_HOUR="2")
    _register(_client())
    thief = _client()
    # Two requests: the first mints the link the thief "stole", the second
    # exhausts the request bucket for the address.
    for _ in range(2):
        assert (
            thief.post(
                "/v1/auth/magic-link/request",
                json={"email": EMAIL},
                headers={"Authorization": DEMO_BEARER},
            ).status_code
            == 202
        )
    assert (
        thief.post(
            "/v1/auth/magic-link/request",
            json={"email": EMAIL},
            headers={"Authorization": DEMO_BEARER},
        ).status_code
        == 429
    )
    r = thief.post(
        "/v1/auth/magic-link/confirm",
        content=f"token={_latest_token(outbox)}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/?auth=ok"
    assert _subjects(outbox).count("New sign-in to CiteVyn") == 1
    assert _audit_metadata("magic_link") == [{"event": "magic_link"}]


def test_a_suppressed_notice_is_recorded_on_the_audit_row(
    step_up_factory: Callable[..., Path],
) -> None:
    """When the per-address ceiling drops a notice, operators can still see
    it happened. RED if ``notice_suppressed`` is not written."""
    step_up_factory(
        CITEVYN_RATE_LIMIT_MAGIC_LINK_PER_HOUR="1",
        CITEVYN_RATE_LIMIT_PASSWORD_CHANGE_PER_HOUR="100",
    )
    client = _client()
    _register(client)
    first = _update(client, current_password=OLD, new_password=NEW)
    second = _update(client, current_password=NEW, new_password="third passphrase 3")
    assert first.status_code == second.status_code == 200
    rows = _audit_metadata("password_changed")
    assert [r.get("notice_suppressed") for r in rows] == [None, True]
