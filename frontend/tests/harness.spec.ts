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
 * only go red when a third-party request actually escapes, or when what the
 * browser applied is not what `tests/fonts/` holds.
 *
 * WHAT EACH TEST GOES RED FOR (one line each, per the house rule):
 *   1. a page resource is fetched from a host the fixture does not intercept
 *   2. the `fonts.googleapis.com` route stops fulfilling, so Google answers
 *   3. a font file 404s (an unvendored subset), or the two faces do not end up
 *      loaded — which is what a swapped or corrupt vendored file looks like
 *   4. `fixtures.ts` overrides `page` instead of `context`, so a second page
 *      opened by a test is not covered
 *
 * #365 HAS NOW LANDED, and this is what actually happened to each of them —
 * recorded because the prediction above was only half right.
 *
 *   1. STRENGTHENED, not unchanged. It used to exempt the two font hosts from
 *      the off-origin check, because the page legitimately asked for them. It
 *      no longer does, so the exemption is gone and the assertion is now the
 *      flat "this page reaches NO third party". That makes it the e2e-level
 *      detector for a reintroduced third-party font link: `fixtures.ts` would
 *      serve such a link from disk and every other test would stay green, but
 *      the REQUEST is still recorded, so this one goes red.
 *   2. Vacuous by design, as predicted: `askedGoogle` is false, so the
 *      implication is satisfied without reading anything.
 *   3. HALF vacuous. Its first two assertions decay exactly as predicted. Its
 *      third does NOT — `loadedFamilies` and the monospaced/proportional shape
 *      check now prove the SELF-HOSTED faces really arrive and really are the
 *      right two files, which is a stronger thing than it used to say.
 *   4. Unchanged and at full force.
 *
 * So `tests/fonts/` and the routes are kept as a backstop rather than deleted
 * (see `fixtures.ts` for why), and test 3 is kept because most of it still
 * bites. What guards the PRODUCTION path is elsewhere on purpose:
 * `src/test/buildGuards.test.ts` asserts the emitted `dist/index.html` and the
 * emitted CSS carry no off-origin render-blocking reference, and
 * `tests/fonts-offline.spec.ts` proves the app mounts with every font request
 * failing. Neither runs through this fixture, because a guard that runs THROUGH
 * the stub cannot see the path the stub is hiding.
 */
import { test, expect } from "./fixtures";
import type { Page } from "@playwright/test";

/** Everything the fixture is supposed to intercept. */
const FONT_HOSTS = /^https:\/\/fonts\.(googleapis|gstatic)\.com\//;

/** Load the app, recording every request and response, with fonts settled. */
async function loadRecording(page: Page) {
  const requests: string[] = [];
  const responses: { url: string; status: number }[] = [];
  page.on("request", (r) => requests.push(r.url()));
  page.on("response", (r) => responses.push({ url: r.url(), status: r.status() }));

  await page.goto("/", { waitUntil: "commit" });
  await page.waitForSelector(".theme-toggle", { timeout: 30000 });

  // Ask for the faces explicitly rather than waiting for a paint to ask for
  // them. `document.fonts.status` is ALSO "loaded" before anything has started
  // loading, so polling it cannot tell "all done" from "not begun" — a wait on
  // it can return with an empty recording and make every check below vacuous.
  // `document.fonts.load()` starts the load and resolves when it finishes, so
  // there is nothing to race.
  //
  // `allSettled`, not `all`: `document.fonts.load()` REJECTS with a bare
  // `NetworkError` when a face fails to load, and that rejection would come out
  // of `page.evaluate` as "page.evaluate: NetworkError: A network error
  // occurred" — burying every named diagnosis below it. Measured: with the
  // stylesheet route neutered, all three tests reported that instead of "the
  // stylesheet reached the network". A failed load is DATA here, not an error;
  // the assertions are what interpret it.
  const loadedFamilies = await page.evaluate(async () => {
    await Promise.allSettled([
      document.fonts.load('400 16px "Geist"'),
      document.fonts.load('400 16px "JetBrains Mono"'),
    ]);
    await document.fonts.ready;
    return [...document.fonts].filter((f) => f.status === "loaded").map((f) => f.family);
  });

  const origin = new URL(page.url()).origin;
  return {
    requests,
    responses,
    loadedFamilies,
    offOrigin: requests.filter((u) => !u.startsWith(origin)),
    fontResponses: responses.filter((r) => FONT_HOSTS.test(r.url)),
    askedGoogle: requests.some((u) => u.startsWith("https://fonts.googleapis.com/")),
  };
}

test.describe("e2e harness: nothing reaches a third party", () => {
  test("the page reaches no third-party host at all", async ({ page }) => {
    const { requests, offOrigin } = await loadRecording(page);

    // PARTNER, and it has to come first: the assertion below is "this set
    // contains nothing bad", which an empty or broken recording satisfies for
    // free. The app is served as unbundled ESM in dev, so a real load is dozens
    // of requests.
    expect(
      requests.length,
      "the request recorder saw almost nothing — the check below would be vacuous",
    ).toBeGreaterThan(10);

    // NO font-host exemption (#365). Until the faces were self-hosted this
    // filtered `FONT_HOSTS` out, because the page legitimately asked Google for
    // them and the fixture answered from disk. It does not any more, so the
    // exemption would only ever hide a regression: `fixtures.ts` still answers
    // those two hosts from disk, which means a reintroduced third-party font
    // link would leave every other test in the suite green. The REQUEST is
    // recorded either way, and this is where it fails.
    expect(
      offOrigin,
      "this page reached a third-party host, so first paint now depends on someone " +
        "else's network again — that is the #365 defect (a render-blocking " +
        "third-party stylesheet also blocks <script type=module> from executing, " +
        "and the visitor gets a blank page). Self-host it, or argue for it here " +
        "and widen backend/app/core/security_headers.py's CSP in the same change",
    ).toEqual([]);
  });

  test("and what it applied is the vendored copy, not Google's response", async ({ page }) => {
    const { askedGoogle } = await loadRecording(page);

    const marker = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue("--e2e-fonts-vendored").trim(),
    );

    // Written as an implication, not a bare `toBe("1")`, so that removing the
    // third-party link (#365) leaves this green instead of red.
    //
    // The marker could be missing for two different reasons, and only one of
    // them is this test's. `src/test/buildGuards.test.ts` separately asserts
    // that `tests/fonts/google-fonts-latin.css` still CONTAINS the marker — so
    // if that guard is green and this one is red, the file is fine and the
    // route is what failed. Without that split, a regenerated CSS file that
    // dropped the marker would fail here with a message blaming the network,
    // and send the next reader after a problem that does not exist.
    expect(
      !askedGoogle || marker === "1",
      "the fonts.googleapis.com stylesheet reached the network. tests/fixtures.ts's " +
        "context.route no longer matches it, so every navigation in this suite is " +
        "again waiting on a third-party request before the app can mount. (If " +
        "buildGuards' \"the vendored stylesheet carries its marker\" test is ALSO " +
        "red, the file lost the marker instead and the route is fine.)",
    ).toBe(true);
  });

  test("every font request was served, and both faces really ended up loaded", async ({ page }) => {
    const { fontResponses, loadedFamilies, askedGoogle } = await loadRecording(page);

    // tests/fixtures.ts answers an unvendored subset, or an unexpected
    // `family=`, with 404 rather than letting it reach the network. That is the
    // signal this reads: needing latin-ext/cyrillic/greek/vietnamese, or adding
    // a third family, turns the suite red here — naming the file to vendor —
    // instead of silently rendering in a fallback face and quietly moving every
    // pixel measurement in fidelity.spec.ts and visual.spec.ts.
    expect(
      fontResponses.filter((r) => r.status !== 200).map((r) => `${r.status} ${r.url}`),
      "a font request was not served from tests/fonts/ — vendor it (see the " +
        "regeneration note in tests/fonts/google-fonts-latin.css)",
    ).toEqual([]);

    // PARTNER for that emptiness check, in the same implication form so #365
    // leaves it green: while the page still asks Google, the recording must
    // hold the stylesheet AND both woff2 files. It comes AFTER on purpose —
    // when a request really did fail, the check above names which one, and
    // seeing "404 .../css2?..." beats seeing "only 1 response recorded".
    expect(
      !askedGoogle || fontResponses.length >= 3,
      `only ${fontResponses.length} font responses were recorded while the page still ` +
        "asks fonts.googleapis.com — expected the stylesheet plus two woff2 files, so " +
        "the check above was vacuous",
    ).toBe(true);

    expect(
      loadedFamilies,
      "the browser did not end up with both faces loaded — a vendored file is " +
        "missing or truncated",
    ).toEqual(expect.arrayContaining(["Geist", "JetBrains Mono"]));

    // A 200 only proves bytes arrived, and "loaded" only proves they parsed —
    // neither proves they were the RIGHT bytes. Swap the two entries in
    // VENDORED_FONTS and every status is still 200 and both families still
    // report `loaded`, because each file is a perfectly valid woff2; the suite
    // would simply render each face in the other's metrics. That is exactly the
    // parity this whole change exists to keep, and CI could not see it
    // (visual.spec.ts is darwin-only, and fidelity's D2.5 margin might absorb
    // it). So check the one property that tells these two faces apart without
    // any pixel baseline: JetBrains Mono is monospaced and Geist is not.
    const shape = await page.evaluate(() => {
      const width = (family: string, text: string) => {
        const ctx = document.createElement("canvas").getContext("2d")!;
        ctx.font = `16px "${family}"`;
        return ctx.measureText(text).width;
      };
      return {
        monoNarrow: width("JetBrains Mono", "iiiiiiiiii"),
        monoWide: width("JetBrains Mono", "MMMMMMMMMM"),
        geistNarrow: width("Geist", "iiiiiiiiii"),
        geistWide: width("Geist", "MMMMMMMMMM"),
      };
    });
    expect(
      shape.monoWide,
      "the face serving JetBrains Mono is not monospaced — tests/fixtures.ts's " +
        "VENDORED_FONTS is serving the wrong file for it",
    ).toBeCloseTo(shape.monoNarrow, 1);
    expect(
      shape.geistNarrow,
      "the face serving Geist is monospaced — tests/fixtures.ts's VENDORED_FONTS " +
        "is serving JetBrains Mono's file under Geist's name",
    ).toBeLessThan(shape.geistWide * 0.9);
  });

  test("a page the test opens itself is covered too, not just the fixture's", async ({
    context,
  }) => {
    // THE ONLY THING that goes red if `fixtures.ts` overrides `page` instead of
    // `context`. Playwright builds `page` from `context`, so a context-level
    // route reaches every page in the context; a `page.route` reaches only the
    // first. Without this test that choice is unverified, and reverting it
    // would leave the whole suite green — the shape AGENTS.md warns about,
    // where the fix for a review finding ships without its own check.
    const second = await context.newPage();
    const requests: string[] = [];
    second.on("request", (r) => requests.push(r.url()));

    await second.goto("/", { waitUntil: "commit" });
    await second.waitForSelector(".theme-toggle", { timeout: 30000 });
    const marker = await second.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue("--e2e-fonts-vendored").trim(),
    );
    const askedGoogle = requests.some((u) => u.startsWith("https://fonts.googleapis.com/"));
    await second.close();

    // Partner: prove the second page really loaded before reading the marker.
    expect(requests.length, "the second page recorded almost nothing").toBeGreaterThan(10);

    expect(
      !askedGoogle || marker === "1",
      "a page opened with context.newPage() got Google's stylesheet, so the font " +
        "route is bound to a single page rather than to the context",
    ).toBe(true);
  });
});
