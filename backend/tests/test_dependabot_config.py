"""Guards ``.github/dependabot.yml`` against the SILENT-DISABLE failure (#423).

A malformed or incomplete ``dependabot.yml`` does not fail loudly. GitHub
ignores the file, or the offending entry, and the only symptom is an absence of
pull requests — which is indistinguishable from "there was nothing to update".
Nothing in CI executes this file, so nothing else in the repo can observe it.

That is not hypothetical here. ``npm`` was never declared, so ``frontend/``
received security PRs (which bypass this file entirely) and no scheduled version
updates at all, for the life of the repo. ``@types/node`` sat two majors behind
``frontend/.nvmrc`` until #413 found it by accident.

So these tests assert the PARSED structure, never the text. A test that greps for
the string ``npm`` passes on a file where the entry has been commented out, moved
under the wrong key, or had its indentation broken — every shape that actually
causes the outage. ``yaml.safe_load`` plus a lookup by
``(package-ecosystem, directory)`` is the only assertion that distinguishes
"declared and effective" from "the word appears somewhere".

The pattern-to-package tests exist for the second silent failure in the same
file: a ``groups.patterns`` entry naming a package that is not in
``frontend/package.json`` matches nothing and groups nothing, with no error
anywhere.
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

# Both manifest sections npm installs a DIRECT dependency from. Dependabot's
# version updates only ever touch these two; a transitive package (browserslist,
# fast-uri, @vitest/mocker — all three have opened security PRs on this repo)
# is reached only by a security update, which ignores dependabot.yml.
_DIRECT_SECTIONS = ("dependencies", "devDependencies")

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
    entries, or retargeting its `directory:`."""
    assert _entry(ecosystem, directory)["package-ecosystem"] == ecosystem


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


def test_the_npm_labels_are_labels_this_repo_actually_has() -> None:
    """Dependabot cannot create a label that does not exist; it drops it.

    The two below were confirmed present with `gh label list` while #423 was
    built -- that command is what settles this, and no test can run it. What
    this test CAN hold is that the entry does not drift to an invented label
    like `frontend`, which the repo has never had.

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
    any npm group, or adding "major" to an `update-types` list.
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
    update_types = _npm_groups()[groups[0]].get("update-types") or []
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
