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

/** Three lines separated by single newlines — the shape a real answer has. */
const MULTILINE_ANSWER =
  "Rate limits apply per organization.\n" +
  "They are returned in the response headers.\n" +
  "Retry after the window resets.";

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
    await expect(body).toContainText("Retry after the window resets.", {
      timeout: 20000,
    });

    // Count the line boxes the answer text actually occupies. `getClientRects()`
    // on a Range over the text returns one rect per rendered line, so this reads
    // the real layout rather than the CSS that produced it.
    const lineCount = await body.evaluate((el) => {
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      let node: Node | null;
      while ((node = walker.nextNode())) {
        if ((node.textContent || "").includes("Retry after the window resets.")) {
          const range = document.createRange();
          // Measure the whole text node that carries the answer.
          range.selectNodeContents(node.parentNode as Node);
          return range.getClientRects().length;
        }
      }
      return 0;
    });

    // Three newline-separated sentences, each far short of the bubble width, so
    // with newlines preserved they occupy 3 line boxes and without them 1.
    expect(lineCount).toBeGreaterThanOrEqual(3);
  });
});
