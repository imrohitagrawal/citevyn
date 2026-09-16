/**
 * #445 — the ONE empty-submit signal, shared by both composers.
 *
 * CiteVyn has two places to type a question: the landing hero and the chat
 * composer. Before this file they disagreed on identical input. The hero
 * focused the box, flashed an amber border and rendered `.hero-nudge`;
 * `submitChat` had a bare `return` and did nothing observable at all, so a
 * screen-reader user pressed Enter on an empty box and was told nothing.
 *
 * The strings live HERE, in one module with no React and no app imports, so
 * that "the two composers say the same thing" is true by construction rather
 * than by two editors remembering. `frontend/src/test/composerEmptyParity.test.tsx`
 * is the guard that keeps it true through the DOM the reader actually gets.
 *
 * WHAT THIS DELIBERATELY DOES NOT SAY, because copy that is only true on one
 * of the two screens cannot be shared: the hero's nudge used to end "— or tap
 * one of the examples below", and the chat screen renders its suggestion chips
 * ONLY while the transcript is empty. The clause is therefore false on any
 * chat screen with messages in it, and a sentence that has to be qualified per
 * screen is not a shared sentence. What replaced it is true on both.
 */

/**
 * Announced verbatim by the persistent `role="status"` region on BOTH screens,
 * and rendered verbatim (behind a `⚠ ` glyph) by the visible nudge on both.
 *
 * No glyph in the string itself: the announced copy and the visible copy are
 * the same sentence, and `ChatView` already records what a literal glyph does
 * inside a live region (its avatar's "CV" got read out with the refusal).
 *
 * TWO FULL STOPS, NO EM DASH, and neither is a style choice:
 *
 *   - `ChatView.tsx` records beside `REFUSED_TEXT` that NVDA's
 *     `locale/en/symbols.dic` gives `—` the level `most`, so a reader running
 *     punctuation above the default hears the dash read out mid-sentence, and
 *     that "every other sr-only string on this screen already uses a full
 *     stop". This string goes into the same region, so it follows the same
 *     rule. A first draft of it did not, and a review caught the contradiction.
 *   - It says "Nothing was sent", not "the box is empty, so nothing was sent".
 *     The second clause stops being true the moment the reader starts typing,
 *     while the visible nudge stays on screen for the rest of its window — a
 *     sentence that expires before the element carrying it is not copy this
 *     change should ship, having been written to stop exactly that class of
 *     false statement to an AT user.
 */
export const EMPTY_SUBMIT_NUDGE = "Type a question first. Nothing was sent.";

/** Decoration for the VISIBLE nudge only. Never announced. */
export const EMPTY_SUBMIT_NUDGE_GLYPH = "⚠";

/**
 * How long the nudge stays up. One number for both composers: the hero's 3000
 * was the only value, and `frontend/tests/landing.spec.ts` asserts the hero
 * nudge is gone after it.
 */
export const EMPTY_SUBMIT_NUDGE_MS = 3000;
