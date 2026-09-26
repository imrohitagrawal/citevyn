"""Shareable answers through the real routes (ADR-0005 §4, Phase 6C).

"A frozen copy of the answer, its citations and the crawl date; revocable."
Pro makes a share; any account lists and revokes its own; anyone with the link
reads it at ``/s/<share_id>``. All of it is 404 while the access model is off.

Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import shares as shares_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import Membership, Message, SharedAnswer
from tests.test_answer_allowance import (  # noqa: F401 (env is a fixture)
    DEMO,
    REFUSED,
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


@pytest.fixture
def pro(env: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str]]:
    """A signed-in Pro account with the access model on. A fixture, so the app
    shuts down even when a test fails."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _make_pro(user)
        yield client, user


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
    env: pytest.MonkeyPatch, pro: tuple[TestClient, str]
) -> None:
    """Turns red if: the public page skips the access-model check."""
    client, _ = pro
    sid, mid = _ask(client)
    url = _share(client, sid, mid).json()["url"]
    assert client.get(url).status_code == 200
    env.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "false")
    get_settings.cache_clear()
    assert TestClient(create_app()).get(url).status_code == 404


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


def test_pro_shares_an_answer_and_a_stranger_can_read_it(
    env: pytest.MonkeyPatch, pro: tuple[TestClient, str]
) -> None:
    """Turns red if: the share is not stored, the link does not open for a
    signed-out stranger, or the page misses the question, answer or sources."""
    client, user = pro
    sid, mid = _ask(client)
    res = _share(client, sid, mid)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["url"] == f"/s/{body['share_id']}"
    assert len(body["share_id"]) == 32
    [row] = _rows()
    assert (row.user_id, str(row.message_id), row.question) == (user, mid, QUESTION)
    # An exact copy: not a summary, not the normalised query, no citation field lost.
    history = client.get(f"/v1/sessions/{sid}", headers=DEMO).json()["messages"]
    original = next(m for m in history if m["message_id"] == mid)
    assert row.answer == original["content"]
    assert row.citations == original["citations"]
    dates = [c["docs_as_of"] for c in original["citations"] if c.get("docs_as_of")]
    assert dates and row.docs_as_of == min(dates)  # the oldest: the bound for every source

    page = TestClient(create_app()).get(body["url"])  # a separate cookie jar
    assert page.status_code == 200
    assert QUESTION in page.text
    assert row.citations[0]["title"] in page.text
    assert 'id="source-' in page.text


def test_the_page_is_not_indexed_or_cached(
    env: pytest.MonkeyPatch, pro: tuple[TestClient, str]
) -> None:
    """no-store makes a revoke take effect at once; noindex keeps shared pages
    out of search. Turns red if: either header is dropped."""
    client, _ = pro
    sid, mid = _ask(client)
    page = client.get(_share(client, sid, mid).json()["url"])
    assert page.headers["x-robots-tag"] == "noindex"
    assert page.headers["cache-control"] == "no-store"


def test_the_copy_is_frozen(env: pytest.MonkeyPatch, pro: tuple[TestClient, str]) -> None:
    """Changing, then deleting, the original answer does not change the page.
    Turns red if: the page reads the live message instead of the copy, or the
    message_id foreign key is not SET NULL (the delete would fail or cascade)."""
    client, _ = pro
    sid, mid = _ask(client)
    url = _share(client, sid, mid).json()["url"]
    before = client.get(url).text

    async def _edit_then_delete() -> None:
        async with get_sessionmaker()() as s:
            msg = await s.get(Message, uuid.UUID(mid))
            assert msg is not None
            msg.content = "EDITED AFTER SHARING"
            msg.citations = []
            await s.commit()
            assert client.get(url).text == before  # still the copy
            await s.delete(msg)
            await s.commit()

    asyncio.run(_edit_then_delete())
    after = client.get(url)
    assert after.status_code == 200
    assert after.text == before
    [row] = _rows()
    assert row.message_id is None


def test_a_refusal_cannot_be_shared(env: pytest.MonkeyPatch, pro: tuple[TestClient, str]) -> None:
    """A refusal has no sources, so there is nothing worth a public page.
    Turns red if: the "has citations" half of the check is dropped."""
    client, _ = pro
    sid = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO).json()["session_id"]
    res = client.post(f"/v1/sessions/{sid}/messages", json={"message": REFUSED}, headers=DEMO)
    assert res.status_code == 200 and res.json()["citations"] == []
    assert _share(client, sid, str(res.json()["message_id"])).status_code == 422
    assert _rows() == []


def test_each_answer_is_shared_with_its_own_question(
    env: pytest.MonkeyPatch, pro: tuple[TestClient, str]
) -> None:
    """Two turns in one session, and a question in another session between them.
    Turns red if: the question lookup sorts the wrong way, ignores the answer's
    time, or ignores the session."""
    client, _ = pro
    second_q = "How do I set Claude Code permission modes?"
    sid = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO).json()["session_id"]
    first = client.post(f"/v1/sessions/{sid}/messages", json={"message": QUESTION}, headers=DEMO)
    other_sid, _ = _ask(client, "How do I install Claude Code?")  # another session
    second = client.post(f"/v1/sessions/{sid}/messages", json={"message": second_q}, headers=DEMO)
    assert first.json()["citations"] and second.json()["citations"]

    async def _other_session_question_at_the_same_moment() -> None:
        # Stamped exactly at the first answer's time, in ANOTHER session: only
        # the session filter keeps it off the first answer's page.
        async with get_sessionmaker()() as s:
            answer = await s.get(Message, uuid.UUID(str(first.json()["message_id"])))
            assert answer is not None
            s.add(
                Message(
                    session_id=uuid.UUID(other_sid),
                    role="user",
                    content="A QUESTION FROM ANOTHER SESSION",
                    created_at=answer.created_at,
                )
            )
            await s.commit()

    asyncio.run(_other_session_question_at_the_same_moment())
    _share(client, sid, str(second.json()["message_id"]))
    _share(client, sid, str(first.json()["message_id"]))
    by_message = {str(r.message_id): r.question for r in _rows()}
    assert by_message == {
        str(first.json()["message_id"]): QUESTION,
        str(second.json()["message_id"]): second_q,
    }


def test_sharing_the_same_answer_twice_gives_the_same_link(
    env: pytest.MonkeyPatch, pro: tuple[TestClient, str]
) -> None:
    """Turns red if: a second share makes a second row (or a second link)."""
    client, _ = pro
    sid, mid = _ask(client)
    first = _share(client, sid, mid)
    second = _share(client, sid, mid)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["share_id"] == second.json()["share_id"]
    assert len(_rows()) == 1


def test_only_a_cited_answer_can_be_shared(
    env: pytest.MonkeyPatch, pro: tuple[TestClient, str]
) -> None:
    """Turns red if: a question (user message) can be shared."""
    client, _ = pro
    sid, _mid = _ask(client)
    history = client.get(f"/v1/sessions/{sid}", headers=DEMO).json()["messages"]
    question_id = next(m["message_id"] for m in history if m["role"] == "user")
    assert _share(client, sid, question_id).status_code == 422
    assert _rows() == []


def test_someone_elses_answer_cannot_be_shared(
    env: pytest.MonkeyPatch, pro: tuple[TestClient, str]
) -> None:
    """Turns red if: the session-ownership check is skipped."""
    client, _ = pro
    sid, mid = _ask(client)
    with TestClient(create_app()) as other:
        stranger = _signed_in(other, "b@example.com")
        _make_pro_sub(stranger)
        assert _share(other, sid, mid).status_code == 404
    assert _rows() == []


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
    env: pytest.MonkeyPatch, pro: tuple[TestClient, str]
) -> None:
    """Turns red if: the cap is not enforced (the check is dropped or off by one)."""
    env.setattr(shares_module, "MAX_LIVE_SHARES", 1)
    client, _ = pro
    sid, mid = _ask(client)
    sid2, mid2 = _ask(client, "How do I set Claude Code permission modes?")
    assert _share(client, sid, mid).status_code == 201
    res = _share(client, sid2, mid2)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "too_many_shares"


# ---------------------------------------------------------------------------
# Listing and revoking
# ---------------------------------------------------------------------------


def test_revoking_kills_the_link(env: pytest.MonkeyPatch, pro: tuple[TestClient, str]) -> None:
    """Turns red if: a revoked share still opens, or still lists."""
    client, _ = pro
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


def test_a_lapsed_account_can_still_list_and_revoke(
    env: pytest.MonkeyPatch, pro: tuple[TestClient, str]
) -> None:
    """Turns red if: list or revoke require Pro (links would outlive their owner's control)."""
    client, user = pro
    sid, mid = _ask(client)
    share = _share(client, sid, mid).json()
    _lapse(user)
    assert _share(client, sid, mid).status_code == 403  # making needs Pro again
    assert len(client.get("/v1/me/shares", headers=DEMO).json()["shares"]) == 1
    assert client.delete(f"/v1/me/shares/{share['share_id']}", headers=DEMO).status_code == 204


def test_a_stranger_cannot_see_or_revoke_my_shares(
    env: pytest.MonkeyPatch, pro: tuple[TestClient, str]
) -> None:
    """Turns red if: list or revoke skip the owner check."""
    client, _ = pro
    sid, mid = _ask(client)
    share = _share(client, sid, mid).json()
    with TestClient(create_app()) as other:
        _signed_in(other, "b@example.com")
        assert other.get("/v1/me/shares", headers=DEMO).json()["shares"] == []
        assert other.delete(f"/v1/me/shares/{share['share_id']}", headers=DEMO).status_code == 404
    assert client.get(share["url"]).status_code == 200


@pytest.mark.parametrize("share_id", ["x", "A" * 32, "a" * 31, "g" * 32, "a" * 33])
def test_a_malformed_link_is_404(env: pytest.MonkeyPatch, share_id: str) -> None:
    """Turns red if: a malformed id reaches the page (or errors instead of 404)."""
    _on(env)
    with TestClient(create_app()) as client:
        assert client.get(f"/s/{share_id}").status_code == 404


def test_the_list_is_newest_first_and_cuts_long_questions(
    env: pytest.MonkeyPatch, pro: tuple[TestClient, str]
) -> None:
    """Turns red if: the list is oldest first, or returns a question longer
    than 200 characters (the page shows it whole)."""
    client, _ = pro
    long_q = QUESTION + " " + "Please explain in detail. " * 12
    sid1, mid1 = _ask(client)
    sid2, mid2 = _ask(client, long_q)
    one = _share(client, sid1, mid1).json()
    two = _share(client, sid2, mid2).json()
    listed = client.get("/v1/me/shares", headers=DEMO).json()["shares"]
    assert [s["share_id"] for s in listed] == [two["share_id"], one["share_id"]]
    assert listed[0]["question"] == long_q[:200]
    assert " ".join(long_q.split()) in client.get(two["url"]).text
