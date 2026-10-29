# Owner decisions

Product and process decisions the owner has made, newest first. Read this before
asking the owner anything; the answer may already be here.

**Rules for this file**

- Each entry is one decision, stated plainly, with its date and where it applies.
  It is written by the session that recorded it, from the owner's instruction.
  The owner's own messages are kept privately, outside this repository.
- Only what the owner decided goes here. A session's recommendation is not a
  decision until the owner accepts it. An open question goes in its issue as
  `PROPOSED — AWAITING OWNER`, not here.
- This repository is public. Record the decision, not the conversation: no
  costs or account details, no credentials, no personal data, nothing about
  other projects or machines.
- Add new entries at the top.

---

### 2026-10-29 — One shared public-content guard that covers every client
There is one public-content guard, kept in the shared CI repository and used by
every repository instead of a copy per project. It covers every way an agent or
a person publishes, and no layer removes a check another layer had: a git
pre-push hook for pushes, a `gh` wrapper that checks what `gh` posts at the
moment it runs, a Claude Code hook for GitHub tools and for ways round the
guard, and a CI step on public repositories as a backstop.

### 2026-10-29 — The owner's two published contact addresses are allowed
The commit address and the business contact address are public on purpose
(commits and the legal pages already show them). The guard allows exactly those
two addresses wherever they appear; any other personal address is still
blocked. The list lives in one file in the shared guard, and changing it is its
own reviewed change.

### 2026-10-29 — Dependency alerts are fixed by the agent, every session
Open Dependabot alerts and their pull requests are handled by the working agent at
the start of every session, without being asked: critical and high before planned
work, medium and low in the same session when a patch or minor bump fixes them,
one pull request at a time with every required check green. Major-version bumps
become their own work package. Merging such fixes is approved standing; deploying
a runtime fix still needs the session's deploy approval. Rules: the delivery
skill's Dependencies section.

### 2026-10-29 — Session prompts and working notes are kept private
Next-session prompts and other session working notes live in a private
repository, not in this one. This repository carries the delivery process, the
checks, and this decision log.

### 2026-10-29 — Claude Code hooks are committed; permissions stay personal
`.claude/settings.json` is committed and holds hooks only. Each developer's
permissions and plugins live in the gitignored `.claude/settings.local.json`.

### 2026-10-29 — One delivery process, carried into every next-session prompt
Every work package follows `.claude/skills/citevyn-delivery/SKILL.md`. Each
session ends with a complete next-session prompt that names it (kept privately),
and with a cleanup of what the work created.

### 2026-10-29 — Agents fan out across eight lenses; one writer per worktree
Work packages run as a fan of agents across eight lenses (planning, execution,
testing, PR review, security, availability, phone users, persona testers),
sized to the package. Many agents may read in parallel; only one writes in a
given worktree.

### 2026-10-28 — Environment files are documented, and deleted only after a full check
`docs/ENVIRONMENT_FILES.md` (#557) says which file each command reads. A file is
deleted only after the four checks in that document show nothing uses it.

### 2026-10-28 — Account deletion: wording approved, and no manual steps
The Privacy page text for "Delete account" and the 60-day list of deleted
account ids is approved (#551). Deleted accounts stay deleted after a restore,
and old backups prune themselves, with no manual step (#546, #547, #548).
Backups are kept for 30 days.

### 2026-10-28 — 14-day refund window; the legal pages go live
A first Pro payment can be refunded within 14 days, once per customer. The
Terms, Privacy and Refunds pages were published (#544).

### 2026-09-28 — Digest sign-up wording approved; Codex pages added to the watcher
The digest sign-up text is approved as drafted, and the Codex documentation
pages are watched for changes (#528).

### 2026-09-27 — History retention is a setting; the scheduled jobs run one at a time
How long conversation history is kept is configurable. The watcher, alerts and
digest jobs are scheduled in that order.

### 2026-09-27 — "What changed" alerts fetch vendor pages live
Alerts compare freshly fetched vendor pages, rather than waiting for an index
promotion.

### 2026-09-26 — Memberships: a subscription binds to one account once
After that, its status follows the payment provider.

---

## Earlier decisions

Decisions from before 2026-09-26 are recorded in their issues and ADRs, for
example full user accounts (#270, ADR-0004) and the Pro plan (#488, ADR-0005).
