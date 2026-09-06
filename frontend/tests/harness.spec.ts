/**
 * Guards the e2e harness itself.
 *
 * `tests/fixtures.ts` stubs the render-blocking `fonts.googleapis.com`
 * stylesheet so a third-party request can never gate the app's mount. Nothing
 * else in the suite would notice if that stub stopped working: the fonts would
 * simply load again, every test would keep passing on a fast network, and the
 * flake would come back only on CI, only sometimes — which is exactly how it
 * arrived (run 34050016261, `landing.spec.ts:96` blank for 30.0 s).
 *
 * So the stub gets its own assertion, made from inside the browser against what
 * the page ACTUALLY applied. A timing assertion could not tell a working stub
 * from a fast network, and counting requests cannot distinguish a fulfilled
 * route from a real response — but a custom property that only the stub defines
 * can.
 */
import { test, expect } from "./fixtures";

test.describe("e2e harness: third-party fonts are stubbed", () => {
  test("the stylesheet the browser applied is the stub, not Google's", async ({ page }) => {
    await page.goto("/", { waitUntil: "commit" });
    await page.waitForSelector(".theme-toggle", { timeout: 30000 });

    const marker = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue("--e2e-fonts-stubbed").trim(),
    );
    expect(
      marker,
      "the Google Fonts stylesheet reached the network. tests/fixtures.ts's " +
        "page.route no longer matches it, so every navigation in this suite is " +
        "again waiting on a third-party request before the app can mount.",
    ).toBe("1");
  });

  test("and the page really does link that stylesheet, so the stub is load-bearing", async ({
    page,
  }) => {
    // PARTNER. The assertion above is the only thing standing between this suite
    // and the flake, and it would pass just as happily if index.html stopped
    // linking any third-party stylesheet at all — at which point the stub is
    // dead code asserting nothing. This says out loud what it depends on.
    //
    // If index.html ever stops loading fonts from a third party (self-hosting
    // them is the standing suggestion), this test goes red on purpose: delete
    // it, delete the marker test, and delete the stub together.
    await page.goto("/", { waitUntil: "commit" });
    await page.waitForSelector(".theme-toggle", { timeout: 30000 });

    const thirdPartyStylesheets = await page.evaluate(() =>
      [...document.querySelectorAll<HTMLLinkElement>('link[rel~="stylesheet"]')]
        .map((l) => l.href)
        .filter((href) => !href.startsWith(window.location.origin)),
    );
    expect(
      thirdPartyStylesheets.some((href) => href.startsWith("https://fonts.googleapis.com/")),
      "index.html no longer render-blocks on a fonts.googleapis.com stylesheet",
    ).toBe(true);
  });
});
