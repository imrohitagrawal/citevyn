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

---

## Handover — Deliverable 2 complete  (2026-07-09)
- Branch / commit: `fix/citevyn-landing-hardening` @ `5f1c2b4` (this handover note is a
  follow-up commit on top, mirroring the D1 pattern)
- What changed (all TDD — failing/target test first):
  - **PRODUCTION (only 2 edits, both a genuine bug a test uncovered):**
    `src/styles/landing.css`:
    - **P0 fix** — `body { background: var(--bg); color: var(--ink) }`. Root cause:
      `reset.css` set `body { background: var(--surface-base) }`, but `--surface-base`
      is defined ONLY under `:root[data-style="softly|devtools|devtools-alt"]` — it is
      undefined for the default `[data-theme="light|dark"]`, so `body` painted
      transparent → every bg-less section (hero, ticker gaps, personas, how, why, demo,
      pricing) sat on the browser-default white, making the light `--ink` hero title
      nearly invisible in dark mode. Anchored the page canvas to the themed
      `--bg`/`--ink` so one root-var flip repaints the whole page. Token-clean (no hex).
    - **Mobile a11y fix** — extended the `@media (max-width:900px)` 44px touch-target
      rule to `.ticker-chip` (measured 40.25px) and `.nav-link` (measured 33px; it's an
      inline `<a>`, so it also got `display:inline-flex; align-items:center` to honor
      `min-height`). Uncovered by the new "44px touch-target floor" sweep. Others already
      passed (`.faq-toggle` 65.5, `.demo-question` 50.5, `.suggestion-btn` 47).
  - **TESTS:**
    - `tests/fidelity.spec.ts` — +2 tests × both themes (=+4): the P0 fail-first
      "themed page canvas" section-background sweep (asserts `body` bg == `--bg` and
      every region resolves to a themed bg via ancestor-walk, never white), and the
      ticker 80px edge-fade masks (`::before`/`::after` width 80px + gradient to `--bg`).
    - `tests/behavior.spec.ts` — +26 tests: mobile touch-target sweep; hero
      `.card-content` min-height≥322; placeholder ~3.2s cadence bracket; streaming
      incremental + sources-only-after-caret ordering; duplicate pulse iteration-count
      `3` + re-fire on 2nd duplicate; marquee `infinite` + first-8-labels == last-8;
      persona/step/feature card hover-lift (settled translateY<0, reverts); 4 chat
      suggestions + 6 persona buttons each send their own label (parametrized);
      send-button click path + empty no-op; refusal exact badge text + non-empty body +
      case/whitespace-insensitive dedup; Get-Pro-twice single bubble; reduced-motion
      full sweep (shake/pulse/caret disabled); dark-theme parity (demo refusal amber+0
      sources, hero empty-ask amber border, sourced demo source cards readable, Get-Pro
      flow); timer-cleanup (ChatView unmount mid-stream → no console/page errors).
    - `tests/landing.spec.ts` — flake fixes only (see below).
- Decisions made:
  - **P1 flakes fixed at the source (now green under DEFAULT parallelism, not just
    `--workers=1`):**
    - *Both marquee-hover tests* (`behavior.spec.ts` + `landing.spec.ts`) — root cause:
      `.ticker-track` is `width:max-content` + `translateX`-animated, so its bounding box
      runs far off-screen left and a `position`-relative hover lands nowhere. Fixed by
      hovering the STATIONARY `.ticker-strip` (the track fills it, so `.ticker-track:hover`
      still matches).
    - *progress-pill* — replaced the fixed `waitForTimeout(400)` with a poll of the
      settled invariant (exactly one dot ≥20px wide, exactly one `.active`, same dot).
    - *`[dark]` heading-contrast* (`landing.spec.ts`) — replaced the 200ms sleep after
      the theme toggle with a poll on the root `--bg` flipping to `#161618`.
  - **Timer-cleanup: Playwright instead of Vitest.** jsdom hangs in this sandbox (a
    trivial `--environment=jsdom` smoke test produced zero output for >2min; node-env
    passed in 1.8s), so a Vitest+renderHook unit test was not runnable here. The plan
    explicitly allowed "OR a landing→chat→back navigation loop"; implemented that
    (unmount ChatView mid-stream, assert no console/page errors — the `if (list)` guards
    in `streamBot`/`flashExisting` keep the orphaned timers from throwing). Removed the
    vitest.config.ts + `src/hooks/useLandingState.test.ts` I had drafted.
  - **Code review (general-purpose reviewer): "Ready to merge — Yes."** All findings
    Minor (one redundant-but-harmless `color:var(--ink)` line — kept, the plan directs
    it and it's self-documenting; two low-risk timing brackets — accepted). Reviewer
    independently confirmed the P0 root cause, token-cleanliness, and that all three
    flake fixes are correct root-cause fixes.
- Test tally: type-check ✅ | lint:css ✅ | test:ui **108 passed (3.3m, DEFAULT
  parallelism — isolated run)** | test:visual **22 passed (33.7s, isolated)**.
  (Net +30 test:ui over D1's 78: +4 fidelity, +26 behavior.)
- New/changed baselines: regenerated **9 dark** visual snapshots (all legit — the P0 fix
  repaints the dark page canvas #161618) and eyeballed each: `hero-dark`, `ticker-dark`
  (fully masked → the change was only the 1px strip border edge), `personas-dark`,
  `how-it-works-dark`, `comparison-dark`, `demo-dark`, `pricing-dark`, `faq-dark`
  (sliver of sticky-header backdrop at top), `chat-empty-dark`. **Light baselines were
  byte-identical** (unchanged) — `git status` flagged only the 9 dark PNGs.
- Gotchas for next chat:
  - **Do NOT `pkill -f esbuild`** — Vite's dev server spawns an esbuild child; killing it
    leaves the server serving `index.html` (HTTP 200) but unable to transform JS, so the
    React app never mounts and EVERY test fails at `gotoApp` waiting for `.theme-toggle`.
    If that happens, `lsof -ti tcp:3000 | xargs kill -9` and restart `npm run dev`.
  - **jsdom hangs here** — don't add Vitest/jsdom unit tests expecting them to run; use
    Playwright. `npm test` (`vitest run`) with no config would also try to run the
    `*.spec.ts` playwright files under vitest — leave it alone.
  - **Do NOT run `test:ui` and `test:visual` concurrently / back-to-back on 5 workers** —
    machine contention caused a one-off `comparison-light` "two consecutive stable
    screenshots" flake (0.03 ratio) and a transient empty-Ask amber-border miss. Both
    passed clean when run isolated. Run heavy suites one at a time.
  - Playwright's `| tail` / `--reporter=line` buffer output until the run ends, so
    interim reads of the piped output file look empty — wait for completion.
- START HERE next: Deliverable 3 (code-quality / taste refactors), first task = **H1
  (`askHero` self-contained)**. The now-hardened suite is the regression net — run the
  FULL suite after EACH refactor task and change NO tests to accommodate a refactor.

---

## Handover — Deliverable 2.5 complete  (2026-07-10)
- Branch / commit: `fix/citevyn-landing-hardening` @ **`5b35457`**
  (D2.5 = the B3 highlight-legibility HOTFIX; base was `4137f4a`). This handover note
  is a follow-up commit on top, mirroring the D1/D2 pattern.
- What changed (all TDD — failing legibility tests written & confirmed RED first):
  - `src/styles/tokens.css` — new **`--hl-band`** token in all three theme blocks
    (light `60%`, dark `12%`, `prefers-color-scheme:dark` `12%`). Mirrors the existing
    `--hl-phrase-band` idea for the large `.highlight` headings.
  - `src/styles/landing.css`:
    - **Bug 1** — `.highlight` gradient stop now `var(--hl-band)` instead of a hardcoded
      `60%`, so in DARK mode the yellow backs the FULL text (caps on yellow, not on the
      dark canvas). Light keeps the faithful 60% underline band → light appearance
      unchanged.
    - `.cta-banner .highlight` gets `--hl-band: 60%` so the inverted CTA "citing."
      keeps its 60% band in BOTH themes (its text is light `var(--bg)`, never had the
      dark-on-dark problem — excluded from the fuller dark fill).
    - **Bug 2** — `.doc-line.highlight-line { height: auto }` (scoped) so the yellow box
      grows to hold "Use --model to pick a model per run." instead of clipping to the 7px
      skeleton-bar height. The other three grey bars stay 7px (100/84/66).
  - `tests/helpers.ts` — new `highlightBackdropBrightFraction()` helper: element-screenshots
    a highlight span, decodes it via the pngjs Playwright already bundles
    (`playwright-core/lib/utilsBundle.js`, loaded through `createRequire` since the specs
    are ESM), and samples a pixel row through the FIRST-line cap region, returning the
    fraction of "bright" (luma ≥ 100) backdrop pixels.
  - `tests/fidelity.spec.ts` — +2 tests × both themes (=+4):
    1. **Highlight-backdrop legibility** — for `.hero-title .highlight`, `#why .highlight`,
       `.highlight-phrase`, asserts the cap-row backdrop is majority-bright (dark ink needs
       a bright backdrop). Confirmed RED on old code: dark hero/why cap-row bright fraction
       = 0 (dark-on-dark); light already passed (light canvas is bright → light untouched).
    2. **doc-line height** — `.doc-line.highlight-line` bounding height ≥ its font-size, and
       the three plain skeleton bars stay `7px`. RED on old code (7px < 11px).
- Decisions made:
  - **Theme-aware legibility, not "always yellow".** The real invariant is "dark `--hl-ink`
    text must sit on a BRIGHT backdrop." In light mode the page canvas is already bright, so
    light keeps the 60% band (design unchanged, as the plan requires); only dark needed the
    fuller fill. This is exactly why the test fails in dark only on old code.
  - **Pixel-sampling is the deliverable.** The old tests only checked the text *color value*
    and the baselines were captured from the broken render — that's how these bugs hid. The
    new test looks at actual rendered pixels behind the glyphs.
  - Both bugs **visually verified** with 3× zoomed element screenshots in dark AND light
    (hero "check,", why "I don't know.", the `.highlight-phrase`, and the doc-line box) —
    all legible; light hero highlight keeps the faithful underline look.
- Test tally: type-check ✅ | lint:css ✅ | test:ui **112 passed (1.3m, DEFAULT parallelism)**
  | test:visual **22 passed** (all under Node 22 — see env gotcha). Net +4 test:ui over D2's
  108 (the 2 new fidelity tests × 2 themes).
- New/changed baselines (regenerated under Node 22 + a clean `npm ci`, each eyeballed):
  - `hero-dark` — "check," now backed by full yellow (was a 60% band leaving caps on dark).
  - `comparison-dark` — "#why .highlight" + `.highlight-phrase` fully yellow-backed.
  - `how-it-works-dark` / `how-it-works-light` — doc-line box now holds its text (Bug 2).
    NOTE: Playwright's `--update-snapshots` will NOT overwrite a baseline whose diff is under
    `maxDiffPixelRatio` (0.02) — the hero/why highlight is a small fraction of the section,
    so I had to `rm` `hero-dark`/`comparison-dark` first to force regeneration from the fixed
    render (else the broken baseline persists silently — the same trap that hid these bugs).
  - `demo-dark` / `demo-light` — **NOT a D2.5 CSS change** (the demo section contains zero
    `.highlight`/`.highlight-phrase`/`.doc-line` — verified). These drifted by **1px height**
    (598→599) purely from the `npm ci` reinstall's sub-pixel render delta; regenerated so the
    suite is self-consistent under the current toolchain. Content confirmed identical/correct.
- Gotchas for next chat (IMPORTANT — environment was badly degraded this session):
  - **Commit landed as `5b35457`** (10 files: landing.css, tokens.css, fidelity.spec.ts,
    helpers.ts, + the 6 baseline PNGs above). It was blocked for a long stretch by
    `fatal: mmap failed: Operation timed out` while the machine thrashed (`vm.swapusage`
    peaked ~20GB used / ~16MB RAM free — mostly VS Code + system, not our processes); it
    went through once swap use dropped to ~12GB. **Watch for this**: under heavy memory
    pressure git mmap and vite startup both stall/time out — free RAM (trim VS Code) before
    running heavy git/test/dev commands. The pre-existing `M docs/.../2026-07-09-...bugfix-
    hardening.md` edit and the unrelated `D .agents/skills/e2e-testing-patterns/references/
    details.md` deletion were deliberately left OUT of this commit (not ours).
  - **NOT pushed / no PR** — correct per plan: all deliverables land on
    `fix/citevyn-landing-hardening`; the single PR to `main` is Deliverable 4's job.
  - **Node toolchain was broken & repaired.** The machine's default `node` is a Homebrew
    **v26.4.0** build whose ESM loader intermittently throws `ERR_INVALID_PACKAGE_CONFIG`
    (rollup/vite/react-dom) under load, AND `node_modules` had a corrupted `@babel/core`
    (`_debug is not a function`) so `@vitejs/plugin-react` failed with
    `babel$1.transformAsync is not a function` → HTTP 500 on every module → app never
    mounted (all Playwright tests timed out at `.theme-toggle`). **Fix applied:** installed
    **Node 22 LTS** (`brew install node@22`, non-invasive — default node NOT relinked) and
    ran **`npm ci`** to repair the tree. Everything below assumes Node 22:
    `export PATH="/opt/homebrew/opt/node@22/bin:$PATH"` before `npm run dev` / playwright.
  - **Dev server:** the original long-running `npm run dev` was killed (with the user's
    explicit consent) to clear a corrupt `.vite` optimize cache. I restarted it under Node 22
    via a fifo-stdin launcher (the harness's background `< /dev/null` makes vite quit on EOF;
    a held-open fifo keeps it alive) — script at
    `<session scratchpad>/launch_vite.sh`. I then **stopped it again** to free memory for the
    git commit, so **no dev server is currently running** — restart with Node 22 before the
    next Playwright run. `node node_modules/vite/bin/vite.js` under Node 22 serves fine.
  - Standalone `.mjs` helper scripts must live INSIDE `frontend/` (module resolution) and be
    run with the Node 22 binary; under the broken v26.4.0 they crash on import.
- START HERE next: **first, complete the blocked commit above** (record its SHA), then
  Deliverable 3 (code-quality / taste refactors), first task = **H1 (`askHero`
  self-contained)**. Run the FULL suite under Node 22 after each refactor; change NO tests.

---

## Handover — Deliverable 3 complete  (2026-07-10)
- Branch / commit: fix/citevyn-landing-hardening @ **8b7b491** (`refactor(frontend): self-contained
  handlers, single dedup guard, typed timers, view-owned scroll`). Started from D2.5 = 405b581.
  This handover-log entry lands as a separate `docs(frontend)` commit above it (repo pattern).
- What changed (9 files, behavior-preserving):
  - `src/hooks/useLandingState.ts` — **H1** `askHero` is self-contained (calls `enterChat(q)` on
    valid input, returns void; focus+nudge on empty), moved AFTER `enterChat` for the TDZ; hook
    `onHeroKey` calls `askHero()`. **H2** `getPro` = one-liner `enterChat("What do I get with
    CiteVyn Pro?")` (bespoke guard + streamBot/flashExisting dup deleted). **M1** imports the
    single `PLACEHOLDERS`, action renamed `ADVANCE_PLACEHOLDER` (payload dropped), modulus from
    `.length`, exposes `heroPlaceholder`. **M2** `streamText`/new `timeout()`/`interval()` return a
    `Timer {stop()}`; every stored ref is `Timer|null`; cleanup is `t?.stop()`; `clearTimeout(...as
    any)` gone. **M3** extracted module-level `scrollToId(id)`. **M4** removed streamBot's three
    `#chat-list` scroll calls. Review fix: dropped the now-redundant `askHero` from the return.
  - `src/components/LandingPage.tsx` — pass hook `onHeroKey`/`onAskHero`/`heroPlaceholder` through
    (no inline nav wiring, no inline placeholder array); KB import moved to the top.
  - `src/components/ChatView.tsx` — owns autoscroll via `useEffect([messages])`; dropped the
    `chatListRef` alias + the unused optional `chatListRef?` prop.
  - `src/components/Hero.tsx` — removed the `startHeroLoop` SSR-ceremony state/effect → constant
    `min-height: 92px`; react import is now `import type * as React`.
  - `src/components/landing-sections.tsx` — Pricing behavior on the data: `action:
    "openChat"|"getPro"|null` per tier + a `handlers` map; `onClick={tier.action ?
    handlers[tier.action] : undefined} disabled={!tier.action}`.
  - `src/data/knowledgeBase.ts` — added `KB["pro"]` (byte-identical canned answer) + a `matchKB`
    rule `t.includes("citevyn pro")` (placed after the out-of-scope guard).
  - `src/styles/tokens.css` — `--hl-ink` (theme-invariant `#1c1b19`) collapsed to the `:root`
    block only; removed the identical `[data-theme="dark"]` + `prefers-color-scheme` copies.
  - `tests/fidelity.spec.ts` + `tests/helpers.ts` — new `resolveColor()` helper; the two highlight
    assertions read the expected color from the `--hl-ink` custom property (value identical to the
    old `TOKENS.light.ink` = `rgb(28,27,25)`); `loadPNG()` now guards the playwright-core pngjs
    require with clear error messages.
- Decisions made:
  - **PLACEHOLDERS single source = `knowledgeBase.ts` (imported), not a new const in the hook.**
    An identical `PLACEHOLDERS` already existed there; importing it is the DRY-est reading of M1's
    "count in one place" (avoids a third copy). Reducer references the imported `.length`.
  - **H2 needs a `matchKB` route.** `send` resolves answers via `matchKB`, so routing getPro
    through it required the `"citevyn pro"` rule — "citevyn pro" collides with none of the four-tool
    questions and is checked after the out-of-scope guard. This is a deliberate (documented)
    widening of free-typed matching, NOT the B4 out-of-scope matcher work.
  - **Kept the defensive fallbacks** `KB[hero.key] || KB["claude-code"]` and `KB[demo.key]?.sources
    ?? 0`: `hero.key`/`demo.key` are typed `string` (not a key union), so these fallbacks are what
    make the indexing total — removing them needs a key-type change (broader than a behavior-
    preserving pass). Left per the plan's escape hatch.
  - **Did NOT add `"#how .highlight"` to the D2.5 legibility sweep** — `#how` (HowItWorks) renders
    `.doc-line.highlight-line`, not a `.highlight` span (which has its own dedicated test); adding
    it would fail on `toBeVisible`.
  - **Two commits** (code `8b7b491`, then this docs entry) mirror the D1/D2/D2.5 pattern. NO Claude
    attribution footer (per commit-message-pref).
- Test tally: **type-check ✅ | test:ui 112 passed | test:visual 22 passed** — run green after EVERY
  task (H1, H2, M1, M2, M3, M4, Lows-1, Lows-2) AND after the code-review fixes, under Node 22 at
  DEFAULT parallelism (test:visual finalized with `--workers=1`, see gotcha). **NO test was changed
  to accommodate a refactor.** `lint:css` — see gotcha (green by construction, not live-run).
- New/changed baselines: **NONE** — no visual snapshot was regenerated. `test:visual` stayed at 22
  passed against the existing baselines throughout (tokens `--hl-ink` value unchanged; Hero
  `min-height` steady-state identical), confirming zero pixel drift from the refactor.
- Code review: dispatched a general-purpose senior-reviewer subagent over the working-tree diff. It
  traced H1 (nav/nudge parity), H2 (matchKB route order + dedup), M4 (messages is a fresh array per
  render → effect re-pins), M2 (all refs stopped, no raw id) — verdict **Ready to merge: Yes**, zero
  Critical/Important. Applied its two actionable Minors: dropped the redundant returned `askHero`
  and sharpened the `matchKB` "citevyn pro" comment. (Its 3rd note — the fidelity.spec edit — is the
  planned in-scope Low with an identical asserted value, so intentionally kept.)
- Gotchas for next chat (ENVIRONMENT WAS SEVERELY DEGRADED — read before D4's clean-room run):
  - **`lint:css` could NOT be live-run this session.** `stylelint` hangs/`ETIMEDOUT`s while its ESM
    dep tree loads (`node:internal/modules/esm/load` → `readFileSync` errno -60) because the repo
    lives under `~/Documents` (iCloud-backed) AND the machine was thrashing (swap pinned ~11.3 GB
    used / <70 MB RAM free, from Chrome + VS Code, not our procs). **It is green by construction:**
    `lint:css` only lints `src/styles/landing.css`, which **no D3 task touched** (the sole CSS change
    is `tokens.css`, not in the lint glob) and which was green at the 405b581 baseline. **D4 Step 1
    must run `npm run lint:css` cold** to close this out — do it when RAM is freer (trim VS
    Code/Chrome first); expect it to pass with no output.
  - **`@types/* 2` junk broke type-check at session start.** `node_modules/@types/` had macOS
    " 2" duplicate dirs (`react 2`, `react-dom 2`, `node 2`, …) → `tsc` TS2688. Removed them with
    `find node_modules/@types -maxdepth 1 -type d -name "* 2" -exec rm -rf {} +`. If type-check
    fails again with `Cannot find type definition file for '<x> 2'`, re-run that.
  - **Playwright OOM.** One `test:visual` run had all workers `SIGKILL`ed (OOM under the swap
    thrash). Re-ran with `--workers=1` → 22 passed. Under memory pressure prefer `--workers=1` for
    visual; don't run test:ui and test:visual concurrently.
  - **Node 22 still mandatory** (`export PATH="/opt/homebrew/opt/node@22/bin:$PATH"`); default node
    is the broken Homebrew v26.4.0. Dev server launched via a fifo-held-stdin script (harness
    background makes vite quit on EOF). Type-check is slow (~2–2.5 min) under the thrash but passes.
  - **NOT pushed / no PR** — correct per plan; the single PR to `main` is Deliverable 4's job.
    `main`/`origin/main` drift was NOT re-checked this session (D4 Step 4 must verify before PR).
- START HERE next: **Deliverable 4 (final verification, docs & PR)**. First task = **Step 1
  clean-room verification**: stop the dev server, `rm -rf node_modules/.vite`, restart under Node 22,
  then `type-check && lint:css && test:ui && test:visual` with `--workers=1` **twice** on a cold
  server — and this is where `lint:css` finally gets its live green (see gotcha). Then D4 Steps 2–5.
