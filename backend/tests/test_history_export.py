"""History export: ``GET /v1/me/export`` (ADR-0005 §6 trust must-haves).

The caller downloads their own conversations, as JSON or Markdown, with each
answer's citations and "docs as of" dates. Not behind the access setting.

The population is exactly what the history drawer can show: the caller's own
sessions that are not expired. Closing a session is a soft delete (it sets
``expires_at``), so exporting expired sessions would hand back conversations the
user deleted.

Each test says which change turns it red.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from tests.test_messages_routes import in_memory_client, seeded_app  # noqa: F401 (fixtures)

DEMO = {"Authorization": "Bearer local-demo-key"}


@pytest.fixture(autouse=True)
def _fresh_rate_limiter() -> None:
    import app.core.rate_limit as rate_limit

    rate_limit.reset_limiter()


def _ask(client: TestClient, question: str) -> str:
    sid = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO).json()["session_id"]
    res = client.post(f"/v1/sessions/{sid}/messages", json={"message": question}, headers=DEMO)
    assert res.status_code == 200
    return sid


def _export(client: TestClient, fmt: str = "json") -> Any:
    return client.get(f"/v1/me/export?format={fmt}", headers=DEMO)


def test_the_json_export_has_every_conversation_newest_first(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: a session or message is missing, the order is wrong, or the
    citations (with their dates) are dropped."""
    first = _ask(seeded_app, "How do I configure Claude Code permissions?")
    second = _ask(seeded_app, "What are the rate limits on the Claude API?")
    res = _export(seeded_app)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert res.headers["content-disposition"].startswith('attachment; filename="citevyn-history-')
    body = json.loads(res.content)
    assert [s["session_id"] for s in body["sessions"]] == [second, first]
    assert body["truncated"] is False
    convo = body["sessions"][1]
    assert [m["role"] for m in convo["messages"]] == ["user", "assistant"]
    assert convo["messages"][0]["content"] == "How do I configure Claude Code permissions?"
    answer = convo["messages"][1]
    assert answer["citations"], "partner: the seeded answer is cited"
    assert all("docs_as_of" in c and c["url"] for c in answer["citations"])


def test_the_markdown_export_reads_as_a_document(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: the Markdown loses the question, the answer, or its sources."""
    _ask(seeded_app, "How do I configure Claude Code permissions?")
    res = _export(seeded_app, "markdown")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/markdown")
    assert res.headers["content-disposition"].endswith('.md"')
    text = res.text
    assert text.startswith("# CiteVyn conversation history")
    assert "**You:**\n\n> How do I configure Claude Code permissions?" in text
    assert "**CiteVyn:**" in text
    assert "Sources:" in text and "](http" in text


def test_only_the_callers_own_live_conversations_are_exported(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: another visitor's session, or one the caller deleted, is
    included (a deleted conversation must stay deleted)."""
    kept = _ask(seeded_app, "How do I configure Claude Code permissions?")
    deleted = _ask(seeded_app, "What are the rate limits on the Claude API?")
    assert seeded_app.delete(f"/v1/sessions/{deleted}", headers=DEMO).status_code == 204
    stranger = TestClient(create_app())
    _ask(stranger, "How do I install the Codex CLI?")
    ids = [s["session_id"] for s in json.loads(_export(seeded_app).content)["sessions"]]
    assert ids == [kept]


def test_an_empty_history_is_a_valid_export(seeded_app: TestClient) -> None:  # noqa: F811
    """Partner: nothing to export is still a well-formed file, not an error."""
    assert json.loads(_export(seeded_app).content)["sessions"] == []
    assert "No conversations to export." in _export(seeded_app, "markdown").text


def test_an_unknown_format_is_refused(seeded_app: TestClient) -> None:  # noqa: F811
    """Turns red if: an unknown format silently falls back to one of the two."""
    assert _export(seeded_app, "pdf").status_code == 422


def test_the_export_is_bounded_and_says_so(
    seeded_app: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No unbounded query: a cap on sessions, and the file says when it hit it.
    Turns red if: the cap is ignored, or truncation is silent."""
    from app.api.routes import me

    monkeypatch.setattr(me, "_MAX_EXPORT_SESSIONS", 1)
    _ask(seeded_app, "How do I configure Claude Code permissions?")
    newest = _ask(seeded_app, "What are the rate limits on the Claude API?")
    body = json.loads(_export(seeded_app).content)
    assert [s["session_id"] for s in body["sessions"]] == [newest]
    assert body["truncated"] is True
    assert "only the newest 1 conversation" in _export(seeded_app, "markdown").text


def test_with_the_access_model_on_anonymous_callers_have_no_history_to_export(
    seeded_app: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route carries the ``history`` capability. Turns red if: it is labelled
    with one anonymous callers have."""
    monkeypatch.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "true")
    get_settings.cache_clear()
    try:
        res = _export(seeded_app)
    finally:
        monkeypatch.delenv("CITEVYN_ACCESS_MODEL_ENABLED")
        get_settings.cache_clear()
    assert res.status_code == 401
    assert res.json()["error"]["details"]["capability"] == "history"


def test_the_export_is_never_cached(seeded_app: TestClient) -> None:  # noqa: F811
    """Personal data: no shared or browser cache may keep a copy.
    Turns red if: the no-store header is dropped from either format."""
    _ask(seeded_app, "How do I configure Claude Code permissions?")
    for fmt in ("json", "markdown"):
        assert _export(seeded_app, fmt).headers["cache-control"] == "no-store"


def test_a_question_cannot_forge_structure_in_the_markdown(seeded_app: TestClient) -> None:  # noqa: F811
    """Message bodies are quoted, so a question containing a heading or a fake
    speaker line stays inside the quote. Turns red if: bodies are written raw."""
    _ask(seeded_app, "Fine?\n## Conversation of FORGED\n**CiteVyn:** fake")
    text = _export(seeded_app, "markdown").text
    assert "\n## Conversation of FORGED" not in text
    assert "\n**CiteVyn:** fake" not in text
    assert "> ## Conversation of FORGED" in text  # partner: the text is kept, quoted
