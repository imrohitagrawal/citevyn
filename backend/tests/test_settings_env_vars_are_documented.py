"""Every ``Settings`` field is declared in an env example, or exempted by name (#332).

WHY THIS FILE EXISTS
--------------------
``test_oauth_env_vars_are_documented.py`` (#289) enforces this rule for the six
OAuth settings only, and said so out loud: 23 of the 84 fields on ``Settings``
were declared in NO env example, so a blanket rule would have failed on arrival.
This is the blanket rule, with those 23 triaged first.

It is a SEPARATE file rather than a widening of the OAuth one, because the two
rules genuinely differ. The OAuth guard demands a declaration in all FOUR
operator-facing files (both env examples, the README table and
``docs/DEPLOY_FLY.md``) -- correct for six credentials an operator must set to
get working buttons, and wrong for a retrieval-tuning float that nobody puts in
a Fly secrets block. This rule asks only that a field be declared in at least
one of the two env examples. Widening ``if "oauth" in f`` in place would have
forced every knob into DEPLOY_FLY.md and made that runbook unreadable, so the
stricter OAuth rule stays where it is and keeps applying on top of this one.

The documentation is the smaller half of the fix. Writing 23 variables down once
does not stop the twenty-fourth arriving undocumented on the next PR -- which is
exactly how #289 and then #332 happened, twice, in the same class. This test is
the part that does not rot.

THE DOUBLE-PREFIX TRAP, AND WHY THE NAME IS NEVER HAND-BUILT
------------------------------------------------------------
``Settings`` sets ``env_prefix="CITEVYN_"``. The field ``citevyn_alias_intent_check``
therefore reads ``CITEVYN_CITEVYN_ALIAS_INTENT_CHECK`` -- and because
``model_config`` uses ``extra="ignore"``, an operator who sets the intuitive
``CITEVYN_ALIAS_INTENT_CHECK`` gets NO error: the variable is silently dropped
and the feature they meant to disable stays on. Documenting that name would be
worse than documenting nothing.

So this module never builds a name by concatenating ``"CITEVYN_"`` with an
uppercased field. It ASKS pydantic-settings which environment variable it will
read, via the same ``EnvSettingsSource`` the application itself uses, and
``test_the_resolver_matches_what_settings_actually_reads`` proves that answer
against a real ``Settings()`` round-trip -- including the negative half, that
the intuitive short name does nothing.

Two distinct hazards live here and it is worth keeping them apart, because
conflating them produces a test that feels safe and is not:

* the HUMAN hazard -- a person writing documentation sees ``citevyn_...``,
  reads the leading ``citevyn_`` AS the prefix, strips it, and writes
  ``CITEVYN_ALIAS_INTENT_CHECK``. That name is silently ignored. Guarded by
  ``test_the_hand_built_name_for_the_double_prefixed_field_does_nothing``.
* the MECHANICAL hazard -- the rule for turning a field into a variable name
  changes (``env_prefix``, an alias, a pydantic-settings upgrade) and hardcoded
  documentation quietly stops describing what the app reads. Guarded by the
  round-trip, which was mutation-checked to be the assertion that catches it.

RED BITE (each assertion, and the one change that turns it red)
---------------------------------------------------------------
* ``test_every_setting_is_declared_in_an_env_example`` -- delete any
  ``CITEVYN_<NAME>=`` line from ``.env.example``/``prod.env.example``, or add a
  new field to ``Settings`` without one.
* ``test_the_resolver_finds_every_settings_field`` -- break the resolver so it
  returns fewer names than ``Settings`` has fields (the non-vacuity partner:
  without it, a resolver returning ``[]`` makes the rule above pass while
  guarding nothing).
* ``test_the_resolver_matches_what_settings_actually_reads`` -- change
  ``env_prefix`` on ``Settings`` while the resolver hand-builds
  ``"CITEVYN_" + field``. Measured, not assumed: under that pair the coverage
  rule above goes GREEN (it is blind to the change and would keep blessing
  documentation for variables the app no longer reads) and this round-trip goes
  RED for all eight boolean fields. That is the whole reason the name is asked
  of pydantic-settings and then pinned to observed behaviour.
  A hand-built name with the prefix UNCHANGED does NOT turn anything red, and
  the honest reason is worth writing down: for ``citevyn_alias_intent_check``
  the two methods happen to agree, because the prefix is ``CITEVYN_`` and the
  field itself starts with ``citevyn_``. The doubled prefix is a trap for a
  human writing documentation, not a divergence between the two resolution
  methods -- so ``test_the_hand_built_name_for_the_double_prefixed_field_does_nothing``
  below guards the human half, and this one guards the mechanical half.
* ``test_a_longer_variable_does_not_satisfy_a_shorter_one`` -- relax
  ``_declared_names`` to a substring search.
* ``test_every_exemption_is_a_real_settings_field`` -- delete or rename an
  exempted field without removing its entry.
* ``test_every_exemption_is_actually_dead_config`` -- put a LIVE setting in
  ``_EXEMPT`` (adversarial review's finding: a reviewer pointed out that
  ``test_every_exemption_carries_a_reason`` only measures string LENGTH, so
  forty characters of plausible prose could silence this guard for a real knob).
  Measured: exempting ``environment`` now fails naming all eleven of its read
  sites. The reason is a checked fact, not a sentence.
* ``test_the_read_site_scanner_finds_real_readers`` -- make ``_app_read_sites``
  return ``[]``. This is the partner for the assertion above, which checks an
  ABSENCE: a blind scanner would declare every field dead and bless any
  exemption at all.
* ``test_no_boolean_field_is_silently_dropped_from_the_round_trip`` -- change
  any bool field to ``bool | None``. ``field.annotation is bool`` is an exact
  type test, so such a field would otherwise vanish from the behavioural
  round-trip while the suite stayed green.
* ``test_an_exempted_field_is_not_also_documented`` -- document an exempted
  field without deleting the (now false) exemption.
* ``test_no_env_example_declares_a_variable_that_does_not_exist`` -- write a
  variable name into an env example or the README table that no ``Settings``
  field reads. This one had a live instance: the README told operators to set
  ``CITEVYN_LLM_API_KEY``, which is not a field and was read by nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic_settings.sources import EnvSettingsSource

from app.core.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The two files an operator copies to stand the service up. A field must be
# declared in AT LEAST ONE of them -- some knobs are dev-only, some prod-only,
# and forcing every field into both would be noise, not documentation.
_ENV_EXAMPLES: tuple[Path, ...] = (
    Path(".env.example"),
    Path("infra/docker/prod.env.example"),
)

# The README env table is checked for WRONG names only (the reverse direction),
# not for coverage: it is a curated highlights table, not the full list, and
# says so.
_README = Path("README.md")

# ---------------------------------------------------------------------------
# Deliberately undocumented, with the reason. This list is the load-bearing
# half of the fix: without it the guard gets switched off the first time it is
# inconvenient. Every entry here is DEAD CONFIG -- verified by grepping the
# whole repo for a read site and finding none -- so documenting it would promise
# an operator a knob that does nothing, which is the same failure as the
# ``CITEVYN_LLM_API_KEY`` row this change deletes from the README.
#
# The honest fix for all six is to delete the fields or wire them up; that is a
# code change to a public config surface, so it is tracked as #378 rather than
# smuggled into a documentation PR. ``test_every_exemption_is_a_real_settings_field``
# below goes red when #378 lands and an entry here is not removed with it, so
# this list cannot outlive the reason for it.
# ---------------------------------------------------------------------------
_EXEMPT: dict[str, str] = {
    "app_name": (
        "Dead config: nothing outside config.py reads it. FastAPI's title is "
        "the separate hardcoded literal at app/main.py:109."
    ),
    "admin_api_key_header": (
        "Dead config: the admin header name is hardcoded at "
        'app/core/security.py:68 as Header(alias="X-Admin-API-Key"). Setting '
        "this renames nothing, so documenting it would advertise a knob that "
        "cannot move."
    ),
    "worker_poll_seconds": (
        "Dead config: app/worker/cli.py is a one-shot run/evaluate/list-sources "
        "CLI, not the polling daemon config.py's comment describes. Nothing "
        "reads it."
    ),
    "worker_max_runtime_seconds": (
        "Dead config: no long-running worker process exists to bound. Nothing reads it."
    ),
    "worker_fetch_timeout_seconds": (
        "Dead config: the worker's LocalFetcher takes no configurable timeout. Nothing reads it."
    ),
    "worker_max_chunks_per_doc": (
        "Dead config: no per-document chunk cap is enforced anywhere in the "
        "ingestion path. Nothing reads it."
    ),
}

# ``CITEVYN_``-prefixed variables that are NOT ``Settings`` fields but are read
# by something real, so the reverse guard must not flag them. Each names its
# consumer, so a dead one can be spotted.
_NON_SETTINGS_VARS: dict[str, str] = {
    "CITEVYN_PUBLIC_HOST": "Caddy (infra/docker/Caddyfile) + the deploy scripts",
    "CITEVYN_ACME_EMAIL": "Caddy's ACME contact (infra/docker/Caddyfile)",
}


_APP_ROOT = _REPO_ROOT / "backend" / "app"
_CONFIG_MODULE = _APP_ROOT / "core" / "config.py"


def _app_read_sites(field_name: str) -> list[str]:
    """Every place under ``backend/app`` that reads ``settings.<field_name>``.

    ``config.py`` itself is excluded: it DECLARES the field, and its validators
    referring to a sibling is not a consumer reading configuration.

    This exists so an exemption is a CHECKED FACT rather than a sentence.
    Every entry in ``_EXEMPT`` claims "nothing reads it"; without this the only
    thing standing between the guard and a developer silencing it is a string
    long enough to pass a length check.

    WHAT IT CANNOT SEE, stated rather than implied. It matches the literal
    ``.<field>`` attribute access, so a read through ``getattr(settings, name)``
    with a computed name is invisible to it. That is not hypothetical here --
    ``app/api/routes/oauth.py:250`` resolves its credential fields exactly that
    way. None of the six currently-exempted fields is read through that idiom
    (checked), so today's exemptions are sound; but a future exemption could be
    wrong in a way this cannot catch, so treat a zero result as strong evidence
    rather than proof, and grep by hand before adding an entry.
    """
    pattern = re.compile(rf"\.{re.escape(field_name)}\b")
    hits: list[str] = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path == _CONFIG_MODULE:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")
    return hits


def _resolved_env_names() -> dict[str, str]:
    """field name -> the environment variable ``Settings`` will actually read.

    Asked of pydantic-settings, never hand-built. ``EnvSettingsSource`` is the
    source the application's own ``Settings()`` uses, so its answer is by
    construction the name the consumer honours -- prefix rules, aliases and the
    ``citevyn_alias_intent_check`` double-prefix case included.
    ``test_the_resolver_matches_what_settings_actually_reads`` pins that claim
    to observed behaviour rather than trusting this comment.
    """
    source = EnvSettingsSource(Settings)
    resolved: dict[str, str] = {}
    for field_name, field in Settings.model_fields.items():
        infos = source._extract_field_info(field, field_name)  # noqa: SLF001
        assert infos, f"pydantic-settings resolved no env name for {field_name!r}"
        resolved[field_name] = infos[0][1].upper()
    return resolved


def _declared_names(path: Path) -> set[str]:
    """Every ``CITEVYN_*`` variable DECLARED in ``path``.

    A declaration is ``NAME=`` at the start of a line, optionally behind a
    comment marker (these templates comment out optional knobs on purpose).
    A mention in prose does not count -- that exact hole made the first version
    of the #289 guard pass while the variable it guarded had been deleted. The
    trailing ``=`` is also what makes the match EXACT: ``CITEVYN_FOO_BAR=``
    yields ``CITEVYN_FOO_BAR``, never ``CITEVYN_FOO``.
    """
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    return set(
        re.findall(r"^[ \t]*#?[ \t]*(?:export[ \t]+)?(CITEVYN_[A-Z0-9_]+)[ \t]*=", text, re.M)
    )


def _readme_table_names() -> set[str]:
    """Every ``CITEVYN_*`` name written as a backticked cell in the README."""
    text = (_REPO_ROOT / _README).read_text(encoding="utf-8")
    return set(re.findall(r"`(CITEVYN_[A-Z0-9_]+)`", text))


# ---------------------------------------------------------------------------
# Non-vacuity partners. Every assertion below counts something; these prove the
# thing being counted exists, so a broken collector cannot report green.
# ---------------------------------------------------------------------------


def test_the_resolver_finds_every_settings_field() -> None:
    """Partner: without this, a resolver returning {} passes every rule below."""
    resolved = _resolved_env_names()
    assert len(resolved) == len(Settings.model_fields)
    # A floor on the absolute number too, so a change that shrinks BOTH sides
    # (say, fields moving into a nested model the resolver stops walking) is
    # still caught. 84 fields at the time of writing.
    assert len(resolved) >= 84, f"expected >=84 Settings fields, found {len(resolved)}"
    assert resolved["environment"] == "CITEVYN_ENVIRONMENT"


def test_the_env_examples_declare_something_at_all() -> None:
    """Partner: an unreadable or renamed template must go red, not vacuously pass."""
    for doc in _ENV_EXAMPLES:
        path = _REPO_ROOT / doc
        assert path.exists(), f"{doc} is missing"
        assert len(_declared_names(path)) >= 20, (
            f"{doc} declares almost nothing ({len(_declared_names(path))} names) -- "
            f"the declaration regex has probably stopped matching, which would make "
            f"the coverage rule below pass by collecting zero."
        )


def test_the_readme_table_declares_something_at_all() -> None:
    """Partner for the reverse guard's README half."""
    assert len(_readme_table_names()) >= 20


# ---------------------------------------------------------------------------
# The resolver is right (the double-prefix trap), proved against real Settings.
# ---------------------------------------------------------------------------

# Every boolean field, driven end to end. Booleans are the subset that can be
# flipped generically without inventing a value that satisfies each field's
# constraints -- and it happens to contain the trap case.
_BOOL_FIELDS: tuple[str, ...] = tuple(
    name
    for name, field in Settings.model_fields.items()
    if field.annotation is bool  # noqa: E721 - exact type, not a subclass
)


def test_no_boolean_field_is_silently_dropped_from_the_round_trip() -> None:
    """``field.annotation is bool`` is an EXACT type test, and that is a trap.

    A future ``bool | None`` or ``Annotated[bool, ...]`` field would not match,
    so it would vanish from the one test that proves name resolution against a
    real ``Settings()`` -- silently, with the suite still green. Rather than
    guess at every annotation shape, this asserts that no field mentions ``bool``
    without being in the round-tripped set, so such a field arrives with a red
    test explaining itself instead of slipping past.
    """
    bool_ish = {
        name for name, field in Settings.model_fields.items() if "bool" in str(field.annotation)
    }
    missed = sorted(bool_ish - set(_BOOL_FIELDS))
    assert not missed, (
        f"boolean-ish field(s) {missed} are not covered by the round-trip, because "
        f"`field.annotation is bool` is an exact type match. Widen _BOOL_FIELDS to "
        f"include them, or the resolver is never behaviourally proved for them."
    )


def test_there_are_bool_fields_to_round_trip() -> None:
    """Partner: the parametrised round-trip below is empty without this."""
    assert len(_BOOL_FIELDS) >= 5, f"expected several bool settings, found {_BOOL_FIELDS}"
    assert "citevyn_alias_intent_check" in _BOOL_FIELDS, (
        "the double-prefix trap case must be inside the round-tripped set, "
        "otherwise the resolver is only proved on names where hand-building "
        "would have worked anyway"
    )


@pytest.mark.parametrize("field_name", _BOOL_FIELDS)
def test_the_resolver_matches_what_settings_actually_reads(
    field_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting the RESOLVED name must flip the field on a real ``Settings()``.

    This is the assertion that makes the resolver trustworthy rather than
    merely plausible: it observes what the consumer receives.
    """
    env_name = _resolved_env_names()[field_name]
    default = Settings.model_fields[field_name].default
    assert isinstance(default, bool)
    monkeypatch.setenv(env_name, "false" if default else "true")
    # ``_env_file=None`` so a developer's real backend/.env cannot colour the
    # result; this test is about name resolution, not about their config.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert getattr(settings, field_name) is (not default), (
        f"{env_name} did not reach Settings.{field_name}"
    )


def test_the_hand_built_name_for_the_double_prefixed_field_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative half: ``"CITEVYN_" + field.upper()`` is the WRONG rule.

    ``citevyn_alias_intent_check`` + prefix ``CITEVYN_`` is
    ``CITEVYN_CITEVYN_ALIAS_INTENT_CHECK``. The intuitive
    ``CITEVYN_ALIAS_INTENT_CHECK`` is silently ignored (``extra="ignore"``), so
    an operator who set it would believe the check was off while it stayed on.
    """
    assert _resolved_env_names()["citevyn_alias_intent_check"] == (
        "CITEVYN_CITEVYN_ALIAS_INTENT_CHECK"
    )
    monkeypatch.setenv("CITEVYN_ALIAS_INTENT_CHECK", "false")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.citevyn_alias_intent_check is True, (
        "the short name unexpectedly took effect -- if pydantic-settings ever "
        "starts honouring it, the docs written against the doubled name need "
        "revisiting"
    )


def test_exactly_one_settings_field_carries_the_doubled_prefix() -> None:
    """Any FUTURE field named ``citevyn_*`` walks into the same trap.

    Recorded as an assertion rather than a comment so adding one is a
    deliberate act with a red test in front of it, not a silent repeat.
    """
    doubled = sorted(n for n in Settings.model_fields if n.startswith("citevyn_"))
    assert doubled == ["citevyn_alias_intent_check"], (
        f"new doubled-prefix field(s): {doubled}. Their env var is "
        f"CITEVYN_CITEVYN_<REST>, not CITEVYN_<REST>; document the doubled "
        f"spelling or the operator sets a variable that does nothing."
    )


# ---------------------------------------------------------------------------
# The exact-match rule, proved rather than asserted in prose.
# ---------------------------------------------------------------------------


def test_a_longer_variable_does_not_satisfy_a_shorter_one(tmp_path: Path) -> None:
    """``CITEVYN_FOO_BAR=`` must NOT count as documenting ``CITEVYN_FOO``.

    A substring search would count it, and would have reported this whole class
    of gap as smaller than it is -- ``CITEVYN_DEMO_API_KEY`` would be "found"
    by ``CITEVYN_DEMO_API_KEY_HEADER``, and so on.
    """
    sample = tmp_path / "sample.env.example"
    sample.write_text("CITEVYN_FOO_BAR=1\n# CITEVYN_BAZ=2\nCITEVYN_QUX mentioned in prose\n")
    declared = _declared_names(sample)
    assert declared == {"CITEVYN_FOO_BAR", "CITEVYN_BAZ"}
    assert "CITEVYN_FOO" not in declared
    assert "CITEVYN_QUX" not in declared


# ---------------------------------------------------------------------------
# The rule.
# ---------------------------------------------------------------------------


def test_every_setting_is_declared_in_an_env_example() -> None:
    resolved = _resolved_env_names()
    documented: set[str] = set()
    for doc in _ENV_EXAMPLES:
        documented |= _declared_names(_REPO_ROOT / doc)
    missing = sorted(
        f"{env} (Settings.{field})"
        for field, env in resolved.items()
        if field not in _EXEMPT and env not in documented
    )
    assert not missing, (
        f"{len(missing)} Settings field(s) are declared in no env example:\n"
        + "\n".join(f"  - {m}" for m in missing)
        + f"\n\nAn operator greps {[str(d) for d in _ENV_EXAMPLES]} for the knob "
        f"they need; a field that exists on Settings and appears in neither is "
        f"undiscoverable. Either add a `NAME=` line to the template that fits "
        f"it, or add the field to _EXEMPT in this file WITH A REASON."
    )


def test_every_exemption_is_a_real_settings_field() -> None:
    """Partner: an exemption for a field that no longer exists is stale cover.

    Without this, deleting a field would leave its exemption behind, quietly
    widening the hole for a future field that happens to reuse the name.
    """
    stale = sorted(f for f in _EXEMPT if f not in Settings.model_fields)
    assert not stale, (
        f"_EXEMPT names field(s) that are not on Settings: {stale}. "
        f"Delete the entries -- a stale exemption excuses a field nobody meant to excuse."
    )


def test_every_exemption_carries_a_reason() -> None:
    """Weak on its own -- a length check cannot read prose.

    Kept only to catch an empty or one-word entry. The assertion that actually
    holds the exemption list honest is the next one, which checks the CLAIM
    rather than the sentence.
    """
    for field, reason in _EXEMPT.items():
        assert len(reason) >= 40, f"{field}: an exemption needs a real reason, got {reason!r}"


@pytest.mark.parametrize("field_name", sorted(_EXEMPT))
def test_every_exemption_is_actually_dead_config(field_name: str) -> None:
    """An exempted field must genuinely have NO reader under ``backend/app``.

    Every entry in ``_EXEMPT`` justifies itself with "nothing reads it". This
    turns that sentence into a checked fact, which is the difference between an
    exemption list and an off switch: a developer cannot quiet this guard for a
    LIVE setting by writing forty characters of plausible prose, because a live
    setting has read sites and this goes red naming them.

    Turns red when: any of the six exempted fields gains a reader (which is
    exactly when it stops being dead config and starts deserving an env-example
    entry), or when #378 wires one of them up without removing its exemption.
    """
    hits = _app_read_sites(field_name)
    assert not hits, (
        f"Settings.{field_name} is exempted from documentation on the grounds that "
        f"nothing reads it, but it is read at {hits}. Either the exemption is wrong "
        f"and the field needs an env-example entry, or the read site is new -- "
        f"in both cases document it and delete the _EXEMPT entry."
    )


def test_the_read_site_scanner_finds_real_readers() -> None:
    """Partner: without this, a scanner returning [] declares everything dead.

    ``test_every_exemption_is_actually_dead_config`` asserts an ABSENCE, so it
    passes trivially if ``_app_read_sites`` is broken -- and it would then bless
    an exemption for a heavily-used setting. These two fields are read all over
    the app; if the scanner cannot see them it cannot see anything.
    """
    env_hits = _app_read_sites("environment")
    assert len(env_hits) >= 8, f"scanner found only {env_hits} readers of Settings.environment"
    assert any("main.py" in h for h in env_hits)
    assert any("auth_sessions.py" in h for h in env_hits)
    # A second, differently-shaped field, so the partner does not rest on one name.
    assert len(_app_read_sites("cost_budget_enabled")) >= 1
    # And the scanner must EXCLUDE config.py, or every field looks "read".
    assert not any("core/config.py" in h for h in env_hits)


def test_an_exempted_field_is_not_also_documented() -> None:
    """If it got documented, the exemption is now false and must go.

    Keeps the list honest: an exemption that no longer describes reality is
    exactly the "switched off because it was inconvenient" failure this file
    exists to prevent.
    """
    resolved = _resolved_env_names()
    documented: set[str] = set()
    for doc in _ENV_EXAMPLES:
        documented |= _declared_names(_REPO_ROOT / doc)
    contradicted = sorted(f for f in _EXEMPT if resolved.get(f) in documented)
    assert not contradicted, (
        f"{contradicted} are documented AND exempted. Remove them from _EXEMPT."
    )


# ---------------------------------------------------------------------------
# The reverse direction: a documented name that reads nothing is worse than an
# absent one, because the operator believes they configured something.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [*(str(d) for d in _ENV_EXAMPLES), str(_README)],
)
def test_no_env_example_declares_a_variable_that_does_not_exist(source: str) -> None:
    real = set(_resolved_env_names().values())
    if source == str(_README):
        written = _readme_table_names()
    else:
        written = _declared_names(_REPO_ROOT / source)
    unknown = sorted(n for n in written if n not in real and n not in _NON_SETTINGS_VARS)
    assert not unknown, (
        f"{source} names {unknown}, which no Settings field reads. A wrong name is "
        f'worse than a missing one: `extra="ignore"` means the operator gets no '
        f"error, sets it, and believes it took effect. (README.md carried exactly "
        f"this bug -- `CITEVYN_LLM_API_KEY`, read by nothing, where "
        f"`CITEVYN_ANTHROPIC_API_KEY` was meant.) If it is genuinely a non-Settings "
        f"variable, add it to _NON_SETTINGS_VARS with its consumer."
    )


def test_the_non_settings_allowlist_is_not_stale() -> None:
    """Partner: an allowlist entry nobody writes any more is dead cover."""
    written: set[str] = set(_readme_table_names())
    for doc in _ENV_EXAMPLES:
        written |= _declared_names(_REPO_ROOT / doc)
    unused = sorted(n for n in _NON_SETTINGS_VARS if n not in written)
    assert not unused, (
        f"_NON_SETTINGS_VARS excuses {unused}, which appear in none of the guarded "
        f"files any more. Delete the entries."
    )
