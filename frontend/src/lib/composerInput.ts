/**
 * #446 — the shared INPUT rules for both question composers.
 *
 * CiteVyn has two places to type a question: the landing hero and the chat
 * composer. #445 put their empty-submit SIGNAL in one module for exactly one
 * reason — two implementations of the same idea drift apart, and the bug that
 * issue closed was the drift, not the signal. This module is the same move for
 * the two rules that decide whether a keystroke submits at all and how much
 * text the box will hold.
 *
 * No React import and no app import, deliberately: `isSubmitKey` is a pure
 * predicate over a structural shape and `MAX_QUESTION_LENGTH` is a number, so
 * both are testable without a DOM and neither can acquire a dependency on the
 * screens that use them.
 */

/**
 * The ceiling both composers put on a question, mirroring the server's
 * `AnswerRequest.message` (`max_length=4000` in
 * `backend/app/api/routes/messages.py`).
 *
 * Before this the browser accepted what the server rejects. The request went
 * out, came back 422, and — because `submitChat` clears the box BEFORE the
 * request — the user watched a long question disappear and got told the
 * service was "TEMPORARILY UNAVAILABLE", which was false twice over: the
 * rejection is permanent for that input, and nothing was unavailable.
 *
 * `maxLength` on the element is what makes this bite for a person typing or
 * pasting; the 422 branch in `handleApiError` is what makes it honest for
 * anything that reaches the route anyway. The two are not redundant — a
 * `maxLength` attribute is advisory to anything that is not a keyboard, and
 * React's controlled `value` can be set past it programmatically.
 *
 * The two numbers agreeing is NOT left to whoever edits one of them:
 * `backend/tests/test_ui_input_limits_match_the_api.py` reads this literal out
 * of this file and compares it against the Pydantic field's own metadata.
 */
export const MAX_QUESTION_LENGTH = 4000;

/**
 * The minimal shape `isSubmitKey` needs. React's
 * `KeyboardEvent<HTMLInputElement>` is structurally assignable to it, and so is
 * a hand-built object in a test — which is the point of not naming React here.
 */
export interface SubmitKeyEvent {
  key: string;
  keyCode?: number;
  nativeEvent?: { isComposing?: boolean };
}

/**
 * True when this keystroke should submit the composer.
 *
 * Enter, and NOT while an input method editor is mid-composition. Both
 * composers used a bare `e.key === "Enter"`, which is wrong for every language
 * that needs an IME: a Japanese, Chinese or Korean writer types phonetically,
 * the IME offers candidates, and **Enter picks a candidate**. With a bare
 * check, that first Enter submitted the half-composed phonetic text as the
 * question and cleared the box. The user could not even see what went wrong,
 * because what was sent was not what they were writing.
 *
 * TWO CHECKS, NOT ONE, and the second is not belt-and-braces:
 *
 *   - `nativeEvent.isComposing` is the standard signal (UI Events, "composing"
 *     flag). Chrome and Firefox set it on the `keydown` that confirms a
 *     candidate.
 *   - `keyCode === 229` is the legacy signal, and it is the ONLY one Safari
 *     gives: WebKit dispatches the confirming `keydown` with `isComposing`
 *     already false and the historical "IME in progress" keyCode of 229. A
 *     guard that checks only the standard property is therefore wrong on
 *     WebKit. How wide that is in practice is NOT measured here — this repo
 *     has no iOS device in the loop — so the claim stays at "WebKit reports
 *     the legacy code", which is what the second check is for.
 *
 * `keyCode` is deprecated for identifying WHICH key was pressed and is not used
 * for that here — 229 is not a key, it is a sentinel meaning "the IME handled
 * this". There is no modern replacement for it on the browser that needs it.
 */
export function isSubmitKey(e: SubmitKeyEvent): boolean {
  if (e.key !== "Enter") return false;
  if (e.nativeEvent?.isComposing) return false;
  if (e.keyCode === 229) return false;
  return true;
}
