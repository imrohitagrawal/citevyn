"""Route tests for ``app/api/routes/search.py`` (Slice 8 step 3).

Tests exercise:

* ``POST /v1/search/exact`` — auth, validation, response shape,
  active-sentinel resolution, product-area scoping.
* ``GET /health/index`` — placeholder (``status="pre_index"``)
  when no index row exists, real rows when seeded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import db as db_module
from app.core.config import get_settings
from app.embeddings import EmbedderIdentity, configured_embedder_identity
from app.embeddings.stub import StubEmbedder
from app.main import create_app
from app.models.enums import EvaluationStatus, IndexStatus
from app.models.evaluation import EvaluationRun
from app.models.index_versions import IndexVersion
from tests.conftest import seed_catalog

# ---------------------------------------------------------------------------
# Shared fixture: an app whose get_session is bound to a seeded test session
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_seeded_session(session: AsyncSession):
    """Build a FastAPI app whose ``get_session`` returns the seeded session.

    The ``session`` fixture (in conftest.py) is a per-test
    in-memory SQLite engine with the schema already migrated.
    We override the dependency on each test so the route
    reads the same data the test sees.
    """
    app = create_app()

    async def _override():
        yield session

    app.dependency_overrides[db_module.get_session] = _override
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /v1/search/exact
# ---------------------------------------------------------------------------


API_KEY = "local-demo-key"


def test_search_exact_requires_api_key(app_with_seeded_session) -> None:
    """No bearer → 401."""
    with TestClient(app_with_seeded_session) as client:
        response = client.post(
            "/v1/search/exact",
            json={"term": "--model", "product_area": "codex"},
        )
        assert response.status_code == 401


def test_search_exact_rejects_missing_fields(app_with_seeded_session) -> None:
    """FastAPI's 422 envelope when required fields are missing."""
    with TestClient(app_with_seeded_session) as client:
        response = client.post(
            "/v1/search/exact",
            json={},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "validation_error"
        assert "request_id" in body


def test_search_exact_returns_hit_with_score_one(app_with_seeded_session, session) -> None:
    """A known term in the active index returns one hit with score=1.0."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(seed_catalog(session))

    with TestClient(app_with_seeded_session) as client:
        response = client.post(
            "/v1/search/exact",
            json={"term": "--model", "product_area": "codex"},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["query"] == "--model"
        assert body["product_area"] == "codex"
        assert body["index_version"] == "active"
        assert body["total"] == 1
        hit = body["hits"][0]
        assert hit["term_text"] == "--model"
        assert hit["term_type"] == "flag"
        assert hit["product_area"] == "codex"
        assert hit["score"] == 1.0
        assert hit["index_version"] == "active"
        assert body["request_id"].startswith("req_")


def test_search_exact_returns_empty_for_unknown_term(app_with_seeded_session, session) -> None:
    """An unknown term returns ``total=0`` and an empty list, not 404."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(seed_catalog(session))

    with TestClient(app_with_seeded_session) as client:
        response = client.post(
            "/v1/search/exact",
            json={"term": "--never-seen", "product_area": "codex"},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["hits"] == []


def test_search_exact_scopes_by_product_area(app_with_seeded_session, session) -> None:
    """The same term in two product areas is a different answer."""
    import asyncio
    from datetime import UTC, datetime

    from app.models.chunks import Chunk
    from app.models.documents import Document
    from app.models.enums import DocumentStatus, IndexStatus, TermType
    from app.models.exact_terms import ExactTerm
    from app.models.index_versions import IndexVersion

    async def _setup() -> None:
        # Drop the seed; rebuild with two product areas containing
        # the same term so we can prove the route scopes them.
        await seed_catalog(session)
        from sqlalchemy import select

        active = (
            await session.execute(
                select(IndexVersion).where(IndexVersion.status == IndexStatus.active)
            )
        ).scalar_one()
        now = datetime.now(UTC)
        doc = Document(
            index_version=active.index_version,
            source_name="claude_api",
            product_area="claude_api",
            source_url="https://example.com/claude-api-extra",
            title="Claude API extras",
            identity_checksum="cafe" * 16,
            last_fetched_at=now,
            status=DocumentStatus.active,
        )
        session.add(doc)
        await session.flush()
        chunk = Chunk(
            document_id=doc.document_id,
            product_area="claude_api",
            section_path="flags",
            heading="flags",
            parent_heading=None,
            chunk_text="The --model flag selects the model.",
            context_summary="--model in claude_api.",
            chunk_order=0,
            content_checksum="cafe_chunk_0",
            exact_terms=[],
        )
        session.add(chunk)
        await session.flush()
        session.add(
            ExactTerm(
                term_text="--model",
                term_type=TermType.flag,
                product_area="claude_api",
                document_id=doc.document_id,
                chunk_id=chunk.chunk_id,
            )
        )
        await session.commit()

    asyncio.get_event_loop().run_until_complete(_setup())

    with TestClient(app_with_seeded_session) as client:
        codex = client.post(
            "/v1/search/exact",
            json={"term": "--model", "product_area": "codex"},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        claude = client.post(
            "/v1/search/exact",
            json={"term": "--model", "product_area": "claude_api"},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert codex.status_code == 200
        assert claude.status_code == 200
        assert codex.json()["hits"][0]["product_area"] == "codex"
        assert claude.json()["hits"][0]["product_area"] == "claude_api"
        assert codex.json()["hits"][0]["chunk_id"] != claude.json()["hits"][0]["chunk_id"]


def test_search_exact_clamps_limit_to_max_results(app_with_seeded_session, session) -> None:
    """The route's request validation caps ``limit`` to :data:`MAX_RESULTS`."""
    import asyncio
    from datetime import UTC, datetime

    from app.models.chunks import Chunk
    from app.models.documents import Document
    from app.models.enums import DocumentStatus, IndexStatus, TermType
    from app.models.exact_terms import ExactTerm
    from app.models.index_versions import IndexVersion

    async def _setup() -> None:
        await seed_catalog(session)
        from sqlalchemy import select

        active = (
            await session.execute(
                select(IndexVersion).where(IndexVersion.status == IndexStatus.active)
            )
        ).scalar_one()
        now = datetime.now(UTC)
        for i in range(30):
            doc = Document(
                index_version=active.index_version,
                source_name=f"src_{i}",
                product_area="codex",
                source_url=f"https://example.com/{i}",
                title=f"src {i}",
                identity_checksum=f"chk_{i}" + "0" * 60,
                last_fetched_at=now,
                status=DocumentStatus.active,
            )
            session.add(doc)
            await session.flush()
            chunk = Chunk(
                document_id=doc.document_id,
                product_area="codex",
                section_path=f"h{i}",
                heading=f"h{i}",
                parent_heading=None,
                chunk_text="x" * 10,
                context_summary="x" * 10,
                chunk_order=0,
                content_checksum=f"chk_codex_chunk_{i}",
                exact_terms=[],
            )
            session.add(chunk)
            await session.flush()
            session.add(
                ExactTerm(
                    term_text="--model",
                    term_type=TermType.flag,
                    product_area="codex",
                    document_id=doc.document_id,
                    chunk_id=chunk.chunk_id,
                )
            )
        await session.commit()

    asyncio.get_event_loop().run_until_complete(_setup())

    with TestClient(app_with_seeded_session) as client:
        # 1000 is over the route's le=MAX_RESULTS → 422.
        bad = client.post(
            "/v1/search/exact",
            json={"term": "--model", "product_area": "codex", "limit": 1000},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert bad.status_code == 422

        # But a limit at the cap returns MAX_RESULTS rows.
        ok = client.post(
            "/v1/search/exact",
            json={"term": "--model", "product_area": "codex", "limit": 25},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert ok.status_code == 200
        body = ok.json()
        from app.services.exact_lookup import MAX_RESULTS

        assert body["total"] == MAX_RESULTS


def test_search_exact_passes_through_term_type_filter(app_with_seeded_session, session) -> None:
    """``term_type`` filter is forwarded to the service."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(seed_catalog(session))

    with TestClient(app_with_seeded_session) as client:
        wrong = client.post(
            "/v1/search/exact",
            json={
                "term": "--model",
                "product_area": "codex",
                "term_type": "command",  # wrong type → no hit
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert wrong.status_code == 200
        assert wrong.json()["total"] == 0


def test_search_exact_422_for_invalid_term_type(app_with_seeded_session, session) -> None:
    """An unknown ``term_type`` value is rejected by the schema."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(seed_catalog(session))

    with TestClient(app_with_seeded_session) as client:
        response = client.post(
            "/v1/search/exact",
            json={
                "term": "--model",
                "product_area": "codex",
                "term_type": "not_a_real_type",
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# /health/index
# ---------------------------------------------------------------------------


def test_health_index_pre_index_when_no_rows(app_with_seeded_session) -> None:
    """An empty catalog returns ``status=pre_index`` and null index rows."""
    with TestClient(app_with_seeded_session) as client:
        response = client.get("/health/index")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "pre_index"
        assert body["active_index"] is None
        assert body["previous_good_index"] is None
        assert body["request_id"].startswith("req_")


def test_health_index_ready_when_active_present(app_with_seeded_session, session) -> None:
    """A seeded catalog with an active row reports ``status=ready``."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(seed_catalog(session))

    with TestClient(app_with_seeded_session) as client:
        response = client.get("/health/index")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["active_index"]["index_version"] == "v1"
        assert body["active_index"]["promoted_at"] is not None
        assert body["previous_good_index"] is None


def test_health_index_pre_index_has_null_vector_arm(app_with_seeded_session) -> None:
    """Phase 4c: with no active index there is nothing to embed → vector_arm is null."""
    with TestClient(app_with_seeded_session) as client:
        body = client.get("/health/index").json()
        assert body["status"] == "pre_index"
        assert body["vector_arm"] is None


def test_health_index_reports_dead_vector_arm(app_with_seeded_session, session) -> None:
    """Phase 4c: the seeded catalog's chunks are unembedded (embedder=None), so the
    vector_arm block reports ``dead`` — the exact #97 failure an operator must SEE,
    while the top-level ``status`` stays ``ready`` (additive; does not drain the pod)."""
    import asyncio

    asyncio.get_event_loop().run_until_complete(seed_catalog(session))

    with TestClient(app_with_seeded_session) as client:
        body = client.get("/health/index").json()
        assert body["status"] == "ready"  # unchanged, additive signal
        va = body["vector_arm"]
        assert va["status"] == "dead"
        assert va["healthy"] is False
        assert va["chunks_total"] > 0
        assert va["chunks_embedded"] == 0
        assert va["embedded_ratio"] == 0.0
        # The configured query embedder identity is surfaced (provider/model/dim only).
        assert set(va["configured_query_embedder"]) == {"provider", "model", "dim"}


def test_health_index_reports_null_when_the_index_was_never_evaluated(
    app_with_seeded_session, session
) -> None:
    """Never-evaluated stays ``null`` — the partner to the non-null case (#229).

    ``TestHealthIndexReportsTheEvidence`` in ``test_promotion_eval.py`` proves
    the field goes NON-null once a run exists. This proves it is still ``null``
    when no run does, so neither assertion is the vacuous half of the pair: a
    fix that hard-coded some id would fail here, and a fix that changed nothing
    would fail there.
    """
    import asyncio

    asyncio.get_event_loop().run_until_complete(seed_catalog(session))

    with TestClient(app_with_seeded_session) as client:
        body = client.get("/health/index").json()
    assert body["active_index"]["index_version"] == "v1"
    assert body["active_index"]["evaluation_run_id"] is None


def test_health_index_never_credits_one_index_with_anothers_run(
    app_with_seeded_session, session
) -> None:
    """Another index's passing run must NOT surface on the active index (#229).

    Reporting a run that belongs to a different index version would be strictly
    worse than the ``null`` the issue is about: ``null`` under-reports, while a
    borrowed run id certifies something nobody measured.
    """
    import asyncio

    asyncio.get_event_loop().run_until_complete(seed_catalog(session))

    now = datetime.now(UTC)
    other = IndexVersion(
        index_version="v2",
        status=IndexStatus.candidate,
        source_version_hash="sha256:v2",
        created_at=now,
        promoted_at=None,
    )
    run = EvaluationRun(
        suite_name="promotion",
        index_version="v2",
        started_at=now,
        completed_at=now,
        status=EvaluationStatus.passed,
        metrics={"pass_rate": 1.0, "cases_total": 4, "cases_passed": 4},
        failure_summary={},
    )
    session.add_all([other, run])
    asyncio.get_event_loop().run_until_complete(session.flush())
    other.evaluation_run_id = run.run_id
    asyncio.get_event_loop().run_until_complete(session.commit())

    with TestClient(app_with_seeded_session) as client:
        body = client.get("/health/index").json()

    # ``v1`` is the active index and was never evaluated.
    assert body["active_index"]["index_version"] == "v1"
    assert body["active_index"]["evaluation_run_id"] is None
    # ...and the run that DOES exist is genuinely linked to v2, so the null
    # above is a real scoping result and not "no runs exist anywhere".
    linked = asyncio.get_event_loop().run_until_complete(session.get(IndexVersion, "v2"))
    assert linked is not None
    assert linked.evaluation_run_id == run.run_id


def test_search_exact_422_redacts_user_input(app_with_seeded_session) -> None:
    """The 422 envelope must not echo back user-provided input.

    Pydantic's default ``errors()`` includes an ``input`` key
    with the offending value verbatim. We strip that to
    ``<N chars redacted>`` (string) or ``<redacted>`` (other) so
    a chat payload (or, in a future slice, a pasted token) is
    never round-tripped through the error response.

    We use ``term_type="bogus"`` (not in the
    :class:`TermType` enum) to force a validation error after
    Pydantic has captured the offending input.
    """
    sensitive = "user-pasted-secret-value-that-must-not-leak"
    with TestClient(app_with_seeded_session) as client:
        response = client.post(
            "/v1/search/exact",
            json={"term": sensitive, "product_area": "codex", "term_type": "bogus"},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert response.status_code == 422
        body_str = response.text
        assert sensitive not in body_str, f"422 envelope leaked the offending input: {body_str}"
        # The redactor's marker is in the body.
        assert "redacted" in body_str


# ---------------------------------------------------------------------------
# /health/index — deterministic resolution + dual-active honesty (#264)
# ---------------------------------------------------------------------------


def _healthy_seed(session) -> EmbedderIdentity:
    """Seed the catalog so the vector arm is genuinely ``healthy``.

    Every chunk is embedded and the index stamp equals the CONFIGURED query
    embedder, so ``derive_vector_arm_status`` returns ``healthy``. Derived from
    ``get_settings()`` rather than hard-coded so a stray ``backend/.env`` cannot
    silently turn the control case into a ``mismatch``.
    """
    import asyncio

    identity = configured_embedder_identity(get_settings())
    asyncio.get_event_loop().run_until_complete(
        seed_catalog(
            session,
            embedder=StubEmbedder(dim=identity.dim),
            embedder_identity=identity,
        )
    )
    return identity


def _add_index(session, *, version: str, status: IndexStatus, promoted_at, identity=None) -> None:
    import asyncio

    session.add(
        IndexVersion(
            index_version=version,
            status=status,
            source_version_hash=f"sha256:{version}",
            created_at=datetime.now(UTC),
            promoted_at=promoted_at,
            embedding_provider=identity.provider if identity else None,
            embedding_model=identity.model if identity else None,
            embedding_dim=identity.dim if identity else None,
        )
    )
    asyncio.get_event_loop().run_until_complete(session.commit())


def test_health_index_reports_healthy_with_exactly_one_active_row(
    app_with_seeded_session, session
) -> None:
    """CONTROL for the dual-active case below (#264).

    Without this, ``status == "ambiguous"`` there could pass for the wrong
    reason — a broken seed (no chunks ⇒ ``empty``, unembedded ⇒ ``dead``, a
    stray stamp ⇒ ``mismatch``) would never produce ``healthy`` in the first
    place, so the ambiguous assertion would prove nothing about dual-active.
    This pins that the very same seed DOES read ``healthy`` when one row is
    active, which is what makes the ambiguous flip meaningful.
    """
    _healthy_seed(session)

    with TestClient(app_with_seeded_session) as client:
        body = client.get("/health/index").json()

    va = body["vector_arm"]
    assert va["status"] == "healthy"
    assert va["healthy"] is True
    assert va["embedder_match"] is True
    assert va["chunks_total"] > 0
    assert va["chunks_embedded"] == va["chunks_total"]
    assert va["active_index_count"] == 1


def test_health_index_reports_ambiguous_when_two_rows_are_active(
    app_with_seeded_session, session
) -> None:
    """#264: dual-active turns the read path's vector arm OFF, so the route must not say healthy.

    Measured in the issue: with two ``active`` rows both stamped to the
    configured embedder, ``HybridRetriever._active_index_stamp`` returns
    ``IndexStampStatus.ambiguous`` and the vector arm is disabled, while this
    route reported ``{"status": "healthy", "healthy": true, "embedder_match":
    true}``. The dashboard read green at the exact moment semantic recall was
    off.

    Turns RED if the route drops the ``active_count > 1`` check: the seed is the
    one the control test above proves reads ``healthy``, so with the check gone
    this reports ``healthy`` again.
    """
    identity = _healthy_seed(session)
    _add_index(
        session,
        version="a2",
        status=IndexStatus.active,
        promoted_at=datetime.now(UTC) + timedelta(minutes=5),
        identity=identity,
    )

    with TestClient(app_with_seeded_session) as client:
        body = client.get("/health/index").json()

    va = body["vector_arm"]
    assert va["status"] == "ambiguous"
    assert va["healthy"] is False
    # Fails closed, exactly as ``is_index_embedder_mismatch`` does for the
    # ``ambiguous`` sentinel on the read path (#226).
    assert va["embedder_match"] is False
    assert va["active_index_count"] == 2
    # The counts are not knowable — there is no single index whose chunks these
    # would be — so they are ``null`` rather than a plausible-looking lie.
    assert va["chunks_total"] is None
    assert va["chunks_embedded"] is None
    assert va["embedded_ratio"] is None
    assert va["index_embedder"] is None
    # The configured query embedder IS knowable and stays populated.
    assert set(va["configured_query_embedder"]) == {"provider", "model", "dim"}
    # Additive signal: the top-level probe still says an active index exists, so
    # a load balancer does not drain the pod over an operator-facing verdict.
    assert body["status"] == "ready"


def test_health_index_agrees_with_the_read_path_on_the_same_dual_active_session(
    app_with_seeded_session, session
) -> None:
    """The actual defect in #264 was a DISAGREEMENT, so pin both sides at once.

    Asserting only that the route says ``ambiguous`` would let the two desync
    again — which is exactly how this bug came to exist: #226 hardened the read
    path and the route was left behind. Here one session drives both, so the
    test fails if either half changes its mind independently.

    Turns RED if the route stops resolving through the shared resolver: the
    control test above proves this seed reads ``healthy``, while
    ``_vector_arm_enabled`` has failed closed on dual-active since #226.
    """
    import asyncio

    from app.retrieval.hybrid import HybridRetriever

    identity = _healthy_seed(session)
    _add_index(
        session,
        version="a2",
        status=IndexStatus.active,
        promoted_at=datetime.now(UTC) + timedelta(minutes=5),
        identity=identity,
    )

    retriever = HybridRetriever(session, active_index_version=None, embedder_identity=identity)
    arm_enabled = asyncio.get_event_loop().run_until_complete(retriever._vector_arm_enabled())

    with TestClient(app_with_seeded_session) as client:
        body = client.get("/health/index").json()

    assert arm_enabled is False, "precondition: the read path fails closed on dual-active (#226)"
    assert body["vector_arm"]["healthy"] is False, (
        "the route reported the vector arm healthy while retrieval had it OFF — #264"
    )


def test_health_index_declines_to_name_an_active_row_when_ambiguous(
    app_with_seeded_session, session
) -> None:
    """``active_index`` is ``null`` under dual-active, not one of the N rows (#264).

    Naming the newest row would be deterministic but would restate the coin flip
    at a second key: a dashboard reading ``active_index.index_version`` would
    take it as *the* answer while ``vector_arm.status`` says nobody knows. The
    ``pre_index`` branch already emits ``active_index: null``, so a null here is
    a shape consumers must already handle.
    """
    identity = _healthy_seed(session)
    _add_index(
        session,
        version="a2",
        status=IndexStatus.active,
        promoted_at=datetime.now(UTC) + timedelta(minutes=5),
        identity=identity,
    )

    with TestClient(app_with_seeded_session) as client:
        body = client.get("/health/index").json()

    assert body["active_index"] is None
    # Partner: both rows really ARE active, so the null above is a deliberate
    # refusal and not "there was nothing to name".
    assert body["vector_arm"]["active_index_count"] == 2
    assert "2 index versions are marked active" in body["message"]


def test_health_index_previous_good_names_the_real_rollback_target(
    app_with_seeded_session, session
) -> None:
    """``previous_good_index`` must be the most recently demoted row (#264).

    ``promote_version`` demotes the outgoing ``active`` row to ``previous_good``
    and never clears the ones already there, so after the second promotion the
    table holds more than one. ``DEPLOY_FLY.md`` §4.4 makes this route the
    post-deploy check and §6 item 4 makes the previous-good index the rollback
    target, so naming an arbitrary stale row points an incident at the wrong
    index.

    Adversarial by construction — the three orderings disagree again:
    insertion order and ``index_version DESC`` both say ``pg_stale``
    (``"pg_stale" > "pg_recent"``), only ``promoted_at DESC`` says
    ``pg_recent``.
    """
    import asyncio

    _healthy_seed(session)
    now = datetime.now(UTC)
    _add_index(
        session,
        version="pg_stale",
        status=IndexStatus.previous_good,
        promoted_at=now - timedelta(hours=2),
    )
    _add_index(
        session,
        version="pg_recent",
        status=IndexStatus.previous_good,
        promoted_at=now - timedelta(minutes=5),
    )

    with TestClient(app_with_seeded_session) as client:
        body = client.get("/health/index").json()

    assert body["previous_good_index"]["index_version"] == "pg_recent"
    # Partner assertion: the stale row still EXISTS, so the line above is a
    # genuine ordering result and not "the other candidate was never there".
    stale = asyncio.get_event_loop().run_until_complete(session.get(IndexVersion, "pg_stale"))
    assert stale is not None
    assert stale.status is IndexStatus.previous_good
