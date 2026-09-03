/**
 * Demo-mode Playwright config for CI (#311).
 *
 * WHY A SEPARATE CONFIG
 * ---------------------
 * The demo suite is the one that should have caught #302, but no CI job ran it,
 * so 39 of its runs were failing on `main` unnoticed. It cannot simply be handed
 * to the default config on a Linux runner, because `tests/visual.spec.ts` has
 * only `-darwin` baselines -- every screenshot would fail with "snapshot missing"
 * on ubuntu-latest.
 *
 * So CI runs everything EXCEPT the visual specs. Generating Linux baselines is
 * tracked separately; until then the visual specs stay a local/darwin tool and
 * this config is honest about that rather than pretending they ran.
 *
 * `testIgnore` is used instead of a `--grep-invert "visual regression"` on the
 * command line on purpose: the grep form silently stops excluding anything if
 * someone renames the describe block, and a suite that quietly starts running
 * baseline-less screenshot tests fails for a reason nobody will connect to this.
 */
import base from "./playwright.config";
import { defineConfig } from "@playwright/test";

export default defineConfig({
  ...base,
  testIgnore: /visual\.spec\.ts$/,
});
