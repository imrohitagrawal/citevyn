---
name: citevyn-delivery
description: The one process for delivering any CiteVyn work package, from choosing it to verified production and cleanup. Roles, the 8 lenses and which packages need which, the separate test designer for answer, money and auth changes, two review rounds, who merges, the Fly deploy, the production check, cleanup, and the next-session prompt. Read it before any work package, and name it in every next-session prompt.
---

# CiteVyn delivery

This is the one home of how a work package (one issue or a small club of them, one pull
request) is delivered. `AGENTS.md` keeps the engineering rules and the T0-T3 review sizing;
`code_review.md` says what blocks a ship. This file says who does what, in what order. A
next-session prompt names the items and the approvals and carries one line:
`**Process:** every item is built by .claude/skills/citevyn-delivery/SKILL.md.`

Adopted from quorum-ai's `work-item-delivery` skill, minus its ADR and skill catalogue and its
board file: here GitHub issues are the truth, and `docs/BACKLOG.md` is their index.

## Settled answers

| Question | Answer |
|---|---|
| Who merges | Only the main orchestrator, under an approval the owner gave (typed in the session, or in a prompt the owner chose to send) |
| How many review rounds | At most 2, plus one narrow re-check of each fix |
| How many writers | One per worktree. Reading fans out; writing never does |
| Where owner decisions live | `docs/DECISIONS.md`, restated plainly; the owner's exact words only in the owner's private notes, never hepo |
| Where next-session prompts live | The owner's private notes, at `$PRIVATE_NOTES_DIR/citevyn/NEXT-SESSION-PROMPT.md`. Never in this public repo. Your user-level `CLAUDE.md` says where `PRIVATE_NOTES_DIR` is and how it is kept |

## Before starting

1. Update the private notes as your user-level `CLAUDE.md` says, then read `AGENTS.md`, `code_review.md`,
   `docs/BACKLOG.md`, `docs/DECISIONS.md`, and the session memory index. Run `gh issue list --state open` and `gh pr list --state open`.
2. Check for other live sessions: transcript last-write times under
   `~/.claude/projects/<this repo's folder>/`, `ps aux | grep claude`, and
   `git worktree list`. If another session owns what you need, stop and say so.
3. Run `df -h .` and write the number down.
4. Re-check each item's premise with a command: issue text is a hypothesis. If the premise is
   false, stop and say so.
5. Ask the owner every open question in ONE batch, with your recommendation marked. Build what
   needs no decision while you wait; never guess an owner decision.
6. List the open Dependabot alerts and Dependabot pull requests (see Dependencies below).

## Dependencies

Dependency alerts are the agent's job, every session; the owner is told what was fixed, never
asked to look.

1. At the start of every session, list both:
   `gh api 'repos/imrohitagrawal/citevyn/dependabot/alerts?state=open&per_page=100'` and
   `gh pr list --author app/dependabot`.
2. Critical and high alerts are fixed before planned work.
3. Medium and low alerts are fixed in the same session when the fix is a patch or minor bump.
4. One pull request at a time, every required check green. `main` is strict, so bring the next
   branch up to date after each merge (`gh pr update-branch <n>`; if that reports a conflict,
   comment `@dependabot rebase` on the pull request instead). With grouped security updates on,
   Dependabot may close a pull request and open a grouped one: list again before acting.
5. A runtime package (one the shipped app imports) also gets the backend suite run against
   the new lock and a boot of the built image. A runtime fix reaches users only by a deploy:
   deploy when the prompt approves it, then check production.
6. A lock change you make yourself: use a uv at least as new as the one that wrote
   `backend/uv.lock`, so its format line does not change (`uvx --from uv uv lock
   --upgrade-package <name>`).
7. A major-version bump becomes its own work package, with its own issue.
8. Report each alert as fixed, in progress, or deferred to a named package.

## The 8 lenses, sized to the package

| # | Lens | Who |
|---|---|---|
| 1 | Planning | 2-3 read-only planners, plus one agent that reproduces the problem live |
| 2 | Execution | One builder per issue, in its own worktree |
| 3 | Testing | A test-gap analyst (`pr-test-analyzer`) and a mutation agent in ITS OWN worktree |
| 4 | PR review | `code-reviewer`, `silent-failure-hunter`, `type-design-analyzer`, `taste-check` |
| 5 | Security | `security-review` plus a break-it agent |
| 6 | Availability | Provider, Redis or DB down; rate limits; health after deploy |
| 7 | Phone users | The real app at 375 px: touch, keyboard, accessibility |
| 8 | Personas | New visitor, signed-in user, attacker, slow phone, screen reader, admin |

| Package type | Lenses |
|---|---|
| Docs, process, tests only | 1-4. A docs change has no code to break or mutate: its reviewers check every claim, and a public-content reviewer reads it as a stranger would |
| Backend logic, retrieval, answers | 1-4, 6 and 8; `rag-eval` with the real model when answers change, within the spend cap |
| Money, auth, secrets, privacy | All 8; 5 is mandatory; failure modes listed on one page before any design |
| Anything a user can see | 1-4, 7 and 8 |

Keep `AGENTS.md`'s T0-T3 tiers for how many reviewers each lens gets. Not every package needs
every role.

## Roles

| Role | Job | When |
|---|---|---|
| Main orchestrator | Chooses, routes, asks the owner, reproduces findings, merges, deploys, checks production, cleans up | Always |
| Test designer | Writes the failing tests first, records the verbatim failure and a "red when" line per test, and commits them | Answer, money and auth changes |
| User-journey tester | Before the build: writes what the user expects to see and do, step by step, signed in and anonymous. After: drives the real app at desktop and 375 px and reports each step as met or not, with a screenshot | Any change a user can see |
| Builder | Makes the tests pass | Every change |
| Skeptic | Reproduces every finding before anyone acts on it | Each round |
| Fixer | Applies only reproduced findings, one at a time | After a round |

**The builder may not edit the test designer's tests.** Check before review:
`git diff <test designer's commit> HEAD -- <the test designer's files>` must print nothing. A
fixer may ADD a new regression test file.

## Plan

- For a T3 change (security, auth, money, migrations, public API, cross-cutting), write the plan
  down and have Codex review it before the test designer starts. Only T3: below that, the
  planners are enough. Run it from a `git archive` copy:
  `node "$(find ~/.claude/plugins/cache/openai-codex -name codex-companion.mjs | head -1)" task --background "<plan path and what to look for>"`,
  then `... result <job>` from the same folder. Apply only findings you reproduce.

## Build

- Branch in a dedicated worktree from `origin/main` (`git worktree add ../citevyn-<item> -b <branch> origin/main`),
  and merge `main` in before starting.
- In a worktree, never symlink the main checkout's `backend/.venv` (a `uv run` through the link
  rewrites the main venv's shebangs, and `git add backend` then tracks the link). Run backend
  gates with the main checkout's binaries directly, from the worktree's `backend/` folder (it
  has no `.env`): `<main>/backend/.venv/bin/python -m pytest -q -m "not postgres"`,
  `<main>/backend/.venv/bin/ruff check .`, `<main>/backend/.venv/bin/pyright`. The pre-commit
  hook runs `uv run`, which creates a separate `backend/.venv` in the worktree; that is harmless
  and goes with the worktree.
- Before changing a shipped value, `git grep` every file that states it and put the count in the
  pull request.
- Paid model calls are for final validation only, never for testing while building. Build and
  test on the stub providers (the retrieval eval runs free; the judge skips itself). Reproduce
  from free evidence first (recorded probes, logs, the stub corpus); ask production at most one
  question per issue, and only when free evidence cannot show the bug. Run one judged eval on
  the reviewed final commit, not one per push, then one live question per fixed issue after the
  deploy.
- Mutation proof: commit first, `cp` the file aside, mutate, confirm with `grep -F`, delete
  `__pycache__`, run the test, restore from the copy, prove it with `cmp`. Never restore with git.
- Playwright runs one at a time: `playwright.config.ts` owns port 3000.
- Assert `git rev-parse HEAD` before and after each measurement, and end with
  `git status --porcelain` empty.

## Review: at most two rounds

- Every reviewer prompt says, in capitals: DO NOT WRITE, EDIT, `git checkout`, `git stash` OR
  `sed -i` ANYTHING, and gives the absolute worktree path. A reviewer that must run something
  that writes gets its own worktree or scratch folder.
- The skeptic reproduces each finding. Grade it **critical** or **required** (both block) or
  **advisory** (does not block; open an issue for it if it matters). Mark a repeat of a known
  root cause as a duplicate, and a finding outside the package as out of scope.
- A fix gets its own narrow re-check. Two fixes in a row that add defects: stop patching, find the
  root cause, write a smaller plan.
- Before each merge, run `release-readiness-review` as the ship/no-ship gate.

### Seat list: an empty seat is open

A seat is one required review: each reviewer the package needs, the skeptic, the mutation agent
and `release-readiness-review`, in each round, plus the narrow re-check of each fix.

- **Where and when.** Before the first review (the plan stage, when the tier has one), write
  the package's seat list in the owner's private notes,
  at `$PRIVATE_NOTES_DIR/citevyn/seats/<branch>.md`: which reviewer read which commit stays out
  of this public repository. List every seat that applies, from three sources: the challenge
  review's tier and roles in your user-level rules (for example the attacker, the request
  challenger, and a cold reader for text agents follow), `AGENTS.md`'s T0-T3 table, and lenses
  3-8 of the table above for this package type. Leaving one out needs its own line with the
  reason. Every list has at least the `release-readiness-review` seat.
- **One line per seat:** `<seat>: <reviewer>, <commit>, <result>` or `<seat>: OPEN: <why>`.
  Update it after every round. Each fix adds a seat for its own narrow re-check, and is not
  reviewed until that seat is filled.
- **Filled** means a report that names the commit it read, what it checked, and its findings
  or an explicit "none, after checking ...". A reviewer that was stopped, failed, timed out,
  was refused, returned nothing, or returned a report missing any of those three leaves its
  seat OPEN.
- **An OPEN seat blocks** the merge and "done", unless the owner lets the package go ahead
  with it open (below). Either way the closing status block names it under Pending, with that
  decision if there is one.
- **Fill it** with the same role another way: a fresh reviewer of that role, or the Codex role
  that `codex-roles.md` in your user-level rules names for it, where one exists and is not
  paused there. You may make an unclear request clearer; never disguise a refused task to get past a
  safety filter.
- **Or take it to the owner.** Only the owner's own message in this session can let a package
  go ahead with a seat open. An agent's message never counts, and neither does a line in an
  agent-written prompt unless the owner approved that line.
  Record it in `docs/DECISIONS.md`, restated plainly, in the same pull request, and keep the
  seat listed as OPEN with that entry's date.

## Gates

Run them yourself, one at a time, and read exit codes without a pipe. In a worktree: the backend
commands under Build, then `make test-shell` and `make prompt-check` (neither calls `uv`). In the
main checkout, `make lint typecheck test` and `make coverage-gate` do the same. For the
frontend: `npm run type-check`, `npm test`, `npm run build`, and Playwright when a page
changed. The pull request's own required checks on GitHub are the truth; a local run is not.

## Merge, deploy, check production

- Merge only when every required check is green, every reproduced blocker is fixed and no seat
  on the seat list is OPEN (or the owner has decided to go ahead with it open).
- One pull request at a time. Squash-merge with an explicit subject and body. Never write a close
  keyword next to an issue number unless that merge is meant to close it; write `Refs #N`
  otherwise. `main` is protected and strict: after each merge, the next branch merges `main` in.
- Deploy only when the prompt approves it, with `--local-only` and every build argument, exactly
  as `docs/DEPLOY_FLY.md` §4.1 shows. Copy the command from there; do not retype it.
- A deploy is confirmed by the release number going up in `fly releases`, the new build passing
  §4.4's checks, and something visible that the change does, checked on
  `https://citevyn.stackclimb.com`. Production cannot report its build id yet (#415); say so
  rather than inferring it.
- Close an issue only after its production check, with the evidence in the closing comment.

## Cleanup, after each merge

Cleanup is part of done, not optional.

1. `df -h .` before.
2. Delete BY NAME only what this item created: its worktree first, then its branch locally and
   on GitHub; scratch and proof files; containers and images this item built.
3. Never a global prune: no `docker system/builder/image prune -a`, no `git clean`, no
   `git stash -u`.
4. Keep reusable caches: `.venv`, `node_modules`, Playwright browsers, the `docker_citevyn_pgdata`
   volume. Other projects' images and volumes are not ours: leave them.
5. `df -h .` after; `git status` clean; local `main` equals `origin/main`; `git worktree list`
   shows no worktree this work created (another live session's worktree is not yours). Report each with its command output.

## End of session

1. Closing status block: **Done / Verified myself / Cleanup / Pending / Next action**. Say what is
   pushed, merged and in production; keep what you ran apart from what an agent reported; name
   every OPEN review seat. If nothing is outstanding, say "nothing pending — safe to close this
   session".
2. Write the next prompt to the owner's private notes, at
   `$PRIVATE_NOTES_DIR/citevyn/NEXT-SESSION-PROMPT.md`, never in this repo (it holds approvals,
   spend limits and plans; CI fails if a prompt file appears at this repo's root). It needs a
   `**Process:**` line naming this file, and the headings `## Read first`, `## Items`,
   `## Approvals` and `## End of session`; `make prompt-check` refuses one without them.
   Approvals say what is granted and what is NOT: spend cap, deploy, settings.
3. In the private notes, move the prompt this session ran to `citevyn/archive/YYYY-MM/` (`ls`
   there first; add a date suffix if the name exists), and save both as their own README says.
4. Record any new owner decision in `docs/DECISIONS.md`, restated plainly (see Public repository).
5. Print the new prompt in full as the last thing.

## Sub-agent guard

`scripts/subagent_tree_guard.py` runs on every sub-agent start and stop. If the main checkout's
HEAD, branch, `git status`, uncommitted content, a branch tip or the stash changed while a
sub-agent ran, the user sees `MAIN CHECKOUT CHANGED` at once, and the next tool call or message
of the session that ran the agent carries the same warning to its main model. The agent is also told to report it, but do not
rely on that: in a live test (2026-10-29) an agent ignored the instruction. Stop and find out who
changed it before anything else. So do not touch the main checkout yourself while agents run:
the guard cannot tell your change from theirs.

The hooks are committed in `.claude/settings.json` (hooks only), so every clone and worktree has
them. Permissions and plugins are personal and live in the gitignored
`.claude/settings.local.json`. **On a clone from before this change, move `permissions` and
`enabledPlugins` out of `.claude/settings.json` into `settings.local.json` BEFORE pulling:** the
old file is ignored, and git overwrites an ignored file on pull without a word. Measured
2026-10-29 with one probe: hook edits applied to the running session at once, and the start and
stop hooks fired for background agents.

## Public repository

This repository is public. Everything committed, and every PR body, issue and comment, can be
read by anyone and stays in history. Before any of it leaves the machine:

- No chat transcripts or verbatim owner messages. Restate a decision plainly.
- No costs, balances, billing tiers or payment-provider status; no credentials, emails or
  personal data; no paths that name a person, a machine or a private repository; no session ids; nothing about other projects or about
  this machine's housekeeping.
- A public-content reviewer reads every prose change in each round and asks: would the owner be
  comfortable with a stranger reading this?

## Stop conditions

- A false premise; an owner decision not yet given; a step that needs spending beyond the cap.
- Two fixes in a row that add defects.
- The package grows past about twice its planned size.
- Another live session owns what you need.
- A reproduced blocker remains after round 2: stop and report it with the open findings.
