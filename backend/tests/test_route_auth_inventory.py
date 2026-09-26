"""Structural auth inventory over every route the app serves, plus an
executed credential-less call for each route that requires a credential (#451).

Why a structural inventory rather than more hand-written cases
-------------------------------------------------------------
Two hand-maintained lists already existed and had already drifted:

* ``test_admin_routes.py``'s missing-key ``parametrize`` named five of the
  eight routes carrying ``Depends(rate_limited_admin)``. ``/v1/admin/budget``
  is covered by a separate one-line assertion in the same file, so the two
  genuinely uncovered routes were ``GET /v1/admin/evaluations/{run_id}`` and
  ``GET /v1/admin/ingestion_jobs/{job_id}`` — the evaluation-metrics and
  ingestion-failure detail views.
* An AST scan of every ``TestClient`` call in ``backend/tests`` found no
  credential-less call at all to ``POST /v1/auth/register``,
  ``POST /v1/auth/login``, ``POST /v1/auth/logout``, ``GET /v1/auth/me``,
  ``POST /v1/auth/me/password``, ``POST /v1/auth/magic-link/request`` or
  ``GET /v1/me/sessions``.

So this module derives the population from the app instead of from a list a
human has to remember to extend. The credential-less checks below are
``parametrize``\\ d over the LIVE inventory, so a route added tomorrow is
called without a credential tomorrow, with nobody editing this file.

Deriving the population from the app is not, by itself, enough
-------------------------------------------------------------
A ``parametrize`` fed by a classifier silently shrinks when the classifier
mis-reads a route: drop ``Depends(rate_limited_admin)`` from a handler and
that route simply stops being generated, and the suite stays green. So the
classes are ALSO pinned as exact expected sets below. Removing, swapping or
forgetting an auth dependency moves a route between classes, and two of the
three set assertions go red naming it.

That is a deliberate tradeoff: adding a ``/v1`` route means adding a line
here. The alternative — an inventory that accepts whatever it is handed — is
the failure mode this file exists to close.

The walk has to recurse
-----------------------
No route in this app depends on a credential check DIRECTLY.
``require_public_client_token`` sits two levels down (under
``rate_limited_demo``) on seven routes — the six ``/v1/auth/*`` bearer routes
**and** ``POST /v1/search/exact`` — and three levels down (under
``resolve_principal`` -> ``rate_limited_demo``) on the other six:
``GET /v1/me/sessions`` plus the five session/message routes. Seven plus six
is the whole bearer class of thirteen. ``require_admin_api_key`` sits two
levels down under ``rate_limited_admin`` on all eight admin routes.

A direct-dependency-only classifier would therefore put EVERY route in the
credential-less class — which is what ``test_each_auth_class_is_non_empty``
and the depth sentinels below catch.

What this module does NOT assert
--------------------------------
The reason strings in ``EXPECTED_OPEN`` are prose, not assertions. Nothing
here checks that OAuth ``state``/PKCE or a magic-link token actually rejects
anything — ``GET /v1/auth/magic-link/confirm`` answers 200 with no token at
all — and the three OAuth cases 404 in a test environment with no provider
configured, so they never reach the redirect logic. They are a scope
statement about why each route is credential-less by design; each mechanism
is tested in its own module (``test_oauth_routes.py``,
``test_magic_link_routes.py``).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app.access.deps import capability_of
from app.access.policy import Capability
from app.core import db as db_module
from app.core.config import get_settings
from app.core.security import require_admin_api_key, require_public_client_token
from app.main import create_app
from app.models import Base

ADMIN_HEADER = "X-Admin-API-Key"

# Auth classes. The value is what the class MEANS, not just a label: a route
# in ``ADMIN`` must 401 without ``X-Admin-API-Key``, a route in ``BEARER``
# must 401 without ``Authorization``, and a route in ``OPEN`` is reachable
# with no credential AT ALL and must be listed, with a reason, in
# ``EXPECTED_OPEN`` below.
ADMIN = "admin-api-key"
BEARER = "public-bearer"
OPEN = "no-credential"


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def _api_routes() -> tuple[list[Any], list[str]]:
    """Return ``(routes_with_a_dependant, paths_of_dependant_less_ROUTES)``.

    FastAPI >=0.140 wraps each ``include_router`` call in an internal
    ``_IncludedRouter`` node; the real ``APIRoute`` objects live on its
    ``original_router.routes``. Unwrapping both shapes is the same
    defence ``test_session_ownership.py`` already carries — without it a
    future FastAPI bump makes this whole module scan zero routes. The
    unwrap goes exactly ONE level, which is all the app needs (ten flat
    ``include_router`` calls, no nesting); a nested router would surface as
    a dependant-less entry and fail loudly in
    ``test_every_route_without_a_dependant_is_a_known_framework_path``
    rather than slip through.

    Dependant-less ROUTES are returned rather than dropped: silently
    skipping them is exactly how an unclassified route would hide.

    ``Mount`` is excluded, and that is not the same thing as dropping it.
    ``app/main.py`` mounts the built frontend at ``/`` with ``StaticFiles``
    whenever ``backend/frontend_dist`` exists, and a ``Mount``'s ``path`` is
    the empty string once Starlette has normalised it. It serves static
    files, has no endpoint to guard and cannot carry a dependency, so it is
    not a member of any auth class — but it is asserted about on its own
    terms in ``test_the_only_mount_is_the_static_frontend``, because
    swallowing every ``Mount`` silently is how a future ``app.mount`` of a
    real sub-application would escape this module. Without this the whole
    file goes red with ``not in FRAMEWORK_PATHS: ['']`` — naming nothing —
    on any checkout that has a ``frontend_dist`` (a live-OAuth setup
    symlinks one).
    """
    app = create_app()
    flat: list[Any] = []
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        flat.extend(original_router.routes if original_router is not None else [route])

    with_dependant = [r for r in flat if getattr(r, "dependant", None) is not None]
    without = [
        str(getattr(r, "path", r))
        for r in flat
        if getattr(r, "dependant", None) is None and not isinstance(r, Mount)
    ]
    return with_dependant, without


def _mounts() -> list[Any]:
    """Every ``Mount`` on the app, so none is excluded without being checked."""
    app = create_app()
    flat: list[Any] = []
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        flat.extend(original_router.routes if original_router is not None else [route])
    return [r for r in flat if isinstance(r, Mount)]


def _depth_of(dependant: Dependant, target: Any, _depth: int = 1) -> int | None:
    """Shallowest depth at which ``target`` appears in ``dependant``'s tree.

    ``None`` when it does not appear. Depth 1 means a direct dependency of
    the route. Recursion is the point of this helper — see the module
    docstring — and ``test_the_walk_recurses_past_the_first_level`` pins
    that it really does.
    """
    best: int | None = None
    for sub in dependant.dependencies:
        if sub.call is target:
            best = _depth if best is None else min(best, _depth)
        inner = _depth_of(sub, target, _depth + 1)
        if inner is not None:
            best = inner if best is None else min(best, inner)
    return best


def _classify(route: Any) -> str:
    admin = _depth_of(route.dependant, require_admin_api_key) is not None
    bearer = _depth_of(route.dependant, require_public_client_token) is not None
    if admin and bearer:
        # Never true today. If it becomes true, that is a real finding
        # (two credential systems on one route), not something to classify
        # away quietly.
        raise AssertionError(
            f"{sorted(route.methods)} {route.path} resolves BOTH credential checks"
        )
    if admin:
        return ADMIN
    if bearer:
        return BEARER
    return OPEN


IMPLICIT_METHODS = frozenset({"HEAD", "OPTIONS"})


def _inventory() -> dict[tuple[str, str], str]:
    """Map ``(method, path_template)`` to its auth class.

    ``HEAD`` and ``OPTIONS`` are skipped because Starlette synthesises them
    alongside a declared ``GET``; they are not separately declared routes.
    Skipping is a silent drop, though, so
    ``test_every_inspectable_route_contributes_to_the_inventory`` asserts no
    route is left contributing nothing — a route declared only as
    ``@router.head(...)`` would otherwise escape all three exact-set
    assertions and every executed call without a word.
    """
    routes, _ = _api_routes()
    out: dict[tuple[str, str], str] = {}
    for route in routes:
        auth_class = _classify(route)
        for method in sorted(route.methods):
            if method in IMPLICIT_METHODS:
                continue
            out[(method, route.path)] = auth_class
    return out


INVENTORY = _inventory()


# ---------------------------------------------------------------------------
# Capabilities (ADR-0005 Phase 1): every route declares exactly one
# ---------------------------------------------------------------------------


def _capabilities_of(dependant: Dependant) -> list[Capability]:
    """Every capability label anywhere in ``dependant``'s tree, in walk order.

    Recursive for the same reason the credential walk is: a label hidden one
    level down must still be seen, and two route-level labels must be caught,
    not resolved by picking one.

    Not seen: a label passed to ``include_router(dependencies=...)``. FastAPI
    (0.141) enforces it but does not put it in ``route.dependant``, so such a
    route looks unlabelled here and fails CI. That errs strict, never loose;
    label routes individually.
    """
    found: list[Capability] = []
    for sub in dependant.dependencies:
        cap = capability_of(sub.call)
        if cap is not None:
            found.append(cap)
        found.extend(_capabilities_of(sub))
    return found


def _route_capabilities() -> dict[tuple[str, str], list[Capability]]:
    routes, _ = _api_routes()
    out: dict[tuple[str, str], list[Capability]] = {}
    for route in routes:
        caps = _capabilities_of(route.dependant)
        for method in sorted(route.methods):
            if method not in IMPLICIT_METHODS:
                out[(method, route.path)] = caps
    return out


def capability_inventory() -> dict[tuple[str, str], Capability]:
    """``(method, path) -> capability`` for every route. Also read by
    ``test_access_policy.py`` to check ``docs/ACCESS_POLICY.md``.

    Raises rather than guessing when a route has zero or several labels.
    """
    out: dict[tuple[str, str], Capability] = {}
    for key, caps in _route_capabilities().items():
        if len(caps) != 1:
            raise AssertionError(f"{key} declares {len(caps)} capabilities: {caps}")
        out[key] = caps[0]
    return out


# Every route's capability, pinned. Removing a ``requires(...)`` from a route,
# or changing which one it carries, turns this red naming the route.
EXPECTED_CAPABILITY: dict[tuple[str, str], Capability] = {
    ("GET", "/about"): Capability.public,
    ("GET", "/health"): Capability.public,
    ("GET", "/health/dependencies"): Capability.public,
    ("GET", "/health/index"): Capability.public,
    ("POST", "/v1/auth/register"): Capability.sign_in,
    ("POST", "/v1/auth/login"): Capability.sign_in,
    ("POST", "/v1/auth/logout"): Capability.sign_in,
    ("GET", "/v1/auth/me"): Capability.sign_in,
    ("POST", "/v1/auth/magic-link/request"): Capability.sign_in,
    ("GET", "/v1/auth/magic-link/confirm"): Capability.sign_in,
    ("POST", "/v1/auth/magic-link/confirm"): Capability.sign_in,
    ("GET", "/v1/auth/oauth/{provider}/start"): Capability.sign_in,
    ("GET", "/v1/auth/oauth/{provider}/connect/start"): Capability.sign_in,
    ("GET", "/v1/auth/oauth/{provider}/callback"): Capability.sign_in,
    ("POST", "/v1/auth/me/password"): Capability.manage_account,
    ("POST", "/v1/sessions"): Capability.chat,
    ("POST", "/v1/sessions/{session_id}/messages"): Capability.chat,
    ("GET", "/v1/me/sessions"): Capability.history,
    ("GET", "/v1/me/export"): Capability.history,
    ("GET", "/v1/sessions/{session_id}"): Capability.history,
    ("DELETE", "/v1/sessions/{session_id}"): Capability.history,
    ("GET", "/v1/sessions/{session_id}/messages/{message_id}"): Capability.history,
    ("POST", "/v1/search/exact"): Capability.exact_search,
    ("PUT", "/v1/sessions/{session_id}/messages/{message_id}/feedback"): Capability.feedback,
    ("POST", "/v1/source-requests"): Capability.feedback,
    ("GET", "/v1/admin/feedback"): Capability.operate,
    ("POST", "/v1/billing/checkout"): Capability.billing,
    ("POST", "/v1/billing/portal"): Capability.billing,
    ("GET", "/v1/billing/membership"): Capability.billing,
    ("POST", "/v1/billing/webhook"): Capability.public,
    ("GET", "/v1/admin/source_requests"): Capability.operate,
    ("GET", "/v1/admin/budget"): Capability.operate,
    ("GET", "/v1/admin/evaluations"): Capability.operate,
    ("GET", "/v1/admin/evaluations/{run_id}"): Capability.operate,
    ("GET", "/v1/admin/index_versions"): Capability.operate,
    ("GET", "/v1/admin/index_versions/{index_version}"): Capability.operate,
    ("POST", "/v1/admin/index_versions/{index_version}/promote"): Capability.operate,
    ("GET", "/v1/admin/ingestion_jobs"): Capability.operate,
    ("GET", "/v1/admin/ingestion_jobs/{job_id}"): Capability.operate,
}


def test_every_route_declares_exactly_one_capability() -> None:
    """The CI guard: an unlabelled route fails, and so does a doubly labelled one.

    Turns red if: any route loses its ``requires(...)``, or gains a second one.
    """
    wrong = {key: caps for key, caps in _route_capabilities().items() if len(caps) != 1}
    assert wrong == {}, f"routes without exactly one capability: {wrong}"


def test_the_capability_labels_are_exactly_the_expected_map() -> None:
    """Turns red if: a route's label changes, or a route is added or removed
    without updating the pinned map (and ``docs/ACCESS_POLICY.md``)."""
    assert capability_inventory() == EXPECTED_CAPABILITY


def test_the_capability_map_covers_the_whole_credential_inventory() -> None:
    """Partner: the capability walk and the credential walk see the same routes,
    so a route cannot escape one of them. Turns red if: they diverge."""
    assert set(EXPECTED_CAPABILITY) == set(INVENTORY)
    assert len(EXPECTED_CAPABILITY) == 39


def test_operate_is_exactly_the_admin_key_class() -> None:
    """``operate`` is granted only by the admin key, so the two must coincide.
    Turns red if: an admin route is labelled otherwise, or a non-admin route is
    labelled ``operate``."""
    operate = {k for k, c in EXPECTED_CAPABILITY.items() if c is Capability.operate}
    assert operate == set(_class_members(ADMIN))


def test_a_public_route_needs_no_credential() -> None:
    """A ``public`` route behind a credential would be mislabelled.
    Turns red if: a route labelled ``public`` requires the bearer or admin key."""
    public = {k for k, c in EXPECTED_CAPABILITY.items() if c is Capability.public}
    assert public  # partner: the check is not vacuous
    assert all(INVENTORY[k] == OPEN for k in public)


def _class_members(auth_class: str) -> list[tuple[str, str]]:
    return sorted(key for key, value in INVENTORY.items() if value == auth_class)


# ---------------------------------------------------------------------------
# The pinned expectation
# ---------------------------------------------------------------------------

EXPECTED_ADMIN: set[tuple[str, str]] = {
    ("GET", "/v1/admin/budget"),
    ("GET", "/v1/admin/evaluations"),
    ("GET", "/v1/admin/feedback"),
    ("GET", "/v1/admin/source_requests"),
    ("GET", "/v1/admin/evaluations/{run_id}"),
    ("GET", "/v1/admin/index_versions"),
    ("GET", "/v1/admin/index_versions/{index_version}"),
    ("POST", "/v1/admin/index_versions/{index_version}/promote"),
    ("GET", "/v1/admin/ingestion_jobs"),
    ("GET", "/v1/admin/ingestion_jobs/{job_id}"),
}

EXPECTED_BEARER: set[tuple[str, str]] = {
    ("POST", "/v1/auth/login"),
    ("POST", "/v1/auth/logout"),
    ("POST", "/v1/billing/checkout"),
    ("GET", "/v1/billing/membership"),
    ("POST", "/v1/billing/portal"),
    ("POST", "/v1/auth/magic-link/request"),
    ("GET", "/v1/auth/me"),
    ("POST", "/v1/auth/me/password"),
    ("POST", "/v1/auth/register"),
    ("GET", "/v1/me/export"),
    ("GET", "/v1/me/sessions"),
    ("POST", "/v1/search/exact"),
    ("POST", "/v1/source-requests"),
    ("PUT", "/v1/sessions/{session_id}/messages/{message_id}/feedback"),
    ("POST", "/v1/sessions"),
    ("DELETE", "/v1/sessions/{session_id}"),
    ("GET", "/v1/sessions/{session_id}"),
    ("POST", "/v1/sessions/{session_id}/messages"),
    ("GET", "/v1/sessions/{session_id}/messages/{message_id}"),
}

# Reachable with NO credential, by design. Each entry needs a reason, because
# this is the list an attacker reads first.
EXPECTED_OPEN: dict[tuple[str, str], str] = {
    ("GET", "/about"): "public marketing/legal copy",
    ("GET", "/health"): "liveness probe, called by the platform before any key exists",
    ("GET", "/health/dependencies"): "readiness probe, same reason",
    ("GET", "/health/index"): "index/vector-arm health, surfaced in the public UI",
    # ADR-0004 PR 12 / the magic-link flow: these four are reached by a real
    # top-level browser navigation or by a provider's own redirect, neither of
    # which can set an ``Authorization`` header. They carry
    # ``rate_limited_oauth_navigation`` instead, and their own
    # state/PKCE/single-use-token mechanism is the guard.
    ("GET", "/v1/auth/magic-link/confirm"): "email link; the URL token is the credential",
    ("POST", "/v1/auth/magic-link/confirm"): "same flow; the body token is the credential",
    ("GET", "/v1/auth/oauth/{provider}/start"): "browser navigation; state/PKCE guards it",
    ("GET", "/v1/auth/oauth/{provider}/connect/start"): "same, account-linking entry point",
    ("GET", "/v1/auth/oauth/{provider}/callback"): "provider redirect; state/PKCE guards it",
    # ADR-0005 Phase 4: Stripe calls it directly with no browser credential. The
    # Stripe signature over the raw body, inside a replay window, is the credential;
    # it answers 404 unless the access model is on.
    ("POST", "/v1/billing/webhook"): "Stripe webhook; the signature is the credential",
}

# Routes the framework registers that have no ``dependant`` to inspect.
# Present only when ``openapi_url`` is set (it is ``None`` in production), so
# this is a SUBSET assertion, not equality.
FRAMEWORK_PATHS: set[str] = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

# Concrete values for every path parameter in the inventory. Keyed by the
# parameter name so a new route reusing an existing name needs no edit, and
# ``test_every_path_parameter_has_a_substitution`` fails loudly for a name
# that is missing rather than leaving the route silently uncalled.
PATH_PARAM_VALUES: dict[str, str] = {
    "session_id": "00000000-0000-4000-8000-000000000001",
    "message_id": "00000000-0000-4000-8000-000000000002",
    "run_id": "00000000-0000-4000-8000-000000000003",
    "job_id": "00000000-0000-4000-8000-000000000004",
    "index_version": "v1",
    "provider": "github",
}

_PARAM_RE = re.compile(r"\{([^}]+)\}")


def _path_params(path: str) -> list[str]:
    return _PARAM_RE.findall(path)


def _concrete(path: str) -> str:
    return _PARAM_RE.sub(lambda m: PATH_PARAM_VALUES[m.group(1)], path)


# ---------------------------------------------------------------------------
# Fixture: a client with a real (throwaway) database
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Generator[TestClient, None, None]:
    """A client over a file-backed sqlite DB.

    The credential checks run ahead of ``get_session`` in every route's
    dependency order, so a 401 never opens a connection — but giving the app
    a working database means a REGRESSION that reorders the dependencies
    shows up as the wrong status code rather than as a connection error that
    could be mistaken for the 401 under test.
    """
    import app.core.rate_limit as rate_limit
    import app.core.redis_client as redis_client
    from app.llm import factory as llm_factory

    db_module.reset_engine()
    # Same rationale as ``tests/conftest.py``'s ``client`` fixture: these
    # caches are process-wide, so a previous test's closed handle would leak
    # into this app. Resetting the limiter also keeps the handful of
    # credential-less routes this module calls (which DO consume the
    # per-visitor bucket) from spending another module's quota.
    rate_limit.reset_limiter()
    redis_client.reset_redis_client()
    llm_factory.reset_llm_client()
    get_settings.cache_clear()
    monkeypatch.setenv("CITEVYN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'inventory.db'}")
    get_settings.cache_clear()
    engine = db_module.get_engine()

    async def _init_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())
    try:
        yield TestClient(create_app())
    finally:
        get_settings.cache_clear()
        db_module.reset_engine()
        monkeypatch.delenv("CITEVYN_DATABASE_URL", raising=False)


# ---------------------------------------------------------------------------
# The classifier cannot pass by classifying nothing
# ---------------------------------------------------------------------------


def test_each_auth_class_is_non_empty() -> None:
    """The partner for every "no route is missing a guard" assertion below.

    A classifier that returns an empty inventory, or that collapses every
    route into one class, satisfies a "nothing is missing" check vacuously.
    This is the check that the thing being counted exists. It is also what
    catches a direct-dependency-only walk: with no recursion, ``ADMIN`` and
    ``BEARER`` are both empty (see the module docstring).
    """
    assert INVENTORY, "the route walk found no routes at all"
    for auth_class in (ADMIN, BEARER, OPEN):
        assert _class_members(auth_class), (
            f"auth class {auth_class!r} is empty — the walk is vacuous"
        )


@pytest.mark.parametrize(
    "method,path,expected",
    [
        # Ordered deliberately: the first two entries are reachable by a walk
        # that stops after two levels, so a classifier that ``break``s early
        # still gets them right. The entries after them are not.
        ("POST", "/v1/auth/login", BEARER),
        ("GET", "/v1/admin/budget", ADMIN),
        ("GET", "/v1/me/sessions", BEARER),
        ("GET", "/v1/sessions/{session_id}/messages/{message_id}", BEARER),
        ("GET", "/v1/admin/evaluations/{run_id}", ADMIN),
        ("GET", "/health", OPEN),
        ("GET", "/v1/auth/oauth/{provider}/start", OPEN),
    ],
)
def test_named_sentinels_land_in_their_expected_class(
    method: str, path: str, expected: str
) -> None:
    """Named routes, one per class, checked individually.

    Each is its own test case rather than a loop, so nothing can be skipped
    by an early exit, and a failure names the route.
    """
    assert (method, path) in INVENTORY, f"{method} {path} is not in the inventory at all"
    assert INVENTORY[(method, path)] == expected


def test_the_walk_recurses_past_the_first_level() -> None:
    """No credential check is a DIRECT dependency of any route.

    ``require_public_client_token`` is two levels down on ``/v1/auth/login``
    (under ``rate_limited_demo``) and THREE levels down on ``/v1/me/sessions``
    (under ``resolve_principal`` -> ``rate_limited_demo``). Both depths are
    asserted here, and the shallower one first, so a walk that recursed
    exactly one level further than the naive version — enough for
    ``/v1/auth/login`` — still fails on ``/v1/me/sessions``.

    Measured, this is not a two-route claim: the depth-2 group is seven routes
    (the six ``/v1/auth/*`` bearer routes plus ``POST /v1/search/exact``) and
    the depth-3 group is six, which is the whole bearer class. The partition is
    asserted below so the two named sentinels cannot be the only thing holding
    it up.
    """
    routes, _ = _api_routes()
    by_key = {(m, r.path): r for r in routes for m in r.methods}

    login = by_key[("POST", "/v1/auth/login")]
    me_sessions = by_key[("GET", "/v1/me/sessions")]
    budget = by_key[("GET", "/v1/admin/budget")]

    assert _depth_of(login.dependant, require_public_client_token) == 2
    assert _depth_of(me_sessions.dependant, require_public_client_token) == 3
    assert _depth_of(budget.dependant, require_admin_api_key) == 2

    # And the claim that motivates all of the above: nothing is at depth 1.
    shallow = [
        f"{sorted(r.methods)} {r.path}"
        for r in routes
        if _depth_of(r.dependant, require_public_client_token) == 1
        or _depth_of(r.dependant, require_admin_api_key) == 1
    ]
    assert not shallow, (
        "a route now depends on a credential check directly; that is fine, but "
        f"this test's rationale needs rewriting: {shallow}"
    )

    # The whole partition, not just the two sentinels — so the docstring's
    # enumeration above is asserted rather than asserted-about-two-examples.
    # (It was wrong once: an earlier draft said the depth-2 group was the
    # ``/v1/auth/*`` routes and omitted ``POST /v1/search/exact``.)
    by_depth: dict[int, set[tuple[str, str]]] = {}
    for route in routes:
        depth = _depth_of(route.dependant, require_public_client_token)
        if depth is None:
            continue
        for method in route.methods:
            if method not in IMPLICIT_METHODS:
                by_depth.setdefault(depth, set()).add((method, route.path))

    assert sorted(by_depth) == [2, 3], (
        f"bearer depths are no longer exactly 2 and 3: {sorted(by_depth)}"
    )
    assert by_depth[2] == {
        ("POST", "/v1/auth/login"),
        ("POST", "/v1/auth/logout"),
        ("POST", "/v1/auth/magic-link/request"),
        ("GET", "/v1/auth/me"),
        ("POST", "/v1/auth/me/password"),
        ("POST", "/v1/auth/register"),
        ("POST", "/v1/search/exact"),
    }
    assert by_depth[3] == {
        ("POST", "/v1/billing/checkout"),
        ("GET", "/v1/billing/membership"),
        ("POST", "/v1/billing/portal"),
        ("GET", "/v1/me/export"),
        ("GET", "/v1/me/sessions"),
        ("PUT", "/v1/sessions/{session_id}/messages/{message_id}/feedback"),
        ("POST", "/v1/source-requests"),
        ("POST", "/v1/sessions"),
        ("GET", "/v1/sessions/{session_id}"),
        ("DELETE", "/v1/sessions/{session_id}"),
        ("POST", "/v1/sessions/{session_id}/messages"),
        ("GET", "/v1/sessions/{session_id}/messages/{message_id}"),
    }
    # The two groups partition the bearer class exactly: 7 + 6 = 13.
    assert by_depth[2] | by_depth[3] == EXPECTED_BEARER
    assert not by_depth[2] & by_depth[3]


def test_every_inspectable_route_contributes_to_the_inventory() -> None:
    """No route may end up contributing zero ``(method, path)`` entries.

    ``_inventory`` skips ``HEAD`` and ``OPTIONS``, which Starlette synthesises
    alongside a declared ``GET``. A route declared ONLY with those methods
    would therefore contribute nothing at all and escape all three exact-set
    assertions, the non-emptiness check and every executed credential call —
    silently, which is the one failure mode this module is built to prevent.

    Measured today: the declared methods across the app are DELETE, GET and
    POST only, so the skip is currently unreachable. This asserts it stays
    that way instead of trusting it.
    """
    routes, _ = _api_routes()
    contributes_nothing = {
        f"{sorted(r.methods)} {r.path}" for r in routes if not set(r.methods) - IMPLICIT_METHODS
    }
    assert not contributes_nothing, (
        "route(s) declare only HEAD/OPTIONS, so _inventory() drops them entirely "
        f"and no auth assertion in this file can see them: {sorted(contributes_nothing)}"
    )
    # Partner: routes exist and they really do reach the inventory, so the
    # emptiness asserted above is not emptiness of the whole population.
    assert routes, "no inspectable routes at all"
    assert len(INVENTORY) >= len(routes), (
        f"{len(routes)} routes produced only {len(INVENTORY)} inventory entries"
    )


def test_the_credential_checks_the_classifier_knows_are_the_only_ones() -> None:
    """``_classify`` hardcodes two credential dependencies. Pin that.

    A third credential check added to ``app.core.security`` would leave its
    routes classified ``OPEN``, and the fix this file's own docstring suggests
    for an unexpected ``OPEN`` member — "add it to ``EXPECTED_OPEN`` with a
    reason" — would then label a *guarded* route credential-less. That is a
    wrong fix the file would have invited, so the assumption is asserted here
    rather than left implicit.

    Deliberately matched on the naming convention, not on a hand-copied list:
    a new ``require_*`` in that module fails this and has to be triaged.
    """
    import app.core.security as security_module

    requires = {
        name
        for name in dir(security_module)
        if name.startswith("require_") and callable(getattr(security_module, name))
    }
    assert requires == {"require_public_client_token", "require_admin_api_key"}, (
        "app.core.security gained or lost a require_* credential dependency; "
        f"_classify() only knows two of them: {sorted(requires)}"
    )


def test_the_only_mount_is_the_static_frontend(tmp_path) -> None:
    """``_api_routes`` excludes ``Mount``; this is what stops that being a drop.

    ``app/main.py`` mounts the built SPA at ``/`` with ``StaticFiles`` when
    ``backend/frontend_dist`` exists. It has no endpoint and cannot carry an
    auth dependency, so it is in no auth class — but a future
    ``app.mount("/internal", some_asgi_app)`` WOULD serve real endpoints, and
    excluding every ``Mount`` blindly is how it would escape this module.

    Asserted against a real mount rather than against the usual empty list:
    ``frontend_dist`` is gitignored and absent in CI, so this test builds the
    app with ``FRONTEND_DIST`` pointed at a tmp directory. Without that the
    check would pass by having nothing to check.
    """
    import app.main as main_module

    (tmp_path / "index.html").write_text("<html></html>")
    original = main_module.FRONTEND_DIST
    try:
        main_module.FRONTEND_DIST = tmp_path
        mounts = _mounts()
        assert len(mounts) == 1, f"expected exactly the frontend mount, got {mounts}"
        assert isinstance(mounts[0].app, StaticFiles), (
            f"the mount at {mounts[0].path!r} is not StaticFiles but "
            f"{type(mounts[0].app).__name__} — it may serve guarded endpoints, so it "
            "cannot be excluded from the auth inventory without being classified"
        )
        # Partner, and it has to run while FRONTEND_DIST is still under this
        # test's control: point it at a path that does not exist and the mount
        # goes away, which proves the single mount above came from the fixture
        # rather than from something this test does not own.
        #
        # An earlier version asserted this AFTER the `finally` restored the real
        # path, which made it a test of the developer's filesystem. On any
        # checkout that HAS `backend/frontend_dist` — the live-OAuth local setup
        # this repo documents, gitignored and so absent only in CI — it failed,
        # with a message reading "even with no frontend_dist" about a machine
        # where frontend_dist is exactly what is there. Reproduced by creating
        # the directory and running this test: red before, green after.
        main_module.FRONTEND_DIST = tmp_path / "definitely-not-built"
        assert _mounts() == [], (
            "a Mount exists with FRONTEND_DIST pointed at a nonexistent path, so "
            "the mount found above did not come from this test's fixture"
        )
    finally:
        main_module.FRONTEND_DIST = original


def test_every_route_without_a_dependant_is_a_known_framework_path() -> None:
    """Routes the classifier cannot inspect must be named, not ignored.

    ``/openapi.json`` and friends have no ``dependant``. Dropping anything
    without one would mean a future raw Starlette ``Route`` mounted on the app
    escaped every class in this file without a single assertion noticing.
    """
    _, without_dependant = _api_routes()
    unexpected = sorted(set(without_dependant) - FRAMEWORK_PATHS)
    assert not unexpected, (
        f"route(s) with no inspectable dependencies are not in FRAMEWORK_PATHS: {unexpected}"
    )
    # Partner: the framework paths really are present, so the subtraction above
    # is not trivially empty because the list was.
    #
    # Unconditional on purpose. ``create_app`` passes ``openapi_url=None`` when
    # ``environment == "production"``, which removes all four — but the suite
    # cannot run in that mode at all: ``Settings`` refuses
    # ``CITEVYN_LLM_PROVIDER=stub`` under ``CITEVYN_ENVIRONMENT=production``, so
    # ``conftest.py`` fails to import (measured), and the only way past that is
    # a real paid provider. Guarding this branch on the setting would add a
    # false arm no test can ever execute.
    assert without_dependant, "no dependant-less routes found — the control for this test is gone"


# ---------------------------------------------------------------------------
# The classes are exactly what we expect
# ---------------------------------------------------------------------------


def test_the_admin_class_is_exactly_the_expected_set() -> None:
    assert set(_class_members(ADMIN)) == EXPECTED_ADMIN


def test_the_bearer_class_is_exactly_the_expected_set() -> None:
    assert set(_class_members(BEARER)) == EXPECTED_BEARER


def test_the_credential_less_class_is_exactly_the_expected_set() -> None:
    """The one that bites hardest.

    A new ``/v1`` route with no auth dependency, or an existing route whose
    dependency is dropped or swapped for one that does not check a
    credential, lands here and fails — with the route named. Every entry
    carries a reason, so extending the list is a decision somebody makes on
    purpose in a diff a reviewer can see.
    """
    assert set(_class_members(OPEN)) == set(EXPECTED_OPEN)
    assert all(EXPECTED_OPEN.values()), "every credential-less route needs a stated reason"


def test_every_path_parameter_has_a_substitution() -> None:
    """Otherwise a templated route silently never gets called below."""
    missing = sorted(
        {
            name
            for _, path in INVENTORY
            for name in _path_params(path)
            if name not in PATH_PARAM_VALUES
        }
    )
    assert not missing, f"add these to PATH_PARAM_VALUES: {missing}"
    # Partner: the substitution map is exercised at all.
    assert any(_path_params(path) for _, path in INVENTORY), "no templated routes — control gone"


# ---------------------------------------------------------------------------
# Executed credential-less calls, one test case per route
# ---------------------------------------------------------------------------


def _assert_auth_required(response: Any, label: str, *, expect_bearer_challenge: bool) -> None:
    """Assert the whole thing the CLIENT receives, not just the status code.

    ``docs/API_SPEC.md`` §4: the error body is flat ``{request_id, status,
    error}`` — never nested under ``detail``, which is what FastAPI would emit
    if ``error_response`` were bypassed.

    **The header assertion is the load-bearing one, and measurement says so.**
    ``app/main.py`` installs a global handler that re-envelopes ANY
    ``HTTPException`` into that same flat shape. Measured: replacing
    ``error_response`` in ``require_admin_api_key`` with a bare
    ``HTTPException(status_code=401, detail="...")`` produces a byte-identical
    ``{request_id, status, error: {code: "auth_required", ...}}`` body — so the
    ``detail`` / ``status`` / ``code`` assertions below CANNOT detect a bypassed
    ``error_response``. A second mechanism supplies the same observation. The
    only consumer-visible difference is the lost ``WWW-Authenticate`` header.
    The envelope assertions stay because they pin the contract clients parse;
    they are simply not what catches that particular regression.

    ``expect_bearer_challenge`` splits the header check by class rather than
    dropping it. RFC 7235 wants a challenge on every 401, so PRESENCE is
    asserted for both classes. The literal value ``Bearer`` is only *correct*
    on the bearer routes; ``app/core/errors.py`` sends it on the admin routes
    too, whose credential is the ``X-Admin-API-Key`` header. Pinning that value
    there would turn this test red on a correct RFC fix to the scheme — a test
    locking in a defect — so the admin cases require the header to exist
    without dictating what it says.
    """
    assert response.status_code == 401, f"{label} returned {response.status_code}: {response.text}"
    challenge = response.headers.get("WWW-Authenticate")
    assert challenge, f"{label} 401 has no WWW-Authenticate challenge (RFC 7235)"
    if expect_bearer_challenge:
        assert challenge == "Bearer", f"{label} challenge is {challenge!r}, expected 'Bearer'"
    body = response.json()
    assert "detail" not in body, f"{label} envelope is nested under 'detail': {body}"
    assert body["status"] == "error", label
    assert body["request_id"], label
    assert body["error"]["code"] == "auth_required", f"{label} -> {body['error']}"


@pytest.mark.parametrize("method,path", _class_members(ADMIN))
def test_every_admin_route_rejects_a_missing_admin_key(
    client: TestClient, method: str, path: str
) -> None:
    """Derived from the live inventory, so a ninth admin route is covered the
    day it is added. ``test_the_admin_class_is_exactly_the_expected_set`` is
    what stops this parametrize shrinking silently instead."""
    _assert_auth_required(
        client.request(method, _concrete(path)),
        f"{method} {path}",
        expect_bearer_challenge=False,
    )


@pytest.mark.parametrize("method,path", _class_members(BEARER))
def test_every_bearer_route_rejects_a_missing_bearer_token(
    client: TestClient, method: str, path: str
) -> None:
    """No body is sent on the POSTs on purpose: the credential check must run
    BEFORE request-body validation, or an unauthenticated caller learns the
    body schema from a 422. A 422 here is a real finding — and it is what these
    POST cases actually return once the bearer guard is removed, so the 401 is
    the guard and not FastAPI's routing.

    **Known limitation, one case.** ``GET /v1/auth/me`` answers 401
    ``auth_required`` with a VALID public bearer too, because it additionally
    requires a signed-in auth session. Its case here therefore passes whether
    or not the bearer guard is present, and does not pin that guard on its own.
    Measured: removing the missing-credential branch from
    ``require_public_client_token`` turns the other twelve cases red and leaves
    this one green. The twelve carry the proof; this one is coverage.
    """
    _assert_auth_required(
        client.request(method, _concrete(path)),
        f"{method} {path}",
        expect_bearer_challenge=True,
    )


@pytest.mark.parametrize("method,path", _class_members(ADMIN))
def test_every_admin_route_rejects_a_wrong_admin_key(
    client: TestClient, method: str, path: str
) -> None:
    """401, not 403: a wrong key must be indistinguishable from a missing one
    (``require_admin_api_key`` compares with ``secrets.compare_digest``)."""
    response = client.request(method, _concrete(path), headers={ADMIN_HEADER: "wrong"})
    _assert_auth_required(response, f"{method} {path}", expect_bearer_challenge=False)


@pytest.mark.parametrize("method,path", _class_members(BEARER))
def test_every_bearer_route_rejects_a_wrong_bearer_token(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(
        method, _concrete(path), headers={"Authorization": "Bearer not-the-token"}
    )
    _assert_auth_required(response, f"{method} {path}", expect_bearer_challenge=True)


# The credential-less routes whose `!= 401` is carried by an unrelated 404:
# with no OAuth provider configured under test they 404 before reaching their
# own logic. Measured, not assumed. Each is pinned to 404 in the test below, so
# the set cannot quietly become a list of routes nobody checks.
_OAUTH_UNCONFIGURED_404: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/v1/auth/oauth/{provider}/start"),
        ("GET", "/v1/auth/oauth/{provider}/callback"),
        ("GET", "/v1/auth/oauth/{provider}/connect/start"),
    }
)


def test_the_unconfigured_oauth_register_is_complete_and_reachable() -> None:
    """The partner for ``_OAUTH_UNCONFIGURED_404``: it cannot be emptied.

    Measured while writing it, which is why it exists: dropping one entry from
    that frozenset does NOT redden the parametrized test, because the 404 pin
    only fires for members. So the set could be shrunk to nothing and the
    weaker `!= 401` assertion would go back to being silently carried by a 404,
    which is the hole the register was added to close. This asserts the set is
    exactly the OAuth routes the classifier calls credential-less, derived from
    the inventory rather than hand-kept beside it.

    Turns red if: an entry is removed from ``_OAUTH_UNCONFIGURED_404``, one is
    added that is not a credential-less OAuth route, or an OAuth route leaves
    the credential-less class.

    It earned its place on the first run: the hand-written register had
    ``POST .../unlink``, which is NOT credential-less, and was missing
    ``GET .../connect/start``, which is. Two errors in three entries, caught
    before the register could go on quietly excusing the wrong routes.
    """
    oauth_open = {(method, path) for method, path in _class_members(OPEN) if "/oauth/" in path}
    assert oauth_open, (
        "no OAuth route is classified credential-less any more, so this register "
        "and the 404 pin it drives are guarding nothing — delete both or re-derive"
    )
    assert oauth_open == _OAUTH_UNCONFIGURED_404, (
        "_OAUTH_UNCONFIGURED_404 has drifted from the credential-less OAuth routes.\n"
        f"  registered, not credential-less OAuth: "
        f"{sorted(_OAUTH_UNCONFIGURED_404 - oauth_open)}\n"
        f"  credential-less OAuth, not registered: "
        f"{sorted(oauth_open - _OAUTH_UNCONFIGURED_404)}"
    )


@pytest.mark.parametrize("method,path", _class_members(OPEN))
def test_a_credential_less_route_does_not_answer_401(
    client: TestClient, method: str, path: str
) -> None:
    """The counterpart to the two classes above, and the reason they mean
    something: these routes must NOT 401. Without this, a change that made
    every route 401 would leave every "rejects a missing credential"
    assertion in this file green.

    Only the status code's 401-ness is asserted — what these routes do with a
    missing token, missing state or an empty index is each route's own test's
    business.

    WHAT THIS ASSERTION CANNOT SEE ON ITS OWN, recorded rather than left for the
    next reader to measure: the OAuth paths answer 404 with no provider
    configured under test, before reaching any logic of their own. For those,
    ``!= 401`` is satisfied by that 404 and not by anything about credentials.
    ``_OAUTH_UNCONFIGURED_404`` names them and pins the 404, so the day one
    answers something else this file notices instead of shrugging.
    """
    response = client.request(method, _concrete(path), follow_redirects=False)
    assert response.status_code != 401, (
        f"{method} {path} is classified as credential-less but answered 401 — "
        "either it gained a credential dependency (update EXPECTED_OPEN) or the "
        "classifier is wrong"
    )
    if (method, path) in _OAUTH_UNCONFIGURED_404:
        assert response.status_code == 404, (
            f"{method} {path} is registered as answering 404 with no OAuth provider "
            f"configured, but answered {response.status_code}. While it sits in "
            "_OAUTH_UNCONFIGURED_404 the `!= 401` assertion above is carried by that "
            "404 rather than by the route's own credential handling; if the route "
            "now really does handle a missing credential, take it out of the set."
        )


@pytest.mark.parametrize(
    "method,path,expected_status",
    [
        ("GET", "/v1/me/sessions", 200),
        ("POST", "/v1/sessions", 201),
    ],
)
def test_a_bearer_route_accepts_the_bearer_token(
    client: TestClient, method: str, path: str, expected_status: int
) -> None:
    """Absence-as-success partner for the 401 parametrizes.

    Every executed check above asserts a REJECTION. A route that rejected
    everything — a typo in the expected token, a dependency that always
    raises, a 401 emitted by middleware ahead of the router — would pass all
    of them. These accepted calls prove the 401s above are the gate closing
    rather than the gate being welded shut.

    Both routes are in the depth-3 ``resolve_principal`` group, which is also
    the group whose bearer check the shallow walk described in the module
    docstring would miss. ``GET /v1/auth/me`` is deliberately NOT used here:
    it 401s with ``auth_required`` even WITH a valid public bearer, because it
    additionally requires a signed-in auth session — which would have made
    this partner pass while proving nothing.
    """
    body = {"channel": "chat"} if method == "POST" else None
    response = client.request(
        method,
        path,
        json=body,
        headers={"Authorization": f"Bearer {get_settings().public_client_token}"},
    )
    assert response.status_code == expected_status, response.text


def test_an_admin_route_accepts_the_admin_key(client: TestClient) -> None:
    """Same partner, for the admin class."""
    response = client.get(
        "/v1/admin/index_versions", headers={ADMIN_HEADER: get_settings().admin_api_key}
    )
    assert response.status_code == 200, response.text


def test_uuid_substitutions_are_real_uuids() -> None:
    """Guards the harness, not the app: a malformed value would make every
    templated route above 422 on path validation instead of 401, and a 422
    would look like a routing miss rather than a broken fixture."""
    for name in ("session_id", "message_id", "run_id", "job_id"):
        uuid.UUID(PATH_PARAM_VALUES[name])
