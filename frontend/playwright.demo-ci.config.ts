/**
 * Demo-mode Playwright config for CI (#311).
 *
 * WHY A SEPARATE CONFIG
 * ---------------------
 * The demo suite is the one that should have caught #302, but no CI job ran it,
 * so 39 of its runs were failing on `main` unnoticed. It cannot simply be handed
 * to the default config on a Linux runner, because `tests/visual.spec.ts` is a
 * pixel suite: it compares screenshots against committed per-platform baselines,
 * and a screenshot job is only worth running with its browser pinned.
 *
 * So CI runs everything EXCEPT the visual specs here, and runs the visual specs
 * on their own, through `playwright.visual-ci.config.ts`, inside Playwright's
 * own container image.
 *
 * WHAT #325 CHANGED, BECAUSE THIS COMMENT USED TO SAY THE OPPOSITE
 * ---------------------------------------------------------------
 * This block used to read: "Generating Linux baselines is tracked separately;
 * until then the visual specs stay a local/darwin tool and this config is honest
 * about that rather than pretending they ran." That was true while
 * `tests/visual.spec.ts-snapshots/` held only `*-chromium-darwin.png` — on
 * `ubuntu-latest` every screenshot would have failed as "snapshot missing".
 *
 * That reasoning no longer holds. The `visual-e2e` job in
 * `.github/workflows/frontend.yml` runs these specs against `*-chromium-linux.png`
 * baselines on every pull request, so the exclusion below no longer means "these
 * tests run nowhere". It means "these tests do not run in the REQUIRED job" — a
 * different and deliberate claim: `visual-e2e` is advisory, so a font-rendering
 * shift prompts someone to look at a diff image instead of blocking every merge.
 *
 * Both halves are in place: the wiring and the `-linux` pixels. `visual-e2e`
 * runs green against them, verified in the container against this tree.
 *
 * The two configs are an exact partition of what `playwright.config.ts` selects.
 * Measured 2026-09-16 with `--list`: 230 here + 22 there = 252. Both halves are
 * asserted numerically, by the two jobs' "Guard against a vacuous selection"
 * steps. Deleting either assertion re-opens a hole the other cannot see.
 *
 * `testIgnore` is used instead of a `--grep-invert "visual regression"` on the
 * command line on purpose: the grep form silently stops excluding anything if
 * someone renames the describe block, and a suite that quietly starts running
 * screenshot comparisons inside the required job fails for a reason nobody will
 * connect to this.
 */
import base from "./playwright.config";
import { defineConfig } from "@playwright/test";

export default defineConfig({
  ...base,
  testIgnore: /visual\.spec\.ts$/,
});
