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

/**
 * #446 review. The SECOND thing a composer refuses, sharing the first's
 * mechanism, its window and its live region.
 *
 * WHY THIS EXISTS AT ALL — it is a regression this PR introduced and then had
 * to undo. The first version put `maxLength={4000}` on both boxes. That stops
 * the 422, and it does so by SILENTLY DESTROYING TEXT: paste a 5,000-character
 * question and the browser clamps it to 4,000 with no event, no message and
 * nothing on screen changing. The user then sends a question cut mid-sentence
 * and gets a confident answer to the fragment. The previous behaviour was a
 * badly-worded red badge — wrong, but LOUD. Trading a loud wrong signal for a
 * quiet one is the exact defect #445 and #446 are both about, committed in the
 * PR arguing against it.
 *
 * So the attribute is gone and the limit is enforced where it can be ANNOUNCED,
 * at submit, before the box is cleared. The text stays on screen, which is what
 * makes "shorten it" a true instruction rather than one aimed at an empty box.
 *
 * A FUNCTION, not a constant, because the honest sentence needs the actual
 * length: "too long" leaves the reader guessing how much to cut.
 *
 * `actual` must be a CODE POINT count (`questionLength`), not `.length`.
 * The number in this sentence is the one the user is being asked to reduce,
 * so it has to be the number the server is measuring — otherwise a reader
 * pasting emoji is told to cut 5,000 characters from a question the server
 * considers 2,500 long.
 *
 * Same prosody rules as `EMPTY_SUBMIT_NUDGE`, for the same reason — two full
 * stops, no em dash. `ChatView` records that NVDA's `locale/en/symbols.dic`
 * gives `—` the level `most`, so a reader running punctuation above the default
 * hears the dash read out mid-sentence.
 */
export function overLengthNudge(actual: number, limit: number): string {
  return `Your question is ${actual} characters. The limit is ${limit}. Shorten it and send it again.`;
}


/**
 * Is a nudge showing? THE one reader of a nudge value, for both composers.
 *
 * #446 review. The two views diverged the moment the flag stopped being a
 * boolean: `Hero` rendered `{heroNudge ?? ""}` and `ChatView` rendered
 * `{chatNudge ? chatNudge : …}`. Null-coalescing tests for null; truthiness also
 * catches `""`. So an empty-string message would have shown in the hero and
 * fallen through in the chat — a divergence inside the ONE mechanism #445 built
 * specifically so the two composers could not disagree, introduced by the change
 * that generalised it.
 *
 * `composerEmptyParity` could not see it either, because it only drives the
 * empty-submit path, where the message is never empty. A guard is only as wide
 * as the inputs it drives.
 *
 * The fix is not "pick the right operator in both files" — that is the same
 * remembering-to-agree that keeps failing. It is to have ONE reader, so there is
 * no second operator to get wrong.
 */
export function hasNudge(message: string | null): boolean {
  return message !== null;
}
