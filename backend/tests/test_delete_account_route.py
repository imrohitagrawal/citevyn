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


def _delete(client: Any) -> Any:
    return client.post("/v1/auth/me/delete", headers={"Authorization": DEMO_BEARER})


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
    assert _login(_client(), PASSWORD).status_code == 200
