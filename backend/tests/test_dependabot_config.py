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
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT_YML = REPO_ROOT / ".github" / "dependabot.yml"
FRONTEND_PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"

# Every manifest section that declares a DIRECT dependency. An earlier version
# of this line listed only the first two and called them "both" sections npm
# installs from, which is false: `optionalDependencies` are installed too, and
# `peerDependencies` are declared directly whether or not npm installs them.
# frontend/package.json has neither today, so the error was latent -- but a
# package added under one would have sat outside this guard's population,
# ungrouped, and invisible to the census below. All four are read.
#
# Deliberately NOT shared with test_node_version_pin.py's _TYPES_NODE_SECTIONS,
# which is the same two strings for a DIFFERENT reason: that one is the set npm
# resolves a version from, so its guard fails when @types/node appears in more
# than one of them. This one is "everywhere a direct dependency can hide", and
# wants to be as wide as possible. Unifying them would couple two invariants
# that should be free to move apart.
#
# A transitive package (browserslist, fast-uri, @vitest/mocker -- all three have
# opened security PRs on this repo) appears in none of these and is reached only
# by a security update, which ignores dependabot.yml.
_DIRECT_SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)

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

# Keys that are VALID, parse cleanly, leave every other assertion in this file
# green -- and switch scheduled updates off, wholly or in part. This is the
# #423 outcome reached from the other direction: the entry is declared and
# ineffective, which reads as "nothing to update" exactly like the absent entry
# did. Adversarial review measured each of these surviving the first version of
# this file, which asserted structure but never absence:
#
#   ignore: [{dependency-name: "*"}]      -- no version update ever opens again.
#   allow: [{dependency-type: "production"}]
#   dependency-type: "production"         -- drops every devDependency, which
#                                            here is the whole toolchain plus
#                                            @types/node itself.
#   open-pull-requests-limit: 0           -- GitHub documents zero as the way to
#                                            "temporarily disable version
#                                            updates for a package manager".
#                                            Checked separately below, because
#                                            the key must be PRESENT, not absent.
#   versioning-strategy: ...              -- changes what gets written to the
#                                            manifest at all.
#
# None of these is banned on principle; each is banned from arriving SILENTLY.
# If one is genuinely wanted, delete it from this tuple in the same commit and
# say why -- the same treatment `_INTENTIONALLY_NOT_ON_PUSH` gets in
# test_ci_workflow_conditions.py.
_SILENT_DISABLE_KEYS: tuple[str, ...] = (
    "ignore",
    "allow",
    "cooldown",
    "dependency-type",
    "versioning-strategy",
)

# The group-level members of the same family, both measured surviving:
# `exclude-patterns: ["*"]` leaves `patterns` intact, so every pattern-to-package
# assertion below still passes while Dependabot groups nothing; a group
# `dependency-type: development` makes `frontend-runtime` (react, react-dom, the
# only two production dependencies) match nothing.
_SILENT_DISABLE_GROUP_KEYS: tuple[str, ...] = ("exclude-patterns", "dependency-type")

# Every key an `updates:` entry may carry. An UNKNOWN key -- a typo like
# `registires:` for `registries:` -- is the cheapest way to invalidate this
# file, and GitHub's response to an invalid file is to ignore it, which is #423
# again. Whether Dependabot's schema truly rejects unknown keys is UNVERIFIED
# offline and recorded in _UNGUARDABLE; asserting a closed set costs nothing
# either way and turns a typo into a failing test instead of silence.
#
# Taken from GitHub's options reference (docs.github.com/code-security/
# dependabot/working-with-dependabot/dependabot-options-reference). Keys this
# repo bans outright are in _SILENT_DISABLE_KEYS above and are NOT listed here.
_KNOWN_ENTRY_KEYS: frozenset[str] = frozenset(
    {
        "package-ecosystem",
        "directory",
        "directories",
        "schedule",
        "target-branch",
        "open-pull-requests-limit",
        "rebase-strategy",
        "commit-message",
        "groups",
        "labels",
        "milestone",
        "assignees",
        "reviewers",
        "pull-request-branch-name",
        "registries",
        "insecure-external-code-execution",
        "patterns",
        "vendor",
    }
)

# What this file CANNOT see. Recorded house-style (test_node_version_pin.py's
# `_UNGUARDABLE`) because the first version of this guard asserted completeness
# and was wrong about it: adversarial review found five silent-disable shapes it
# missed. A guard that does not write its holes down reads as having none.
_UNGUARDABLE: tuple[str, ...] = (
    "Whether GitHub ACCEPTS this file. Nothing in CI runs Dependabot, and there "
    "is no validation endpoint. These tests assert the shape this repo intends; "
    "the schema itself is checked only by GitHub, which reports a rejected file "
    "in the repo's Dependabot UI and nowhere a test can read. Settling command "
    "for a schema question: the options reference at "
    "docs.github.com/code-security/dependabot/working-with-dependabot/"
    "dependabot-options-reference.",
    "Whether a scheduled run actually OPENS a PR. Dependabot may find nothing to "
    "update, hit its own errors, or be disabled at the repo or org level -- all "
    "of which look identical from in here. The observable proof is a PR whose "
    "title carries this file's `deps`/`deps(dev)` prefix rather than "
    "Dependabot's default `chore(...)`/`build(...)`.",
    "Everything the four PRE-EXISTING entries configure beyond their existence, "
    "their PR limit and the absence of a silent-disable key. Their schedules, "
    "target branches and (except docker's, pinned below) their groups are "
    "unasserted. Deliberate scope: #423 is about npm. The sharpest gap is "
    "docker's `docker-base-images` group, which is why that ONE is pinned -- "
    "test_node_version_pin.py's prose depends on it grouping every base image.",
    "Transitive packages. `frontend/package-lock.json` is not read here, so a "
    "group pattern naming a transitive dependency would be reported as matching "
    "nothing even though Dependabot could reach it through a security update. "
    "Every pattern in the file today names a DIRECT dependency, so the question "
    "has not arisen.",
    "Whether `labels` name labels the repo really has. `gh label list` settles "
    "it and no test can run it offline; the npm labels are pinned to the two "
    "verified present when #423 shipped, which catches drift to an invented "
    "name but not a label someone later DELETES from the repo.",
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
    """Dependabot's group matcher: a literal name, or one trailing ``*``.

    Deliberately narrower than ``fnmatch``. This repo only writes exact names
    and ``prefix/*`` wildcards, and a matcher that accepts more than the
    patterns actually in the file would let a typo'd pattern pass by matching
    something it should not.
    """
    if pattern.endswith("*"):
        return package.startswith(pattern[:-1])
    return package == pattern


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

    Which change turns this RED: adding `ignore`, `allow`, `cooldown`,
    `dependency-type` or `versioning-strategy` to any entry. If one is genuinely
    wanted, take it out of _SILENT_DISABLE_KEYS in the same commit and say why.
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
    key that is neither in _KNOWN_ENTRY_KEYS nor deliberately banned.
    """
    entry = _entry(ecosystem, directory)
    unknown = sorted(set(entry) - _KNOWN_ENTRY_KEYS - set(_SILENT_DISABLE_KEYS))
    assert not unknown, (
        f"the {ecosystem} entry on {directory} declares {unknown}, which is in "
        "neither _KNOWN_ENTRY_KEYS nor the banned list. Either it is a typo, or "
        "it is a real option this file has not seen before -- in which case add "
        "it to _KNOWN_ENTRY_KEYS, having checked it in GitHub's options "
        "reference."
    )


def test_the_known_key_list_is_not_so_wide_it_accepts_anything() -> None:
    """The partner to the test above. A `_KNOWN_ENTRY_KEYS` that had grown to
    include everything would leave the unknown-key check passing on any file at
    all, and the check "no unknown keys" reports success by finding NOTHING —
    the shape this repo has been bitten by.

    Which change turns this RED: adding a banned key to _KNOWN_ENTRY_KEYS, or
    widening it until a plainly invalid key is accepted.
    """
    assert not (_KNOWN_ENTRY_KEYS & set(_SILENT_DISABLE_KEYS)), (
        "a key cannot be both allowed and banned; _KNOWN_ENTRY_KEYS overlaps "
        f"_SILENT_DISABLE_KEYS at {sorted(_KNOWN_ENTRY_KEYS & set(_SILENT_DISABLE_KEYS))}"
    )
    for invented in ("registires", "package_ecosystem", "scheduel", ""):
        assert invented not in _KNOWN_ENTRY_KEYS, (
            f"{invented!r} is in _KNOWN_ENTRY_KEYS, so the unknown-key check "
            "would accept it. That list must stay the documented options only."
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


# ─────────────────────────── grouping is real, not a wish ───────────────────────


@pytest.mark.parametrize("group_name", _collect_npm_group_names())
def test_each_npm_group_batches_only_minor_and_patch_version_updates(group_name: str) -> None:
    """A group WITHOUT `update-types` swallows majors too, and a major is the one
    bump that needs its own review and its own revert. `applies-to:
    version-updates` keeps a security fix from being held behind a routine bump.

    Which change turns this RED: deleting `update-types` or `applies-to` from
    any npm group, adding "major" to an `update-types` list, or adding an
    `exclude-patterns` / `dependency-type` that quietly empties the group.
    """
    group = _npm_groups()[group_name]
    assert group.get("applies-to") == "version-updates", (
        f"npm group {group_name!r} must set `applies-to: version-updates` so "
        "security updates stay ungrouped and ship on their own."
    )
    # The group-level silent-disable family. Both leave `patterns` in place, so
    # every pattern-to-package assertion below still passes while Dependabot
    # groups nothing -- measured green against the first draft of this module.
    narrowing = sorted(key for key in _SILENT_DISABLE_GROUP_KEYS if key in group)
    assert not narrowing, (
        f"npm group {group_name!r} carries {narrowing}. `exclude-patterns` and a "
        "group `dependency-type` subtract from `patterns` AFTER the matching "
        "this file checks, so the group can be empty in practice with every "
        "assertion here green. `exclude-patterns: ['@types/node']` would remove "
        "the one package #413 exists for."
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
    # A trailing `*` is a prefix match, and `/` is not a boundary for it --
    # Dependabot's matcher is Ruby's File.fnmatch WITHOUT FNM_PATHNAME.
    ("@types/*", "@types/node", True),
    ("@types/*", "@types/react-dom", True),
    ("@types/*", "@typescript-eslint/parser", False),
    ("@testing-library/*", "@testing-library/jest-dom", True),
    ("@testing-library/*", "@playwright/test", False),
    # Case-sensitive on purpose. Dependabot folds case; this matcher does not,
    # so it is NARROWER -- a pattern whose case is wrong matches nothing here
    # and reddens test_every_npm_group_pattern_matches_a_real_frontend_package
    # instead of passing quietly. Fails safe, and pinned so it stays that way.
    ("React", "react", False),
    ("@TYPES/*", "@types/node", False),
    # A bare `*` matches everything, which is how the docker entry groups every
    # base image.
    ("*", "react", True),
    ("*", "@types/node", True),
)


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
