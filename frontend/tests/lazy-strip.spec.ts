/**
 * #358 — the deferred below-the-fold marketing strip, in a real browser.
 *
 * The unit tests (src/components/LandingPage.strip.test.tsx) run against a
 * HAND-INSTALLED IntersectionObserver, because jsdom implements none. That
 * proves the wiring but not the behaviour: only a real engine decides when the
 * sentinel is "near enough" to the viewport to fire. This file is the part that
 * cannot be faked.
 *
 * IT ALSO RECORDS AN HONEST LIMIT. The deferral is real on a phone-sized
 * viewport, where the sentinel sits far below the fold. On a roomy desktop
 * viewport it is NOT: Hero + ticker + sources-strip are short enough that the
 * sentinel starts within the observer's 400 px lead margin, so the chunk is
 * requested immediately after first paint. That is still off the critical
 * rendering path — the entry chunk is 3,521 B gzip smaller and paints sooner —
 * but it is not a byte the desktop reader never downloads, and the first test
 * below MEASURES that rather than leaving it as a claim. If a future change
 * makes the Hero taller, the desktop case starts deferring too and that test's
 * recorded geometry is what will show it.
 */
import { test, expect } from "./fixtures";
import { gotoApp } from "./helpers";

/** Only exists inside the deferred chunk. */
const DEFERRED = "#pricing";
/** Never leaves the eager bundle — the control for every assertion below. */
const EAGER = "#demo";

const PHONE = { width: 390, height: 844 };

/** Must match STRIP_ROOT_MARGIN in src/lib/useDeferredReveal.ts. */
const LEAD_MARGIN_PX = 400;

test.describe("the deferred landing strip (#358)", () => {
  test("records where the sentinel sits relative to the fold on desktop", async ({ page }, testInfo) => {
    const requested: string[] = [];
    page.on("request", (r) => {
      if (/landing-strip/.test(r.url())) requested.push(r.url());
    });

    await gotoApp(page);

    const geometry = await page.evaluate(() => {
      const el = document.querySelector("[data-strip-sentinel]");
      return {
        sentinelTop: el ? Math.round(el.getBoundingClientRect().top) : null,
        viewportHeight: window.innerHeight,
      };
    });

    // The sentinel must exist, or the whole gate is dead and every other
    // assertion in this file is about nothing.
    expect(geometry.sentinelTop).not.toBeNull();

    // RECORDED, not asserted. Measured on Desktop Chrome 1280x720 at the time
    // of writing: sentinelTop 854 px, viewport 720 px, so 854 < 720 + 400 and
    // the desktop observer fires on first paint — the chunk is off the critical
    // path but not skipped.
    //
    // An earlier draft ASSERTED that inequality to keep the docs honest. That
    // was backwards: making the Hero taller would push the sentinel past the
    // lead margin and start deferring on desktop too — a strict improvement to
    // exactly the thing #358 is about — and the test would have gone RED for it.
    // A check that fails when the behaviour gets better locks in the weaker
    // behaviour, so this attaches the number to the report instead.
    const deferredOnThisViewport = geometry.sentinelTop! >= geometry.viewportHeight + LEAD_MARGIN_PX;

    // The one-directional guard that DOES belong here: whichever side of the
    // margin the sentinel falls on, the desktop reader must end up with the
    // content. That fails only when something is broken, never when it improves.
    await expect(page.locator(DEFERRED)).toBeVisible({ timeout: 10_000 });

    // ASSERT THE THING, NOT A PROXY. Geometry only PREDICTS whether the chunk
    // is fetched on first paint; the request count IS the fact the budget file
    // reasons about. Recorded, not asserted in a direction, for the same reason
    // the inequality is not: fetching it later would be an improvement.
    await testInfo.attach("desktop-first-paint", {
      body: JSON.stringify(
        {
          ...geometry,
          leadMarginPx: LEAD_MARGIN_PX,
          deferredOnThisViewport,
          stripRequestsWithoutScrolling: requested.length,
        },
        null,
        2,
      ),
      contentType: "application/json",
    });
    // Geometry and network must at least AGREE. If the sentinel is inside the
    // lead margin the chunk must have been requested, and vice versa — a
    // mismatch means the gate is not doing what the docs say it does, whichever
    // way round it is.
    expect(requested.length > 0).toBe(!deferredOnThisViewport);
  });

  test("defers the strip on a phone-sized viewport until the reader scrolls", async ({ page }) => {
    await page.setViewportSize(PHONE);
    await gotoApp(page);

    // Eager content is there...
    await expect(page.locator(EAGER)).toBeAttached();

    // MEASURED at 390x844: sentinelTop 1458 px, i.e. 1458 > 844 + 400, so the
    // sentinel starts OUTSIDE the lead margin. That inequality is the reason
    // the deferral below actually happens here and not on desktop; pinning it
    // means a layout change that breaks the premise fails loudly rather than
    // turning the rest of this test into a tautology.
    const sentinelTop = await page.evaluate(() =>
      Math.round(document.querySelector("[data-strip-sentinel]")!.getBoundingClientRect().top),
    );
    expect(sentinelTop).toBeGreaterThan(PHONE.height + LEAD_MARGIN_PX);
    // ...and the deferred content is genuinely not. Given a real timeout, so
    // this is "did not arrive", not "was not there for one tick".
    await expect(page.locator(DEFERRED)).toHaveCount(0);
    await page.waitForTimeout(750);
    await expect(page.locator(DEFERRED)).toHaveCount(0);

    // Scrolling is what fetches it.
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));

    await expect(page.locator(DEFERRED)).toBeAttached({ timeout: 10_000 });
    await expect(page.locator("footer")).toBeAttached();
  });

  test("requests the strip chunk only once, even though two boundaries mount it", async ({ page }) => {
    await page.setViewportSize(PHONE);
    const requested: string[] = [];
    page.on("request", (r) => {
      if (/landing-strip/.test(r.url())) requested.push(r.url());
    });

    await gotoApp(page);
    await page.waitForTimeout(500);
    // Nothing fetched while the strip is still below the fold.
    expect(requested).toEqual([]);

    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await expect(page.locator("footer")).toBeAttached({ timeout: 10_000 });
    await page.waitForTimeout(300);

    // LandingStripTop and LandingStripBottom are two lazy() wrappers over ONE
    // module, so the reader pays one round trip, not two.
    //
    // BE HONEST ABOUT WHAT THIS PROVES. The suite runs against the Vite DEV
    // server, and both wrappers use the same specifier, so the browser's own
    // ESM module cache would collapse two requests into one regardless of how
    // the production build chunks. What this test really catches is the deferred
    // half being split back into TWO modules (two URLs, two requests). The
    // production one-chunk property is proved where it can be — against the
    // build manifest, in scripts/lazy-landing-strip.test.mjs.
    expect(requested.length).toBe(1);
  });

  test.describe("header nav links that point into the deferred strip", () => {
    for (const [label, id] of [
      ["Who it's for", "who"],
      ["How it works", "how"],
      ["Pricing", "pricing"],
      ["FAQ", "faq"],
    ] as const) {
      test(`"${label}" mounts its target and scrolls to it`, async ({ page }) => {
        await page.setViewportSize(PHONE);
        await gotoApp(page);
        await expect(page.locator(`#${id}`)).toHaveCount(0);

        await page.getByRole("link", { name: label }).click();

        // Before #358 `scrollToSection` gave up the moment the element was
        // missing, so these four links did nothing at all: no mount, no scroll.
        await expect(page.locator(`#${id}`)).toBeVisible({ timeout: 10_000 });
        await expect
          .poll(async () => page.evaluate(() => window.scrollY), { timeout: 10_000 })
          .toBeGreaterThan(0);
      });
    }
  });

  test("still arrives on scroll after a trip through the chat screen", async ({ page }) => {
    // <main> — and the observer's sentinel with it — is destroyed for the chat
    // screen and rebuilt on the way back. The first implementation attached the
    // observer from a useEffect over a stable ref object, so it went on watching
    // the DETACHED original node and the strip became permanently unreachable by
    // scrolling for the rest of the session. Caught in review, before merge.
    await page.setViewportSize(PHONE);
    await gotoApp(page);
    await expect(page.locator(DEFERRED)).toHaveCount(0);

    await page.locator(".cta-button").first().click();
    await expect(page.locator(".chat-input")).toBeVisible({ timeout: 10_000 });
    await page.locator(".back-button").click();
    await expect(page.locator("#top")).toBeVisible();

    // Still deferred — the round trip must not have revealed it by accident,
    // or the scroll below would prove nothing.
    await expect(page.locator(DEFERRED)).toHaveCount(0);

    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await expect(page.locator(DEFERRED)).toBeAttached({ timeout: 10_000 });
  });

  test("a deep link to a deferred section lands on it", async ({ page }) => {
    await page.setViewportSize(PHONE);
    await page.goto("/#faq", { waitUntil: "commit" });
    await page.waitForSelector(".theme-toggle", { timeout: 30_000 });

    await expect(page.locator("#faq")).toBeVisible({ timeout: 10_000 });
    await expect.poll(async () => page.evaluate(() => window.scrollY), { timeout: 10_000 }).toBeGreaterThan(0);
  });
});
