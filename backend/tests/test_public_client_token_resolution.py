"""How ``CITEVYN_PUBLIC_CLIENT_TOKEN`` resolves, now that it is the only name (#430).

WHY THIS FILE EXISTS
--------------------
The value is PUBLIC -- ``infra/docker/Dockerfile.api`` bakes it into the JS bundle
at build time, so any visitor can read it out of ``/assets/index-*.js`` -- and its
job (``docs/SECURITY_MODEL.md``) is a CSRF guard plus a rate-limit turnstile,
explicitly "not, on its own, an identity control". The old ``CITEVYN_DEMO_API_KEY``
spelling put it in the Fly secrets list beside four values that must never be
disclosed, and that mismatch produced two real errors: the owner read the bundled
token as a leaked provider key, and #416 was filed as a production-key exposure.

The rename shipped in two steps, because a FLAG DAY here re-runs release v6
(2026-09-02), which shipped a mismatched build argument and 401'd every browser
call for about an hour while ``/health`` stayed green -- #296. Step 1 accepted
BOTH names. THIS FILE IS AFTER STEP 2: the Fly secret has moved (``fly secrets
list`` shows only ``CITEVYN_PUBLIC_CLIENT_TOKEN``, and ``printenv
CITEVYN_DEMO_API_KEY`` exits 1 inside the running container), so the deprecated
alias is deleted and the old name reads NOTHING.

The dual-name cases that used to live here went with the alias. What is kept is
everything that still describes reality -- the new name resolves, an empty value
does not become the default, production refuses a weak one and boots on a strong
one -- plus new cases proving the OLD name is genuinely inert. "The alias is gone"
is an ABSENCE, and an absence is exactly the kind of claim that can be made
without being true, so it is asserted from three directions: the old name alone
reaches the published default, an unset new name does not fall through to it, and
a strong old name alone is REFUSED at a production boot.

WHY THE RESOLUTION IS TESTED RATHER THAN REASONED ABOUT
-------------------------------------------------------
``Settings`` sets ``env_prefix="CITEVYN_"``, and pydantic-settings does NOT apply
that prefix to a field carrying a ``validation_alias`` -- so the alias must spell
``CITEVYN_`` out, and an alias written without it reads nothing while the suite
stays green (the default is a legal value, so there is no error to notice).

Setting a ``validation_alias`` also REPLACES the field name as an input key, so
without ``populate_by_name=True`` a ``Settings(public_client_token=...)`` kwarg is
SILENTLY DROPPED -- measured: the value came back from the environment with no
error raised. Dozens of tests construct ``Settings`` that way.

RED BITE (each assertion, and the one change that turns it red)
---------------------------------------------------------------
* ``test_the_new_name_alone_resolves`` -- delete the ``validation_alias``, or
  write it WITHOUT the ``CITEVYN_`` prefix (the ``env_prefix`` trap above).
* ``test_the_retired_name_alone_is_ignored`` /
  ``test_an_UNSET_new_name_does_NOT_fall_through_to_the_retired_one`` /
  ``test_a_strong_retired_name_alone_is_refused_in_production`` -- re-add
  ``"CITEVYN_DEMO_API_KEY"`` to the field's aliases. These three are what make the
  removal a fact rather than a claim.
* ``test_an_empty_value_is_taken_literally_and_rejected_by_min_length`` -- delete
  ``min_length=1``. The field would then carry ``""`` into the bearer, i.e. a bare
  ``Authorization: Bearer ``, which is the wire shape of the #296 outage.
* ``test_neither_name_set_falls_back_to_the_published_default`` -- change the
  field's ``default=``.
* ``test_a_kwarg_by_field_name_is_not_silently_dropped`` -- remove
  ``populate_by_name=True`` from ``model_config``.
* ``test_production_refuses_a_weak_token`` -- weaken or delete
  ``_reject_weak_public_client_token_in_production``.
* ``test_a_strong_token_boots_in_production`` -- its partner. Without it a
  validator that rejected EVERYTHING would satisfy the row above, and that is not
  hypothetical: the first draft of the parametrised rejection test passed while
  every case was in fact dying on an unrelated ``CITEVYN_LLM_PROVIDER``
  validator. Only the boots-on-strong control exposed it, which is why
  ``_production_env`` exists.
* ``test_the_production_error_names_the_variable_to_set`` -- point the message at
  another variable, or re-introduce the retired spelling into it.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings

_NEW = "CITEVYN_PUBLIC_CLIENT_TOKEN"
_RETIRED = "CITEVYN_DEMO_API_KEY"

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
_VIA_RETIRED = "token-from-the-old-name-not-a-real-secret"

# The published default. Not a secret; it is printed in README.md and
# .env.example, which is exactly why production refuses it.
_PUBLISHED_DEFAULT = "local-demo-key"

# Strong enough to boot production. Used under BOTH names on purpose: the same
# value is accepted through the new one and refused through the retired one, so
# neither result can be explained by the value itself.
_STRONG = "client-token-long-enough-for-the-floor"


@pytest.fixture(autouse=True)
def _clear_both_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither name may leak in from the developer's shell.

    Without this the "neither set" case reads whatever is exported and passes for
    the wrong reason -- the failure mode this repo has already hit in
    ``tests/shell/test_env_guard.sh``.
    """
    monkeypatch.delenv(_NEW, raising=False)
    monkeypatch.delenv(_RETIRED, raising=False)


def _production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other settings a production ``Settings()`` needs to reach the token guard.

    Each of these is a SEPARATE validator that also raises in production, so a
    test that set none of them would have its ``pytest.raises`` satisfied by the
    wrong validator entirely -- measured on the first draft of this file, where
    ``CITEVYN_LLM_PROVIDER`` was doing all the work.
    """
    monkeypatch.setenv("CITEVYN_ENVIRONMENT", "production")
    monkeypatch.setenv("CITEVYN_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("CITEVYN_ADMIN_API_KEY", "admin-key-long-enough-for-the-floor")


# Every case below builds ``Settings(_env_file=None)`` so a developer's real
# ``backend/.env`` cannot colour the result: this module is about name
# resolution, not about their configuration.


# ---------------------------------------------------------------------------
# Resolution.
# ---------------------------------------------------------------------------


def test_the_new_name_alone_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches an alias written without the ``CITEVYN_`` prefix.

    ``env_prefix`` is NOT re-applied to an aliased field, so a bare
    ``"PUBLIC_CLIENT_TOKEN"`` alias reads nothing -- and reads nothing SILENTLY,
    because the field has a legal default.
    """
    monkeypatch.setenv(_NEW, _VIA_NEW)
    assert Settings(_env_file=None).public_client_token == _VIA_NEW  # type: ignore[call-arg]


def test_neither_name_set_falls_back_to_the_published_default() -> None:
    """Unchanged behaviour: a plain local checkout still boots with no env at all."""
    assert Settings(_env_file=None).public_client_token == _PUBLISHED_DEFAULT  # type: ignore[call-arg]


def test_the_retired_name_alone_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CITEVYN_DEMO_API_KEY`` reads NOTHING now -- the removal, asserted.

    The value must land on the published default, i.e. exactly where it lands
    with no environment at all. "It is not ``_VIA_RETIRED``" alone would be
    satisfied by a typo in the fixture; asserting the default pins WHERE the value
    goes rather than only where it does not.
    """
    monkeypatch.setenv(_RETIRED, _VIA_RETIRED)
    assert Settings(_env_file=None).public_client_token == _PUBLISHED_DEFAULT  # type: ignore[call-arg]


def test_the_new_name_still_wins_over_a_stale_retired_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A machine that never cleaned up its old variable is unaffected.

    Partner for the case above: with both set the answer must be the new value --
    not the old one, and not the default. Together they say the retired name is
    inert whether or not the new one is present.
    """
    monkeypatch.setenv(_RETIRED, _VIA_RETIRED)
    monkeypatch.setenv(_NEW, _VIA_NEW)
    assert Settings(_env_file=None).public_client_token == _VIA_NEW  # type: ignore[call-arg]


def test_an_empty_value_is_taken_literally_and_rejected_by_min_length() -> None:
    """An EMPTY variable is not the same as an unset one.

    ``min_length=1`` is what turns ``CITEVYN_PUBLIC_CLIENT_TOKEN=`` into a loud
    failure rather than a bearer of ``""``, in EVERY environment -- the production
    validator is not involved. It must NOT silently become ``local-demo-key``
    either: substituting a default for a value an operator explicitly blanked is
    how a misconfiguration gets shipped instead of reported.

    MEASURED, not assumed: pydantic reports the failure under the ALIAS the
    operator actually set, not under the field name. That is the useful direction
    -- the message names the variable they need to go and fix -- so it is pinned
    here rather than left to chance.
    """
    with pytest.raises(ValueError) as excinfo:
        Settings(_env_file=None, **{_NEW: ""})  # type: ignore[call-arg]
    message = str(excinfo.value)
    assert "at least 1 character" in message, message
    assert _NEW in message, (
        f"the validation error does not name {_NEW}, the variable that was set:\n{message}"
    )


def test_an_empty_ENVIRONMENT_variable_is_present_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction every shell consumer had to be corrected for (#430 review).

    An exported-but-EMPTY variable IS present, so ``min_length=1`` rejects it and
    the app refuses to boot. That is NOT the same rule as the shell's
    ``${VAR:-fallback}``, which treats empty as absent.

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
    with pytest.raises(ValueError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    message = str(excinfo.value)
    assert "at least 1 character" in message, message
    assert _NEW in message, message


def test_an_UNSET_new_name_does_NOT_fall_through_to_the_retired_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one case that CHANGED DIRECTION when the alias was deleted.

    It asserted ``== _VIA_RETIRED`` while the alias existed and asserts the
    published default now, so it is the cheapest tripwire for an accidental
    restoration: re-adding the alias turns exactly this red.
    """
    monkeypatch.delenv(_NEW, raising=False)
    monkeypatch.setenv(_RETIRED, _VIA_RETIRED)
    assert Settings(_env_file=None).public_client_token == _PUBLISHED_DEFAULT  # type: ignore[call-arg]


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
# The production validator: what makes a botched deploy fail LOUDLY.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "weak",
    [_PUBLISHED_DEFAULT, "LOCAL-DEMO-KEY", "  local-demo-key ", "short"],
    ids=["default", "upper", "padded", "too-short"],
)
def test_production_refuses_a_weak_token(monkeypatch: pytest.MonkeyPatch, weak: str) -> None:
    """The publicly-known default, its case and whitespace variants, and anything short.

    THE MESSAGE IS ASSERTED, not merely the raise, and `""` is deliberately NOT a
    case here. Both come from the same finding: a bare ``pytest.raises(ValueError)``
    cannot say WHICH validator fired, and review demonstrated it -- with
    ``_reject_weak_public_client_token_in_production`` disabled, every id above went
    red but an ``empty`` case stayed GREEN, because ``min_length=1`` rejects ``""``
    in every environment and the production validator never sees it. It was filed
    under a guard it does not exercise. The empty case has its own home in
    ``test_an_empty_value_is_taken_literally_and_rejected_by_min_length`` and
    ``test_an_empty_ENVIRONMENT_variable_is_present_and_raises``, both of which
    assert the ``at least 1 character`` message that distinguishes the two.
    """
    _production_env(monkeypatch)
    monkeypatch.setenv(_NEW, weak)
    with pytest.raises(ValueError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    message = str(excinfo.value)
    assert "must be set to a strong secret" in message, (
        "something other than _reject_weak_public_client_token_in_production raised, "
        f"so this case is not exercising the guard it is filed under:\n{message}"
    )


def test_an_empty_token_is_rejected_BEFORE_the_production_guard_sees_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the empty case actually belongs, with the message that proves it.

    ``min_length=1`` fires first and in EVERY environment, so an empty value never
    reaches the production validator. Recorded as its own case rather than dropped,
    because "production refuses an empty token" is true and worth pinning -- it is
    just not the production validator that does it, and a test that implied
    otherwise would credit the wrong guard.
    """
    _production_env(monkeypatch)
    monkeypatch.setenv(_NEW, "")
    with pytest.raises(ValueError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    message = str(excinfo.value)
    assert "at least 1 character" in message, message
    assert "must be set to a strong secret" not in message, (
        "the production validator answered for an empty value, so min_length no "
        f"longer fires first and the ordering this case pins has changed:\n{message}"
    )


def test_a_strong_token_boots_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE CONTROL. Without it a validator that rejected EVERYTHING passes above.

    Not hypothetical: the first draft of this file had every "refuses" case dying
    on an unrelated ``CITEVYN_LLM_PROVIDER`` validator, and only this direction
    exposed it.
    """
    _production_env(monkeypatch)
    monkeypatch.setenv(_NEW, _STRONG)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.public_client_token == _STRONG
    assert settings.environment == "production"


def test_a_strong_retired_name_alone_is_refused_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The removal, asserted where it matters most.

    A deploy still carrying ONLY the old secret does not quietly serve: the value
    never reaches the field, the field keeps the published default, and the
    production validator refuses to boot on it. That is a crash-loop an operator
    sees, rather than 401s behind a green ``/health``.

    It pairs with ``test_a_strong_token_boots_in_production`` -- the SAME value,
    accepted under the new name and refused under the retired one. Neither case
    alone distinguishes "the alias is gone" from "the validator rejects
    everything". The message assertion is what proves the refusal is the
    fell-back-to-the-default one and not some unrelated raise.
    """
    _production_env(monkeypatch)
    monkeypatch.setenv(_RETIRED, _STRONG)
    with pytest.raises(ValueError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    message = str(excinfo.value)
    assert "the publicly-known default" in message, (
        "the old name supplied a value after all, or something else raised -- this "
        f"is not the 'fell back to the default' refusal it asserts:\n{message}"
    )


def test_the_production_error_names_the_variable_to_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator reading this message is being told what to fix.

    It must name the variable they set TODAY, and must no longer offer the retired
    spelling as an alternative -- that sentence would send them to set a name
    nothing reads, and this message is the only place a broken deploy is reached.
    """
    _production_env(monkeypatch)
    monkeypatch.setenv(_NEW, _PUBLISHED_DEFAULT)
    with pytest.raises(ValueError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    message = str(excinfo.value)
    assert _NEW in message, f"the error does not name {_NEW}:\n{message}"
    assert _RETIRED not in message, (
        f"the error still offers the retired {_RETIRED}, which reads nothing:\n{message}"
    )
    assert "the publicly-known default" in message, (
        "the error no longer distinguishes the default from a short value, which is "
        "the half that tells an operator whether they set nothing or set it badly"
    )


def test_the_production_error_distinguishes_a_short_value_from_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partner: alone, the assertion above is satisfied by a constant sentence."""
    _production_env(monkeypatch)
    monkeypatch.setenv(_NEW, "short")
    with pytest.raises(ValueError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    message = str(excinfo.value)
    assert "16-character minimum" in message, message
    assert "the publicly-known default" not in message, message
