/**
 * scrollToSection — smooth-scroll a landing section into view below the ~72px
 * fixed header, waiting for it if it is not in the DOM yet (#358).
 *
 * Lives here rather than inside useLandingState because two callers need it:
 * the header nav (via `goSection`) and LandingPage's on-load `#hash` handler.
 *
 * WHY IT WAITS. Four of the five header nav links — "Who it's for", "How it
 * works", "Pricing", "FAQ" — point into the lazily-loaded landing strip, so at
 * click time the target genuinely does not exist: the caller reveals the strip,
 * React re-renders, and the chunk resolves some time later. The original
 * `if (!el) return;` made that a SILENT no-op, and the link looked dead.
 *
 * WHY A MutationObserver AND NOT A FRAME COUNTER. The first version retried on
 * requestAnimationFrame for a fixed 120 frames. Review reproduced two ways that
 * gets the budget wrong in both directions at once:
 *
 *   - TOO SHORT where it matters. With the chunk delayed 3 s — ordinary on a
 *     congested mobile connection, or a cold edge — the section mounted and the
 *     page never moved: `top=7701, scrollY=0`. The reader had to click twice,
 *     with nothing to say the first click had done anything. That is the exact
 *     "the link looked dead" failure this function exists to remove, moved
 *     behind a latency threshold rather than fixed.
 *   - TOO LONG where it does not. rAF is throttled hard in a background tab
 *     (~32x, measured on this project before), so 120 frames there is minutes.
 *
 * A MutationObserver fires on the actual DOM insertion, so it does not depend
 * on frame timing in either direction, and it does not burn frames polling. The
 * deadline is then a real clock, which is what "give up after a while" meant
 * all along. Falls back to doing nothing where MutationObserver is absent —
 * that is the pre-#358 behaviour, and no browser with IntersectionObserver
 * lacks it.
 */

/** How long to keep waiting for a section that has not arrived yet. */
export const SCROLL_TARGET_TIMEOUT_MS = 10_000;

/** Offset for the fixed header, in px. */
const HEADER_OFFSET = 72;

/**
 * Only the most recent request may scroll. Two nav clicks in quick succession
 * ("Pricing", then "FAQ") both resolve when the single shared chunk lands, and
 * without this both would scroll — landing the reader on whichever finished
 * last rather than on what they last asked for.
 */
let latestRequest = 0;

function scrollTo(el: Element): void {
  const y = el.getBoundingClientRect().top + window.pageYOffset - HEADER_OFFSET;
  // A JS `behavior` option OVERRIDES the CSS cascade, so reset.css's
  // `@media (prefers-reduced-motion: reduce) { scroll-behavior: auto }` cannot
  // reach this call — the same trap ChatView.tsx already documents. Measured in
  // review under `reducedMotion: reduce`: clicking "Pricing" delivered a ~7,500 px
  // animated glide to a reader who had asked for no motion. The literal predates
  // #358, but this change made four of the five nav links actually work at phone
  // widths, so it is newly delivered on those.
  //
  // `matchMedia?.` because jsdom does not implement it.
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  window.scrollTo({ top: y, behavior: reduced ? "auto" : "smooth" });
}

export function scrollToSection(id: string, timeoutMs: number = SCROLL_TARGET_TIMEOUT_MS): void {
  const request = ++latestRequest;

  const present = document.getElementById(id);
  if (present) {
    scrollTo(present);
    return;
  }

  if (typeof MutationObserver === "undefined") return;

  let timer: ReturnType<typeof setTimeout> | undefined;
  const observer = new MutationObserver(() => {
    // Superseded by a later call — stop watching and let that one win.
    if (request !== latestRequest) {
      observer.disconnect();
      if (timer !== undefined) clearTimeout(timer);
      return;
    }
    const el = document.getElementById(id);
    if (!el) return;
    observer.disconnect();
    if (timer !== undefined) clearTimeout(timer);
    scrollTo(el);
  });

  observer.observe(document.body, { childList: true, subtree: true });
  // Bounded, so a bogus id (a typo, a removed section) does not leave an
  // observer watching the whole document for the life of the page.
  timer = setTimeout(() => observer.disconnect(), timeoutMs);
}
