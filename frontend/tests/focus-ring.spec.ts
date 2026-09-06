/**
 * Focus-ring guard (#355).
 *
 * WHY THIS IS A PLAYWRIGHT SPEC AND NOT A UNIT TEST. `vite.config.ts` sets
 * `css: false` for vitest, so no unit test in this repo can observe ANY
 * computed style — the defect this guards was invisible to all 482 of them.
 * And a guard asserting the CSS TEXT would not have caught it either: the
 * declaration `outline: 2px solid var(--accent)` was present in reset.css the
 * whole time. It simply did not compute, because `--accent` is defined only
 * under the three unreachable `[data-style]` skins, so the `outline` shorthand
 * was invalid at computed-value time, every longhand became `unset`, and
 * `outline-style` computed to `none` — REMOVING Chrome's own default ring,
 * because an author rule outranks the UA stylesheet.
 *
 * So this asserts what the USER PERCEIVES: it drives the page with real Tab
 * presses, reads the RESOLVED `outline-style` / `outline-color` off each
 * focused control, composites the actual painted backdrop behind the ring, and
 * computes the WCAG contrast ratio. Nothing here reads a stylesheet.
 *
 * It runs under `playwright.demo-ci.config.ts`, which is a REQUIRED check.
 *
 * WHAT THIS SWEEP DOES NOT COVER, stated so its green is not read as more than
 * it is. It walks the landing page, the chat screen (with an answer rendered)
 * and the sign-in dialog. It does NOT open `HistoryDrawer`,
 * `ConnectedAccountsDrawer`, `Nudge`, `AccountMenu`'s popup or `ToastHost`.
 * The first four need a signed-in session; `ToastHost` does NOT — an earlier
 * version of this note said it did, and that was the wrong mechanism.
 * `ToastHost` is unreachable here because it only renders once a toast exists,
 * and in DEMO mode nothing raises one: `handleApiError` needs a live request,
 * and `park()` runs only under `if (inFlight.current)`, which `markInFlight`
 * sets from `sendLive` alone. So the restored parked-question toast, its
 * `overflowWrap`, the identical-toast collapse and the 3-card cap have NO
 * coverage in this REQUIRED job — structurally the same demo-mode blind spot
 * this file's own history is about. They are covered by vitest instead
 * (`useToast.test.tsx`, `useLandingState.test.tsx`) and the gap is recorded in
 * docs/BACKLOG.md rather than left to be rediscovered.
 *
 * The four signed-in surfaces were measured by hand during review (3-8 stops
 * each, worst ratio 13.07:1, both themes) and they inherit the same global
 * rule, so the exposure there is a FUTURE control setting `outline: none`. It
 * also cannot see `/about`, which is served by the API and carries its own copy
 * of the tokens in `frontend/public/about.css` (measured 14.43-17.21:1 by hand,
 * in agreement with the SPA).
 */
import { test, expect } from "@playwright/test";
import {
  gotoApp,
  ensureTheme,
  enterChat,
  waitStreamDone,
  contrastRatio,
  type ThemeName,
} from "./helpers";

/**
 * WCAG 2.4.11 Focus Appearance asks for 3:1 between the indicator and adjacent
 * colours. `--ink` measures 9.35:1 or better on every surface a ring is actually
 * drawn over in this app (see docs/UI_DESIGN.md §1.1 — NOT every token: --ink on
 * --hl is 1.41:1, and the ring is never drawn over --hl because `outline-offset`
 * puts it on the parent). The floor is set at the standard's number, not at
 * ours, so a future token change has room to be different without being bad.
 */
const MIN_RING_CONTRAST = 3;

/**
 * Controls that deliberately carry no outline of their own. Each is a text
 * input inside a box that shows a `:focus-within` border instead, and each is
 * asserted separately below — the exemption is not a free pass. Adding an
 * `outline: none` anywhere else makes the sweep fail, which is the point.
 *
 * A SELECTOR, not a set of class tokens. The first version matched any element
 * carrying `chat-input` among its classes, so a future control that happened to
 * share that token — a second input, a styled wrapper — would have been
 * exempted wholesale and never measured. `matches()` pins the element itself.
 */
const OUTLINE_EXEMPT_SELECTOR = "#hero-input, input.chat-input";

type Stop = {
  id: string;
  tag: string;
  cls: string;
  label: string;
  focusVisible: boolean;
  /** True when the control sits inside the app's one inverted (--ink) panel. */
  inInverted: boolean;
  /** True when the control sits inside a modal dialog. */
  inDialog: boolean;
  /** This exact ELEMENT was already visited earlier in the walk. */
  seenBefore: boolean;
  /** Matches OUTLINE_EXEMPT_SELECTOR — decided in the page, on the element. */
  exempt: boolean;
  /**
   * Set when an ancestor paints a `background-image` (a gradient) before any
   * opaque `background-color` is found. `backdropOf` composites COLOURS only,
   * so in that case it does not know what is actually behind the ring and must
   * say so rather than return a confident wrong answer.
   */
  unmeasurableBackdrop: string | null;
  outlineStyle: string;
  outlineWidth: number;
  /** Resolved through a canvas, so `color-mix()` / `color(srgb …)` normalise. */
  outlineRGBA: number[];
  outlineRaw: string;
  /** The composited paint the ring is actually drawn over. */
  backdropRGB: number[];
  backdropRaw: string;
};

/**
 * Press Tab `max` times and record what the browser focuses at each stop.
 *
 * Real key presses, not `el.focus()`: `:focus-visible` is a heuristic keyed on
 * the interaction modality, and a programmatic focus on a <button> does not
 * match it. Reading a ring off an element that is not in the state the rule
 * targets would be a test that passes for the wrong reason.
 */
async function tabWalk(page: import("@playwright/test").Page, max: number): Promise<Stop[]> {
  const stops: Stop[] = [];
  // Clear any stamps a previous walk in this same page left behind, so two
  // sweeps in one test do not silently measure nothing the second time.
  await page.evaluate(() =>
    document.querySelectorAll("[data-fr-seen]").forEach((n) => n.removeAttribute("data-fr-seen")),
  );
  for (let i = 0; i < max; i++) {
    await page.keyboard.press("Tab");
    const stop = await page.evaluate((EXEMPT_SELECTOR) => {
      const el = document.activeElement as HTMLElement | null;
      if (!el || el === document.body || el === document.documentElement) return null;

      // Normalise ANY CSS colour (including color-mix's `color(srgb …)`) to
      // 8-bit RGBA by painting it. Parsing the string forms by hand is how a
      // colour silently reads as 0,0,0 and a contrast assertion passes for the
      // wrong reason — measured: a naive rgb() regex returned null for the
      // header's color-mix background.
      const paint = (col: string): number[] => {
        const c = document.createElement("canvas");
        c.width = c.height = 1;
        const ctx = c.getContext("2d")!;
        ctx.clearRect(0, 0, 1, 1);
        // Sentinel: canvas silently IGNORES an unparseable fillStyle and keeps
        // the previous one, which would hand back a confident wrong colour.
        // The first version of this only SET the sentinel and never compared
        // against it — a comment standing in for a check, which is worse than
        // nothing here: an unparseable colour would have come back magenta,
        // and magenta measures 5.77:1 against the dark --bg, so it would have
        // PASSED the 3:1 floor. Now it throws.
        ctx.fillStyle = "#ff00ff";
        ctx.fillStyle = col;
        ctx.fillRect(0, 0, 1, 1);
        const d = ctx.getImageData(0, 0, 1, 1).data;
        if (d[0] === 255 && d[1] === 0 && d[2] === 255 && !/^#ff00ff$/i.test(col.trim())) {
          throw new Error(`focus-ring guard: canvas could not parse the colour ${col}`);
        }
        return [d[0], d[1], d[2], d[3] / 255];
      };

      // `over(fg, bg)` — source-over compositing, so a translucent header or a
      // backdrop resolves to the colour a human eye actually receives.
      const over = (fg: number[], bg: number[]): number[] => {
        const a = fg[3];
        return [0, 1, 2].map((i) => Math.round(fg[i] * a + bg[i] * (1 - a)));
      };

      // The ring is drawn at `outline-offset: 2px`, i.e. OUTSIDE the border
      // box, so what sits behind it is the ancestor chain's paint, not the
      // control's own background. Start at the parent for exactly that reason.
      const backdropOf = (node: HTMLElement): { rgb: number[]; unmeasurable: string | null } => {
        const layers: number[][] = [];
        let unmeasurable: string | null = null;
        // Is the FOCUSED element lifted into its own stacking position? If it
        // is, an ancestor's painted pseudo-element cannot cover its ring, so a
        // painting ::before/::after below is not an occluder and the colour
        // composite stays honest.
        const own = getComputedStyle(node);
        const raised = own.position !== "static" && own.zIndex !== "auto";
        let n: HTMLElement | null = node.parentElement;
        while (n) {
          const cs = getComputedStyle(n);
          // ::before / ::after are INVISIBLE to getComputedStyle(n) and paint
          // over descendants when they are positioned above them. This is not
          // hypothetical: `.ticker-strip::before/::after` are 80px
          // `linear-gradient(to right, var(--bg), transparent)` fades at
          // z-index 1, and `.ticker-chip` is a plain non-positioned <button> in
          // the tab order — so the fade paints OVER its focus ring. Measured on
          // a natural Tab walk: the guard reported 17.21:1 for a ring painting
          // at 1.47-1.85:1, i.e. up to 2x BELOW its own 3:1 floor, on the first
          // ticker stop an ordinary keyboard user reaches.
          for (const pseudo of ["::before", "::after"]) {
            const ps = getComputedStyle(n, pseudo);
            if (!ps.content || ps.content === "none") continue;
            const paints =
              (ps.backgroundImage && ps.backgroundImage !== "none") ||
              paint(ps.backgroundColor)[3] > 0;
            if (paints && !raised && unmeasurable === null) {
              unmeasurable =
                `${n.tagName}.${typeof n.className === "string" ? n.className : ""}${pseudo} ` +
                `paints over an unraised descendant`;
            }
          }
          // A gradient is a background-IMAGE, and this walk composites
          // background-COLOURS. An ancestor painting `linear-gradient(--ink,
          // --ink)` has `backgroundColor: rgba(0,0,0,0)`, so it looked
          // TRANSPARENT to the first version of this walk and the composite
          // fell through to the page canvas: the guard measured 16.35:1 for a
          // ring that a reviewer measured at 1.00:1 in the same pixel. That is
          // the .cta-banner defect again, one CSS property over. Rather than
          // guess, record that this backdrop cannot be composited and let
          // assertRings fail loudly with the element named.
          if (cs.backgroundImage && cs.backgroundImage !== "none" && unmeasurable === null) {
            unmeasurable = `${n.tagName}.${typeof n.className === "string" ? n.className : ""} paints ${cs.backgroundImage.slice(0, 60)}`;
          }
          const c = paint(cs.backgroundColor);
          if (c[3] > 0) {
            layers.push(c);
            if (c[3] >= 1) break;
          }
          n = n.parentElement;
        }
        // Anything still translucent lands on the canvas the browser paints,
        // which is white unless html/body says otherwise (they do here).
        let acc = [255, 255, 255];
        for (let i = layers.length - 1; i >= 0; i--) acc = over(layers[i], acc);
        return { rgb: acc, unmeasurable };
      };

      const cs = getComputedStyle(el);
      const backdrop = backdropOf(el);
      // TRUE identity, stamped on the element itself. The first version keyed
      // de-duplication on `tag.class#id|label`, which is not an identity: a
      // reviewer put a SECOND `.cta-pill` with `outline: none` next to the real
      // one, and because the twin collapsed onto the same key it was never
      // measured and the sweep stayed green on a genuinely broken control.
      const seenBefore = el.hasAttribute("data-fr-seen");
      el.setAttribute("data-fr-seen", "");
      return {
        seenBefore,
        exempt: el.matches(EXEMPT_SELECTOR),
        unmeasurableBackdrop: backdrop.unmeasurable,
        id: el.id || "",
        tag: el.tagName,
        cls: typeof el.className === "string" ? el.className : "",
        label: (el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 40),
        focusVisible: el.matches(":focus-visible"),
        inInverted: el.closest(".cta-banner") !== null,
        inDialog: el.closest('[role="dialog"]') !== null,
        outlineStyle: cs.outlineStyle,
        outlineWidth: parseFloat(cs.outlineWidth) || 0,
        outlineRGBA: paint(cs.outlineColor),
        outlineRaw: cs.outlineColor,
        backdropRGB: backdrop.rgb,
        backdropRaw: getComputedStyle(el.parentElement ?? el).backgroundColor,
      };
    }, OUTLINE_EXEMPT_SELECTOR);
    // A null stop means focus left the document (headless Chromium re-enters
    // from the top on the next press). CONTINUE rather than break: an early
    // break here silently truncated the sweep at the marquee — the ticker
    // duplicates its chips, so a de-dupe-and-break walk stopped 24 controls in
    // and never reached the inverted CTA banner further down the page.
    if (stop === null) continue;
    if (stop.seenBefore) continue; // this exact element again — report it once
    stops.push(stop as Stop);
  }
  await page.evaluate(() =>
    document.querySelectorAll("[data-fr-seen]").forEach((n) => n.removeAttribute("data-fr-seen")),
  );
  return stops;
}

function isExempt(s: Stop) {
  return s.exempt;
}

/** Assert every keyboard stop in `stops` renders a ring a human can see. */
function assertRings(stops: Stop[], where: string) {
  const failures: string[] = [];
  let checked = 0;
  for (const s of stops) {
    if (isExempt(s)) continue;
    checked += 1;
    const who = `${where}: ${s.tag}.${s.cls || "-"} "${s.label}"`;
    if (!s.focusVisible) {
      failures.push(`${who} — Tab focused it but it does not match :focus-visible`);
      continue;
    }
    if (s.outlineStyle === "none" || s.outlineWidth < 2) {
      failures.push(
        `${who} — outline-style: ${s.outlineStyle}, outline-width: ${s.outlineWidth}px ` +
          `(no visible ring; this is #355's exact signature)`,
      );
      continue;
    }
    if (s.unmeasurableBackdrop !== null) {
      // NOT skipped. A backdrop this sweep cannot composite is a backdrop it
      // cannot vouch for, and staying quiet about it is how a guard reports
      // green on something it never looked at.
      failures.push(
        `${who} — the paint behind this ring cannot be composited from ` +
          `background-color alone (${s.unmeasurableBackdrop}); the ring's real ` +
          `contrast is unknown, so this sweep refuses to pass it`,
      );
      continue;
    }
    const ratio = contrastRatio(s.outlineRGBA, s.backdropRGB);
    if (ratio < MIN_RING_CONTRAST) {
      failures.push(
        `${who} — ring ${s.outlineRaw} on ${s.backdropRaw} measures ${ratio.toFixed(2)}:1, ` +
          `below the ${MIN_RING_CONTRAST}:1 floor`,
      );
    }
  }
  // A sweep that checked nothing is not a pass. This is the partner that proves
  // the thing being counted exists — without it, an exemption list that grew to
  // cover everything, or a tabWalk that broke on its first press, would report
  // "0 failures" and look green.
  expect(checked, `${where}: the sweep found no controls to check`).toBeGreaterThan(0);
  expect(failures, `${where}: controls with no perceivable focus ring`).toEqual([]);
}

for (const theme of ["light", "dark"] as ThemeName[]) {
  test.describe(`focus ring — ${theme} theme (#355)`, () => {
    test.beforeEach(async ({ page }) => {
      await gotoApp(page);
      await ensureTheme(page, theme);
    });

    test("every keyboard stop on the landing page renders a ring at >= 3:1", async ({ page }) => {
      const stops = await tabWalk(page, 140);
      // The landing page has well over a dozen reachable controls; a walk that
      // collapses to two or three means Tab stopped moving, not that the page
      // got simpler.
      expect(stops.length, "keyboard stops reached on the landing page").toBeGreaterThanOrEqual(20);
      // Coverage partner: the sweep must actually REACH the inverted panel near
      // the bottom of the page, or the one surface where a flat --ink ring is
      // invisible would be swept over without ever being measured.
      expect(
        stops.filter((s) => s.inInverted).map((s) => s.label),
        "the sweep must reach the inverted CTA banner",
      ).not.toEqual([]);
      assertRings(stops, `landing/${theme}`);
    });

    test("every keyboard stop on the chat screen renders a ring at >= 3:1", async ({ page }) => {
      await enterChat(page);
      // Ask something first. An empty chat screen has four controls and NO
      // answer, so the sweep would otherwise never walk anything an answer
      // renders. Demo mode answers from the built-in KB, no backend needed.
      //
      // What this still does NOT reach: `a.citation-chip`. Chips come from `[n]`
      // markers, which only the LIVE path emits — measured in demo mode:
      // `chips=0, cards=2`. A live-only test here would self-skip, and the
      // required job pins `stats["skipped"] == 4`, so the chip's own rule is
      // covered by the injected-element test below instead.
      await page.locator(".chat-input").fill("What is Claude Code?");
      await page.keyboard.press("Enter");
      await waitStreamDone(page);
      const rendered = await page.evaluate(() => ({
        cards: document.querySelectorAll(".sources .source-card").length,
        chips: document.querySelectorAll("a.citation-chip").length,
      }));
      // Partner: proves the sweep below had a rendered answer to walk. Without
      // it, an answer that failed to render would leave the sweep measuring the
      // same four empty-screen controls and still reporting green.
      expect(rendered.cards, "the demo answer must render source cards").toBeGreaterThan(0);
      const stops = await tabWalk(page, 40);
      expect(stops.length, "keyboard stops reached on the chat screen").toBeGreaterThanOrEqual(5);
      assertRings(stops, `chat/${theme}`);
    });

    test("every keyboard stop inside the sign-in dialog renders a ring at >= 3:1", async ({
      page,
    }) => {
      // The dialog is a lazy chunk on a --surface background, and the focus
      // trap (#331) keeps Tab inside it — so it is both the densest cluster of
      // controls in the app and the one the landing sweep can never reach.
      await page.locator(".account-button", { hasText: "Sign in" }).click();
      await expect(page.locator('[role="dialog"]')).toBeVisible();
      const stops = await tabWalk(page, 30);
      expect(stops.length, "keyboard stops reached inside the dialog").toBeGreaterThanOrEqual(3);
      // Partner: the trap really is holding, so these stops are the dialog's
      // controls and not the page behind it.
      const outside = stops.filter((s) => !s.inDialog).map((s) => s.label);
      expect(outside, "Tab escaped the dialog — these stops are not what this test claims").toEqual(
        [],
      );
      assertRings(stops, `auth-dialog/${theme}`);
    });

    test("the ring stays visible on the INVERTED CTA banner, where --ink alone would be 1:1", async ({
      page,
    }) => {
      // The trap this test exists for: `--ink` is the right ring on every
      // surface in the app EXCEPT `.cta-banner`, which paints `--ink` as its
      // BACKGROUND. Measured before the fix, a plain `var(--ink)` ring on
      // `.cta-pill` scored 1.00:1 in both themes — a "fix" that is invisible on
      // the one surface it is drawn over. Anything that re-points the ring at a
      // single flat token turns this red.
      const pill = page.locator(".cta-pill").first();
      await pill.scrollIntoViewIfNeeded();
      await pill.press("Tab"); // establish keyboard modality on a real element
      await pill.focus();
      const m = await pill.evaluate((el) => {
        const paint = (col: string) => {
          const c = document.createElement("canvas");
          c.width = c.height = 1;
          const ctx = c.getContext("2d")!;
          ctx.clearRect(0, 0, 1, 1);
          ctx.fillStyle = "#ff00ff";
          ctx.fillStyle = col;
          ctx.fillRect(0, 0, 1, 1);
          const d = ctx.getImageData(0, 0, 1, 1).data;
          return [d[0], d[1], d[2], d[3] / 255];
        };
        const cs = getComputedStyle(el);
        const banner = el.closest(".cta-banner") as HTMLElement;
        return {
          outlineStyle: cs.outlineStyle,
          outlineWidth: parseFloat(cs.outlineWidth) || 0,
          ring: paint(cs.outlineColor),
          bannerBg: paint(getComputedStyle(banner).backgroundColor),
          ringRaw: cs.outlineColor,
          bannerRaw: getComputedStyle(banner).backgroundColor,
        };
      });
      expect(m.outlineStyle).not.toBe("none");
      expect(m.outlineWidth).toBeGreaterThanOrEqual(2);
      const ratio = contrastRatio(m.ring, m.bannerBg.slice(0, 3));
      expect(
        ratio,
        `ring ${m.ringRaw} on the inverted banner ${m.bannerRaw} measured ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(MIN_RING_CONTRAST);
    });

    test("refuses to vouch for a ring whose backdrop it cannot composite", async ({ page }) => {
      // The bite-proof for the gradient blind spot. `backdropOf` composites
      // background-COLOURS; a panel painting --ink via a GRADIENT reports
      // `backgroundColor: rgba(0,0,0,0)`, so the walk used to fall through to
      // the page canvas and score a 1.00:1 ring as 16.35:1 — reproduced by a
      // reviewer, and it is the .cta-banner defect one CSS property over.
      //
      // Injected at runtime rather than shipped in the app: the point is to
      // prove the DETECTION fires, not to add an inverted panel to the page.
      await page.evaluate(() => {
        const panel = document.createElement("div");
        panel.id = "fr-gradient-probe";
        panel.style.backgroundImage = "linear-gradient(var(--ink), var(--ink))";
        panel.style.padding = "24px";
        const btn = document.createElement("button");
        btn.className = "fr-gradient-pill";
        btn.textContent = "Gradient probe";
        panel.appendChild(btn);
        document.body.appendChild(panel);
      });
      const stops = await tabWalk(page, 140);
      const probe = stops.filter((s) => s.cls.includes("fr-gradient-pill"));
      // Partner: the sweep must actually have REACHED the probe, or the
      // assertion below would pass on an empty list.
      expect(probe.length, "the sweep must reach the injected probe").toBe(1);
      expect(
        probe[0].unmeasurableBackdrop,
        "a gradient-painted ancestor must be reported as unmeasurable",
      ).not.toBeNull();
      // And it must FAIL the sweep rather than being quietly skipped.
      expect(() => assertRings(probe, "probe")).toThrow(/cannot be composited/);
      await page.evaluate(() => document.getElementById("fr-gradient-probe")?.remove());
    });

    test("a citation chip's ring follows --focus-ring, not a hardcoded --ink", async ({
      page,
    }) => {
      // `a.citation-chip:focus-visible` has its OWN declaration at a specificity
      // the shared rule cannot reach, so it used to hardcode `var(--ink)` and
      // silently opt out of the token. Today that is invisible — outside
      // `.cta-banner` the two resolve to the same colour — which is exactly why
      // it needs proving rather than eyeballing: re-point `--focus-ring` on an
      // ancestor and the chip's ring must follow, or a chip inside a future
      // inverted surface is a 1.00:1 ring again.
      //
      // The chip is INJECTED rather than driven through the app: chips are
      // rendered from `[n]` markers, which only the live path emits (measured in
      // demo mode: chips=0, cards=2), and a live-only test would self-skip and
      // break the required job's `skipped == 4` pin. What is under test is the
      // CSS rule, and an `<a class="citation-chip">` is what that rule selects.
      await enterChat(page);
      const ring = await page.evaluate(() => {
        const host = document.querySelector("#chat-list") as HTMLElement;
        const chip = document.createElement("a");
        chip.className = "citation-chip";
        chip.href = "#";
        chip.textContent = "[1]";
        host.appendChild(chip);
        // Read the rule directly: `:focus-visible` needs keyboard modality, so
        // resolve the declared value instead of faking a focus state.
        const declared = getComputedStyle(chip).getPropertyValue("--focus-ring").trim();
        host.style.setProperty("--focus-ring", "rgb(1, 2, 3)");
        const repointed = getComputedStyle(chip).getPropertyValue("--focus-ring").trim();
        // And prove the RULE consumes it, by applying the same declaration the
        // stylesheet does and reading what it computes to.
        chip.style.outline = "2px solid var(--focus-ring)";
        const outline = getComputedStyle(chip).outlineColor;
        host.style.removeProperty("--focus-ring");
        chip.remove();
        return { declared, repointed, outline };
      });
      // Partner: the token really was something else first, so the change below
      // is a change and not a coincidence.
      expect(ring.declared).not.toBe("rgb(1, 2, 3)");
      expect(ring.repointed).toBe("rgb(1, 2, 3)");
      expect(ring.outline, "the chip's ring must resolve through --focus-ring").toBe(
        "rgb(1, 2, 3)",
      );
      // And the stylesheet rule itself must be the token, not a hardcoded --ink.
      const rule = await page.evaluate(() => {
        for (const sheet of Array.from(document.styleSheets)) {
          let rules: CSSRuleList;
          try {
            rules = sheet.cssRules;
          } catch {
            continue; // cross-origin sheet
          }
          for (const r of Array.from(rules)) {
            if (r instanceof CSSStyleRule && r.selectorText === "a.citation-chip:focus-visible") {
              return r.style.getPropertyValue("outline");
            }
          }
        }
        return null;
      });
      expect(rule, "a.citation-chip:focus-visible must exist as its own rule").not.toBeNull();
      expect(rule).toContain("var(--focus-ring)");
    });

    test("a broken TWIN of a compliant control is still measured", async ({ page }) => {
      // The bite-proof for element identity. De-duplication used to key on
      // `tag.class#id|label`, which is not an identity: a reviewer put a second
      // `.cta-pill` with `outline: none` beside the real one, the twin
      // collapsed onto the same key, was never measured, and the sweep reported
      // 10/10 green on a genuinely unringed, keyboard-reachable control.
      await page.evaluate(() => {
        const real = document.querySelector(".cta-pill") as HTMLElement;
        const twin = real.cloneNode(true) as HTMLElement;
        twin.style.outline = "none";
        twin.setAttribute("data-fr-twin", "");
        real.after(twin);
      });
      const stops = await tabWalk(page, 140);
      const pills = stops.filter((s) => s.cls.includes("cta-pill"));
      // Partner: BOTH must be walked, or the assertion below is vacuous.
      expect(pills.length, "both the real pill and its twin must be measured").toBe(2);
      expect(() => assertRings(stops, "twin")).toThrow(/no visible ring/);
      await page.evaluate(() => document.querySelector("[data-fr-twin]")?.remove());
    });

    test("the two outline-exempt text inputs show a focus-within border instead", async ({
      page,
    }) => {
      // The exemption list above is only honest if the exempted controls really
      // do signal focus another way. Both boxes already declare
      // `transition: border-color`, so the treatment was always intended.
      const border = (box: string) =>
        page.locator(box).first().evaluate((b) => getComputedStyle(b).borderTopColor);
      const ink = async () =>
        page.evaluate(() => {
          const probe = document.createElement("span");
          probe.style.color = "var(--ink)";
          document.body.appendChild(probe);
          const c = getComputedStyle(probe).color;
          probe.remove();
          return c;
        });

      // Both boxes declare `transition: border-color`, so a single read taken
      // right after `.focus()` lands MID-TRANSITION and returns the start
      // colour — a false red the first time this was written. Poll the settled
      // value instead.
      const check = async (box: string, input: string, what: string) => {
        const inkColor = await ink();
        const before = await border(box);
        // The partner. Without it, a box whose resting border ALREADY equalled
        // --ink would satisfy every assertion below while signalling nothing.
        expect(before, `${what}: resting border must differ from --ink`).not.toBe(inkColor);
        await page.locator(input).first().focus();
        // Poll for the TARGET, not merely for "changed": polling for `!= before`
        // returns on the transition's first frame and hands back an
        // intermediate colour (measured rgb(134,134,135) between --border-2 and
        // --ink), which then fails the equality below for the wrong reason.
        await expect
          .poll(() => border(box), { message: `${what} must take the --ink focus border` })
          .toBe(inkColor);
        await page.evaluate(() => (document.activeElement as HTMLElement)?.blur());
        // The reverse direction too: a rule that painted --ink unconditionally
        // would satisfy the assertion above and still signal nothing.
        await expect
          .poll(() => border(box), { message: `${what} must revert on blur` })
          .toBe(before);
      };

      await check(".hero-input-box", "#hero-input", "the hero box");

      await enterChat(page);
      // The composer is focused on mount (#302), so blur first or `before` is
      // already the focused colour for a reason unrelated to the rule.
      await page.evaluate(() => (document.activeElement as HTMLElement)?.blur());
      await page.waitForTimeout(300);
      await check(".composer-box", ".chat-input", "the composer");
    });
  });
}
