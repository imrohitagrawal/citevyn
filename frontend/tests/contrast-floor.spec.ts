/**
 * #403 — a WCAG contrast FLOOR sweep, not a token ban.
 *
 * WHY THIS FILE EXISTS. #396 shipped a guard that asks "does anything render
 * `--faint`?". That is a question about one token, and the text runs listed
 * below failed WCAG 1.4.3 AA using OTHER tokens, so nothing caught them. (The
 * issue's title says "seven"; this sweep enumerated them and found more, so no
 * count is quoted here — the table is the list, and it is NOT claimed to be
 * exhaustive beyond what this suite can reach.) The guard could not
 * see them by construction — it was never looking at contrast. This file asks
 * the question the criterion actually poses: does every text run a reader can
 * see clear the ratio its size and weight earn?
 *
 * WHAT IT MEASURED, on the rendered page, before the fix in this change
 * (light / dark; `—` means that theme already cleared the floor):
 *
 *   .refusal-badge        11px/600   3.20 / 3.24
 *   .hero-nudge           13px/500   3.68 / —
 *   .compare-footer.good  12.5px     3.60 / —
 *   span inside it        12.5px     3.60 / —
 *   .check-icon           9px        3.42 / —      (x2 — two instances)
 *   .compare-footer.bad   12.5px     —    / 3.22
 *   span inside it        12.5px     —    / 3.22
 *   .source-badge.zero    10px/700   —    / 2.92
 *   #hero-input::placeholder 16px    —    / 3.61
 *   .notice-rate-limit    11px/600   4.31 / 3.17
 *   .notice-error         11px/600   —    / 2.67
 *
 * The last two are not reachable in demo mode, so they are measured on a
 * SYNTHETIC element carrying the real classes, mounted inside the real chat
 * view. That is stated rather than hidden: it measures the stylesheet's answer
 * for those classes on that backdrop, not the transport-failure flow itself.
 *
 * WHAT THIS GUARD CANNOT SEE — written down because a guard that reads as
 * exhaustive and is not is worse than no guard:
 *   - `/about`. A static page outside the SPA; this suite never navigates to
 *     it. `public/about.css` is covered by the static `--faint` half instead.
 *   - SIGNED-IN SURFACES. The demo config runs with `VITE_API_LIVE: "false"`,
 *     so `AuthModal`, `HistoryDrawer`, `AccountMenu` and
 *     `ConnectedAccountsDrawer` never mount. `--color-error` is used inline at
 *     13-14px in two of them (`AuthModal.tsx:480`, `HistoryDrawer.tsx:136`);
 *     those ratios are COMPUTED in `tokens.css`'s comment, not rendered here.
 *   - States demo mode cannot reach: the pending bubble, the live-answer
 *     suggestion list, real transport failures.
 *   - THE BLOCKED RUNS, whose ancestor shapes are enumerated and justified in
 *     `ALLOWED_BLOCKERS` below. They are reported, not skipped — a NEW
 *     unmodellable region fails the blocked-list test by name, and a listed
 *     shape that stops existing fails it too, so the list cannot rot.
 *   - NON-TEXT CONTRAST (SC 1.4.11, 3:1 for UI components and meaningful
 *     graphics). This sweep scores TEXT RUNS only. The page has real non-text
 *     users of the same tokens — `.status-dot` and `.auto-dot`'s 6px discs,
 *     `.check-icon`'s disc against the card behind it, `.hero-input-box.shake`'s
 *     border, `.invention`'s dotted underline — and each of them clears 3:1 in
 *     both themes today (computed, recorded in this PR, not asserted here). A
 *     future retune of `--color-success` toward the light end would take three
 *     of them under the floor with this whole suite green. A named hole, not a
 *     present defect.
 *   - PSEUDO-ELEMENTS OUTSIDE ITS LIST. It visits the element, `::before`,
 *     `::after`, `::placeholder`, `::first-line`, `::first-letter` and
 *     `::marker`. `::selection` is deliberately NOT among them: its backdrop is
 *     its OWN `background`, not the element's, so scoring it through this
 *     compositor would invent a failure. `landing.css` sets it to
 *     `var(--hl-ink)` on `var(--hl)`, measured 12.40:1 light / 10.60:1 dark by
 *     hand — comfortable, and unguarded.
 *   - SVG `fill`. `score` reads `color` and `-webkit-text-fill-color`; an
 *     `<svg><text fill="…">` would be scored at its inherited `color` instead.
 *     There is no `<text>` element anywhere in `frontend/src` today (grep), so
 *     this is latent.
 *   - `text-shadow` and `-webkit-text-stroke`, which can wash a legible colour
 *     out or rescue an illegible one. Neither appears in `frontend/src` today.
 *   - `@media` BLOCKS OTHER THAN THE TWO SWEPT. This file runs at 1280 and at
 *     390 CSS px, so it sees `landing.css`'s `max-width: 900px` and
 *     `max-width: 600px` rules. A `min-width`, `print`, `prefers-contrast` or
 *     `forced-colors` block would be invisible. None exists today; the only
 *     other queries in the app are `prefers-reduced-motion` and
 *     `prefers-color-scheme`.
 *   - STATE PSEUDO-CLASSES. `:hover`, `:focus`, `:active` and `:visited`
 *     colours are never resolved. Every hover/focus rule in `landing.css`
 *     today moves colour TOWARD `var(--ink)`, which is the safe direction.
 *   - AN OVERLAY SIBLING. The compositor walks ANCESTORS, so an
 *     absolutely-positioned sibling painting over a run is invisible to it —
 *     measured by a reviewer as 21:1 reported against 1.17:1 real.
 *     `frontend/tests/focus-ring.spec.ts` answers that class from rendered
 *     pixels; this sweep cannot.
 *   - NON-TEXT CONTRAST is listed above; so is everything a floor cannot
 *     express: text over a photo, a moving gradient, colour used as the only
 *     carrier of meaning (SC 1.4.1).
 *
 * ONE LINE PER TEST names the change that turns it red.
 */
import { test, expect } from "./fixtures";
import { ensureTheme, enterChat, gotoApp, waitStreamDone, type ThemeName } from "./helpers";
import { installKit, wcagFloor, type FloorSweep } from "./contrast-kit";

const THEMES: ThemeName[] = ["light", "dark"];

/** iPhone 14 metrics — under both of `landing.css`'s breakpoints (900px, 600px). */
const PHONE = { width: 390, height: 844 };

/**
 * The ancestor shapes whose paint the flat-`background-color` compositor
 * genuinely cannot model, each with the reason it is allowed to stay
 * unmeasured. A blocked run whose blocker is NOT on this list fails the test.
 *
 * Matching is on the blocker STRING's leading `element property:` part, not on
 * the blocked element's text, so a copy edit does not move the guard.
 */
const ALLOWED_BLOCKERS: { prefix: string; why: string }[] = [
  {
    prefix: "header.header backdrop-filter:",
    // `background: color-mix(in srgb, var(--bg) 82%, transparent)` over a
    // 12px blur of whatever has scrolled under it. The backdrop genuinely is
    // not a flat colour, and it changes as the page scrolls.
    why: "the sticky header is a translucent blur over scrolling content",
  },
  {
    prefix: "span.highlight background-image:",
    why: "the yellow underline band is a gradient; fidelity.spec.ts D2.5 checks it from real PIXELS",
  },
  {
    prefix: "span.highlight-phrase background-image:",
    why: "same gradient band, small variant; same pixel check covers it",
  },
];
// `button.cta.cta-outlined opacity:` used to be a fourth entry here, for
// "Contact sales". It is gone because it was keyed on the WRONG THING: the
// reason that button is exempt is that it is `:disabled` and SC 1.4.3 excludes
// inactive components, but the string being matched was its `opacity: 0.5` —
// so the exemption would have evaporated the day the opacity moved, and any
// ENABLED control that acquired an opacity would have been waved through.
// `isVisible` in the kit now skips disabled controls by their disabled-ness,
// which is why no run is blocked by that shape any more.

/** Selectors #403 retuned, with the theme(s) in which they were below AA. */
type Probe = {
  selector: string;
  pseudo?: string;
  why: string;
  /** Mount it first — demo mode does not reach this state on its own. */
  synthetic?: boolean;
};

const RETUNED: Probe[] = [
  { selector: ".refusal-badge", why: "--color-warning on --hl-soft: 3.20 light / 3.24 dark" },
  { selector: ".compare-footer.good", why: "--color-success on --surface: 3.60 light" },
  { selector: ".compare-footer.bad", why: "--color-error-strong on --surface: 3.22 dark" },
  { selector: ".source-badge.zero", why: "--color-error-strong on --surface-2: 2.92 dark" },
  { selector: ".check-icon", why: "--bg on the --color-success disc: 3.42 light" },
  { selector: ".hero-nudge", why: "--color-warning on --bg: 3.68 light" },
  {
    selector: "#hero-input",
    pseudo: "::placeholder",
    why: "unstyled, so Chromium's rgb(117, 117, 117) in BOTH themes: 3.61 dark",
  },
  { selector: ".notice-rate-limit", synthetic: true, why: "hardcoded amber: 4.31 light / 3.17 dark" },
  { selector: ".notice-error", synthetic: true, why: "hardcoded red: 2.67 dark" },
];

/**
 * Put the landing page into the state that renders every probe except the
 * nudge: the lazy below-the-fold strip mounted and the REFUSAL demo answer
 * chosen.
 */
async function openEveryProbedState(page: import("@playwright/test").Page) {
  await page.locator("#pricing").scrollIntoViewIfNeeded();
  await expect(page.locator("footer")).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));

  // The refusal demo answer — `.refusal-badge` renders only for this one.
  await page.locator(".demo-question").nth(3).click();
  await waitStreamDone(page);
  await expect(page.locator(".demo-right .refusal-badge")).toBeVisible();
}

/**
 * Show the empty-Ask nudge and hold until its fade-up has actually landed.
 *
 * SEPARATE from `openEveryProbedState`, and always called immediately before
 * the measurement that needs it, because `useLandingState.ts` clears the nudge
 * on a hard 3000 ms timer. Opening it early and measuring after a few round
 * trips is a race the measurement loses silently — the element is simply gone
 * and the probe reports "matched zero elements" on a slow machine only.
 *
 * The opacity poll is not politeness either. `.hero-nudge` is
 * `animation: cv-fadeup 0.25s ease both`, so at the instant it appears its
 * computed `opacity` is 0 — the compositor then correctly REFUSES to score it
 * and the run lands in `blocked` rather than being measured. Measured: sampling
 * immediately reports `opacity:0`, the sweep sees nothing, and a genuine
 * 3.68:1 failure reads as a pass.
 */
async function showNudge(page: import("@playwright/test").Page) {
  await page.locator(".ask-button").click();
  await expect(page.locator(".hero-nudge")).toBeVisible();
  await expect
    .poll(() => page.locator(".hero-nudge").evaluate((el) => getComputedStyle(el).opacity), {
      timeout: 2000,
    })
    .toBe("1");
}

/**
 * Mount the two transport-failure badges where a real one would render.
 *
 * Demo mode never produces a 429 or a 5xx, so `ChatView`'s `errorKind` branches
 * are unreachable here and the only honest alternative to this is not measuring
 * them at all — which is how they shipped at 2.67:1. The badge is appended to a
 * REAL `.message.bot-msg .content`, the exact parent
 * `ChatView.tsx:536`/`:539` give it, so the backdrop it composites over is the
 * one a reader would get. What this does NOT exercise is the transport-failure
 * flow itself; that belongs to the live suite.
 */
async function mountSyntheticNotices(page: import("@playwright/test").Page) {
  // A real answer first, so a real `.content` exists to host the badge.
  await page.locator(".chat-input").fill("What is Claude Code?");
  await page.keyboard.press("Enter");
  await waitStreamDone(page);
  await expect(page.locator(".message.bot-msg .content").first()).toBeVisible();

  // The source cards fade up (`animation: cv-fadeup … both`), so at the instant
  // they mount their computed `opacity` is 0 and the compositor correctly
  // REFUSES to score anything inside them. Sweeping there reported every source
  // title and url as an unexplained blocker rather than measuring them — the
  // same shape as `.hero-nudge` on the landing page. Held until it settles, so
  // they are MEASURED rather than skipped.
  await expect(page.locator(".message.bot-msg .sources").first()).toBeVisible();
  await expect
    .poll(() =>
      page.locator(".message.bot-msg .sources").first().evaluate((el) => getComputedStyle(el).opacity),
    )
    .toBe("1");

  const mounted = await page.evaluate(() => {
    const host = document.querySelector(".message.bot-msg .content");
    if (!host) return 0;
    for (const cls of ["notice-rate-limit", "notice-error"]) {
      const el = document.createElement("div");
      el.className = `notice-badge ${cls}`;
      el.textContent = "SYNTHETIC PROBE";
      host.appendChild(el);
    }
    return document.querySelectorAll(".notice-badge").length;
  });
  // The precondition, asserted rather than assumed: if the host selector went
  // away, nothing would be mounted and the probes below would report "matched
  // zero elements" — a failure whose cause reads as a renamed class rather than
  // as this helper.
  expect(mounted, "the synthetic notice badges were not mounted into a real answer bubble").toBe(2);
}

/**
 * Blocked runs whose reasons are not ALL on the allow-list.
 *
 * Per BLOCKER, not per entry. `entry.includes(prefix)` over the joined string
 * was a demonstrated bypass: a reviewer put a `filter: contrast(0.2)` element
 * inside `header.header`, the entry read `p.cv-waved filter:contrast(0.2);
 * header.header backdrop-filter:blur(12px)`, the allowed half matched, and the
 * unexplained filter was waved through with the suite green.
 */
function unexplainedBlockers(sweep: FloorSweep): string[] {
  const out: string[] = [];
  for (const run of sweep.blocked) {
    for (const blocker of run.blockers) {
      if (!ALLOWED_BLOCKERS.some(({ prefix }) => blocker.startsWith(prefix))) {
        out.push(`${run.what} sits under ${blocker}`);
      }
    }
  }
  return out;
}

/**
 * The derived non-vacuity partner, asserted in EVERY sweep.
 *
 * `measured + blocked` must account for every visible text run the walk found.
 * A threshold cannot do this job: a reviewer hid the landing page's sections
 * one at a time and `examined` stayed at 1279 while `measured` fell from 260 to
 * 10, and `measured > 200` still passed with two whole sections gone.
 *
 * A blocker REMOVES a run from `findings`, so the allow-list has to be checked
 * wherever a sweep is checked — not only in the one desktop landing test, which
 * is what let `.citation-chip .chip-bracket { opacity: 0.45 }` and
 * `.send-button[aria-disabled] { opacity: 0.7 }` sit in an unaudited state.
 */
function expectSweepAccountsForEverything(label: string, sweep: FloorSweep, minRuns: number) {
  expect(
    sweep.measured + sweep.blocked.length,
    `${label}: the sweep found ${sweep.textRuns} visible text runs but accounts for ` +
      `${sweep.measured} measured + ${sweep.blocked.length} blocked`,
  ).toBe(sweep.textRuns);
  expect(sweep.textRuns, `${label}: the sweep found almost no text at all`).toBeGreaterThan(minRuns);
  expect(unexplainedBlockers(sweep), `${label}: unexplained unmodellable ancestors`).toEqual([]);
}

function reportFindings(label: string, sweep: FloorSweep): string[] {
  return sweep.findings.map(
    (f) =>
      `${label}: ${f.what} — ${f.ratio.toFixed(2)}:1 needs ${f.floor}:1 ` +
      `(${f.fontSize}px/${f.fontWeight}, ${f.colour} on ${f.backdrop})`,
  );
}

for (const theme of THEMES) {
  test.describe(`[${theme}] every text run clears its WCAG floor (#403)`, () => {
    test.beforeEach(async ({ page }) => {
      await installKit(page);
      await gotoApp(page);
      await ensureTheme(page, theme);
    });

    test("the landing page, in every state the demo can reach", async ({ page }, testInfo) => {
      // RED WHEN: any of the nine selectors in `RETUNED` goes back to its
      // pre-#403 colour — verified one at a time, each reverting to exactly the
      // before-ratio quoted in this file's header.
      await openEveryProbedState(page);
      await showNudge(page);
      const sweep = await page.evaluate(() => window.__cv396.floorSweep());
      await testInfo.attach(`floor-landing-${theme}`, {
        body: JSON.stringify(sweep, null, 2),
        contentType: "application/json",
      });
      expectSweepAccountsForEverything("landing", sweep, 200);
      expect(reportFindings("landing", sweep)).toEqual([]);
    });

    test("the landing page at a PHONE viewport", async ({ page }) => {
      // The bypass #396 learned the hard way: a desktop-only suite cannot see
      // `@media (max-width: …)` rules at all, so a colour added inside one is
      // invisible to every gate while every phone visitor renders it.
      // RED WHEN: a below-floor `color` is added to either `@media (max-width:)`
      // block in `landing.css` — verified with `.composer-hint { color: var(--faint) }`
      // in the 600px block, which this test reports and the desktop sweep does not.
      await page.setViewportSize(PHONE);
      const active = await page.evaluate(() => ({
        under900: matchMedia("(max-width: 900px)").matches,
        under600: matchMedia("(max-width: 600px)").matches,
        heroDescription: getComputedStyle(document.querySelector(".hero-description")!).fontSize,
      }));
      expect(active.under900, "the 900px breakpoint is not active at this viewport").toBe(true);
      expect(active.under600, "the 600px breakpoint is not active at this viewport").toBe(true);
      // 19px is the desktop rule; the 600px block overrides it to 16px. Without
      // this, a viewport change that silently stopped applying would leave two
      // identical sweeps.
      expect(active.heroDescription, "the mobile override did not take effect").toBe("16px");

      await openEveryProbedState(page);
      await showNudge(page);
      const sweep = await page.evaluate(() => window.__cv396.floorSweep());
      expectSweepAccountsForEverything("phone", sweep, 200);
      expect(reportFindings("phone", sweep)).toEqual([]);
    });

    test("the chat view, transport-failure badges included", async ({ page }) => {
      // RED WHEN: `.notice-rate-limit` or `.notice-error` goes back to its
      // hardcoded rgba() — verified, and the dark theme reports 3.17:1 and
      // 2.67:1 respectively.
      await enterChat(page);
      await mountSyntheticNotices(page);
      const sweep = await page.evaluate(() => window.__cv396.floorSweep());
      expectSweepAccountsForEverything("chat", sweep, 10);
      expect(reportFindings("chat", sweep)).toEqual([]);
    });

    test("every selector #403 retuned, measured by name with its floor", async ({ page }, testInfo) => {
      // The whole-page sweeps above report a CLEAN page many ways — including
      // by never reaching the elements in question. This names each one, fails
      // when it matches nothing, and records the after-ratio.
      // RED WHEN: any listed class is renamed (the zero-match assertion fires by
      // name), or any listed run drops below its floor.
      await openEveryProbedState(page);
      const report: Record<string, string> = {};
      const missing: string[] = [];
      const tooLow: string[] = [];
      const blocked: string[] = [];

      const measureAll = async (probes: Probe[]) => {
        for (const { selector, pseudo, why } of probes) {
          const n = await page.evaluate((s) => window.__cv396.count(s), selector);
          if (n === 0) {
            missing.push(`${selector} (${why})`);
            continue;
          }
          for (let i = 0; i < n; i++) {
            const run = await page.evaluate(
              ([s, idx, p]) =>
                window.__cv396.measure(s as string, idx as number, (p as string) || undefined),
              [selector, i, pseudo ?? ""] as const,
            );
            if (!run) {
              missing.push(`${selector}[${i}] could not be measured (${why})`);
              continue;
            }
            if (run.blockers.length) {
              blocked.push(`${selector}[${i}] sits under ${run.blockers.join("; ")}`);
              continue;
            }
            const key = `${selector}${pseudo ?? ""}[${i}]`;
            report[key] = `${run.ratio.toFixed(2)}:1 (floor ${run.floor}) ${run.colour} on ${run.backdrop}`;
            if (run.ratio < run.floor) {
              tooLow.push(`${key} ${run.ratio.toFixed(2)}:1 needs ${run.floor}:1 — ${why}`);
            }
          }
        }
      };

      await measureAll(RETUNED.filter((p) => !p.synthetic && p.selector !== ".hero-nudge"));
      // The nudge is opened LAST on this screen and measured immediately: it is
      // cleared on a hard 3 s timer, so measuring it after the loop above would
      // be a race this test loses only on a slow machine.
      await showNudge(page);
      await measureAll(RETUNED.filter((p) => p.selector === ".hero-nudge"));
      await enterChat(page);
      await mountSyntheticNotices(page);
      await measureAll(RETUNED.filter((p) => p.synthetic));

      await testInfo.attach(`retuned-${theme}`, {
        body: JSON.stringify(report, null, 2),
        contentType: "application/json",
      });
      expect(missing, "these matched zero elements, so they measured nothing").toEqual([]);
      expect(blocked, "the composite cannot be trusted over these ancestors").toEqual([]);
      expect(tooLow, "below their WCAG floor").toEqual([]);
      // One measurement per probe, at least — the loop must have run.
      expect(Object.keys(report).length).toBeGreaterThanOrEqual(RETUNED.length);
    });

    test("the runs it REFUSES to measure are only the four known shapes", async ({ page }) => {
      // A sweep can also be clean because it declared everything unmeasurable.
      // This pins the refusals to an enumerated list with a written reason each,
      // so a new gradient, filter or translucent ancestor over real text is a
      // named failure rather than a silent hole.
      // RED WHEN: a new unmodellable ancestor is introduced over text, or
      // `blockersOf`'s `stopAtOpaque` early exit is removed (which reports every
      // element under any decorated ancestor and the list explodes).
      await openEveryProbedState(page);
      await showNudge(page);
      const sweep = await page.evaluate(() => window.__cv396.floorSweep());

      // Non-vacuity: the shapes below EXIST on this page, so an empty `blocked`
      // list means the blocker detection died, not that the page got simpler.
      expect(sweep.blocked.length, "nothing was reported as unmeasurable — the blocker pass is dead")
        .toBeGreaterThan(0);

      expect(
        unexplainedBlockers(sweep),
        "these text runs sit under something this compositor cannot model, and no reason is " +
          "recorded for them in ALLOWED_BLOCKERS",
      ).toEqual([]);

      // And every allowed shape is still PRESENT. Without this the allow-list
      // rots into a list of things that stopped existing, and the day one comes
      // back it is waved through by a rule nobody re-read.
      const everyBlocker = sweep.blocked.flatMap((run) => run.blockers);
      for (const { prefix, why } of ALLOWED_BLOCKERS) {
        expect(
          everyBlocker.some((blocker) => blocker.startsWith(prefix)),
          `no run is blocked by "${prefix}" any more (${why}) — drop it from ALLOWED_BLOCKERS ` +
            "so the list keeps meaning what it says",
        ).toBe(true);
      }

      // THE BYPASS THIS SHAPE EXISTS FOR, planted and required to be reported.
      // A run under BOTH an allowed shape and an unexplained one used to be
      // waved through on the allowed half — reproduced by a reviewer inside the
      // header, which is why the plant goes there too.
      // RED WHEN: `unexplainedBlockers` goes back to matching the joined entry
      // instead of each blocker.
      const planted = await page.evaluate(() => {
        const el = document.createElement("p");
        el.className = "cv403-waved";
        el.textContent = "waved through";
        el.style.filter = "contrast(0.2)";
        document.querySelector("header.header")!.appendChild(el);
        const out = window.__cv396.floorSweep();
        el.remove();
        return { sweep: out, run: out.blocked.find((r) => r.what.includes("cv403-waved")) ?? null };
      });
      expect(planted.run, "the plant was not blocked at all").not.toBeNull();
      // The preconditions: the plant really did pick up BOTH an allowed blocker
      // and an unexplained one. Without them a green result below could mean
      // the plant never landed.
      expect(
        planted.run!.blockers.some((b) => b.startsWith("header.header backdrop-filter:")),
        "the plant did not pick up the allowed header blocker, so it proves nothing",
      ).toBe(true);
      expect(
        planted.run!.blockers.some((b) => b.includes("filter:contrast(0.2)")),
        "the unexplained filter was not recorded as its own blocker",
      ).toBe(true);
      // AND THE MATCHER REPORTS IT. This is the assertion the fix is for:
      // asserting the kit's structure alone left the entry-level `includes`
      // mutant ALIVE, because the kit returns both blockers either way.
      expect(
        unexplainedBlockers(planted.sweep).join(" | "),
        "the unexplained filter was waved through on the allowed header blocker",
      ).toContain("filter:contrast(0.2)");
    });

    test("an unmodellable ancestor ABOVE an opaque background is still reported", async ({
      page,
    }) => {
      // `stopAtOpaque` stops the BACKGROUND walk at the first opaque ancestor,
      // which is right — nothing behind it paints through. An earlier version
      // stopped the WHOLE walk there, and that was unsound: `opacity`,
      // `filter`, `backdrop-filter` and `mix-blend-mode` transform the subtree
      // AFTER it is drawn, so an opaque background underneath is no protection.
      // A reviewer measured all four against real screenshot pixels — a group
      // `opacity: .5` over an opaque white card read 4.54:1 from this
      // compositor against 1.96:1 from the pixels, and `mix-blend-mode: screen`
      // read 21:1 against a real 1.00:1 with the glyphs invisible.
      //
      // Nothing in this app has that shape today, so without these plants the
      // whole fix is an EQUIVALENT MUTATION — verified: reverting it left every
      // other test in this file green.
      // RED WHEN: `blockersOf` goes back to `break`ing out of the loop when it
      // reaches an opaque background, or drops the `mix-blend-mode` push.
      for (const [prop, value, needle] of [
        ["opacity", "0.5", "opacity:0.5"],
        ["filter", "contrast(0.2)", "filter:contrast(0.2)"],
        ["mixBlendMode", "screen", "mix-blend-mode:screen"],
        ["backdropFilter", "blur(3px)", "backdrop-filter:blur(3px)"],
      ] as const) {
        const out = await page.evaluate(
          ([prop, value]) => {
            // outer (the unmodellable shape) > opaque card > the text.
            const outer = document.createElement("div");
            outer.className = "cv403-outer";
            (outer.style as unknown as Record<string, string>)[prop as string] = value as string;
            const card = document.createElement("div");
            card.style.background = "#ffffff";
            const text = document.createElement("p");
            text.className = "cv403-under-opaque";
            text.textContent = "text on an opaque card, under something else";
            text.style.color = "#595959";
            card.appendChild(text);
            outer.appendChild(card);
            document.body.appendChild(outer);
            const applied = (getComputedStyle(outer) as unknown as Record<string, string>)[
              prop as string
            ];
            const cardIsOpaque = getComputedStyle(card).backgroundColor;
            const run = window.__cv396.measure(".cv403-under-opaque", 0);
            outer.remove();
            return { applied, cardIsOpaque, blockers: run ? run.blockers : ["<not measured>"] };
          },
          [prop, value] as const,
        );
        // Preconditions: the engine accepted the property, and the card really
        // is the opaque ancestor the walk would otherwise stop at.
        expect(out.applied, `${prop} was not accepted by the engine`).not.toBe("none");
        expect(out.cardIsOpaque, "the planted card is not opaque").toBe("rgb(255, 255, 255)");
        expect(
          out.blockers.join(" | "),
          `${prop} above an opaque background was not reported`,
        ).toContain(needle);
      }
    });

    test("visually-hidden text and inactive controls are out of scope, and only those", async ({
      page,
    }) => {
      // TWO EXEMPTIONS, each proved in BOTH directions against the same colour,
      // so neither can pass on a dead checker and neither can quietly grow into
      // "skip everything".
      //
      //   - `.sr-only` is `width: 1px; height: 1px; clip-path: inset(50%)`, so
      //     it HAS a rect and the old `> 0.5px` rule scored it: a reviewer
      //     planted one at 2.88:1 and the sweep reported a failure over text no
      //     sighted reader can see. `ChatView.tsx` ships two such regions.
      //   - SC 1.4.3 exempts "text … that is part of an inactive user interface
      //     component". `.cta:disabled` used to escape only incidentally, via
      //     its `opacity: 0.5` making it a blocker — keying on the wrong thing.
      // RED WHEN: the 2px rect bar or the `[disabled]` check is removed from
      // `isVisible` (the negative halves), or widened to skip real text (the
      // positive halves).
      const seen = await page.evaluate(() => {
        const mk = (cls: string, apply: (el: HTMLElement) => void, tag = "p") => {
          const el = document.createElement(tag);
          el.className = cls;
          el.textContent = "planted";
          el.style.color = "#949494"; // 2.85:1 on white — below the floor
          el.style.background = "#ffffff";
          el.style.fontSize = "12px";
          apply(el);
          document.body.appendChild(el);
          return el;
        };
        const hidden = mk("cv403-sronly", (el) => {
          el.style.width = "1px";
          el.style.height = "1px";
          el.style.overflow = "hidden";
          el.style.clipPath = "inset(50%)";
        });
        const shown = mk("cv403-shown", () => {});
        const off = mk("cv403-disabled", (el) => el.setAttribute("disabled", ""), "button");
        const on = mk("cv403-enabled", () => {}, "button");
        const rects = {
          hiddenW: hidden.getBoundingClientRect().width,
          shownW: shown.getBoundingClientRect().width,
          offDisabled: (off as HTMLButtonElement).disabled,
          onDisabled: (on as HTMLButtonElement).disabled,
        };
        const named = window.__cv396.floorSweep().findings.map((f) => f.what);
        for (const el of [hidden, shown, off, on]) el.remove();
        return { named, rects };
      });

      // Preconditions: the plants really are the shapes they claim to be.
      expect(seen.rects.hiddenW, "the sr-only plant is not 1px wide").toBeLessThan(2);
      expect(seen.rects.shownW, "the visible plant has no width").toBeGreaterThan(20);
      expect(seen.rects.offDisabled, "the disabled plant is not disabled").toBe(true);
      expect(seen.rects.onDisabled, "the enabled plant is disabled").toBe(false);

      const named = seen.named.join(" | ");
      expect(named, "a visually-hidden 1x1 run was scored").not.toContain("cv403-sronly");
      expect(named, "an inactive control's text was scored").not.toContain("cv403-disabled");
      expect(named, "a NORMAL run at the same colour was not scored").toContain("cv403-shown");
      expect(named, "an ENABLED control at the same colour was not scored").toContain(
        "cv403-enabled",
      );
    });

    test("the sweep can still SEE a run below its floor", async ({ page }) => {
      // The detector-still-works partner. Four green "no findings" results above
      // are indistinguishable from a sweep that returns [] unconditionally.
      //
      // TWO elements are planted, the COMPLIANT one first, and both are appended
      // at the very end of <body>. That shape is deliberate: a `continue` turned
      // into a `break` anywhere in the sweep's loops stops the walk early, and
      // an offender planted at the end is then never reached. Verified by making
      // that mutation.
      // RED WHEN: `floorSweep` stops comparing `ratio` against `floor`, or any
      // `continue` in its loops becomes a `break`.
      const before = await page.evaluate(() => window.__cv396.floorSweep());
      expect(reportFindings("pre-plant", before)).toEqual([]);

      const planted = await page.evaluate(() => {
        const mk = (cls: string, colour: string) => {
          const el = document.createElement("p");
          el.className = cls;
          el.textContent = "planted";
          el.style.color = colour;
          el.style.background = "#ffffff";
          el.style.fontSize = "12px";
          document.body.appendChild(el);
          return getComputedStyle(el).color;
        };
        return {
          // #595959 on white is 7.00:1 — comfortably over the floor.
          compliant: mk("cv403-compliant", "#595959"),
          // #949494 on white is 2.85:1 — under 4.5:1 at 12px.
          offender: mk("cv403-planted-offender", "#949494"),
        };
      });
      // Preconditions: both plants really took the colours they were given, so
      // "reported" and "not reported" mean what they say.
      expect(planted.compliant).toBe("rgb(89, 89, 89)");
      expect(planted.offender).toBe("rgb(148, 148, 148)");

      const after = await page.evaluate(() => window.__cv396.floorSweep());
      const named = after.findings.map((f) => f.what);
      expect(named.join(" | "), "the planted low-contrast run was not reported").toContain(
        "cv403-planted-offender",
      );
      expect(named.join(" | "), "the COMPLIANT plant was reported as a failure").not.toContain(
        "cv403-compliant",
      );
      // EVERY finding is the offender, and nothing else on the page joined it.
      // Not `length === 1`: the sweep visits `::first-line` and `::first-letter`
      // as well as the element, and all three legitimately paint the same
      // illegible glyphs, so the offender is named more than once by design.
      expect(
        named.filter((w) => !w.includes("cv403-planted-offender")),
        "the plant was not the only thing reported",
      ).toEqual([]);
      expect(named.length, "the plant was reported zero times").toBeGreaterThan(0);

      await page.evaluate(() => {
        document.querySelector(".cv403-planted-offender")?.remove();
        document.querySelector(".cv403-compliant")?.remove();
      });
      expect(
        reportFindings("post-plant", await page.evaluate(() => window.__cv396.floorSweep())),
      ).toEqual([]);
    });

    test("the sweep scores -webkit-text-fill-color, which OVERRIDES color on the glyphs", async ({
      page,
    }) => {
      // The bypass #396 measured and closed for its token ban, asserted here for
      // the floor. `getComputedStyle(el).color` keeps reporting the legible
      // value while the glyphs paint at the fill colour, so a `color`-only
      // sweep reads a comfortable ratio off text nobody can read.
      // RED WHEN: `score` stops preferring `webkitTextFillColor` over `color`.
      const readings = await page.evaluate(() => {
        const el = document.createElement("p");
        el.className = "cv403-fill-offender";
        el.textContent = "planted";
        el.style.background = "#ffffff";
        el.style.fontSize = "12px";
        // `color` stays LEGIBLE (7.00:1). Only the fill is illegible (2.85:1),
        // so nothing but the text-fill pass can find this element.
        el.style.color = "#595959";
        el.style.webkitTextFillColor = "#949494";
        document.body.appendChild(el);
        const s = getComputedStyle(el);
        return { color: s.color, fill: s.webkitTextFillColor };
      });
      // The precondition: if the browser had refused the property the plant
      // would be a no-op and the assertion below would pass for the wrong
      // reason.
      expect(readings.color, "the plant's `color` was not left legible").not.toBe(readings.fill);

      const run = await page.evaluate(() =>
        window.__cv396.measure(".cv403-fill-offender", 0),
      );
      expect(run, "the plant was not found").not.toBeNull();
      expect(run!.colour, "the fill colour is not what was scored").toContain("148, 148, 148");
      expect(run!.ratio).toBeLessThan(run!.floor);

      const sweep = await page.evaluate(() => window.__cv396.floorSweep());
      expect(sweep.findings.map((f) => f.what).join(" | ")).toContain("cv403-fill-offender");

      await page.evaluate(() => document.querySelector(".cv403-fill-offender")?.remove());
      expect(
        reportFindings("post-plant", await page.evaluate(() => window.__cv396.floorSweep())),
      ).toEqual([]);
    });

    test("a wrapper div is not scored for text its children paint", async ({ page }) => {
      // The shape that makes a whole-page floor sweep either useful or noise.
      // Scoring every element that merely CONTAINS text would report each run
      // once per ancestor, at whatever colour each ancestor inherited — so a
      // legible `<span>` inside an illegible-coloured `<div>` would be a
      // finding, and real failures would drown.
      // RED WHEN: `paintsText` stops requiring a non-whitespace DIRECT child
      // text node (e.g. it falls back to `el.textContent`).
      const seen = await page.evaluate(() => {
        const wrap = document.createElement("div");
        wrap.className = "cv403-wrapper";
        wrap.style.color = "#949494"; // 2.85:1 — below the floor
        wrap.style.background = "#ffffff";
        wrap.style.fontSize = "12px";
        const child = document.createElement("span");
        child.className = "cv403-child";
        child.style.color = "#595959"; // 7.00:1 — over it
        child.textContent = "legible";
        wrap.appendChild(child);
        document.body.appendChild(wrap);
        const out = window.__cv396.floorSweep().findings.map((f) => f.what);
        document.querySelector(".cv403-wrapper")?.remove();
        return out;
      });
      expect(
        seen.join(" | "),
        "the wrapper was scored for its child's text, at the wrapper's own colour",
      ).not.toContain("cv403-wrapper");

      // The partner: a wrapper that owns its OWN text node IS scored, so the
      // rule above is "no direct text", not "never score a div".
      const withOwnText = await page.evaluate(() => {
        const wrap = document.createElement("div");
        wrap.className = "cv403-wrapper-with-text";
        wrap.style.color = "#949494";
        wrap.style.background = "#ffffff";
        wrap.style.fontSize = "12px";
        wrap.appendChild(document.createTextNode("its own words"));
        const child = document.createElement("span");
        child.style.color = "#595959";
        child.textContent = "legible";
        wrap.appendChild(child);
        document.body.appendChild(wrap);
        const out = window.__cv396.floorSweep().findings.map((f) => f.what);
        document.querySelector(".cv403-wrapper-with-text")?.remove();
        return out;
      });
      expect(
        withOwnText.join(" | "),
        "a wrapper with its OWN illegible text node was not scored",
      ).toContain("cv403-wrapper-with-text");
    });

    test("the site header's own text clears AA over every surface it can float on", async ({
      page,
    }) => {
      // C1. THE STRUCTURAL HOLE THE ALLOW-LIST OPENS, closed on its own terms.
      //
      // `blockersOf` walks ANCESTORS, so allowing `header.header
      // backdrop-filter:` exempts every text run inside `<header>` — the logo
      // and all five nav links, in both themes and on both screens. A reviewer
      // proved the consequence by painting the logo `#fbfbfb` on the near-white
      // header: the sweep reported zero findings and the allow-list test stayed
      // green. The primary navigation of the site was never contrast-checked.
      //
      // The sweep cannot fix this — the header really is a 12px blur of
      // whatever has scrolled under it, and that is not a flat colour. So this
      // test answers the question the sweep cannot, DETERMINISTICALLY and
      // conservatively: composite the header's own
      // `color-mix(in srgb, var(--bg) 82%, transparent)` over each opaque
      // surface the page can put behind it, and require the header's text to
      // clear its floor over EVERY one. That is stricter than reality, because
      // the blur averages toward the mean rather than reaching either extreme.
      //
      // RED WHEN: any header text colour, or the header's own background alpha,
      // moves such that the worst case drops under the floor — verified by
      // painting the logo `#fbfbfb`, which reports 1.06:1 here.
      const worst = await page.evaluate(() => {
        const header = document.querySelector("header.header") as HTMLElement;
        const kit = window.__cv396;
        // The header's own background, un-premultiplied, with its alpha.
        const own = kit.parse(getComputedStyle(header).backgroundColor)!;
        // Every opaque page surface that can scroll under a sticky header.
        const behind = ["var(--bg)", "var(--surface)", "var(--surface-2)", "var(--ink)", "var(--hl)"]
          .map((token) => ({ token, rgb: kit.resolve(token) }))
          .filter((x): x is { token: string; rgb: number[] } => !!x.rgb);

        const lum = (rgb: number[]) => {
          const c = rgb.map((v) => {
            const x = v / 255;
            return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
          });
          return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
        };
        const ratio = (a: number[], b: number[]) => {
          const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
          return (hi + 0.05) / (lo + 0.05);
        };

        const runs: { what: string; over: string; ratio: number; floor: number }[] = [];
        const texts = Array.from(header.querySelectorAll<HTMLElement>("*")).filter((el) =>
          Array.from(el.childNodes).some(
            (n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? "").trim().length > 0,
          ),
        );
        for (const el of texts) {
          const cs = getComputedStyle(el);
          const colour = kit.parse(cs.webkitTextFillColor) ?? kit.parse(cs.color);
          if (!colour || (colour[3] ?? 1) <= 0.05) continue;
          const size = parseFloat(cs.fontSize);
          const weight = Number(cs.fontWeight) || 400;
          const pt = size * 0.75;
          const floor = pt >= 18 || (pt >= 14 && weight >= 700) ? 3 : 4.5;
          // Any opaque background the element itself paints wins outright.
          const ownBg = kit.parse(cs.backgroundColor);
          for (const { token, rgb } of behind) {
            const base =
              ownBg && (ownBg[3] ?? 1) > 0.999
                ? ownBg.slice(0, 3)
                : [0, 1, 2].map((i) => own[3] * own[i] + (1 - own[3]) * rgb[i]);
            const a = colour[3] ?? 1;
            const painted = [0, 1, 2].map((i) => a * colour[i] + (1 - a) * base[i]);
            runs.push({
              what: `${el.tagName.toLowerCase()}.${el.className || "-"} — "${(el.textContent ?? "").trim().slice(0, 24)}"`,
              over: token,
              ratio: ratio(painted, base),
              floor,
            });
          }
        }
        return runs;
      });

      // Non-vacuity: the header really does carry text, over several surfaces.
      // Without this, an empty `worst` passes the assertion below silently —
      // which is the precise shape that let the header go unchecked at all.
      const subjects = new Set(worst.map((r) => r.what));
      expect(subjects.size, "no text was found in header.header").toBeGreaterThan(1);
      expect(new Set(worst.map((r) => r.over)).size, "no surfaces were composited").toBe(5);

      const tooLow = worst
        .filter((r) => r.ratio < r.floor)
        .map((r) => `${r.what} over ${r.over}: ${r.ratio.toFixed(2)}:1 needs ${r.floor}:1`);
      expect(tooLow, "header text below AA over a surface it can float on").toEqual([]);
    });

    test("the floor the SWEEP applies follows WCAG's large-text definition", async ({ page }) => {
      // ASSERTED THROUGH THE SWEEP, not through the exported helper.
      //
      // `installKit` serializes its own copy of the rule into the page — an
      // init script cannot close over module scope — so pinning the exported
      // `wcagFloor` proves nothing about the floor that decides real findings.
      // A reviewer showed the gap by inspection: changing the in-page copy to a
      // flat `3` would have left every boundary assertion green. These plant
      // real elements and read back the floor the SWEEP assigned them.
      //
      // A guard that applied one flat 4.5:1 would be stricter than the
      // criterion and would fail legitimate display type; one that applied 3:1
      // everywhere would wave through the 9px and 10px runs this change exists
      // to fix. The boundary is where both mistakes live.
      // RED WHEN: the in-page `floorFor` changes either threshold, or drops the
      // weight condition from the 14pt branch.
      const cases: [number, number, number][] = [
        // px, weight, expected floor. 18pt = 24px; 14pt = 18.6666…px.
        [23.9, 400, 4.5],
        [24, 400, 3],
        [24, 700, 3],
        [18.7, 400, 4.5], // large only at weight >= 700 below 18pt
        [18.7, 700, 3],
        [18.5, 700, 4.5], // 13.875pt — under 14pt even at weight 700
        // 18.66px is 13.995pt and 18.68px is 14.01pt. The pair straddles 14pt
        // while staying outside Chromium's 1/64px font-size quantisation, which
        // is why they are not written as 18.666 / 18.6667: the engine rounds
        // 18.666px to 18.671875px, so a test written at the exact boundary
        // asserts a size the browser never applied. Measured before choosing
        // these values.
        [18.66, 700, 4.5],
        [18.68, 700, 3],
        // The sizes this app actually ships at the failing end.
        [9, 400, 4.5],
        [10, 700, 4.5],
        [11, 600, 4.5],
        [12.5, 400, 4.5],
        [16, 400, 4.5],
      ];
      const got = await page.evaluate((rows) => {
        const out: { px: number; weight: number; floor: number | null; size: string }[] = [];
        for (const [px, weight] of rows) {
          const el = document.createElement("p");
          el.className = "cv403-floor-probe";
          el.textContent = "probe";
          el.style.fontSize = `${px}px`;
          el.style.fontWeight = String(weight);
          document.body.appendChild(el);
          const run = window.__cv396.measure(".cv403-floor-probe", 0);
          out.push({
            px,
            weight,
            floor: run ? run.floor : null,
            // The precondition: the engine kept the size we asked for. A
            // rounded or rejected value would make a "correct" floor correct
            // for a size that is not under test.
            size: getComputedStyle(el).fontSize,
          });
          el.remove();
        }
        return out;
      }, cases);

      for (let i = 0; i < cases.length; i++) {
        const [px, weight, want] = cases[i];
        expect(parseFloat(got[i].size), `${px}px was not applied by the engine`).toBeCloseTo(px, 3);
        expect(got[i].floor, `the sweep gave ${px}px/${weight} the wrong floor`).toBe(want);
      }

      // The exported copy is kept in step with the in-page one. Callers outside
      // the page use it, so a drift between the two would be a second rule.
      for (const [px, weight, want] of cases) {
        expect(wcagFloor(px, weight), `wcagFloor drifted from the sweep at ${px}px/${weight}`).toBe(
          want,
        );
      }
    });
  });
}
