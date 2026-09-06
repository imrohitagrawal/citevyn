/**
 * Guards the e2e harness itself.
 *
 * `tests/fixtures.ts` serves Geist and JetBrains Mono from `tests/fonts/` so no
 * test ever waits on `fonts.googleapis.com` before the app can mount. Nothing
 * else in the suite would notice if that stopped working: the fonts would simply
 * load from Google again, every test would keep passing on a fast network, and
 * the flake would come back only in CI, only sometimes — which is exactly how it
 * arrived (run 34050016261, `landing.spec.ts:96` blank for 30.0 s).
 *
 * Every assertion here is an INVARIANT, deliberately: "nothing reaches a third
 * party", not "index.html still has that link". #365 proposes removing the link
 * from `index.html` altogether, and a guard that reds when the underlying bug is
 * FIXED is this repo's standing anti-pattern. These stay green either way; they
 * only go red when a third-party request actually escapes.
 */
import { test, expect } from "./fixtures";

test.describe("e2e harness: nothing reaches a third party", () => {
  /** Load the app, recording every request and every response the page makes. */
  async function loadRecording(page: import("@playwright/test").Page) {
    const requests: string[] = [];
    const responses: { url: string; status: number }[] = [];
    page.on("request", (r) => requests.push(r.url()));
    page.on("response", (r) => responses.push({ url: r.url(), status: r.status() }));
    await page.goto("/", { waitUntil: "commit" });
    await page.waitForSelector(".theme-toggle", { timeout: 30000 });
    // The font files are requested only once the stylesheet has been parsed and
    // a glyph needs them, which is after mount. Without this the font requests
    // are simply not in the recording yet and every check below reads as green
    // on an empty set.
    await page.waitForFunction(() => document.fonts.status === "loaded", null, { timeout: 15000 });
    const origin = new URL(page.url()).origin;
    return {
      requests,
      responses,
      thirdParty: requests.filter((u) => !u.startsWith(origin)),
      origin,
    };
  }

  test("every off-origin request the page makes is one the fixture serves from disk", async ({
    page,
  }) => {
    const { requests, thirdParty } = await loadRecording(page);

    // PARTNER, and it has to come first: every assertion below is "this set
    // contains nothing bad", which an empty or broken recording satisfies for
    // free. The app is served as unbundled ESM in dev, so a real load is dozens
    // of requests.
    expect(
      requests.length,
      "the request recorder saw almost nothing — the checks below would be vacuous",
    ).toBeGreaterThan(10);

    const allowed = /^https:\/\/fonts\.(googleapis|gstatic)\.com\//;
    expect(
      thirdParty.filter((u) => !allowed.test(u)),
      "this page reached a third-party host the harness does not intercept, so the " +
        "app's load now depends on someone else's network again — add it to " +
        "tests/fixtures.ts or remove it from the page",
    ).toEqual([]);
  });

  test("and what it applied is the vendored copy, not Google's response", async ({ page }) => {
    const { requests } = await loadRecording(page);
    const askedGoogle = requests.some((u) => u.startsWith("https://fonts.googleapis.com/"));

    const marker = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue("--e2e-fonts-vendored").trim(),
    );

    // Written as an implication, not a bare `toBe("1")`, so that removing the
    // third-party link (#365) leaves this green instead of red. It goes red for
    // one reason only: the page asked Google and got Google's answer.
    expect(
      !askedGoogle || marker === "1",
      "the fonts.googleapis.com stylesheet reached the network. tests/fixtures.ts's " +
        "context.route no longer matches it, so every navigation in this suite is " +
        "again waiting on a third-party request before the app can mount.",
    ).toBe(true);
  });

  test("every font file came back 200, so no glyph fell through to a fallback face", async ({
    page,
  }) => {
    const { responses } = await loadRecording(page);
    const fontResponses = responses.filter((r) =>
      r.url.startsWith("https://fonts.gstatic.com/"),
    );

    // tests/fixtures.ts answers an unvendored subset with 404 rather than
    // letting it reach the network. That is the signal this test reads: content
    // needing latin-ext/cyrillic/greek/vietnamese turns the suite red here,
    // naming the file to vendor, instead of silently rendering in a fallback
    // face and quietly moving every pixel measurement in fidelity.spec.ts and
    // visual.spec.ts.
    expect(
      fontResponses.filter((r) => r.status !== 200).map((r) => r.url),
      "a font subset was requested that tests/fonts/ does not have — vendor it " +
        "(see the regeneration note in tests/fonts/google-fonts-latin.css)",
    ).toEqual([]);
  });
});
