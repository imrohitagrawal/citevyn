"""Invariants for the Fly.io deployment config (``fly.toml``).

``fly.toml`` is production configuration that nothing else in the repo
executes, so a wrong value is invisible until it is live — and several of the
values are *load-bearing* in ways an editor would not guess:

* ``[env]`` is stored in git in PLAINTEXT. A credential added there (rather
  than via ``fly secrets set``) is committed and, in a public repo, published.
* The rate limiter's client-IP header must stay ``Fly-Client-IP``. Behind
  Fly's proxy the socket peer is always the proxy, and Fly overwrites that
  header on every inbound request, so it is the only value a client cannot
  forge. (If Cloudflare proxying is ever enabled the correct value becomes
  ``CF-Connecting-IP`` — see ``docs/DEPLOY_FLY.md`` §5.2. This test then has
  to be updated deliberately, in the same change, which is the point.)
* The health check must point at ``/health`` (pure liveness) and NOT at
  ``/health/dependencies``, which returns 503 when Postgres is unreachable —
  a transient Neon blip would get the machine replaced, which fixes nothing.
* The release command must invoke alembic via ``python -m``: the venv's
  ``alembic`` console-script shebang points at the builder-stage path and does
  not resolve in the runtime image.

See ``docs/DEPLOY_FLY.md`` for the operator runbook these values belong to.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FLY_TOML = REPO_ROOT / "fly.toml"


@pytest.fixture(scope="module")
def fly_config() -> dict[str, Any]:
    assert FLY_TOML.is_file(), f"{FLY_TOML} is missing"
    with FLY_TOML.open("rb") as handle:
        return tomllib.load(handle)


def test_fly_toml_parses_as_toml(fly_config: dict[str, Any]) -> None:
    """Happy path: the file flyctl reads is well-formed and names the app."""
    assert fly_config["app"] == "citevyn"
    assert fly_config["primary_region"]


def test_release_command_runs_migrations_via_python_dash_m(
    fly_config: dict[str, Any],
) -> None:
    release_command = fly_config["deploy"]["release_command"]
    assert release_command.startswith("python -m alembic"), (
        "invoke alembic via `python -m`: the console script's shebang points at "
        "the builder-stage venv path and does not exist in the runtime image"
    )
    assert "--config /db/alembic.ini" in release_command
    assert release_command.endswith("upgrade head")


def test_machine_is_the_measured_256mb_shared_cpu_shape(
    fly_config: dict[str, Any],
) -> None:
    vms = fly_config["vm"]
    assert len(vms) == 1, "this deployment is deliberately a single machine"
    assert vms[0]["size"] == "shared-cpu-1x"
    assert vms[0]["memory"] == "256mb"


def test_machine_scales_to_zero(fly_config: dict[str, Any]) -> None:
    service = fly_config["http_service"]
    assert service["internal_port"] == 8000
    assert service["force_https"] is True
    assert service["auto_start_machines"] is True
    assert service["auto_stop_machines"] in ("stop", True)
    assert service["min_machines_running"] == 0


def test_health_check_probes_liveness_not_dependencies(
    fly_config: dict[str, Any],
) -> None:
    checks = fly_config["http_service"]["checks"]
    assert len(checks) == 1
    check = checks[0]
    assert check["method"] == "GET"
    assert check["path"] == "/health", (
        "/health/dependencies 503s on a transient Postgres blip; Fly would "
        "replace a machine that is not actually broken"
    )


def test_rate_limiter_reads_the_unforgeable_fly_header(
    fly_config: dict[str, Any],
) -> None:
    env = fly_config["env"]
    assert env["CITEVYN_RATE_LIMIT_ENABLED"] == "true"
    assert env["CITEVYN_RATE_LIMIT_CLIENT_IP_HEADER"] == "Fly-Client-IP"


def test_production_guards_and_spend_ceiling_are_set(
    fly_config: dict[str, Any],
) -> None:
    env = fly_config["env"]
    # Turns on the parse-time config guards (no stub LLM, no default keys).
    assert env["CITEVYN_ENVIRONMENT"] == "production"
    assert env["CITEVYN_LLM_PROVIDER"] != "stub"
    # The owner's daily ceiling.
    assert env["CITEVYN_COST_HARD_DAILY_USD"] == "2"
    assert float(env["CITEVYN_COST_SOFT_DAILY_USD"]) < float(env["CITEVYN_COST_HARD_DAILY_USD"])


# Names whose values are credentials or contain them. ``*_URL`` is included
# because both connection strings CiteVyn needs (the Neon DSN and the Upstash
# rediss:// URL) embed a password in the userinfo.
_SECRET_NAME = re.compile(r"(API_KEY|SECRET|TOKEN|PASSWORD|_DSN|_URL)$")


def test_no_secret_shaped_setting_is_committed_in_plaintext(
    fly_config: dict[str, Any],
) -> None:
    """Failure path: a credential in ``[env]`` is committed in cleartext.

    ``fly secrets set`` is the only correct home for these (encrypted at rest,
    injected into both the app machine and the release machine). Committing one
    here publishes it with the repo.
    """
    offenders = sorted(name for name in fly_config["env"] if _SECRET_NAME.search(name))
    assert offenders == [], (
        f"secret-shaped settings must be set with `fly secrets set`, not committed "
        f"in fly.toml [env]: {offenders}"
    )


def test_no_env_value_looks_like_a_credential(fly_config: dict[str, Any]) -> None:
    """Edge case: a credential smuggled in under an innocuous NAME.

    The name-shape check above is blind to
    ``CITEVYN_ENDPOINT = "postgresql://user:hunter2@host/db"``. Match on the
    value instead: a userinfo-bearing URI, or a long opaque provider-key token.
    """
    credential_value = re.compile(
        r"://[^/\s]+:[^/@\s]+@"  # scheme://user:password@host
        r"|\b(sk-|AIza|xox[baprs]-|ghp_)"  # common provider key prefixes
    )
    offenders = sorted(
        name for name, value in fly_config["env"].items() if credential_value.search(str(value))
    )
    assert offenders == [], (
        f"these fly.toml [env] values look like credentials; move them to "
        f"`fly secrets set`: {offenders}"
    )
