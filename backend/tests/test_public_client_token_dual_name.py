"""Both spellings of the public client token resolve to one setting (#430).

WHY THIS FILE EXISTS
--------------------
``CITEVYN_DEMO_API_KEY`` is being renamed to ``CITEVYN_PUBLIC_CLIENT_TOKEN``. The
value is PUBLIC -- ``infra/docker/Dockerfile.api`` bakes it into the JS bundle at
build time, so any visitor can read it out of ``/assets/index-*.js`` -- and its
job (``docs/SECURITY_MODEL.md``) is a CSRF guard plus a rate-limit turnstile,
explicitly "not, on its own, an identity control". The old ``*_API_KEY`` name put
it in the Fly secrets list beside four values that must never be disclosed, and
that mismatch has now produced two real errors: the owner read the bundled token
as a leaked provider key, and #416 was filed as a production-key exposure.

A FLAG-DAY rename re-runs release v6 (2026-09-02), which shipped a mismatched
build argument and 401'd every browser call for about an hour while ``/health``
stayed green -- #296. So BOTH names are read, new one first, until the Fly secret
and the deploy build argument have moved.

WHY THE RESOLUTION IS TESTED RATHER THAN REASONED ABOUT
-------------------------------------------------------
``Settings`` sets ``env_prefix="CITEVYN_"``, and pydantic-settings does NOT apply
that prefix to a field carrying a ``validation_alias`` -- so the aliases must
spell ``CITEVYN_`` out, and an alias written without it reads nothing while the
suite stays green (the default is a legal value, so there is no error to notice).
Two measured facts drove the implementation, neither of which is obvious:

* ``AliasChoices`` order is PRIORITY: the first name present wins.
* setting a ``validation_alias`` REPLACES the field name as an input key, so
  without ``populate_by_name=True`` a ``Settings(public_client_token=...)`` kwarg
  is SILENTLY DROPPED -- measured: the value came back from the environment with
  no error raised. Dozens of tests construct ``Settings`` that way.

RED BITE (each assertion, and the one change that turns it red)
---------------------------------------------------------------
* ``test_the_old_name_alone_still_resolves`` -- delete ``"CITEVYN_DEMO_API_KEY"``
  from the field's ``AliasChoices``. This is the assertion that makes the rename
  a two-step migration instead of a flag day.
* ``test_the_new_name_alone_resolves`` -- delete
  ``"CITEVYN_PUBLIC_CLIENT_TOKEN"``, or write either alias WITHOUT the
  ``CITEVYN_`` prefix (the ``env_prefix`` trap above).
* ``test_the_new_name_wins_when_both_are_set`` -- swap the two ``AliasChoices``
  entries. Nothing else in the suite can see that swap: with one name set they
  behave identically, so only the both-set case distinguishes them.
* ``test_neither_name_set_falls_back_to_the_published_default`` -- change the
  field's ``default=``.
* ``test_a_kwarg_by_field_name_is_not_silently_dropped`` -- remove
  ``populate_by_name=True`` from ``model_config``.
* ``test_production_still_refuses_a_weak_token_under_either_name`` -- weaken or
  delete ``_reject_weak_public_client_token_in_production``. That validator is
  what makes a botched rename fail LOUDLY at boot rather than serve 401s behind a
  green ``/health``, so it is the load-bearing half of the migration being safe.
* ``test_the_production_error_names_the_variable_to_set`` -- point the message
  back at the deprecated spelling, which would tell a new operator to set the
  name being retired.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings

_NEW = "CITEVYN_PUBLIC_CLIENT_TOKEN"
_OLD = "CITEVYN_DEMO_API_KEY"

# Long enough to clear the 16-character production floor, and visibly distinct
# from one another so a test cannot pass by reading the wrong variable.
#
# WORD-SHAPED ON PURPOSE, and every fixture value in this file follows it. An
# earlier draft padded these with `-0123456789`; gitleaks' `generic-api-key` rule
# scored that at 4.2 entropy and failed the required `quality-gate` check on a
# value that is a literal in a test file and a secret nowhere.
#
# The fix is the test DATA, never an allowlist entry. Silencing a scanner to
# accommodate a fixture is how a real leak gets waved through later, and #430 --
# the issue this file exists for -- is about secret scanning being trustworthy.
#
# BOTH DIRECTIONS MEASURED, in a throwaway repo holding only this file (gitleaks
# 8.30.1), because "the finding went away" on its own is exactly the shape of a
# guard that stopped working:
#   * this file as shipped                                 -> no leaks found
#   * `public_client_token = "<openssl rand -base64 32>"`   -> leaks found: 1
#   * `public_client_token = "client-token-long-enough..."` -> no leaks found
# The middle line is the one that matters: a genuine secret in this exact
# position is still caught. (A first attempt at that control planted the value
# under `_PLANTED = ...` and found nothing -- the rule needs a keyword like
# `token`/`key`/`secret` adjacent to the value, so a control without one proves
# nothing. Recorded because it would have read as a passing check.)
#
# Keep new fixtures to lowercase words and hyphens: long enough for the
# 16-character production floor, boring enough to score low.
_VIA_NEW = "token-from-the-new-name-not-a-real-secret"
_VIA_OLD = "token-from-the-old-name-not-a-real-secret"

# The published default. Not a secret; it is printed in README.md and
# .env.example, which is exactly why production refuses it.
_PUBLISHED_DEFAULT = "local-demo-key"


@pytest.fixture(autouse=True)
def _clear_both_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither name may leak in from the developer's shell.

    Without this the "neither set" case reads whatever is exported and passes for
    the wrong reason -- the failure mode this repo has already hit in
    ``tests/shell/test_env_guard.sh``.
    """
    monkeypatch.delenv(_NEW, raising=False)
    monkeypatch.delenv(_OLD, raising=False)


# Every case below builds ``Settings(_env_file=None)`` so a developer's real
# ``backend/.env`` cannot colour the result: this module is about name
# resolution, not about their configuration.


# ---------------------------------------------------------------------------
# The five resolution cases.
# ---------------------------------------------------------------------------


def test_the_old_name_alone_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of shipping this as step 1-2 rather than a flag day."""
    monkeypatch.setenv(_OLD, _VIA_OLD)
    assert Settings(_env_file=None).public_client_token == _VIA_OLD  # type: ignore[call-arg]


def test_the_new_name_alone_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches an alias written without the ``CITEVYN_`` prefix.

    ``env_prefix`` is NOT re-applied to an aliased field, so a bare
    ``"PUBLIC_CLIENT_TOKEN"`` alias reads nothing -- and reads nothing SILENTLY,
    because the field has a legal default.
    """
    monkeypatch.setenv(_NEW, _VIA_NEW)
    assert Settings(_env_file=None).public_client_token == _VIA_NEW  # type: ignore[call-arg]


def test_the_new_name_wins_when_both_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AliasChoices`` order is priority, and only this case can observe it.

    During the migration a machine legitimately carries both: the old Fly secret
    is still set when the new one is added. If the OLD name won, setting the new
    secret would appear to do nothing and the cutover would silently never happen.
    """
    monkeypatch.setenv(_OLD, _VIA_OLD)
    monkeypatch.setenv(_NEW, _VIA_NEW)
    assert Settings(_env_file=None).public_client_token == _VIA_NEW  # type: ignore[call-arg]


def test_neither_name_set_falls_back_to_the_published_default() -> None:
    """Unchanged behaviour: a plain local checkout still boots with no env at all."""
    assert Settings(_env_file=None).public_client_token == _PUBLISHED_DEFAULT  # type: ignore[call-arg]


def test_an_empty_value_is_taken_literally_and_rejected_by_min_length() -> None:
    """An EMPTY variable is not the same as an unset one, in either spelling.

    ``min_length=1`` is what turns ``CITEVYN_PUBLIC_CLIENT_TOKEN=`` into a loud
    boot failure rather than a bearer of ``""``, in EVERY environment -- the
    production validator is not involved. Recorded because the FRONTEND half of
    this rename deliberately does the opposite (an empty *new* build arg falls
    through to the old one), and the two must not be assumed to match.

    MEASURED, not assumed: pydantic reports the failure under the ALIAS the
    operator actually set, not under the field name. That is the useful direction
    -- the message names the variable they need to go and fix -- so it is pinned
    here rather than left to chance.
    """
    for name in (_NEW, _OLD):
        with pytest.raises(ValueError) as excinfo:
            Settings(_env_file=None, **{name: ""})  # type: ignore[call-arg]
        message = str(excinfo.value)
        assert "at least 1 character" in message, message
        assert name in message, (
            f"the validation error does not name {name}, the variable that was set:\n{message}"
        )


def test_an_empty_ENVIRONMENT_variable_is_present_and_beats_a_strong_other_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction every shell consumer had to be corrected for (#430 review).

    ``AliasChoices`` selects the first alias PRESENT in the environment, and an
    exported-but-empty variable IS present -- so an empty
    ``CITEVYN_PUBLIC_CLIENT_TOKEN`` wins over a perfectly good
    ``CITEVYN_DEMO_API_KEY`` and the app refuses to boot rather than falling
    through. That is NOT the same rule as the shell's ``${VAR:-fallback}``, which
    treats empty as absent.

    Three reviewers independently found ``_env_guard.sh`` blessing exactly this
    ``.env`` while the api died at boot on it. The guard now resolves the name
    from the ``.env`` FILE, the same source the container is fed from;
    ``tests/shell/test_env_guard.sh`` case 11s is the other half of this pair, and
    this is the half that pins the BEHAVIOUR the guard has to mirror.

    The kwarg case above cannot stand in for this one: it proves pydantic rejects
    an empty VALUE, not that an empty ENVIRONMENT VARIABLE counts as present. If
    pydantic-settings ever started treating an empty env var as unset, that test
    would stay green and this one would go red -- which is the whole point, because
    the shell guard would then be wrong in the other direction.
    """
    monkeypatch.setenv(_NEW, "")
    monkeypatch.setenv(_OLD, "old-name-value-long-enough-for-the-floor")
    with pytest.raises(ValueError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    message = str(excinfo.value)
    assert "at least 1 character" in message, message
    assert _NEW in message, message


def test_an_UNSET_new_name_does_fall_through_to_a_strong_old_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner for the case above: UNSET and EMPTY must behave differently.

    Without this, "empty new name is rejected" would be satisfied by a Settings
    that ignored the new alias entirely, and the migration would not work at all.
    """
    monkeypatch.delenv(_NEW, raising=False)
    monkeypatch.setenv(_OLD, "old-name-value-long-enough-for-the-floor")
    assert (
        Settings(_env_file=None).public_client_token  # type: ignore[call-arg]
        == "old-name-value-long-enough-for-the-floor"
    )


def test_a_kwarg_by_field_name_is_not_silently_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """``populate_by_name=True`` is required, and its absence is SILENT.

    Measured before the fix: with a ``validation_alias`` and no
    ``populate_by_name``, the kwarg was ignored and the ENVIRONMENT value was
    kept -- no error, no warning. Dozens of tests in this suite construct
    ``Settings(public_client_token=...)``, so every one of them would have been
    asserting against the wrong value.
    """
    monkeypatch.setenv(_NEW, _VIA_NEW)
    settings = Settings(_env_file=None, public_client_token="kwarg-value-long-enough-for-the-floor")  # type: ignore[call-arg]
    assert settings.public_client_token == "kwarg-value-long-enough-for-the-floor", (
        "the kwarg was dropped in favour of the environment -- populate_by_name "
        "is missing from Settings.model_config"
    )


# ---------------------------------------------------------------------------
# The production validator: what makes a botched rename fail LOUDLY.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [_NEW, _OLD], ids=["new-name", "deprecated-name"])
@pytest.mark.parametrize(
    "weak",
    [_PUBLISHED_DEFAULT, "LOCAL-DEMO-KEY", "  local-demo-key ", "short", ""],
    ids=["default", "upper", "padded", "too-short", "empty"],
)
def test_production_still_refuses_a_weak_token_under_either_name(
    monkeypatch: pytest.MonkeyPatch, name: str, weak: str
) -> None:
    """The rename must not create a path into production that skips the guard.

    Checked through BOTH spellings: a deploy still carrying only the old Fly
    secret must be refused exactly as before, or the migration would have opened
    a window in which the publicly-known default boots in production.
    """
    monkeypatch.setenv("CITEVYN_ENVIRONMENT", "production")
    monkeypatch.setenv("CITEVYN_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("CITEVYN_ADMIN_API_KEY", "admin-key-long-enough-for-the-floor")
    monkeypatch.setenv(name, weak)
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("name", [_NEW, _OLD], ids=["new-name", "deprecated-name"])
def test_a_strong_token_boots_in_production_under_either_name(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Partner: without this, a validator that rejected EVERYTHING would pass above.

    An "it raises" assertion is loudest when the thing never succeeds. This is the
    other direction, and it is also the case the owner's machine is in today: the
    old secret alone must still boot production.
    """
    monkeypatch.setenv("CITEVYN_ENVIRONMENT", "production")
    monkeypatch.setenv("CITEVYN_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("CITEVYN_ADMIN_API_KEY", "admin-key-long-enough-for-the-floor")
    monkeypatch.setenv(name, "client-token-long-enough-for-the-floor")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.public_client_token == "client-token-long-enough-for-the-floor"


def test_the_production_error_names_the_variable_to_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """It must name the NEW variable even when the value arrived by the old one.

    An operator reading this message is being told what to fix. Naming the
    deprecated spelling would send them to set the name being retired -- and this
    message is the only place the rename reaches someone whose deploy is already
    broken.
    """
    monkeypatch.setenv("CITEVYN_ENVIRONMENT", "production")
    monkeypatch.setenv("CITEVYN_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("CITEVYN_ADMIN_API_KEY", "admin-key-long-enough-for-the-floor")
    monkeypatch.setenv(_OLD, _PUBLISHED_DEFAULT)
    with pytest.raises(ValueError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    message = str(excinfo.value)
    assert _NEW in message, f"the error does not name {_NEW}:\n{message}"
    # POSITION, not mere presence. Review defeated the presence-only version: the
    # message mentions BOTH names (it has to -- it tells the operator the old one
    # is still read), so a message rewritten to "CITEVYN_DEMO_API_KEY must be set
    # ... (CITEVYN_PUBLIC_CLIENT_TOKEN is also read)" -- exactly the mutation this
    # test's docstring claims to catch -- kept an `_NEW in message` assertion GREEN.
    # The subject of the sentence is what an operator acts on, so that is what is
    # pinned.
    assert message.index(_NEW) < message.index(_OLD), (
        "the deprecated name is named BEFORE the new one, so the sentence tells the "
        f"operator to set the variable being retired:\n{message}"
    )
    assert "the publicly-known default" in message, (
        "the error no longer distinguishes the default from a short value, which is "
        "the half that tells an operator whether they set nothing or set it badly"
    )
