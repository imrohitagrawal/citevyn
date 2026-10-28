"""The "Delete my account" route (#546, PR 2): ``POST /v1/auth/me/delete``.

The account holder deletes their own account. It needs a fresh email
confirmation: the caller's OWN session must have been signed in with an
emailed link within the step-up window (``step_up_active``, ADR-0004 PR 15),
the same proof that lets a forgotten password be replaced. It runs the same
``delete_account`` as the operator's command: the same refusal while a
subscription can still bill, the same tombstone, the same outside deletion
list (#546 PR 1). Every docstring names the change that turns the test red.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.core import deletion_list
from app.core.db import get_sessionmaker
from app.core.deletion_list import DeletionList
from app.models import AuthSession, DeletedAccount, Membership, User
from app.models.deleted_accounts import email_hash
from tests.test_password_step_up import (  # noqa: F401 - fixtures
    DEMO_BEARER,
    EMAIL,
    _age_stamps,
    _client,
    _login,
    _me,
    _register,
    _sign_in_with_link,
    step_up_app,
    step_up_factory,
)
from tests.test_password_step_up import OLD as PASSWORD

KEY = "test:deleted_accounts"


def _delete(client: Any, user_id: str | None = None) -> Any:
    """Delete as the page does: naming the account it shows (by default the
    account this browser is signed in to)."""
    if user_id is None:
        me = client.get("/v1/auth/me", headers={"Authorization": DEMO_BEARER})
        user_id = me.json()["user_id"] if me.status_code == 200 else "usr_" + "0" * 32
    return client.post(
        "/v1/auth/me/delete", json={"user_id": user_id}, headers={"Authorization": DEMO_BEARER}
    )


def _count(model: Any, *where: Any) -> int:
    async def _go() -> int:
        async with get_sessionmaker()() as s:
            query = select(func.count()).select_from(model).where(*where)
            return int((await s.execute(query)).scalar_one())

    return asyncio.run(_go())


def _add_membership(user_id: str, status: str) -> None:
    async def _go() -> None:
        async with get_sessionmaker()() as s:
            now = datetime.now(UTC)
            s.add(
                Membership(
                    account_id=user_id,
                    status=status,
                    stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
                    created_at=now,
                    updated_at=now,
                )
            )
            await s.commit()

    asyncio.run(_go())


@pytest.fixture
async def fake() -> AsyncGenerator[Any]:
    import fakeredis.aioredis as fake_aioredis

    client = fake_aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_a_freshly_confirmed_session_deletes_the_account(step_up_app: Path) -> None:  # noqa: F811
    """Turns red if: the route does not delete, keeps the tombstone out, or
    leaves the caller's cookie set."""
    user_id = _register(_client())
    browser = _sign_in_with_link(step_up_app)
    r = _delete(browser)
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": True}
    assert _count(User, User.user_id == user_id) == 0
    assert _count(DeletedAccount, DeletedAccount.email_hash == email_hash(EMAIL)) == 1
    cleared = r.headers.get("set-cookie", "")
    assert "citevyn_session=" in cleared and "Max-Age=0" in cleared


def test_the_security_log_keeps_a_line_linked_to_no_account(step_up_app: Path) -> None:  # noqa: F811
    """Turns red if: a self-service deletion leaves no audit row, or the row
    keeps the account id."""
    from app.models import AuditEvent
    from tests.test_password_step_up import _audit_metadata

    _register(_client())
    browser = _sign_in_with_link(step_up_app)
    assert _delete(browser).status_code == 200
    assert _audit_metadata("account_deleted") == [
        {"event": "account_deleted", "step_up": "magic_link"}
    ]
    assert _count(AuditEvent, AuditEvent.user_id.is_not(None)) == 0


def test_every_session_of_the_account_is_signed_out(step_up_app: Path) -> None:  # noqa: F811
    """Turns red if: another device stays signed in after the deletion."""
    other = _client()
    user_id = _register(other)  # this browser holds a password session
    browser = _sign_in_with_link(step_up_app)
    assert _delete(browser).status_code == 200
    assert _count(AuthSession, AuthSession.user_id == user_id) == 0
    assert _me(other).status_code == 401


def test_the_deletion_is_recorded_outside_the_database(
    step_up_app: Path,  # noqa: F811
    fake: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The button goes through the same delete_account as the command, so a
    restore cannot bring the account back. Turns red if: the route deletes by
    some other path that skips the list."""
    the_list = DeletionList(fake, KEY)
    monkeypatch.setattr(deletion_list, "current", lambda: the_list)
    user_id = _register(_client())
    browser = _sign_in_with_link(step_up_app)
    assert _delete(browser).status_code == 200
    assert asyncio.run(fake.zscore(KEY, user_id)) is not None


# ---------------------------------------------------------------------------
# Refusals: nothing is deleted
# ---------------------------------------------------------------------------


def test_a_password_session_must_confirm_by_email_first(step_up_app: Path) -> None:  # noqa: F811
    """A stolen password (or an unattended signed-in laptop) must not be
    enough. Turns red if: the step-up check is dropped."""
    client = _client()
    user_id = _register(client)
    r = _delete(client)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "verification_required"
    assert _count(User, User.user_id == user_id) == 1


def test_an_expired_confirmation_is_refused(step_up_app: Path) -> None:  # noqa: F811
    """Turns red if: the window is not enforced."""
    user_id = _register(_client())
    browser = _sign_in_with_link(step_up_app)
    _age_stamps(60 * 60)
    assert _delete(browser).status_code == 403
    assert _count(User, User.user_id == user_id) == 1


def test_another_session_s_confirmation_does_not_count(step_up_app: Path) -> None:  # noqa: F811
    """Turns red if: the check looks at any session of the account rather than
    the caller's own."""
    client = _client()
    user_id = _register(client)
    _sign_in_with_link(step_up_app)  # a different browser confirms
    assert _delete(client).status_code == 403
    assert _count(User, User.user_id == user_id) == 1


def test_an_anonymous_caller_is_refused(step_up_app: Path) -> None:  # noqa: F811
    """Turns red if: an anonymous visitor reaches delete_account."""
    r = _delete(_client())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "auth_required"


def test_a_page_showing_another_account_deletes_nothing(
    step_up_app: Path,  # noqa: F811
) -> None:
    """Round-2 finding: a tab still labelled with account A, in a browser that
    another tab has since signed into account B with a link. Turns red if: the
    server deletes the cookie's account without checking it is the one shown."""
    a = _register(_client())
    browser = _sign_in_with_link(step_up_app)  # this browser is now account A too
    r = _delete(browser, user_id="usr_" + "b" * 32)  # the page shows someone else
    assert r.status_code == 422
    assert "different account" in r.json()["error"]["message"]
    assert _count(User, User.user_id == a) == 1
    assert _delete(browser).status_code == 200  # partner: naming it right works


def test_an_anonymous_visitor_with_a_session_cookie_is_refused(
    step_up_app: Path,  # noqa: F811
) -> None:
    """An anonymous visitor holds a real session cookie once they chat.
    Turns red if: the registered-account check is dropped (the step-up check
    would then answer 403 instead of 401)."""
    client = _client()
    r = client.post("/v1/sessions", json={}, headers={"Authorization": DEMO_BEARER})
    assert r.status_code in (200, 201), r.text
    assert client.cookies  # partner: the visitor does have a session
    r = _delete(client, user_id="usr_" + "0" * 32)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "auth_required"


@pytest.mark.parametrize("status", ["active", "past_due", "trialing"])
def test_a_live_subscription_must_be_cancelled_first(step_up_app: Path, status: str) -> None:  # noqa: F811
    """Cancelling at the payment provider is not done here (a live payment
    action). Turns red if: the billing refusal is skipped, or not reported as
    a plain 409."""
    user_id = _register(_client())
    _add_membership(user_id, status)
    browser = _sign_in_with_link(step_up_app)
    r = _delete(browser)
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "already_subscribed"
    assert "cancel" in body["message"].lower()
    assert _count(User, User.user_id == user_id) == 1
    # The audit line is in the deletion's transaction: no deletion, no line.
    from tests.test_password_step_up import _audit_metadata

    assert _audit_metadata("account_deleted") == []


def test_an_ended_subscription_does_not_block(step_up_app: Path) -> None:  # noqa: F811
    """Partner of the refusal. Turns red if: any membership row blocks."""
    user_id = _register(_client())
    _add_membership(user_id, "canceled")
    browser = _sign_in_with_link(step_up_app)
    assert _delete(browser).status_code == 200
    assert _count(User, User.user_id == user_id) == 0


def test_redis_down_refuses_with_a_503_and_changes_nothing(
    step_up_app: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turns red if: the route deletes without recording the deletion outside
    the database, or reports the failure as a 500."""
    from tests.test_deletion_list import Broken

    user_id = _register(_client())
    browser = _sign_in_with_link(step_up_app)
    # The gate also reads Redis on the session lookup; broken from here on,
    # so the 503 may come from either. Both must leave the account intact.
    monkeypatch.setattr(deletion_list, "current", lambda: DeletionList(Broken(), KEY))
    r = _delete(browser)
    assert r.status_code == 503
    assert _count(User, User.user_id == user_id) == 1


class WriteFails:
    """Redis answers reads (the sign-in gate) but refuses the write."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        import redis.exceptions

        if name == "zadd":

            async def _fail(*_a: Any, **_k: Any) -> Any:
                raise redis.exceptions.ConnectionError("write refused")

            return _fail
        return getattr(self._real, name)


def test_a_failed_list_write_is_a_503_and_changes_nothing(
    step_up_app: Path,  # noqa: F811
    fake: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate's read works, the deletion's write fails. Turns red if: the
    route lets DeletionListUnavailable escape as a 500, or deletes anyway."""
    monkeypatch.setattr(deletion_list, "current", lambda: DeletionList(WriteFails(fake), KEY))
    user_id = _register(_client())
    browser = _sign_in_with_link(step_up_app)
    r = _delete(browser)
    assert r.status_code == 503
    assert "Nothing was deleted" in r.json()["error"]["message"]
    assert _count(User, User.user_id == user_id) == 1


def test_the_route_works_with_the_access_model_on(
    step_up_factory: Callable[..., Path],  # noqa: F811
) -> None:
    """Accounts exist with the access model on or off; the button must work in
    both. Turns red if: the route's capability is one a Free account lacks."""
    from tests.conftest import bot_check_headers
    from tests.test_password_step_up import _latest_token

    outbox = step_up_factory(CITEVYN_ACCESS_MODEL_ENABLED="true", CITEVYN_BOT_CHECK_SECRET="b" * 40)
    client = _client()
    r = client.post(
        "/v1/auth/register",
        json={"email": EMAIL, "password": PASSWORD},
        headers={"Authorization": DEMO_BEARER, **bot_check_headers(client)},
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["user_id"]
    browser = _client()
    r = browser.post(
        "/v1/auth/magic-link/request",
        json={"email": EMAIL},
        headers={"Authorization": DEMO_BEARER, **bot_check_headers(browser)},
    )
    assert r.status_code == 202, r.text
    r = browser.post(
        "/v1/auth/magic-link/confirm",
        content=f"token={_latest_token(outbox)}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text
    r = _delete(browser)
    assert r.status_code == 200, r.text
    assert _count(User, User.user_id == user_id) == 0


def test_me_reports_the_confirmation_the_route_needs(step_up_app: Path) -> None:  # noqa: F811
    """The dialog decides between "email me a link" and "delete" from
    ``password_step_up`` on /me. Partner: it is false on a password session,
    true on a link session. Turns red if: the two drift apart."""
    client = _client()
    _register(client)
    assert _me(client).json()["password_step_up"] is False
    browser = _sign_in_with_link(step_up_app)
    assert _me(browser).json()["password_step_up"] is True


@pytest.mark.parametrize(
    ("environment", "name", "secure"),
    [
        ("production", "__Host-citevyn_session", True),
        ("local", "citevyn_session", False),
    ],
)
def test_the_cookie_is_cleared_with_the_attributes_it_was_set_with(
    environment: str, name: str, secure: bool
) -> None:
    """Review finding: a browser ignores a ``__Host-`` Set-Cookie without
    ``Secure``, so in production the dead cookie stayed. Turns red if:
    clearing drops Secure in production (logout and delete share it)."""
    from types import SimpleNamespace

    from fastapi import Response

    from app.core.auth_sessions import clear_session_cookie

    response = Response()
    clear_session_cookie(response, SimpleNamespace(environment=environment))  # type: ignore[arg-type]
    header = response.headers["set-cookie"]
    assert header.startswith(f"{name}=")
    assert "Max-Age=0" in header and "Path=/" in header
    assert ("Secure" in header) is secure
    assert "HttpOnly" in header


def test_a_failed_audit_write_deletes_nothing(
    step_up_app: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-2 finding: the audit line and the deletion share one commit.
    Turns red if: the audit row is written after the deletion commits (the
    account would be gone with no audit line and a 500)."""
    user_id = _register(_client())
    browser = _sign_in_with_link(step_up_app)

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("audit store down")

    monkeypatch.setattr("app.api.routes.auth.record_audit_event", boom)
    with pytest.raises(RuntimeError):  # the test client re-raises; production answers 500
        _delete(browser)
    assert _count(User, User.user_id == user_id) == 1


@pytest.mark.parametrize("route", ["logout", "delete"])
def test_logout_and_delete_both_clear_through_the_shared_helper(
    step_up_app: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """Round-2 finding: the production-cookie test calls the helper directly,
    so a route going back to a bare delete_cookie stayed green. Turns red if:
    either route stops clearing through clear_session_cookie."""
    from app.core import auth_sessions

    calls: list[int] = []
    real = auth_sessions.clear_session_cookie

    def spy(*a: Any, **k: Any) -> None:
        calls.append(1)
        real(*a, **k)

    monkeypatch.setattr(auth_sessions, "clear_session_cookie", spy)
    monkeypatch.setattr("app.api.routes.auth.clear_session_cookie", spy)
    _register(_client())
    browser = _sign_in_with_link(step_up_app)
    if route == "logout":
        r = browser.post("/v1/auth/logout", headers={"Authorization": DEMO_BEARER})
        assert r.status_code in (200, 204), r.text
    else:
        assert _delete(browser).status_code == 200
    assert calls == [1]


def test_an_account_deleted_meanwhile_is_a_401(
    step_up_app: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another tab, or the operator, deleted it between the session lookup and
    the deletion. Turns red if: that race answers a 500 instead of the 401
    the client treats as "signed out"."""
    from app.services import account_deletion

    _register(_client())
    browser = _sign_in_with_link(step_up_app)

    async def gone(*_a: Any, **_k: Any) -> Any:
        raise account_deletion.AccountNotFound("usr_x")

    monkeypatch.setattr(account_deletion, "delete_account", gone)
    r = _delete(browser)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "auth_required"
