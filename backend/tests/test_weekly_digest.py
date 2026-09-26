"""The weekly "what changed" digest (ADR-0005 §5, Phase 7B).

Opt-in, verified addresses only, one email per account per week, a one-click
unsubscribe that a mail scanner cannot trigger, and nothing at all while off.
No real email: a recording client stands in for delivery.

Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import db as db_module
from app.core.config import Settings, get_settings
from app.core.db import get_sessionmaker
from app.core.email_client import (
    EmailDeliveryError,
    EmailMessage,
    FileOutboxEmailClient,
    ResendEmailClient,
)
from app.main import create_app
from app.models import Base, DigestSend, DocChange, User
from app.services.digest import digest_message, send_weekly_digest, week_start
from tests.conftest import bot_check_headers

DEMO = {"Authorization": "Bearer local-demo-key"}
NOW = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)  # a Wednesday


class _Recorder:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_for = fail_for or set()

    async def send(self, message: EmailMessage) -> None:
        if message.to_addr in self.fail_for:
            raise EmailDeliveryError("provider said no")
        self.sent.append(message)


@pytest.fixture
def env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> Generator[pytest.MonkeyPatch, None, None]:
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
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


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _add_user(
    email: str, *, verified: bool = True, opted_in: bool = True, uid: str | None = None
) -> str:
    async def go() -> str:
        async with get_sessionmaker()() as s:
            user_id = uid or f"usr_{uuid.uuid4().hex[:12]}"
            s.add(
                User(
                    user_id=user_id,
                    role="demo_user",
                    created_at=NOW,
                    email=email,
                    email_verified_at=NOW if verified else None,
                    digest_opt_in_at=NOW if opted_in else None,
                )
            )
            await s.commit()
            return user_id

    return _run(go())


def _add_change(
    title: str = "Rate limits", *, area: str = "claude_api", at: datetime = NOW
) -> None:
    async def go() -> None:
        async with get_sessionmaker()() as s:
            s.add(
                DocChange(
                    url=f"https://platform.claude.com/docs/{title.lower().replace(' ', '-')}.md",
                    product_area=area,
                    title=title,
                    detected_at=at,
                    old_sha256="a" * 64,
                    new_sha256="b" * 64,
                    summary={
                        "changed": ["Limits > Tier 1"],
                        "added": [],
                        "removed": [],
                        "more": 0,
                        "lines_added": 1,
                        "lines_removed": 1,
                    },
                )
            )
            await s.commit()

    _run(go())


def _send(client: Any, *, now: datetime = NOW, **settings: Any) -> Any:
    async def go() -> Any:
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        async with get_sessionmaker()() as s:
            stats = await send_weekly_digest(
                s, client, Settings(**settings), now=now, sleep=fake_sleep
            )
        stats.slept = slept  # type: ignore[attr-defined]
        return stats

    return _run(go())


def _sends() -> list[DigestSend]:
    async def go() -> list[DigestSend]:
        async with get_sessionmaker()() as s:
            return list((await s.execute(select(DigestSend))).scalars())

    return _run(go())


# ---------------------------------------------------------------------------
# The email
# ---------------------------------------------------------------------------


def _change(title: str, area: str = "claude_api") -> DocChange:
    return DocChange(
        url=f"https://x.example/{title}.md",
        product_area=area,
        title=title,
        detected_at=NOW,
        old_sha256="a",
        new_sha256="b",
        summary={
            "changed": ["A", "B", "C", "D"],
            "added": ["New"],
            "removed": [],
            "more": 0,
            "lines_added": 3,
            "lines_removed": 1,
        },
    )


def test_week_start_is_the_monday() -> None:
    """Turns red if: the once-a-week key is not the ISO week's Monday (UTC)."""
    assert week_start(NOW) == date(2026, 9, 28)
    assert week_start(datetime(2026, 9, 28, 0, 0, tzinfo=UTC)) == date(2026, 9, 28)


def test_the_email_groups_by_product_and_links_the_readable_page() -> None:
    """Turns red if: products are not grouped by plain name, the link points at
    the Markdown twin, or the section summary is missing."""
    msg = digest_message(
        to_addr="a@example.com",
        changes=[_change("Rate limits"), _change("Models", "gemini_api")],
        unsubscribe_url="https://site.example/digest/unsubscribe?t=x.y",
    )
    assert msg.subject == "What changed in the AI docs this week"
    assert "The Claude API" in msg.text and "The Gemini API" in msg.text
    assert "https://x.example/Rate limits" in msg.text and ".md" not in msg.text
    assert "changed: A, B, C and 1 more; new: New" in msg.text


def test_the_email_says_why_and_carries_one_click_unsubscribe() -> None:
    """RFC 8058. Turns red if: the link or either header is missing."""
    url = "https://site.example/digest/unsubscribe?t=x.y"
    msg = digest_message(to_addr="a@example.com", changes=[_change("P")], unsubscribe_url=url)
    assert url in msg.text and url in msg.html
    assert msg.headers == {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def test_the_email_html_escapes_everything() -> None:
    """Page titles come from vendor pages. Turns red if: a value reaches the HTML raw."""
    evil = _change("<script>x()</script>")
    msg = digest_message(
        to_addr="a@example.com", changes=[evil], unsubscribe_url='https://s/u?t="><b>'
    )
    assert "<script>" not in msg.html and '"><b>' not in msg.html
    assert "&lt;script&gt;" in msg.html


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def test_no_changes_means_no_email(env: pytest.MonkeyPatch) -> None:
    """Turns red if: an empty digest is sent."""
    _add_user("a@example.com")
    rec = _Recorder()
    assert _send(rec).sent == 0
    assert rec.sent == [] and _sends() == []


def test_only_opted_in_verified_accounts_get_it(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the opt-in or the verified-address rule is dropped."""
    _add_change()
    _add_user("yes@example.com")
    _add_user("unverified@example.com", verified=False)
    _add_user("not-opted@example.com", opted_in=False)
    rec = _Recorder()
    assert _send(rec).sent == 1
    assert [m.to_addr for m in rec.sent] == ["yes@example.com"]


def test_changes_older_than_a_week_are_left_out(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the 7-day window is dropped."""
    _add_change("Old", at=NOW - timedelta(days=8))
    _add_user("a@example.com")
    rec = _Recorder()
    assert _send(rec).changes == 0
    assert rec.sent == []


def test_a_second_run_in_the_same_week_mails_no_one_twice(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the (account, week) claim is not enforced."""
    _add_change()
    _add_user("a@example.com")
    rec = _Recorder()
    _send(rec)
    again = _send(rec)
    assert (again.sent, again.skipped_already_sent) == (0, 1)
    assert len(rec.sent) == 1
    assert len(_sends()) == 1


def test_a_failed_send_is_retried_by_the_next_run(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a failed send keeps its claim (the account would miss the week)."""
    _add_change()
    _add_user("a@example.com")
    first = _send(_Recorder(fail_for={"a@example.com"}))
    assert (first.sent, first.failed) == (0, 1)
    assert _sends() == []
    rec = _Recorder()
    assert _send(rec).sent == 1


def test_a_run_stops_at_its_cap_and_paces_its_sends(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the per-run cap or the pause between sends is dropped."""
    _add_change()
    for i in range(3):
        _add_user(f"u{i}@example.com")
    rec = _Recorder()
    stats = _send(rec, digest_max_per_run=2, digest_send_interval_seconds=0.6)
    assert (stats.sent, stats.capped) == (2, True)
    assert stats.slept == [0.6, 0.6]


def test_each_email_has_its_own_unsubscribe_token(env: pytest.MonkeyPatch) -> None:
    """Only the hash is stored. Turns red if: the raw token is stored, or two
    emails share one."""
    _add_change()
    _add_user("a@example.com")
    _add_user("b@example.com")
    rec = _Recorder()
    _send(rec)
    tokens = [m.headers["List-Unsubscribe"].split("?t=")[1].rstrip(">") for m in rec.sent]  # type: ignore[index]
    raw = {t.split(".")[1] for t in tokens}
    assert len(raw) == 2  # the random part differs, not just the send id before it
    assert not raw & {s.token_hash for s in _sends()}


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def _on(env: pytest.MonkeyPatch) -> None:
    env.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "true")
    get_settings.cache_clear()


def _signed_in(client: TestClient) -> str:
    res = client.post(
        "/v1/auth/register",
        json={"email": "me@example.com", "password": "correct horse battery"},
        headers={**DEMO, **bot_check_headers(client)},
    )
    assert res.status_code == 201, res.text
    return str(res.json()["user_id"])


def _verify(user_id: str) -> None:
    async def go() -> None:
        async with get_sessionmaker()() as s:
            user = await s.get(User, user_id)
            assert user is not None
            user.email_verified_at = NOW
            await s.commit()

    _run(go())


def _opted_in(user_id: str) -> bool:
    async def go() -> bool:
        async with get_sessionmaker()() as s:
            user = await s.get(User, user_id)
            return user is not None and user.digest_opt_in_at is not None

    return _run(go())


def test_with_the_setting_off_there_is_no_digest(env: pytest.MonkeyPatch) -> None:
    """Turns red if: any digest route answers while the access model is off."""
    with TestClient(create_app()) as client:
        _signed_in(client)
        assert client.get("/v1/me/digest", headers=DEMO).status_code == 404
        assert (
            client.put("/v1/me/digest", json={"subscribed": True}, headers=DEMO).status_code == 404
        )
        assert client.get("/digest/unsubscribe?t=x.y").status_code == 404
        assert client.post("/digest/unsubscribe?t=x.y").status_code == 404


def test_subscribing_needs_a_verified_address(env: pytest.MonkeyPatch) -> None:
    """Turns red if: an unverified account can opt in, or a verified one cannot."""
    _on(env)
    with TestClient(create_app()) as client:
        user = _signed_in(client)
        res = client.put("/v1/me/digest", json={"subscribed": True}, headers=DEMO)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "verification_required"
        _verify(user)
        res = client.put("/v1/me/digest", json={"subscribed": True}, headers=DEMO)
        assert res.json()["subscribed"] is True
        assert client.get("/v1/me/digest", headers=DEMO).json()["subscribed"] is True
        assert (
            client.put("/v1/me/digest", json={"subscribed": False}, headers=DEMO).json()[
                "subscribed"
            ]
            is False
        )
    assert not _opted_in(user)


def _one_sent_token(env: pytest.MonkeyPatch, user_id: str) -> str:
    _add_change()
    rec = _Recorder()
    _send(rec)
    [msg] = rec.sent
    return msg.headers["List-Unsubscribe"].split("?t=")[1].rstrip(">")  # type: ignore[index]


def test_opening_the_unsubscribe_link_changes_nothing(env: pytest.MonkeyPatch) -> None:
    """Mail scanners open links. Turns red if: GET unsubscribes."""
    _on(env)
    user = _add_user("a@example.com")
    t = _one_sent_token(env, user)
    with TestClient(create_app()) as client:
        page = client.get(f"/digest/unsubscribe?t={t}")
    assert page.status_code == 200
    assert 'method="post"' in page.text and "Stop the weekly digest" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert _opted_in(user)


def test_the_button_or_one_click_post_unsubscribes_without_a_cookie(
    env: pytest.MonkeyPatch,
) -> None:
    """RFC 8058 one-click comes from the mail app: no cookie, no Origin.
    Turns red if: POST does not unsubscribe, or needs a session."""
    _on(env)
    user = _add_user("a@example.com")
    t = _one_sent_token(env, user)
    with TestClient(create_app()) as client:
        res = client.post(f"/digest/unsubscribe?t={t}", data={"List-Unsubscribe": "One-Click"})
        assert res.status_code == 200
        again = client.post(f"/digest/unsubscribe?t={t}")
        assert again.status_code == 200  # idempotent
    assert not _opted_in(user)
    [row] = _sends()
    assert row.unsubscribed_at is not None


@pytest.mark.parametrize("bad", ["garbage", "not-a-uuid.token", f"{uuid.uuid4()}.token", ""])
def test_a_wrong_token_unsubscribes_no_one(env: pytest.MonkeyPatch, bad: str) -> None:
    """Turns red if: a guessed or mangled token unsubscribes anyone."""
    _on(env)
    user = _add_user("a@example.com")
    good = _one_sent_token(env, user)
    send_id = good.split(".")[0]
    with TestClient(create_app()) as client:
        for t in (bad, f"{send_id}.wrong"):
            assert client.post(f"/digest/unsubscribe?t={t}").status_code in (404, 422)
    assert _opted_in(user)


# ---------------------------------------------------------------------------
# Email headers reach the provider; transactional mail is unchanged
# ---------------------------------------------------------------------------


def test_resend_receives_the_headers_and_transactional_mail_has_none() -> None:
    """Turns red if: the digest's List-Unsubscribe headers are dropped, or a
    transactional email gains a headers field."""
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "1"})

    client = ResendEmailClient(
        api_key="k",
        from_addr="CiteVyn <x@example.com>",
        base_url="https://r.example",
        transport=httpx.MockTransport(handler),
    )
    _run(
        client.send(
            EmailMessage("a@example.com", "s", "t", "h", headers={"List-Unsubscribe": "<u>"})
        )
    )
    _run(client.send(EmailMessage("a@example.com", "s", "t", "h")))
    assert bodies[0]["headers"] == {"List-Unsubscribe": "<u>"}
    assert "headers" not in bodies[1]


def test_the_file_outbox_shows_the_headers(tmp_path: Any) -> None:
    """Turns red if: a local developer cannot see the unsubscribe headers."""
    outbox = FileOutboxEmailClient(tmp_path)
    _run(
        outbox.send(
            EmailMessage("a@example.com", "s", "t", "h", headers={"List-Unsubscribe": "<u>"})
        )
    )
    [path] = list(tmp_path.iterdir())
    assert "List-Unsubscribe: <u>" in path.read_text()


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_the_command_refuses_while_off(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """Turns red if: the digest can be sent without the owner turning it on."""
    from app.worker import cli

    monkeypatch.delenv("CITEVYN_WEEKLY_DIGEST_ENABLED", raising=False)
    get_settings.cache_clear()

    async def boom(*_a: object, **_k: object) -> int:
        raise AssertionError("the digest ran while off")

    monkeypatch.setattr(cli, "_digest", boom)
    assert cli.main(["digest"]) == 2
    assert "The weekly digest is off" in capsys.readouterr().out
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Review round 1 (dc03869): each test below failed or could not fail before
# ---------------------------------------------------------------------------


def test_a_rerun_mails_the_accounts_the_first_run_did_not(env: pytest.MonkeyPatch) -> None:
    """The blocker: a re-run rolled back on the first account already mailed,
    expiring the loaded changes, then crashed on the next account AFTER claiming
    it, so that account silently missed the week. Turns red if: a re-run cannot
    mail an account that comes after one it skips."""
    _add_change()
    _add_user("a@example.com", uid="usr_a")
    first = _Recorder()
    _send(first)
    _add_user("b@example.com", uid="usr_b")  # opts in mid-week
    second = _Recorder()
    stats = _send(second)
    assert (stats.sent, stats.skipped_already_sent) == (1, 1)
    assert [m.to_addr for m in second.sent] == ["b@example.com"]
    assert len(_sends()) == 2


def test_runs_either_side_of_monday_never_repeat_a_change(env: pytest.MonkeyPatch) -> None:
    """The week key is a calendar week but the window is 7 days: without
    'since this account's last digest', Sunday night and Monday morning runs
    both sent the same change. Turns red if: a change is mailed twice."""
    sunday = datetime(2026, 9, 27, 23, 0, tzinfo=UTC)
    monday = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
    _add_change(at=sunday - timedelta(hours=1))
    _add_user("a@example.com")
    rec = _Recorder()
    assert _send(rec, now=sunday).sent == 1
    again = _send(rec, now=monday)
    assert (again.sent, again.skipped_nothing_new) == (0, 1)
    assert len(rec.sent) == 1
    _add_change("Models", at=monday - timedelta(minutes=30))  # something new: it goes out
    rec2 = _Recorder()
    _send(rec2, now=monday + timedelta(minutes=1))
    # already mailed this week (Monday's run claimed nothing, so this is its first)
    assert [m.to_addr for m in rec2.sent] == ["a@example.com"]
    assert "Models" in rec2.sent[0].text and "Rate limits" not in rec2.sent[0].text


def test_failed_sends_count_toward_the_per_run_cap(env: pytest.MonkeyPatch) -> None:
    """A provider that rejects everything must not be tried for every account.
    Turns red if: only successes count toward the cap."""
    _add_change()
    for i in range(5):
        _add_user(f"u{i}@example.com")
    everyone = {f"u{i}@example.com" for i in range(5)}
    stats = _send(_Recorder(fail_for=everyone), digest_max_per_run=2)
    assert (stats.failed, stats.capped) == (2, True)


def test_an_unexpected_error_releases_the_claim(env: pytest.MonkeyPatch) -> None:
    """Turns red if: an error other than EmailDeliveryError leaves the account
    claimed (it would miss the week)."""
    _add_change()
    _add_user("a@example.com")

    class Broken:
        async def send(self, message: EmailMessage) -> None:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _send(Broken())
    assert _sends() == []
    rec = _Recorder()
    assert _send(rec).sent == 1


def test_a_site_url_with_a_line_break_is_refused(env: pytest.MonkeyPatch) -> None:
    """The site URL goes into a mail header. Turns red if: a CR/LF in it can
    add headers (e.g. Bcc) to every digest."""
    _add_change()
    _add_user("a@example.com")
    rec = _Recorder()
    with pytest.raises(ValueError, match="line break"):
        _send(rec, magic_link_base_url="https://x.example\r\nBcc: evil@example.com")
    assert rec.sent == [] and _sends() == []


def _digest_env(env: pytest.MonkeyPatch, tmp_path: Any, **extra: str) -> None:
    values = {
        "CITEVYN_WEEKLY_DIGEST_ENABLED": "true",
        "CITEVYN_ACCESS_MODEL_ENABLED": "true",
        "CITEVYN_MAGIC_LINK_BASE_URL": "https://citevyn.example",
        "CITEVYN_EMAIL_OUTBOX_DIR": str(tmp_path / "outbox"),
        **extra,
    }
    for k, v in values.items():
        env.setenv(k, v)
    get_settings.cache_clear()


def test_the_command_refuses_with_the_access_model_off(
    env: pytest.MonkeyPatch, tmp_path: Any, capsys: Any
) -> None:
    """Unsubscribe links are 404 while the access model is off. Turns red if:
    the digest can be sent then."""
    from app.worker import cli

    _digest_env(env, tmp_path, CITEVYN_ACCESS_MODEL_ENABLED="false")
    _add_change()
    _add_user("a@example.com")
    assert cli.main(["digest"]) == 2
    assert "access model is off" in capsys.readouterr().out
    assert not (tmp_path / "outbox").exists()


def test_the_command_refuses_without_a_site_url(
    env: pytest.MonkeyPatch, tmp_path: Any, capsys: Any
) -> None:
    """Turns red if: emails can carry a localhost unsubscribe link."""
    from app.worker import cli

    _digest_env(env, tmp_path)
    env.delenv("CITEVYN_MAGIC_LINK_BASE_URL")
    get_settings.cache_clear()
    assert cli.main(["digest"]) == 2
    assert "MAGIC_LINK_BASE_URL" in capsys.readouterr().out


def test_the_command_sends_through_the_configured_client(
    env: pytest.MonkeyPatch, tmp_path: Any, capsys: Any
) -> None:
    """The enabled path end to end, into the local outbox (never real mail).
    Turns red if: the wiring (settings, client, session, summary) breaks."""
    from app.worker import cli

    _digest_env(env, tmp_path)
    _add_change(at=datetime.now(UTC) - timedelta(hours=1))
    _add_user("a@example.com")
    env.setenv("CITEVYN_DIGEST_SEND_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    assert cli.main(["digest"]) == 0
    assert "sent 1" in capsys.readouterr().out
    [mail] = list((tmp_path / "outbox").iterdir())
    text = mail.read_text()
    assert "To: a@example.com" in text
    assert "List-Unsubscribe: <https://citevyn.example/digest/unsubscribe?t=" in text


def test_the_database_refuses_a_second_claim_for_the_same_week(env: pytest.MonkeyPatch) -> None:
    """Two OVERLAPPING runs both pass the up-front check; the unique constraint
    is what stops the second claim. Turns red if: the constraint is dropped."""
    from sqlalchemy.exc import IntegrityError

    user = _add_user("a@example.com")

    async def claim() -> None:
        async with get_sessionmaker()() as s:
            s.add(
                DigestSend(
                    user_id=user, week_start=week_start(NOW), sent_at=NOW, token_hash="h" * 64
                )
            )
            await s.commit()

    _run(claim())
    with pytest.raises(IntegrityError):
        _run(claim())


# ---------------------------------------------------------------------------
# Remaining branches (CI's coverage gate found them untested)
# ---------------------------------------------------------------------------


def test_an_overlapping_run_that_claims_first_is_skipped_and_the_rest_are_mailed(
    env: pytest.MonkeyPatch,
) -> None:
    """Run A claims usr_b AFTER this run's up-front read but before its insert.
    Turns red if: the lost insert crashes the run, or mails usr_b twice."""
    _add_change()
    _add_user("a@example.com", uid="usr_a")
    _add_user("b@example.com", uid="usr_b")
    _add_user("c@example.com", uid="usr_c")

    class OtherRunClaimsB(_Recorder):
        async def send(self, message: EmailMessage) -> None:
            await super().send(message)
            if message.to_addr == "a@example.com":
                async with get_sessionmaker()() as other:
                    other.add(
                        DigestSend(
                            user_id="usr_b",
                            week_start=week_start(NOW),
                            sent_at=NOW,
                            token_hash="x" * 64,
                        )
                    )
                    await other.commit()

    rec = OtherRunClaimsB()
    stats = _send(rec)
    assert (stats.sent, stats.skipped_already_sent) == (2, 1)
    assert [m.to_addr for m in rec.sent] == ["a@example.com", "c@example.com"]


def test_opening_a_bad_unsubscribe_link_shows_a_404_page(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a bad link renders the confirm button or changes anything."""
    _on(env)
    user = _add_user("a@example.com")
    _one_sent_token(env, user)
    with TestClient(create_app()) as client:
        page = client.get(f"/digest/unsubscribe?t={uuid.uuid4()}.nope")
    assert page.status_code == 404
    assert 'method="post"' not in page.text and "does not work" in page.text
    assert _opted_in(user)


def test_the_settings_route_is_404_for_an_account_that_no_longer_exists() -> None:
    """Turns red if: a principal whose user row is gone gets a 500."""
    from fastapi import HTTPException

    from app.api.routes.digest import _user

    class NoRows:
        async def get(self, *_a: object) -> None:
            return None

    class Req:
        class state:  # noqa: N801
            request_id = "r"

    with pytest.raises(HTTPException) as exc:
        _run(_user(NoRows(), Req(), "usr_gone"))  # type: ignore[arg-type]
    assert exc.value.status_code == 404


def test_a_resend_transport_failure_is_an_email_delivery_error() -> None:
    """The sender releases its claim only on EmailDeliveryError. Turns red if: a
    network failure escapes as a raw httpx error."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = ResendEmailClient(
        api_key="k",
        from_addr="CiteVyn <x@example.com>",
        base_url="https://r.example",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(EmailDeliveryError, match="transport error: ConnectError"):
        _run(client.send(EmailMessage("a@example.com", "s", "t", "h")))


def test_the_command_refuses_without_email_delivery(
    env: pytest.MonkeyPatch, tmp_path: Any, capsys: Any
) -> None:
    """Turns red if: the digest runs with nowhere to send mail."""
    from app.worker import cli

    _digest_env(env, tmp_path)
    env.setattr("app.services.notifications.build_email_client", lambda _s: None)
    assert cli.main(["digest"]) == 2
    assert "No email delivery" in capsys.readouterr().out
