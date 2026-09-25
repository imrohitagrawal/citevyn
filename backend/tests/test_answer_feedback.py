"""Feedback, "report wrong answer" and "request this source" (ADR-0005 §6).

Trust must-haves, so NOT behind the access setting. Three routes for users, two
for operators:

* ``PUT /v1/sessions/{sid}/messages/{mid}/feedback`` — thumbs up/down on one of
  the caller's own ANSWERS; a thumbs-down may carry a reason ("report wrong
  answer") and a comment. One row per (answer, user): a second vote replaces it.
* ``POST /v1/source-requests`` — "request this source", usually from a refusal;
  feeds the gap log.
* ``GET /v1/admin/feedback`` and ``GET /v1/admin/source_requests`` — the
  operator's view (admin key only).

Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import AnswerFeedback, SourceRequest
from tests.test_messages_routes import in_memory_client, seeded_app  # noqa: F401 (fixtures)

DEMO = {"Authorization": "Bearer local-demo-key"}
ADMIN = {"X-Admin-API-Key": "local-admin-key"}


@pytest.fixture(autouse=True)
def _fresh_rate_limiter() -> None:
    """Each test asks several questions; the per-visitor limiter is process-wide."""
    import app.core.rate_limit as rate_limit

    rate_limit.reset_limiter()


def _answer(client: TestClient) -> tuple[str, str, str]:
    """Ask one question; return (session_id, answer_message_id, question_message_id)."""
    sid = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO).json()["session_id"]
    res = client.post(
        f"/v1/sessions/{sid}/messages",
        json={"message": "How do I configure Claude Code permissions?"},
        headers=DEMO,
    )
    assert res.status_code == 200
    answer_id = res.json()["message_id"]
    history = client.get(f"/v1/sessions/{sid}", headers=DEMO).json()["messages"]
    question_id = next(m["message_id"] for m in history if m["role"] == "user")
    return sid, answer_id, question_id


def _rows(model: Any) -> list[Any]:
    async def _run() -> list[Any]:
        async with get_sessionmaker()() as s:
            return list((await s.execute(select(model))).scalars().all())

    return asyncio.run(_run())


def _put(client: TestClient, sid: str, mid: str, body: dict[str, object]) -> Any:
    return client.put(f"/v1/sessions/{sid}/messages/{mid}/feedback", json=body, headers=DEMO)


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


def test_a_thumbs_up_is_recorded_against_the_answer(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: the route does not store the vote, or stores it against the
    wrong message or user."""
    sid, answer_id, _ = _answer(seeded_app)
    res = _put(seeded_app, sid, answer_id, {"rating": "up"})
    assert res.status_code == 200
    assert res.json()["rating"] == "up"
    [row] = _rows(AnswerFeedback)
    assert str(row.message_id) == answer_id
    assert row.rating == "up"
    assert row.reason is None
    assert row.user_id.startswith("anon_")


def test_a_second_vote_replaces_the_first(seeded_app: TestClient) -> None:  # noqa: F811
    """ "Report wrong answer" after a thumbs up is a change of mind, not a second row.
    Turns red if: votes accumulate, or the reason/comment is dropped."""
    sid, answer_id, _ = _answer(seeded_app)
    _put(seeded_app, sid, answer_id, {"rating": "up"})
    res = _put(
        seeded_app,
        sid,
        answer_id,
        {"rating": "down", "reason": "wrong", "comment": "The flag is --permission-mode."},
    )
    assert res.status_code == 200
    rows = _rows(AnswerFeedback)
    assert len(rows) == 1
    assert (rows[0].rating, rows[0].reason, rows[0].comment) == (
        "down",
        "wrong",
        "The flag is --permission-mode.",
    )


def test_only_answers_can_be_rated(seeded_app: TestClient) -> None:  # noqa: F811
    """The user's own question is not something to rate.
    Turns red if: the route accepts a user-role message."""
    sid, _, question_id = _answer(seeded_app)
    res = _put(seeded_app, sid, question_id, {"rating": "down"})
    assert res.status_code == 422
    assert _rows(AnswerFeedback) == []


def test_someone_elses_answer_is_a_404(seeded_app: TestClient) -> None:  # noqa: F811
    """Ownership: another visitor's session looks like no session at all.
    Turns red if: the route skips the session-ownership check."""
    sid, answer_id, _ = _answer(seeded_app)
    stranger = TestClient(create_app())  # a separate cookie jar: another visitor
    res = stranger.put(
        f"/v1/sessions/{sid}/messages/{answer_id}/feedback", json={"rating": "up"}, headers=DEMO
    )
    assert res.status_code == 404
    assert _rows(AnswerFeedback) == []


def test_a_message_from_another_session_of_mine_is_a_404(seeded_app: TestClient) -> None:  # noqa: F811
    """The message must belong to the session in the path.
    Turns red if: the message lookup ignores the session id."""
    sid_a, answer_a, _ = _answer(seeded_app)
    sid_b, _, _ = _answer(seeded_app)
    res = _put(seeded_app, sid_b, answer_a, {"rating": "up"})
    assert res.status_code == 404
    assert sid_a != sid_b


@pytest.mark.parametrize(
    "body",
    [
        {"rating": "sideways"},
        {"rating": "up", "reason": "wrong"},  # a reason only explains a thumbs DOWN
        {"rating": "down", "reason": "because"},
        {"rating": "down", "comment": "x" * 1001},
        {},
    ],
)
def test_bad_feedback_is_refused(seeded_app: TestClient, body: dict[str, object]) -> None:  # noqa: F811
    """Turns red if: any of these validation rules is dropped."""
    sid, answer_id, _ = _answer(seeded_app)
    assert _put(seeded_app, sid, answer_id, body).status_code == 422
    assert _rows(AnswerFeedback) == []


# ---------------------------------------------------------------------------
# Source requests (the gap log)
# ---------------------------------------------------------------------------


def test_a_source_request_is_recorded(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: the request is not stored, or loses a field."""
    sid, answer_id, _ = _answer(seeded_app)
    res = seeded_app.post(
        "/v1/source-requests",
        json={
            "question": "How do I use Claude Code with Bedrock?",
            "message_id": answer_id,
            "requested_url": "https://docs.anthropic.com/en/docs/claude-code/bedrock",
            "note": "Refused, but this page exists.",
        },
        headers=DEMO,
    )
    assert res.status_code == 202
    [row] = _rows(SourceRequest)
    assert row.question == "How do I use Claude Code with Bedrock?"
    assert str(row.message_id) == answer_id
    assert row.requested_url == "https://docs.anthropic.com/en/docs/claude-code/bedrock"
    assert row.status == "open"
    assert sid  # the session exists; the request itself needs no session id


def test_a_source_request_needs_no_message(seeded_app: TestClient) -> None:  # noqa: F811
    """Partner: a request typed without a linked answer is fine too."""
    _answer(seeded_app)  # mints the visitor
    res = seeded_app.post(
        "/v1/source-requests", json={"question": "Gemini Live API?"}, headers=DEMO
    )
    assert res.status_code == 202
    [row] = _rows(SourceRequest)
    assert row.message_id is None
    assert row.user_id.startswith("anon_")


def test_a_source_request_cannot_point_at_someone_elses_message(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: the linked message's ownership is not checked (it would let a
    caller attach notes to other people's conversations)."""
    _, answer_id, _ = _answer(seeded_app)
    stranger = TestClient(create_app())
    stranger.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO)
    res = stranger.post(
        "/v1/source-requests", json={"question": "q", "message_id": answer_id}, headers=DEMO
    )
    assert res.status_code == 404
    assert _rows(SourceRequest) == []


@pytest.mark.parametrize(
    "body",
    [
        {"question": ""},
        {"question": "x" * 2001},
        {"question": "q", "requested_url": "javascript:alert(1)"},
        {"question": "q", "requested_url": "ftp://example.com/x"},
        {"question": "q", "requested_url": "https://user:pass@evil.example/"},
        {"question": "q", "requested_url": "https://evil.com/\n<script>alert(1)</script>"},
        {"question": "q", "requested_url": "https://evil.com/a\tb"},
        {"question": "q", "requested_url": "https://\u202eevil.com"},
        {"question": "q", "requested_url": "https://evil.com/a b"},
        {"question": "q", "note": "x" * 1001},
        {"question": "q", "message_id": "not-a-uuid"},
    ],
)
def test_bad_source_requests_are_refused(seeded_app: TestClient, body: dict[str, object]) -> None:  # noqa: F811
    """Turns red if: any of these validation rules is dropped (a javascript: URL
    would reach the operator's admin view)."""
    _answer(seeded_app)
    assert seeded_app.post("/v1/source-requests", json=body, headers=DEMO).status_code == 422
    assert _rows(SourceRequest) == []


@pytest.mark.parametrize(
    ("route", "field", "limit"),
    [
        ("feedback", "comment", 1000),
        ("source", "note", 1000),
        ("source", "question", 2000),
        ("source", "requested_url", 2048),
    ],
)
def test_each_limit_is_the_one_in_force_in_both_directions(
    seeded_app: TestClient,  # noqa: F811
    route: str,
    field: str,
    limit: int,
) -> None:
    """TEST_STRATEGY §8b: the last accepted length AND the first rejected one.
    A rejection alone proves only that SOMETHING is refused. Counted in code
    points: an emoji is one character here, as it is in the browser.
    Turns red if: a limit moves, or is off by one."""
    sid, answer_id, _ = _answer(seeded_app)

    def body(n: int) -> dict[str, object]:
        if field == "requested_url":
            value: str = "https://d/" + "a" * (n - len("https://d/"))
        else:
            value = "😀" * n
        if route == "feedback":
            return {"rating": "down", "reason": "other", field: value}
        return {"question": "q", field: value} if field != "question" else {field: value}

    def send(n: int) -> int:
        if route == "feedback":
            return _put(seeded_app, sid, answer_id, body(n)).status_code
        return seeded_app.post("/v1/source-requests", json=body(n), headers=DEMO).status_code

    ok = 200 if route == "feedback" else 202
    assert send(limit) == ok
    assert send(limit + 1) == 422


# ---------------------------------------------------------------------------
# The operator's view
# ---------------------------------------------------------------------------


def test_the_operator_sees_feedback_with_the_answer_it_is_about(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: the admin list omits the vote, its reason, or the answer text
    an operator needs to judge it."""
    sid, answer_id, _ = _answer(seeded_app)
    _put(seeded_app, sid, answer_id, {"rating": "down", "reason": "outdated"})
    res = seeded_app.get("/v1/admin/feedback", headers=ADMIN)
    assert res.status_code == 200
    assert res.json()["count"] == 1
    [item] = res.json()["feedback"]
    assert item["message_id"] == answer_id
    assert (item["rating"], item["reason"]) == ("down", "outdated")
    assert item["answer_excerpt"]  # the answer text, trimmed


def test_the_operator_sees_the_gap_log_newest_first(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: the gap log is unordered or missing a request."""
    _answer(seeded_app)
    for q in ("first", "second"):
        seeded_app.post("/v1/source-requests", json={"question": q}, headers=DEMO)
    res = seeded_app.get("/v1/admin/source_requests", headers=ADMIN)
    assert res.status_code == 200
    assert [r["question"] for r in res.json()["source_requests"]] == ["second", "first"]


def test_the_operator_views_need_the_admin_key(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: either list is reachable with only the public credentials."""
    for path in ("/v1/admin/feedback", "/v1/admin/source_requests"):
        assert seeded_app.get(path, headers=DEMO).status_code == 401


def test_with_the_access_model_on_anonymous_callers_cannot_leave_feedback(
    seeded_app: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``feedback`` is an account capability (docs/ACCESS_POLICY.md).
    Turns red if: the routes are labelled with a capability anonymous callers have."""
    sid, answer_id, _ = _answer(seeded_app)
    monkeypatch.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "true")
    get_settings.cache_clear()
    try:
        vote = _put(seeded_app, sid, answer_id, {"rating": "up"})
        req = seeded_app.post("/v1/source-requests", json={"question": "q"}, headers=DEMO)
    finally:
        monkeypatch.delenv("CITEVYN_ACCESS_MODEL_ENABLED")
        get_settings.cache_clear()
    assert (vote.status_code, req.status_code) == (401, 401)
    # The refusal must come from the capability check, not from the bearer check,
    # which also answers 401 auth_required.
    for res in (vote, req):
        assert res.json()["error"]["details"] == {"capability": "feedback", "required_tier": "free"}
    assert uuid.UUID(answer_id)


def test_a_source_request_on_an_expired_sessions_message_is_a_404(seeded_app: TestClient) -> None:  # noqa: F811
    """The linked-message check is its own copy of the owner-and-not-expired rule.
    Turns red if: it stops checking expiry."""
    sid, answer_id, _ = _answer(seeded_app)
    assert seeded_app.delete(f"/v1/sessions/{sid}", headers=DEMO).status_code == 204
    res = seeded_app.post(
        "/v1/source-requests", json={"question": "q", "message_id": answer_id}, headers=DEMO
    )
    assert res.status_code == 404


def test_a_link_padded_with_spaces_is_measured_after_trimming(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: the length limit counts surrounding whitespace the stored
    value will not keep."""
    _answer(seeded_app)
    url = "https://d/" + "a" * (2048 - len("https://d/"))
    res = seeded_app.post(
        "/v1/source-requests", json={"question": "q", "requested_url": f"  {url}  "}, headers=DEMO
    )
    assert res.status_code == 202
    assert _rows(SourceRequest)[0].requested_url == url


def test_a_racing_second_vote_updates_instead_of_failing(
    seeded_app: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two PUTs for one answer can both see "no row yet" on Postgres (READ
    COMMITTED); the second INSERT then hits the unique constraint. Simulated by
    hiding the existing row from the first lookup. Turns red if: the route lets
    the IntegrityError become a 500 instead of updating the row."""
    from app.api.routes import feedback as fb

    sid, answer_id, _ = _answer(seeded_app)
    assert _put(seeded_app, sid, answer_id, {"rating": "up"}).status_code == 200
    real = fb._find_vote
    calls = {"n": 0}

    async def racing(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(*args, **kwargs)

    monkeypatch.setattr(fb, "_find_vote", racing)
    res = _put(seeded_app, sid, answer_id, {"rating": "down", "reason": "wrong"})
    assert res.status_code == 200
    [row] = _rows(AnswerFeedback)
    assert (row.rating, row.reason) == ("down", "wrong")
    assert calls["n"] == 2  # partner: the race path really ran


def test_an_explicit_null_link_is_the_same_as_none(seeded_app: TestClient) -> None:  # noqa: F811
    """A client may send ``"requested_url": null``. Turns red if: the validator
    refuses or mangles an explicit null."""
    _answer(seeded_app)
    res = seeded_app.post(
        "/v1/source-requests", json={"question": "q", "requested_url": None}, headers=DEMO
    )
    assert res.status_code == 202
    assert _rows(SourceRequest)[0].requested_url is None


def test_the_operator_can_filter_feedback_by_rating(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: ``?rating=`` is ignored (the operator would read the praise
    mixed into the complaints)."""
    sid_a, answer_a, _ = _answer(seeded_app)
    sid_b, answer_b, _ = _answer(seeded_app)
    _put(seeded_app, sid_a, answer_a, {"rating": "up"})
    _put(seeded_app, sid_b, answer_b, {"rating": "down", "reason": "wrong"})
    downs = seeded_app.get("/v1/admin/feedback?rating=down", headers=ADMIN).json()["feedback"]
    assert [f["message_id"] for f in downs] == [answer_b]
    both = seeded_app.get("/v1/admin/feedback", headers=ADMIN).json()["feedback"]
    assert len(both) == 2  # partner: without the filter both are there
