"""IDOR regression: ownership + expiry predicate on the 4 session/message routes.

``docs/ADR/0004-user-accounts.md`` PR 1. Before this fix,
``app/api/routes/sessions.py:231,260`` and ``app/api/routes/messages.py:150,197``
each discarded the caller's ``user_id`` (``del user_id`` / an unused ``_user_id``
param) and loaded the row by primary key alone. That is harmless with exactly
one principal (the constant ``demo_user``), but it is a live cross-account IDOR
the moment a second principal exists — this test seeds one directly, the way a
real second account will look once ADR-0004 PR 6 ships, rather than waiting for
that PR to prove the predicate works.

Every assertion here is **404**, never 403: a mismatch must be indistinguishable
from a genuine miss, or the response becomes a membership oracle over the UUID
space (an attacker could tell "wrong owner" from "does not exist" via status
code alone).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import Base, Message, MessageRole, Session, User, UserRole

DEMO_BEARER = "Bearer local-demo-key"
OTHER_USER_ID = "other_user"


@pytest.fixture
def in_memory_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> Generator[TestClient, None, None]:
    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "session_ownership.db"
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())
    try:
        client = TestClient(create_app())
        yield client
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()
        monkeypatch.delenv("CITEVYN_DATABASE_URL", raising=False)


@pytest.fixture
def other_users_session(
    in_memory_client: TestClient,
) -> Generator[tuple[uuid.UUID, uuid.UUID], None, None]:
    """Seed a session (+ one message) owned by ``OTHER_USER_ID``, not the demo caller.

    Seeded directly against the DB, bypassing the API — ``POST /v1/sessions``
    always pins the session to the *authenticated* caller, so there is no
    route-level way to create a row owned by someone else. A real second
    account (ADR-0004 PR 6) would do this legitimately by registering; this
    fixture stands in for that until PR 6 ships.
    """
    factory = get_sessionmaker()
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    now = datetime.now(UTC)

    async def _seed() -> None:
        async with factory() as db:
            db.add(User(user_id=OTHER_USER_ID, role=UserRole.demo_user, created_at=now))
            db.add(
                Session(
                    session_id=session_id,
                    user_id=OTHER_USER_ID,
                    channel="chat",
                    created_at=now,
                    expires_at=now + timedelta(hours=2),
                )
            )
            db.add(
                Message(
                    message_id=message_id,
                    session_id=session_id,
                    role=MessageRole.user,
                    content="a message belonging to another account",
                    created_at=now,
                )
            )
            await db.commit()

    asyncio.run(_seed())
    yield session_id, message_id


def test_get_session_on_another_users_session_returns_404(
    in_memory_client: TestClient, other_users_session: tuple[uuid.UUID, uuid.UUID]
) -> None:
    session_id, _ = other_users_session
    response = in_memory_client.get(
        f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_delete_session_on_another_users_session_returns_404(
    in_memory_client: TestClient, other_users_session: tuple[uuid.UUID, uuid.UUID]
) -> None:
    session_id, _ = other_users_session
    response = in_memory_client.delete(
        f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER}
    )
    assert response.status_code == 404


def test_post_message_on_another_users_session_returns_404(
    in_memory_client: TestClient, other_users_session: tuple[uuid.UUID, uuid.UUID]
) -> None:
    session_id, _ = other_users_session
    response = in_memory_client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "can I read your history?", "answer_style": "short"},
        headers={"Authorization": DEMO_BEARER},
    )
    assert response.status_code == 404


def test_get_message_on_another_users_session_returns_404(
    in_memory_client: TestClient, other_users_session: tuple[uuid.UUID, uuid.UUID]
) -> None:
    session_id, message_id = other_users_session
    response = in_memory_client.get(
        f"/v1/sessions/{session_id}/messages/{message_id}",
        headers={"Authorization": DEMO_BEARER},
    )
    assert response.status_code == 404


def test_a_closed_session_is_unreadable_even_by_its_owner(in_memory_client: TestClient) -> None:
    """The expiry half of the predicate: DELETE only sets ``expires_at`` to
    now (no separate ``closed`` column) — a closed session must not stay
    readable or writable for the remainder of its original TTL."""
    create = in_memory_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    session_id = create.json()["session_id"]
    closed = in_memory_client.delete(
        f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER}
    )
    assert closed.status_code == 204

    get = in_memory_client.get(f"/v1/sessions/{session_id}", headers={"Authorization": DEMO_BEARER})
    assert get.status_code == 404

    post = in_memory_client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "does this still work?", "answer_style": "short"},
        headers={"Authorization": DEMO_BEARER},
    )
    assert post.status_code == 404


# ---------------------------------------------------------------------------
# Route-inventory test — makes the *next* IDOR impossible, not just this one
# ---------------------------------------------------------------------------


def test_every_session_scoped_route_resolves_ownership_via_resolve_principal() -> None:
    """Every route whose path contains ``{session_id}`` must depend on
    ``resolve_principal`` (the dependency that resolves the per-visitor
    cookie identity the ownership predicate checks against, ADR-0004 PR 3).

    Nothing like this test existed before ADR-0004 PR 1 — a `grep` for
    ``app.routes`` in this suite previously returned nothing — which is
    precisely how ``del user_id`` propagated to four routes across two
    files unnoticed. A future route that nests under ``/sessions/{session_id}``
    but forgets to depend on ``resolve_principal`` (and therefore cannot
    enforce ownership) fails this test immediately, rather than shipping a
    silent IDOR that only a targeted regression test would catch. Checks for
    ``resolve_principal`` directly, not the ``rate_limited_demo`` it wraps —
    PR 3 made ``rate_limited_demo`` a nested (not direct) dependency of these
    routes, so a direct-dependency check on it would now fail even though
    ownership is still correctly enforced.
    """
    from app.core.auth_sessions import resolve_principal

    app = create_app()
    # FastAPI >=0.140 wraps each ``include_router`` call in an internal
    # ``_IncludedRouter`` node for routing-performance reasons; the real
    # ``APIRoute`` objects live on its ``original_router.routes``, not
    # flattened into ``app.routes`` directly. Unwrap both shapes so this
    # test does not silently pass on Zero routes after a future FastAPI
    # bump changes the wrapping again (that failure mode is exactly what
    # the vacuity assertion below catches).
    all_routes: list[object] = []
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        all_routes.extend(original_router.routes if original_router is not None else [route])

    session_scoped_routes = [
        route for route in all_routes if "{session_id}" in getattr(route, "path", "")
    ]
    assert session_scoped_routes, "no {session_id} routes found — the scan is vacuous"

    missing: list[str] = []
    for route in session_scoped_routes:
        dependant = getattr(route, "dependant", None)
        assert dependant is not None, f"{route.path} has no dependant to inspect"
        callables = {dep.call for dep in dependant.dependencies}
        if resolve_principal not in callables:
            missing.append(f"{route.methods} {route.path}")

    assert not missing, (
        "route(s) under {session_id} do not depend on resolve_principal, so they "
        f"cannot enforce the ownership predicate: {missing}"
    )
