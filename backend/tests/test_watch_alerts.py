"""Watch alert emails (ADR-0005 §5, Phase 7C-2).

Current Pro accounts with a verified address get one email per run listing the
changes that match their watches, never the same change twice, with a
one-click unsubscribe a mail scanner cannot trigger. No real email: a recording
client stands in for delivery.

Each test says which change turns it red.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import db as db_module
from app.core.config import Settings, get_settings
from app.core.db import get_sessionmaker
from app.core.email_client import EmailDeliveryError, EmailMessage
from app.main import create_app
from app.models import AlertSend, Base, DocChange, Membership, User, Watch
from app.services.alerts import send_watch_alerts
from app.watch.pages import WATCHED_PAGES

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
PAGE = WATCHED_PAGES[0]
OTHER = WATCHED_PAGES[1]


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
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
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
    email: str,
    *,
    uid: str | None = None,
    pro: bool = True,
    verified: bool = True,
    watches: list[tuple[str, str]] | None = None,
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
                )
            )
            await s.flush()
            if pro:
                s.add(
                    Membership(
                        account_id=user_id,
                        stripe_subscription_id=f"sub_{user_id}",
                        plan="pro",
                        status="active",
                        cancel_at_period_end=False,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            for kind, value in watches or [("term", "--permission-mode")]:
                s.add(Watch(user_id=user_id, kind=kind, value=value, created_at=NOW))
            await s.commit()
            return user_id

    return _run(go())


def _add_change(
    *,
    url: str = PAGE.url,
    title: str = "Settings",
    added: list[str] | None = None,
    at: datetime = NOW,
    truncated: bool = False,
) -> uuid.UUID:
    async def go() -> uuid.UUID:
        async with get_sessionmaker()() as s:
            row = DocChange(
                url=url,
                product_area="claude_code",
                title=title,
                detected_at=at,
                old_sha256="a" * 64,
                new_sha256="b" * 64,
                summary={
                    "changed": ["Flags"],
                    "added": [],
                    "removed": [],
                    "more": 0,
                    "lines_added": 1,
                    "lines_removed": 0,
                },
                lines={
                    "added": added if added is not None else ["use --permission-mode plan"],
                    "removed": [],
                    "truncated": truncated,
                },
            )
            s.add(row)
            await s.commit()
            return row.change_id

    return _run(go())


def _send(client: Any, *, now: datetime = NOW, **settings: Any) -> Any:
    async def go() -> Any:
        slept: list[float] = []

        async def fake_sleep(sec: float) -> None:
            slept.append(sec)

        async with get_sessionmaker()() as s:
            stats = await send_watch_alerts(
                s, client, Settings(**settings), now=now, sleep=fake_sleep
            )
        stats.slept = slept  # type: ignore[attr-defined]
        return stats

    return _run(go())


def _sends() -> list[AlertSend]:
    async def go() -> list[AlertSend]:
        async with get_sessionmaker()() as s:
            return list((await s.execute(select(AlertSend))).scalars())

    return _run(go())


def _watch_count(user_id: str) -> int:
    async def go() -> int:
        async with get_sessionmaker()() as s:
            return len(
                list((await s.execute(select(Watch).where(Watch.user_id == user_id))).scalars())
            )

    return _run(go())


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def test_a_matching_change_is_alerted_once_listing_what_matched(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a match is not mailed, the email hides which watch matched,
    or a non-matching account is mailed."""
    _add_change()
    _add_user("yes@example.com", watches=[("term", "--permission-mode"), ("page", PAGE.url)])
    _add_user("no@example.com", watches=[("term", "--resume")])
    rec = _Recorder()
    stats = _send(rec)
    assert stats.sent == 1
    [msg] = rec.sent
    assert msg.to_addr == "yes@example.com"
    assert '"--permission-mode"' in msg.text and f"the page {PAGE.url}" in msg.text
    assert msg.headers and msg.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_a_change_is_never_alerted_twice(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a second run re-sends an alerted change."""
    _add_change()
    _add_user("a@example.com")
    rec = _Recorder()
    _send(rec)
    assert _send(rec, now=NOW + timedelta(hours=1)).sent == 0
    assert len(rec.sent) == 1


def test_a_change_saved_while_a_run_was_going_is_alerted_next_run(env: pytest.MonkeyPatch) -> None:
    """Dedupe is per change, not by time (the digest's #524 gap cannot happen
    here). Turns red if: an earlier-stamped change added after a run is missed."""
    _add_change(title="First")
    _add_user("a@example.com")
    rec = _Recorder()
    _send(rec)
    _add_change(title="Late", at=NOW - timedelta(minutes=1))  # stamped before run 1
    _send(rec, now=NOW + timedelta(hours=1))
    assert len(rec.sent) == 2 and "Late" in rec.sent[1].text and "First" not in rec.sent[1].text


def test_only_current_pro_accounts_with_a_verified_address_are_alerted(
    env: pytest.MonkeyPatch,
) -> None:
    """Turns red if: a lapsed or never-Pro account, or an unverified address, is mailed."""
    _add_change()
    _add_user("pro@example.com")
    _add_user("free@example.com", pro=False)
    _add_user("unverified@example.com", verified=False)
    rec = _Recorder()
    stats = _send(rec)
    assert [m.to_addr for m in rec.sent] == ["pro@example.com"]
    assert stats.skipped_not_pro == 1
    assert len(_sends()) == 1  # only the Pro account's alert was claimed


def test_a_failed_send_is_retried_and_an_error_releases_the_claim(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a failed or crashed send keeps its claim (the alert would
    never be sent)."""
    _add_change()
    _add_user("a@example.com")
    assert _send(_Recorder(fail_for={"a@example.com"})).failed == 1
    assert _sends() == []

    class Broken:
        async def send(self, message: EmailMessage) -> None:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _send(Broken())
    assert _sends() == []
    rec = _Recorder()
    assert _send(rec).sent == 1


def test_the_run_is_capped_counting_failures_and_paced(env: pytest.MonkeyPatch) -> None:
    """Turns red if: failures do not count toward the cap, or sends are not paced."""
    _add_change()
    for i in range(4):
        _add_user(f"u{i}@example.com", uid=f"usr_{i}")
    stats = _send(
        _Recorder(fail_for={"u0@example.com"}), alert_max_per_run=2, alert_send_interval_seconds=0.6
    )
    assert (stats.sent, stats.failed, stats.capped) == (1, 1, True)
    assert stats.slept == [0.6, 0.6]


def test_an_overlapping_run_that_claims_first_is_skipped(env: pytest.MonkeyPatch) -> None:
    """Turns red if: the lost claim crashes the run instead of skipping."""
    change = _add_change()
    _add_user("a@example.com", uid="usr_a")
    b = _add_user("b@example.com", uid="usr_b")

    class OtherRunClaimsB(_Recorder):
        async def send(self, message: EmailMessage) -> None:
            await super().send(message)
            if message.to_addr == "a@example.com":
                async with get_sessionmaker()() as other:
                    other.add(
                        AlertSend(
                            email_id=uuid.uuid4(),
                            user_id=b,
                            change_id=change,
                            sent_at=NOW,
                            token_hash="x" * 64,
                        )
                    )
                    await other.commit()

    rec = OtherRunClaimsB()
    assert _send(rec).sent == 1
    assert [m.to_addr for m in rec.sent] == ["a@example.com"]


def test_a_very_large_change_says_a_match_may_be_missing(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a truncated change is alerted without saying so."""
    _add_change(truncated=True)
    _add_user("a@example.com")
    rec = _Recorder()
    _send(rec)
    assert "a match may be missing" in rec.sent[0].text


def test_the_email_escapes_everything(env: pytest.MonkeyPatch) -> None:
    """Page titles come from vendor pages. Turns red if: a value reaches the HTML raw."""
    _add_change(title="<script>x()</script>")
    _add_user("a@example.com", watches=[("term", "--permission-mode")])
    rec = _Recorder()
    _send(rec)
    assert "<script>" not in rec.sent[0].html and "&lt;script&gt;" in rec.sent[0].html


def test_a_site_url_with_whitespace_is_refused(env: pytest.MonkeyPatch) -> None:
    """It goes into a mail header. Turns red if: a CR/LF, tab or space is accepted."""
    _add_change()
    _add_user("a@example.com")
    for bad in (
        "https://x.example\r\nBcc: e@example.com",
        "https://x.example\x0bX: 1",
        "https://x .example",
    ):
        with pytest.raises(ValueError):
            _send(_Recorder(), magic_link_base_url=bad)
    assert _sends() == []


# ---------------------------------------------------------------------------
# Unsubscribe
# ---------------------------------------------------------------------------


def _on(env: pytest.MonkeyPatch) -> None:
    env.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "true")
    get_settings.cache_clear()


def _one_token(user_email: str) -> str:
    rec = _Recorder()
    _send(rec)
    [msg] = [m for m in rec.sent if m.to_addr == user_email]
    return msg.headers["List-Unsubscribe"].split("?t=")[1].rstrip(">")  # type: ignore[index]


def test_with_the_setting_off_there_is_no_unsubscribe_route(env: pytest.MonkeyPatch) -> None:
    """Uses a VALID token, so a missing off-check cannot hide behind a bad-token
    404. Turns red if: the route answers (or removes watches) while off."""
    _add_change()
    user = _add_user("a@example.com")
    t = _one_token("a@example.com")
    with TestClient(create_app()) as client:
        assert client.get(f"/alerts/unsubscribe?t={t}").status_code == 404
        assert client.post(f"/alerts/unsubscribe?t={t}").status_code == 404
    assert _watch_count(user) == 1


def test_opening_the_link_changes_nothing_and_posting_removes_the_watches(
    env: pytest.MonkeyPatch,
) -> None:
    """Mail scanners open links; one-click comes without a cookie. Turns red if:
    GET removes watches, or POST does not (or needs a session)."""
    _on(env)
    _add_change()
    user = _add_user("a@example.com", watches=[("term", "--permission-mode"), ("page", OTHER.url)])
    t = _one_token("a@example.com")
    with TestClient(create_app()) as client:
        page = client.get(f"/alerts/unsubscribe?t={t}")
        assert page.status_code == 200 and 'method="post"' in page.text
        assert _watch_count(user) == 2
        assert client.post(f"/alerts/unsubscribe?t={t}").status_code == 200
        assert client.post(f"/alerts/unsubscribe?t={t}").status_code == 200  # idempotent
    assert _watch_count(user) == 0


def test_a_wrong_token_removes_nothing(env: pytest.MonkeyPatch) -> None:
    """Turns red if: a guessed or mangled token removes anyone's watches."""
    _on(env)
    _add_change()
    user = _add_user("a@example.com")
    good = _one_token("a@example.com")
    email_id = good.split(".")[0]
    with TestClient(create_app()) as client:
        for t in ("garbage", f"{uuid.uuid4()}.x", f"{email_id}.wrong"):
            assert client.post(f"/alerts/unsubscribe?t={t}").status_code == 404
            assert client.get(f"/alerts/unsubscribe?t={t}").status_code == 404
    assert _watch_count(user) == 1


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _alerts_env(env: pytest.MonkeyPatch, tmp_path: Any, **extra: str) -> None:
    values = {
        "CITEVYN_WATCH_ALERTS_ENABLED": "true",
        "CITEVYN_ACCESS_MODEL_ENABLED": "true",
        "CITEVYN_MAGIC_LINK_BASE_URL": "https://citevyn.example",
        "CITEVYN_EMAIL_OUTBOX_DIR": str(tmp_path / "outbox"),
        "CITEVYN_ALERT_SEND_INTERVAL_SECONDS": "0",
        **extra,
    }
    for k, v in values.items():
        env.setenv(k, v)
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"CITEVYN_WATCH_ALERTS_ENABLED": "false"}, "Watch alerts are off"),
        ({"CITEVYN_ACCESS_MODEL_ENABLED": "false"}, "access model is off"),
        ({"CITEVYN_MAGIC_LINK_BASE_URL": "   "}, "MAGIC_LINK_BASE_URL"),
    ],
)
def test_the_command_refuses_until_everything_is_set(
    env: pytest.MonkeyPatch, tmp_path: Any, capsys: Any, override: dict[str, str], message: str
) -> None:
    """Turns red if: alerts can be sent while off, without working unsubscribe
    links, or with a blank site URL."""
    from app.worker import cli

    _alerts_env(env, tmp_path, **override)
    _add_change()
    _add_user("a@example.com")
    assert cli.main(["alerts"]) == 2
    assert message in capsys.readouterr().out
    assert not (tmp_path / "outbox").exists()


def test_the_command_refuses_without_email_delivery(
    env: pytest.MonkeyPatch, tmp_path: Any, capsys: Any
) -> None:
    """Turns red if: the alerts run with nowhere to send mail."""
    from app.worker import cli

    _alerts_env(env, tmp_path)
    env.setattr("app.services.notifications.build_email_client", lambda _s: None)
    assert cli.main(["alerts"]) == 2
    assert "No email delivery" in capsys.readouterr().out


def test_the_command_sends_through_the_configured_client(
    env: pytest.MonkeyPatch, tmp_path: Any, capsys: Any
) -> None:
    """The enabled path end to end, into the local outbox (never real mail).
    Turns red if: the wiring breaks."""
    from app.worker import cli

    _alerts_env(env, tmp_path)
    _add_change(at=datetime.now(UTC) - timedelta(hours=1))
    _add_user("a@example.com")
    assert cli.main(["alerts"]) == 0
    assert "sent 1" in capsys.readouterr().out
    [mail] = list((tmp_path / "outbox").iterdir())
    assert "List-Unsubscribe: <https://citevyn.example/alerts/unsubscribe?t=" in mail.read_text()
