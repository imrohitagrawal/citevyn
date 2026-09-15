"""Guards ``.github/dependabot.yml`` against the SILENT-DISABLE failure (#423).

A malformed or incomplete ``dependabot.yml`` does not fail loudly. GitHub
ignores the file, or the offending entry, and the only symptom is an absence of
pull requests — which is indistinguishable from "there was nothing to update".
Nothing in CI executes this file, so nothing else in the repo can observe it.

That is not hypothetical here. ``npm`` was never declared, so ``frontend/``
received security PRs (which bypass this file entirely) and no scheduled version
updates at all, for the life of the repo. ``@types/node`` sat at ``^20.16.0``
while ``frontend/.nvmrc`` had moved to ``26`` until #413 found it by accident.
(#423 called that "two majors" and this docstring repeated it. It is six major
numbers, three even-numbered Node LTS generations; ``git log -p --
frontend/package.json`` shows the single edit ``^20.16.0`` -> ``^26.5.1``.)

So these tests assert the PARSED structure, never the text. A test that greps for
the string ``npm`` passes on a file where the entry has been commented out, moved
under the wrong key, or had its indentation broken — every shape that actually
causes the outage. ``yaml.safe_load`` plus a lookup by
``(package-ecosystem, directory)`` is what separates a declared entry from a
mention of one.

That is necessary and it is not sufficient, which the first version of this file
got wrong: its docstring claimed the lookup distinguished "declared and
effective" from "the word appears somewhere", and adversarial review then
measured five configs that are declared, INEFFECTIVE, and left all 32 assertions
green — ``ignore: [{dependency-name: "*"}]``, ``cooldown``, a group
``exclude-patterns``, a group ``dependency-type``, and
``open-pull-requests-limit: 0`` (GitHub's documented off-switch). Each of those
reproduces #423 exactly, from a file that parses and declares everything.
They are asserted now, as absences; see ``_SILENT_DISABLE_KEYS``.

The pattern-to-package tests exist for the second silent failure in the same
file: a ``groups.patterns`` entry naming a package that is not in
``frontend/package.json`` matches nothing and groups nothing, with no error
anywhere.

What this file still cannot see is enumerated in ``_UNGUARDABLE`` and pinned by
``test_the_guards_own_blind_spots_are_written_down`` — the completeness claim
above is exactly the kind this guard has already been wrong about once.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT_YML = REPO_ROOT / ".github" / "dependabot.yml"
FRONTEND_PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"

# The manifest sections DEPENDABOT reads, which is the only population that
# matters here -- not the sections npm installs, and not the sections a human
# would call "direct". Verbatim from dependabot-core,
# npm_and_yarn/lib/dependabot/npm_and_yarn/file_parser.rb:
#
#   DEPENDENCY_TYPES = %w(dependencies devDependencies optionalDependencies).freeze
#
# This line has now been wrong twice in opposite directions, which is why it
# carries its source. It first listed two sections and called them "both" that
# npm installs from -- false, `optionalDependencies` are installed too. The
# correction then over-shot and added `peerDependencies`, on npm's semantics
# rather than Dependabot's: Dependabot does NOT parse that section, so demanding
# a group for a package declared there would plant a pattern matching nothing --
# exactly the silent no-op dependabot.yml's own comment warns about. Three
# sections, from the parser.
#
# Deliberately NOT shared with test_node_version_pin.py's _TYPES_NODE_SECTIONS,
# which is two of these strings for a DIFFERENT reason: that one is the set npm
# resolves a version from, so its guard fails when @types/node appears in more
# than one. Unifying them would couple two invariants that should be free to
# move apart.
#
# A transitive package (browserslist, fast-uri, @vitest/mocker -- all three have
# opened security PRs on this repo) appears in none of these and is reached only
# by a security update, which ignores dependabot.yml.
_DIRECT_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")

# Every ecosystem this file is expected to declare, as (ecosystem, directory).
# Recorded so that DELETING one is a test failure rather than a quiet loss of
# coverage -- the exact class of bug #423 was. Adding an ecosystem is a
# deliberate act and updating this tuple is part of it.
_EXPECTED_ECOSYSTEMS: tuple[tuple[str, str], ...] = (
    ("uv", "/backend"),
    ("npm", "/frontend"),
    ("docker", "/infra/docker"),
    ("docker-compose", "/infra/docker"),
    ("github-actions", "/"),
)

_NPM = ("npm", "/frontend")

# Every label the repository actually has, verbatim from
# ``gh label list --limit 100`` against imrohitagrawal/citevyn on 2026-09-15
# (#436). Dependabot cannot CREATE a label: a requested name the repo does not
# have is silently dropped, so a config asking for one is claiming something
# that never happens. That is measurable at the CONSUMER and was measured --
# PRs #433 and #434 (uv, requesting `python`), #227 (docker, requesting
# `docker`) and #243 (github-actions, requesting `ci`) each landed carrying
# `dependencies` alone, while #307 (npm, requesting `javascript`) carried both.
#
# This is a SNAPSHOT, and the direction it fails in matters. No offline test
# can run `gh label list`, so it catches a config drifting to an invented name
# and does NOT catch a label someone later DELETES from the repo. Recorded in
# _UNGUARDABLE. Widening this set is how a phantom label gets waved through, so
# add to it only from a fresh `gh label list`, never to make a test pass.
_LABELS_VERIFIED_PRESENT: frozenset[str] = frozenset(
    {
        "bug",
        "documentation",
        "duplicate",
        "enhancement",
        "good first issue",
        "help wanted",
        "invalid",
        "question",
        "wontfix",
        "dependencies",
        "github_actions",
        "javascript",
        "on-hold",
        "full-eval",
        "discussion",
    }
)

# Truthiness alone is satisfied by a set holding one name, and every "is this
# label real?" assertion below passes vacuously against an EMPTY set only if
# the file names no labels at all -- which the per-entry check rules out.
# Pin the hand-count so shrinking the snapshot to fit a config is deliberate.
_LABELS_VERIFIED_PRESENT_COUNT = 15

# Every option an ``updates:`` entry may carry, from GitHub's options reference
# (docs.github.com/en/code-security/dependabot/working-with-dependabot/
# dependabot-options-reference). Read TWICE, and the two reads of the same page
# did not return an identical index -- so this is their UNION, which is the safe
# direction: the allow-list only has to be wide enough not to fail a legitimate
# edit, while the BANNED subset below does the load-bearing work.
#
# `reviewers` is deliberately ABSENT. GitHub removed it from dependabot.yml in
# 2025, replaced by CODEOWNERS, and it appears nowhere on the reference page. An
# earlier version of this set listed it, which would have waved through a key
# GitHub no longer accepts -- the exact "invalid file gets ignored" route this
# check exists to close. Entry-level `patterns` is absent for the same reason:
# it is a `groups.<name>` option and appears at entry level only inside one
# multi-ecosystem example.
#
# An UNKNOWN key -- a typo like `registires:` for `registries:` -- is the
# cheapest way to invalidate this file, and GitHub's response to an invalid file
# is to ignore it, which is #423 again.
_KNOWN_ENTRY_KEYS: frozenset[str] = frozenset(
    {
        "allow",
        "assignees",
        "commit-message",
        "cooldown",
        "directories",
        "directory",
        "enable-beta-ecosystems",
        "exclude-paths",
        "groups",
        "ignore",
        "insecure-external-code-execution",
        "labels",
        "milestone",
        "multi-ecosystem-group",
        "open-pull-requests-limit",
        "package-ecosystem",
        "pull-request-branch-name",
        "rebase-strategy",
        "registries",
        "schedule",
        "target-branch",
        "vendor",
        "versioning-strategy",
    }
)

# Every key a ``groups.<name>`` block may carry, from the same reference.
_KNOWN_GROUP_KEYS: frozenset[str] = frozenset(
    {
        "applies-to",
        "dependency-type",
        "exclude-patterns",
        "group-by",
        "patterns",
        "update-types",
    }
)

# The SUBSET of the documented options this repo refuses to accept silently.
# Each is valid, parses cleanly, leaves every structural assertion in this file
# green -- and switches scheduled updates off, wholly or in part. That is the
# #423 outcome reached from the other direction: the entry is declared and
# INEFFECTIVE, which reads as "nothing to update" exactly like the absent entry
# did. Adversarial review measured every one of these surviving the first
# version of this file, which asserted structure but never absence:
#
#   ignore: [{dependency-name: "*"}]  -- no version update ever opens again.
#   allow: [{dependency-type: "production"}]
#                                     -- drops every devDependency, which here
#                                        is the whole toolchain plus @types/node.
#   cooldown: {default-days: 365}     -- every PR deferred a year.
#   versioning-strategy: ...          -- changes what reaches the manifest.
#   open-pull-requests-limit: 0       -- GitHub documents zero as the way to
#                                        "temporarily disable version updates
#                                        for a package manager". Checked
#                                        SEPARATELY below, because that key must
#                                        be PRESENT with a sane value, not absent.
#
# These are a SUBSET of _KNOWN_ENTRY_KEYS on purpose, and that is the fix for a
# defect review found in the first attempt: when the banned keys were held OUT
# of the known set, following the documented escape hatch (delete the key from
# this tuple) left the suite red on the UNKNOWN-key check, with a message
# accusing the maintainer of a typo. A hatch that does not work is a guard
# people delete.
#
# The hatch is now two adjacent edits in this file, both deliberate: drop the
# key here, and drop it from the pinned set in
# test_both_ban_lists_are_real_subsets_of_the_documented_options. Measured: the
# second is the only failure that remains, and its message names the key and
# says it was measured switching updates off -- which is the conversation the
# pin exists to force. Deliberately NOT reduced to one edit: a ban that can
# vanish from a single line is a ban that vanishes.
#
# `dependency-type` is NOT in this entry-level tuple. The first version put it
# here on the belief that it was an entry-level option. It is not -- it exists
# only under `allow[]` and under `groups.<name>`. At entry level the unknown-key
# check already rejects it; at group level it is banned just below.
_SILENT_DISABLE_KEYS: tuple[str, ...] = (
    "allow",
    "cooldown",
    "ignore",
    "versioning-strategy",
)

# The group-level members of the same family, both measured surviving:
# `exclude-patterns: ["*"]` leaves `patterns` intact, so every pattern-to-package
# assertion below still passes while Dependabot groups nothing; a group
# `dependency-type: development` makes `frontend-runtime` (react and react-dom,
# the only two production dependencies) match nothing.
_SILENT_DISABLE_GROUP_KEYS: tuple[str, ...] = ("dependency-type", "exclude-patterns")

# What this file CANNOT see. Recorded house-style (test_node_version_pin.py's
# `_UNGUARDABLE`) because the first version of this guard asserted completeness
# and was wrong about it: adversarial review found five silent-disable shapes it
# missed. A guard that does not write its holes down reads as having none.
_UNGUARDABLE: tuple[str, ...] = (
    "Whether GitHub ACCEPTS this file -- from in here. Nothing in CI runs "
    "Dependabot and no test can ask. But it is NOT unobservable, which this "
    "entry claimed until it was read against reality: a PR that touches "
    "dependabot.yml gets a check run named `.github/dependabot.yml` from the "
    "`dependabot` app, titled 'Dependabot config file validation'. It reported "
    "`success` / 'All changes look good' on this file at 30cf1ad. It is not a "
    "REQUIRED context, so it gates nothing -- read it on the PR. Settling "
    "command: `gh api repos/<owner>/<repo>/commits/<sha>/check-runs`. For a "
    "question about one option, the reference at docs.github.com/en/"
    "code-security/dependabot/working-with-dependabot/"
    "dependabot-options-reference.",
    "Whether a scheduled run actually OPENS a PR. Dependabot may find nothing to "
    "update, hit its own errors, or be disabled at the repo or org level -- all "
    "of which look identical from in here. The observable proof is a PR whose "
    "title carries this file's `deps`/`deps(dev)` prefix rather than "
    "Dependabot's default `chore(...)`/`build(...)`.",
    "The four PRE-EXISTING entries' schedule VALUES, target branches and (except "
    "docker's, pinned below) their groups. Every entry is now held to having a "
    "`schedule.interval` at all, a PR limit of at least 1, no banned key and no "
    "unrecognised key -- this entry said otherwise until review caught it, which "
    "is a register making the false-completeness claim it exists to prevent. "
    "What is unasserted is which weekday, which branch, and what the uv/"
    "docker-compose/github-actions groups contain. Deliberate scope: #423 is "
    "about npm. The sharpest gap was docker's `docker-base-images` group, which "
    "is why that ONE is pinned -- test_node_version_pin.py's prose depends on it "
    "grouping every base image.",
    "Transitive packages. `frontend/package-lock.json` is not read here, so a "
    "group pattern naming a transitive dependency would be reported as matching "
    "nothing even though Dependabot could reach it through a security update. "
    "Every pattern in the file today names a DIRECT dependency, so the question "
    "has not arisen.",
    "Whether `labels` name labels the repo really has. `gh label list` settles "
    "it and no test can run it offline. Every label in the file is now checked "
    "against _LABELS_VERIFIED_PRESENT, a snapshot of that command taken while "
    "#436 shipped -- which catches drift to an invented name (the #436 defect: "
    "`python`, `docker` and `ci` were all requested and none existed) but NOT "
    "a label someone later DELETES from the repo, which would leave this file "
    "green while Dependabot silently dropped it again.",
)

# Truthiness alone lets a single entry vanish in silence, which is how a
# recorded limitation quietly stops being recorded.
_UNGUARDABLE_COUNT = 5


def _config() -> dict[str, Any]:
    """The parsed file, or a failure that names the parse error.

    A syntax error here raises out of ``yaml.safe_load`` and reddens every test
    in this module -- which is the point: GitHub's response to an unparseable
    file is to ignore it, so a broken file must not be able to look healthy.
    """
    data = yaml.safe_load(DEPENDABOT_YML.read_text(encoding="utf-8"))
    assert isinstance(data, dict), (
        f"{DEPENDABOT_YML} does not parse to a YAML mapping (got "
        f"{type(data).__name__}). GitHub ignores a file it cannot read, so this "
        "shape ships as 'Dependabot is configured' while no version update ever "
        "runs."
    )
    return data


def _updates() -> list[dict[str, Any]]:
    config = _config()
    updates = config.get("updates")
    assert isinstance(updates, list) and updates, (
        "dependabot.yml has no non-empty `updates:` list. Without it the file "
        "parses and configures nothing."
    )
    for entry in updates:
        assert isinstance(entry, dict), f"`updates` contains a non-mapping entry: {entry!r}"
    return updates


def _entry(ecosystem: str, directory: str) -> dict[str, Any]:
    matches = [
        entry
        for entry in _updates()
        if entry.get("package-ecosystem") == ecosystem and entry.get("directory") == directory
    ]
    assert matches, (
        f"dependabot.yml declares no entry with package-ecosystem "
        f"{ecosystem!r} and directory {directory!r}. Version updates run ONLY "
        "for declared ecosystems; security updates ignore this file and open "
        "PRs anyway, so the symptom of this being missing is 'no PRs', which "
        "looks exactly like 'nothing to update' (#423)."
    )
    assert len(matches) == 1, (
        f"dependabot.yml declares {ecosystem!r} on {directory!r} more than once. "
        "Dependabot does not merge duplicate entries, and which one wins is not "
        "something this repo should be relying on."
    )
    return matches[0]


def _direct_dependencies() -> dict[str, str]:
    """Every package frontend/package.json declares directly, name -> range."""
    manifest = json.loads(FRONTEND_PACKAGE_JSON.read_text(encoding="utf-8"))
    declared: dict[str, str] = {}
    for section in _DIRECT_SECTIONS:
        declared.update(manifest.get(section) or {})
    assert declared, (
        f"{FRONTEND_PACKAGE_JSON} declares no direct dependencies in "
        f"{_DIRECT_SECTIONS}. Every pattern assertion below would then be "
        "vacuously true, so this is checked first."
    )
    return declared


def _matches(pattern: str, package: str) -> bool:
    """Dependabot's group matcher, ported line for line.

    Dependabot does NOT use ``File.fnmatch`` for group patterns. It uses its own
    ``WildcardMatcher``, dependabot-core ``common/lib/wildcard_matcher.rb``,
    called from ``common/lib/dependabot/dependency_group.rb``
    (``patterns.any? { |rule| WildcardMatcher.match?(rule, dependency_name) }``)::

        regex_string = "a#{wildcard_string.downcase}a".split("*")
                                                      .map { |p| Regexp.quote(p) }
                                                      .join(".*").gsub(/^a|a$/, "")
        regex = /^#{regex_string}$/
        regex.match?(candidate_string.downcase)

    Both sides are downcased, and ``*`` becomes ``.*`` so it crosses ``/``. The
    padding with ``a`` exists so a leading or trailing ``*`` does not produce an
    empty split part, and is then stripped back off. ``Regexp.quote`` makes
    every other character LITERAL -- so ``?`` and ``[...]``, which fnmatch
    treats as metacharacters, match themselves here.

    This function has now been wrong twice, in the same place, and the second
    time is why it is a port rather than a description. First it was a
    hand-rolled exact-or-trailing-``*`` matcher, case-SENSITIVE, argued as
    "narrower, so it fails safe": measured NOT safe for the overlap census,
    where narrower means MISSING a real overlap, so a package matched by two
    groups is reported as matched by one and Dependabot assigns it by YAML
    order. Then it was ``fnmatch`` on lower-cased strings, with a comment citing
    ``File.fnmatch`` with ``FNM_CASEFOLD`` -- right about the case folding, and
    a wrong citation: measured to disagree on ``("react?", "react-")``,
    ``("[r]eact", "react")`` and ``("[r]eact", "[r]eact")``.

    Both errors were latent -- zero disagreement across every pattern in
    dependabot.yml against every package in frontend/package.json, on both
    versions. They are fixed anyway, because "no impact today" is a statement
    about today's manifest, and this file exists precisely because nobody was
    watching that manifest.
    """
    padded = f"a{pattern.lower()}a"
    body = ".*".join(re.escape(part) for part in padded.split("*"))
    body = re.sub(r"^a|a$", "", body)
    return re.fullmatch(body, package.lower()) is not None


def _npm_groups() -> dict[str, dict[str, Any]]:
    groups = _entry(*_NPM).get("groups")
    assert isinstance(groups, dict) and groups, (
        "The npm entry declares no `groups:`. Ungrouped, 18 direct frontend "
        "dependencies open up to 18 separate PRs against a branch where every "
        "merge staled every other PR."
    )
    return groups


def _npm_patterns() -> list[tuple[str, str]]:
    """(group name, pattern) for every pattern under the npm entry."""
    pairs = [
        (name, pattern)
        for name, group in _npm_groups().items()
        for pattern in (group.get("patterns") or [])
    ]
    assert pairs, "No npm group declares any `patterns:`; every group matches nothing."
    return pairs


# The parametrised tests below read the file at COLLECTION time. If the npm
# entry is missing, or the YAML will not parse, that read raises and pytest
# reports a collection error for the whole module -- red, but red without
# naming which guard caught it, and with every other test in the file
# unreported. Worse, the obvious alternative (an empty parameter list) makes
# pytest SKIP the test silently, which is the vacuous-guard shape this repo
# has been bitten by.
#
# So collection degrades to a single sentinel parameter instead. The test body
# re-reads the file and fails on the real assertion, with the real message.
_MISSING = "<npm entry missing, or dependabot.yml does not parse>"


def _collect_npm_group_names() -> list[str]:
    try:
        return sorted(_npm_groups())
    except Exception:
        return [_MISSING]


def _collect_npm_patterns() -> list[tuple[str, str]]:
    try:
        return _npm_patterns()
    except Exception:
        return [(_MISSING, _MISSING)]


# ──────────────────────── the file is real and parseable ────────────────────────


def test_dependabot_config_exists_and_declares_schema_version_2() -> None:
    """Which change turns this RED: deleting .github/dependabot.yml, or
    changing `version:` away from 2 (the only schema Dependabot accepts)."""
    assert DEPENDABOT_YML.is_file(), f"{DEPENDABOT_YML} is missing"
    assert _config().get("version") == 2, (
        "dependabot.yml must declare `version: 2`. Dependabot rejects any other "
        "value and runs nothing, with no failing check anywhere in this repo."
    )


@pytest.mark.parametrize(("ecosystem", "directory"), _EXPECTED_ECOSYSTEMS)
def test_every_recorded_ecosystem_is_still_declared(ecosystem: str, directory: str) -> None:
    """Which change turns this RED: deleting any one of the five `updates:`
    entries, retargeting its `directory:`, or dropping its `schedule:`.

    The body asserts the schedule, not `entry["package-ecosystem"] == ecosystem`
    — which was this test's first shape and is a tautology, since `_entry` has
    already filtered on exactly that. It still went red on a deleted entry (the
    helper raises), but for the helper's reason rather than its own. An entry
    with no `schedule:` runs on Dependabot's default cadence rather than the one
    this file argues for, so that is the cheapest real thing to hold.
    """
    schedule = _entry(ecosystem, directory).get("schedule")
    assert isinstance(schedule, dict) and schedule.get("interval"), (
        f"the {ecosystem} entry on {directory} declares no `schedule.interval`, "
        "so its cadence is Dependabot's default rather than this file's choice."
    )


def test_no_ecosystem_is_declared_that_is_not_recorded() -> None:
    """The partner to the test above: it proves the recorded list is the WHOLE
    population, so the parametrised test is not passing while three unlisted
    entries rot beside it.

    Which change turns this RED: adding a sixth `updates:` entry without adding
    it to _EXPECTED_ECOSYSTEMS.
    """
    declared = {(entry.get("package-ecosystem"), entry.get("directory")) for entry in _updates()}
    assert declared == set(_EXPECTED_ECOSYSTEMS), (
        f"dependabot.yml declares {sorted(declared)} but this test records "
        f"{sorted(_EXPECTED_ECOSYSTEMS)}. Reconcile the two: an ecosystem nobody "
        "wrote down is an ecosystem nobody reviews."
    )


# ─────────────────── declared, but switched off without saying so ──────────────


@pytest.mark.parametrize(("ecosystem", "directory"), _EXPECTED_ECOSYSTEMS)
def test_no_entry_carries_a_key_that_silently_disables_it(ecosystem: str, directory: str) -> None:
    """The other route to #423: an entry that is declared and INEFFECTIVE.

    Every key in _SILENT_DISABLE_KEYS parses, is valid, and leaves every other
    assertion in this file green while cancelling some or all of the updates the
    entry exists to produce. `ignore: [{dependency-name: "*"}]` is the complete
    version — measured green against the first draft of this module, which
    asserted structure and never absence, and which claimed in its own docstring
    to distinguish "declared and effective" from "the word appears somewhere".
    It did not. This is the assertion that makes that sentence true.

    Which change turns this RED: adding `allow`, `cooldown`, `ignore` or
    `versioning-strategy` to any entry. If one is genuinely wanted, take it out
    of _SILENT_DISABLE_KEYS and out of the pinned set in
    test_both_ban_lists_are_real_subsets_of_the_documented_options, in the same
    commit, and say why. Two adjacent lines, both of them a deliberate act. It
    used to be worse: the banned keys were held OUT of _KNOWN_ENTRY_KEYS, so
    using the hatch failed the UNKNOWN-key check and told the maintainer they
    had made a typo.
    """
    entry = _entry(ecosystem, directory)
    present = sorted(key for key in _SILENT_DISABLE_KEYS if key in entry)
    assert not present, (
        f"the {ecosystem} entry on {directory} carries {present}, which can "
        "switch its version updates off without failing anything. That is #423's "
        "outcome reached from the other side: the entry is declared, so it looks "
        "configured, and no PR ever opens. Remove it, or -- if it is deliberate "
        "-- drop the key from _SILENT_DISABLE_KEYS in the same commit with the "
        "reason."
    )


@pytest.mark.parametrize(("ecosystem", "directory"), _EXPECTED_ECOSYSTEMS)
def test_no_entry_carries_an_unrecognised_key(ecosystem: str, directory: str) -> None:
    """A typo'd key (`registires:` for `registries:`) invalidates the file, and
    GitHub's response to an invalid file is to ignore it — #423 again, from a
    one-character edit.

    Which change turns this RED: misspelling any key in any entry, or adding a
    key that is not a documented `updates:` option.
    """
    entry = _entry(ecosystem, directory)
    unknown = sorted(set(entry) - _KNOWN_ENTRY_KEYS)
    assert not unknown, (
        f"the {ecosystem} entry on {directory} declares {unknown}, which is not "
        "a documented `updates:` option. Either it is a typo -- an invalid file "
        "is IGNORED by GitHub, not rejected loudly -- or it is a real option "
        "this file has not seen before, in which case add it to "
        "_KNOWN_ENTRY_KEYS having checked it in GitHub's options reference."
    )


@pytest.mark.parametrize(("ecosystem", "directory"), _EXPECTED_ECOSYSTEMS)
def test_no_group_carries_a_key_that_silently_empties_it(ecosystem: str, directory: str) -> None:
    """The group-level ban, applied to EVERY entry rather than only npm.

    It first lived inside the npm-only group test, which left the docker
    `docker-base-images` group open: `exclude-patterns: ["*"]` on it was
    measured leaving the whole module green while the group matched nothing.
    That group is the one keeping the uv builder and the slim runtime in ONE PR
    -- split apart they can land at different interpreter majors and ship a
    non-booting image (#34) -- and test_node_version_pin.py's prose depends on
    it. The ban is per-entry now, so the pin below and this check cover both
    halves: the group still exists, and it still has its reach.

    Which change turns this RED: adding `exclude-patterns` or a group
    `dependency-type` to any group of any entry.
    """
    groups = _entry(ecosystem, directory).get("groups") or {}
    offenders = {
        name: sorted(key for key in _SILENT_DISABLE_GROUP_KEYS if key in group)
        for name, group in groups.items()
        if any(key in group for key in _SILENT_DISABLE_GROUP_KEYS)
    }
    assert not offenders, (
        f"the {ecosystem} entry on {directory} has groups carrying keys that "
        f"subtract from `patterns` AFTER the matching this file checks: "
        f"{offenders}. A group can then be empty in practice with every "
        "assertion here green."
    )


@pytest.mark.parametrize(("ecosystem", "directory"), _EXPECTED_ECOSYSTEMS)
def test_no_group_carries_an_unrecognised_key(ecosystem: str, directory: str) -> None:
    """The same check one level down. `groups.<name>` takes six documented keys
    and nothing else; a typo there invalidates the file just as completely.

    Which change turns this RED: misspelling a key inside any group, in any
    entry — `update_types` for `update-types`, `exclude_patterns`, `pattern`.
    """
    groups = _entry(ecosystem, directory).get("groups") or {}
    offenders = {
        name: sorted(set(group) - _KNOWN_GROUP_KEYS)
        for name, group in groups.items()
        if set(group) - _KNOWN_GROUP_KEYS
    }
    assert not offenders, (
        f"the {ecosystem} entry on {directory} has groups declaring keys that "
        f"are not documented `groups.<name>` options: {offenders}."
    )


def test_the_known_key_list_is_not_so_wide_it_accepts_anything() -> None:
    """The partner to the test above. A `_KNOWN_ENTRY_KEYS` that had grown to
    include everything would leave the unknown-key check passing on any file at
    all, and the check "no unknown keys" reports success by finding NOTHING —
    the shape this repo has been bitten by.

    Which change turns this RED: widening either known set until a plainly
    invalid key is accepted, or re-adding `reviewers` (removed by GitHub in
    2025) or an entry-level `patterns`.
    """
    for invented in ("registires", "package_ecosystem", "scheduel", ""):
        assert invented not in _KNOWN_ENTRY_KEYS, (
            f"{invented!r} is in _KNOWN_ENTRY_KEYS, so the unknown-key check "
            "would accept it. That set must stay the documented options only."
        )
    for retired in ("reviewers", "patterns"):
        assert retired not in _KNOWN_ENTRY_KEYS, (
            f"{retired!r} is back in _KNOWN_ENTRY_KEYS. `reviewers` was removed "
            "from dependabot.yml by GitHub in 2025 (CODEOWNERS replaces it) and "
            "`patterns` is a `groups.<name>` option, so allowing either at entry "
            "level waves through a key GitHub does not accept -- and an invalid "
            "file is IGNORED, which is #423."
        )
    for wrong_level in ("package-ecosystem", "schedule", "labels"):
        assert wrong_level not in _KNOWN_GROUP_KEYS, (
            f"{wrong_level!r} is an entry-level option and must not be accepted "
            "inside a `groups.<name>` block."
        )


def test_both_ban_lists_are_real_subsets_of_the_documented_options() -> None:
    """The partner that stops the two ban lists from being emptied in silence.

    Every banned-key check reports success by finding NOTHING, so an empty ban
    list makes them all pass on a config that is switched off. Measured: with
    `_SILENT_DISABLE_GROUP_KEYS` emptied and `exclude-patterns: ["*"]` added to
    all four npm groups, the whole module stayed green — Dependabot grouping
    nothing, every assertion satisfied. That is the "a check that counts nothing
    needs a partner proving the thing counted exists" rule, and this is the
    partner.

    Which change turns this RED: emptying either ban list, dropping one of the
    four measured offenders from it, or banning a key that is not a documented
    option (which would ban nothing, since the unknown-key check rejects it
    first).
    """
    entry_bans = set(_SILENT_DISABLE_KEYS)
    group_bans = set(_SILENT_DISABLE_GROUP_KEYS)
    assert entry_bans and group_bans, (
        "a ban list is empty, so its check passes on every config -- including "
        "the ones it exists to reject."
    )
    assert entry_bans <= _KNOWN_ENTRY_KEYS, (
        f"these banned entry keys are not documented options: "
        f"{sorted(entry_bans - _KNOWN_ENTRY_KEYS)}. Banning an undocumented key "
        "bans nothing -- the unknown-key check rejects it first -- and it breaks "
        "the escape hatch, which is to delete the key from the ban list alone."
    )
    assert group_bans <= _KNOWN_GROUP_KEYS, (
        f"these banned group keys are not documented `groups.<name>` options: "
        f"{sorted(group_bans - _KNOWN_GROUP_KEYS)}."
    )
    # The specific offenders adversarial review MEASURED surviving. Pinned by
    # name rather than by a count: a count says how many, not which, and the one
    # that gets dropped is the one nobody notices.
    assert {"allow", "cooldown", "ignore", "versioning-strategy"} <= entry_bans, (
        f"an entry-level ban was dropped; the list is now {sorted(entry_bans)}. "
        "Each of the four was measured switching updates off with every other "
        "assertion in this module green."
    )
    assert {"dependency-type", "exclude-patterns"} <= group_bans, (
        f"a group-level ban was dropped; the list is now {sorted(group_bans)}. "
        "`exclude-patterns: ['*']` empties a group while leaving `patterns` -- "
        "and so every pattern assertion here -- intact."
    )


@pytest.mark.parametrize(("ecosystem", "directory"), _EXPECTED_ECOSYSTEMS)
def test_no_entry_is_switched_off_with_a_zero_pr_limit(ecosystem: str, directory: str) -> None:
    """GitHub documents `open-pull-requests-limit: 0` as the way to "temporarily
    disable version updates for a package manager". It is a one-character edit
    that produces exactly the silence #423 was, and it is not absence — the key
    is present and valid — so no absence check can catch it.

    Measured: with only the npm entry pinned, `0` on the docker and
    github-actions entries left every test in this module green.

    Which change turns this RED: setting any entry's limit to 0.
    """
    limit = _entry(ecosystem, directory).get("open-pull-requests-limit")
    assert isinstance(limit, int) and limit >= 1, (
        f"the {ecosystem} entry on {directory} has open-pull-requests-limit "
        f"{limit!r}. Zero is GitHub's documented off-switch for an ecosystem, "
        "and a missing value is the default rather than this repo's choice."
    )


def test_the_docker_base_images_group_another_guard_depends_on_still_exists() -> None:
    """The one pre-existing entry whose GROUPING is load-bearing elsewhere.

    dependabot.yml's own comment: the uv builder and the slim runtime "MUST move
    in lockstep, or a minor bump drifts the builder/runtime interpreter apart and
    ships a non-booting image (the exact class #34 fixed)". And
    test_node_version_pin.py's module docstring states as fact that
    "`.github/dependabot.yml` groups every base image under `/infra/docker`" —
    a premise of that guard, asserted nowhere until here.

    Deliberately narrow: this file does not audit the other entries (see
    _UNGUARDABLE). This one is pinned because deleting it falsifies a different
    guard's reasoning in silence.

    Which change turns this RED: deleting the `docker-base-images` group, or
    narrowing its `*` pattern.
    """
    groups = _entry("docker", "/infra/docker").get("groups") or {}
    assert "docker-base-images" in groups, (
        "the `docker-base-images` group is gone. It is what keeps the uv builder "
        "and the slim runtime in ONE PR; split across two PRs they can land at "
        "different interpreter majors and ship a non-booting image (#34)."
    )
    assert "*" in (groups["docker-base-images"].get("patterns") or []), (
        "the `docker-base-images` group no longer matches every image. Its whole "
        "purpose is that ALL base-image bumps arrive together."
    )


# ──────────────────────────── the npm entry's shape ────────────────────────────


def test_the_npm_entry_targets_the_frontend_on_main() -> None:
    """The whole of #423 in one assertion: an entry whose PARSED
    `package-ecosystem` is npm and whose `directory` is /frontend.

    Which change turns this RED: deleting the npm block, commenting it out,
    renaming its ecosystem, or pointing it at a directory other than /frontend.
    """
    npm = _entry(*_NPM)
    assert npm["directory"] == "/frontend", (
        "The npm entry must point at /frontend -- package.json and "
        "package-lock.json live there, and Dependabot silently finds no "
        "manifest anywhere else."
    )
    assert npm.get("target-branch") == "main", (
        "The npm entry must target `main` explicitly, matching every other entry in this file."
    )


def test_the_npm_schedule_is_monthly() -> None:
    """`main` is protected with `strict: true`, so every merge makes every other
    open PR stale and needing `gh pr update-branch`. Weekly npm batches turn
    that into a standing chore; monthly does not.

    Which change turns this RED: setting `schedule.interval` to weekly or daily.
    """
    schedule = _entry(*_NPM).get("schedule")
    assert isinstance(schedule, dict), "The npm entry declares no `schedule:`"
    assert schedule.get("interval") == "monthly", (
        f"The npm schedule is {schedule.get('interval')!r}. It is monthly on "
        "purpose (#423): a protected, strict `main` makes a batch of open PRs "
        "cost O(n^2) rebases. Change it deliberately, with the comment in "
        "dependabot.yml, or not at all."
    )


def test_the_npm_pr_limit_is_at_or_below_the_backend_s() -> None:
    """Which change turns this RED: raising the npm limit above 3, or above the
    backend's own limit."""
    npm_limit = _entry(*_NPM).get("open-pull-requests-limit")
    backend_limit = _entry("uv", "/backend").get("open-pull-requests-limit")
    assert npm_limit == 3, (
        f"The npm open-pull-requests-limit is {npm_limit!r}, expected 3. Each "
        "open PR is one more that goes stale on every merge into a strict "
        "protected branch."
    )
    assert isinstance(backend_limit, int) and npm_limit <= backend_limit, (
        f"npm's limit ({npm_limit}) must not exceed the backend's "
        f"({backend_limit}): npm updates monthly and the backend weekly, so a "
        "larger npm queue would sit open for longer."
    )


def test_the_npm_entry_auto_rebases() -> None:
    """Which change turns this RED: dropping `rebase-strategy: auto` from the
    npm entry, or setting it to `disabled`."""
    assert _entry(*_NPM).get("rebase-strategy") == "auto", (
        "Without auto-rebase, a monthly npm PR is stale by the time anyone "
        "looks at it -- `main` requires branches to be up to date."
    )


def test_the_npm_commit_prefixes_match_the_rest_of_the_repo() -> None:
    """The prefix is the ONLY evidence in a PR title that the PR came through
    this file. Every `/frontend` Dependabot PR to date carries Dependabot's
    default `chore(...)`/`build(...)` instead, which is how #423 was spotted.

    Which change turns this RED: dropping `commit-message` from the npm entry,
    or changing its prefixes away from the backend's.
    """
    npm_message = _entry(*_NPM).get("commit-message")
    backend_message = _entry("uv", "/backend").get("commit-message")
    assert isinstance(npm_message, dict), "The npm entry declares no `commit-message:`"
    assert isinstance(backend_message, dict), "The uv entry lost its `commit-message:`"
    assert npm_message.get("prefix") == backend_message.get("prefix") == "deps"
    assert (
        npm_message.get("prefix-development")
        == backend_message.get("prefix-development")
        == "deps(dev)"
    )
    assert npm_message.get("include") == "scope", (
        "`include: scope` is what puts `(deps)`/`(deps-dev)` in the title; "
        "without it the two prefixes above are indistinguishable in `git log`."
    )


def test_the_npm_labels_stay_the_two_verified_present() -> None:
    """Dependabot cannot create a label that does not exist; it drops it.

    Named for what it does. Its first name claimed it checked the labels the
    repo "actually has", which it cannot: it pins a hardcoded pair and never
    consults the repo, so deleting the `javascript` label from the repo leaves
    it green. `gh label list` is what settles the question and no test can run
    it offline; recorded in _UNGUARDABLE.

    The two below were confirmed present with `gh label list` while #423 was
    built. What this test CAN hold is that the entry does not drift to an
    invented label like `frontend`, which the repo has never had.

    Which change turns this RED: changing either npm label.
    """
    assert _entry(*_NPM).get("labels") == ["dependencies", "javascript"], (
        "npm labels must stay `dependencies` + `javascript`. `gh label list` "
        "shows those two exist; `frontend` does not, and a label Dependabot "
        "cannot find is silently dropped."
    )


@pytest.mark.parametrize(("ecosystem", "directory"), _EXPECTED_ECOSYSTEMS)
def test_every_label_this_entry_requests_is_one_the_repo_has(
    ecosystem: str, directory: str
) -> None:
    """The npm test above holds ONE entry to two names. This holds EVERY entry
    to the whole snapshot, which is what #436 needed: `python` on uv, `docker`
    on both docker entries and `ci` on github-actions were each requested from
    the day the entry was written, and each was silently dropped on every PR
    those entries ever opened. A config that asks for a label the repo does not
    have is claiming something that does not happen.

    The non-emptiness assertion is not decoration. `all(... in ...)` over an
    empty list is True, so an entry that lost its `labels:` key entirely would
    otherwise satisfy this test while carrying no label at all -- the vacuous
    shape this repo has shipped before.

    Which change turns this RED: adding any label to any entry that is not in
    _LABELS_VERIFIED_PRESENT (reinstating `python`, `docker` or `ci` does it),
    or deleting an entry's `labels:` key.
    """
    labels = _entry(ecosystem, directory).get("labels")
    assert isinstance(labels, list) and labels, (
        f"the {ecosystem} entry on {directory!r} declares no non-empty "
        "`labels:`. Every entry carries at least `dependencies`; without it "
        "its PRs are indistinguishable from a human's in any label filter."
    )
    phantom = [label for label in labels if label not in _LABELS_VERIFIED_PRESENT]
    assert not phantom, (
        f"the {ecosystem} entry on {directory!r} requests {phantom!r}, which "
        "`gh label list` does not return. Dependabot cannot create a label; it "
        "drops it and opens the PR anyway, so the symptom is a PR that merely "
        "looks under-labelled rather than any error (#436). Use a label the "
        "repo has, or delete the request -- do NOT widen "
        "_LABELS_VERIFIED_PRESENT to make this pass."
    )


def test_the_verified_label_snapshot_cannot_be_emptied_or_quietly_shrunk() -> None:
    """The partner. The test above is an "every X is in SET" check, and the
    classic way one of those stops meaning anything is for SET to be widened
    until nothing fails it -- or for its own contents to drift so far from the
    repo that it stops describing anything real.

    `dependencies` is named explicitly because it is on every entry in the
    file: if the snapshot were emptied, the per-entry test would redden on it
    first, and this pins that relationship rather than leaving it to luck.

    Which change turns this RED: emptying or deleting _LABELS_VERIFIED_PRESENT,
    dropping `dependencies` or `github_actions` from it, or adding/removing any
    name without updating _LABELS_VERIFIED_PRESENT_COUNT in the same edit.
    """
    assert _LABELS_VERIFIED_PRESENT, (
        "_LABELS_VERIFIED_PRESENT is empty. Every 'this label is real' check "
        "in this module reads it, and an empty allow-list turns them from a "
        "guard into a blanket rejection nobody would ship -- so it would be "
        "'fixed' by deleting the checks."
    )
    assert len(_LABELS_VERIFIED_PRESENT) == _LABELS_VERIFIED_PRESENT_COUNT, (
        f"_LABELS_VERIFIED_PRESENT holds {len(_LABELS_VERIFIED_PRESENT)} "
        f"names, _LABELS_VERIFIED_PRESENT_COUNT says "
        f"{_LABELS_VERIFIED_PRESENT_COUNT}. Widening the snapshot is exactly "
        "how a phantom label gets waved through: re-run `gh label list` and "
        "update the count in the same edit."
    )
    for required in ("dependencies", "github_actions", "javascript"):
        assert required in _LABELS_VERIFIED_PRESENT, (
            f"{required!r} is used by dependabot.yml and `gh label list` "
            "returns it. Removing it from the snapshot reddens a config that "
            "is correct."
        )


def test_the_labels_checked_are_a_real_population_not_an_empty_one() -> None:
    """The second partner, one level up. The per-entry check is parametrised,
    so deleting a row makes the suite SMALLER rather than RED, and a config
    that had shed its labels everywhere would leave a green, shrunken run --
    which is what a vacuous guard looks like from the outside.

    So count the labels the file actually names, across every entry, and assert
    the population exists. `dependencies` is on all five entries today.

    Which change turns this RED: deleting `labels:` from every entry.
    """
    every_label = [
        label
        for entry in _updates()
        for label in (entry.get("labels") or [])
        if isinstance(label, str)
    ]
    assert every_label, (
        "dependabot.yml names no labels at all. Every label check in this "
        "module would then pass over an empty collection, which is the shape "
        "that has shipped as a guard here before."
    )
    assert set(every_label) <= _LABELS_VERIFIED_PRESENT, (
        f"labels in dependabot.yml that `gh label list` does not return: "
        f"{sorted(set(every_label) - _LABELS_VERIFIED_PRESENT)!r}"
    )


# ─────────────────────────── grouping is real, not a wish ───────────────────────


@pytest.mark.parametrize("group_name", _collect_npm_group_names())
def test_each_npm_group_batches_only_minor_and_patch_version_updates(group_name: str) -> None:
    """A group WITHOUT `update-types` swallows majors too, and a major is the one
    bump that needs its own review and its own revert. `applies-to:
    version-updates` keeps a security fix from being held behind a routine bump.

    Which change turns this RED: deleting `update-types` or `applies-to` from
    any npm group, or adding "major" to an `update-types` list. The
    group-emptying keys (`exclude-patterns`, a group `dependency-type`) used to
    be checked here too; they moved to
    test_no_group_carries_a_key_that_silently_empties_it, which covers every
    entry rather than only npm.
    """
    group = _npm_groups()[group_name]
    assert group.get("applies-to") == "version-updates", (
        f"npm group {group_name!r} must set `applies-to: version-updates` so "
        "security updates stay ungrouped and ship on their own."
    )
    assert sorted(group.get("update-types") or []) == ["minor", "patch"], (
        f"npm group {group_name!r} has update-types "
        f"{group.get('update-types')!r}. It must be exactly minor + patch: a "
        "group that also batches majors hides a breaking change inside a "
        "rollup, and -- specifically -- would hide an @types/node major bump "
        "that has to move together with frontend/.nvmrc (#413)."
    )


@pytest.mark.parametrize(("group_name", "pattern"), _collect_npm_patterns())
def test_every_npm_group_pattern_matches_a_real_frontend_package(
    group_name: str, pattern: str
) -> None:
    """A pattern naming a package that is not in frontend/package.json matches
    nothing and groups nothing, silently -- no error from Dependabot, no failing
    check, just a dependency that keeps opening its own PR.

    Which change turns this RED: misspelling any pattern (`@testing-libary/*`,
    `vitests`), or keeping a pattern for a package that has been removed from
    frontend/package.json.
    """
    # Re-read so that a sentinel parameter (see _collect_npm_patterns) fails on
    # the reason it was collected, not on "the sentinel matched no package".
    assert (group_name, pattern) in _npm_patterns()
    declared = _direct_dependencies()
    matched = sorted(name for name in declared if _matches(pattern, name))
    assert matched, (
        f"npm group {group_name!r} declares pattern {pattern!r}, which matches "
        f"none of frontend/package.json's direct dependencies: "
        f"{sorted(declared)}."
    )


def test_every_direct_frontend_dependency_falls_into_exactly_one_group() -> None:
    """The partner that stops the pattern test above from being vacuous: it
    proves the patterns cover the real population rather than a convenient
    subset of it, and that no package is claimed by two groups (where which one
    wins is Dependabot's file order, not this repo's intent).

    Which change turns this RED: adding a dependency to frontend/package.json
    without adding it to a group, or widening two patterns until they overlap.
    """
    declared = _direct_dependencies()
    owners: dict[str, list[str]] = {name: [] for name in declared}
    for group_name, pattern in _npm_patterns():
        for name in declared:
            if _matches(pattern, name) and group_name not in owners[name]:
                owners[name].append(group_name)

    ungrouped = sorted(name for name, groups in owners.items() if not groups)
    assert not ungrouped, (
        f"frontend/package.json declares {ungrouped} but no npm group in "
        "dependabot.yml matches them, so each opens its own PR. Add each to the "
        "group it belongs to (runtime / build / test / types)."
    )

    overlapping = {name: groups for name, groups in owners.items() if len(groups) > 1}
    assert not overlapping, (
        f"These packages match more than one npm group: {overlapping}. "
        "Dependabot assigns a package to the FIRST group whose patterns match, "
        "so the outcome depends on the order of keys in the YAML rather than on "
        "anything written down."
    )


def test_types_node_is_grouped_where_the_node_pin_guard_expects_it() -> None:
    """@types/node's major is held equal to frontend/.nvmrc by
    test_node_version_pin.py::test_types_node_major_matches_the_pin. That guard
    is only compatible with grouping if the group excludes majors: a batched
    minor/patch bump stays inside the pinned major and stays green, while a
    major bump escapes the group, arrives standalone and turns the required
    `pytest + lint` check RED until .nvmrc moves with it. That red is CORRECT --
    the runtime and the types describing it must move together (#413).

    Which change turns this RED: dropping @types/node out of every group, or
    letting its group batch majors (which would bury the deliberate red inside
    a rollup).
    """
    groups = [
        group_name for group_name, pattern in _npm_patterns() if _matches(pattern, "@types/node")
    ]
    assert len(groups) == 1, (
        f"@types/node is matched by {groups or 'no'} npm group(s); it must be "
        "matched by exactly one."
    )
    # The non-empty check first, and it is not decoration. `"major" not in []`
    # is TRUE, so on a group that has NO `update-types` at all -- the state in
    # which Dependabot DOES batch majors -- the absence check below reports
    # success. Measured: deleting `update-types` from frontend-types left this
    # test green in its first shape, and only its partner
    # (test_each_npm_group_batches_only_minor_and_patch_version_updates) went
    # red. A test that passes for someone else's reason is not a test.
    update_types = _npm_groups()[groups[0]].get("update-types") or []
    assert update_types, (
        f"npm group {groups[0]!r} declares no `update-types`, so it batches "
        "EVERY semver level including majors. An @types/node major would then "
        "arrive inside a rollup instead of standalone."
    )
    assert "major" not in update_types, (
        f"npm group {groups[0]!r} batches majors, so an @types/node major bump "
        "would arrive inside a rollup PR. It must arrive standalone: it is the "
        "bump that requires frontend/.nvmrc, the Dockerfiles and the workflows "
        "to move in the same PR."
    )
    assert "@types/node" in _direct_dependencies(), (
        "frontend/package.json no longer declares @types/node, so the grouping "
        "assertion above is about a package that does not exist."
    )


# ──────────────────── the matcher, and what this file cannot see ────────────────


# Planted cases for `_matches`, house style (test_node_version_pin.py's
# `test_the_dockerfile_scan_recognises_real_from_syntaxes`). Three assertions
# route through `_matches` and nothing proved it distinguishes a match from a
# non-match: mutating it to be case-insensitive left the whole module green,
# and the mutants that did die were all killed by ONE downstream test rather
# than by the matcher's own contract.
_MATCHER_CASES: tuple[tuple[str, str, bool], ...] = (
    # Exact names are exact. `react` must NOT swallow `react-dom`, or the two
    # would be one group's problem and the census would report an overlap.
    ("react", "react", True),
    ("react", "react-dom", False),
    ("react", "preact", False),
    # A trailing `*` becomes `.*`, so `/` is not a boundary for it.
    ("@types/*", "@types/node", True),
    ("@types/*", "@types/react-dom", True),
    ("@types/*", "@typescript-eslint/parser", False),
    ("@testing-library/*", "@testing-library/jest-dom", True),
    ("@testing-library/*", "@playwright/test", False),
    # Case-INSENSITIVE, because WildcardMatcher downcases both sides. These three
    # rows are the ones that matter: with a case-sensitive model the last of
    # them reports ONE owner where Dependabot sees two, and the group a package
    # lands in is then decided by YAML order rather than by anything written
    # down. That is the overlap test's stated failure, passing silently.
    ("React", "react", True),
    ("@TYPES/*", "@types/node", True),
    ("js*", "JSONStream", True),
    # A bare `*` matches everything, which is how the docker entry groups every
    # base image.
    ("*", "react", True),
    ("*", "@types/node", True),
    # `?` and `[...]` are LITERAL. `Regexp.quote` escapes everything that is not
    # a `*`, so WildcardMatcher has exactly one metacharacter. An fnmatch-based
    # model disagrees on all four of these rows, which is how the wrong citation
    # in the previous version was found. npm names cannot contain `?` or `[`, so
    # no real manifest reaches these -- they pin the matcher's contract, not a
    # package.
    ("react?", "react-", False),
    ("react?", "react?", True),
    ("[r]eact", "react", False),
    ("[r]eact", "[r]eact", True),
)

# Pinned by count as well as content. Every row above is a separate parametrised
# test, so deleting one removes a test rather than failing one -- pytest reports
# a smaller run, not a red one, and this repo has shipped that shape before.
_MATCHER_CASE_COUNT = 17


@pytest.mark.parametrize(("pattern", "package", "expected"), _MATCHER_CASES)
def test_the_group_matcher_distinguishes_a_match_from_a_near_miss(
    pattern: str, package: str, expected: bool
) -> None:
    """Which change turns this RED: making `_matches` case-insensitive, turning
    the exact-name branch into a substring or prefix test, or making the
    trailing-`*` branch treat `/` as a boundary."""
    assert _matches(pattern, package) is expected


def test_the_matcher_cases_cover_both_answers() -> None:
    """The partner: a table of only-True cases would let `_matches` return True
    unconditionally and stay green, and a table of only-False cases would let it
    return False unconditionally.

    Which change turns this RED: deleting every True case, or every False one.
    """
    answers = {expected for _, _, expected in _MATCHER_CASES}
    assert answers == {True, False}, (
        f"the matcher table only asserts {answers}. A one-sided table is "
        "satisfied by a constant function."
    )


def test_the_guards_own_blind_spots_are_written_down() -> None:
    """State what this file CANNOT see, so the gap is recorded, not assumed away.

    The first version of this module claimed, in its own docstring, to
    distinguish a config that is "declared and effective" from one where "the
    word appears somewhere". Adversarial review then measured five shapes that
    are declared, ineffective, and green: `ignore`, `cooldown`,
    `exclude-patterns`, a group `dependency-type`, and
    `open-pull-requests-limit: 0`. Those are now asserted. What remains
    genuinely out of reach is below.

    This test asserts nothing about dependabot.yml. It exists so the limitation
    is in the file a maintainer reads, rather than discovered the hard way.
    """
    assert _UNGUARDABLE, "the blind-spot register must not be silently emptied"
    # Truthiness alone lets a single entry be deleted in silence, which is how a
    # recorded limitation quietly stops being recorded. Pin the hand-count.
    assert len(_UNGUARDABLE) == _UNGUARDABLE_COUNT, (
        f"the blind-spot register holds {len(_UNGUARDABLE)} entries, "
        f"_UNGUARDABLE_COUNT says {_UNGUARDABLE_COUNT}. Adding or closing a "
        "blind spot is a deliberate act: update the count in the same edit."
    )


def test_the_matcher_table_has_not_lost_a_case() -> None:
    """Each row of _MATCHER_CASES is its own parametrised test, so deleting one
    makes the suite SMALLER rather than RED — and a smaller green run is exactly
    what a vacuous guard looks like.

    Which change turns this RED: deleting any row from _MATCHER_CASES without
    updating _MATCHER_CASE_COUNT.
    """
    assert len(_MATCHER_CASES) == _MATCHER_CASE_COUNT, (
        f"_MATCHER_CASES holds {len(_MATCHER_CASES)} rows, _MATCHER_CASE_COUNT "
        f"says {_MATCHER_CASE_COUNT}. Adding or removing a case is a deliberate "
        "act: update the count in the same edit."
    )
    # The case-folding rows specifically. They are the ones that encode a
    # measured defect (see _matches), so name them rather than trusting the
    # count to notice which row went missing.
    assert ("js*", "JSONStream", True) in _MATCHER_CASES, (
        "the case-folding overlap case is gone. It is the one that fails OPEN "
        "if _matches is case-sensitive: the overlap census reports one owner "
        "where Dependabot sees two."
    )
