/**
 * scrollToSection — smooth-scroll a landing section into view below the ~72px
 * fixed header, waiting for it if it is not in the DOM yet (#358).
 *
 * Lives here rather than inside useLandingState because two callers need it:
 * the header nav (via `goSection`) and LandingPage's on-load `#hash` handler.
 *
 * WHY IT RETRIES. Four of the five header nav links — "Who it's for", "How it
 * works", "Pricing", "FAQ" — point into the lazily-loaded landing strip, so at
 * click time the target genuinely does not exist: the caller reveals the strip,
 * React re-renders, and the chunk resolves a few frames later. The original
 * `if (!el) return;` made that a SILENT no-op, and the link looked dead.
 *
 * It gives up after a bounded number of frames rather than retrying forever, so
 * a bogus id (a typo, a removed section) still costs nothing. The bound is a
 * COUNT, not a wall clock, so a test running fake timers cannot turn it into an
 * unbounded loop. Where requestAnimationFrame is absent it falls back to a
 * timer; if neither ever runs the outcome is the old behaviour — no scroll.
 */

/** ~2 s at 60 fps. */
export const SCROLL_TARGET_FRAMES = 120;

/** Offset for the fixed header, in px. */
const HEADER_OFFSET = 72;

export function scrollToSection(id: string, framesLeft: number = SCROLL_TARGET_FRAMES): void {
  const el = document.getElementById(id);
  if (!el) {
    if (framesLeft <= 0) return;
    const again = () => scrollToSection(id, framesLeft - 1);
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(again);
    else setTimeout(again, 16);
    return;
  }
  const y = el.getBoundingClientRect().top + window.pageYOffset - HEADER_OFFSET;
  window.scrollTo({ top: y, behavior: "smooth" });
}
