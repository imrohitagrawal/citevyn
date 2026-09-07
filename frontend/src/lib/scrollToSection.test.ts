/**
 * scrollToSection (#358) — the retry is the whole point of this module.
 *
 * Four of the five header nav links now point into a lazily-loaded chunk, so
 * at click time the target element does not exist. The version this replaced
 * did `if (!el) return;` — a SILENT no-op that made the link look dead.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SCROLL_TARGET_FRAMES, scrollToSection } from "./scrollToSection";

/**
 * Drives the retry by hand. requestAnimationFrame callbacks are queued rather
 * than run, so a test decides exactly how many frames pass — no fake timers, no
 * wall clock, and a runaway retry loop shows up as a growing queue instead of
 * hanging the suite.
 */
function installFrameQueue() {
  const queue: FrameRequestCallback[] = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    queue.push(cb);
    return queue.length;
  });
  return {
    get pending() {
      return queue.length;
    },
    /** Runs every callback queued right now (callbacks may queue more). */
    tick(times = 1) {
      for (let i = 0; i < times; i += 1) {
        const due = queue.splice(0, queue.length);
        for (const cb of due) cb(0);
      }
    },
  };
}

let scrollTo: ReturnType<typeof vi.fn>;

beforeEach(() => {
  scrollTo = vi.fn();
  vi.stubGlobal("scrollTo", scrollTo);
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

function addSection(id: string) {
  const el = document.createElement("section");
  el.id = id;
  document.body.appendChild(el);
  return el;
}

describe("scrollToSection", () => {
  it("scrolls straight away when the section is already in the DOM", () => {
    const frames = installFrameQueue();
    addSection("pricing");

    scrollToSection("pricing");

    expect(scrollTo).toHaveBeenCalledTimes(1);
    expect(scrollTo).toHaveBeenCalledWith({ top: -72, behavior: "smooth" });
    // No retry was scheduled — the happy path costs nothing.
    expect(frames.pending).toBe(0);
  });

  it("waits for a section that only appears after the lazy chunk lands", () => {
    const frames = installFrameQueue();

    scrollToSection("faq");
    // The element genuinely is not there yet.
    expect(scrollTo).not.toHaveBeenCalled();
    expect(frames.pending).toBe(1);

    frames.tick(3);
    expect(scrollTo).not.toHaveBeenCalled();

    // ...the chunk resolves and React mounts the section.
    addSection("faq");
    frames.tick();

    expect(scrollTo).toHaveBeenCalledTimes(1);
  });

  it("gives up after a bounded number of frames rather than retrying forever", () => {
    const frames = installFrameQueue();

    scrollToSection("never-arrives");
    // One frame per attempt; after the budget the queue drains and stays drained.
    frames.tick(SCROLL_TARGET_FRAMES + 5);

    expect(frames.pending).toBe(0);
    expect(scrollTo).not.toHaveBeenCalled();

    // PARTNER for that "never called": the same helper, same queue, does scroll
    // when the element is present — so the assertion above is about the bound,
    // not about a broken harness.
    addSection("never-arrives");
    scrollToSection("never-arrives");
    expect(scrollTo).toHaveBeenCalledTimes(1);
  });

  it("still scrolls once a section arrives on the LAST frame of the budget", () => {
    const frames = installFrameQueue();

    scrollToSection("who");
    frames.tick(SCROLL_TARGET_FRAMES - 1);
    expect(scrollTo).not.toHaveBeenCalled();

    addSection("who");
    frames.tick();

    expect(scrollTo).toHaveBeenCalledTimes(1);
  });

  it("falls back to a timer where requestAnimationFrame does not exist", () => {
    vi.stubGlobal("requestAnimationFrame", undefined);
    vi.useFakeTimers();
    try {
      scrollToSection("how");
      expect(scrollTo).not.toHaveBeenCalled();

      addSection("how");
      vi.advanceTimersByTime(20);

      expect(scrollTo).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("offsets the scroll by the fixed header height", () => {
    installFrameQueue();
    const el = addSection("pricing");
    vi.spyOn(el, "getBoundingClientRect").mockReturnValue({ top: 500 } as DOMRect);
    Object.defineProperty(window, "pageYOffset", { value: 1000, configurable: true });

    scrollToSection("pricing");

    // 500 + 1000 - 72. Pinning the arithmetic, not just "it was called".
    expect(scrollTo).toHaveBeenCalledWith({ top: 1428, behavior: "smooth" });
  });
});
