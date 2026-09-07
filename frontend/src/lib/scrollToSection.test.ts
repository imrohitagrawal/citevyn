/**
 * scrollToSection (#358) — waiting for the target is the whole point.
 *
 * Four of the five header nav links now point into a lazily-loaded chunk, so at
 * click time the target element does not exist. The version this replaced did
 * `if (!el) return;` — a SILENT no-op that made the link look dead.
 *
 * The retry mechanism is a MutationObserver rather than a frame counter,
 * because review reproduced the frame counter being simultaneously too short
 * (a 3 s chunk delay mounted the section and never scrolled) and too long (rAF
 * throttling in a background tab stretched 120 frames to minutes). These tests
 * drive real DOM insertions, so they exercise the mechanism that ships.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SCROLL_TARGET_TIMEOUT_MS, scrollToSection } from "./scrollToSection";

let scrollTo: ReturnType<typeof vi.fn>;

beforeEach(() => {
  scrollTo = vi.fn();
  vi.stubGlobal("scrollTo", scrollTo);
  // Built HERE, not lazily inside addSection: if the first section of a test
  // also created the host, that insertion would mutate document.body's own
  // childList and fire even without `subtree`, so the very first wait in each
  // test would survive the mutation.
  document.body.innerHTML = '<div id="test-root"><main></main></div>';
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.body.innerHTML = "";
});

/**
 * Appends the section NESTED, the way the app does — `body > #root > … > main >
 * section#id` — not as a direct child of body.
 *
 * Load-bearing. With a flat `document.body.appendChild`, dropping `subtree:
 * true` from the MutationObserver options survived every test in this file,
 * because a direct child of body still shows up in body's own childList. In the
 * real DOM nothing would ever fire and all four deferred nav links would go
 * dead again. Found in review.
 */
function addSection(id: string) {
  const el = document.createElement("section");
  el.id = id;
  // Into the pre-built nest, never into document.body — see the note above and
  // the beforeEach, which builds the host BEFORE any test runs so that no
  // insertion is ever a direct child of body.
  document.querySelector("#test-root main")!.appendChild(el);
  return el;
}

/** MutationObserver callbacks are delivered as microtasks. */
const settle = () => new Promise((r) => setTimeout(r, 0));

describe("scrollToSection", () => {
  it("scrolls straight away when the section is already in the DOM", () => {
    addSection("pricing");

    scrollToSection("pricing");

    expect(scrollTo).toHaveBeenCalledTimes(1);
    expect(scrollTo).toHaveBeenCalledWith({ top: -72, behavior: "smooth" });
  });

  it("waits for a section that only appears after the lazy chunk lands", async () => {
    scrollToSection("faq");
    // The element genuinely is not there yet.
    expect(scrollTo).not.toHaveBeenCalled();

    await settle();
    expect(scrollTo).not.toHaveBeenCalled();

    // ...the chunk resolves and React mounts the section.
    addSection("faq");
    await settle();

    expect(scrollTo).toHaveBeenCalledTimes(1);
  });

  it("still scrolls when the chunk is SLOW — far past any frame budget", async () => {
    // The regression this mechanism exists for. The previous implementation
    // gave up after ~120 animation frames (~2 s), so a 3 s chunk mounted the
    // section and left the reader at the top of the page with a link that had
    // visibly done nothing. Driven on fake timers so the test does not
    // actually wait seconds.
    vi.useFakeTimers();
    try {
      scrollToSection("pricing");
      vi.advanceTimersByTime(5_000); // ~2.5x the old budget
      expect(scrollTo).not.toHaveBeenCalled();

      addSection("pricing");
      await vi.advanceTimersByTimeAsync(0);

      expect(scrollTo).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("gives up on a real clock, so a bogus id does not watch the document forever", async () => {
    vi.useFakeTimers();
    try {
      scrollToSection("never-arrives");
      vi.advanceTimersByTime(SCROLL_TARGET_TIMEOUT_MS + 1);

      // Past the deadline the observer is disconnected, so a late arrival is
      // ignored rather than yanking the page around minutes later.
      addSection("never-arrives");
      await vi.advanceTimersByTimeAsync(0);
      expect(scrollTo).not.toHaveBeenCalled();

      // PARTNER for that "never called": arriving JUST INSIDE the deadline
      // does scroll, so the assertion above is about the deadline and not
      // about a harness that can never observe anything.
      scrollToSection("arrives-in-time");
      vi.advanceTimersByTime(SCROLL_TARGET_TIMEOUT_MS - 1);
      addSection("arrives-in-time");
      await vi.advanceTimersByTimeAsync(0);
      expect(scrollTo).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("lets the MOST RECENT request win when two nav links are clicked in a row", async () => {
    // Both targets live in the same chunk, so they appear in the same commit.
    // Without a request token both pending waits would fire and the reader
    // would land on whichever resolved last rather than what they last asked
    // for.
    scrollToSection("pricing");
    scrollToSection("faq");

    const faq = addSection("faq");
    vi.spyOn(faq, "getBoundingClientRect").mockReturnValue({ top: 900 } as DOMRect);
    addSection("pricing");
    await settle();

    expect(scrollTo).toHaveBeenCalledTimes(1);
    expect(scrollTo).toHaveBeenCalledWith({ top: 828, behavior: "smooth" });
  });

  it("does nothing, rather than throwing, where MutationObserver is absent", async () => {
    vi.stubGlobal("MutationObserver", undefined);

    expect(() => scrollToSection("how")).not.toThrow();

    addSection("how");
    await settle();
    // Degrades to the pre-#358 behaviour: no scroll, no crash.
    expect(scrollTo).not.toHaveBeenCalled();
  });

  it("honours prefers-reduced-motion, which the CSS cascade cannot reach here", () => {
    // A JS `behavior` option OVERRIDES `scroll-behavior` from CSS, so
    // reset.css's reduced-motion rule does not apply to this call. Measured in
    // review before this check existed: a ~7,500 px animated glide delivered
    // under `reducedMotion: reduce`.
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce") }));
    addSection("pricing");

    scrollToSection("pricing");

    expect(scrollTo).toHaveBeenCalledWith({ top: -72, behavior: "auto" });
  });

  it("keeps the smooth scroll when reduced motion is NOT requested", () => {
    // PARTNER for the test above: proves the branch is reading the query and
    // not just hard-coding "auto".
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    addSection("pricing");

    scrollToSection("pricing");

    expect(scrollTo).toHaveBeenCalledWith({ top: -72, behavior: "smooth" });
  });

  it("offsets the scroll by the fixed header height", () => {
    const el = addSection("pricing");
    vi.spyOn(el, "getBoundingClientRect").mockReturnValue({ top: 500 } as DOMRect);
    Object.defineProperty(window, "pageYOffset", { value: 1000, configurable: true });

    scrollToSection("pricing");

    // 500 + 1000 - 72. Pinning the arithmetic, not just "it was called".
    expect(scrollTo).toHaveBeenCalledWith({ top: 1428, behavior: "smooth" });
  });
});
