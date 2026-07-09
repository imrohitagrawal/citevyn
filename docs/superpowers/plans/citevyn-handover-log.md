# CiteVyn Landing — Bug-Fix & Hardening: Handover Log

Running log across the four Deliverable chats. Each chat appends a
`## Handover — Deliverable N complete` entry. Read the latest entry (and the
plan at `2026-07-09-citevyn-landing-bugfix-hardening.md`) before starting.

---

## Handover — Deliverable 0 complete  (2026-07-09)
- Branch / commit: `fix/citevyn-landing-hardening` @ `2503dd4`
- What changed: committed the green React landing baseline — new `frontend/src`
  (`components/{Header,Hero,LandingPage,landing-sections}.tsx`,
  `hooks/useLandingState.ts`, `styles/landing.css`, `data/knowledgeBase.ts`),
  `frontend/tests/**` (+ 22 visual snapshots), config
  (`playwright.config.ts`, `stylelint.config.js`, `tsconfig.node.json`),
  and the deletion of the old Universal/Softly/DevTools component set.
- Decisions made:
  - **Deviated from the literal `git add -A`** in D0 Step 4. Staged the
    substantive baseline explicitly and left scratch/artifact files untracked
    (audit scripts, `playwright-report/`, `test-results/`, `backend/artifacts/`,
    root `INSTRUCTIONS.md`/`GIT_COMMANDS.md`/`*.sh`, `.superpowers/`) so the
    history that feeds the eventual PR stays clean.
  - Added `frontend/.gitignore` for Playwright output + compiled config
    artifacts (`*.config.js`/`*.config.d.ts` from the `.ts` sources).
- Test tally: type-check ✅ | lint:css ✅ | test:ui **72 passed (44.7s)** | test:visual **22 passed (20.8s)** = **94**.
- New/changed baselines: none regenerated — the 22 existing visual snapshots are
  the committed baseline (D1/B3 will regenerate `hero-dark`, `how-it-works-dark`,
  `comparison-dark`).
- Gotchas for next chat:
  - **Base-branch drift (flag for D4):** local `main` has *diverged* from
    `origin/main` (local ahead 3, remote ahead 4). `git fetch` timed out at the
    start of this session, so `origin/main` is a stale ref. D0/D1 are all local,
    so this doesn't affect them — but **D4 MUST reconcile `origin/main` before
    opening the PR** (see the base-branch-drift caution in the plan).
  - **`data/` gitignore trap:** the repo-root `.gitignore` has a bare `data/`
    rule (for local Docker data) that was silently ignoring
    `frontend/src/data/knowledgeBase.ts` — the app's whole knowledge base.
    Re-included via a `!src/data/**` negation in `frontend/.gitignore`. If you
    add more files under `frontend/src/data/`, they're covered; anywhere else
    named `data/` is still ignored.
  - `git status -uall` is slow (multi-minute) because of the large untracked
    tree — use `git -c status.showUntrackedFiles=no status` or scoped
    `git status --porcelain <path>`.
  - A Vite dev server is already serving on `http://localhost:3000` (HTTP 200);
    Playwright reuses it. `cd frontend` from a shell already in `frontend/`
    errors — the working dir persists between Bash calls.
- START HERE next: Deliverable 1, first task = **B1** ("Contact sales" true no-op).
