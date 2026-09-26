"""The docs watcher (ADR-0005 §5 "what changed" alerts, Phase 7A).

A run fetches a fixed list of vendor documentation pages (their Markdown
versions), compares each with the last snapshot, and records a change when the
text differs. No network in these tests: every request goes to a fake
transport. Each test says which change turns it red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, DocChange, DocSnapshot
from app.watch.diff import normalize, section_changes
from app.watch.fetch import FetchError, RobotsCache, fetch_page
from app.watch.pages import WatchedPage
from app.watch.runner import run_watch

HOSTS = frozenset({"docs.example.com"})
UA = "CiteVyn-docs-watcher/test"


# ---------------------------------------------------------------------------
# The diff
# ---------------------------------------------------------------------------


def test_normalize_ignores_whitespace_noise() -> None:
    """Turns red if: trailing spaces, CRLF or extra blank lines count as a change."""
    assert normalize("# A\r\nline  \r\n\r\n\r\n\r\nnext\n\n") == normalize("# A\nline\n\nnext")


def test_a_changed_section_is_named_with_its_heading_path() -> None:
    """Turns red if: the change is not attributed to its section, or counts are wrong."""
    old = "# Limits\n## Tier 1\n50 requests per minute\n## Tier 2\n1000 requests per minute\n"
    new = "# Limits\n## Tier 1\n60 requests per minute\n## Tier 2\n1000 requests per minute\n"
    s = section_changes(old, new)
    assert s["changed"] == ["Limits > Tier 1"]
    assert s["added"] == [] and s["removed"] == []
    assert (s["lines_added"], s["lines_removed"]) == (1, 1)


def test_added_and_removed_sections_are_listed_separately() -> None:
    """Turns red if: a new or deleted section is reported as merely changed."""
    old = "# Models\n## Old model\ntext\n## Kept\nsame\n"
    new = "# Models\n## Kept\nsame\n## New model\nfresh\n"
    s = section_changes(old, new)
    assert s["added"] == ["Models > New model"]
    assert s["removed"] == ["Models > Old model"]
    assert s["changed"] == []


def test_identical_text_has_no_changes() -> None:
    """Turns red if: an unchanged page produces a summary with content."""
    s = section_changes("# A\nx\n", "# A\nx\n")
    assert (s["changed"], s["added"], s["removed"], s["lines_added"], s["lines_removed"]) == (
        [],
        [],
        [],
        0,
        0,
    )


def test_headings_inside_code_blocks_are_not_sections() -> None:
    """A shell comment in a fenced block ('# install') is not a heading.
    Turns red if: fenced lines are parsed as headings."""
    old = "# Setup\n```bash\n# install\nnpm i\n```\n"
    new = "# Setup\n```bash\n# install\nnpm i -g\n```\n"
    assert section_changes(old, new)["changed"] == ["Setup"]


def test_the_section_lists_are_capped() -> None:
    """A page rewritten end to end must not produce an unbounded summary.
    Turns red if: the cap is dropped."""
    old = "".join(f"# S{i}\nold\n" for i in range(100))
    new = "".join(f"# S{i}\nnew\n" for i in range(100))
    s = section_changes(old, new)
    assert len(s["changed"]) == 20
    assert s["more"] == 80


# ---------------------------------------------------------------------------
# The fetcher: only allowlisted hosts, only https, robots.txt obeyed, bounded
# ---------------------------------------------------------------------------


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)  # type: ignore[arg-type]


def _site(pages: dict[str, httpx.Response], robots: str = "User-agent: *\nAllow: /\n") -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return pages.get(str(request.url), httpx.Response(404))

    return handler


async def _fetch(client: httpx.AsyncClient, url: str, **kw: object) -> str:
    return await fetch_page(
        client,
        url,
        allowed_hosts=HOSTS,
        user_agent=UA,
        max_bytes=int(kw.get("max_bytes", 1_000_000)),  # type: ignore[arg-type]
        robots=RobotsCache(client, UA),
    )


async def test_a_markdown_page_is_fetched_with_our_user_agent() -> None:
    """Turns red if: the page text is not returned, or the User-Agent is not ours."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text="# Page\nbody", headers={"content-type": "text/markdown"})

    async with _client(handler) as c:
        assert await _fetch(c, "https://docs.example.com/p.md") == "# Page\nbody"
    assert seen and all(ua == UA for ua in seen)


async def test_robots_txt_is_obeyed() -> None:
    """Turns red if: a page robots.txt disallows is fetched."""
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        fetched.append(request.url.path)
        return httpx.Response(200, text="x", headers={"content-type": "text/markdown"})

    async with _client(handler) as c:
        with pytest.raises(FetchError, match="robots"):
            await _fetch(c, "https://docs.example.com/private/p.md")
    assert fetched == []


async def test_a_host_not_on_the_list_is_never_fetched() -> None:
    """Turns red if: a URL outside the allowlisted hosts is requested."""
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, text="x")

    async with _client(handler) as c:
        with pytest.raises(FetchError, match="host"):
            await _fetch(c, "https://evil.example/p.md")
    assert called == []


async def test_plain_http_is_refused() -> None:
    """Turns red if: an http:// URL is fetched."""
    async with _client(_site({})) as c:
        with pytest.raises(FetchError, match="https"):
            await _fetch(c, "http://docs.example.com/p.md")


async def test_a_redirect_to_another_host_is_refused() -> None:
    """Turns red if: a redirect leads the watcher off the allowlisted hosts."""
    handler = _site(
        {
            "https://docs.example.com/p.md": httpx.Response(
                301, headers={"location": "https://evil.example/p.md"}
            )
        }
    )
    async with _client(handler) as c:
        with pytest.raises(FetchError, match="host"):
            await _fetch(c, "https://docs.example.com/p.md")


async def test_a_redirect_on_the_same_host_is_followed() -> None:
    """Turns red if: a same-host redirect (a moved page) fails the fetch."""
    handler = _site(
        {
            "https://docs.example.com/old.md": httpx.Response(301, headers={"location": "/new.md"}),
            "https://docs.example.com/new.md": httpx.Response(
                200, text="moved", headers={"content-type": "text/markdown"}
            ),
        }
    )
    async with _client(handler) as c:
        assert await _fetch(c, "https://docs.example.com/old.md") == "moved"


async def test_a_redirect_loop_stops() -> None:
    """Turns red if: redirects are followed without a limit."""
    handler = _site(
        {"https://docs.example.com/a.md": httpx.Response(302, headers={"location": "/a.md"})}
    )
    async with _client(handler) as c:
        with pytest.raises(FetchError, match="redirect"):
            await _fetch(c, "https://docs.example.com/a.md")


async def test_a_page_over_the_size_cap_is_refused() -> None:
    """Turns red if: an oversized body is accepted."""
    handler = _site(
        {
            "https://docs.example.com/big.md": httpx.Response(
                200, text="x" * 2000, headers={"content-type": "text/markdown"}
            )
        }
    )
    async with _client(handler) as c:
        with pytest.raises(FetchError, match="size"):
            await _fetch(c, "https://docs.example.com/big.md", max_bytes=1000)


async def test_a_non_text_response_is_refused() -> None:
    """A binary or HTML-app response is not a Markdown page.
    Turns red if: a non-text content type is accepted."""
    handler = _site(
        {
            "https://docs.example.com/p.md": httpx.Response(
                200, content=b"\x00\x01", headers={"content-type": "application/octet-stream"}
            )
        }
    )
    async with _client(handler) as c:
        with pytest.raises(FetchError, match="content type"):
            await _fetch(c, "https://docs.example.com/p.md")


async def test_an_error_status_is_a_fetch_error() -> None:
    """Turns red if: a 404 or 500 body is stored as the page."""
    async with _client(_site({})) as c:
        with pytest.raises(FetchError, match="404"):
            await _fetch(c, "https://docs.example.com/missing.md")


async def test_robots_is_read_once_per_host() -> None:
    """Turns red if: robots.txt is fetched again for every page."""
    robots_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal robots_calls
        if request.url.path == "/robots.txt":
            robots_calls += 1
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text="x", headers={"content-type": "text/plain"})

    async with _client(handler) as c:
        cache = RobotsCache(c, UA)
        for p in ("a", "b", "c"):
            await fetch_page(
                c,
                f"https://docs.example.com/{p}.md",
                allowed_hosts=HOSTS,
                user_agent=UA,
                max_bytes=1000,
                robots=cache,
            )
    assert robots_calls == 1


async def test_a_robots_txt_that_cannot_be_read_blocks_the_host() -> None:
    """A 5xx or network failure on robots.txt means we do not know the rules:
    fetch nothing. (A 404 means there are no rules: allowed.)
    Turns red if: an unreadable robots.txt is treated as permission."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(503)
        return httpx.Response(200, text="x", headers={"content-type": "text/plain"})

    async with _client(handler) as c:
        with pytest.raises(FetchError, match="robots"):
            await _fetch(c, "https://docs.example.com/p.md")

    def no_robots(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    async with _client(no_robots) as c:
        assert await _fetch(c, "https://docs.example.com/p.md") == "ok"  # partner


# ---------------------------------------------------------------------------
# One run against the database
# ---------------------------------------------------------------------------


PAGES = (
    WatchedPage("claude_api", "Limits", "https://docs.example.com/limits.md"),
    WatchedPage("gemini_api", "Models", "https://docs.example.com/models.md"),
)
T0 = datetime(2026, 9, 27, 6, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Any) -> Any:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'w.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _serving(texts: dict[str, str | int]) -> object:
    """A fake site: a str is the page body, an int is an error status."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        body = texts.get(str(request.url), 404)
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(200, text=body, headers={"content-type": "text/markdown"})

    return handler


async def _run(db: Any, texts: dict[str, str | int], now: datetime) -> Any:
    async with _client(_serving(texts)) as c, db() as s:
        return await run_watch(s, c, PAGES, user_agent=UA, now=now)


async def _rows(db: Any, model: Any) -> list[Any]:
    async with db() as s:
        return list((await s.execute(select(model))).scalars())


LIMITS = "https://docs.example.com/limits.md"
MODELS = "https://docs.example.com/models.md"


async def test_the_first_run_is_a_baseline_with_no_changes(db: Any) -> None:
    """Turns red if: a page seen for the first time is reported as changed."""
    stats = await _run(db, {LIMITS: "# L\n50 rpm", MODELS: "# M\nflash"}, T0)
    assert (stats.checked, stats.baseline, stats.changed) == (2, 2, 0)
    assert await _rows(db, DocChange) == []
    assert {s.url for s in await _rows(db, DocSnapshot)} == {LIMITS, MODELS}


async def test_a_changed_page_records_one_change_with_its_sections(db: Any) -> None:
    """Turns red if: a real change is missed, recorded twice, or unsummarised."""
    await _run(db, {LIMITS: "# L\n## Tier 1\n50 rpm", MODELS: "# M\nflash"}, T0)
    stats = await _run(
        db, {LIMITS: "# L\n## Tier 1\n60 rpm", MODELS: "# M\nflash"}, T0 + timedelta(days=1)
    )
    assert (stats.changed, stats.unchanged) == (1, 1)
    [change] = await _rows(db, DocChange)
    assert (change.url, change.product_area, change.title) == (LIMITS, "claude_api", "Limits")
    assert change.summary["changed"] == ["L > Tier 1"]
    assert change.detected_at.replace(tzinfo=UTC) == T0 + timedelta(days=1)
    snap = next(s for s in await _rows(db, DocSnapshot) if s.url == LIMITS)
    assert snap.content == "# L\n## Tier 1\n60 rpm"  # the snapshot moves on
    assert change.new_sha256 == snap.content_sha256 != change.old_sha256


async def test_an_unchanged_page_records_nothing(db: Any) -> None:
    """Turns red if: an identical re-fetch (even with whitespace noise) is a change."""
    await _run(db, {LIMITS: "# L\n50 rpm", MODELS: "# M\nflash"}, T0)
    stats = await _run(
        db, {LIMITS: "# L  \r\n50 rpm\n\n\n", MODELS: "# M\nflash"}, T0 + timedelta(days=1)
    )
    assert (stats.changed, stats.unchanged) == (0, 2)
    assert await _rows(db, DocChange) == []


async def test_a_failed_fetch_keeps_the_old_text_and_says_why(db: Any) -> None:
    """A failure is never a change, and the next good fetch compares with the
    last GOOD text. Turns red if: a failure clears the snapshot, records a
    change, or hides its reason."""
    await _run(db, {LIMITS: "# L\n50 rpm", MODELS: "# M\nflash"}, T0)
    stats = await _run(db, {LIMITS: 503, MODELS: "# M\nflash"}, T0 + timedelta(days=1))
    assert (stats.failed, stats.changed, stats.unchanged) == (1, 0, 1)  # the other page still ran
    snap = next(s for s in await _rows(db, DocSnapshot) if s.url == LIMITS)
    assert snap.content == "# L\n50 rpm"
    assert snap.last_error == "HTTP 503"
    await _run(db, {LIMITS: "# L\n50 rpm", MODELS: "# M\nflash"}, T0 + timedelta(days=2))
    assert await _rows(db, DocChange) == []  # back to the same text: still no change
    snap = next(s for s in await _rows(db, DocSnapshot) if s.url == LIMITS)
    assert snap.last_error is None


async def test_a_page_failing_on_its_first_run_gets_a_baseline_later(db: Any) -> None:
    """Turns red if: a page that failed first time is later reported as changed."""
    await _run(db, {LIMITS: 500, MODELS: "# M\nflash"}, T0)
    stats = await _run(db, {LIMITS: "# L\n50 rpm", MODELS: "# M\nflash"}, T0 + timedelta(days=1))
    assert (stats.baseline, stats.changed) == (1, 0)


def test_the_cli_refuses_to_run_while_off(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """Default off: no fetch happens until the owner turns it on.
    Turns red if: the command runs (or reaches the network) with the setting off."""
    from app.core.config import get_settings
    from app.worker import cli

    monkeypatch.delenv("CITEVYN_DOCS_WATCH_ENABLED", raising=False)
    get_settings.cache_clear()

    async def boom(*_a: object, **_k: object) -> int:
        raise AssertionError("the watcher ran while off")

    monkeypatch.setattr(cli, "_watch", boom)
    assert cli.main(["watch"]) == 2
    assert "off" in capsys.readouterr().out
    get_settings.cache_clear()


def test_the_watch_list_is_https_markdown_on_a_small_set_of_hosts() -> None:
    """Turns red if: a watched URL is not https, or the host set grows unnoticed."""
    from app.watch.pages import WATCHED_PAGES, watched_hosts

    assert all(p.url.startswith("https://") for p in WATCHED_PAGES)
    assert all(p.url.endswith((".md", ".md.txt")) for p in WATCHED_PAGES)
    assert watched_hosts() == {
        "platform.claude.com",
        "code.claude.com",
        "learn.chatgpt.com",
        "ai.google.dev",
    }
    assert {p.product_area for p in WATCHED_PAGES} == {
        "claude_api",
        "claude_code",
        "codex",
        "gemini_api",
    }


# ---------------------------------------------------------------------------
# Review round 1: every finding below failed (or could fail) before its fix
# ---------------------------------------------------------------------------


def _redirecting(location: str, *, robots: str = "User-agent: *\nAllow: /\n") -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if request.url.path == "/p.md":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, text="target", headers={"content-type": "text/markdown"})

    return handler


async def test_a_malformed_redirect_is_a_fetch_error_not_a_crash() -> None:
    """httpx.InvalidURL is not an httpx.HTTPError. Turns red if: it escapes."""
    async with _client(_redirecting("https:\\\\evil.example/x")) as c:
        with pytest.raises(FetchError, match="malformed"):
            await _fetch(c, "https://docs.example.com/p.md")


async def test_one_malformed_redirect_does_not_stop_the_run(db: Any) -> None:
    """Turns red if: one bad page aborts the pages after it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if str(request.url) == LIMITS:
            return httpx.Response(302, headers={"location": "https:\\\\evil.example/x"})
        return httpx.Response(200, text="# M\nok", headers={"content-type": "text/markdown"})

    async with _client(handler) as c, db() as s:
        stats = await run_watch(s, c, PAGES, user_agent=UA, now=T0)
    assert (stats.failed, stats.baseline) == (1, 1)


async def test_a_redirect_target_is_checked_against_robots_txt() -> None:
    """Turns red if: only the first URL is checked against robots.txt."""
    async with _client(
        _redirecting("/private/p.md", robots="User-agent: *\nDisallow: /private/\n")
    ) as c:
        with pytest.raises(FetchError, match="disallowed by robots"):
            await _fetch(c, "https://docs.example.com/p.md")


async def test_a_redirect_to_another_port_is_refused() -> None:
    """Turns red if: an allowlisted host on a non-default port is fetched."""
    async with _client(_redirecting("https://docs.example.com:8443/x")) as c:
        with pytest.raises(FetchError, match="port"):
            await _fetch(c, "https://docs.example.com/p.md")


async def test_redirects_are_never_followed_by_the_client() -> None:
    """Even a client built to follow redirects must not carry the watcher off
    the allowlist. Turns red if: the stream call leaves redirects to the client."""
    transport = httpx.MockTransport(_redirecting("https://evil.example/x"))  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as c:
        with pytest.raises(FetchError, match="host"):
            await _fetch(c, "https://docs.example.com/p.md")


async def test_uncompressed_transfer_is_requested() -> None:
    """The size cap counts decompressed bytes, so a compressed page could hold
    far more than the cap in memory. Turns red if: identity is not requested."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding", ""))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text="x", headers={"content-type": "text/plain"})

    async with _client(handler) as c:
        await _fetch(c, "https://docs.example.com/p.md")
    assert seen and all(v == "identity" for v in seen)


async def test_robots_txt_redirects_are_followed_on_watched_hosts_only() -> None:
    """RFC 9309: follow robots.txt redirects (at least 5). Turns red if: a
    same-host robots redirect disables the host, or an off-host one is followed."""

    def moved(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(301, headers={"location": "/robots-v2.txt"})
        if request.url.path == "/robots-v2.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    async with _client(moved) as c:
        assert await _fetch(c, "https://docs.example.com/p.md") == "ok"
        with pytest.raises(FetchError, match="disallowed"):
            await _fetch(c, "https://docs.example.com/private/p.md")

    def off_host(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(301, headers={"location": "https://evil.example/robots.txt"})
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    async with _client(off_host) as c:
        with pytest.raises(FetchError, match="robots.txt unreadable"):
            await _fetch(c, "https://docs.example.com/p.md")


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_a_robots_txt_the_server_will_not_give_skips_the_host(status: int) -> None:
    """429 is 'slow down', not 'no rules'. Turns red if: 429 counts as permission,
    or the reason reads as a robots.txt disallow."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(status)
        return httpx.Response(200, text="x", headers={"content-type": "text/plain"})

    async with _client(handler) as c:
        with pytest.raises(FetchError, match="robots.txt unreadable"):
            await _fetch(c, "https://docs.example.com/p.md")


async def test_an_oversized_robots_txt_skips_the_host() -> None:
    """Turns red if: robots.txt is read without a size cap."""
    from app.watch.fetch import MAX_ROBOTS_BYTES

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="#" * (MAX_ROBOTS_BYTES + 10))
        return httpx.Response(200, text="x", headers={"content-type": "text/plain"})

    async with _client(handler) as c:
        with pytest.raises(FetchError, match="robots.txt unreadable"):
            await _fetch(c, "https://docs.example.com/p.md")


async def test_a_page_that_never_finishes_hits_the_deadline(
    db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turns red if: a trickling server can stall the run (no overall deadline)."""
    import asyncio

    from app.watch import runner

    monkeypatch.setattr(runner, "PAGE_DEADLINE_SECONDS", 0.05)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if str(request.url) == LIMITS:
            await asyncio.sleep(5)
        return httpx.Response(200, text="# M\nok", headers={"content-type": "text/markdown"})

    async with _client(handler) as c, db() as s:
        stats = await run_watch(s, c, PAGES, user_agent=UA, now=T0)
    assert (stats.failed, stats.baseline) == (1, 1)
    snap = next(x for x in await _rows(db, DocSnapshot) if x.url == LIMITS)
    assert snap.last_error is not None and "within" in snap.last_error


def test_the_diff_stays_fast_on_hostile_pages() -> None:
    """Each shape took seconds to hours before the fix (measured by the breaker:
    a 4 KB heading of spaces 37 s; 130 KB of repeated short lines 281 s).
    Turns red if: the heading regex backtracks, duplicate headings are searched,
    or line counting is quadratic."""
    import time

    shapes = [
        ("# x" + " " * 4000 + "y\n", "# x" + " " * 4000 + "z\n"),
        (
            "".join(f"line {i % 7}\n" for i in range(20000)),
            "".join(f"line {i % 5}\n" for i in range(20000)),
        ),
        ("# FAQ\nx\n" * 4000, "# FAQ\ny\n" * 4000),
    ]
    for old, new in shapes:
        start = time.perf_counter()
        section_changes(old, new)
        assert time.perf_counter() - start < 2.0


def test_line_counts_see_every_content_line() -> None:
    """A '---' rule or a '++x' line is content, not a diff header.
    Turns red if: such lines are left out of the counts."""
    s = section_changes("# A\n---\ntext\n", "# A\ntext\n++x\n")
    assert (s["lines_removed"], s["lines_added"]) == (1, 1)


def test_a_fence_closes_only_on_a_matching_fence() -> None:
    """A ~~~ line inside a ``` block does not close it; the heading after the
    real close is a section. Turns red if: any fence toggles the state."""
    old = "# A\n```\n~~~\n# not a heading\n```\n# Real\nold\n"
    new = "# A\n```\n~~~\n# not a heading\n```\n# Real\nnew\n"
    assert section_changes(old, new)["changed"] == ["Real"]


def test_the_cli_runs_every_page_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: Any, tmp_path: Any
) -> None:
    """The enabled path, end to end on fakes: exit 0 when every page is fetched,
    3 when one fails. Turns red if: the wiring (client, session, redirects off,
    the summary line, the exit codes) breaks."""
    import asyncio

    from app.core.config import get_settings
    from app.watch import pages as pages_module
    from app.worker import cli

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")

    async def init() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: maker)
    monkeypatch.setattr(pages_module, "WATCHED_PAGES", PAGES)
    status = {"code": 200}
    clients: list[httpx.AsyncClient] = []
    real = httpx.AsyncClient

    def fake_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            if str(request.url) == LIMITS and status["code"] != 200:
                return httpx.Response(status["code"])
            return httpx.Response(200, text="# P\nok", headers={"content-type": "text/markdown"})

        client = real(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setenv("CITEVYN_DOCS_WATCH_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert cli.main(["watch"]) == 0
        assert (
            "checked 2: 0 changed, 0 unchanged, 2 first seen, 0 failed" in capsys.readouterr().out
        )
        assert clients and clients[0].follow_redirects is False
        status["code"] = 503
        assert cli.main(["watch"]) == 3
        assert "1 failed" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()
        asyncio.run(engine.dispose())


def test_two_sections_with_the_same_heading_stay_separate() -> None:
    """Two '## Example' sections under one parent: a change in the FIRST must be
    reported. Turns red if: the second overwrites the first (one shared key)."""
    old = "# API\n## Example\nfirst old\n## Example\nsecond\n"
    new = "# API\n## Example\nfirst new\n## Example\nsecond\n"
    assert section_changes(old, new)["changed"] == ["API > Example"]
