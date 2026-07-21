/**
 * Rendered-format regression guard for chat answers (#215 / B1).
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * `white-space: pre-wrap` was added to the chat bubble in `7e9bc92`/`ad062bb`
 * and silently deleted by `2503dd4` ("commit green CiteVyn landing baseline"),
 * where the landing design port overwrote the chat CSS. The regression shipped
 * to production: a multi-line answer renders as one run-on blob.
 *
 * A 94-test UI suite did not catch it, for two compounding reasons:
 *   1. jsdom does not compute CSS, so no vitest test can see it; and
 *   2. every canned demo answer — the stub's and all 15 offline `KB` entries —
 *      is a single unbroken line, so even a browser test had nothing to look at.
 *
 * So this spec drives the LIVE path and supplies its own multi-line payload via
 * `page.route`, which is the only way to get a newline into a rendered answer.
 *
 * WHAT IT ASSERTS, AND WHY THAT SHAPE
 * -----------------------------------
 * It asserts RENDERED GEOMETRY — the number of line boxes the text node
 * actually occupies — not `getComputedStyle().whiteSpace`. Two reasons:
 *   - A property assertion is mechanism-coupled: B3 re-scopes `pre-wrap` off the
 *     markdown container on purpose, which would fail a property check while the
 *     user-visible behaviour is still correct.
 *   - `white-space: pre-line` also preserves newlines. A property assertion
 *     pinned to the literal string `pre-wrap` would pass while a mutant that
 *     breaks long-line wrapping goes unnoticed; a geometry assertion does not
 *     care which mechanism produced the lines, only that they are there.
 */
import { test, expect } from "@playwright/test";
import { enterChat, gotoApp } from "./helpers";

test.beforeEach(async ({ page }) => {
  await gotoApp(page);
});

/**
 * Three SHORT lines separated by single newlines.
 *
 * Deliberately short enough that they cannot wrap at any viewport width the
 * suite might run at. An earlier version used realistic sentence-length lines,
 * which made the assertion viewport-coupled: below ~500px the *broken* render
 * wraps into 3+ line boxes on its own and the guard passes while the bug is
 * present. With unwrappable lines the count is decided purely by whether
 * newlines are preserved, so the guard holds at any width — including a future
 * mobile project, which would otherwise silently defuse it.
 */
const MULTILINE_ANSWER = "Alpha.\nBeta.\nGamma.";

test.describe("answer formatting", () => {
  test("a multi-line answer renders as multiple lines, not one blob (live only)", async ({
    page,
  }) => {
    await enterChat(page);

    const isLive = await page.evaluate(() =>
      /LIVE/i.test(document.querySelector(".demo-badge")?.textContent || ""),
    );
    if (!isLive) {
      test.skip(
        true,
        "Needs the live path to inject a multi-line payload. Run via: " +
          "VITE_LIVE_STUB=1 npx playwright test --config=playwright.live.config.ts",
      );
      return;
    }

    // Own the payload outright. `route.fulfill` answers in the browser, so this
    // wins over the in-process vite stub regardless of whether it is enabled.
    await page.route("**/v1/sessions/*/messages", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          request_id: "req_fmt",
          message_id: "msg_fmt",
          answer: MULTILINE_ANSWER,
          citations: [],
          domain: "claude_api",
          intent: "how_to",
          confidence: "low",
          cache_hit: false,
          retrieval_strategy: "hybrid_reranked",
          unsupported: false,
          no_answer: false,
          source_version_hash: "stub",
          answer_policy_version: "stub",
        }),
      });
    });

    await page.locator(".chat-input").fill("What are the rate limits on the Claude API?");
    await page.keyboard.press("Enter");

    const body = page.locator(".message.bot .content").last();
    await expect(body).toContainText("Gamma.", { timeout: 20000 });

    // Count the line boxes the answer text actually occupies. `getClientRects()`
    // on a Range over the text returns one rect per rendered line, so this reads
    // the real layout rather than the CSS that produced it.
    // Count the DISTINCT vertical positions the answer text occupies. Counting
    // `getClientRects().length` directly is unreliable: `pre-wrap` emits extra
    // zero-width rects at the break points, so the raw count overstates lines.
    // Distinct `top` values are exactly "how many lines does a human see".
    const lineTops = await page
      .locator(".message.bot .message-body")
      .last()
      .evaluate((el) => {
        const range = document.createRange();
        range.selectNodeContents(el);
        const tops = new Set<number>();
        for (const rect of Array.from(range.getClientRects())) {
          if (rect.width > 0) tops.add(Math.round(rect.top));
        }
        return tops.size;
      });

    // "Alpha.\nBeta.\nGamma." cannot wrap at any width, so the count is decided
    // solely by whether the newlines survive: 3 when preserved, 1 when not.
    expect(lineTops).toBe(3);
  });
});
