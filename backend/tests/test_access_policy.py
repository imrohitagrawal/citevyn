"""The access policy (ADR-0005, Phase 1): capabilities, tiers, the setting.

Three things are pinned here:

1. **The policy table's shape.** Tiers only ever add capabilities (anonymous ⊂
   free ⊂ pro), and ``operate`` is granted by no tier.
2. **The documented table equals the code's table.** ``docs/ACCESS_POLICY.md``
   is what a reader trusts; it is parsed and compared both ways, capability by
   capability and route by route.
3. **The setting.** With ``CITEVYN_ACCESS_MODEL_ENABLED`` false (the default),
   today's public flows are unchanged — anonymous chat included — and the
   capability check never touches the database. With it true, a caller whose
   tier lacks a capability is refused with the right code.

The per-route labels themselves are pinned in ``test_route_auth_inventory.py``,
next to the credential classes they must agree with.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.access import deps as access_deps
from app.access.deps import requires
from app.access.policy import (
    CREDENTIAL_ONLY,
    POLICY,
    Allowance,
    Capability,
    Tier,
    has_capability,
    lowest_tier_with,
)
from app.core import db as db_module
from app.core.config import Settings, get_settings
from app.core.errors import APIErrorCode
from app.main import create_app
from app.models import Base

DOC = Path(__file__).resolve().parents[2] / "docs" / "ACCESS_POLICY.md"
DEMO_BEARER = {"Authorization": "Bearer local-demo-key"}
TIER_ORDER = (Tier.anonymous, Tier.free, Tier.pro)


# ---------------------------------------------------------------------------
# 1. The table's shape
# ---------------------------------------------------------------------------


def test_the_policy_has_a_row_for_every_tier() -> None:
    """Turns red if: a tier is added to the enum with no row in POLICY."""
    assert set(POLICY) == set(Tier)


def test_each_tier_only_ever_adds_capabilities() -> None:
    """Paying more never loses a capability. Turns red if: a capability is given
    to a lower tier but not a higher one."""
    for lower, higher in zip(TIER_ORDER, TIER_ORDER[1:], strict=False):
        missing = POLICY[lower].capabilities - POLICY[higher].capabilities
        assert not missing, f"{higher} lacks {sorted(missing)} that {lower} has"


def test_operate_is_granted_by_no_tier() -> None:
    """Operator routes are reached with the admin API key, never by a plan.
    Turns red if: any tier's row includes ``operate``."""
    for tier, row in POLICY.items():
        assert not (row.capabilities & CREDENTIAL_ONLY), tier
    assert Capability.operate in CREDENTIAL_ONLY  # partner: the set is not empty


def test_every_capability_is_granted_somewhere() -> None:
    """A capability no tier and no credential grants is unreachable dead config.
    Turns red if: a capability is added to the enum and to no row."""
    granted = set().union(*(row.capabilities for row in POLICY.values())) | CREDENTIAL_ONLY
    assert granted == set(Capability)


def test_the_locked_tier_decisions() -> None:
    """ADR-0005 §1. Turns red if: anonymous can chat, free cannot, a Pro feature
    leaks to free, or an allowance kind changes."""
    assert not has_capability(Tier.anonymous, Capability.chat)
    assert has_capability(Tier.anonymous, Capability.sign_in)
    assert has_capability(Tier.free, Capability.chat)
    assert not has_capability(Tier.free, Capability.mcp_ask)
    assert has_capability(Tier.pro, Capability.mcp_ask)
    assert has_capability(Tier.free, Capability.weekly_digest)
    assert not has_capability(Tier.free, Capability.watch_alerts)
    assert POLICY[Tier.anonymous].allowance is Allowance.none
    assert POLICY[Tier.free].allowance is Allowance.once
    assert POLICY[Tier.pro].allowance is Allowance.monthly


def test_lowest_tier_with() -> None:
    """Turns red if: the "sign in" vs "upgrade" choice picks the wrong tier."""
    assert lowest_tier_with(Capability.public) is Tier.anonymous
    assert lowest_tier_with(Capability.chat) is Tier.free
    assert lowest_tier_with(Capability.byok) is Tier.pro
    assert lowest_tier_with(Capability.operate) is None


def test_the_allowance_numbers_are_settings_with_the_locked_defaults() -> None:
    """ADR-0005: 25 answers once, 1,000 a month, and the model is OFF by default.
    Turns red if: a default changes."""
    fields = Settings.model_fields
    assert fields["access_model_enabled"].default is False
    assert fields["access_free_trial_answers"].default == 25
    assert fields["access_pro_monthly_answers"].default == 1000


# ---------------------------------------------------------------------------
# 2. The documented table equals the code's table
# ---------------------------------------------------------------------------


def _doc_section(title: str) -> list[str]:
    text = DOC.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"docs/ACCESS_POLICY.md has no '## {title}' section"
    return [ln for ln in match.group(1).splitlines() if ln.startswith("| `")]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


_CELL = {"yes": True, "—": False}


def _documented_capability_table() -> dict[str, dict[str, bool]]:
    rows: dict[str, dict[str, bool]] = {}
    for line in _doc_section("Capabilities by tier"):
        cells = _cells(line)
        name = cells[0].strip("`")
        # A repeated row would let a wrong row be silently overwritten by a
        # right one; an unknown cell ("maybe") would silently read as "no".
        assert name not in rows, f"capability {name!r} appears twice in the document"
        tier_cells = cells[1 : 1 + len(TIER_ORDER)]
        assert all(c in _CELL for c in tier_cells), f"{name}: cells must be yes or —: {tier_cells}"
        rows[name] = {tier.value: _CELL[c] for tier, c in zip(TIER_ORDER, tier_cells, strict=True)}
    return rows


def test_the_documented_capability_table_equals_the_code() -> None:
    """Turns red if: the code grants a capability the document does not show,
    or the document shows one the code does not grant (either direction)."""
    documented = _documented_capability_table()
    assert documented, "no capability rows parsed"  # partner: parsing found rows
    in_code = {
        cap.value: {tier.value: has_capability(tier, cap) for tier in TIER_ORDER}
        for cap in Capability
    }
    assert documented == in_code


def _documented_routes() -> dict[tuple[str, str], str]:
    routes: dict[tuple[str, str], str] = {}
    for line in _doc_section("Routes"):
        method, path, capability = (c.strip("`") for c in _cells(line)[:3])
        assert (method, path) not in routes, f"{method} {path} appears twice in the document"
        routes[(method, path)] = capability
    return routes


def test_the_doc_parser_rejects_duplicate_and_unknown_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Turns red if: the parser accepts a duplicate row (a wrong row placed above a
    right one would then pass) or reads an unknown cell as "no"."""
    import tests.test_access_policy as me

    text = DOC.read_text(encoding="utf-8")
    good = "| `chat` | — | yes | yes | Ask a live question |"
    assert good in text  # partner: the row being tampered with exists
    for bad in (
        good.replace("| — |", "| maybe |", 1),
        "| `chat` | yes | yes | yes | wrong |\n" + good,
    ):
        copy = tmp_path / "ACCESS_POLICY.md"
        copy.write_text(text.replace(good, bad), encoding="utf-8")
        monkeypatch.setattr(me, "DOC", copy)
        with pytest.raises(AssertionError):
            _documented_capability_table()
    route = "| `POST` | `/v1/sessions` | `chat` |"
    assert route in text
    copy.write_text(text.replace(route, "| `POST` | `/v1/sessions` | `public` |\n" + route))
    with pytest.raises(AssertionError):
        _documented_routes()


def test_the_documented_route_table_equals_the_code() -> None:
    """Turns red if: a route's label in code and in the document differ, or a
    route exists in only one of them."""
    from tests.test_route_auth_inventory import capability_inventory

    documented = _documented_routes()
    assert len(documented) >= 30  # partner: the parser really read the table
    in_code = {key: cap.value for key, cap in capability_inventory().items()}
    assert documented == in_code


# ---------------------------------------------------------------------------
# 3. The setting
# ---------------------------------------------------------------------------


@pytest.fixture
def app_db(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Generator[None, None, None]:
    """A file-backed sqlite database with the schema, and fresh caches."""
    import app.core.rate_limit as rate_limit

    db_module.reset_engine()
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    get_settings.cache_clear()
    rate_limit.reset_limiter()

    async def _init() -> None:
        async with db_module.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield
    get_settings.cache_clear()
    db_module.reset_engine()
    rate_limit.reset_limiter()


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITEVYN_ACCESS_MODEL_ENABLED", "true")
    get_settings.cache_clear()


def _public_flows(client: TestClient) -> dict[str, int]:
    """Today's anonymous public flows, as the browser runs them. Status codes only."""
    out: dict[str, int] = {}
    out["health"] = client.get("/health").status_code
    out["about"] = client.get("/about").status_code
    out["me"] = client.get("/v1/auth/me", headers=DEMO_BEARER).status_code
    created = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO_BEARER)
    out["create_session"] = created.status_code
    if created.status_code == 201:
        sid = created.json()["session_id"]
        out["ask"] = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"message": "How do I set the model in Claude Code?"},
            headers=DEMO_BEARER,
        ).status_code
        out["read_session"] = client.get(f"/v1/sessions/{sid}", headers=DEMO_BEARER).status_code
    out["my_sessions"] = client.get("/v1/me/sessions", headers=DEMO_BEARER).status_code
    out["exact_search"] = client.post(
        "/v1/search/exact",
        json={"product_area": "claude_code", "term": "--model"},
        headers=DEMO_BEARER,
    ).status_code
    return out


def test_with_the_setting_off_anonymous_public_flows_are_unchanged(
    app_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE flag-off proof (ADR-0005): with the default setting, an anonymous
    visitor can still ask, read and search exactly as before.

    Turns red if: any capability check enforces while the setting is false.
    """
    assert get_settings().access_model_enabled is False
    with TestClient(create_app()) as client:
        flows = _public_flows(client)
    assert flows == {
        "health": 200,
        "about": 200,
        # 401 today too: "who am I" with no session cookie yet is "nobody".
        "me": 401,
        "create_session": 201,
        "ask": 200,
        "read_session": 200,
        "my_sessions": 200,
        "exact_search": 200,
    }


def test_with_the_setting_off_the_check_never_resolves_a_tier(
    app_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off must mean OFF: no extra database work on any request. Turns red if:
    ``requires`` resolves the caller's tier before checking the setting."""
    calls: list[str] = []

    async def spy(*_a: Any, **_k: Any) -> Tier:
        calls.append("resolve")
        return Tier.free

    monkeypatch.setattr(access_deps, "resolve_tier", spy)

    def no_db() -> Any:
        calls.append("sessionmaker")
        raise AssertionError("the capability check opened a database session")

    # Also the session factory, so a change that reads the database directly
    # (not through resolve_tier) before checking the setting is caught too.
    monkeypatch.setattr(access_deps, "get_sessionmaker", no_db)
    with TestClient(create_app()) as client:
        flows = _public_flows(client)
    assert calls == []
    assert flows["ask"] == 200  # partner: the flows really ran


def test_with_the_setting_on_the_same_flows_close_to_anonymous(
    app_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partner to the flag-off proof: the SAME flows, with the setting on, are
    refused where ADR-0005 says so — which shows the flag-off test would notice
    enforcement. Health, the About page and "who am I" stay open ("who am I"
    answers 401 with no session either way; the route's own answer, not ours).

    Turns red if: the setting is ignored, or a public door closes.
    """
    _enable(monkeypatch)
    with TestClient(create_app()) as client:
        flows = _public_flows(client)
        refused = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO_BEARER)
    assert flows == {
        "health": 200,
        "about": 200,
        # 401 today too: "who am I" with no session cookie yet is "nobody".
        "me": 401,
        "create_session": 401,
        "my_sessions": 401,
        "exact_search": 401,
    }
    body = refused.json()
    assert body["error"]["code"] == APIErrorCode.auth_required.value
    assert body["error"]["details"] == {"capability": "chat", "required_tier": "free"}


def test_a_visitor_minted_before_launch_is_still_anonymous_after_it(
    app_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch day: visitors already hold an ``anon_`` session cookie minted while
    the setting was off. Turning it on must not treat that cookie as an account.

    Turns red if: tier resolution counts any resolved principal as registered.
    """
    with TestClient(create_app()) as client:
        before = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO_BEARER)
        assert before.status_code == 201  # the anonymous cookie now exists
        assert client.cookies, "partner: a session cookie was really minted"
        _enable(monkeypatch)
        after = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO_BEARER)
    assert after.status_code == 401
    assert after.json()["error"]["code"] == APIErrorCode.auth_required.value


def test_with_the_setting_on_a_signed_in_account_can_chat(
    app_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turns red if: a registered account resolves to the anonymous tier."""
    _enable(monkeypatch)
    with TestClient(create_app()) as client:
        reg = client.post(
            "/v1/auth/register",
            json={"email": "a@example.com", "password": "correct horse battery"},
            headers=DEMO_BEARER,
        )
        assert reg.status_code == 201
        created = client.post("/v1/sessions", json={"channel": "chat"}, headers=DEMO_BEARER)
        mine = client.get("/v1/me/sessions", headers=DEMO_BEARER)
    assert created.status_code == 201
    assert mine.status_code == 200


def test_with_the_setting_on_a_free_account_is_refused_a_pro_capability(
    app_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Pro route exists yet, so a throwaway route carries a Pro capability.
    Turns red if: a free account passes, or the refusal is not ``plan_required``."""
    _enable(monkeypatch)

    async def free(*_a: Any, **_k: Any) -> Tier:
        return Tier.free

    monkeypatch.setattr(access_deps, "resolve_tier", free)
    probe = FastAPI()

    @probe.get("/probe", dependencies=[Depends(requires(Capability.mcp_ask))])
    async def _probe() -> dict[str, str]:
        return {"ok": "yes"}

    # A bare FastAPI app has no envelope-flattening handler (that lives in
    # app.main), so the envelope sits under FastAPI's default "detail" key.
    res = TestClient(probe).get("/probe")
    assert res.status_code == 403
    error = res.json()["detail"]["error"]
    assert error["code"] == APIErrorCode.plan_required.value
    assert error["details"] == {"capability": "mcp_ask", "required_tier": "pro"}


def test_plan_required_reaches_the_client_flattened_through_the_real_app(
    app_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real app flattens the error envelope; the bare probe above does not.
    Turns red if: the 403 body or its ``details`` do not survive app.main's handler."""
    _enable(monkeypatch)

    async def free(*_a: Any, **_k: Any) -> Tier:
        return Tier.free

    monkeypatch.setattr(access_deps, "resolve_tier", free)
    app = create_app()

    @app.get("/v1/_probe_pro", dependencies=[Depends(requires(Capability.mcp_ask))])
    async def _probe() -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as client:
        res = client.get("/v1/_probe_pro")
    assert res.status_code == 403
    assert res.json()["error"] == {
        "code": "plan_required",
        "message": "Your plan does not include this.",
        "details": {"capability": "mcp_ask", "required_tier": "pro"},
    }


def test_with_the_setting_on_a_pro_account_passes_a_pro_capability(
    app_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partner: the refusal above is about the tier, not the route.
    Turns red if: the check refuses a tier that has the capability."""
    _enable(monkeypatch)

    async def pro(*_a: Any, **_k: Any) -> Tier:
        return Tier.pro

    monkeypatch.setattr(access_deps, "resolve_tier", pro)
    probe = FastAPI()

    @probe.get("/probe", dependencies=[Depends(requires(Capability.mcp_ask))])
    async def _probe() -> dict[str, str]:
        return {"ok": "yes"}

    assert TestClient(probe).get("/probe").status_code == 200


def test_operate_is_never_decided_by_a_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the setting on, an ``operate`` route is left entirely to the admin key.
    Turns red if: ``requires(operate)`` resolves a tier (it would refuse every
    operator, since no tier has ``operate``)."""
    _enable(monkeypatch)
    calls: list[str] = []

    async def spy(*_a: Any, **_k: Any) -> Tier:
        calls.append("resolve")
        return Tier.anonymous

    monkeypatch.setattr(access_deps, "resolve_tier", spy)
    probe = FastAPI()

    @probe.get("/op", dependencies=[Depends(requires(Capability.operate))])
    async def _op() -> dict[str, str]:
        return {"ok": "yes"}

    assert TestClient(probe).get("/op").status_code == 200
    assert calls == []


def test_with_the_setting_on_the_open_doors_never_resolve_a_tier(
    app_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``public`` and ``sign_in`` routes must not read the database for the tier:
    a health probe has to answer while the database is down, and sign-in is how
    an anonymous caller becomes someone. Turns red if: ``requires`` resolves the
    tier before checking whether anonymous callers already have the capability.
    """
    _enable(monkeypatch)
    calls: list[str] = []

    async def spy(*_a: Any, **_k: Any) -> Tier:
        calls.append("resolve")
        return Tier.anonymous

    monkeypatch.setattr(access_deps, "resolve_tier", spy)
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/about").status_code == 200
        assert client.get("/v1/auth/me", headers=DEMO_BEARER).status_code == 401
    assert calls == []


def test_requires_marks_its_capability_for_the_route_inventory() -> None:
    """The inventory reads the label off the dependency. Turns red if: the marker
    attribute is renamed or dropped."""
    dep = requires(Capability.history)
    assert access_deps.capability_of(dep) is Capability.history
    assert access_deps.capability_of(lambda: None) is None
