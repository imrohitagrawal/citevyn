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

// ---------------------------------------------------------------------------
// #303 — the markdown subset, citation chips and collapsed cards, END TO END.
//
// Unit tests cover the parser and the component in jsdom. This drives the real
// browser because three of the claims are only true in a real layout: that the
// unsupported markup is VISIBLE text rather than a swallowed element, that a
// chip is a working link, and that the whole thing survives the streaming
// reveal (the parser re-runs on every cumulative chunk).
// ---------------------------------------------------------------------------
const RICH_ANSWER = [
  "Claude Code applies edits through **permissioned tools** [1].",
  "Install it with `npm i -g @anthropic-ai/claude-code` [2].",
  "It is useful for:",
  "- multi-file refactors [1]",
  "- test runs and PR prep [3]",
  "Unsupported markup like <script>alert(1)</script> and # Heading shows literally.",
].join("\n");

const RICH_CITATIONS = [
  { citation_id: "c1", source_name: "About CiteVyn", title: "About CiteVyn", url: "/about", chunk_id: "a", marker: 1 },
  { citation_id: "c2", source_name: "Claude Code Docs", title: "Install", url: "https://docs.claude.com/install", chunk_id: "b", marker: 2 },
  { citation_id: "c3", source_name: "About CiteVyn", title: "About CiteVyn", url: "/about", chunk_id: "c", marker: 3 },
];

test.describe("answer rendering: markdown subset + citation chips (#303)", () => {
  test("renders the subset, chips the markers, collapses cards per document (live only)", async ({
    page,
  }) => {
    await enterChat(page);
    const isLive = await page.evaluate(() =>
      /LIVE/i.test(document.querySelector(".demo-badge")?.textContent || ""),
    );
    if (!isLive) {
      test.skip(true, "Needs the live path to inject an answer with [n] markers.");
      return;
    }

    await page.route("**/v1/sessions/*/messages", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          request_id: "req_303",
          message_id: "msg_303",
          answer: RICH_ANSWER,
          citations: RICH_CITATIONS,
          domain: "claude_code",
          intent: "how_to",
          confidence: "high",
          cache_hit: false,
          retrieval_strategy: "hybrid_reranked",
          unsupported: false,
          no_answer: false,
          source_version_hash: "stub",
          answer_policy_version: "stub",
        }),
      });
    });

    await page.locator(".chat-input").fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    const bot = page.locator(".message.bot-msg").last();
    await expect(bot.locator(".source-card").first()).toBeVisible({ timeout: 20000 });

    // The subset renders as real elements.
    await expect(bot.locator("strong")).toHaveText("permissioned tools");
    await expect(bot.locator(".answer-code")).toHaveText("npm i -g @anthropic-ai/claude-code");
    await expect(bot.locator(".answer-list li")).toHaveCount(2);

    // Unsupported markup is VISIBLE text, and produced no element.
    await expect(bot).toContainText("<script>alert(1)</script>");
    await expect(bot).toContainText("# Heading");
    expect(await bot.locator("script").count()).toBe(0);

    // Three citations, two documents -> two cards; the repeated one lists both markers.
    await expect(bot.locator(".source-card")).toHaveCount(2);
    await expect(bot.locator(".source-card").first().locator(".source-number")).toHaveText("1, 3");

    // Chips are real links, and the plain [n] form survives for copy/paste.
    const chips = bot.locator(".citation-chip");
    await expect(chips).toHaveCount(4); // [1] [2] [1] [3]
    await expect(chips.first()).toHaveAttribute("href", "/about");
    await expect(chips.first()).toHaveAttribute("target", "_blank");
    await expect(chips.first()).toHaveText("[1]");

    // Hovering a card lights up every chip OCCURRENCE it backs. "About CiteVyn"
    // backs markers 1 and 3, and [1] is written twice, so that is three chips —
    // not two. The distinction matters: a reader scanning for "which sentences
    // does this source support?" needs every occurrence lit, not one per marker.
    await bot.locator(".source-card").first().hover();
    await expect(bot.locator(".citation-chip.is-active")).toHaveCount(3);
    // ...and hovering the OTHER card lights exactly its one chip, so the first
    // assertion is not just "all chips are always active".
    await bot.locator(".source-card").nth(1).hover();
    await expect(bot.locator(".citation-chip.is-active")).toHaveCount(1);

    // The legend appears once, under the first cited answer.
    await expect(page.locator(".citation-legend")).toHaveCount(1);
  });
});
