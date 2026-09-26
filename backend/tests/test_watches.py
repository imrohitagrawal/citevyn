"""Pro watches (ADR-0005 §5, Phase 7C-1): watch a page or a term (a flag, a
model id, a command) and match it against what changed.

Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import watches as watches_module
from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import create_app
from app.models import Base, Membership, Watch
from app.watch.diff import MAX_CHANGED_CHARS, changed_lines
from app.watch.match import matches
from app.watch.pages import WATCHED_PAGES, human_url
from tests.conftest import bot_check_headers

DEMO = {"Authorization": "Bearer local-demo-key"}
PAGE = WATCHED_PAGES[0]


# ---------------------------------------------------------------------------
# What a change records, and what matches
# ---------------------------------------------------------------------------


def test_changed_lines_are_the_added_and_removed_lines_in_page_order() -> None:
    """Turns red if: order is lost, a moved line counts, or a blank line is kept."""
    old = "# A\nkeep\n--old-flag\n\nmoved\n"
    new = "# A\nmoved\nkeep\n--new-flag\n\n\nthird\n"
    assert changed_lines(old, new) == {
        "added": ["--new-flag", "third"],
        "removed": ["--old-flag"],
        "truncated": False,
    }
    # A new blank line between two lines is a real added line, and still left out.
    assert changed_lines("a\nb", "a\n\nb") == {"added": [], "removed": [], "truncated": False}


def test_a_term_deep_in_a_long_paragraph_still_matches() -> None:
    """Vendor Markdown puts a paragraph on one line. Turns red if: lines are cut
    short, so a term past the cut never matches (the review's probe)."""
    old = "Intro " * 60 + "old tail"
    new = "Intro " * 60 + "use --permission-mode plan"
    lines = changed_lines(old, new)
    assert matches("term", "--permission-mode", url="u", lines=lines)
    many = "".join(f"line {i}\n" for i in range(300)) + "--new-flag\n"
    assert matches("term", "--new-flag", url="u", lines=changed_lines("", many))


def test_changed_lines_are_bounded_and_say_so() -> None:
    """Turns red if: a rewrite stores unbounded text, cuts a line in half, or
    hides that later lines were dropped."""
    line = "x" * 1000
    new = "\n".join(f"{i}{line}" for i in range(MAX_CHANGED_CHARS // 1000 + 50))
    got = changed_lines("", new)
    assert got["truncated"] is True
    assert sum(len(x) for x in got["added"]) <= MAX_CHANGED_CHARS
    assert all(x.endswith(line) for x in got["added"])  # whole lines only
    assert changed_lines("a", "b")["truncated"] is False  # partner


def test_a_page_watch_matches_only_its_page() -> None:
    """Turns red if: a page watch fires for another page."""
    assert matches("page", PAGE.url, url=PAGE.url, lines=None)
    assert not matches("page", PAGE.url, url="https://other.example/x.md", lines=None)


def test_a_term_watch_matches_changed_lines_ignoring_case() -> None:
    """Turns red if: case matters, or only added (not removed) lines are searched."""
    lines = {"added": ["Use --Permission-Mode plan"], "removed": ["model: claude-old-1"]}
    assert matches("term", "--permission-mode", url="u", lines=lines)
    assert matches("term", "CLAUDE-OLD-1", url="u", lines=lines)
    assert not matches("term", "--resume", url="u", lines=lines)


def test_a_term_watch_needs_changed_lines() -> None:
    """A change recorded before lines were stored has none. Turns red if: that
    matches every term, or an unknown kind matches anything."""
    assert not matches("term", "--x", url="u", lines=None)
    assert not matches("colour", "--x", url="u", lines={"added": ["--x"]})


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> Generator[pytest.MonkeyPatch, None, None]:
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'w.db'}")
    get_settings.cache_clear()
    rate_limit.reset_limiter()

    async def _init() -> None:
        async with db_module.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield monkeypatch
    get_settings.cache_clear()
    db_module.reset_engine()
    rate_limit.reset_limiter()


def _on(env: pytest.MonkeyPatch) -> None:
    env.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "true")
    get_settings.cache_clear()


def _signed_in(client: TestClient, email: str = "a@example.com") -> str:
    res = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct horse battery"},
        headers={**DEMO, **bot_check_headers(client)},
    )
    assert res.status_code == 201, res.text
    return str(res.json()["user_id"])


def _membership(user_id: str, status: str = "active") -> None:
    async def go() -> None:
        async with get_sessionmaker()() as s:
            existing = (
                await s.execute(select(Membership).where(Membership.account_id == user_id))
            ).scalar_one_or_none()
            if existing is None:
                s.add(
                    Membership(
                        account_id=user_id,
                        stripe_subscription_id=f"sub_{user_id}",
                        plan="pro",
                        status=status,
                        cancel_at_period_end=False,
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
            else:
                existing.status = status
            await s.commit()

    asyncio.run(go())


def _watch(client: TestClient, kind: str, value: str) -> Any:
    return client.post("/v1/me/watches", json={"kind": kind, "value": value}, headers=DEMO)


def _rows() -> list[Watch]:
    async def go() -> list[Watch]:
        async with get_sessionmaker()() as s:
            return list((await s.execute(select(Watch))).scalars())

    return asyncio.run(go())


def test_with_the_setting_off_there_are_no_watches(env: pytest.MonkeyPatch) -> None:
    """Turns red if: any watch route answers while the access model is off (a
    REAL watch is used for delete, so a missing check cannot hide behind a
    'not found')."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _membership(user)
        wid = _watch(client, "term", "--x").json()["watch_id"]
        env.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "false")
        get_settings.cache_clear()
        assert _watch(client, "term", "--y").status_code == 404
        assert client.get("/v1/me/watches", headers=DEMO).status_code == 404
        assert client.delete(f"/v1/me/watches/{wid}", headers=DEMO).status_code == 404
    assert len(_rows()) == 1


def test_a_free_account_cannot_make_a_watch(env: pytest.MonkeyPatch) -> None:
    """Turns red if: watch_alerts is granted to Free."""
    _on(env)
    with TestClient(create_app()) as client:
        _signed_in(client)
        res = _watch(client, "term", "--x")
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "plan_required"
    assert _rows() == []


def test_pro_watches_a_page_by_either_url_and_a_term(env: pytest.MonkeyPatch) -> None:
    """The readable URL is stored as the watched Markdown twin, and a repeat
    returns the same watch. Turns red if: either URL is refused, the page is
    stored as typed, or a repeat makes a second row."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _membership(user)
        page = _watch(client, "page", human_url(PAGE.url))
        assert page.status_code == 201, page.text
        assert page.json()["value"] == PAGE.url
        assert PAGE.title in page.json()["label"]
        again = _watch(client, "page", PAGE.url)
        assert (again.status_code, again.json()["watch_id"]) == (200, page.json()["watch_id"])
        term = _watch(client, "term", "  --Permission-Mode  ")
        assert (term.status_code, term.json()["value"]) == (201, "--permission-mode")
        same = _watch(client, "term", "--PERMISSION-MODE")  # one watch, any case
        assert (same.status_code, same.json()["watch_id"]) == (200, term.json()["watch_id"])
        slash = _watch(client, "page", human_url(PAGE.url) + "/#section")  # same page
        assert (slash.status_code, slash.json()["watch_id"]) == (200, page.json()["watch_id"])
        listed = client.get("/v1/me/watches", headers=DEMO).json()
        assert [w["kind"] for w in listed["watches"]] == ["page", "term"]
        assert len(listed["pages"]) == len(WATCHED_PAGES)
    assert len(_rows()) == 2


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("page", "https://evil.example/x.md"),
        ("term", "x"),
        ("term", "y" * 65),
        ("term", "a‮b"),
        ("term", "two\nlines"),
        ("colour", "--x"),
    ],
)
def test_an_invalid_watch_is_refused(env: pytest.MonkeyPatch, kind: str, value: str) -> None:
    """Turns red if: an unwatched page, a too-short or too-long term, a hidden
    or multi-line term, or an unknown kind is stored."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _membership(user)
        assert _watch(client, kind, value).status_code == 422
    assert _rows() == []


def test_there_is_a_cap_on_watches(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the cap is not enforced (or is off by one)."""
    _on(env)
    env.setattr(watches_module, "MAX_WATCHES", 1)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _membership(user)
        assert _watch(client, "term", "--a").status_code == 201
        res = _watch(client, "term", "--b")
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "too_many_watches"
        # Watching something you already watch is not a new watch, even at the cap.
        assert _watch(client, "term", "--A").status_code == 200


def test_a_lapsed_account_can_still_list_and_remove(env: pytest.MonkeyPatch) -> None:
    """Turns red if: list or remove require Pro."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _membership(user)
        wid = _watch(client, "term", "--a").json()["watch_id"]
        _membership(user, status="canceled")
        assert _watch(client, "term", "--b").status_code == 403
        assert len(client.get("/v1/me/watches", headers=DEMO).json()["watches"]) == 1
        assert client.delete(f"/v1/me/watches/{wid}", headers=DEMO).status_code == 204
    assert _rows() == []


def test_a_stranger_cannot_see_or_remove_my_watches(env: pytest.MonkeyPatch) -> None:
    """Turns red if: list or remove skip the owner check."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        _membership(user)
        wid = _watch(client, "term", "--a").json()["watch_id"]
    with TestClient(create_app()) as other:
        _signed_in(other, "b@example.com")
        assert other.get("/v1/me/watches", headers=DEMO).json()["watches"] == []
        assert other.delete(f"/v1/me/watches/{wid}", headers=DEMO).status_code == 404
        assert other.delete("/v1/me/watches/not-a-uuid", headers=DEMO).status_code == 404
    assert len(_rows()) == 1
