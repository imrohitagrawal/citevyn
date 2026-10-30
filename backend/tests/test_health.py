import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import db as db_module
from app.main import create_app


def test_health_returns_status_and_request_id(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "citevyn-backend"
    assert body["request_id"].startswith("req_")


def test_dependencies_report_no_external_dependencies(client: TestClient) -> None:
    response = client.get("/health/dependencies")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    # The default test settings point at a working SQLite URL, so the
    # postgres dependency reports a healthy ping. (Post-Slice 2 the
    # route delegates to ``app.core.db.ping_database``.)
    assert body["dependencies"]["postgres"]["status"] == "healthy"
    assert "latency_ms" in body["dependencies"]["postgres"]
    assert body["request_id"].startswith("req_")


@pytest.fixture
def app_with_session(session: AsyncSession):
    """Build a FastAPI app whose ``get_session`` returns ``session``.

    Pre-Slice 8 the ``/health/index`` route was a hard-coded
    placeholder. Now it reads ``index_versions`` from the DB,
    so the test needs a session with the schema already
    migrated. The ``session`` fixture from ``conftest.py``
    builds that schema, so we just override the dependency.
    """
    app = create_app()

    async def _override():
        yield session

    app.dependency_overrides[db_module.get_session] = _override
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


def test_index_reports_pre_index_status(app_with_session) -> None:
    with TestClient(app_with_session) as client:
        response = client.get("/health/index")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "pre_index"
        assert body["active_index"] is None
        assert body["previous_good_index"] is None
        assert body["request_id"].startswith("req_")


def test_index_reports_the_query_embedder_with_no_tracker(app_with_session, monkeypatch) -> None:
    """Red when: the ``query_embedder`` block is dropped from the pre_index
    shape, or reports "ok" when nothing is tracked (no Redis), which would hide
    a refusal from the uptime probe."""
    from app.embeddings import query_health

    monkeypatch.setattr(query_health, "current", lambda: None)
    with TestClient(app_with_session) as client:
        body = client.get("/health/index").json()

    assert body["status"] == "pre_index"
    assert body["query_embedder"] == {"state": "unknown", "refused_since": None}


def test_index_reports_a_refused_query_embedder_without_detail(
    app_with_session, monkeypatch
) -> None:
    """Red when: the route stops reading the tracker, or the public block starts
    carrying the status code, the provider or a nested ``healthy`` key.

    The route is public (#273), so the block says only THAT the provider refuses
    the account and since when; which refusal it was is in the server log.
    ``deploy_verify.sh`` greps the whole body for ``"healthy": true``, so a
    nested ``healthy`` here could let a broken deploy pass that check."""
    from app.embeddings import query_health

    since = "2026-10-30T12:00:00+00:00"

    class _RefusedTracker:
        async def status(self) -> dict[str, str | None]:
            return {"state": "refused", "refused_since": since}

    monkeypatch.setattr(query_health, "current", lambda: _RefusedTracker())

    with TestClient(app_with_session) as client:
        body = client.get("/health/index").json()

    block = body["query_embedder"]
    assert block == {"state": "refused", "refused_since": since}
    text = json.dumps(block).lower()
    for leak in ("402", "payment", "gemini", "openrouter", "healthy", "balance"):
        assert leak not in text, leak
