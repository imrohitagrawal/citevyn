"""End-to-end tests for the messages HTTP routes (Slice 7).

Covers the two endpoints defined in ``docs/API_SPEC.md`` §5:

* ``POST /v1/sessions/{session_id}/messages`` — the answer endpoint.
* ``GET /v1/sessions/{session_id}/messages/{message_id}`` — fetch one
  message for citation hydration.

The happy-path test seeds the minimal catalog (an active index, five
documents — the four product areas plus the About-CiteVyn source — one
chunk each) through a one-shot async seed and asserts the full
grounded-answer shape. The error-path tests
confirm the standard envelope and status mapping without seeding.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import _GENERIC_LLM_UNAVAILABLE_REASON, create_app
from app.models import Base, IndexStatus, IndexVersion
from tests.conftest import seed_catalog

DEMO_BEARER = "Bearer local-demo-key"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> Generator[TestClient, None, None]:
    """A TestClient backed by a per-test SQLite file under tmp_path.

    A temp file is shared across connections; ``:memory:`` would give
    every async connection its own database and the route's
    :func:`get_session` would not see rows seeded by the fixture. The
    schema is created up front so the route does not need migrations.
    """
    db_module.reset_engine()
    get_settings.cache_clear()
    db_file = tmp_path / "messages_route.db"
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
def seeded_app(
    in_memory_client: TestClient,
) -> Generator[TestClient, None, None]:
    """Yield the in-memory client after seeding the catalog + index."""
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
# POST /v1/sessions/{session_id}/messages
# ---------------------------------------------------------------------------


def test_post_message_returns_200_with_full_shape(seeded_app: TestClient) -> None:
    """The full happy path: session exists, supported question, grounded
    answer, citations, and an audit row."""
    create = seeded_app.post(
        "/v1/sessions",
        json={"channel": "chat"},
        headers={"Authorization": DEMO_BEARER},
    )
    assert create.status_code == 201
    session_id = create.json()["session_id"]

    response = seeded_app.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "How do I configure Claude Code permissions?", "answer_style": "short"},
        headers={"Authorization": DEMO_BEARER},
    )

    assert response.status_code == 200
    body = response.json()
    # Spec §5 fields.
    assert body["request_id"].startswith("req_")
    assert uuid.UUID(body["message_id"])
    assert isinstance(body["answer"], str) and body["answer"]
    assert body["domain"] == "claude_code"
    assert body["intent"] == "how_to"
    assert body["confidence"] in {"high", "medium", "low"}
    assert body["cache_hit"] is False
    assert body["retrieval_strategy"] == "hybrid_reranked"
    assert body["unsupported"] is False
    assert body["no_answer"] is False
    # Citations are projected from the chunks the seed returned.
    assert isinstance(body["citations"], list)


def test_post_message_answers_citevyn_meta_question(seeded_app: TestClient) -> None:
    """#49: a question about CiteVyn itself ("What is CiteVyn Pro?") is answered
    from the indexed About-CiteVyn source — classified in-domain and grounded —
    NOT refused as unsupported the way it was before the source existed."""
    create = seeded_app.post(
        "/v1/sessions",
        json={"channel": "chat"},
        headers={"Authorization": DEMO_BEARER},
    )
    session_id = create.json()["session_id"]

    response = seeded_app.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "What do I get with CiteVyn Pro?", "answer_style": "short"},
        headers={"Authorization": DEMO_BEARER},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["domain"] == "citevyn"
    assert body["unsupported"] is False
    assert body["no_answer"] is False
    assert isinstance(body["answer"], str) and body["answer"]
    # Grounded in the About-CiteVyn chunk, cited to the host-agnostic /about.
    assert len(body["citations"]) >= 1
    assert any("/about" in (c.get("url") or "") for c in body["citations"])


def test_post_message_returns_unsupported_shape(seeded_app: TestClient) -> None:
    """An off-domain question must come back with the unsupported flag,
    not a transport error (per spec §6)."""
    create = seeded_app.post(
        "/v1/sessions",
        json={"channel": "chat"},
        headers={"Authorization": DEMO_BEARER},
    )
    session_id = create.json()["session_id"]

    response = seeded_app.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "What is the recipe for chocolate cake?"},
        headers={"Authorization": DEMO_BEARER},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["unsupported"] is True
    assert body["no_answer"] is True
    assert body["domain"] == "unsupported"
    assert body["intent"] == "unsupported"
    assert body["confidence"] == "none"
    assert body["citations"] == []


def test_post_message_greeting_carries_neutral_general_domain(seeded_app: TestClient) -> None:
    """A bare greeting is a friendly reply, not a refusal: over the wire it
    carries the neutral ``general`` domain with ``unsupported``/``no_answer``
    both false, and the stored ``domain`` replayed by GET agrees (#89). A
    greeting must never surface ``domain: "unsupported"`` — that would break
    the ``domain == "unsupported"`` ⟺ ``unsupported == true`` invariant."""
    create = seeded_app.post(
        "/v1/sessions",
        json={"channel": "chat"},
        headers={"Authorization": DEMO_BEARER},
    )
    session_id = create.json()["session_id"]

    response = seeded_app.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "good morning"},
        headers={"Authorization": DEMO_BEARER},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "greeting"
    assert body["domain"] == "general"
    assert body["unsupported"] is False
    assert body["no_answer"] is False
    assert body["citations"] == []

    # The stored domain replayed by GET matches the wire response.
    replay = seeded_app.get(
        f"/v1/sessions/{session_id}/messages/{body['message_id']}",
        headers={"Authorization": DEMO_BEARER},
    )
    assert replay.status_code == 200
    assert replay.json()["domain"] == "general"


def test_post_message_rejects_bad_answer_style(seeded_app: TestClient) -> None:
    create = seeded_app.post(
        "/v1/sessions",
        json={"channel": "chat"},
        headers={"Authorization": DEMO_BEARER},
    )
    session_id = create.json()["session_id"]

    response = seeded_app.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "hi", "answer_style": "essay"},
        headers={"Authorization": DEMO_BEARER},
    )

    assert response.status_code == 422
    envelope = response.json()  # flat, per docs/API_SPEC.md §4 — NOT nested under "detail"
    assert envelope["error"]["code"] == "validation_error"
    assert "answer_style" in envelope["error"]["message"]


def test_post_message_returns_404_when_session_missing(in_memory_client: TestClient) -> None:
    response = in_memory_client.post(
        f"/v1/sessions/{uuid.uuid4()}/messages",
        json={"message": "hello", "answer_style": "short"},
        headers={"Authorization": DEMO_BEARER},
    )

    assert response.status_code == 404
    envelope = response.json()  # flat, per docs/API_SPEC.md §4 — NOT nested under "detail"
    assert envelope["error"]["code"] == "not_found"


def test_post_message_requires_bearer_token(in_memory_client: TestClient) -> None:
    response = in_memory_client.post(
        f"/v1/sessions/{uuid.uuid4()}/messages",
        json={"message": "hello", "answer_style": "short"},
    )
    assert response.status_code == 401


def test_post_message_rejects_wrong_bearer_token(in_memory_client: TestClient) -> None:
    response = in_memory_client.post(
        f"/v1/sessions/{uuid.uuid4()}/messages",
        json={"message": "hello", "answer_style": "short"},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /v1/sessions/{session_id}/messages/{message_id}
# ---------------------------------------------------------------------------


def test_get_message_returns_payload_and_evidence(seeded_app: TestClient) -> None:
    create = seeded_app.post(
        "/v1/sessions",
        json={"channel": "chat"},
        headers={"Authorization": DEMO_BEARER},
    )
    session_id = create.json()["session_id"]

    post = seeded_app.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "How do I configure Claude Code permissions?"},
        headers={"Authorization": DEMO_BEARER},
    )
    message_id = post.json()["message_id"]

    response = seeded_app.get(
        f"/v1/sessions/{session_id}/messages/{message_id}",
        headers={"Authorization": DEMO_BEARER},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["message_id"] == message_id
    assert body["session_id"] == session_id
    assert body["role"] == "assistant"
    assert isinstance(body["content"], str)
    # Evidence rows are persisted by the orchestrator.
    assert isinstance(body["evidence"], list)


def test_get_message_returns_404_when_message_missing(seeded_app: TestClient) -> None:
    create = seeded_app.post(
        "/v1/sessions",
        json={"channel": "chat"},
        headers={"Authorization": DEMO_BEARER},
    )
    session_id = create.json()["session_id"]

    response = seeded_app.get(
        f"/v1/sessions/{session_id}/messages/{uuid.uuid4()}",
        headers={"Authorization": DEMO_BEARER},
    )
    assert response.status_code == 404
    envelope = response.json()  # flat, per docs/API_SPEC.md §4 — NOT nested under "detail"
    assert envelope["error"]["code"] == "not_found"


def test_get_message_returns_404_when_session_missing(in_memory_client: TestClient) -> None:
    response = in_memory_client.get(
        f"/v1/sessions/{uuid.uuid4()}/messages/{uuid.uuid4()}",
        headers={"Authorization": DEMO_BEARER},
    )
    assert response.status_code == 404


def test_get_message_requires_bearer_token(seeded_app: TestClient) -> None:
    create = seeded_app.post(
        "/v1/sessions",
        json={"channel": "chat"},
        headers={"Authorization": DEMO_BEARER},
    )
    session_id = create.json()["session_id"]

    response = seeded_app.get(f"/v1/sessions/{session_id}/messages/{uuid.uuid4()}")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Exception handler
# ---------------------------------------------------------------------------


# The kind of message an LLM provider client can carry after a 5xx: the
# upstream body (raw error text + provider identity) embedded in the
# OrchestratorError. This is exactly what must NOT reach the client.
_LEAKY_UPSTREAM_MESSAGE = (
    'Anthropic returned 500: {"error":{"type":"api_error",'
    '"message":"upstream model overloaded at datacenter us-east-1"}}'
)


def _boom_client(
    in_memory_client: TestClient, monkeypatch: pytest.MonkeyPatch, *, message: str
) -> TestClient:
    """Wire the route to an orchestrator that raises OrchestratorError(message)."""
    from app.answer import orchestrator as orch_module
    from app.api.routes import messages as messages_module

    class _BoomOrchestrator:
        def __init__(self, settings, session, **_kwargs: object) -> None:
            del settings, session

        async def ask(self, **_kwargs: object) -> object:
            raise orch_module.OrchestratorError(message)

    monkeypatch.setattr(orch_module, "Orchestrator", _BoomOrchestrator)
    monkeypatch.setattr(messages_module, "Orchestrator", _BoomOrchestrator)
    return in_memory_client


def _post_boom_message(client: TestClient) -> object:
    create = client.post(
        "/v1/sessions",
        json={"channel": "chat"},
        headers={"Authorization": DEMO_BEARER},
    )
    session_id = create.json()["session_id"]
    return client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "How do I configure Claude Code permissions?"},
        headers={"Authorization": DEMO_BEARER},
    )


def test_orchestrator_error_maps_to_500_envelope(
    in_memory_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the orchestrator raises :class:`OrchestratorError` the route
    must return 500 with the standard envelope, not leak the Python
    traceback to the client."""
    client = _boom_client(in_memory_client, monkeypatch, message="LLM provider timed out")
    response = _post_boom_message(client)

    assert response.status_code == 500
    body = response.json()
    # The exception handler returns the envelope at the top level
    # (not nested under ``detail``) so the standard envelope shape is
    # preserved.
    assert body["error"]["code"] == "internal_error"
    assert "unavailable" in body["error"]["message"].lower()
    # The client-facing reason is a fixed generic string — never the raw
    # cause — so provider/upstream detail cannot leak through the envelope.
    assert body["error"]["details"]["reason"] == _GENERIC_LLM_UNAVAILABLE_REASON


def test_orchestrator_error_does_not_leak_upstream_body_to_client(
    in_memory_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Security regression (issue #50): a provider error body embedded in the
    OrchestratorError must NOT reach the caller via ``error.details.reason``,
    yet MUST still be logged server-side for operators."""
    client = _boom_client(in_memory_client, monkeypatch, message=_LEAKY_UPSTREAM_MESSAGE)

    with caplog.at_level("WARNING", logger="citevyn.request"):
        response = _post_boom_message(client)

    assert response.status_code == 500
    reason = response.json()["error"]["details"]["reason"]
    # Client sees ONLY the generic reason...
    assert reason == _GENERIC_LLM_UNAVAILABLE_REASON
    # ...with none of the upstream body, provider identity, or internal detail.
    assert "Anthropic" not in reason
    assert "upstream model overloaded" not in reason
    assert "us-east-1" not in reason
    assert "500" not in reason

    # WHAT THE OPERATOR ACTUALLY RECEIVES (#361). This block used to read
    # ``rec.__dict__["cause"]`` and claim "the full cause is preserved
    # SERVER-SIDE so an SRE can still debug it". That attribute was never
    # rendered -- ``LOG_FORMAT`` dropped every ``extra=`` field -- so the test
    # proved a promise production did not keep. The formatter now emits extras,
    # and the free text is DELIBERATELY still withheld (it is the one field on
    # this path that interpolates raw upstream provider output). What the SRE
    # gets is the exception class plus the request id.
    #
    # RED if ``main.py`` stops passing ``cause_type``, or if ``cause``'s text
    # starts reaching the emitted line.
    from app.core.logging import build_log_formatter

    emitted = "\n".join(build_log_formatter().format(rec) for rec in caplog.records)
    assert "orchestrator_error" in emitted, emitted
    assert "cause_type=" in emitted, (
        f"the log line names no exception class, so the SRE has nothing to go on: {emitted!r}"
    )
    assert _LEAKY_UPSTREAM_MESSAGE not in emitted, (
        f"raw upstream provider text reached the log line: {emitted!r}"
    )
    # Partner: the record still CARRIES the cause, so the assertion above is
    # about suppression at render time, not about the field having vanished.
    assert any(
        _LEAKY_UPSTREAM_MESSAGE in str(rec.__dict__.get("cause", "")) for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# #384: what the request log line ACTUALLY says about the route it served.
#
# These are the load-bearing tests for the per-segment path sweep. Everything
# else about #384 is a unit assertion on ``redact_value``; this drives a real
# request through the real ``RequestIDMiddleware`` and renders the record it
# emitted through ``build_log_formatter()`` -- the object production installs --
# because this repo has repeatedly shipped guards that pinned a constant while
# the emitted artifact went unchecked.
# ---------------------------------------------------------------------------


def _emitted_request_lines(caplog: pytest.LogCaptureFixture) -> str:
    from app.core.logging import build_log_formatter

    formatter = build_log_formatter()
    return "\n".join(
        formatter.format(rec) for rec in caplog.records if rec.name == "citevyn.request"
    )


def test_the_request_log_line_names_the_session_route_it_served(
    in_memory_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """THE #384 REGRESSION, read off the emitted line.

    Before the fix every session route printed ``'path': '/[REDACTED]'`` --
    observed in the Fly v17 logs -- because ``HIGH_ENTROPY_RE``'s character
    class contains ``/`` and swallowed the whole path in one match. An operator
    could not tell an answer POST from a session DELETE.

    A 404 is used on purpose: it still logs ``request_completed``, and it needs
    no seeded catalog, no LLM and no paid call.

    RED if ``PATH_KEYS`` stops containing ``"path"``.
    """
    session_id = uuid.uuid4()
    with caplog.at_level("INFO", logger="citevyn.request"):
        response = in_memory_client.post(
            f"/v1/sessions/{session_id}/messages",
            json={"message": "hello", "answer_style": "short"},
            headers={"Authorization": DEMO_BEARER},
        )
    assert response.status_code == 404

    emitted = _emitted_request_lines(caplog)
    assert "request_completed" in emitted, f"no request line was emitted at all: {emitted!r}"
    assert "'/v1/sessions/[REDACTED]/messages'" in emitted, (
        f"the operator cannot tell which route this was: {emitted!r}"
    )
    # The UUID itself is still blanked -- the fix restores the ROUTE SHAPE, it
    # does not start printing opaque ids into the path.
    assert str(session_id) not in emitted, emitted


def test_the_request_log_line_prints_a_short_route_unchanged(
    in_memory_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """PARTNER proving the test above is not vacuous: a real short route was
    ALREADY printed in full before the fix, so 'the path field reaches the line'
    is not what that test is measuring -- the route shape of a long path is.

    RED if ``app.core.middleware`` stops passing ``path=`` to
    ``build_log_event`` (executed, both these tests fail), or if ``redact_value``
    starts blanking a short path.

    NOT red if ``"path"`` is removed from ``EMITTED_TEXT_KEYS`` -- measured, and
    worth writing down because it is the obvious wrong guess. That allowlist
    governs the ``extra=`` path only; ``RequestIDMiddleware`` logs the
    ``build_log_event`` DICT as the record's message, so it never consults the
    allowlist. ``test_log_extra_fields.py`` covers that other route into the
    same field.
    """
    with caplog.at_level("INFO", logger="citevyn.request"):
        response = in_memory_client.get("/health")
    assert response.status_code == 200

    emitted = _emitted_request_lines(caplog)
    assert "'/health'" in emitted, emitted
    assert "[REDACTED]" not in emitted, f"a short route was over-redacted: {emitted!r}"


def test_the_request_log_line_bounds_a_crafted_many_segment_path(
    in_memory_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """#390, read off the emitted line rather than off ``redact_value``.

    The per-segment sweep of #384 costs one ``re.sub`` per segment inside the
    event loop, on every request, and a 404 matches no route so nothing
    throttles it. Above ``MAX_PATH_SEGMENTS`` the path field is replaced by a
    marker naming the element count.

    THE SECURITY HALF, and the reason this drives a real request instead of
    calling the helper: an obviously-fake 27-character literal is planted in
    the crafted path -- deliberately under ``HIGH_ENTROPY_RE``'s 32-character
    floor, so NO sweep in this module removes it. Under #390's proposed
    whole-string fallback that literal reaches the log line; under the marker
    it cannot, because the marker emits no character of its input.

    ``"/" * n`` is not used as the fixture: httpx rejects a relative URL
    beginning ``//`` before the app ever sees it, so the crafted path is built
    from one-character segments instead. Measured, not guessed.

    RED if the marker arm is removed (the line carries 160 characters of path
    plus ``…<truncated>``), if ``MAX_PATH_SEGMENTS`` is raised above 101, or if
    the arm is replaced by a whole-string sweep (the planted literal appears).
    """
    from app.core.logging import MAX_PATH_SEGMENTS

    fake_key = "re_" + "M5jEIbOReG1FaJDGhnseqgg4"
    assert len(fake_key) == 27
    crafted = "/a" * 100 + "/." + fake_key
    assert len(crafted.split("/")) > MAX_PATH_SEGMENTS

    with caplog.at_level("INFO", logger="citevyn.request"):
        response = in_memory_client.get(crafted)
    assert response.status_code == 404

    emitted = _emitted_request_lines(caplog)
    assert "request_completed" in emitted, f"no request line was emitted at all: {emitted!r}"
    assert re.search(r"'path': '/\[REDACTED\]…<\d+ segments>'", emitted), emitted
    assert fake_key not in emitted, f"a sub-floor literal reached the log line: {emitted!r}"
    # Positive partner: the line still carries the operational content, so this
    # is not "the middleware stopped logging".
    assert "'status_code': 404" in emitted, emitted
    assert "'method': 'GET'" in emitted, emitted
