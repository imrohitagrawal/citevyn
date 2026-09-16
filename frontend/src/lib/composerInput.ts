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
 * How long a question is, counted the way the SERVER counts it.
 *
 * `String.prototype.length` counts UTF-16 code units. Pydantic's `max_length`
 * counts what Python's `len()` counts, which is code POINTS. For every character
 * outside the Basic Multilingual Plane — emoji, and a great deal of CJK
 * Extension B — those two numbers differ by a factor of two.
 *
 * THE DIRECTION OF THAT ERROR REVERSED INSIDE THIS ISSUE, which is why it is a
 * function now and not a `.length`:
 *
 *   - While the limit was a `maxLength` ATTRIBUTE, code units made the browser
 *     STRICTER than the server. Wrong, but safe: it could only stop something
 *     the server would also have stopped.
 *   - Once the limit moved into the submit handler, the same `.length` started
 *     REFUSING QUESTIONS THE SERVER WOULD ACCEPT. Measured: 2,500 emoji are
 *     `len()` 2,500 to the server and `.length` 5,000 here, so the composer
 *     refused a legal question — and told the user it was "5000 characters",
 *     a number the server would never have agreed with.
 *
 * That is a false refusal aimed squarely at non-Latin writers, which is the same
 * population the IME guard above exists for.
 *
 * `for...of` iterates a string by code point, so this counts the same units
 * Python does. Not `Array.from(q).length`: that allocates an array the size of
 * the question, and with the `maxLength` attribute gone the box can now hold an
 * arbitrarily large paste.
 */
export function questionLength(text: string): number {
  let n = 0;
  for (const _ of text) n++;
  return n;
}

/**
 * The minimal shape `isSubmitKey` needs. React's
 * `KeyboardEvent<HTMLInputElement>` is structurally assignable to it, and so is
 * a hand-built object in a test — which is the point of not naming React here.
 */
export interface SubmitKeyEvent {
  key: string;
  keyCode?: number;
  /**
   * React's synthetic event nests the real one here. OPTIONAL, because a raw
   * DOM `KeyboardEvent` has no such property at all.
   */
  nativeEvent?: { isComposing?: boolean };
  /**
   * ...and a raw DOM `KeyboardEvent` carries the flag HERE instead.
   *
   * BOTH SHAPES ARE READ, and that is a fix for a real hole rather than
   * defensive noise. With only `nativeEvent?.isComposing`, a raw DOM event is
   * still structurally assignable to this interface — every required property
   * matches and the missing one is optional — so a caller wired to a plain
   * `addEventListener` compiles clean, `e.nativeEvent` is `undefined`, the
   * composition check evaluates to `undefined`, and the IME guard is SKIPPED.
   * No error, no warning: just a guard that quietly is not there.
   *
   * Measured before fixing: `isSubmitKey({ key: "Enter", isComposing: true })`
   * returned `true`.
   *
   * That is the failure mode a shared helper is supposed to remove, so the
   * helper must not reintroduce it. Making the wrong call a type error was the
   * alternative and is worse here — React's synthetic event legitimately has
   * `nativeEvent`, so a type that rejected the raw shape would have to reject
   * one of the two real callers.
   */
  isComposing?: boolean;
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
/**
 * THE TENSION THIS LEAVES, recorded because a future reader WILL trip on it.
 *
 * When this returns false the caller does nothing: no submit, no nudge, no DOM
 * mutation at all. That is indistinguishable, from the outside, from the bare
 * `return` that #445 was filed against — and #445's whole finding was that an
 * unobservable no-op is a defect, because a screen-reader user presses a key and
 * is told nothing.
 *
 * It is correct HERE, and the difference is who the keystroke belonged to. An
 * Enter that confirms an IME candidate is not a submit the user made and had
 * refused; it is a keystroke the input method consumed, and the composer never
 * saw a submit at all. Announcing "nothing was sent" would be a false statement
 * about an action that did not happen, and it would fire on every candidate a
 * CJK user picks — several times per sentence.
 *
 * The empty and over-length refusals are the opposite case: there the user DID
 * submit and was refused, so both announce. If you ever find yourself adding a
 * nudge to this predicate's false branch, that is the distinction to check
 * first.
 */
export function isSubmitKey(e: SubmitKeyEvent): boolean {
  if (e.key !== "Enter") return false;
  // Either shape — see `SubmitKeyEvent.isComposing`. React nests it, the DOM
  // does not, and reading only one of the two silently disables this line for
  // the other caller.
  if (e.nativeEvent?.isComposing || e.isComposing) return false;
  if (e.keyCode === 229) return false;
  return true;
}
