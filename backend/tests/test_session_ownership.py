"""IDOR regression: ownership + expiry predicate on the 4 session/message routes.

``docs/ADR/0004-user-accounts.md`` PR 1. Before this fix,
``app/api/routes/sessions.py:231,260`` and ``app/api/routes/messages.py:150,197``
each discarded the caller's ``user_id`` (``del user_id`` / an unused ``_user_id``
param) and loaded the row by primary key alone. That is harmless with exactly
one principal (the constant ``demo_user``), but it is a live cross-account IDOR
the moment a second principal exists — this test seeds one directly rather than
driving the account routes to make one.

Second principals are no longer hypothetical: PR 3 gave every visitor a
cookie-derived ``anon_`` principal and PR 6 shipped real ``usr_`` accounts, so
the predicate has been load-bearing in production since PR 3. Seeding the row
directly is still the right shape here — it keeps this an ownership test rather
than a registration test — but it is a convenience now, not a stand-in for a PR
that has not landed.

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
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    # The limiter is process-wide and nothing in this fixture used to reset it,
    # so every request this file makes spent the SAME 30/hour demo bucket as
    # whatever module ran before it. Running this file together with
    # ``test_messages_routes.py`` produced six 429-driven failures for that
    # reason alone, with nothing here changed — reproduced at d4763d6 in a
    # throwaway ``git archive`` tree, so it predates #451/#452. No test in this
    # file asserts anything about rate limiting, so there is no accumulated
    # state worth preserving. ``conftest.py``'s own ``client`` fixture has
    # always done this; this fixture simply never did.
    rate_limit.reset_limiter()
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
            # One flush per level, parent first. ``sessions.user_id`` and
            # ``messages.session_id`` are foreign keys, but no ORM
            # ``relationship()`` links these mapped classes, so a single
            # combined flush is NOT ordered by them -- SQLAlchemy emitted the
            # ``sessions`` insert first and it failed the key (#286). Same
            # trap as the ``_mint_principal`` production bug.
            #
            # This is ordering only. ``OTHER_USER_ID`` stays a genuinely
            # distinct principal from the demo caller's, which is the whole
            # point of the file: collapsing the two identities would make
            # every 404 assertion below vacuously true.
            db.add(User(user_id=OTHER_USER_ID, role=UserRole.demo_user, created_at=now))
            await db.flush()
            db.add(
                Session(
                    session_id=session_id,
                    user_id=OTHER_USER_ID,
                    channel="chat",
                    created_at=now,
                    expires_at=now + timedelta(hours=2),
                )
            )
            await db.flush()
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


def test_message_read_is_scoped_to_the_session_in_the_path(
    in_memory_client: TestClient, other_users_session: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """One session, another session's message id — the *second* half of the
    predicate in ``messages.py::require_message`` (#452).

    ``require_session`` above it proves the caller owns the session in the
    path. ``Message.session_id == session_id`` is then the only thing tying
    the requested message to that session, and before this test it could be
    deleted with the whole suite green:

    * ``test_get_message_on_another_users_session_returns_404`` pairs the
      victim's session with the victim's own message, so its 404 is raised by
      the ownership check one call earlier and never reaches this predicate.
    * ``test_messages_routes.py`` pairs a real session with ``uuid.uuid4()``,
      a message id that exists in NO session, so it stays 404 either way.

    So this test pairs the caller's OWN (owned, live) session with a message
    belonging to a different session. The message row exists, the session is
    the caller's, and only the scoping predicate can produce the 404.

    The partner assertion in the same test fetches one of the caller's own
    messages through its own session and requires 200: without it, a 404 from
    an unseeded fixture, a mis-cased path or a broken cookie would read as a
    passing access-control check.
    """
    _, other_users_message_id = other_users_session

    # The caller's own session, created through the API so it is pinned to the
    # cookie principal ``resolve_principal`` mints for this client.
    create = in_memory_client.post(
        "/v1/sessions", json={"channel": "chat"}, headers={"Authorization": DEMO_BEARER}
    )
    assert create.status_code == 201, create.text
    own_session_id = uuid.UUID(create.json()["session_id"])

    # A message in that session, seeded directly rather than through
    # ``POST .../messages`` — this is an authorization test, not a test of the
    # answer pipeline, and the pipeline would need a whole seeded corpus.
    own_message_id = uuid.uuid4()

    async def _seed_own_message() -> None:
        factory = get_sessionmaker()
        async with factory() as db:
            db.add(
                Message(
                    message_id=own_message_id,
                    session_id=own_session_id,
                    role=MessageRole.user,
                    content="a message the caller genuinely owns",
                    created_at=datetime.now(UTC),
                )
            )
            await db.commit()

    asyncio.run(_seed_own_message())

    # Partner: the caller CAN read its own message through its own session.
    # Stated first so a fixture that seeded nothing fails here, loudly, rather
    # than making the cross-session assertion below pass for the wrong reason.
    own = in_memory_client.get(
        f"/v1/sessions/{own_session_id}/messages/{own_message_id}",
        headers={"Authorization": DEMO_BEARER},
    )
    assert own.status_code == 200, own.text
    assert own.json()["message_id"] == str(own_message_id)

    # The guard: another session's message id, through the caller's own session.
    crossed = in_memory_client.get(
        f"/v1/sessions/{own_session_id}/messages/{other_users_message_id}",
        headers={"Authorization": DEMO_BEARER},
    )
    assert crossed.status_code == 404, (
        "a message belonging to another session was readable through the "
        f"caller's own session: {crossed.status_code} {crossed.text}"
    )
    # 404, never 403 — same membership-oracle reason as the rest of this file.
    assert crossed.json()["error"]["code"] == "not_found"


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
