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

  test("duplicate question: no new bubble, scrolls to original, pulses (cv-pulse)", async ({ page }) => {
    await enterChat(page);
    const input = page.locator(".chat-input");
    await input.fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    await waitStreamDone(page);
    // add a few more so the original scrolls out of view
    for (const q of ["How do I install the Codex CLI?", "How do I get a Gemini API key?"]) {
      await input.fill(q);
      await page.keyboard.press("Enter");
      await waitStreamDone(page);
    }
    const before = await page.locator(".message.user-msg").count();
    // re-ask the very first question
    await input.fill("What is Claude Code?");
    await page.keyboard.press("Enter");
    // no new user bubble
    await expect(page.locator(".message.user-msg")).toHaveCount(before);
    // the ORIGINAL (first) bubble gets the pulse animation
    const original = page.locator("#cv-msg-0");
    await expect.poll(async () =>
      original.evaluate((el) => getComputedStyle(el).animationName)
    ).toBe("cv-pulse");
    // and it is scrolled into view within the list
    const visible = await original.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const list = document.getElementById("chat-list")!.getBoundingClientRect();
      return r.top >= list.top - 4 && r.bottom <= list.bottom + 4;
    });
    expect(visible).toBe(true);
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
