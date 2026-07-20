"""Unit tests for the uniform error envelope factory."""

from __future__ import annotations

import pytest

from app.core.errors import (
    APIErrorCode,
    ErrorEnvelope,
    error_response,
    status_code_for,
)


def test_status_code_for_auth_required_is_401() -> None:
    assert status_code_for(APIErrorCode.auth_required) == 401


def test_status_code_for_validation_error_is_422() -> None:
    assert status_code_for(APIErrorCode.validation_error) == 422


def test_status_code_for_rate_limited_is_429() -> None:
    assert status_code_for(APIErrorCode.rate_limited) == 429


def test_status_code_for_internal_error_is_500() -> None:
    assert status_code_for(APIErrorCode.internal_error) == 500


def test_error_response_builds_http_exception_with_envelope() -> None:
    exc = error_response(
        request_id="req_abc",
        code=APIErrorCode.auth_required,
        message="nope",
    )

    assert exc.status_code == 401
    assert exc.headers == {"WWW-Authenticate": "Bearer"}
    detail = exc.detail
    assert isinstance(detail, dict)
    assert detail["request_id"] == "req_abc"
    assert detail["status"] == "error"
    assert detail["error"]["code"] == "auth_required"
    assert detail["error"]["message"] == "nope"


def test_error_response_details_round_trip() -> None:
    exc = error_response(
        request_id="req_xyz",
        code=APIErrorCode.validation_error,
        message="bad field",
        details={"field": "message", "reason": "too_long"},
    )

    detail = exc.detail
    assert isinstance(detail, dict)
    err = detail["error"]
    assert err["code"] == "validation_error"
    assert err["details"] == {"field": "message", "reason": "too_long"}


def test_error_envelope_model_validates_against_pydantic() -> None:
    env = ErrorEnvelope(
        request_id="req_1",
        error={"code": APIErrorCode.internal_error, "message": "boom"},
    )

    assert env.status == "error"
    assert env.error.code is APIErrorCode.internal_error


@pytest.mark.parametrize(
    "code,expected",
    [
        (APIErrorCode.auth_required, 401),
        (APIErrorCode.validation_error, 422),
        (APIErrorCode.rate_limited, 429),
        (APIErrorCode.ingestion_failed, 500),
        (APIErrorCode.evaluation_failed, 500),
        (APIErrorCode.index_unavailable, 503),
        (APIErrorCode.cost_limit_reached, 503),
        (APIErrorCode.rate_limiter_unavailable, 503),
        (APIErrorCode.internal_error, 500),
    ],
)
def test_status_code_mapping_is_stable(code: APIErrorCode, expected: int) -> None:
    assert status_code_for(code) == expected


def _spec_error_codes() -> set[str]:
    """Parse the ``| code | meaning |`` rows out of ``docs/API_SPEC.md`` §15.

    A substring search over the whole document cannot detect the drift that
    matters (a code missing from the TABLE but mentioned in prose, or a table
    row for a code that no longer exists), so we parse the section for real.
    """
    from pathlib import Path

    spec = Path(__file__).resolve().parents[2] / "docs" / "API_SPEC.md"
    lines = spec.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("## 15."))
    codes: set[str] = set()
    for line in lines[start + 1 :]:
        if line.startswith("## "):  # next section — stop
            break
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        # Skip the header row and the ``|---|---|`` separator.
        if len(cells) < 2 or cells[0] in {"Code", ""} or set(cells[0]) <= {"-", ":"}:
            continue
        codes.add(cells[0])
    return codes


# Framework-level fallbacks: uniform-envelope plumbing, deliberately NOT part
# of the published contract, so §15 must not list them.
_TRANSPORT_HELPERS = {
    APIErrorCode.validation_error,
    APIErrorCode.not_found,
    APIErrorCode.internal_error,
}


def test_api_spec_section_15_matches_the_error_code_enum_both_ways() -> None:
    """§15 and :class:`APIErrorCode` must agree in BOTH directions (#167).

    Forward drift: a public code added to the enum but never documented (the
    original #167 risk — the frontend branches on it). Reverse drift: a row
    left in the table for a code that was renamed or deleted, which sends
    integrators after a code the server can never emit. A bare
    ``value in text`` substring check catches neither.
    """
    expected = {code.value for code in APIErrorCode if code not in _TRANSPORT_HELPERS}

    assert _spec_error_codes() == expected


def test_spec_parity_check_does_not_see_transport_helpers() -> None:
    """Guard the guard: the §15 parser must really be reading table rows.

    If ``_spec_error_codes`` degenerated into a whole-file substring scan it
    would pick up ``validation_error`` etc. from elsewhere in the spec and the
    set-equality test above would start passing (or failing) for the wrong
    reason.
    """
    documented = _spec_error_codes()

    assert documented, "parsed nothing — the §15 heading or table shape moved"
    for helper in _TRANSPORT_HELPERS:
        assert helper.value not in documented


# ---------------------------------------------------------------------------
# Wire-shape (boundary) tests
#
# The unit tests above assert what ``error_response`` BUILDS. These assert
# what a client actually RECEIVES, which is a different thing: the envelope
# lives in ``HTTPException.detail`` and FastAPI's default handler serializes
# that as ``{"detail": {...}}``. Nothing above could see that nesting, which
# is exactly how the frontend half of #167 shipped as dead code.
# ---------------------------------------------------------------------------


@pytest.fixture
def wire_client(monkeypatch: pytest.MonkeyPatch):
    """A TestClient over the REAL app whose rate limiter is a broken Redis.

    ``get_limiter`` is patched rather than the module-level singleton because
    :func:`app.core.rate_limit.get_limiter` rebuilds the limiter whenever the
    cached one does not match current settings — assigning to ``_limiter``
    would be silently discarded.
    """
    from fastapi.testclient import TestClient

    from app.core import rate_limit
    from app.main import create_app

    class _BrokenRedis:
        async def eval(self, *args, **kwargs):  # noqa: ANN001
            import redis.exceptions

            raise redis.exceptions.ConnectionError("simulated outage")

    broken = rate_limit.RedisRateLimiter(
        client=_BrokenRedis(),  # type: ignore[arg-type]
        window_seconds=60,
        demo_user_per_window=3,
        admin_per_window=10,
        key_prefix="citevyn:rl:wire",
    )
    monkeypatch.setattr(rate_limit, "get_limiter", lambda settings: broken)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


def test_limiter_outage_wire_body_is_flat_not_nested_under_detail(wire_client) -> None:
    """#167 (dead-code defect): the code must be readable at ``body.error.code``.

    The frontend reads ``body.error.code``. If the envelope stays wrapped in
    ``detail`` that read is ``undefined``, ``errorCode()`` returns null, and
    the ``rate_limiter_unavailable`` UI branch can never fire — which is what
    the first cut of this fix actually shipped.
    """
    response = wire_client.post(
        "/v1/sessions",
        json={"user_id": "demo_user", "channel": "chat"},
        headers={"Authorization": "Bearer local-demo-key"},
    )

    assert response.status_code == 503
    body = response.json()
    assert "detail" not in body, f"envelope is still nested: {body}"
    assert body["error"]["code"] == APIErrorCode.rate_limiter_unavailable.value
    # docs/API_SPEC.md §4: the error body is flat {request_id, status, error}.
    assert body["status"] == "error"
    assert body["request_id"]


def test_auth_required_wire_body_is_flat_and_keeps_the_challenge_header(
    wire_client,
) -> None:
    """Flattening must not drop ``WWW-Authenticate`` (RFC 7235) from the 401."""
    response = wire_client.post("/v1/sessions", json={"user_id": "demo_user"})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    body = response.json()
    assert "detail" not in body
    assert body["error"]["code"] == APIErrorCode.auth_required.value


def test_framework_raised_404_also_gets_the_standard_envelope(wire_client) -> None:
    """Edge case: Starlette's own 404 never had an envelope — now it does.

    Otherwise a client hitting a typo'd path gets ``{"detail": "Not Found"}``
    and needs a second parser for the one error the app did not raise itself.
    """
    body = wire_client.get("/v1/definitely-not-a-route").json()

    assert body["error"]["code"] == APIErrorCode.not_found.value
    assert body["status"] == "error"
