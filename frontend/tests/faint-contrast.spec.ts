/**
 * #396 — the half that asserts WHAT THE CONSUMER RECEIVES.
 *
 * `scripts/faint-contrast.test.mjs` reads the stylesheet and the sources. This
 * file reads the rendered page, and it is the only half that can see RENDERED
 * COLOUR — which is what the following two shapes come down to:
 *
 *   1. INLINE STYLES. `Hero.tsx` set `color: "var(--faint)"` on the `▸` glyph
 *      and on the visible `TRY:` label. An inline style beats every stylesheet
 *      rule, so every CSS-ONLY sweep of this token — including the one in the
 *      issue — reported the file clean while those two shipped the defect.
 *      NOT "the only half that would have caught it", which an earlier draft
 *      of this comment and of the CHANGELOG both claimed: reverting one of
 *      those two inline styles was measured, and the STATIC half goes red as
 *      well, on `no .tsx/.ts source carries an inline var(--faint)`. It sees
 *      them by scanning the SOURCE, not by rendering. What is genuinely
 *      exclusive to this file is everything below.
 *   2. INHERITED FONT SIZES, the REAL painted backdrop, and the EFFECTIVE
 *      paint colour. `.chat-input::placeholder` and `.generic-avatar` set no
 *      `font-size`; `.step-number` paints on `.step-card`'s `--surface`, not on
 *      the `--surface-2` inset panel a reader of the CSS would assume; and a
 *      value built at runtime — a `color-mix`, a raw hex, an alias of the same
 *      rgb under another token name — never appears in any source as the string
 *      the static scan looks for. Only the cascade knows.
 *
 * THE MEASUREMENT. `--faint`'s best light-theme ratio against any surface it
 * ever painted on is 2.92:1 (on `--surface`; 2.77 on `--bg`, 2.58 on
 * `--surface-2`) — under even WCAG's relaxed 3:1 LARGE-TEXT floor, so the token
 * is illegal for text at every size and weight, and the sweep needs no size
 * condition. Both tokens are RESOLVED AT RUNTIME from a probe element rather
 * than hardcoded as rgb strings, so retuning a token moves this guard with it.
 *
 * WHAT THIS GUARD CANNOT SEE — stated because a guard that reads as exhaustive
 * and is not is worse than no guard:
 *   - `/about`. It is a static page outside the SPA and this suite never
 *     navigates to it. `public/about.css` is covered by the STATIC half instead.
 *   - Signed-in surfaces. The demo config runs with `VITE_API_LIVE: "false"`;
 *     `AccountMenu`, `AuthModal`, `HistoryDrawer` and `ConnectedAccountsDrawer`
 *     are not opened here.
 *   - States not reachable in demo mode: the pending bubble (demo answers are
 *     synchronous), the live-answer suggestion list, the hero card's later
 *     animation frames, error and refusal chrome.
 *   - Backdrops that are not a flat `background-color`. The compositor below
 *     walks `background-color` up the ancestor chain, so a gradient, an image,
 *     a painting `::before`/`::after` overlay, an ancestor `opacity`, a
 *     `filter` or a `backdrop-filter` would make its answer wrong — the exact
 *     trap `helpers.ts`'s focus-ring comment records. That is why the ratio
 *     assertions ASSERT THE PRECONDITION rather than assuming it: any such
 *     ancestor over a measured element fails the test by name instead of being
 *     silently mis-measured. `opaqueBlockers` reports all six shapes, and each
 *     one is planted and required to be reported below — including the
 *     pseudo-element pass, which an earlier draft's docstring CLAIMED to do
 *     while only ever calling `getComputedStyle(n)` with no pseudo argument.
 *     This app has that exact shape live: `.ticker-strip::before/::after` are
 *     80px gradients that `landing.css` records as having painted over a focus
 *     ring at 1.47:1.
 *   - Z-ORDER. `opaqueBlockers` reports a painting pseudo-element wherever it
 *     sits in the ancestor chain; it does not work out whether that paint
 *     actually lands on top of the measured glyphs. It over-reports rather
 *     than under-reports, which fails closed.
 *
 * ONE LINE PER TEST names the change that turns it red.
 */
import { test, expect } from "./fixtures";
import { ensureTheme, enterChat, gotoApp, resolveColor, type ThemeName } from "./helpers";
// The page-side measurement kit moved to `contrast-kit.ts` when #403 needed the
// SAME compositor for the contrast-FLOOR sweep. One implementation, one place a
// mis-measurement can be fixed; this file kept every one of its assertions.
import { installKit, paintedRatio } from "./contrast-kit";

const THEMES: ThemeName[] = ["light", "dark"];

/**
 * ONE FLAT FLOOR of 4.5:1, applied to every probe.
 *
 * That is WCAG 1.4.3 AA for small text, and it is deliberately STRICTER than
 * WCAG for the one probe that is not small: `.step-number` is 24px at weight
 * 700, which qualifies as large text and would only owe 3:1. An earlier
 * version of this comment claimed "everything measured here is small text",
 * which the probe list itself contradicts twenty lines later.
 *
 * A flat floor is used anyway, for two reasons. `.step-number` clears 4.5:1 on
 * `--muted` regardless (measured), so the strictness costs nothing today. And a
 * size-conditional floor would have to resolve inherited sizes to know which
 * branch to take — `.chat-input::placeholder` and `.generic-avatar` set no
 * `font-size` of their own — which is the same blind spot the static half's ban
 * drops the size condition to avoid.
 */
const AA_SMALL_TEXT = 4.5;

/**
 * The narrow viewport the second sweep runs at.
 *
 * WITHOUT IT, `@media (max-width: …)` rules were UNREACHABLE by this file, and
 * that was a total bypass, measured: `.composer-hint { color: VAR(--faint); }`
 * added inside `landing.css`'s existing `@media (max-width: 600px)` block left
 * the static suite, this suite, `fidelity.spec.ts` and stylelint all green
 * while every phone visitor rendered the #396 defect. Two independent causes,
 * and this is the second one — the first was the case-sensitive `var` match in
 * `scripts/faint-contrast.mjs`.
 *
 * 390x844 is iPhone 14 metrics; what matters is only that it is under BOTH of
 * this stylesheet's breakpoints (900px and 600px), which the sweep asserts
 * rather than assumes.
 */
const PHONE = { width: 390, height: 844 };

type Probe = { selector: string; why: string };

/**
 * Is `colour` the OPAQUE token `want`, channel for channel?
 *
 * A separate named predicate rather than an inline comparison so the alpha
 * clause has somewhere to be tested. Inline, it was an EQUIVALENT mutation:
 * nothing measured on this page is translucent today, so weakening the alpha
 * test changed no result, and a mutant that removes it would have survived
 * silently while the RED-WHEN line above it implied otherwise.
 *
 * `Math.round(0.5)` is 1, so alpha must NOT be folded into the rounded rgb
 * comparison — a half-transparent `color-mix(… transparent)` would read as the
 * opaque token. `.cta-footnote` in this stylesheet is that shape.
 */
function isOpaqueToken(colour: readonly number[] | null, want: readonly number[]): boolean {
  if (!colour) return false;
  const rgbMatches =
    colour.slice(0, 3).map(Math.round).join() === want.slice(0, 3).map(Math.round).join();
  const alphaMatches = Math.abs((colour[3] ?? 1) - 1) < 0.01;
  return rgbMatches && alphaMatches;
}

/**
 * Rules #396 moved off `--faint`, restricted to the ones RELIABLY PRESENT on a
 * demo-mode landing page. A selector that matches nothing FAILS below by name,
 * so this list cannot quietly empty itself when a class is renamed — but that
 * also means a genuinely conditional selector does not belong in it.
 * `.source-url`, `.card-title`, `.auto-badge`, `.progress-text`,
 * `.suggestions-label`, `.suggestion-url` and `.pending-bubble` are therefore
 * absent: they appear only in particular hero-animation frames or live-mode
 * states. The whole-page `--faint` sweep still covers them whenever they exist.
 */
const LANDING_PROBES: Probe[] = [
  { selector: ".hero-input-box > span:first-child", why: "the ▸ glyph, an INLINE style" },
  { selector: ".hero-chips > span:first-child", why: "the TRY: label, an INLINE style" },
  { selector: ".shortcut-badge", why: "the / hint in the hero box" },
  { selector: ".mono-label", why: "every section kicker" },
  { selector: ".persona-tag", why: "the persona card tags" },
  { selector: ".step-number", why: "24px/700 — large text, and STILL 2.92:1 on --surface" },
  { selector: ".typing-prompt", why: "the › in the step-1 preview" },
  { selector: ".doc-page", why: "the page number in the step-2 preview" },
  { selector: ".not-covered", why: "the 'not covered' line in the step-3 preview" },
  { selector: ".generic-avatar", why: "no font-size of its own — inherits 12px" },
  { selector: ".unit", why: "the pricing unit suffix" },
  { selector: ".demo-q-tag", why: "the demo question tags" },
  { selector: ".mvp-badge", why: "the footer PREVIEW pill" },
  { selector: ".footer-meta", why: "the footer meta row" },
  { selector: ".footer-copy", why: "the footer copyright line" },
];

const CHAT_PROBES: Probe[] = [
  { selector: ".demo-badge", why: "the demo-mode badge in the chat header" },
  { selector: ".composer-prompt", why: "the › chevron — decoration exemption DECLINED" },
  { selector: ".composer-hint", why: "the rule #396 was filed against" },
];

for (const theme of THEMES) {
  test.describe(`[${theme}] --faint colours no text (#396)`, () => {
    test.beforeEach(async ({ page }) => {
      await installKit(page);
      await gotoApp(page);
      await ensureTheme(page, theme);
      // The below-the-fold strip is lazily mounted (#358). Without this, half
      // the probe list matches zero elements and the sweep never sees the
      // footer at all.
      await page.locator("#pricing").scrollIntoViewIfNeeded();
      await expect(page.locator("footer")).toBeVisible();
      await page.evaluate(() => window.scrollTo(0, 0));
    });

    test("not one element on the landing page renders --faint", async ({ page }) => {
      // THE CHECK THAT WOULD HAVE CAUGHT Hero.tsx. It reads the RENDERED
      // colour, so it does not care whether the value came from a stylesheet,
      // an inline `style` attribute, or a pseudo-element.
      // RED WHEN: either `color: "var(--muted)"` in Hero.tsx goes back to
      // `var(--faint)` (verified: both offenders are named), or any
      // `landing.css` rule does.
      const faint = await resolveColor(page, "var(--faint)");
      const faintRgb = await page.evaluate((c) => window.__cv396.parse(c), faint);
      expect(faintRgb, `--faint (${faint}) did not parse`).not.toBeNull();

      const { examined, offenders } = await page.evaluate(
        (rgb) => window.__cv396.sweep(rgb),
        faintRgb!.map(Math.round),
      );
      // NON-VACUITY, derived not constant: a sweep that walked nothing reports
      // zero offenders and is indistinguishable from a clean page.
      expect(examined, "the sweep examined nothing — it is not measuring the page").toBeGreaterThan(
        200,
      );
      expect(offenders, `these render --faint (${faint}), which clears no WCAG text floor`).toEqual(
        [],
      );
    });

    test("not one element in the chat view renders --faint, ::placeholder included", async ({
      page,
    }) => {
      // `.chat-input::placeholder` sets no font-size of its own; only the
      // rendered pseudo-element can be asked what colour it is.
      // RED WHEN: `.chat-input::placeholder` goes back to `var(--faint)`.
      await enterChat(page);
      const faint = await resolveColor(page, "var(--faint)");
      const faintRgb = await page.evaluate((c) => window.__cv396.parse(c), faint);
      const { examined, offenders } = await page.evaluate(
        (rgb) => window.__cv396.sweep(rgb),
        faintRgb!.map(Math.round),
      );
      expect(examined).toBeGreaterThan(50);
      expect(offenders).toEqual([]);

      // The placeholder is REACHED by the sweep, not merely absent from it —
      // otherwise "no offender" would also be the answer if pseudo-elements
      // were never visited.
      const ph = await page.evaluate(() =>
        window.__cv396.colorOf(".chat-input", 0, "::placeholder"),
      );
      expect(ph, "::placeholder has no computed colour — the sweep's pseudo pass is dead").not.toBeNull();
    });

    for (const [where, probes] of [
      ["landing", LANDING_PROBES],
      ["chat", CHAT_PROBES],
    ] as const) {
      test(`every moved ${where} selector is --muted and clears 4.5:1 on its real backdrop`, async ({
        page,
      }, testInfo) => {
        // RED WHEN: any listed selector is reverted to `var(--faint)` (the
        // colour assertion fires), or a class in the list is renamed (the
        // zero-match assertion fires by name).
        if (where === "chat") await enterChat(page);

        const muted = await resolveColor(page, "var(--muted)");
        const mutedRgb = (await page.evaluate((c) => window.__cv396.parse(c), muted))!.map(
          Math.round,
        );

        const missing: string[] = [];
        const wrongColour: string[] = [];
        const tooLow: string[] = [];
        const blocked: string[] = [];
        const report: Record<string, string> = {};

        for (const { selector, why } of probes) {
          const n = await page.evaluate((s) => window.__cv396.count(s), selector);
          if (n === 0) {
            // A renamed class must FAIL, not silently empty the sweep.
            missing.push(`${selector} (${why})`);
            continue;
          }
          for (let i = 0; i < n; i++) {
            const paint = await page.evaluate(
              ([s, idx]) => window.__cv396.paintOf(s as string, idx as number),
              [selector, i] as const,
            );
            // The EFFECTIVE painted colour, not `color`. When
            // `-webkit-text-fill-color` is unset Chromium resolves it to
            // `color` and the two are identical; when it is set it WINS, and
            // reading `color` here would score a glyph that is not on screen.
            const colour = paint?.fill ?? paint?.color ?? null;
            if (!isOpaqueToken(colour, mutedRgb)) {
              wrongColour.push(
                `${selector}[${i}] is ${colour ? `rgba(${colour.slice(0, 3).map(Math.round).join(", ")}, ${(colour[3] ?? 1).toFixed(3)})` : "unparseable"}, want ${muted} at alpha 1`,
              );
              continue;
            }
            const blockers = await page.evaluate(
              ([s, idx]) => window.__cv396.opaqueBlockers(s as string, idx as number),
              [selector, i] as const,
            );
            if (blockers.length) {
              // The compositor below only models flat `background-color`. An
              // ancestor it cannot model is reported, never silently averaged
              // into a confident wrong number.
              blocked.push(`${selector}[${i}] sits under ${blockers.join("; ")}`);
              continue;
            }
            const bg = await page.evaluate(
              ([s, idx]) => window.__cv396.backdrop(s as string, idx as number),
              [selector, i] as const,
            );
            // `paintedRatio`, not `contrastRatio` on the bare rgb: it
            // composites a translucent text colour onto its backdrop first.
            // `colour` is non-null past `isOpaqueToken`, which rejects null.
            const ratio = paintedRatio(colour!, bg!);
            report[`${selector}[${i}]`] = `${ratio.toFixed(2)}:1 on rgb(${bg!.join(", ")})`;
            if (ratio < AA_SMALL_TEXT) {
              tooLow.push(`${selector}[${i}] ${ratio.toFixed(2)}:1 on rgb(${bg!.join(", ")})`);
            }
          }
        }

        await testInfo.attach(`ratios-${where}-${theme}`, {
          body: JSON.stringify(report, null, 2),
          contentType: "application/json",
        });

        expect(missing, "these selectors matched ZERO elements, so they measured nothing").toEqual(
          [],
        );
        expect(wrongColour, "these did not move to --muted").toEqual([]);
        expect(blocked, "the composite cannot be trusted over these ancestors").toEqual([]);
        expect(tooLow, `below WCAG AA ${AA_SMALL_TEXT}:1`).toEqual([]);
        // Non-vacuity for the ratio loop itself: it must have measured
        // something, or every list above is empty for the wrong reason.
        expect(Object.keys(report).length).toBeGreaterThanOrEqual(probes.length);
      });
    }

    test("the sweep can still SEE a --faint element when one exists", async ({ page }) => {
      // The detector-still-works partner. Three green "no offenders" results
      // above are indistinguishable from a sweep that returns [] unconditionally
      // — this plants one real offender and requires it to be named.
      // RED WHEN: `sweep` stops comparing colours, or `same()` always returns
      // false.
      const faint = await resolveColor(page, "var(--faint)");
      const faintRgb = (await page.evaluate((c) => window.__cv396.parse(c), faint))!.map(Math.round);

      await page.evaluate(() => {
        const el = document.createElement("p");
        el.className = "cv396-planted-offender";
        el.textContent = "planted";
        el.style.color = "var(--faint)";
        document.body.appendChild(el);
      });
      const withPlant = await page.evaluate((rgb) => window.__cv396.sweep(rgb), faintRgb);
      expect(withPlant.offenders.join(" | ")).toContain("cv396-planted-offender");

      await page.evaluate(() => document.querySelector(".cv396-planted-offender")?.remove());
      const after = await page.evaluate((rgb) => window.__cv396.sweep(rgb), faintRgb);
      expect(after.offenders).toEqual([]);
    });

    test("the sweep sees -webkit-text-fill-color, which OVERRIDES color on the glyphs", async ({
      page,
    }) => {
      // THE BYPASS THIS EXISTS FOR, measured before it was closed. Applying
      // `-webkit-text-fill-color: var(--faint)` to `.composer-hint` left
      // `getComputedStyle(el).color` reading rgb(107, 104, 98) — 5.27:1, which
      // is what this sweep used to read — while `webkitTextFillColor` read
      // rgb(154, 151, 143), so the glyphs painted at 2.77:1. The static suite
      // (23 passed), this suite (16 passed) and `npm run lint:css` (exit 0)
      // were ALL green on it.
      // RED WHEN: the `s.webkitTextFillColor` branch is dropped from `sweep`.
      const faint = await resolveColor(page, "var(--faint)");
      const faintRgb = (await page.evaluate((c) => window.__cv396.parse(c), faint))!.map(Math.round);

      const before = await page.evaluate((rgb) => window.__cv396.sweep(rgb), faintRgb);
      expect(before.offenders).toEqual([]);

      const readings = await page.evaluate(() => {
        const el = document.createElement("p");
        el.className = "cv396-fill-offender";
        el.textContent = "planted";
        // `color` stays on the LEGIBLE token. Only the fill is faint, so
        // nothing but the text-fill pass can find this element.
        el.style.color = "var(--muted)";
        el.style.webkitTextFillColor = "var(--faint)";
        document.body.appendChild(el);
        const s = getComputedStyle(el);
        return { color: s.color, fill: s.webkitTextFillColor };
      });
      // The precondition, asserted rather than assumed: if the browser had
      // refused the property the plant would be a no-op and the assertion
      // below would pass for the wrong reason.
      expect(readings.color, "the plant's `color` was not left legible").not.toBe(readings.fill);

      const withPlant = await page.evaluate((rgb) => window.__cv396.sweep(rgb), faintRgb);
      expect(withPlant.offenders.join(" | ")).toContain("cv396-fill-offender");
      expect(withPlant.offenders.join(" | ")).toContain("-webkit-text-fill-color");

      await page.evaluate(() => document.querySelector(".cv396-fill-offender")?.remove());
      expect((await page.evaluate((rgb) => window.__cv396.sweep(rgb), faintRgb)).offenders).toEqual(
        [],
      );
    });

    test("not one element renders --faint at a PHONE viewport either", async ({ page }) => {
      // THE OTHER TOTAL BYPASS, measured before it was closed. This suite only
      // ever ran at the desktop viewport, so `@media (max-width: …)` rules
      // were unreachable: `.composer-hint { color: VAR(--faint); }` added
      // inside `landing.css`'s `@media (max-width: 600px)` block left the
      // static suite (23 passed), this suite (16 passed), `fidelity.spec.ts`
      // (40 passed) and stylelint (exit 0) all green.
      // RED WHEN: a `--faint` colour is added to either `@media (max-width: …)`
      // block in `landing.css` — verified with `.composer-hint` in the 600px
      // one, in the uppercase `VAR()` form the static half now also catches.
      await page.setViewportSize(PHONE);
      // NON-VACUITY, and the thing that makes this test different from the
      // desktop sweeps rather than a slower copy of them: both breakpoints
      // must actually be ACTIVE, and one mobile-only declaration must actually
      // have taken effect. Without this, a viewport change that silently
      // stopped applying would leave three identical sweeps.
      const active = await page.evaluate(() => ({
        under900: matchMedia("(max-width: 900px)").matches,
        under600: matchMedia("(max-width: 600px)").matches,
        heroDescription: getComputedStyle(document.querySelector(".hero-description")!).fontSize,
      }));
      expect(active.under900, "the 900px breakpoint is not active at this viewport").toBe(true);
      expect(active.under600, "the 600px breakpoint is not active at this viewport").toBe(true);
      // 19px is the desktop rule; the 600px block overrides it to 16px.
      expect(active.heroDescription, "the mobile override did not take effect").toBe("16px");

      const faint = await resolveColor(page, "var(--faint)");
      const faintRgb = (await page.evaluate((c) => window.__cv396.parse(c), faint))!.map(Math.round);

      // The landing page first. Re-scrolled because the narrower layout moves
      // everything; the strip is already mounted from `beforeEach`.
      await page.locator("#pricing").scrollIntoViewIfNeeded();
      await expect(page.locator("footer")).toBeVisible();
      const landing = await page.evaluate((rgb) => window.__cv396.sweep(rgb), faintRgb);
      expect(landing.examined, "the phone sweep examined nothing").toBeGreaterThan(200);
      expect(landing.offenders, `these render --faint (${faint}) at ${PHONE.width}px`).toEqual([]);

      // Then the chat screen, which is where `.composer-hint` — the rule #396
      // was filed against, and the one the planted bypass used — lives.
      await enterChat(page);
      expect(
        await page.evaluate(() => window.__cv396.count(".composer-hint")),
        "the phone chat sweep never reached the rule #396 was filed against",
      ).toBeGreaterThan(0);
      const chat = await page.evaluate((rgb) => window.__cv396.sweep(rgb), faintRgb);
      expect(chat.examined).toBeGreaterThan(50);
      expect(chat.offenders, `these render --faint (${faint}) at ${PHONE.width}px`).toEqual([]);
    });

    test("the CTA banner's body sentence is not painted in its own background", async ({ page }) => {
      // NOT a `--faint` finding — a worse one, found by the #396 sweep. The
      // `<p>` had no rule at all, so it inherited `.cta-banner`'s
      // `color: var(--ink)` onto `.cta-banner`'s `background: var(--ink)`.
      // Measured in Chromium: rgb(28, 27, 25) on rgb(28, 27, 25) light and
      // rgb(240, 239, 233) on rgb(240, 239, 233) dark — 1.00:1 in BOTH themes,
      // a whole sentence of marketing copy invisible to every sighted visitor.
      // RED WHEN: `.cta-banner > p:not(.cta-footnote)` is removed from
      // `landing.css` (the ratio returns to 1.00:1).
      const sel = ".cta-banner > p:not(.cta-footnote)";
      const n = await page.evaluate((s) => window.__cv396.count(s), sel);
      expect(n, "the CTA banner's body sentence is not on the page at all").toBe(1);

      const text = await page.locator(sel).innerText();
      expect(text.trim().length, "the probe matched an empty node").toBeGreaterThan(20);

      const blockers = await page.evaluate((s) => window.__cv396.opaqueBlockers(s, 0), sel);
      expect(blockers, "the composite cannot be trusted over these ancestors").toEqual([]);

      const paint = await page.evaluate((s) => window.__cv396.paintOf(s, 0), sel);
      const bg = await page.evaluate((s) => window.__cv396.backdrop(s, 0), sel);
      const ratio = paintedRatio((paint!.fill ?? paint!.color)!, bg!);
      expect(
        ratio,
        `the CTA sentence renders at ${ratio.toFixed(2)}:1 on rgb(${bg!.join(", ")})`,
      ).toBeGreaterThanOrEqual(AA_SMALL_TEXT);
      // Measured after the fix: 16.35:1 light, 15.69:1 dark — `--bg` on
      // `--ink`, the same pair `.cta-banner`'s own `--focus-ring` comment
      // records. Pinned well above AA so a regression toward the background
      // cannot creep in just over the floor.
      expect(ratio).toBeGreaterThan(15);
    });

    test("the ratio composites a TRANSLUCENT text colour instead of scoring it opaque", async ({
      page,
    }) => {
      // `paintedRatio` is pure, so this needs no page — but it runs per theme
      // beside the probes that consume it, where a reader will look for it.
      //
      // The defect it closes: the probe loop used to pass `colour.slice(0, 3)`
      // to `contrastRatio`, dropping alpha entirely. `.cta-footnote` in this
      // stylesheet is `color-mix(in srgb, var(--bg) 50%, transparent)`, so a
      // rule of that shape scored roughly double the ratio a reader gets.
      // RED WHEN: `paintedRatio` drops its `a * colour[i] + (1 - a) * backdrop[i]`
      // term and scores the un-premultiplied colour.
      // `--muted` (#6b6862) on white. Measured: 5.5531 opaque, 2.0734 at half
      // alpha — a 2.7x overstatement if alpha is dropped.
      const OPAQUE = paintedRatio([107, 104, 98, 1], [255, 255, 255]);
      const HALF = paintedRatio([107, 104, 98, 0.5], [255, 255, 255]);
      expect(OPAQUE).toBeCloseTo(5.55, 1);
      // The same colour at 50% alpha is a DIFFERENT, much lower number, and it
      // is below AA where the opaque reading clears it comfortably.
      expect(HALF).toBeCloseTo(2.07, 1);
      expect(HALF).toBeLessThan(AA_SMALL_TEXT);
      // And an omitted alpha still means opaque, so the existing callers that
      // pass a 3-element array are unaffected.
      expect(paintedRatio([107, 104, 98], [255, 255, 255])).toBe(OPAQUE);

      // The colour-ACCEPTANCE half of the same problem. Nothing measured on
      // this page is translucent today, so an inline alpha comparison inside
      // the probe loop was an equivalent mutation — `isOpaqueToken` exists so
      // it has somewhere to be proved.
      // RED WHEN: `isOpaqueToken`'s `Math.abs((colour[3] ?? 1) - 1) < 0.01`
      // becomes `Math.round(colour[3] ?? 1) === 1`, which accepts alpha 0.5.
      const MUTED = [107, 104, 98, 1];
      expect(isOpaqueToken([107, 104, 98, 1], MUTED)).toBe(true);
      expect(isOpaqueToken([107, 104, 98], MUTED), "an omitted alpha means opaque").toBe(true);
      expect(isOpaqueToken([107, 104, 98, 0.5], MUTED), "alpha 0.5 is not the token").toBe(false);
      expect(isOpaqueToken([107, 104, 99, 1], MUTED), "one channel off is not the token").toBe(
        false,
      );
      expect(isOpaqueToken(null, MUTED)).toBe(false);
      void page;
    });

    test("the sweep really visits ::placeholder, not just element colours", async ({ page }) => {
      // Without this, dropping the `::placeholder` pass from the sweep is an
      // EQUIVALENT mutation — nothing on the page is faint, so removing a pass
      // that finds nothing changes no result. Planting a placeholder-only
      // offender is what makes that pass provable.
      // RED WHEN: the `pseudos.push("::placeholder")` branch is removed.
      const faint = await resolveColor(page, "var(--faint)");
      const faintRgb = (await page.evaluate((c) => window.__cv396.parse(c), faint))!.map(Math.round);

      await page.evaluate(() => {
        const style = document.createElement("style");
        style.id = "cv396-planted-style";
        style.textContent = ".cv396-planted-input::placeholder{color:var(--faint)}";
        document.head.appendChild(style);
        const input = document.createElement("input");
        input.className = "cv396-planted-input";
        input.placeholder = "planted";
        document.body.appendChild(input);
      });
      const withPlant = await page.evaluate((rgb) => window.__cv396.sweep(rgb), faintRgb);
      // The element's OWN colour is untouched, so only the pseudo pass can find it.
      expect(withPlant.offenders.join(" | ")).toContain("cv396-planted-input::placeholder");

      await page.evaluate(() => {
        document.querySelector(".cv396-planted-input")?.remove();
        document.getElementById("cv396-planted-style")?.remove();
      });
      expect((await page.evaluate((rgb) => window.__cv396.sweep(rgb), faintRgb)).offenders).toEqual(
        [],
      );
    });

    test("the compositor refuses to answer under an ancestor it cannot model", async ({ page }) => {
      // The precondition check, proved in both directions. Nothing measured
      // today sits under a gradient, a filter or a translucent ancestor, so
      // deleting the check would otherwise be an EQUIVALENT mutation — it would
      // change no result until the day it mattered, which is the day it is
      // needed. This plants each shape and requires it to be reported.
      //
      // EVERY `bad.push(...)` in `opaqueBlockers` has a plant here. An earlier
      // draft claimed "any `bad.push(...)` line removed turns this red" while
      // the `backdrop-filter` push had no fixture at all, so deleting THAT one
      // was an equivalent mutation and the claim was false for it.
      // RED WHEN: any `bad.push(...)` line is removed from `opaqueBlockers`.
      const clean = await page.evaluate(() => window.__cv396.opaqueBlockers(".footer-copy", 0));
      expect(clean, "the footer already sits under something unmodellable").toEqual([]);

      for (const [prop, value, needle] of [
        ["backgroundImage", "linear-gradient(#000, #fff)", "background-image"],
        ["filter", "opacity(0.9)", "filter:"],
        // `backdrop-filter` blurs whatever is BEHIND the element, so the flat
        // `background-color` walk below it stops describing what is painted.
        ["backdropFilter", "blur(4px)", "backdrop-filter:"],
        ["opacity", "0.9", "opacity:"],
      ] as const) {
        const found = await page.evaluate(
          ([p, v]) => {
            const el = document.querySelector("footer") as HTMLElement;
            (el.style as unknown as Record<string, string>)[p as string] = v as string;
            const applied = (getComputedStyle(el) as unknown as Record<string, string>)[
              p as string
            ];
            const out = window.__cv396.opaqueBlockers(".footer-copy", 0);
            (el.style as unknown as Record<string, string>)[p as string] = "";
            return { out, applied };
          },
          [prop, value] as const,
        );
        // The plant is confirmed to have LANDED first. A property the engine
        // rejects leaves the computed style at its default, and "not reported"
        // would then be correct behaviour reported as a pass — the shape that
        // makes a mutant die for the wrong reason.
        expect(found.applied, `${prop} was not accepted by the engine`).not.toBe("none");
        expect(found.out.join(" | "), `${prop} was not reported as a blocker`).toContain(needle);
      }
    });

    test("a PAINTING ::before over a measured element is reported as a blocker", async ({
      page,
    }) => {
      // The pass an earlier draft's docstring CLAIMED and the body did not
      // have: it called `getComputedStyle(n)` with no pseudo argument, so a
      // `::before` overlay was invisible to it. This app ships that shape —
      // `.ticker-strip::before/::after` are 80px gradients that `landing.css`
      // records painting over a focus ring down to 1.47:1.
      // RED WHEN: the `for (const pseudo of ["::before", "::after"])` loop is
      // removed from `opaqueBlockers`.
      const clean = await page.evaluate(() => window.__cv396.opaqueBlockers(".footer-copy", 0));
      expect(clean).toEqual([]);

      const planted = await page.evaluate(() => {
        const style = document.createElement("style");
        style.id = "cv396-pseudo-style";
        style.textContent =
          "footer::before{content:'';position:absolute;inset:0;background:linear-gradient(#000,transparent)}";
        document.head.appendChild(style);
        const applied = getComputedStyle(document.querySelector("footer")!, "::before");
        return {
          out: window.__cv396.opaqueBlockers(".footer-copy", 0),
          content: applied.content,
          image: applied.backgroundImage,
        };
      });
      // Precondition: the pseudo really exists and really paints.
      expect(planted.content, "the planted ::before has no content").not.toBe("none");
      expect(planted.image, "the planted ::before paints nothing").not.toBe("none");
      expect(planted.out.join(" | "), "the ::before overlay was not reported").toContain(
        "footer::before",
      );

      // A pseudo that carries only TEXT is NOT a blocker — it obscures no
      // backdrop, and reporting it would block every measured element in the
      // app for nothing. The negative and the positive share one fixture, so
      // neither can pass on a dead checker.
      const textOnly = await page.evaluate(() => {
        document.getElementById("cv396-pseudo-style")!.textContent = "footer::before{content:'x'}";
        // READ INTO A STRING before the cleanup below. `getComputedStyle`
        // returns a LIVE declaration, so holding the object and reading
        // `.content` after the <style> is removed reports `none` — which is
        // exactly how this assertion failed on its first run, for a reason
        // that had nothing to do with the checker under test.
        const content = getComputedStyle(document.querySelector("footer")!, "::before").content;
        const out = window.__cv396.opaqueBlockers(".footer-copy", 0);
        document.getElementById("cv396-pseudo-style")?.remove();
        return { out, content };
      });
      expect(textOnly.content, "the text-only ::before did not apply").not.toBe("none");
      expect(textOnly.out, "a text-only ::before was wrongly reported as a blocker").toEqual([]);

      expect(
        await page.evaluate(() => window.__cv396.opaqueBlockers(".footer-copy", 0)),
        "the plant was not cleaned up",
      ).toEqual([]);
    });

    test("the colour reader rejects a colour the engine cannot parse", async ({ page }) => {
      // The two-sentinel check, asserted directly. Without it an unparseable
      // value silently reads back as whatever was in `fillStyle` — which is how
      // a sweep starts reporting confident nonsense.
      // RED WHEN: the second sentinel is dropped from `parse`.
      expect(await page.evaluate(() => window.__cv396.parse("not-a-colour"))).toBeNull();
      expect(await page.evaluate(() => window.__cv396.parse("rgb(1, 2, 3)"))).toEqual([1, 2, 3, 1]);
      // Alpha is RECOVERED, not assumed 1 — a half-transparent token would
      // otherwise composite as if it were opaque.
      const half = await page.evaluate(() => window.__cv396.parse("rgba(0, 0, 0, 0.5)"));
      expect(half![3]).toBeGreaterThan(0.45);
      expect(half![3]).toBeLessThan(0.55);
    });
  });
}
