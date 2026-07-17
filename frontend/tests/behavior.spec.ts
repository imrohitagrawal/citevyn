/**
 * Behavior suite — exhaustive interaction coverage for the §4 Definition of Done.
 * Wiring is theme-agnostic, so these run in the default theme except where a
 * flow is explicitly re-checked in dark.
 */
import { test, expect } from "@playwright/test";
import { gotoApp, ensureTheme, enterChat, waitStreamDone, TOKENS, SEMANTIC } from "./helpers";

test.beforeEach(async ({ page }) => {
  await gotoApp(page);
});

// ---------------------------------------------------------------------------
// Hero
// ---------------------------------------------------------------------------
test.describe("Hero", () => {
  test("auto-plays and cycles through all three canned Q&As", async ({ page }) => {
    const q = page.locator(".card-content .message-text").first();
    const seen = new Set<string>();
    seen.add((await q.textContent())!.trim());
    // Watch two advances (~ up to 3 stream+pause cycles).
    for (let i = 0; i < 2; i++) {
      const before = (await q.textContent())!.trim();
      await expect
        .poll(async () => (await q.textContent())!.trim(), { timeout: 15000, intervals: [400] })
        .not.toBe(before);
      seen.add((await q.textContent())!.trim());
    }
    expect(seen.size).toBeGreaterThanOrEqual(3);
  });

  test("progress pill + active class both track the current Q&A", async ({ page }) => {
    const q = page.locator(".card-content .message-text").first();
    const first = (await q.textContent())!.trim();
    await expect
      .poll(async () => (await q.textContent())!.trim(), { timeout: 15000, intervals: [400] })
      .not.toBe(first);
    const dots = page.locator(".progress-dot");
    // The pill width animates 7→22px over .3s and the .active class flips on the
    // same cycle. Poll the real settled invariant — exactly one dot ≥20px wide,
    // exactly one .active, and they are the same dot — instead of a fixed sleep
    // that can read mid-transition under load.
    await expect
      .poll(async () => {
        const widths = await dots.evaluateAll((els) => els.map((e) => getComputedStyle(e).width));
        const actives = await dots.evaluateAll((els) => els.map((e) => e.classList.contains("active")));
        const pillIdx = widths.findIndex((w) => parseFloat(w) >= 20);
        const activeCount = actives.filter(Boolean).length;
        return activeCount === 1 && pillIdx >= 0 && actives[pillIdx] === true;
      }, { timeout: 5000 })
      .toBe(true);
  });

  test("placeholder rotates over time (not stuck after first tick)", async ({ page }) => {
    const input = page.locator("#hero-input");
    const seen = new Set<string>();
    for (let i = 0; i < 4; i++) {
      seen.add((await input.getAttribute("placeholder"))!);
      await page.waitForTimeout(3300);
    }
    expect(seen.size).toBeGreaterThanOrEqual(3);
  });

  test("streaming shows a blinking caret that disappears, then sources fade up", async ({ page }) => {
    await expect(page.locator(".card-content .typing-cursor")).toBeVisible();
    await waitStreamDone(page);
    await expect(page.locator(".hero-card .source-card").first()).toBeVisible();
  });

  test("empty Ask: no nav, amber border, shake, warning ~3s", async ({ page }) => {
    await page.locator(".ask-button").click();
    await expect(page.locator("#top")).toBeVisible(); // still on landing
    const box = page.locator(".hero-input-box");
    await expect(box).toHaveClass(/shake/);
    // border-color transitions over ~200ms — poll until it settles on amber.
    await expect
      .poll(async () => box.evaluate((el) => getComputedStyle(el).borderColor))
      .toBe("rgb(180, 115, 42)"); // --color-warning #b4732a
    await expect(page.locator(".hero-nudge")).toBeVisible();
    await expect(page.locator(".hero-nudge")).not.toBeVisible({ timeout: 4000 });
  });

  test("Enter with text enters chat and streams that answer", async ({ page }) => {
    await page.locator("#hero-input").fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
    await expect(page.locator(".message.user-msg")).toHaveText("What is Claude Code?");
  });

  test("/ focuses hero input and is ignored when typing inside an input", async ({ page }) => {
    await page.locator(".cta-button").focus();
    await page.keyboard.press("/");
    await expect(page.locator("#hero-input")).toBeFocused();
    await page.locator("#hero-input").fill("test");
    await page.keyboard.press("/");
    // Shortcut is ignored while an input is focused, so "/" types normally.
    await expect(page.locator("#hero-input")).toBeFocused();
    expect(await page.locator("#hero-input").inputValue()).toBe("test/");
  });
});

// ---------------------------------------------------------------------------
// Ticker
// ---------------------------------------------------------------------------
test.describe("Question ticker", () => {
  test("marquee animates (cv-scroll 60s) and pauses on hover", async ({ page }) => {
    const track = page.locator(".ticker-track");
    const anim = await track.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { name: cs.animationName, dur: cs.animationDuration };
    });
    expect(anim.name).toBe("cv-scroll");
    expect(anim.dur).toBe("60s");
    // Hover the STATIONARY strip, not the track: the track is width:max-content
    // and translateX-animated, so its bounding box extends far off-screen left
    // and a position-relative hover lands nowhere. The strip is stable and the
    // track fills it, so `.ticker-track:hover` still matches and pauses it.
    const strip = page.locator(".ticker-strip");
    await strip.hover({ force: true, position: { x: 200, y: 25 } });
    await expect
      .poll(async () => track.evaluate((el) => getComputedStyle(el).animationPlayState))
      .toBe("paused");
  });

  test("list is duplicated (16 pills) with fade masks; a pill enters chat", async ({ page }) => {
    await expect(page.locator(".ticker-chip")).toHaveCount(16);
    // Marquee is always moving, so the pill never reaches "stable" — force the click.
    await page.locator(".ticker-chip").first().click({ force: true });
    await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Personas / Demo
// ---------------------------------------------------------------------------
test.describe("Sections", () => {
  test("persona ASK-THIS buttons enter chat with that question", async ({ page }) => {
    const btn = page.locator(".persona-q-btn").first();
    const q = (await btn.locator("span").first().innerText()).trim();
    await btn.click();
    await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
    await expect(page.locator(".message.user-msg").first()).toHaveText(q);
  });

  test("demo rail: select sets active (ink border) + streams; sourced question shows sources + continue", async ({ page }) => {
    const btns = page.locator(".demo-question");
    await expect(btns).toHaveCount(4);
    await btns.nth(1).click(); // codex-flag (sourced)
    await expect(btns.nth(1)).toHaveClass(/active/);
    expect(await btns.nth(1).evaluate((el) => getComputedStyle(el).borderColor)).toBe("rgb(28, 27, 25)");
    await waitStreamDone(page);
    await expect(page.locator(".demo-right .source-card").first()).toBeVisible();
    await expect(page.locator(".continue-btn")).toBeVisible();
  });

  test("demo refusal question shows amber REFUSED badge and no sources", async ({ page }) => {
    // "What's the best laptop for AI coding?" is the 4th demo question (refusal).
    await page.locator(".demo-question").nth(3).click();
    await waitStreamDone(page);
    const badge = page.locator(".demo-right .refusal-badge");
    await expect(badge).toBeVisible();
    expect(await badge.evaluate((el) => getComputedStyle(el).color)).toBe("rgb(180, 115, 42)"); // amber
    expect(await page.locator(".demo-right .source-card").count()).toBe(0);
  });

  test("continue-in-full-chat opens the chat view", async ({ page }) => {
    await page.locator(".demo-question").nth(0).click();
    await waitStreamDone(page);
    await page.locator(".continue-btn").click();
    await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Pricing
// ---------------------------------------------------------------------------
test.describe("Pricing", () => {
  test("Get Pro opens chat with the Pro question + honest not-live reply", async ({ page }) => {
    await page.locator(".pricing-card.featured .cta").click();
    await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
    await expect(page.locator(".message.user-msg")).toHaveText("What do I get with CiteVyn Pro?");
    await waitStreamDone(page);
    await expect(page.locator(".message.bot-msg")).toContainText("Pro isn't live yet");
  });

  test("Demo tier CTA enters chat; Enterprise 'Contact sales' is a no-op", async ({ page }) => {
    // Enterprise (3rd card) is disabled — clicking must NOT navigate.
    const enterpriseCta = page.locator(".pricing-card").nth(2).locator(".cta");
    await enterpriseCta.click({ force: true });
    await expect(page.locator('[data-screen-label="Chat"]')).toHaveCount(0);
    await expect(page.locator("#pricing")).toBeVisible();
    // Demo (1st card) CTA enters chat.
    await page.locator(".pricing-card").nth(0).locator(".cta").click();
    await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
  });

  test("Contact sales is inert to keyboard too (true no-op)", async ({ page }) => {
    const cta = page.locator(".pricing-card").nth(2).locator(".cta");
    await cta.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-screen-label="Chat"]')).toHaveCount(0);
    await expect(cta).toBeDisabled();
  });

  test("Enterprise CTA is out of the tab order (disabled → not focusable)", async ({ page }) => {
    const cta = page.locator(".pricing-card").nth(2).locator(".cta");
    await cta.focus().catch(() => {});
    const focusedIsCta = await cta.evaluate((el) => el === document.activeElement);
    expect(focusedIsCta).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// FAQ
// ---------------------------------------------------------------------------
test.describe("FAQ", () => {
  test("first open by default, one at a time, +/- signs flip", async ({ page }) => {
    const items = page.locator(".faq-item");
    await expect(page.locator(".faq-answer")).toHaveCount(1);
    await expect(items.nth(0).locator(".faq-sign")).toHaveText("−");
    await expect(items.nth(1).locator(".faq-sign")).toHaveText("+");
    await items.nth(2).locator(".faq-toggle").click();
    await expect(items.nth(0).locator(".faq-answer")).toHaveCount(0);
    await expect(items.nth(2).locator(".faq-answer")).toBeVisible();
    await expect(page.locator(".faq-answer")).toHaveCount(1);
  });

  test("toggles expose aria-expanded and aria-controls linkage", async ({ page }) => {
    const toggles = page.locator(".faq-toggle");
    await expect(toggles.nth(0)).toHaveAttribute("aria-expanded", "true");
    await expect(toggles.nth(1)).toHaveAttribute("aria-expanded", "false");
    // aria-controls references the open answer panel by id.
    const controls = await toggles.nth(0).getAttribute("aria-controls");
    expect(controls).toBeTruthy();
    await expect(page.locator(`#${controls}`)).toBeVisible();
    // Toggling item 3 collapses item 1 and expands item 3.
    await toggles.nth(2).click();
    await expect(toggles.nth(0)).toHaveAttribute("aria-expanded", "false");
    await expect(toggles.nth(2)).toHaveAttribute("aria-expanded", "true");
  });
});

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------
test.describe("Navigation", () => {
  test("nav link scrolls to section with ~72px header offset", async ({ page }) => {
    await page.locator(".nav-link", { hasText: "How it works" }).click();
    await expect.poll(async () => {
      const y = await page.locator("#how").evaluate((el) => el.getBoundingClientRect().top);
      return y > 60 && y < 120;
    }, { timeout: 5000 }).toBe(true);
  });

  test("nav links work from the chat view (return to landing, then scroll)", async ({ page }) => {
    await enterChat(page);
    await page.locator(".nav-link", { hasText: "Pricing" }).click();
    await expect(page.locator("#pricing")).toBeVisible();
    await expect.poll(async () => {
      const y = await page.locator("#pricing").evaluate((el) => el.getBoundingClientRect().top);
      return y > 40 && y < 140;
    }, { timeout: 5000 }).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------
test.describe("Chat", () => {
  test("streams answer then numbered sources; user bubble is ink, right, 16/16/4/16", async ({ page }) => {
    await enterChat(page);
    await page.locator(".chat-input").fill("How does Claude Code work?");
    await page.keyboard.press("Enter");
    const user = page.locator(".message.user-msg");
    await expect(user).toBeVisible();
    const style = await user.evaluate((el) => {
      const cs = getComputedStyle(el);
      return {
        bg: cs.backgroundColor,
        align: cs.alignSelf,
        tl: cs.borderTopLeftRadius,
        tr: cs.borderTopRightRadius,
        br: cs.borderBottomRightRadius,
        bl: cs.borderBottomLeftRadius,
      };
    });
    expect(style.bg).toBe("rgb(28, 27, 25)"); // --ink (light)
    expect(style.align).toBe("flex-end");
    // 16 / 16 / 4 / 16  (TL / TR / BR / BL)
    expect([style.tl, style.tr, style.br, style.bl]).toEqual(["16px", "16px", "4px", "16px"]);
    await waitStreamDone(page);
    await expect(page.locator(".message.bot-msg .source-card").first()).toBeVisible();
  });

  test("out-of-scope question → REFUSED badge, zero sources", async ({ page }) => {
    await enterChat(page);
    await page.locator(".chat-input").fill("What is the best laptop to buy?");
    await page.keyboard.press("Enter");
    await waitStreamDone(page);
    await expect(page.locator(".message.bot-msg .refusal-badge")).toBeVisible();
    expect(await page.locator(".message.bot-msg .source-card").count()).toBe(0);
  });

  test("duplicate question: no new bubble, scrolls to TOP of list on the original user question, pulses (cv-pulse)", async ({ page }) => {
    // Demo-mode only: live mode's per-call backend latency (often 5-15s
    // including LLM round-trip + retrieval) makes the "add N questions to
    // push the first one off the top" setup flaky under slow networks /
    // rate limits. The duplicate-guard and scroll-to-top behaviors are
    // UI-only and identical across modes.
    await enterChat(page);
    const isLive = await page.evaluate(
      () => /LIVE/i.test(document.querySelector(".demo-badge")?.textContent || ""),
    );
    if (isLive) {
      test.skip(true, "Duplicate-guard scroll/highlight is UI-only; demo mode is faster and deterministic.");
      return;
    }
    const input = page.locator(".chat-input");
    await input.fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await waitStreamDone(page);
    // Ask several follow-ups so the original question scrolls out of view
    // (off the top of the chat list).
    for (const q of [
      "How do I install the Codex CLI?",
      "How do I get a Gemini API key?",
      "Which Claude models are available in the API?",
    ]) {
      await input.fill(q);
      await page.keyboard.press("Enter");
      await waitStreamDone(page);
    }
    // Sanity: the first user question is now FULLY OFF the top of the
    // chat list viewport (bottom edge above the list's top edge). This
    // guarantees the next assertion actually exercises the scroll-up
    // path — checking only ``top < listTop`` would allow a partially-
    // visible original to pass the post-condition without any scroll
    // having fired.
    const original = page.locator("#cv-msg-0");
    const beforeRect = await original.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const list = document.getElementById("chat-list")!.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom, listTop: list.top };
    });
    expect(beforeRect.bottom).toBeLessThan(beforeRect.listTop);
    const beforeCount = await page.locator(".message.user-msg").count();
    // Re-ask the very first question
    await input.fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    // No new user bubble
    await expect(page.locator(".message.user-msg")).toHaveCount(beforeCount);
    // The ORIGINAL (first) bubble gets the pulse animation — the user said
    // "I asked this before", so we highlight THE QUESTION, not the answer.
    await expect
      .poll(
        async () => original.evaluate((el) => getComputedStyle(el).animationName),
        { timeout: 1500 },
      )
      .toBe("cv-pulse");
    // And it has been scrolled UP to the TOP of the chat list, not just
    // pushed somewhere inside the viewport. We assert the original's top
    // edge is within 20px of the list's top edge — that matches the
    // requested "scrolls to TOP of list" behavior. A weaker "inside
    // viewport" assertion would let a regression that scrolls to the
    // bottom of the list pass.
    await expect
      .poll(
        async () => {
          const rect = await original.evaluate((el) => {
            const r = el.getBoundingClientRect();
            const list = document.getElementById("chat-list")!.getBoundingClientRect();
            return {
              top: r.top,
              bottom: r.bottom,
              listTop: list.top,
              listBottom: list.bottom,
              distanceFromTop: Math.abs(r.top - list.top),
            };
          });
          return (
            rect.top >= rect.listTop - 20 &&
            rect.bottom <= rect.listBottom + 20 &&
            rect.distanceFromTop < 20
          );
        },
        { timeout: 2000 },
      )
      .toBe(true);
  });

  test("typing cadence: many short text updates per second (smooth, not bursty)", async ({ page }) => {
    // The earlier word-by-word implementation emitted one whole whitespace
    // token (sometimes > 10 chars) per ``delay`` tick, so the user saw a
    // burst → pause → burst pattern. The new char-by-char implementation
    // emits 2-3 chars every ~24ms which the eye reads as smooth.
    //
    // We assert on the *number of distinct text-content samples* observed
    // during a fixed observation window while a known-length canned answer
    // streams. With word-by-word bursts we'd see <= text.length "bursts";
    // with smooth per-char we see a sample per ~50ms tick (≥8 in 800ms).
    //
    // NOTE: this test is a DEMO-mode assertion. Live mode runs the same
    // ``streamBot`` path with the same ``streamText`` emitter, but the
    // upstream latency masks the cadence — we measure the *client* ticker,
    // not the wire, so demo mode is the cleanest signal. If live mode is
    // active we skip (the cadence is provably the same since both paths
    // share ``streamText``).
    await enterChat(page);
    const isLive = await page.evaluate(
      () => /LIVE/i.test(document.querySelector(".demo-badge")?.textContent || ""),
    );
    if (isLive) {
      test.skip(true, "Cadence test is demo-mode-only (live mode shares streamText but backend latency dominates the wall-clock signal).");
      return;
    }
    await page.locator(".chat-input").fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    // Wait for the bot bubble to appear, then sample its .streaming child
    // (the text node) for 1200ms. Sample every ~30ms — 2-3 chars/tick at
    // 24ms tick interval means consecutive samples will differ.
    await page.locator(".message.bot-msg .streaming").waitFor({ timeout: 5000 });
    const samples: string[] = await page.evaluate(async () => {
      const out: string[] = [];
      const el = document.querySelector(".message.bot-msg .streaming");
      const start = performance.now();
      while (performance.now() - start < 1200) {
        out.push((el?.textContent ?? "").trim());
        await new Promise((r) => setTimeout(r, 30));
      }
      return out;
    });
    const filled = samples.filter((s) => s.length > 0);
    const distinct = new Set(filled).size;
    // Smooth char-by-char emission produces ≥20 distinct prefixes in 1200ms;
    // a bursty word-by-word emitter produces ≤ text.length/avgTokenLen ≈ 8.
    expect(distinct).toBeGreaterThanOrEqual(15);
    // No single jump between consecutive samples exceeds the per-tick budget.
    let maxJump = 0;
    for (let i = 1; i < filled.length; i++) {
      maxJump = Math.max(maxJump, filled[i].length - filled[i - 1].length);
    }
    expect(maxJump).toBeLessThanOrEqual(6);
  });

  test("loading indicator: pending-bubble renders 3 dots + label when state.pending is true (live only)", async ({ page }) => {
    // The pending indicator is rendered when state.pending=true. That
    // only happens on the live path (sendLive sets it before the API
    // round-trip). In demo mode the answer streams instantly so the
    // indicator never appears. We assert the DOM by intercepting the
    // /v1/sessions/messages response with an artificial delay so the
    // indicator is observable.
    //
    // This test runs under ``playwright.live.config.ts`` (set via
    // ``VITE_LIVE_STUB=1`` to enable the in-process stub backend in
    // ``vite.liveStub.ts``, plus ``VITE_API_LIVE=true``). The
    // ``grep: /live only/i`` filter on that config restricts the run
    // to this test only. Under the demo config (``playwright.config.ts``)
    // the test self-skips via the ``!isLive`` guard below — that's the
    // intended behavior: the demo path is instant by design and
    // there's nothing to assert there.
    await enterChat(page);
    const isLive = await page.evaluate(
      () => /LIVE/i.test(document.querySelector(".demo-badge")?.textContent || ""),
    );
    if (!isLive) {
      test.skip(true, "Loading indicator is a live-path feature; demo is instant. Run via: VITE_LIVE_STUB=1 npx playwright test --config=playwright.live.config.ts");
      return;
    }
    // Throttle the API so we have a window to observe the pending bubble.
    // Two modes:
    //   - stub mode (VITE_LIVE_STUB=1): the dev server's vite.liveStub
    //     plugin serves /v1/sessions/*/messages in-process with a
    //     canned 800ms delay, so page.route() never matches (the
    //     request is answered by the dev server, not the network).
    //   - real-backend mode (VITE_API_LIVE=true without the stub):
    //     we delay the response at the browser level so the bubble
    //     becomes observable.
    //   In demo mode the test skips above, so this block only runs in
    //   live-ish configurations; if the stub isn't active, the route
    //   delay acts as the throttle.
    if (process.env.VITE_LIVE_STUB !== "1") {
      await page.route("**/v1/sessions/*/messages", async (route) => {
        await new Promise((r) => setTimeout(r, 800));
        await route.continue();
      });
    }
    await page.locator(".chat-input").fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await expect(page.locator(".pending-bubble")).toBeVisible({ timeout: 3000 });
    await expect(page.locator(".pending-label")).toHaveText("Searching the docs…");
    await expect(page.locator(".pending-dot")).toHaveCount(3);
    // Pulsing animation must be wired up.
    const anim = await page
      .locator(".pending-dot")
      .first()
      .evaluate((el) => getComputedStyle(el).animationName);
    expect(anim).toBe("cv-pending");
    // Once the API responds and streaming starts, pending goes away.
    await expect(page.locator(".pending-bubble")).toHaveCount(0, { timeout: 15000 });
  });

  test("autoscrolls: list stays pinned to the newest message", async ({ page }) => {
    await enterChat(page);
    const input = page.locator(".chat-input");
    for (const q of ["What is Claude Code?", "How do I install the Codex CLI?", "Which Claude models are available in the API?"]) {
      await input.fill(q);
      await page.keyboard.press("Enter");
      await waitStreamDone(page);
    }
    const atBottom = await page.evaluate(() => {
      const l = document.getElementById("chat-list")!;
      return Math.abs(l.scrollHeight - l.scrollTop - l.clientHeight) < 8;
    });
    expect(atBottom).toBe(true);
  });

  // Regression: an explicit send must bring the new question into view even when the
  // reader had scrolled up (previously there was NO scroll-on-send, so from a scrolled-up
  // position the new question rendered off-screen and the user couldn't tell it was answered).
  test("new question scrolls into view even when the reader has scrolled up", async ({ page }) => {
    await enterChat(page);
    const input = page.locator(".chat-input");
    for (const q of [
      "What is Claude Code?",
      "How do I install the Codex CLI?",
      "Which Claude models are available in the API?",
    ]) {
      await input.fill(q);
      await page.keyboard.press("Enter");
      await waitStreamDone(page);
    }
    // Scroll to the very top, then ask a NEW question.
    await page.evaluate(() => {
      document.getElementById("chat-list")!.scrollTop = 0;
    });
    await input.fill("What does the --model flag do in Codex?");
    await page.keyboard.press("Enter");
    // The list must jump to the new question (at/near the bottom), not stay at the top.
    await expect
      .poll(
        async () =>
          page.evaluate(() => {
            const l = document.getElementById("chat-list")!;
            return l.scrollHeight - l.scrollTop - l.clientHeight;
          }),
        { timeout: 5000 },
      )
      .toBeLessThan(80);
  });

  // Regression (#122): scrolling UP during streaming must hold position — the stick-to-bottom
  // latch disarms on the first upward gesture, so streamed chunks no longer snap the view back.
  test("scrolling up during streaming holds position (no snap-back)", async ({ page }) => {
    await enterChat(page);
    const input = page.locator(".chat-input");
    for (const q of ["What is Claude Code?", "How do I install the Codex CLI?"]) {
      await input.fill(q);
      await page.keyboard.press("Enter");
      await waitStreamDone(page);
    }
    // Ask one more; while its answer is still streaming, scroll up a small amount (the exact
    // band that the old 120px-slack autoscroll would have re-pinned on the next chunk).
    await input.fill("Which Claude models are available in the API?");
    await page.keyboard.press("Enter");
    await page.waitForSelector(".typing-cursor", { timeout: 5000 });
    // Scroll up a SMALL amount — well inside the old 120px slack band — so the old
    // slack-band autoscroll would reliably re-pin on the next chunk, while the new 8px
    // latch reliably disarms and holds. (A larger scroll could clear the old band on its
    // own and pass even on the buggy code.)
    await page.evaluate(() => {
      document.getElementById("chat-list")!.scrollBy(0, -40);
    });
    // Over the next ~700ms of streaming, the view must NOT be yanked back to the bottom
    // (content grows below the held viewport, so the distance only ever increases).
    await page.waitForTimeout(700);
    const distanceFromBottom = await page.evaluate(() => {
      const l = document.getElementById("chat-list")!;
      return l.scrollHeight - l.scrollTop - l.clientHeight;
    });
    expect(distanceFromBottom).toBeGreaterThan(20);
  });

  test("chat fits below the 64px header, max-width 820; back returns + scrolls top", async ({ page }) => {
    await enterChat(page);
    const main = page.locator('[data-screen-label="Chat"]');
    const box = await main.boundingBox();
    expect(box!.width).toBeLessThanOrEqual(820 + 1);
    await page.locator(".chat-input").fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await page.locator(".back-button").click();
    await expect(page.locator("#top")).toBeVisible();
    expect(await page.evaluate(() => window.scrollY)).toBeLessThan(5);
  });

  test("empty state shows CV mark, heading, 4 suggestions that send", async ({ page }) => {
    await enterChat(page);
    await expect(page.locator(".empty-state .logo-avatar")).toHaveText("CV");
    const suggestions = page.locator(".suggestion-btn");
    await expect(suggestions).toHaveCount(4);
    await suggestions.first().click();
    await expect(page.locator(".message.user-msg")).toHaveCount(1);
  });
});

// ---------------------------------------------------------------------------
// Dark-theme smoke (key flows also work in dark)
// ---------------------------------------------------------------------------
test.describe("Dark theme flows", () => {
  test("chat streaming + duplicate guard work in dark mode", async ({ page }) => {
    await ensureTheme(page, "dark");
    await enterChat(page);
    const input = page.locator(".chat-input");
    await input.fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await waitStreamDone(page);
    await expect(page.locator(".message.bot-msg .source-card").first()).toBeVisible();
    const count = await page.locator(".message.user-msg").count();
    await input.fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await expect(page.locator(".message.user-msg")).toHaveCount(count);
  });
});

// ---------------------------------------------------------------------------
// Accessibility / responsive housekeeping (§4)
// ---------------------------------------------------------------------------
test.describe("Reduced motion", () => {
  test("marquee, caret, shake are disabled under prefers-reduced-motion", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    // marquee paused/none
    const marquee = await page.locator(".ticker-track").evaluate((el) => {
      const cs = getComputedStyle(el);
      return cs.animationName === "none" || cs.animationPlayState === "paused";
    });
    expect(marquee).toBe(true);
    // step-01 caret animation disabled
    const caretAnim = await page.locator(".typing-caret").evaluate((el) => {
      const cs = getComputedStyle(el);
      return cs.animationName === "none" || cs.animationDuration === "0s";
    });
    expect(caretAnim).toBe(true);
  });
});

test.describe("Mobile", () => {
  test.use({ viewport: { width: 390, height: 844 } });
  test("hero collapses to a single column and primary CTAs are >=44px", async ({ page }) => {
    const cols = await page.locator(".hero-container").evaluate((el) => getComputedStyle(el).gridTemplateColumns);
    expect(cols.split(" ").length).toBe(1); // single track on mobile
    for (const sel of [".ask-button", ".cta-button"]) {
      const h = await page.locator(sel).first().evaluate((el) => el.getBoundingClientRect().height);
      expect(h, `${sel} touch target`).toBeGreaterThanOrEqual(44);
    }
  });

  test("every interactive control clears the 44px touch-target floor on mobile", async ({ page }) => {
    // Landing-view controls.
    for (const sel of [".nav-link", ".faq-toggle", ".ticker-chip", ".demo-question"]) {
      const h = await page.locator(sel).first().evaluate((el) => el.getBoundingClientRect().height);
      expect(h, `${sel} touch target`).toBeGreaterThanOrEqual(44);
    }
    // Chat-view control lives on the other screen.
    await enterChat(page);
    const sh = await page.locator(".suggestion-btn").first().evaluate((el) => el.getBoundingClientRect().height);
    expect(sh, ".suggestion-btn touch target").toBeGreaterThanOrEqual(44);
  });
});

// ---------------------------------------------------------------------------
// D2 hardening — streaming order, pulse count, hover, cadence, dedup, wiring
// ---------------------------------------------------------------------------
test.describe("Hero card sizing + cadence", () => {
  test("answer card holds a min-height so it can't collapse between empty and streamed states", async ({ page }) => {
    const h = await page.locator(".card-content").evaluate((el) => el.getBoundingClientRect().height);
    expect(h).toBeGreaterThanOrEqual(322);
  });

  test("placeholder cadence brackets the ~3.2s interval", async ({ page }) => {
    const input = page.locator("#hero-input");
    // Sync to a change boundary so we measure a full cycle, not a partial one.
    const start = (await input.getAttribute("placeholder"))!;
    await expect.poll(async () => input.getAttribute("placeholder"), { timeout: 6000 }).not.toBe(start);
    const base = (await input.getAttribute("placeholder"))!;
    // Well before the next tick (~3.2s away) it is unchanged.
    await page.waitForTimeout(1500);
    expect(await input.getAttribute("placeholder")).toBe(base);
    // Past the 3.2s boundary it has advanced.
    await expect.poll(async () => input.getAttribute("placeholder"), { timeout: 3000 }).not.toBe(base);
  });
});

test.describe("Streaming order + duplicate pulse", () => {
  test("bot answer streams incrementally; sources appear only after the caret is gone", async ({ page }) => {
    await enterChat(page);
    await page.locator(".chat-input").fill("How does Claude Code work?");
    await page.keyboard.press("Enter");
    // Catch a mid-stream frame: partial text present, caret present, zero sources.
    const partialLen = (await (await page.waitForFunction(() => {
      const content = document.querySelector(".message.bot-msg .content");
      const cursor = document.querySelector(".message.bot-msg .typing-cursor");
      const sources = document.querySelectorAll(".message.bot-msg .source-card").length;
      const len = content ? (content.textContent || "").trim().length : 0;
      return cursor && sources === 0 && len > 0 ? len : false;
    }, { timeout: 8000 })).jsonValue()) as number;
    expect(partialLen).toBeGreaterThan(0);
    await waitStreamDone(page);
    const finalLen = (await page.locator(".message.bot-msg .content").innerText()).trim().length;
    expect(finalLen).toBeGreaterThan(partialLen); // grew incrementally, not 0→full
    expect(await page.locator(".message.bot-msg .source-card").count()).toBeGreaterThan(0);
  });

  test("duplicate re-ask pulses exactly 3 times, and re-fires on a second duplicate", async ({ page }) => {
    await enterChat(page);
    const input = page.locator(".chat-input");
    await input.fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await waitStreamDone(page);
    // A newer message so #cv-msg-0 is no longer the newest.
    await input.fill("How do I install the Codex CLI?");
    await page.keyboard.press("Enter");
    await waitStreamDone(page);
    const original = page.locator("#cv-msg-0");

    await input.fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await expect.poll(async () => original.evaluate((el) => getComputedStyle(el).animationName)).toBe("cv-pulse");
    expect(await original.evaluate((el) => getComputedStyle(el).animationIterationCount)).toBe("3");
    // Pulse clears after the ~2.1s highlight reset.
    await expect.poll(async () => original.evaluate((el) => getComputedStyle(el).animationName), { timeout: 4000 }).toBe("none");
    // Second duplicate (case-insensitive) → the pulse re-fires (guards flashExisting reset-to-restart).
    await input.fill("what is claude code?");
    await page.keyboard.press("Enter");
    await expect.poll(async () => original.evaluate((el) => getComputedStyle(el).animationName)).toBe("cv-pulse");
  });
});

test.describe("Marquee structure + card hover", () => {
  test("marquee iterates infinitely and the second half duplicates the first", async ({ page }) => {
    const track = page.locator(".ticker-track");
    expect(await track.evaluate((el) => getComputedStyle(el).animationIterationCount)).toBe("infinite");
    const labels = await page.locator(".ticker-chip").allInnerTexts();
    expect(labels).toHaveLength(16);
    expect(labels.slice(0, 8)).toEqual(labels.slice(8, 16));
  });

  test("persona/step/feature cards lift on hover and settle back on unhover", async ({ page }) => {
    const translateY = (t: string) => (t === "none" ? 0 : parseFloat(t.slice(0, -1).split(",").pop()!));
    for (const sel of [".persona-card", ".step-card", ".feature-card"]) {
      const card = page.locator(sel).first();
      await card.scrollIntoViewIfNeeded();
      await card.hover();
      // Poll the SETTLED translateY (the .18s transition starts at identity, so a
      // bare not-"none" check can read the mid-transition matrix(…,0)).
      await expect
        .poll(async () => translateY(await card.evaluate((el) => getComputedStyle(el).transform)))
        .toBeLessThan(-1); // ≈ translateY(-3px)
      // Unhover: park the pointer off the card; transform returns to identity.
      await page.mouse.move(2, 2);
      await expect.poll(async () => card.evaluate((el) => getComputedStyle(el).transform)).toBe("none");
    }
  });
});

test.describe("Wiring: suggestions + personas send their own label", () => {
  for (let i = 0; i < 4; i++) {
    test(`chat suggestion ${i} sends exactly its own label`, async ({ page }) => {
      await enterChat(page);
      const btn = page.locator(".suggestion-btn").nth(i);
      const label = (await btn.innerText()).trim();
      await btn.click();
      await expect(page.locator(".message.user-msg")).toHaveText(label);
    });
  }

  for (let c = 0; c < 3; c++) {
    for (let b = 0; b < 2; b++) {
      test(`persona card ${c} button ${b} asks exactly its own question`, async ({ page }) => {
        const btn = page.locator(".persona-card").nth(c).locator(".persona-q-btn").nth(b);
        const q = (await btn.locator("span").first().innerText()).trim();
        await btn.click();
        await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
        await expect(page.locator(".message.user-msg").first()).toHaveText(q);
      });
    }
  }
});

test.describe("Chat composer + refusal/dedup content", () => {
  test("send button sends on click and is a no-op on empty input", async ({ page }) => {
    await enterChat(page);
    const input = page.locator(".chat-input");
    await input.fill("What is Claude Code?");
    await page.locator(".send-button").click();
    await expect(page.locator(".message.user-msg")).toHaveCount(1);
    await waitStreamDone(page);
    const before = await page.locator(".message.user-msg").count();
    await input.fill("");
    await page.locator(".send-button").click();
    await page.waitForTimeout(250);
    expect(await page.locator(".message.user-msg").count()).toBe(before);
  });

  test("refusal shows the exact badge label + a non-empty body; dedup is case/whitespace-insensitive", async ({ page }) => {
    await enterChat(page);
    const input = page.locator(".chat-input");
    await input.fill("What is the best laptop to buy?");
    await page.keyboard.press("Enter");
    await waitStreamDone(page);
    await expect(page.locator(".message.bot-msg .refusal-badge")).toHaveText("⚠ NO SOURCE — REFUSED");
    expect((await page.locator(".message.bot-msg .content").innerText())).toContain("I can answer questions about");

    await input.fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await waitStreamDone(page);
    const before = await page.locator(".message.user-msg").count();
    // Same question with different case + surrounding whitespace → no new bubble.
    await input.fill("   what is claude code?   ");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(300);
    expect(await page.locator(".message.user-msg").count()).toBe(before);
  });

  test("Get Pro twice adds only one Pro user bubble", async ({ page }) => {
    await page.locator(".pricing-card.featured .cta").click();
    await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
    await waitStreamDone(page);
    await expect(page.locator(".message.user-msg")).toHaveCount(1);
    // Back to landing and hit Get Pro again — the duplicate guard keeps it at one.
    await page.locator(".back-button").click();
    await page.locator(".pricing-card.featured .cta").click();
    await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
    await page.waitForTimeout(300);
    await expect(
      page.locator(".message.user-msg", { hasText: "What do I get with CiteVyn Pro?" })
    ).toHaveCount(1);
  });
});

test.describe("Reduced motion (full sweep)", () => {
  test("shake, cv-pulse, and the typing caret are all disabled under prefers-reduced-motion", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    // Caret: landing.css sets animation:none under reduced motion.
    expect(await page.locator(".typing-caret").evaluate((el) => getComputedStyle(el).animationName)).toBe("none");
    // Empty-ask shake: duration collapsed to ~0 (reset.css blanket rule).
    await page.locator(".ask-button").click();
    const box = page.locator(".hero-input-box");
    await expect(box).toHaveClass(/shake/);
    expect(parseFloat(await box.evaluate((el) => getComputedStyle(el).animationDuration))).toBeLessThan(0.05);
    // Duplicate pulse: duration collapsed to ~0.
    await enterChat(page);
    const input = page.locator(".chat-input");
    await input.fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await waitStreamDone(page);
    await input.fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await expect.poll(async () =>
      page.locator("#cv-msg-0").evaluate((el) => getComputedStyle(el).animationName)
    ).toBe("cv-pulse");
    expect(parseFloat(await page.locator("#cv-msg-0").evaluate((el) => getComputedStyle(el).animationDuration))).toBeLessThan(0.05);
  });
});

test.describe("Dark-theme parity for previously light-only flows", () => {
  test("demo refusal badge is amber with zero sources in dark mode", async ({ page }) => {
    await ensureTheme(page, "dark");
    await page.locator(".demo-question").nth(3).click(); // laptop (refusal)
    await waitStreamDone(page);
    const badge = page.locator(".demo-right .refusal-badge");
    await expect(badge).toBeVisible();
    expect(await badge.evaluate((el) => getComputedStyle(el).color)).toBe(SEMANTIC.amber);
    expect(await page.locator(".demo-right .source-card").count()).toBe(0);
  });

  test("hero empty-ask amber border resolves on the dark input box", async ({ page }) => {
    await ensureTheme(page, "dark");
    await page.locator(".ask-button").click();
    const box = page.locator(".hero-input-box");
    await expect(box).toHaveClass(/shake/);
    await expect.poll(async () => box.evaluate((el) => getComputedStyle(el).borderColor)).toBe(SEMANTIC.amber);
  });

  test("sourced demo answer shows readable source cards in dark", async ({ page }) => {
    await ensureTheme(page, "dark");
    await page.locator(".demo-question").nth(1).click(); // codex-flag (sourced)
    await waitStreamDone(page);
    await expect(page.locator(".demo-right .source-card").first()).toBeVisible();
    // Title reads on the dark surface as --ink; number badge stays dark-on-yellow.
    expect(await page.locator(".demo-right .source-title").first().evaluate((el) => getComputedStyle(el).color)).toBe(TOKENS.dark.ink);
    expect(await page.locator(".demo-right .source-number").first().evaluate((el) => getComputedStyle(el).color)).toBe(TOKENS.light.ink); // --hl-ink
  });

  test("Get-Pro flow streams the honest not-live reply in dark", async ({ page }) => {
    await ensureTheme(page, "dark");
    await page.locator(".pricing-card.featured .cta").click();
    await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
    await expect(page.locator(".message.user-msg")).toHaveText("What do I get with CiteVyn Pro?");
    await waitStreamDone(page);
    await expect(page.locator(".message.bot-msg")).toContainText("Pro isn't live yet");
  });
});

test.describe("Timer cleanup / no post-unmount state updates", () => {
  test("leaving a streaming chat mid-stream raises no console/page errors", async ({ page }) => {
    // The hook's streamBot schedules scroll timeouts + a streaming interval that
    // reference #chat-list. Bailing back to landing tears ChatView down while
    // those timers are still pending — the guards must keep them from firing into
    // a torn-down view (no "Cannot read properties of null", no React warning).
    const errors: string[] = [];
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    page.on("pageerror", (e) => errors.push(String(e)));

    // Distinct questions so each one actually streams (dedup would suppress a repeat).
    const qs = [
      "How does Claude Code work?",
      "How do I install the Codex CLI?",
      "How do I get a Gemini API key?",
    ];
    for (const q of qs) {
      await enterChat(page);
      await page.locator(".chat-input").fill(q);
      await page.keyboard.press("Enter");
      // A prior iteration's stream may still be running (its interval survives the
      // view switch — exactly the leak we're probing), so target the NEWEST cursor.
      await expect(page.locator(".message.bot-msg .typing-cursor").last()).toBeVisible();
      await page.locator(".back-button").click(); // unmount ChatView mid-stream
      await expect(page.locator("#top")).toBeVisible();
      await page.waitForTimeout(400); // let the orphaned timers fire
    }
    expect(errors, errors.join("\n")).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Landing UX regressions (composer reset + sources strip layout)
// ---------------------------------------------------------------------------
test.describe("Landing UX regressions", () => {
  test("hero composer clears after asking and returning to landing", async ({ page }) => {
    const hero = page.locator("#hero-input");
    await hero.fill("What is Claude Code?");
    await page.locator(".ask-button").click();
    await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
    await page.locator(".back-button").click();
    // Back on the landing page, the hero box must be EMPTY — not still holding the
    // question the user already asked (which they would otherwise have to delete).
    await expect(page.locator("#hero-input")).toHaveValue("");
  });

  test("sources-strip label is padded, not flush against the left edge", async ({ page }) => {
    const label = page.locator(".sources-strip .mono-label");
    await label.scrollIntoViewIfNeeded();
    const box = await label.boundingBox();
    // With the .sources-strip-inner wrapper restored, the label sits inside 28px of
    // horizontal padding rather than jammed at x≈0 (the cut-off symptom).
    expect(box!.x).toBeGreaterThan(16);
  });
});
