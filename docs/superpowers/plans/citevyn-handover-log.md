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

---

## Handover — Deliverable 1 complete  (2026-07-09)
- Branch / commit: `fix/citevyn-landing-hardening` @ `95be453`
- What changed (all TDD — failing test first):
  - `src/components/landing-sections.tsx` — **B1**: Enterprise CTA is a real
    `disabled` button (`onClick=undefined`), dropped the `pointerEvents/opacity`
    inline hack. **B5**: FAQ toggles get `id` + `aria-expanded` + `aria-controls`;
    answer panel gets a matching `id`.
  - `src/components/Hero.tsx` — **B2**: progress dot uses `dot.active` instead of
    hardcoded `i === 0`; `heroDots` prop type gains `active: boolean`.
  - `src/hooks/useLandingState.ts` — **B2**: `heroDots` items expose
    `active: k === state.hero.key` (same predicate that drives the 22px pill).
  - `src/styles/landing.css` — **B1**: `.cta:disabled` style. **B3**: added
    `.highlight, .highlight-phrase` to the `color: var(--hl-ink)` group;
    `.cta-banner .highlight { color: var(--bg) }` exception; `.highlight-phrase`
    gradient stop now driven by `var(--hl-phrase-band)`.
  - `src/styles/tokens.css` — **B3**: new `--hl-phrase-band` token (light `62%`,
    dark `12%`) in all three theme blocks (`:root`/light, `[data-theme=dark]`,
    and the `prefers-color-scheme: dark` `:root:not([data-theme])` block — also
    added `--hl-ink` to that last block for completeness).
  - `tests/behavior.spec.ts` — new: B1 keyboard-inert, B2 progress pill+active,
    B5 FAQ aria-expanded/controls, B5 Enterprise-not-focusable.
  - `tests/fidelity.spec.ts` — new: B3 highlighter dark-ink both themes + CTA
    stays light (both-theme loop).
- Decisions made:
  - **B3 = Option B (darken), as directed.** Highlighted words read dark-on-yellow
    (real-highlighter look) in dark mode too. The design source-of-truth shows the
    CTA banner highlight is deliberately `color:var(--bg)` (light, on the inverted
    dark panel), so I **excluded** `.cta-banner .highlight` from the darkening —
    without that exception, blanket-darkening would have made "citing." dark-on-dark.
  - **Mandatory legibility verify (B3 Step 3): PASSED all four spots** (hero
    "check,", why "I don't know.", the ~14.5px `.highlight-phrase`, CTA "citing.").
    The small `.highlight-phrase` on the dark `.compare-card` surface was
    illegible with the original 38% band (dark glyph-tops on dark card), so I
    raised its dark-mode yellow coverage to ~88% via the `--hl-phrase-band` token.
    Hero/why `.highlight` kept the approved 60% band (large font → legible).
    Screenshots were taken from the running dev server and eyeballed.
  - **Code review (general-purpose reviewer): verdict "Ready to merge: Yes"**, all
    findings Minor. Acted on one: dropped the extra `role="region"`/`aria-labelledby`
    I'd added to FAQ answers (non-idiomatic landmark clutter per WAI-ARIA APG;
    also beyond what B5 asked for). Left the conditional `aria-controls` target
    (idiomatic alt = always-render + `hidden`, but that breaks the existing
    `.faq-answer` count===1 tests and exceeds B5 scope) — see gotcha below.
- Test tally: type-check ✅ | lint:css ✅ | test:ui **78 passed (2.9m, --workers=1)** | test:visual **22 passed (12.1s)**. (Net +6 UI tests over the 72 baseline; total now 100.)
- New/changed baselines: regenerated 3 dark/affected visual snapshots and eyeballed each —
  - `hero-dark` (hero "check," highlight text light→dark),
  - `comparison-dark` (why highlight + `.highlight-phrase` darkened, phrase band fuller),
  - `comparison-light` (`.highlight-phrase` was inheriting `--muted`; now dark `--hl-ink` — a small, correct light-mode improvement).
  `how-it-works-dark` did NOT change (it has no `.highlight`/`.highlight-phrase`, only `.doc-line.highlight-line` which was already `--hl-ink`), so I did not regenerate it despite the plan listing it.
- Gotchas for next chat:
  - **Marquee hover test flakes under default parallelism.** `behavior.spec.ts:100`
    ("marquee animates … pauses on hover") intermittently fails with 5 workers
    (hover lands on a moving gap and the `expect.poll` never re-hovers). It passes
    in isolation, with `--repeat-each`, and reliably under `--workers=1` (that's
    why the tally above uses `--workers=1`). **Pre-existing**, not caused by D1
    (nothing here touches the ticker). Good candidate for D2 to harden (re-hover
    inside the poll, or park the mouse on a stable point).
  - **Pre-existing: dark mode renders on a ~white page canvas.** The reviewer found
    `document.body` has no `--bg` background in dark mode (only `#faq`/`footer`/
    `.cta-banner` set their own bg), so the hero/why sit on white and the light
    `--ink` title text is barely visible in dark. Orthogonal to D1 and unchanged by
    it (HEAD's `hero-dark` baseline was already white). **D2's "full section-background
    sweep on theme flip" task should catch/own this** — flagging so it isn't mistaken
    for a B3 regression.
  - **Base-branch drift still unresolved (for D4).** Local `main` diverged from
    `origin/main`; `git fetch` timed out this session. D4 MUST reconcile before
    opening the PR.
  - Working dir persists between Bash calls and a bare `cd frontend` drifts pwd to
    repo root after any command that `cd`s to root — use absolute paths. Run
    Playwright via `frontend/node_modules/.bin/playwright` (a stray global
    `npx playwright` hit a "test.beforeEach() unexpected" version mismatch).
    Watch for stale `.git/index.lock` (removed one dated 5 days old this session).
- START HERE next: Deliverable 2 (test-thoroughness hardening), first task =
  **Hero card `min-height:322`** assertion. Do NOT change production code except
  where a test uncovers a genuine bug.
