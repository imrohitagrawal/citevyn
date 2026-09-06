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
 *     fonts served from disk        mount in 108-158 ms
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
 * WHY IT SERVES THE REAL FONTS INSTEAD OF STUBBING THEM AWAY
 * ---------------------------------------------------------
 * The cheap version of this fix answers the font request with an empty
 * stylesheet. It removes the stall just as well, and no spec names a typeface
 * (grep: no `font-family`, `Geist` or `JetBrains` reference anywhere else under
 * `tests/`) — but "no spec names a font" is not "no spec depends on one", and
 * measuring found two places that do:
 *
 *   - `fidelity.spec.ts`'s D2.5 legibility guard samples a pixel row through the
 *     cap region of a highlighted phrase and requires a bright backdrop
 *     fraction > 0.5. It exists because dark-ink-on-dark once shipped. Measured
 *     on `.highlight-phrase` in dark mode: **0.5437 with real Geist, 0.5336 with
 *     the empty stub** — the margin above the threshold shrinks from 0.0437 to
 *     0.0336, and that was against macOS's SF Pro fallback. CI runs
 *     `ubuntu-latest`, whose fallback face is a third font again, unmeasured and
 *     unmeasurable from here.
 *   - `visual.spec.ts`'s 22 darwin baselines are pixels of real Geist at
 *     `maxDiffPixelRatio: 0.02`. Pointing it at an empty-stub fixture turns 9 of
 *     the 22 red.
 *
 * Trading a network-dependent flake for a platform-dependent pixel margin is not
 * a fix. So the fixture serves the SAME BYTES Google would have served, from
 * `tests/fonts/`: identical metrics to production on every platform, zero
 * third-party network, and `visual.spec.ts` can use this fixture like everything
 * else rather than needing a carve-out.
 *
 * WHAT #365 CHANGED, AND WHY THIS FILE IS STILL HERE
 * --------------------------------------------------
 * This block used to read "what this does not fix": real users still met the
 * render-blocking link, because the harness fix could not reach production.
 * #365 fixed that half. `index.html` no longer carries a
 * `fonts.googleapis.com` stylesheet at all — the faces are self-hosted from
 * `public/fonts/` via `src/styles/fonts.css`, and the CSP's `style-src` /
 * `font-src` dropped both Google hosts as a result.
 *
 * So on `main` today NEITHER route below ever fires. They are kept as a
 * BACKSTOP, not as live machinery: if a future change reintroduces a
 * third-party font request, these routes answer it from disk so the suite does
 * not silently become network-dependent again while someone bisects a flake.
 * What actually catches the REGRESSION is not here — it is
 * `src/test/buildGuards.test.ts`, which asserts the EMITTED `dist/index.html`
 * and the emitted CSS contain no off-origin render-blocking reference, and
 * `tests/fonts-offline.spec.ts`, which proves in a real browser that the app
 * mounts with every font request failing. Both are deliberately outside this
 * fixture: a guard that runs THROUGH the stub cannot see the production path
 * the stub is hiding.
 *
 * The .woff2 bytes moved to `public/fonts/` in #365 and are read from there, so
 * there is exactly ONE copy of them in the repo and the suite renders the same
 * face production ships. `fonts/google-fonts-latin.css` stays here: it is
 * Google's response, which is a test artifact and not something production
 * serves any more.
 */
import { test as base, expect } from "@playwright/test";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
/** Google's stylesheet response, vendored. A test artifact; production never serves it. */
const CSS_DIR = join(HERE, "fonts");
/** The .woff2 bytes — the SAME files production ships, not a second copy (#365). */
const FONT_DIR = join(HERE, "..", "public", "fonts");

/** The stylesheet host. `index.html`'s `<link>` points here. */
export const GOOGLE_FONTS_CSS = /^https:\/\/fonts\.googleapis\.com\//;
/** The font-file host, named by the `src:` URLs inside that stylesheet. */
export const GOOGLE_FONTS_FILES = /^https:\/\/fonts\.gstatic\.com\//;

/**
 * gstatic URL basename -> the vendored file that answers it.
 *
 * Only the `latin` subsets are vendored, because instrumenting a real page load
 * records exactly these two requests and no others. An unlisted basename is
 * answered with 404 rather than passed through to the network, so a new subset
 * requirement surfaces as a loud, local failure in `harness.spec.ts` instead of
 * a silent third-party round trip that can stall.
 */
const VENDORED_FONTS: Record<string, string> = {
  "gyByhwUxId8gMEwcGFWNOITd.woff2": "geist-latin.woff2",
  "tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwgknk-4.woff2":
    "jetbrains-mono-latin.woff2",
};

/**
 * The `family=` parameters `tests/fonts/google-fonts-latin.css` actually
 * covers, as `index.html` spells them.
 *
 * The stylesheet route is pinned to these for the same reason the file route is
 * pinned to two basenames. Answering ANY `fonts.googleapis.com` URL with this
 * one stylesheet is silently wrong the moment the page asks for a third family:
 * the route would hand back Geist + JetBrains Mono, the host is intercepted so
 * no third-party request escapes, the marker is still there — every guard green
 * while the suite renders a face production does not have. A 404 here turns that
 * into a named failure in `harness.spec.ts` instead.
 */
const VENDORED_FAMILIES = ["Geist:wght@400..700", "JetBrains Mono:wght@400;500"];

export const test = base.extend({
  // Overriding `context` rather than `page`: Playwright builds `page` from
  // `context`, so a context-level route covers the default page AND any extra
  // page a test opens (`context.newPage()`, a `target=_blank` popup). A
  // `page.route` would cover only the first. It does not cover a test that
  // builds its own context from the `browser` fixture — no spec does, and
  // src/test/buildGuards.test.ts records that as a known blind spot.
  context: async ({ context }, use) => {
    await context.route(GOOGLE_FONTS_CSS, (route) => {
      // `searchParams.getAll` URL-decodes, so "JetBrains+Mono" arrives as
      // "JetBrains Mono" — compare against the decoded form.
      const families = new URL(route.request().url()).searchParams.getAll("family").sort();
      if (JSON.stringify(families) !== JSON.stringify([...VENDORED_FAMILIES].sort())) {
        return route.fulfill({
          status: 404,
          contentType: "text/plain",
          body:
            `tests/fonts/ covers [${VENDORED_FAMILIES}] but the page asked for ` +
            `[${families}] — vendor the new family or drop it from index.html`,
        });
      }
      return route.fulfill({
        contentType: "text/css",
        path: join(CSS_DIR, "google-fonts-latin.css"),
      });
    });
    await context.route(GOOGLE_FONTS_FILES, (route) => {
      const name = new URL(route.request().url()).pathname.split("/").pop() ?? "";
      // `Object.hasOwn`, not a bare lookup: a basename of `constructor` or
      // `toString` would otherwise return a truthy non-string, and the handler
      // would throw inside the route — which surfaces as a hung request and a
      // 30 s selector timeout rather than as an error anyone can read.
      const vendored = Object.hasOwn(VENDORED_FONTS, name) ? VENDORED_FONTS[name] : undefined;
      // 404, never a pass-through: a miss must fail locally and visibly. Letting
      // it reach the network would quietly restore the exact dependency this
      // fixture exists to remove.
      if (!vendored || !existsSync(join(FONT_DIR, vendored))) {
        return route.fulfill({
          status: 404,
          contentType: "text/plain",
          body: `no vendored copy of ${name} in tests/fonts — see tests/fixtures.ts`,
        });
      }
      return route.fulfill({ contentType: "font/woff2", path: join(FONT_DIR, vendored) });
    });
    await use(context);
  },
});

export { expect };
