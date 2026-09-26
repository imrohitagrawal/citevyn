"""Shareable answers through the real routes (ADR-0005 §4, Phase 6C).

"A frozen copy of the answer, its citations and the crawl date; revocable."
Pro makes a share; any account lists and revokes its own; anyone with the link
reads it at ``/s/<share_id>``. All of it is 404 while the access model is off.

Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import shares as shares_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import Membership, SharedAnswer
from tests.test_answer_allowance import (  # noqa: F401 (env is a fixture)
    DEMO,
    _make_pro,
    _on,
    _signed_in,
    app_env,
)

QUESTION = "How do I configure Claude Code permissions?"


@pytest.fixture
def env(app_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:  # noqa: F811 (the imported fixture)
    """The answer-allowance test's app: a seeded catalog on a fresh SQLite file."""
    return app_env


def _ask(client: TestClient, question: str = QUESTION) -> tuple[str, str]:
    sid = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO).json()["session_id"]
    res = client.post(f"/v1/sessions/{sid}/messages", json={"message": question}, headers=DEMO)
    assert res.status_code == 200, res.text
    assert res.json()["citations"], "the stub must give a cited answer for this question"
    return sid, str(res.json()["message_id"])


def _share(client: TestClient, sid: str, mid: str) -> Any:
    return client.post(f"/v1/sessions/{sid}/messages/{mid}/share", headers=DEMO)


def _rows() -> list[SharedAnswer]:
    async def _run() -> list[SharedAnswer]:
        async with get_sessionmaker()() as s:
            return list((await s.execute(select(SharedAnswer))).scalars())

    return asyncio.run(_run())


def _lapse(user_id: str) -> None:
    async def _run() -> None:
        async with get_sessionmaker()() as s:
            for m in (await s.execute(select(Membership))).scalars():
                if m.account_id == user_id:
                    m.status = "canceled"
            await s.commit()

    asyncio.run(_run())


def _pro_client(env: pytest.MonkeyPatch) -> tuple[TestClient, str]:
    _on(env)
    client = TestClient(create_app())
    client.__enter__()
    user = _signed_in(client)
    _make_pro(user)
    return client, user


# ---------------------------------------------------------------------------
# Off, and not Pro
# ---------------------------------------------------------------------------


def test_with_the_setting_off_there_is_no_sharing(env: pytest.MonkeyPatch) -> None:
    """Turns red if: any share route works with the access model off."""
    with TestClient(create_app()) as client:
        sid, mid = _ask(client)
        assert _share(client, sid, mid).status_code == 404
        assert client.get("/v1/me/shares", headers=DEMO).status_code == 404
        assert client.delete(f"/v1/me/shares/{'a' * 32}", headers=DEMO).status_code == 404
        assert client.get(f"/s/{'a' * 32}").status_code == 404
    assert _rows() == []


def test_a_free_account_cannot_share(env: pytest.MonkeyPatch) -> None:
    """Sharing is Pro. Turns red if: share_answer is granted to Free."""
    _on(env)
    with TestClient(create_app()) as client:
        _signed_in(client)
        sid, mid = _ask(client)
        res = _share(client, sid, mid)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "plan_required"
    assert _rows() == []


def test_a_page_made_while_on_is_gone_when_the_setting_goes_off(
    env: pytest.MonkeyPatch,
) -> None:
    """Turns red if: the public page skips the access-model check."""
    client, _ = _pro_client(env)
    sid, mid = _ask(client)
    url = _share(client, sid, mid).json()["url"]
    assert client.get(url).status_code == 200
    env.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "false")
    get_settings.cache_clear()
    assert TestClient(create_app()).get(url).status_code == 404
    client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


def test_pro_shares_an_answer_and_a_stranger_can_read_it(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the share is not stored, the link does not open for a
    signed-out stranger, or the page misses the question, answer or sources."""
    client, user = _pro_client(env)
    sid, mid = _ask(client)
    res = _share(client, sid, mid)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["url"] == f"/s/{body['share_id']}"
    assert len(body["share_id"]) == 32
    [row] = _rows()
    assert (row.user_id, str(row.message_id), row.question) == (user, mid, QUESTION)
    assert row.citations and row.answer

    page = TestClient(create_app()).get(body["url"])  # a separate cookie jar
    assert page.status_code == 200
    assert QUESTION in page.text
    assert row.citations[0]["title"] in page.text
    assert 'id="source-' in page.text
    client.__exit__(None, None, None)


def test_the_page_is_not_indexed_or_cached(env: pytest.MonkeyPatch) -> None:
    """no-store makes a revoke take effect at once; noindex keeps shared pages
    out of search. Turns red if: either header is dropped."""
    client, _ = _pro_client(env)
    sid, mid = _ask(client)
    page = client.get(_share(client, sid, mid).json()["url"])
    assert page.headers["x-robots-tag"] == "noindex"
    assert page.headers["cache-control"] == "no-store"
    client.__exit__(None, None, None)


def test_the_copy_is_frozen(env: pytest.MonkeyPatch) -> None:
    """Deleting the conversation does not change or remove the shared page.
    Turns red if: the page reads the live message instead of the copy."""
    client, _ = _pro_client(env)
    sid, mid = _ask(client)
    url = _share(client, sid, mid).json()["url"]
    before = client.get(url).text
    assert client.delete(f"/v1/sessions/{sid}", headers=DEMO).status_code in (200, 204)
    after = client.get(url)
    assert after.status_code == 200
    assert after.text == before
    client.__exit__(None, None, None)


def test_sharing_the_same_answer_twice_gives_the_same_link(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a second share makes a second row (or a second link)."""
    client, _ = _pro_client(env)
    sid, mid = _ask(client)
    first = _share(client, sid, mid)
    second = _share(client, sid, mid)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["share_id"] == second.json()["share_id"]
    assert len(_rows()) == 1
    client.__exit__(None, None, None)


def test_only_a_cited_answer_can_be_shared(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a question (user message) can be shared."""
    client, _ = _pro_client(env)
    sid, _mid = _ask(client)
    history = client.get(f"/v1/sessions/{sid}", headers=DEMO).json()["messages"]
    question_id = next(m["message_id"] for m in history if m["role"] == "user")
    assert _share(client, sid, question_id).status_code == 422
    assert _rows() == []
    client.__exit__(None, None, None)


def test_someone_elses_answer_cannot_be_shared(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the session-ownership check is skipped."""
    client, _ = _pro_client(env)
    sid, mid = _ask(client)
    with TestClient(create_app()) as other:
        stranger = _signed_in(other, "b@example.com")
        _make_pro_sub(stranger)
        assert _share(other, sid, mid).status_code == 404
    assert _rows() == []
    client.__exit__(None, None, None)


def _make_pro_sub(user_id: str) -> None:
    from datetime import UTC, datetime

    async def _run() -> None:
        async with get_sessionmaker()() as s:
            s.add(
                Membership(
                    account_id=user_id,
                    stripe_subscription_id="sub_2",
                    plan="pro",
                    status="active",
                    cancel_at_period_end=False,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            await s.commit()

    asyncio.run(_run())


def test_there_is_a_cap_on_live_shares(
    env: pytest.MonkeyPatch,
) -> None:
    """Turns red if: the cap is not enforced (the check is dropped or off by one)."""
    env.setattr(shares_module, "MAX_LIVE_SHARES", 1)
    client, _ = _pro_client(env)
    sid, mid = _ask(client)
    sid2, mid2 = _ask(client, "How do I set Claude Code permission modes?")
    assert _share(client, sid, mid).status_code == 201
    res = _share(client, sid2, mid2)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "too_many_shares"
    client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Listing and revoking
# ---------------------------------------------------------------------------


def test_revoking_kills_the_link(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a revoked share still opens, or still lists."""
    client, _ = _pro_client(env)
    sid, mid = _ask(client)
    share = _share(client, sid, mid).json()
    listed = client.get("/v1/me/shares", headers=DEMO).json()["shares"]
    assert [s["share_id"] for s in listed] == [share["share_id"]]
    assert client.delete(f"/v1/me/shares/{share['share_id']}", headers=DEMO).status_code == 204
    assert client.get(share["url"]).status_code == 404
    assert client.get("/v1/me/shares", headers=DEMO).json()["shares"] == []
    # Sharing again after a revoke makes a NEW link; the old one stays dead.
    again = _share(client, sid, mid).json()
    assert again["share_id"] != share["share_id"]
    assert client.get(share["url"]).status_code == 404
    client.__exit__(None, None, None)


def test_a_lapsed_account_can_still_list_and_revoke(env: pytest.MonkeyPatch) -> None:
    """Turns red if: list or revoke require Pro (links would outlive their owner's control)."""
    client, user = _pro_client(env)
    sid, mid = _ask(client)
    share = _share(client, sid, mid).json()
    _lapse(user)
    assert _share(client, sid, mid).status_code == 403  # making needs Pro again
    assert len(client.get("/v1/me/shares", headers=DEMO).json()["shares"]) == 1
    assert client.delete(f"/v1/me/shares/{share['share_id']}", headers=DEMO).status_code == 204
    client.__exit__(None, None, None)


def test_a_stranger_cannot_see_or_revoke_my_shares(env: pytest.MonkeyPatch) -> None:
    """Turns red if: list or revoke skip the owner check."""
    client, _ = _pro_client(env)
    sid, mid = _ask(client)
    share = _share(client, sid, mid).json()
    with TestClient(create_app()) as other:
        _signed_in(other, "b@example.com")
        assert other.get("/v1/me/shares", headers=DEMO).json()["shares"] == []
        assert other.delete(f"/v1/me/shares/{share['share_id']}", headers=DEMO).status_code == 404
    assert client.get(share["url"]).status_code == 200
    client.__exit__(None, None, None)


@pytest.mark.parametrize("share_id", ["x", "A" * 32, "a" * 31, "g" * 32, "a" * 33])
def test_a_malformed_link_is_404(env: pytest.MonkeyPatch, share_id: str) -> None:
    """Turns red if: a malformed id reaches the page (or errors instead of 404)."""
    _on(env)
    with TestClient(create_app()) as client:
        assert client.get(f"/s/{share_id}").status_code == 404
