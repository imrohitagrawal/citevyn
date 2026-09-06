/**
 * #365: the app must MOUNT even when every font request fails.
 *
 * THE DEFECT THIS EXISTS FOR
 * --------------------------
 * `index.html` used to load Geist and JetBrains Mono with a plain render-blocking
 * `<link rel="stylesheet">` pointing at a third party. A render-blocking
 * stylesheet also blocks `<script type="module">` from EXECUTING, and this app
 * is one module entry — so while that request was slow the visitor got a BLANK
 * page: no spinner, no text, no error. Measured on this app at f171696, same
 * dev server, back to back: 191/207/323 ms to first rendered UI normally,
 * 40 150 ms with the host stalled 40 s, 108/158 ms served from disk. CI met it
 * too (#364, run 34050016261: 31.24 s of video whose first 30 s are one
 * identical 2 133-byte blank frame).
 *
 * WHY THIS FILE AND NOT THE HARNESS FIXTURE
 * -----------------------------------------
 * `tests/fixtures.ts` answers both Google hosts from disk. That is right for
 * the other six specs — it removes a third-party dependency from every
 * navigation — but it means a test running THROUGH it can never see the
 * production path, because the stub is hiding exactly the thing under test. A
 * regression test that passes because fonts loaded fine proves nothing.
 *
 * So each test here registers its OWN route first thing in the body. Playwright
 * matches route handlers in REVERSE registration order, so a handler added
 * inside the test runs BEFORE the fixture's context-level one and wins. That is
 * asserted, not assumed: `the fixture does not answer over the top of these
 * routes` below records what the fixture actually served, and would fail if
 * precedence went the other way and left every test in this file vacuous.
 *
 * It still imports `test` from `./fixtures`, because `src/test/buildGuards.test.ts`
 * requires every spec to and that guard is worth keeping without exceptions.
 * Overriding a route is the honest way through it; carving this file out of the
 * guard would weaken the guard for every future spec too.
 *
 * WHAT EACH TEST GOES RED FOR (one line each, per the house rule):
 *   1. the page's mount waits on any third-party request — i.e. #365 is back
 *   2. the app cannot render without its font FILES (a hard dependency on a
 *      resource that was never meant to be one)
 *   3. the page requests a font from anywhere but this origin
 *   4. the in-test routes stop taking precedence, which would make 1-3 vacuous
 *
 * REVERTING THE FIX turns 1 red: put the third-party stylesheet link back in
 * `index.html` and test 1 stalls on it for the full `STALL_MS` and blows its
 * budget — verified by doing exactly that before this file was committed.
 */
import { test, expect } from "./fixtures";
import type { Page, Route } from "@playwright/test";

/** Any host that is not the dev server. */
const OFF_ORIGIN = /^https?:\/\/(?!localhost|127\.0\.0\.1)/i;
/** The self-hosted faces, as `src/styles/fonts.css` names them. */
const LOCAL_FONTS = "**/fonts/*.woff2";

/**
 * Long enough that a mount which waits on one of these is unmistakable, and far
 * longer than the budget below, so the two can never be confused for jitter.
 * The healthy path never waits at all — no request is made.
 */
const STALL_MS = 60_000;

/**
 * The budget. Healthy mount is ~200-350 ms measured; the defect is the full
 * stall. 10 s sits two orders of magnitude above the signal and six times below
 * the failure, so this cannot flake on a loaded machine the way a tight
 * wall-clock assertion would (#354, #344).
 */
const MOUNT_BUDGET_MS = 10_000;

/** `.theme-toggle` exists only once React has mounted — the same selector `helpers.ts` waits on. */
const MOUNTED = ".theme-toggle";

/**
 * Hold a request open without ever answering it, and hand back a release
 * function. Aborting is NOT the same failure: an aborted stylesheet fails fast
 * and the parser moves on, which is the one third-party outcome that never
 * caused #365. Stalling is what a blocked network and a slow CDN both look
 * like, and it is what leaves the page blank.
 */
function stalling(): { handler: (route: Route) => Promise<void>; release: () => void } {
  const pending: (() => void)[] = [];
  return {
    handler: async (route) => {
      await new Promise<void>((resolve) => pending.push(resolve));
      // Only reached via release(), during teardown. `.catch` because the
      // context may already be closing by then, which is not a failure.
      await route.abort().catch(() => {});
    },
    release: () => {
      for (const resolve of pending.splice(0)) resolve();
    },
  };
}

/** Navigate and return ms from `goto` to the app being on screen. */
async function timeToMount(page: Page): Promise<number> {
  const started = Date.now();
  // `commit` so the clock starts when the HTML lands, not when the load event
  // fires — a blocked stylesheet delays `load` too, which would hide the very
  // gap being measured inside the navigation itself.
  await page.goto("/", { waitUntil: "commit" });
  await page.waitForSelector(MOUNTED, { timeout: MOUNT_BUDGET_MS * 2 });
  return Date.now() - started;
}

test.describe("#365: a failing font never blanks the page", () => {
  test("the app mounts while every third-party host hangs", async ({ page }) => {
    const stall = stalling();
    await page.route(OFF_ORIGIN, stall.handler);
    try {
      const ms = await timeToMount(page);
      expect(
        ms,
        `the app took ${ms} ms to mount while a third-party host was hanging. Something ` +
          `in the critical path is waiting on it — a render-blocking off-origin ` +
          `<link rel=stylesheet> is the shape that caused #365, and it blocks the ` +
          `module entry from EXECUTING, so the visitor sees a blank page for as long ` +
          `as that host is slow.`,
      ).toBeLessThan(MOUNT_BUDGET_MS);

      // Mounting is not rendering. Without this, an empty shell that happens to
      // contain the toggle would pass.
      await expect(page.locator("h1").first()).toBeVisible();
    } finally {
      stall.release();
    }
  });

  test("and it mounts, and renders text, with its own font files failing too", async ({ page }) => {
    // The actual requirement in #365's words: "the app must MOUNT even if every
    // font request fails". Self-hosting moves the request to this origin; it
    // must not have made it load-bearing on the way. Aborted rather than
    // stalled here on purpose — a 404/blocked font file is the realistic
    // same-origin failure, and `font-display: swap` is what has to cover it.
    const stall = stalling();
    await page.route(OFF_ORIGIN, stall.handler);
    await page.route(LOCAL_FONTS, (route) => route.abort());
    try {
      const ms = await timeToMount(page);
      expect(ms, `${ms} ms to mount with all font files failing`).toBeLessThan(MOUNT_BUDGET_MS);

      const heading = page.locator("h1").first();
      await expect(heading).toBeVisible();
      // Real, non-empty text — the fallback stack rendering, not a blank box.
      expect((await heading.innerText()).trim().length).toBeGreaterThan(0);

      // And the faces genuinely did NOT load, so the assertions above describe
      // the failing case rather than a case where the abort silently missed.
      const loaded = await page.evaluate(() =>
        [...document.fonts].filter((f) => f.status === "loaded").map((f) => f.family),
      );
      expect(
        loaded,
        "a face loaded despite every font request being aborted — the route missed, so " +
          "this test proved nothing about the failing path",
      ).toEqual([]);
    } finally {
      stall.release();
    }
  });

  test("every font the page asks for comes from this origin", async ({ page }) => {
    // The positive half. The two tests above are satisfied by a page that uses
    // no web font at all; this one fails if the faces stop being requested, and
    // fails differently if they are requested from someone else.
    const fontRequests: string[] = [];
    page.on("request", (r) => {
      if (r.resourceType() === "font") fontRequests.push(r.url());
    });

    await page.goto("/", { waitUntil: "commit" });
    await page.waitForSelector(MOUNTED, { timeout: MOUNT_BUDGET_MS * 2 });
    await page.evaluate(async () => {
      await Promise.allSettled([
        document.fonts.load('400 16px "Geist"'),
        document.fonts.load('400 16px "JetBrains Mono"'),
      ]);
      await document.fonts.ready;
    });

    const origin = new URL(page.url()).origin;
    expect(
      fontRequests.length,
      "the page requested no web font at all — the origin check below would be vacuous",
    ).toBeGreaterThan(1);
    expect(
      fontRequests.filter((u) => !u.startsWith(origin)),
      "a font file came from a third party. Self-hosting is what let the CSP drop " +
        "fonts.gstatic.com from font-src (#365); this would be a silent CSP violation " +
        "in production, invisible unless you open the console.",
    ).toEqual([]);
  });

  test("the fixture does not answer over the top of these routes", async ({ page }) => {
    // PARTNER for all three above. They rely on a route registered inside the
    // test body taking precedence over `fixtures.ts`'s context-level route for
    // the same URL. Playwright documents last-registered-wins, but if that ever
    // stopped holding, the fixture would quietly serve Geist from disk and
    // every test in this file would pass while testing nothing.
    //
    // So: aim a route at the host the fixture claims, ask for it, and prove the
    // in-test handler is the one that ran.
    let mine = 0;
    await page.route(/^https:\/\/fonts\.googleapis\.com\//, async (route) => {
      mine += 1;
      await route.fulfill({ contentType: "text/css", body: "/* from the test */" });
    });

    await page.goto("/", { waitUntil: "commit" });
    await page.waitForSelector(MOUNTED, { timeout: MOUNT_BUDGET_MS * 2 });
    const body = await page.evaluate(async () => {
      const r = await fetch("https://fonts.googleapis.com/css2?family=Geist:wght@400..700");
      return r.text();
    });

    // Partner, not the proof: `> 0` rather than `=== 1` because what this test
    // is about is PRECEDENCE, not how many times the host is asked. Pinning the
    // count would additionally go red the day the page starts requesting that
    // host again — true, but reported under the wrong name; test 3 above is
    // where that failure belongs and it says so plainly.
    expect(mine, "the in-test route never ran — fixtures.ts answered instead").toBeGreaterThan(0);
    expect(
      body,
      "the response came from tests/fonts/, not from this test's handler, so a route " +
        "registered in a test body no longer overrides the fixture's",
    ).toBe("/* from the test */");
  });
});
