"""Deleting an account on request (#511).

The privacy page promises to delete an account and its data on request; the
runbook says within 30 days. The free trial is once per address (ADR-0005 §1,
``docs/ACCESS_POLICY.md``), so the deletion keeps a tombstone (the SHA-256 of the
address and the trial answers it used) and the trial count carries over if the
same address signs up again.

Hermetic: a file-backed SQLite with foreign keys ON (``app.core.db`` turns the
pragma on for every SQLite connection), so the cascades run here as they do on
Postgres. ``tests/test_pg_integration.py`` repeats the cascade check on a
migrated Postgres.

Every test says which change turns it red.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.access.policy import Tier
from app.access.quota import usage_for
from app.core.config import Settings
from app.models import (
    AlertSend,
    AnswerFeedback,
    AnswerUsage,
    ApiKey,
    AuditAction,
    AuditEvent,
    AuthSession,
    Base,
    DeletedAccount,
    DigestSend,
    DocChange,
    MagicLinkToken,
    Membership,
    Message,
    MessageRole,
    ProviderCall,
    SharedAnswer,
    SourceRequest,
    StripeEvent,
    User,
    UserIdentity,
    UserRole,
    Watch,
)
from app.models import Session as ChatSession
from app.services.account_deletion import (
    AccountNotFound,
    AmbiguousAddress,
    SubscriptionStillLive,
    delete_account,
    email_hash,
    plan_deletion,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
EMAIL = "person@example.com"
OTHER_EMAIL = "someone.else@example.com"

Maker = async_sessionmaker[AsyncSession]


# ---------------------------------------------------------------------------
# Every table that references users.user_id, read from the models
# ---------------------------------------------------------------------------


def user_references() -> dict[str, tuple[str, str]]:
    """``{table: (column, ondelete)}`` for every table with a foreign key onto
    ``users.user_id``, read from SQLAlchemy metadata (not a hand-kept list)."""
    found: dict[str, tuple[str, str]] = {}
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name == "users" and fk.column.name == "user_id":
                assert table.name not in found, f"{table.name} has two FKs onto users"
                found[table.name] = (fk.parent.name, (fk.ondelete or "NO ACTION").upper())
    return found


async def seed_every_reference(s: AsyncSession, user_id: str, now: datetime) -> dict[str, int]:
    """Insert one row into EVERY table that references ``user_id``, plus a chat
    message, a provider call and a payment event. Returns rows inserted per
    referencing table.

    The builders are keyed by table name and checked against
    :func:`user_references`, so a new table with a foreign key onto users fails
    here until it gets a builder (and so a place in the cascade checks)."""
    session_id, message_id, change_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tag = uuid.uuid4().hex[:12]
    s.add(
        ChatSession(
            session_id=session_id,
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
    )
    s.add(
        DocChange(
            change_id=change_id,
            url=f"https://docs.example.com/{tag}",
            product_area="claude_code",
            title="t",
            detected_at=now,
            old_sha256="a" * 64,
            new_sha256="b" * 64,
            summary={},
        )
    )
    await s.flush()
    s.add(
        Message(
            message_id=message_id,
            session_id=session_id,
            role=MessageRole.user,
            content="a question",
            created_at=now,
        )
    )
    await s.flush()

    builders: dict[str, Callable[[], Any]] = {
        "sessions": lambda: None,  # added above: messages need it first
        "auth_sessions": lambda: AuthSession(
            secret_hash="h" * 64, user_id=user_id, created_at=now, expires_at=now
        ),
        "user_identities": lambda: UserIdentity(
            provider="github", provider_account_id=tag, user_id=user_id, created_at=now
        ),
        "magic_link_tokens": lambda: MagicLinkToken(
            secret_hash="m" * 64, user_id=user_id, created_at=now, expires_at=now
        ),
        "audit_events": lambda: AuditEvent(
            user_id=user_id,
            role=UserRole.demo_user,
            action=AuditAction.login,
            timestamp=now,
            metadata_={"event": "login"},
        ),
        "answer_usage": lambda: AnswerUsage(
            user_id=user_id, occurred_at=now, paid_by="platform", channel="chat", tier="free"
        ),
        "api_keys": lambda: ApiKey(
            key_id=tag, user_id=user_id, name="k", secret_hash="k" * 64, created_at=now
        ),
        "memberships": lambda: Membership(
            account_id=user_id,
            status="canceled",
            stripe_subscription_id=f"sub_{tag}",
            created_at=now,
            updated_at=now,
        ),
        "shared_answers": lambda: SharedAnswer(
            share_id=tag,
            user_id=user_id,
            message_id=message_id,
            question="q",
            answer="a",
            citations=[],
            answered_at=now,
            created_at=now,
        ),
        "watches": lambda: Watch(user_id=user_id, kind="term", value=tag, created_at=now),
        "digest_sends": lambda: DigestSend(
            user_id=user_id, week_start=now.date(), sent_at=now, token_hash="d" * 64
        ),
        "alert_sends": lambda: AlertSend(
            email_id=uuid.uuid4(),
            user_id=user_id,
            change_id=change_id,
            sent_at=now,
            token_hash="e" * 64,
        ),
        "answer_feedback": lambda: AnswerFeedback(
            message_id=message_id, user_id=user_id, rating="up", created_at=now, updated_at=now
        ),
        "source_requests": lambda: SourceRequest(
            user_id=user_id, message_id=message_id, question="q", created_at=now
        ),
    }
    assert set(builders) == set(user_references()), (
        "a table with a foreign key onto users has no builder here; add one so the "
        "deletion cascade checks cover it"
    )
    for build in builders.values():
        row = build()
        if row is not None:
            s.add(row)
    s.add(
        ProviderCall(
            occurred_at=now,
            kind="llm",
            call_site="answer",
            provider="stub",
            model="stub",
            user_id=user_id,
        )
    )
    s.add(
        StripeEvent(
            stripe_event_id=f"evt_{tag}",
            event_type="customer.subscription.deleted",
            event_created=1,
            account_id=user_id,
            outcome="applied",
            received_at=now,
        )
    )
    await s.flush()
    return dict.fromkeys(builders, 1)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def maker(tmp_path: Any) -> Generator[Maker, None, None]:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'del.db'}")

    async def _init() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


def run[T](make: Maker, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def _go() -> T:
        async with make() as s:
            return await fn(s)

    return asyncio.run(_go())


def add_user(
    make: Maker,
    email: str | None,
    *,
    answers: int = 0,
    statuses: tuple[str, ...] = (),
    grace_until: datetime | None = None,
) -> str:
    """A verified account with ``answers`` platform-paid trial answers and one
    membership per status. Returns its user id."""
    user_id = "usr_" + uuid.uuid4().hex

    async def _go(s: AsyncSession) -> None:
        s.add(
            User(
                user_id=user_id,
                role=UserRole.demo_user,
                created_at=NOW,
                email=email,
                email_verified_at=NOW,
            )
        )
        await s.flush()
        for _ in range(answers):
            s.add(
                AnswerUsage(
                    user_id=user_id,
                    occurred_at=NOW - timedelta(days=3),
                    paid_by="platform",
                    channel="chat",
                    tier="free",
                )
            )
        # A user-paid (BYOK) answer never counts toward the trial: a partner
        # proving the tombstone counts the trial rows, not every row.
        s.add(
            AnswerUsage(
                user_id=user_id, occurred_at=NOW, paid_by="user", channel="chat", tier="free"
            )
        )
        for status in statuses:
            s.add(
                Membership(
                    account_id=user_id,
                    status=status,
                    stripe_subscription_id=f"sub_{uuid.uuid4().hex}",
                    grace_until=grace_until,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        await s.commit()

    run(make, _go)
    return user_id


def count(make: Maker, model: Any, *where: Any) -> int:
    async def _go(s: AsyncSession) -> int:
        return int(
            (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()
        )

    return run(make, _go)


def tombstone(make: Maker, email: str) -> DeletedAccount | None:
    return run(make, lambda s: s.get(DeletedAccount, email_hash(email)))


def delete(make: Maker, email: str, now: datetime = NOW) -> Any:
    return run(make, lambda s: delete_account(s, email, now))


def trial_used(make: Maker, user_id: str, tier: Tier = Tier.free) -> Any:
    return run(make, lambda s: usage_for(s, user_id, tier, Settings(), NOW))


# ---------------------------------------------------------------------------
# The address hash
# ---------------------------------------------------------------------------


def test_the_hash_is_sha256_hex_of_the_normalised_address() -> None:
    """Turns red if: the hash stops normalising (case or spaces give a new
    hash, so a re-sign-up as ``Person@Example.com`` gets a fresh trial) or is
    not SHA-256 hex (it would not fit ``String(64)``)."""
    import hashlib

    expected = hashlib.sha256(b"person@example.com").hexdigest()
    assert email_hash("person@example.com") == expected
    assert email_hash("  Person@EXAMPLE.com \n") == expected
    assert email_hash("other@example.com") != expected  # partner: not a constant
    assert len(expected) == 64


def test_an_older_mixed_case_address_is_still_found(maker: Maker) -> None:
    """Provider addresses were stored verbatim before ADR-0004 PR 14
    (``oauth.py``), and no migration lower-cased them. Turns red if: the lookup
    compares the stored address exactly, so such an account cannot be deleted
    and the runbook's "case does not matter" is false."""
    user_id = add_user(maker, " Person@Example.COM")
    delete(maker, EMAIL)
    assert count(maker, User, User.user_id == user_id) == 0
    assert tombstone(maker, EMAIL) is not None  # keyed by the normalised address


def test_two_accounts_that_differ_only_in_case_are_refused(maker: Maker) -> None:
    """Turns red if: an address matching two older rows deletes one of them
    at random instead of stopping for the operator."""
    add_user(maker, "Person@example.com")
    add_user(maker, EMAIL)
    with pytest.raises(AmbiguousAddress):
        run(maker, lambda s: plan_deletion(s, EMAIL))
    with pytest.raises(AmbiguousAddress):
        delete(maker, EMAIL)
    assert count(maker, User) == 2
    assert count(maker, DeletedAccount) == 0


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


def test_the_dry_run_counts_what_would_go_and_changes_nothing(maker: Maker) -> None:
    """Turns red if: the plan writes anything, or stops counting a table that
    references the account."""
    user_id = add_user(maker, EMAIL, answers=2)

    async def _seed(s: AsyncSession) -> None:
        await seed_every_reference(s, user_id, NOW)
        await s.commit()

    run(maker, _seed)
    plan = run(maker, lambda s: plan_deletion(s, "  PERSON@example.com "))
    assert plan.user_id == user_id
    assert plan.trial_answers_used == 3  # 2 + the seeded answer_usage row; BYOK not counted
    assert plan.carried_over == 0
    assert plan.live_subscriptions == []
    # 3 platform rows + 1 BYOK row from add_user's partner.
    assert plan.rows["answer_usage"] == 4
    assert set(plan.rows) | set(plan.kept) == set(user_references())
    assert plan.kept == {"audit_events": 1}
    # Review F3: chat messages hang off sessions, not users; still counted.
    assert plan.indirect == {"messages": 1}
    assert all(n == 1 for t, n in plan.rows.items() if t != "answer_usage")
    # Nothing changed.
    assert count(maker, User, User.user_id == user_id) == 1
    assert count(maker, AnswerUsage, AnswerUsage.user_id == user_id) == 4
    assert count(maker, DeletedAccount) == 0


def test_an_unknown_address_is_reported_not_guessed(maker: Maker) -> None:
    """Turns red if: an unknown address returns an empty plan or deletes
    something instead of raising AccountNotFound."""
    add_user(maker, OTHER_EMAIL)
    with pytest.raises(AccountNotFound):
        run(maker, lambda s: plan_deletion(s, EMAIL))
    with pytest.raises(AccountNotFound):
        delete(maker, EMAIL)
    assert count(maker, User) == 1
    assert count(maker, DeletedAccount) == 0


# ---------------------------------------------------------------------------
# Refusing while a subscription can still bill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "grace"),
    [
        (("active",), None),
        (("trialing",), None),
        (("past_due",), NOW + timedelta(days=3)),  # still Pro, in its grace
        (("past_due",), NOW - timedelta(days=3)),  # grace over, but Stripe still retries
        (("canceled", "active"), None),  # any one live subscription refuses
        (("paused",), None),  # a status we do not treat as ended fails closed
    ],
)
def test_a_live_subscription_refuses_and_changes_nothing(
    maker: Maker, statuses: tuple[str, ...], grace: datetime | None
) -> None:
    """Turns red if: the refusal is dropped or narrowed (deleting the account
    would leave the provider billing a person we no longer know), or it
    happens after a write."""
    user_id = add_user(maker, EMAIL, answers=1, statuses=statuses, grace_until=grace)
    with pytest.raises(SubscriptionStillLive) as raised:
        delete(maker, EMAIL)
    assert set(raised.value.statuses) == set(statuses) - {"canceled"}
    assert count(maker, User, User.user_id == user_id) == 1
    assert count(maker, Membership, Membership.account_id == user_id) == len(statuses)
    assert count(maker, DeletedAccount) == 0
    # The plan shows it too, so the dry run can say so before --yes.
    plan = run(maker, lambda s: plan_deletion(s, EMAIL))
    assert set(plan.live_subscriptions) == set(statuses) - {"canceled"}


@pytest.mark.parametrize("statuses", [(), ("canceled",), ("incomplete_expired",)])
def test_an_ended_or_absent_subscription_does_not_block(
    maker: Maker, statuses: tuple[str, ...]
) -> None:
    """Turns red if: the refusal also blocks ended subscriptions, so an
    ex-customer could never be deleted."""
    user_id = add_user(maker, EMAIL, statuses=statuses)
    delete(maker, EMAIL)
    assert count(maker, User, User.user_id == user_id) == 0
    assert count(maker, Membership, Membership.account_id == user_id) == 0


# ---------------------------------------------------------------------------
# The deletion and its tombstone
# ---------------------------------------------------------------------------


def test_delete_removes_the_account_and_every_row_that_references_it(maker: Maker) -> None:
    """Turns red if: the users row survives, a referencing table keeps the
    account's rows, or a different account is touched."""
    user_id = add_user(maker, EMAIL, answers=1)
    other_id = add_user(maker, OTHER_EMAIL, answers=1)

    async def _seed(s: AsyncSession) -> None:
        await seed_every_reference(s, user_id, NOW)
        await seed_every_reference(s, other_id, NOW)
        await s.commit()

    run(maker, _seed)
    report = delete(maker, EMAIL)
    assert report.user_id == user_id
    assert count(maker, User, User.user_id == user_id) == 0
    for table, (column, ondelete) in user_references().items():
        model = Base.metadata.tables[table]
        assert count(maker, model, model.c[column] == user_id) == 0, table
        # Partner: the other account still has its row in every table.
        assert count(maker, model, model.c[column] == other_id) >= 1, table
        if ondelete == "SET NULL":
            assert count(maker, model, model.c[column].is_(None)) == 1, table
    # Kept by design: spend rows and the payment-event log.
    assert count(maker, ProviderCall, ProviderCall.user_id == user_id) == 1
    assert count(maker, StripeEvent, StripeEvent.account_id == user_id) == 1


def test_the_tombstone_records_the_trial_used_and_adds_on_a_second_delete(maker: Maker) -> None:
    """Turns red if: the tombstone is not written, counts BYOK answers, or a
    second deletion of the same address overwrites the first count instead of
    adding to it (delete, re-sign-up, delete again would reset the trial)."""
    add_user(maker, EMAIL, answers=3)
    first = delete(maker, EMAIL)
    stone = tombstone(maker, EMAIL)
    assert stone is not None
    assert (stone.trial_answers_used, first.trial_answers_used) == (3, 3)
    assert first.tombstone_total == 3
    # The address itself is not stored anywhere in the row.
    assert EMAIL not in repr(vars(stone)).lower()

    add_user(maker, EMAIL, answers=2)
    later = NOW + timedelta(days=40)
    second = delete(maker, EMAIL.upper(), now=later)
    stone = tombstone(maker, EMAIL)
    assert stone is not None
    assert stone.trial_answers_used == 5
    assert (second.trial_answers_used, second.carried_over, second.tombstone_total) == (2, 3, 5)
    stamp = stone.deleted_at if stone.deleted_at.tzinfo else stone.deleted_at.replace(tzinfo=UTC)
    assert stamp == later
    assert count(maker, DeletedAccount) == 1


# ---------------------------------------------------------------------------
# The trial carries over to a re-sign-up
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "again", ["person@example.com", "PERSON@Example.COM", " person@example.com "]
)
def test_a_re_sign_up_with_the_same_address_keeps_its_trial_count(maker: Maker, again: str) -> None:
    """Turns red if: usage_for stops adding the tombstone to the trial count,
    or matches the address without normalising it."""
    add_user(maker, EMAIL, answers=4)
    delete(maker, EMAIL)
    # Sign-up stores the normalised address (normalize_email); the hash
    # normalises again, so even a raw spelling here must match.
    new_id = add_user(maker, again, answers=1)
    usage = trial_used(maker, new_id)
    assert (usage.kind, usage.used) == ("trial", 5)


def test_a_different_address_is_not_affected(maker: Maker) -> None:
    """Turns red if: the tombstone lookup ignores the address (every account
    would inherit someone else's count)."""
    add_user(maker, EMAIL, answers=4)
    delete(maker, EMAIL)
    other_id = add_user(maker, OTHER_EMAIL, answers=1)
    assert trial_used(maker, other_id).used == 1


def test_pro_monthly_is_not_affected_by_a_tombstone(maker: Maker) -> None:
    """Turns red if: the tombstone is added for the monthly allowance too (a
    returning customer would start a paid month already part used)."""
    add_user(maker, EMAIL, answers=4)
    delete(maker, EMAIL)
    new_id = add_user(maker, EMAIL)
    pro = trial_used(maker, new_id, Tier.pro)
    assert (pro.kind, pro.used) == ("monthly", 0)
    assert trial_used(maker, new_id).used == 4  # partner: the tombstone is there


def test_a_session_deleted_elsewhere_is_gone_even_if_this_session_still_holds_it(
    maker: Maker,
) -> None:
    """Review F1, the identity-map half. A request that still holds the
    Session object it loaded (say from ``require_session``) must not trust
    that copy after another connection deleted the account. Turns red if:
    ``_ensure_user`` reads the session from the identity map (both
    ``populate_existing`` AND ``with_for_update`` dropped: either alone
    makes SQLAlchemy skip the identity map) or re-creates it, instead of
    raising ``SessionGone``."""
    from app.answer.orchestrator import Orchestrator, SessionGone
    from app.models import Session as ChatSession

    user_id = add_user(maker, EMAIL)
    sid = uuid.uuid4()

    async def _go() -> None:
        async with maker() as s:
            s.add(
                ChatSession(
                    session_id=sid,
                    user_id=user_id,
                    created_at=NOW,
                    expires_at=NOW + timedelta(days=1),
                )
            )
            await s.commit()
        async with maker() as s:
            held = await s.get(ChatSession, sid)  # kept alive in the identity map
            assert held is not None
            async with maker() as other:
                await delete_account(other, EMAIL, NOW)
            with pytest.raises(SessionGone):
                await Orchestrator(Settings(llm_provider="stub"), s)._ensure_user(sid)
            await s.rollback()

    asyncio.run(_go())
    assert count(maker, User) == 0  # no demo_user made
    assert count(maker, Message) == 0


def test_an_account_with_no_address_skips_the_tombstone_lookup(maker: Maker) -> None:
    """Review A6. OAuth can store ``email=None`` (oauth.py). Turns red if:
    usage_for hashes a missing address (it would raise) instead of skipping
    the tombstone lookup."""
    user_id = add_user(maker, None, answers=2)
    usage = trial_used(maker, user_id)
    assert (usage.kind, usage.used) == ("trial", 2)


# ---------------------------------------------------------------------------
# By account id: accounts with no stored address (review F2)
# ---------------------------------------------------------------------------


def test_an_account_with_no_address_is_deleted_by_its_id(maker: Maker) -> None:
    """oauth.py stores ``email=None`` for an unverified, non-ASCII or taken
    provider address, so such an account cannot be named by address. Turns red
    if: --user-id cannot find it, or a tombstone is written for it (there is
    no address to key it by, and such an account never had a trial)."""
    user_id = add_user(maker, None, answers=1)
    plan = run(maker, lambda s: plan_deletion(s, user_id=user_id))
    assert plan.user_id == user_id
    report = run(maker, lambda s: delete_account(s, None, NOW, user_id=user_id))
    assert report.user_id == user_id
    assert report.tombstone_total is None
    assert count(maker, User, User.user_id == user_id) == 0
    assert count(maker, DeletedAccount) == 0


def test_by_id_an_account_with_an_address_still_gets_its_tombstone(maker: Maker) -> None:
    """Turns red if: the id path skips the tombstone for an account that has
    an address (it could then restart its trial)."""
    user_id = add_user(maker, EMAIL, answers=2)
    report = run(maker, lambda s: delete_account(s, None, NOW, user_id=user_id))
    assert report.tombstone_total == 2
    stone = tombstone(maker, EMAIL)
    assert stone is not None and stone.trial_answers_used == 2


@pytest.mark.parametrize("bad", ["demo_user", "admin", "anon_" + "a" * 32, "usr_nobody"])
def test_by_id_only_an_existing_registered_account_is_found(maker: Maker, bad: str) -> None:
    """Turns red if: --user-id can delete a shared or anonymous principal
    (``demo_user`` holds every bearer-token visitor's chats) or invents one."""

    async def _seed(s: AsyncSession) -> None:
        for user_id in ("demo_user", "admin", "anon_" + "a" * 32):
            s.add(User(user_id=user_id, role=UserRole.demo_user, created_at=NOW))
        await s.commit()

    run(maker, _seed)
    with pytest.raises(AccountNotFound):
        run(maker, lambda s: delete_account(s, None, NOW, user_id=bad))
    assert count(maker, User) == 3


def test_exactly_one_of_address_or_id_is_required(maker: Maker) -> None:
    """Turns red if: both or neither are accepted (which one wins would be a
    guess)."""
    with pytest.raises(ValueError):
        run(maker, lambda s: plan_deletion(s))
    with pytest.raises(ValueError):
        run(maker, lambda s: plan_deletion(s, EMAIL, user_id="usr_x"))


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_db(maker: Maker, monkeypatch: pytest.MonkeyPatch) -> Maker:
    monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: maker)
    return maker


def test_the_command_without_yes_prints_the_plan_and_changes_nothing(
    cli_db: Maker, capsys: Any
) -> None:
    """Turns red if: the command deletes without --yes."""
    from app.worker import cli

    user_id = add_user(cli_db, EMAIL, answers=2)
    assert cli.main(["delete-account", "--email", EMAIL]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out
    assert "answer_usage" in out
    assert count(cli_db, User, User.user_id == user_id) == 1
    assert count(cli_db, AnswerUsage, AnswerUsage.user_id == user_id) == 3
    assert count(cli_db, DeletedAccount) == 0


def test_the_command_with_yes_deletes_and_reports(cli_db: Maker, capsys: Any) -> None:
    """Turns red if: --yes does not delete, or the report is not printed."""
    from app.worker import cli

    user_id = add_user(cli_db, EMAIL, answers=2)
    assert cli.main(["delete-account", "--email", EMAIL, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Deleted" in out
    assert "trial answers used: 2" in out.lower()
    assert count(cli_db, User, User.user_id == user_id) == 0
    stone = tombstone(cli_db, EMAIL)
    assert stone is not None and stone.trial_answers_used == 2


def test_the_command_exits_1_for_an_unknown_address(cli_db: Maker, capsys: Any) -> None:
    """Turns red if: an unknown address exits 0 (the operator would tell the
    requester their data was deleted when nothing matched)."""
    from app.worker import cli

    add_user(cli_db, OTHER_EMAIL)
    assert cli.main(["delete-account", "--email", EMAIL]) == 1
    assert cli.main(["delete-account", "--email", EMAIL, "--yes"]) == 1
    assert "No account" in capsys.readouterr().out
    assert count(cli_db, User) == 1


def test_the_command_exits_1_when_the_address_matches_two_accounts(
    cli_db: Maker, capsys: Any
) -> None:
    """Turns red if: the ambiguous case crashes with a traceback or exits 0."""
    from app.worker import cli

    add_user(cli_db, "Person@example.com")
    add_user(cli_db, EMAIL)
    assert cli.main(["delete-account", "--email", EMAIL, "--yes"]) == 1
    assert "2 accounts" in capsys.readouterr().out
    assert count(cli_db, User) == 2


def test_the_command_exits_2_for_a_live_subscription(cli_db: Maker, capsys: Any) -> None:
    """Turns red if: a live subscription does not stop the command, or the
    message does not say to cancel it at the payment provider first."""
    from app.worker import cli

    user_id = add_user(cli_db, EMAIL, statuses=("active",))
    assert cli.main(["delete-account", "--email", EMAIL, "--yes"]) == 2
    out = capsys.readouterr().out
    assert "payment provider" in out
    assert count(cli_db, User, User.user_id == user_id) == 1
    # The dry run says so up front, with the same exit code.
    assert cli.main(["delete-account", "--email", EMAIL]) == 2
    assert "payment provider" in capsys.readouterr().out


def test_the_command_deletes_by_user_id(cli_db: Maker, capsys: Any) -> None:
    """Review F2. Turns red if: --user-id is not wired to the deletion."""
    from app.worker import cli

    user_id = add_user(cli_db, None)
    assert cli.main(["delete-account", "--user-id", user_id]) == 0
    assert count(cli_db, User, User.user_id == user_id) == 1  # dry run
    assert cli.main(["delete-account", "--user-id", user_id, "--yes"]) == 0
    assert "Deleted" in capsys.readouterr().out
    assert count(cli_db, User, User.user_id == user_id) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["delete-account"],
        ["delete-account", "--email", EMAIL, "--user-id", "usr_x"],
    ],
)
def test_the_command_needs_exactly_one_of_email_or_user_id(cli_db: Maker, argv: list[str]) -> None:
    """Turns red if: the two flags stop being one required, exclusive group."""
    from app.worker import cli

    add_user(cli_db, EMAIL)
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)
    assert raised.value.code == 2  # argparse's usage error
    assert count(cli_db, User) == 1


@pytest.mark.parametrize("where", ["plan_deletion", "delete_account"])
def test_an_unexpected_error_exits_3_and_changes_nothing(
    cli_db: Maker, capsys: Any, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """Review F5. Turns red if: an unexpected failure escapes as a traceback,
    or exits 1 (which means "no such account" and would mislead the
    operator)."""
    from app.worker import cli

    user_id = add_user(cli_db, EMAIL)

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("database went away")

    monkeypatch.setattr(f"app.services.account_deletion.{where}", boom)
    assert cli.main(["delete-account", "--email", EMAIL, "--yes"]) == 3
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "Nothing was changed" in out
    assert count(cli_db, User, User.user_id == user_id) == 1
