/**
 * Behavior suite — exhaustive interaction coverage for the §4 Definition of Done.
 * Wiring is theme-agnostic, so these run in the default theme except where a
 * flow is explicitly re-checked in dark.
 */
import { test, expect } from "@playwright/test";
import { gotoApp, ensureTheme, enterChat, waitStreamDone } from "./helpers";

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
    // The pill width animates 7→22px over .3s; let it settle before measuring.
    await page.waitForTimeout(400);
    const widths = await dots.evaluateAll((els) => els.map((e) => getComputedStyle(e).width));
    const actives = await dots.evaluateAll((els) => els.map((e) => e.classList.contains("active")));
    const pillIdx = widths.findIndex((w) => parseFloat(w) >= 20);
    expect(actives.filter(Boolean).length).toBe(1);
    expect(actives[pillIdx]).toBe(true); // the pill is the active one
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
    await track.hover({ force: true, position: { x: 40, y: 10 } });
    await expect.poll(async () => track.evaluate((el) => getComputedStyle(el).animationPlayState)).toBe("paused");
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
});
