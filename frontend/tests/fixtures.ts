/**
 * Shared Playwright fixtures for the CiteVyn UI suite.
 *
 * WHAT THIS FIXES
 * ---------------
 * `index.html` pulls Geist and JetBrains Mono from `fonts.googleapis.com` with a
 * plain `<link rel="stylesheet">`. A render-blocking stylesheet also blocks
 * `<script type="module">` from EXECUTING, so until that third-party request
 * settles the app does not mount, `#root` stays empty, and `.theme-toggle` does
 * not exist. Every navigation in this suite is exposed; `helpers.ts`'s
 * `gotoApp()` and `landing.spec.ts`'s `beforeEach` both wait on that exact
 * selector.
 *
 * Measured on this repo at f171696, same dev server, back to back:
 *
 *     fonts served normally         mount in 191-323 ms
 *     fonts stalled 40 s            mount in 40 150 ms   <-- tracks the stall 1:1
 *     fonts stubbed (this file)     mount in 108-158 ms
 *
 * That is what reddened `main` in run 34050016261. `landing.spec.ts:96` sat on a
 * blank page for exactly 30.0 s — the retained video's first 30 s are one
 * identical 2 133-byte frame — then mounted at +30.21 s, 0.2 s past the 30 s
 * `waitForSelector`, and passed on retry in 1.8 s. It was NOT that test's bug
 * and NOT a cold dev server: the server had been up 7 m 54 s and 147 tests had
 * already passed on it. The retry's own trace shows the cost plainly — the
 * `css2` request's `connect` alone was 27.6 ms of an 80.9 ms total, against
 * 5.4 ms for the whole of `localhost:3000`.
 *
 * So the fix belongs here, once, and not in any single spec.
 *
 * WHY STUB RATHER THAN LET THE FONTS LOAD
 * ---------------------------------------
 * Nothing outside `visual.spec.ts` asserts typography — there is no
 * `font-family`, `Geist` or `JetBrains` reference anywhere else under `tests/`.
 * The real bytes buy those specs nothing and cost them three third-party round
 * trips per test, ~480 per suite run, every one of which can stall.
 *
 * The stub is a real (tiny) stylesheet rather than an abort on purpose:
 * `route.abort()` surfaces as a `net::ERR_FAILED` console message, and
 * `behavior.spec.ts` has tests that fail on any console error.
 *
 * WHY visual.spec.ts DOES NOT USE THIS
 * ------------------------------------
 * Its 22 darwin baselines are pixels of real Geist at `maxDiffPixelRatio: 0.02`,
 * and falling back to `system-ui` moves far more than 2 % of the text pixels.
 * Measured, not assumed: pointing `visual.spec.ts` at this fixture and running
 * it turns **9 of its 22 snapshots red** (hero, personas, how-it-works and
 * pricing in both themes, plus light/pricing); with the real fonts it is 22/22.
 * It is excluded from the CI job by `playwright.demo-ci.config.ts` and stays a
 * local/darwin tool (#325), so it keeps the real fonts and keeps the exposure.
 * `src/test/buildGuards.test.ts` pins that carve-out to exactly that one file,
 * by parsing each spec's imports, so a NEW spec cannot quietly join it.
 *
 * WHAT THIS DOES NOT FIX
 * ----------------------
 * Real users still meet the same render-blocking link: while
 * `fonts.googleapis.com` is slow, the CiteVyn page is blank, not merely
 * unstyled. That is a production concern with a different fix (self-host the
 * two faces, or load them off the critical path — note `script-src 'self'` in
 * `backend/app/core/security_headers.py` rules out the usual
 * `onload="this.media='all'"` trick), and it is tracked separately.
 */
import { test as base, expect } from "@playwright/test";

/**
 * Both Google Fonts hosts. `fonts.googleapis.com` serves the stylesheet;
 * `fonts.gstatic.com` serves the woff2 files it names. With the stylesheet
 * stubbed there is no `@font-face` and so nothing should ever ask gstatic — it
 * is routed anyway so that a stray reference cannot reintroduce the round trip.
 */
export const GOOGLE_FONT_HOSTS = /^https:\/\/fonts\.(googleapis|gstatic)\.com\//;

/**
 * Served in place of the real stylesheet. The custom property is the handle
 * `harness.spec.ts` uses to assert, from inside the browser, that the stub is
 * really what the page applied — a timing assertion could not tell a stub from
 * a fast network, and counting requests cannot distinguish a fulfilled route
 * from a real response.
 */
export const FONT_STUB_CSS = ":root { --e2e-fonts-stubbed: 1; }\n";

export const test = base.extend({
  page: async ({ page }, use) => {
    await page.route(GOOGLE_FONT_HOSTS, (route) =>
      route.fulfill({ status: 200, contentType: "text/css", body: FONT_STUB_CSS }),
    );
    await use(page);
  },
});

export { expect };
