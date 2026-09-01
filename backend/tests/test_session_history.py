"""Tests for ADR-0004 PR 10: `GET /v1/me/sessions` + citation hydration on resume.

Mirrors ``test_messages_routes.py``'s ``seeded_app`` fixture (a full active
index + catalog under ``provider=stub``) rather than importing it — ruff
(F811) flags a pytest fixture re-exported by name and then used as a test
parameter as a redefinition, and every sibling test module in this suite
already defines its own HTTP+DB fixtures rather than cross-importing one.
So the citation tests exercise a REAL grounded-answer path, not a
hand-built fixture: the whole point of migration 0009 is that
``GET /v1/sessions/{id}`` shows the same citations the live POST response
already did.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import db as db_module
from app.core import rate_limit
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import Base, IndexStatus, IndexVersion, Message
from tests.conftest import seed_catalog

DEMO_BEARER = "Bearer local-demo-key"


@pytest.fixture
def in_memory_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> Generator[TestClient, None, None]:
    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "session_history.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    # The process-wide demo_user rate-limit bucket is keyed on the client
    # address, which TestClient defaults to the SAME synthetic host for
    # every test in the whole run -- several tests here register accounts
    # and create sessions in a loop, enough to push a shared, un-reset
    # counter past 30/hour and 429 a LATER test file's very first request
    # (found by running the full suite, not this file in isolation).
    rate_limit.reset_limiter()
    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())
    try:
        client = TestClient(create_app())
        yield client
    finally:
        rate_limit.reset_limiter()
        get_settings.cache_clear()
        db_module.reset_engine()
        monkeypatch.delenv("CITEVYN_DATABASE_URL", raising=False)


@pytest.fixture
def seeded_app(in_memory_client: TestClient) -> Generator[TestClient, None, None]:
    factory = get_sessionmaker()

    async def _seed() -> None:
        async with factory() as session:
            version = IndexVersion(
                index_version="index_v1",
                status=IndexStatus.active,
                source_version_hash="sha256:index-v1",
                created_at=datetime.now(UTC),
                promoted_at=datetime.now(UTC),
            )
            session.add(version)
            await session.flush()
            await seed_catalog(session)

    asyncio.run(_seed())
    yield in_memory_client


# ---------------------------------------------------------------------------
# GET /v1/me/sessions
# ---------------------------------------------------------------------------


def test_list_my_sessions_returns_newest_first_with_message_counts(
    seeded_app: TestClient,
) -> None:
    first = seeded_app.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    ).json()["session_id"]
    seeded_app.post(
        f"/v1/sessions/{first}/messages",
        json={"message": "How do I configure Claude Code permissions?", "answer_style": "short"},
        headers={"Authorization": DEMO_BEARER},
    )
    second = seeded_app.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    ).json()["session_id"]

    response = seeded_app.get("/v1/me/sessions", headers={"Authorization": DEMO_BEARER})
    assert response.status_code == 200
    body = response.json()
    ids = [row["session_id"] for row in body["sessions"]]
    # Newest first: `second` (no messages) comes before `first`.
    assert ids.index(second) < ids.index(first)

    first_row = next(row for row in body["sessions"] if row["session_id"] == first)
    assert first_row["message_count"] == 2  # user + assistant
    second_row = next(row for row in body["sessions"] if row["session_id"] == second)
    assert second_row["message_count"] == 0


def test_list_my_sessions_only_shows_the_callers_own(seeded_app: TestClient) -> None:
    """The real two-account isolation check, mirrored for the list endpoint
    (see test_auth_routes.py::test_two_real_accounts_cannot_read_each_others_sessions
    for the single-session GET version)."""
    seeded_app.post(
        "/v1/auth/register",
        json={"email": "alice-history@example.com", "password": "correct horse battery"},
        headers={"Authorization": DEMO_BEARER},
    )
    seeded_app.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )

    other_client = TestClient(seeded_app.app)
    other_client.post(
        "/v1/auth/register",
        json={"email": "bob-history@example.com", "password": "correct horse battery"},
        headers={"Authorization": DEMO_BEARER},
    )

    response = other_client.get("/v1/me/sessions", headers={"Authorization": DEMO_BEARER})
    assert response.status_code == 200
    assert response.json()["sessions"] == []


def test_list_my_sessions_works_for_an_anonymous_visitor(seeded_app: TestClient) -> None:
    """No account required -- the endpoint lists whatever exists under the
    caller's current cookie, per the module's own docstring."""
    seeded_app.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    response = seeded_app.get("/v1/me/sessions", headers={"Authorization": DEMO_BEARER})
    assert response.status_code == 200
    assert len(response.json()["sessions"]) == 1


# ---------------------------------------------------------------------------
# Message ordering on resume
# ---------------------------------------------------------------------------


def test_the_assistant_reply_is_persisted_strictly_after_its_question(
    seeded_app: TestClient,
) -> None:
    """Found live (a real browser walkthrough of the history drawer): the
    user question and its assistant answer were persisted with the SAME
    `created_at` microsecond, so `GET /v1/sessions/{id}`'s
    `ORDER BY created_at ASC, message_id ASC` fell back to comparing two
    RANDOM UUIDs as a tiebreaker -- and could, and did, put the answer
    above the question. Nothing exercised this read path in full before
    the history drawer existed (the live streaming chat renders its own
    messages client-side and never re-fetches this endpoint), so it stayed
    invisible until now.

    Asserted directly against the persisted `created_at` values, not
    through the API's returned order: a UUID tiebreak is a coin flip, so a
    test that only checks response ORDER can pass by luck even with the
    bug present (confirmed: reverting the fix and rerunning this exact
    scenario passed about half the time). Comparing the actual timestamps
    is the only way to make this test reliably fail on regression.
    """
    create = seeded_app.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    session_id = create.json()["session_id"]
    seeded_app.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "How do I configure Claude Code permissions?", "answer_style": "short"},
        headers={"Authorization": DEMO_BEARER},
    )

    async def _fetch() -> tuple[Message, Message]:
        async with get_sessionmaker()() as db:
            rows = (
                (await db.execute(select(Message).where(Message.session_id == session_id)))
                .scalars()
                .all()
            )
            user_row = next(m for m in rows if m.role.value == "user")
            assistant_row = next(m for m in rows if m.role.value == "assistant")
            return user_row, assistant_row

    user_row, assistant_row = asyncio.run(_fetch())
    assert assistant_row.created_at > user_row.created_at

    # And the API's own ordering agrees, end to end.
    resumed = seeded_app.get(
        f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER}
    ).json()
    assert [m["role"] for m in resumed["messages"]] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# Citation hydration on resume (migration 0009)
# ---------------------------------------------------------------------------


def test_resumed_session_shows_the_same_citations_the_live_answer_did(
    seeded_app: TestClient,
) -> None:
    create = seeded_app.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    session_id = create.json()["session_id"]

    live = seeded_app.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "How do I configure Claude Code permissions?", "answer_style": "short"},
        headers={"Authorization": DEMO_BEARER},
    ).json()
    assert live["citations"], "the seeded happy path must ground an answer with citations"

    resumed = seeded_app.get(
        f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER}
    ).json()
    assistant_message = next(m for m in resumed["messages"] if m["role"] == "assistant")

    # Same shape and content the live response carried -- not reconstructed,
    # not a subset, not re-derived from retrieved_evidence.
    assert assistant_message["citations"] == live["citations"]
    assert all("marker" in c for c in assistant_message["citations"])


def test_a_user_message_has_no_citations(seeded_app: TestClient) -> None:
    create = seeded_app.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    session_id = create.json()["session_id"]
    seeded_app.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "How do I configure Claude Code permissions?", "answer_style": "short"},
        headers={"Authorization": DEMO_BEARER},
    )

    resumed = seeded_app.get(
        f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER}
    ).json()
    user_message = next(m for m in resumed["messages"] if m["role"] == "user")
    assert user_message["citations"] == []


# The cache-hit case (zero retrieved_evidence rows persisted, citations
# column must still be populated) is covered at the Orchestrator level in
# test_answer_orchestrator.py::test_gapped_citation_markers_survive_the_cache_round_trip,
# which asserts DB persistence directly -- the seeded_app HTTP fixture used
# above does not reliably produce a real cache hit (its embedder identity
# does not match settings, so cache writes are skipped; see the existing
# cache tests in test_cache_invalidation.py, which test the same layer for
# the same reason). The read path (GET /v1/sessions/{id}) is identical
# regardless of which write path populated the citations column, so the
# fresh-write resume test above plus the cache-hit persistence test
# together cover the full path without fighting that fixture.
