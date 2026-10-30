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

### 2026-10-30 — The backend reads no env file; the Makefile loads `backend/.env`
`Settings` reads only the process environment, as it already does on Fly. It
used to read `.env` from whatever folder a command ran in, so `make test` read
`backend/.env` while `make migrate`, `seed` and `demo` read a repo-root `.env`
(the root file is now retired). The development commands load `backend/.env`
through `scripts/with_env.py`, with the same parser as before and the shell
winning: `make api`, `worker`, `migrate`, `seed`, `demo`, `ci-smoke`, `eval`,
`golden`, `golden-smoke`, `test-pg`, and every backend step of `make smoke` and
`make e2e`. Commands that newly read the file: `migrate`, `seed`, `demo`,
`smoke` and `e2e` (seed and smoke now ingest with the embedder it names, and
the seed prints that name first). `make test` and coverage load no file, as in
CI. `make demo-frontend` keeps reading its token from `infra/docker/.env`: the
plan to move it assumed the local backend served that bundle, but its real
user is the compose release check (`deploy_verify.sh`). Details:
`docs/ENVIRONMENT_FILES.md`.

### 2026-10-30 — The query embedder is checked every 3 hours, riding the uptime probe
`/health/index` makes one tiny embedding call at most once every 3 hours for the
whole app, so an empty prepaid balance is noticed with no visitors and the
alert clears by itself after a top-up. It only rides the uptime probe's
existing request and never schedules its own wake-up, so the scale-to-zero
machine is not kept up longer. The interval is a setting (0 turns it off).

### 2026-10-30 — The uptime workflow keeps itself enabled; a dead-man's switch comes later
GitHub disables scheduled workflows in a public repository after 60 days with
no activity, which would stop the uptime alerts silently. The workflow
re-enables itself through the API on every scheduled run. Later, the probe
will also ping an outside dead-man's switch that emails when the pings stop;
setting up that account is the owner's step.

### 2026-10-30 — Paid model calls are for final validation only
Code changes are built and tested without paid model calls: stub providers,
the free retrieval eval, and recorded evidence. Paid calls are kept for the
final validation of a finished, reviewed change (one judged eval run) and the
production check after a deploy, with at most one live question per issue to
reproduce a bug that free evidence cannot show. Rules: the delivery skill's
Build section.

### 2026-10-30 — A review seat that returned nothing is open, and blocks
A reviewer that was stopped, failed, timed out, was refused or returned nothing
leaves its seat open. Each package keeps a seat list (every required review,
with its result or "OPEN" and why); an open seat blocks the merge and "done"
and is named in the closing status block. It is filled by the same role
another way, or taken to the owner, who may decide to go ahead with it open.
The rule lives in the delivery skill's Review section.

### 2026-10-30 — The guard is switched on before every repository is clean
The public-content guard is switched on before every repository is clean. An
existing finding blocks a change only when that change edits the file that
holds it; new findings, commit messages and pull request text are always
checked. Existing findings are cleaned in their own approved work. This
repository's pull request check uses the guard.

### 2026-10-30 — Archives the guard cannot open are blocked; other binaries are read by their text
zip, tar, gzip, bzip2 and xz files are read to their last byte, or the check
blocks. Archives the guard cannot open (7z, rar, zstd, lz4, lzip, Unix
compress, cabinet, xar) block, so their contents are never published unread. A
block names the file and says what to do: remove it, commit its contents
unpacked, repack it as zip, tar.gz or tar.xz, or keep it in a private
repository. Every other binary file (images, PDFs, videos) is checked through
its plain text, in runs of 12 or more characters: shorter runs gave false
blocks on ordinary photos, and text inside a binary's compressed parts is not
read. Both gaps are written down in the guard's documentation.

### 2026-10-30 — Text that is already public is no reason to pass the guard
The public-content guard checks every file a change adds or changes whole, and
every commit message, name and pull request title and body, with no exemption
for text that already appears somewhere public. A leak that is already public
must not be copied, kept or extended without a warning, so editing a file that
holds an old match blocks until it is cleaned. The only exemptions are the
listed public addresses, the rules' own allowlists (example domains, shared
home folders), and an address already used on commits, on author lines only.
This repository's test files were moved to example addresses first, so it holds
no match the guard would report.

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
