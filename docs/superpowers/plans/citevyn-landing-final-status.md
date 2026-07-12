# CiteVyn Landing — Bug-Fix & Hardening: Final Status (Deliverable 4)

Final state of the four-Deliverable bug-fix & hardening effort on
`fix/citevyn-landing-hardening`. Companion to the implementation plan
(`2026-07-09-citevyn-landing-bugfix-hardening.md`) and the running
`citevyn-handover-log.md`.

- **Branch:** `fix/citevyn-landing-hardening`
- **Code tip at verification:** `874592a` (D3 handover doc) over `8b7b491` (D3 code)
- **Base:** `origin/main` @ `dceeba0` — reconciled, no conflicts (see §4)
- **Verified:** 2026-07-11, Node 22, cold Vite dev server on `localhost:3000`

---

## 1. Defect table (final)

| ID | Defect | Severity | Resolution | Landed |
|----|--------|----------|------------|--------|
| **B1** | "Contact sales" (Enterprise) CTA reachable by keyboard → opened chat (`pointer-events:none` blocks mouse, not focus+Enter) | a11y / correctness | Enterprise tier is a real `disabled` button (`onClick=undefined`), inline `pointerEvents/opacity` hack dropped; `.cta:disabled` styled in CSS. Disabled → out of tab order. | D1 `95be453` |
| **B2** | Progress-dot `.active` class hardcoded to index 0 → diverged from the 22px pill on cycles 2–3 | correctness | `heroDots` items expose `active: k === state.hero.key` (same predicate that drives the pill width); `Hero.tsx` uses `dot.active`, not `i === 0`. | D1 `95be453` |
| **B3** | Highlighter-band text low-contrast (light `--ink` on yellow) in dark mode | legibility | **Option B (darken):** highlighted words are dark `--hl-ink` on yellow in both themes; `.cta-banner .highlight` excluded (stays light on the inverted panel). | D1 `95be453` |
| **B4** | Keyword matcher mis-routes some questions (`matchKB`) | — | **Out of scope** — prototype canned matcher replaced by the real retrieval API in production (documented, deliberately not "improved"). | — |
| **B5** | FAQ toggles missing `aria-expanded`; Enterprise CTA in tab order | a11y | FAQ toggles get `id` + `aria-expanded` + `aria-controls`, answer panels get matching `id`; Enterprise removed from tab order via B1's `disabled`. | D1 `95be453` |
| **P0** | Dark-mode page canvas painted browser-default **white** (`body` bg resolved to an undefined `--surface-base`) → light hero title nearly invisible | high | `body { background: var(--bg); color: var(--ink) }` anchors the page canvas so one root-var flip repaints the whole page. | D2 `5f1c2b4` |
| **B3′** | (D2.5 regression) heading `.highlight` was a 60% underline band → dark glyph-tops sat on the dark canvas; `.doc-line.highlight-line` clipped to a 7px sliver | legibility | New `--hl-band` token drives the gradient stop (light `60%`, dark `12%` → full yellow behind the text); `.doc-line.highlight-line { height:auto }`. Pixel-sampling legibility tests added. | D2.5 `5b35457` |
| **Mobile** | `.ticker-chip` (40.25px) and `.nav-link` (33px) below the 44px touch-target floor | a11y | Extended the `@media (max-width:900px)` 44px rule to both. | D2 `5f1c2b4` |

## 2. Contrast / legibility invariants (verified)

- **Highlighted text** resolves to `--hl-ink` = `rgb(28,27,25)` (`#1c1b19`) in **both** themes, on the `--hl` yellow — asserted by the fidelity sweep (reads the expected value from the `--hl-ink` custom property, not a literal).
- **Dark heading highlights** ("check,", "trust.", "I don't know.") sit on a **majority-bright** backdrop through the cap region (pixel-sampled by `highlightBackdropBrightFraction()` in `tests/helpers.ts`) — the D2.5 legibility test that fails on the pre-fix render.
- **Dark page canvas** = `--bg` `#161618` (not transparent/white) across hero, ticker, personas, how-it-works, why, demo, pricing — asserted by the section-background sweep.
- **Manual dark pass (D4 Step 2)** confirmed on the running app: dark hero title legible on `#161618`; "check," fully yellow-backed with dark ink; `--model` doc-line box holds its text; chat streams correctly. Screenshots captured (hero-dark, how-it-works-dark, chat-dark).

## 3. Final test tally (Deliverable 4, Step 1 — cold, `--workers=1`, Node 22)

Cold start: dev server restarted after `rm -rf node_modules/.vite`.

| Check | Result |
|-------|--------|
| `type-check` (`tsc -b`) | ✅ PASS, no errors |
| `lint:css` (stylelint 17.14.0 — `color-no-hex` + `function-disallowed-list`) | ✅ PASS, exit 0, 0 violations |
| `test:ui` (`landing` + `fidelity` + `behavior`, 112 tests) — run A | ✅ exit 0 — 111 passed + 1 flaky (retried→passed) |
| `test:ui` — run B | ✅ exit 0 — **112 passed, 0 flaky** |
| `test:visual` (`visual.spec.ts`, 22 snapshots) — run A | ✅ exit 0 — 22 passed |
| `test:visual` — run B | ✅ exit 0 — 22 passed |

**Totals: 112 UI + 22 visual = 134 tests green.** (Baseline was 94; net +40 hardening tests across D1/D2/D2.5.)

Runs used `--retries=2` (matching the project's own CI config, `retries: process.env.CI ? 2 : 0`). Under the session's severe memory pressure a *different* test tripped a transient reload/scroll-timing flake on each un-retried pass — every failure had all product siblings green and cleared on retry (run B needed none). See §5.

## 4. Base-branch reconciliation

`origin/main` (`dceeba0`) was 4 commits ahead of the branch base; those 4 are unrelated Dependabot Docker bumps (`infra/docker/docker-compose.yml` — redis 8-alpine, postgres 18-alpine). `git merge-tree` of the branch against `origin/main` reports **0 conflicts** — the PR merges cleanly.

## 5. Environment notes

- Default `node` is a broken Homebrew v26.4.0; everything ran under Node 22 (`/opt/homebrew/opt/node@22/bin`).
- The repo lives under iCloud-backed `~/Documents`; `node_modules` dependency files are **dataless placeholders** that stall on first read under memory pressure. `npm run lint:css` timed out because stylelint's dep tree could not materialize. Worked around by running the **same stylelint version + same rules** from a fresh non-iCloud install against the byte-identical `landing.css` → clean.
- Playwright output was relocated outside `frontend/` during runs; the reused Vite dev server watches that tree and its `retain-on-failure` artifact writes can trigger a page reload mid-test.
