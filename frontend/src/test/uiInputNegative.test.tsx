/**
 * #446 — the `docs/TEST_STRATEGY.md` §8b sweep.
 *
 * §8 has mandated ten negative tests since the MVP and the repo genuinely
 * follows it — but all ten are about the LLM answering layer. Not one is about
 * a form, a button, or an empty field. §8b names seven UI-input cells and this
 * file is the sweep of every input surface against them.
 *
 * TWO THINGS LIVE HERE, and the first is why the second can be trusted:
 *
 *   1. `SURFACES` — the registry of every input surface, and a STRUCTURAL GUARD
 *      that parses the components with the TypeScript compiler and fails if the
 *      app contains an `<input>` or `<textarea>` the registry does not list.
 *      A hand-kept list is exactly how #445's first fix missed the suggestion
 *      chips: it was complete on the day it was written and silently wrong a
 *      week later. This one cannot be silently wrong.
 *
 *   2. The cells themselves, driven over that registry.
 *
 * WHAT THIS FILE DOES NOT DUPLICATE: #445 already drives an empty CLICK through
 * both composers in `composerEmptyParity.test.tsx` and asserts focus, the
 * visible nudge, the announcement and that nothing was sent. The empty cell
 * here covers what that file does not — the KEYBOARD path, and whitespace-only,
 * neither of which had a test even though both behave correctly today.
 */
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { LandingPage } from "../components/LandingPage";
import { AuthModal, type AuthModalMode } from "../components/AuthModal";
import { useLandingState } from "../hooks/useLandingState";
import { __testOnly } from "../lib/authStore";
import {
  isLiveMode,
  createSession,
  askQuestion,
  login,
  register,
  getCurrentUser,
  apiFetch,
} from "../lib/api";
import AnswerActions from "../components/AnswerActions";
import { MAX_FEEDBACK_COMMENT, MAX_SOURCE_NOTE, MAX_SOURCE_URL } from "../lib/feedbackLimits";
import { requestMagicLink, updatePassword } from "../lib/authActions";
import { ApiClientError } from "../lib/types";
import { MAX_QUESTION_LENGTH, isSubmitKey, questionLength } from "../lib/composerInput";
import {
  EMPTY_SUBMIT_NUDGE,
  EMPTY_SUBMIT_NUDGE_GLYPH,
  hasNudge,
  overLengthNudge,
} from "../lib/composerNudge";

vi.mock("../lib/api", () => ({
  API_BASE_URL: "",
  isLiveMode: vi.fn(() => false),
  createSession: vi.fn(),
  askQuestion: vi.fn(),
  getCurrentUser: vi.fn(() => Promise.resolve(null)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
  apiFetch: vi.fn(),
}));

vi.mock("../lib/authActions", () => ({
  requestMagicLink: vi.fn(),
  updatePassword: vi.fn(),
}));

const SESSION = { request_id: "r", session_id: "s", expires_at: "2026-07-11T12:00:00Z" };

const realScrollIntoView = Element.prototype.scrollIntoView;

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  vi.mocked(isLiveMode).mockReturnValue(false);
  vi.mocked(createSession).mockReset().mockResolvedValue(SESSION);
  vi.mocked(askQuestion).mockReset();
  vi.mocked(login).mockReset();
  vi.mocked(register).mockReset();
  vi.mocked(requestMagicLink).mockReset();
  vi.mocked(updatePassword).mockReset();
  __testOnly.setState(__testOnly.initialState);
  __testOnly.resetBootstrapped();
});

afterEach(() => {
  cleanup();
  document.querySelectorAll("[data-test-trigger]").forEach((el) => el.remove());
  Element.prototype.scrollIntoView = realScrollIntoView;
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 1. The surface registry, and the guard that keeps it honest
// ---------------------------------------------------------------------------

/**
 * Every input surface under `src/`, as `file#ordinal (type=…)`.
 *
 * The ordinal is the element's position among the `<input>`/`<textarea>`
 * elements in its own file, in source order. It is deliberately positional
 * rather than keyed on an attribute: two of AuthModal's three fields are
 * `type="password"` and the third's `autoComplete` is a ternary, so there is no
 * static attribute that distinguishes all five. A positional key also fails
 * LOUDLY when an input is inserted in the middle, which is the behaviour wanted
 * — an inserted field should force someone to look at this list.
 *
 * WHAT THIS REGISTRY DOES NOT SEE, said plainly rather than left for someone to
 * discover: `contentEditable`, `<select>`, and a third-party component that
 * renders its own field. Those would need their own rows.
 *
 * A JSX FACTORY CALL used to be on that list and is now covered — the traversal
 * reads `createElement("input", …)` and the `jsx`/`jsxs` runtime form, and walks
 * `.ts` files as well as `.tsx` because a `.ts` file cannot hold JSX and that is
 * the only way a field could appear in one. It was a surviving mutant, and
 * "only one spelling is supported" is how a guard grows a blind spot.
 *
 * Still not seen, and worth naming because it is the nearest remaining gap: a
 * factory call whose tag is a VARIABLE (`createElement(tag, …)`) rather than a
 * string literal. Nothing in this app does that, and resolving it would need the
 * type checker rather than the syntax tree.
 */
const SURFACES = [
  // ADR-0005 §6 feedback forms. Cells: "§8b cells — AnswerActions" below.
  "components/AnswerActions.tsx#0 (type=radio)",
  "components/AnswerActions.tsx#1 (type=<none>)",
  "components/AnswerActions.tsx#2 (type=url)",
  "components/AnswerActions.tsx#3 (type=<none>)",
  "components/AuthModal.tsx#0 (type=email)",
  "components/AuthModal.tsx#1 (type=password)",
  "components/AuthModal.tsx#2 (type=password)",
  "components/ChatView.tsx#0 (type=text)",
  "components/Hero.tsx#0 (type=<none>)",
] as const;

const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");

/**
 * THE one traversal. Extracted so the "detector works" test below can drive the
 * SHIPPED function over a synthetic source instead of a copy of it.
 *
 * The #445 round caught exactly this pattern — "an earlier draft used a private
 * copy, so its stated RED-WHEN was false" — and the first version of this file
 * reintroduced it: the detector re-implemented the visitor inline, so breaking
 * `collectInputSurfaces` (wrong `ScriptKind`, wrong directory, a compiler API
 * change) left the test that exists to notice that entirely green.
 */
function inputsIn(label: string, text: string): string[] {
  const source = ts.createSourceFile(
    label,
    text,
    ts.ScriptTarget.Latest,
    true,
    label.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const out: string[] = [];
  let ordinal = 0;
  const visit = (node: ts.Node): void => {
    const opening =
      ts.isJsxSelfClosingElement(node) ? node : ts.isJsxOpeningElement(node) ? node : null;
    if (opening) {
      const tag = opening.tagName.getText(source);
      if (tag === "input" || tag === "textarea") {
        let type = "<none>";
        for (const attr of opening.attributes.properties) {
          if (!ts.isJsxAttribute(attr) || attr.name.getText(source) !== "type") continue;
          const init = attr.initializer;
          type =
            init && ts.isStringLiteral(init)
              ? init.text
              : init && ts.isJsxExpression(init)
                ? "<dynamic>"
                : "<none>";
        }
        out.push(`${label}#${ordinal++} (type=${type})`);
      }
    }
    // A FIELD BUILT BY A FACTORY CALL, not written as an element:
    // `createElement("input", …)`, or the `jsx`/`jsxs` runtime form. Found as a
    // surviving mutant by the review's verifier — JSX-only traversal misses it,
    // and it is the one way to add a real input to this app that the registry
    // could not see. Detected here rather than written off, because the compiler
    // makes it about ten lines and "we only support one spelling" is exactly how
    // a guard develops a blind spot.
    //
    // A `.ts` file cannot contain JSX at all, so this is the ONLY way an input
    // can appear there — which is why the walk now includes `.ts`.
    if (ts.isCallExpression(node)) {
      const callee = node.expression.getText(source);
      const isFactory =
        callee === "createElement" ||
        callee.endsWith(".createElement") ||
        callee === "jsx" ||
        callee === "jsxs";
      const first = node.arguments[0];
      if (isFactory && first && ts.isStringLiteral(first)) {
        if (first.text === "input" || first.text === "textarea") {
          out.push(`${label}#${ordinal++} (type=<factory>)`);
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return out;
}

/**
 * Walk every non-test `.tsx` under `src/` with the TypeScript compiler and
 * return one entry per `<input>` / `<textarea>` JSX element.
 *
 * ALL of `src/`, not just `src/components`: the first draft scanned only the
 * components directory while its own comment claimed "every input surface in the
 * app", so an input added under `src/lib`, `src/hooks` or a new directory would
 * have been invisible to the guard whose entire job is to be impossible to slip
 * past. That is the same shape as the hand-kept list this replaces.
 *
 * The compiler, not a regex: `<input` appears inside this repo's comments and
 * inside string literals, and a regex would both over- and under-report.
 * `ts.forEachChild` sees only real JSX elements.
 */
function collectInputSurfaces(): string[] {
  const found: string[] = [];
  const walk = (dir: string, prefix: string): string[] => {
    const out: string[] = [];
    for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) =>
      a.name.localeCompare(b.name),
    )) {
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        out.push(...walk(join(dir, entry.name), rel));
      } else if (
        (entry.name.endsWith(".tsx") || entry.name.endsWith(".ts")) &&
        !entry.name.includes(".test.") &&
        !entry.name.endsWith(".d.ts")
      ) {
        // `.ts` TOO, for the factory-call case above: a `.ts` file cannot hold
        // JSX, so a field there can only be built by `createElement`, and
        // scanning only `.tsx` left that route unwatched.
        out.push(rel);
      }
    }
    return out;
  };
  const componentFiles = walk(SRC_DIR, "");
  expect(
    componentFiles.length,
    "no source files were read — the traversal is looking at the wrong directory",
  ).toBeGreaterThan(0);
  // The directory that actually holds the known surfaces must be in scope. A
  // traversal that silently stopped at the top level would still satisfy the
  // count check above.
  expect(
    componentFiles.some((f) => f.startsWith("components/")),
    "the traversal never descended into src/components",
  ).toBe(true);

  for (const file of componentFiles) {
    found.push(...inputsIn(file, readFileSync(join(SRC_DIR, file), "utf8")));
  }
  return found.sort();
}

describe("§8b registry: the sweep cannot silently miss an input surface", () => {
  it("lists EXACTLY the input elements the components render", () => {
    const actual = collectInputSurfaces();
    // The partner that keeps this from being a check that counts nothing: if
    // the traversal ever found zero (wrong directory, wrong ScriptKind, a
    // compiler API change), `toEqual([])` against an empty registry would pass
    // happily. It must find the surfaces that demonstrably exist.
    expect(actual.length, "the traversal found no inputs at all — it is broken, not the app").toBeGreaterThan(0);
    expect(actual, "an input surface is missing from, or absent in, SURFACES").toEqual([...SURFACES]);
  });

  it("the traversal can actually SEE a new surface (the detector works)", () => {
    // Drives the SHIPPED `inputsIn` over a synthetic source — not a copy of it.
    // An earlier version re-implemented the visitor inline, so any breakage in
    // the real function left this green: a detector that detects a different
    // detector. Break `inputsIn` now and BOTH this and the registry test redden.
    const found = inputsIn(
      "Synthetic.tsx",
      `export const X = () => (<form>{/* <input type="fake" /> in a comment */}
         <label>a<input type="search" /></label>
         <textarea defaultValue="" />
         <input type={dynamic} />
       </form>);`,
    );
    // The commented-out `<input>` is NOT among them — which is the whole reason
    // this uses the compiler and not a regex. A dynamic `type` is reported as
    // such rather than silently dropped.
    expect(found).toEqual([
      "Synthetic.tsx#0 (type=search)",
      "Synthetic.tsx#1 (type=<none>)",
      "Synthetic.tsx#2 (type=<dynamic>)",
    ]);
    // And it finds nothing in a source that genuinely has no inputs, so a
    // traversal that returned a constant would fail here.
    expect(inputsIn("Empty.tsx", "export const Y = () => <div>no fields</div>;")).toEqual([]);

    // THE FACTORY ROUTE, in a `.ts` file where JSX is impossible. This was a
    // surviving mutant: the app could grow a real input the registry could not
    // see. All three spellings, and a non-field factory call that must NOT be
    // picked up — otherwise this would pass on a traversal that flags every call.
    expect(
      inputsIn(
        "Sneaky.ts",
        `import { createElement } from "react";
         export const A = () => createElement("input", { type: "text" });
         export const B = () => React.createElement("textarea", null);
         export const C = () => jsx("input", {});
         export const D = () => createElement("div", null);
         export const E = () => notAFactory("input", {});`,
      ),
    ).toEqual([
      "Sneaky.ts#0 (type=<factory>)",
      "Sneaky.ts#1 (type=<factory>)",
      "Sneaky.ts#2 (type=<factory>)",
    ]);
  });
});

// ---------------------------------------------------------------------------
// 2. The composers: hero and chat, the same cells over both
// ---------------------------------------------------------------------------

interface Composer {
  label: string;
  open: (root: HTMLElement) => void;
  input: string;
  /** The button that submits WITHOUT a keystroke — the paste-only cell needs it. */
  submitButton: string;
  /** This composer's own subtree, so a cell cannot read the OTHER one's live
      region and pass on it. Same selectors `composerEmptyParity` scopes by. */
  scope: string;
}

const COMPOSERS: Composer[] = [
  {
    label: "hero",
    open: () => {},
    input: "#hero-input",
    submitButton: ".ask-button",
    scope: "section.hero",
  },
  {
    label: "chat",
    open: (root) => {
      const cta = root.querySelector(".cta-button") as HTMLElement | null;
      expect(cta, "the header CTA that opens the chat screen was not found").not.toBeNull();
      act(() => {
        fireEvent.click(cta!);
      });
    },
    input: ".chat-input",
    submitButton: ".send-button",
    scope: ".composer",
  },
];

const userMessages = (root: HTMLElement) => root.querySelectorAll(".message.user-msg").length;

function renderComposer(c: Composer) {
  const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
  c.open(container);
  const input = container.querySelector(c.input) as HTMLInputElement | null;
  expect(input, `${c.label}: ${c.input} is not on screen`).not.toBeNull();
  return { container, input: input! };
}

const flush = async () => {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 400));
  });
};

/**
 * CELLS NOT SWEPT FOR THE HERO, named because §8b's rule is "where a cell is
 * meaningless for a surface, say so; do not leave it unmentioned":
 *
 *   - RAPID DOUBLE-SUBMIT, SUBMIT-WHILE-IN-FLIGHT and SUBMIT-WHILE-DISABLED are
 *     swept for the chat composer only (section 4). The hero cannot be in any of
 *     those states: submitting it NAVIGATES to the chat screen, so the hero
 *     unmounts before a second submit is possible, and its Ask button has no
 *     disabled state at all (deliberately — #445 records that giving it one
 *     would restore a silent no-op for screen-reader users). The in-flight
 *     behaviour a hero submit leads to is `enterChat`'s parking, which
 *     `useLandingState.test.tsx` already covers.
 */
for (const c of COMPOSERS) {
  describe(`§8b cells — ${c.label} composer`, () => {
    // CELL: empty submit, KEYBOARD path. composerEmptyParity drives the click
    // path on both composers; nothing drove Enter on an empty hero box.
    it("empty submit: Enter on an empty box sends nothing and warns", async () => {
      const { container, input } = renderComposer(c);
      act(() => {
        fireEvent.keyDown(input, { key: "Enter" });
      });
      await flush();
      expect(userMessages(container), `${c.label}: an EMPTY question was sent`).toBe(0);
      expect(
        container.querySelectorAll(".hero-nudge, .composer-nudge").length,
        `${c.label}: Enter on an empty box said nothing`,
      ).toBe(1);
    });

    // CELL: whitespace-only. Behaves as empty because both paths `.trim()`.
    it("whitespace-only: spaces and tabs are treated as empty, not sent", async () => {
      const { container, input } = renderComposer(c);
      // Spaces and tabs only, no newline: an `<input type="text">` strips CR/LF
      // during value sanitisation, so a `\n` here would assert against a string
      // the element can never hold and the test would be measuring jsdom.
      const WHITESPACE = "   \t   ";
      act(() => {
        fireEvent.change(input, { target: { value: WHITESPACE } });
        fireEvent.keyDown(input, { key: "Enter" });
      });
      await flush();
      expect(userMessages(container), `${c.label}: a WHITESPACE-ONLY question was sent`).toBe(0);
      expect(
        container.querySelectorAll(".hero-nudge, .composer-nudge").length,
        `${c.label}: whitespace-only submitted silently`,
      ).toBe(1);
      // The text the user typed is still there — nothing was eaten.
      expect(input.value, `${c.label}: a refused whitespace submit cleared the box`).toBe(WHITESPACE);
    });

    // CELL: over-length. BEHAVIOURAL — refused, announced, and the text KEPT.
    //
    // THIS CELL IS A REGRESSION THIS PR CAUSED AND THEN UNDID, which is why it
    // asserts what it does. The first version put `maxLength` on the box and
    // asserted the ATTRIBUTE. That stops the 422 by silently destroying text: a
    // pasted 5,000-character question is clamped to 4,000 with no event and
    // nothing on screen changing, and the fragment then gets a confident answer.
    // What it replaced was a badly-worded red badge — wrong, but LOUD. Trading a
    // loud wrong signal for a quiet one is the defect this issue is about.
    //
    // So: no attribute, a refusal that is SAID, and the text left where the user
    // can act on the instruction they were given.
    it("over-length: refused with a true message, and the text is KEPT", async () => {
      const { container, input } = renderComposer(c);
      const tooLong = "x".repeat(MAX_QUESTION_LENGTH + 1);
      act(() => {
        fireEvent.change(input, { target: { value: tooLong } });
        fireEvent.keyDown(input, { key: "Enter" });
      });
      await flush();

      expect(userMessages(container), `${c.label}: an OVER-LENGTH question was sent`).toBe(0);
      // The box was NOT silently clamped on the way in.
      expect(input.value.length, `${c.label}: the browser truncated the text in silence`).toBe(
        MAX_QUESTION_LENGTH + 1,
      );
      // And the refusal did not eat it either — "shorten it" has to be an
      // instruction the user can actually follow.
      expect(input.value, `${c.label}: a refused question was cleared`).toBe(tooLong);

      const nudges = container.querySelectorAll(".hero-nudge, .composer-nudge");
      expect(nudges.length, `${c.label}: over-length was refused SILENTLY`).toBe(1);
      const expected = overLengthNudge(MAX_QUESTION_LENGTH + 1, MAX_QUESTION_LENGTH);
      expect(nudges[0].textContent).toBe(`${EMPTY_SUBMIT_NUDGE_GLYPH} ${expected}`);
      // Announced, not just drawn. The region is the one WP-1 built.
      const region = container.querySelector(`${c.scope} [role="status"]`);
      expect(region, `${c.label}: no live region`).not.toBeNull();
      expect(region!.textContent, `${c.label}: the refusal was never announced`).toBe(expected);
      // The message states the real numbers, so the reader knows how much to cut.
      expect(expected).toContain(String(MAX_QUESTION_LENGTH + 1));
      expect(expected).toContain(String(MAX_QUESTION_LENGTH));
      // It is NOT the empty-submit sentence — one mechanism, two distinct truths.
      expect(region!.textContent).not.toBe(EMPTY_SUBMIT_NUDGE);
    });

    it(`over-length: exactly ${MAX_QUESTION_LENGTH} characters still sends`, async () => {
      // The partner. Without it the cell above passes on a composer that refuses
      // EVERYTHING, and "nothing was sent" would read as success.
      const { container, input } = renderComposer(c);
      act(() => {
        fireEvent.change(input, { target: { value: "y".repeat(MAX_QUESTION_LENGTH) } });
        fireEvent.keyDown(input, { key: "Enter" });
      });
      await flush();
      expect(
        userMessages(container),
        `${c.label}: a question AT the limit was refused — the boundary is off by one`,
      ).toBe(1);
      expect(
        container.querySelectorAll(".hero-nudge, .composer-nudge").length,
        `${c.label}: a legal question raised the over-length warning`,
      ).toBe(0);
    });

    // CELL: over-length, counted the way the SERVER counts. An emoji is ONE
    // character to Pydantic's `len()` and TWO to `String.prototype.length`.
    //
    // The direction of this error reversed inside this issue. As a `maxLength`
    // attribute, code units only made the browser stricter — wrong but safe.
    // In the submit handler the same `.length` REFUSES QUESTIONS THE SERVER
    // WOULD ACCEPT, and tells the user a number the server would never agree
    // with. It aims that false refusal at exactly the writers the IME guard
    // above exists for.
    it("over-length: 2500 emoji are 2500 characters, not 5000, and still send", async () => {
      const { container, input } = renderComposer(c);
      const emoji = "\u{1F642}".repeat(2500);
      // The premise, asserted rather than assumed: this string really does
      // straddle the limit depending on which unit you count.
      expect(emoji.length, "the test string does not exercise the difference").toBe(5000);
      expect(questionLength(emoji)).toBe(2500);
      expect(emoji.length).toBeGreaterThan(MAX_QUESTION_LENGTH);
      expect(questionLength(emoji)).toBeLessThanOrEqual(MAX_QUESTION_LENGTH);

      act(() => {
        fireEvent.change(input, { target: { value: emoji } });
        fireEvent.keyDown(input, { key: "Enter" });
      });
      await flush();

      expect(
        userMessages(container),
        `${c.label}: a legal emoji question was refused — the client is counting UTF-16 code units`,
      ).toBe(1);
      expect(
        container.querySelectorAll(".hero-nudge, .composer-nudge").length,
        `${c.label}: a legal question raised the over-length warning`,
      ).toBe(0);
    });

    it("over-length: an emoji question that IS too long reports the code-point count", async () => {
      // The partner. Without it the cell above passes on a composer that
      // stopped enforcing the limit for astral text altogether.
      const { container, input } = renderComposer(c);
      const emoji = "\u{1F642}".repeat(MAX_QUESTION_LENGTH + 1);
      act(() => {
        fireEvent.change(input, { target: { value: emoji } });
        fireEvent.keyDown(input, { key: "Enter" });
      });
      await flush();
      expect(userMessages(container), `${c.label}: an over-length emoji question was sent`).toBe(0);
      const region = container.querySelector(`${c.scope} [role="status"]`);
      // 4001, the server's count — NOT 8002, the UTF-16 one.
      expect(region!.textContent).toBe(
        overLengthNudge(MAX_QUESTION_LENGTH + 1, MAX_QUESTION_LENGTH),
      );
      expect(region!.textContent, "the user is told a number the server would not agree with").not.toContain(
        String((MAX_QUESTION_LENGTH + 1) * 2),
      );
    });

    // The attribute must NOT come back: it is what caused the silent clamp.
    it("over-length: the box does NOT carry a maxLength attribute", () => {
      const { input } = renderComposer(c);
      expect(
        input.getAttribute("maxlength"),
        `${c.label}: maxLength is back — the browser will truncate a paste in silence again`,
      ).toBeNull();
    });

    // CELL: paste-only. NO KEYDOWN ANYWHERE IN THIS TEST, and that is the entire
    // point of the cell.
    //
    // The first version fired `paste`, then `change`, then `keyDown` — and was
    // therefore a second copy of "Enter submits a typed question" wearing a
    // paste label. §8b's own words for this cell are "handlers that hang off
    // `keydown` miss this", and a test that fires a keydown cannot detect such a
    // handler: deleting its `fireEvent.paste` line changed nothing.
    //
    // The real case is text that arrives with no keystroke for its content and
    // is submitted by the BUTTON — reachable by a mouse-only user, and the path
    // a keydown-anchored implementation breaks.
    it("paste-only: pasted text submits via the button, with no keystroke at all", async () => {
      const { container, input } = renderComposer(c);
      act(() => {
        fireEvent.paste(input, { clipboardData: { getData: () => "  pasted question  " } });
        // React controlled inputs read `value` off the change event, not the
        // paste event; a real browser fires both, so the test does too.
        fireEvent.change(input, { target: { value: "  pasted question  " } });
      });
      const button = container.querySelector(c.submitButton) as HTMLElement | null;
      expect(button, `${c.label}: ${c.submitButton} was not found`).not.toBeNull();
      act(() => {
        fireEvent.click(button!);
      });
      await flush();
      expect(userMessages(container), `${c.label}: a pasted question was not sent`).toBe(1);
      const sent = container.querySelector(".message.user-msg")!.textContent ?? "";
      expect(sent, `${c.label}: the pasted question was not sent`).toContain("pasted question");
      // Trimmed like every other path — the surrounding spaces came from the
      // clipboard and must not reach the corpus as part of the question.
      expect(sent.startsWith(" "), `${c.label}: the pasted question kept its leading space`).toBe(
        false,
      );
    });

    // CELL: IME composition. THE BUG THIS CLOSES: a bare `e.key === "Enter"`
    // submits when a CJK writer presses Enter to pick an IME candidate.
    it("IME: Enter mid-composition does not submit (standard isComposing)", async () => {
      const { container, input } = renderComposer(c);
      act(() => {
        fireEvent.change(input, { target: { value: "にほんご" } });
        fireEvent.keyDown(input, { key: "Enter", isComposing: true });
      });
      await flush();
      expect(
        userMessages(container),
        `${c.label}: a HALF-COMPOSED question was submitted when the IME confirmed a candidate`,
      ).toBe(0);
      expect(input.value, `${c.label}: the composition text was cleared`).toBe("にほんご");
    });

    it("IME: Enter mid-composition does not submit (Safari's keyCode 229)", async () => {
      const { container, input } = renderComposer(c);
      act(() => {
        fireEvent.change(input, { target: { value: "にほんご" } });
        // Safari reports isComposing FALSE on the confirming keydown and only
        // the legacy keyCode. A guard checking isComposing alone is broken on
        // every iPhone, which is why there are two checks and two tests.
        fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
      });
      await flush();
      expect(
        userMessages(container),
        `${c.label}: Safari's IME confirmation submitted a half-composed question`,
      ).toBe(0);
    });

    it("IME: the composition FINISHES and the real Enter submits", async () => {
      // The partner. Without it both tests above pass on a composer that never
      // submits anything at all — absence-as-success, the repo's own scar.
      const { container, input } = renderComposer(c);
      act(() => {
        fireEvent.change(input, { target: { value: "日本語" } });
        fireEvent.keyDown(input, { key: "Enter", isComposing: true });
      });
      await flush();
      expect(userMessages(container), "the composition submitted early").toBe(0);
      act(() => {
        fireEvent.keyDown(input, { key: "Enter" });
      });
      await flush();
      expect(
        userMessages(container),
        `${c.label}: the CONFIRMED question never submitted — the guard blocks everything`,
      ).toBe(1);
    });
  });
}

// ---------------------------------------------------------------------------
// 3. The composers' shared predicate, unit-level
// ---------------------------------------------------------------------------

describe("§8b: the two composers READ a nudge identically", () => {
  // THE DIVERGENCE THIS CATCHES, which shipped and was caught in review: the
  // hero rendered `{heroNudge ?? ""}` and the chat `{chatNudge ? chatNudge : …}`.
  // `??` tests for null; truthiness also catches `""`. An empty-string message
  // therefore showed in one composer and not the other — a disagreement inside
  // the ONE mechanism #445 built so they could not disagree.
  //
  // `composerEmptyParity` could not see it: it drives only the empty-submit
  // path, where the message is never empty. This drives the value SHAPE instead
  // of the user path, which is the axis that broke.
  it("hasNudge is the single reader, and it keys on null — not on truthiness", () => {
    expect(hasNudge(null)).toBe(false);
    // The case the two operators disagreed about. If this ever reads `false`,
    // one composer will show a nudge the other hides.
    expect(hasNudge("")).toBe(true);
    expect(hasNudge(EMPTY_SUBMIT_NUDGE)).toBe(true);
    expect(hasNudge(overLengthNudge(5000, 4000))).toBe(true);
  });

  it("NEITHER view reads the nudge value with its own operator", () => {
    // A structural guard, because the failure was two files each deciding for
    // themselves. It is not a substring search for a banned character: it
    // asserts every read goes through the shared predicate, and it names the
    // exact expressions that regressed.
    const read = (f: string) =>
      readFileSync(join(SRC_DIR, "components", f), "utf8")
        .split("\n")
        .filter((l) => !l.trimStart().startsWith("*") && !l.trimStart().startsWith("//"))
        .join("\n");
    const hero = read("Hero.tsx");
    const chat = read("ChatView.tsx");

    expect(hero).toContain("hasNudge(heroNudge)");
    expect(chat).toContain("hasNudge(chatNudge)");
    // The two spellings that diverged, banned by name.
    expect(hero, "the hero reads the nudge with ?? again").not.toContain("heroNudge ??");
    expect(chat, "the chat reads the nudge with ?? again").not.toContain("chatNudge ??");
    expect(hero, "the hero tests the nudge for truthiness again").not.toMatch(/\{heroNudge\s*&&/);
    expect(chat, "the chat tests the nudge for truthiness again").not.toMatch(/\{chatNudge\s*&&/);
    // The partner: these negatives are over text that really does mention the
    // flags, so they cannot pass on an empty read or a renamed prop.
    expect(hero).toContain("heroNudge");
    expect(chat).toContain("chatNudge");
  });
});

describe("§8b: isSubmitKey is the one rule both composers use", () => {
  it("submits on a plain Enter and nothing else", () => {
    expect(isSubmitKey({ key: "Enter" })).toBe(true);
    expect(isSubmitKey({ key: "a" })).toBe(false);
    expect(isSubmitKey({ key: "Escape" })).toBe(false);
    expect(isSubmitKey({ key: " " })).toBe(false);
  });

  it("reads isComposing in BOTH shapes — React's and the raw DOM's", () => {
    // A raw DOM `KeyboardEvent` puts `isComposing` at the top level and has no
    // `nativeEvent`. Because that property was optional, such an event was
    // structurally assignable with no type error while the guard silently did
    // not run: this returned `true` before the fix.
    expect(isSubmitKey({ key: "Enter", isComposing: true })).toBe(false);
    // React's shape still works.
    expect(isSubmitKey({ key: "Enter", nativeEvent: { isComposing: true } })).toBe(false);
    // Neither shape composing: submits. Without this the two above pass on a
    // predicate that returns false for everything.
    expect(isSubmitKey({ key: "Enter", isComposing: false, nativeEvent: { isComposing: false } })).toBe(
      true,
    );
  });

  it("refuses both IME signals, independently", () => {
    expect(isSubmitKey({ key: "Enter", nativeEvent: { isComposing: true } })).toBe(false);
    expect(isSubmitKey({ key: "Enter", keyCode: 229 })).toBe(false);
    // And neither signal is doing the other's job: with each absent, the other
    // still blocks on its own. A single `||` collapsed into one check would
    // pass a test that only ever sent both together.
    expect(isSubmitKey({ key: "Enter", keyCode: 13, nativeEvent: { isComposing: true } })).toBe(false);
    expect(isSubmitKey({ key: "Enter", keyCode: 229, nativeEvent: { isComposing: false } })).toBe(false);
    expect(isSubmitKey({ key: "Enter", keyCode: 13, nativeEvent: { isComposing: false } })).toBe(true);
  });

  it("pins the limit to the server's literal", () => {
    expect(MAX_QUESTION_LENGTH).toBe(4000);
  });
});

// ---------------------------------------------------------------------------
// 4. Rapid double-submit and submit-while-in-flight, on the chat composer
// ---------------------------------------------------------------------------

describe("§8b cells — chat composer under load", () => {
  function renderInFlight() {
    vi.mocked(isLiveMode).mockReturnValue(true);
    vi.mocked(askQuestion).mockImplementation(() => new Promise(() => {}));
    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
    const cta = container.querySelector(".cta-button") as HTMLElement;
    act(() => {
      fireEvent.click(cta);
    });
    return container;
  }

  const type = (root: HTMLElement, value: string) => {
    const input = root.querySelector(".chat-input") as HTMLInputElement;
    act(() => {
      fireEvent.change(input, { target: { value } });
    });
    return input;
  };

  // CELL: rapid double-submit — the same Enter twice with no gap.
  it("rapid double-submit: two Enters on one question send ONE request", async () => {
    const container = renderInFlight();
    const input = type(container, "What is Claude Code?");
    act(() => {
      fireEvent.keyDown(input, { key: "Enter" });
      fireEvent.keyDown(input, { key: "Enter" });
    });
    await flush();
    expect(
      vi.mocked(askQuestion).mock.calls.length,
      "a double Enter sent the question twice",
    ).toBe(1);
    expect(userMessages(container), "a double Enter produced two user bubbles").toBe(1);
  });

  // CELL: submit-while-in-flight — a DIFFERENT question while one is pending.
  it("submit-while-in-flight: a second question is refused, announced, and KEPT", async () => {
    const container = renderInFlight();
    const input = type(container, "First question");
    act(() => {
      fireEvent.keyDown(input, { key: "Enter" });
    });
    await flush();
    expect(vi.mocked(askQuestion).mock.calls.length, "the first question never went out").toBe(1);

    const second = type(container, "Second question");
    act(() => {
      fireEvent.keyDown(second, { key: "Enter" });
    });
    await flush();
    expect(
      vi.mocked(askQuestion).mock.calls.length,
      "a second question went out while the first was in flight",
    ).toBe(1);
    // Refused, not eaten: the typed text survives, and the refusal is SAID.
    expect(second.value, "the refused question was cleared out of the box").toBe("Second question");
    const said = container.querySelector('.composer [role="status"]')?.textContent ?? "";
    expect(said, "the refusal was not announced to a screen reader").toContain("Not sent.");
  });

  // CELL: submit-while-disabled — the send button advertises unavailable.
  it("submit-while-disabled: the send button is aria-disabled while in flight", async () => {
    const container = renderInFlight();
    const input = type(container, "First question");
    act(() => {
      fireEvent.keyDown(input, { key: "Enter" });
    });
    await flush();
    const send = container.querySelector(".send-button") as HTMLElement;
    expect(send, "no send button").not.toBeNull();
    expect(send).toHaveAttribute("aria-disabled", "true");
    // aria-disabled, NOT the native `disabled` attribute, and that is deliberate
    // (#62): a natively disabled button drops focus to <body> mid-request. The
    // refusal is enforced in the hook, which the test above measures.
    expect(send).not.toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// 5. What a validation failure TELLS the user
// ---------------------------------------------------------------------------

describe("§8b: a rejected question is described honestly", () => {
  const settle = async () => {
    for (let i = 0; i < 40; i++) {
      await act(async () => {
        await Promise.resolve();
      });
    }
    await flush();
  };

  function liveHook() {
    vi.mocked(isLiveMode).mockReturnValue(true);
    return renderHook(() => useLandingState());
  }

  const VALIDATION_422 = new ApiClientError("Request body or parameters failed validation.", 422, {
    request_id: "r",
    status: "error",
    error: { code: "validation_error", message: "Request body or parameters failed validation." },
  });

  it("a 422 is NOT badged as a temporary outage", async () => {
    vi.mocked(askQuestion).mockRejectedValue(VALIDATION_422);
    const { result } = liveHook();
    act(() => result.current.send("x".repeat(MAX_QUESTION_LENGTH + 1)));
    await settle();
    const bot = result.current.state.messages[1];
    // Before #446 this was "error", which renders "⚠ TEMPORARILY UNAVAILABLE"
    // over a rejection that is PERMANENT for this input.
    expect(bot.errorKind, "a 422 still wears the transport-outage badge").toBe("rejected");
    expect(bot.refusal, "a 422 must not wear the content-refusal badge either (#120)").toBe(false);
  });

  it("the MESSAGE names the real limit and does not tell the user to wait", async () => {
    vi.mocked(askQuestion).mockRejectedValue(VALIDATION_422);
    const { result } = liveHook();
    act(() => result.current.send("x".repeat(MAX_QUESTION_LENGTH + 1)));
    await settle();
    const toast = result.current.toasts[0];
    // THE MESSAGE, not the title. The pre-existing status-0 test asserted the
    // title alone and was named "shows a generic error" while the message it
    // never looked at was "Network error — is the backend running?".
    expect(toast.message).toContain(String(MAX_QUESTION_LENGTH));
    expect(toast.message.toLowerCase()).toContain("fail the same way");
    // NOT "shorten it and send it again". This branch is a backstop reached only
    // when something bypassed the composers' own refusal, and by then
    // `submitChat` has cleared the box — so that instruction would point at an
    // empty composer. The cell below measures that the box really is empty here,
    // which is what makes this assertion about copy-vs-screen and not taste.
    expect(
      toast.message.toLowerCase(),
      "the 422 copy tells the user to shorten text that is no longer on screen",
    ).not.toContain("shorten it and send it again");
    expect(
      toast.message.toLowerCase(),
      "the copy still promises the problem will pass on its own",
    ).not.toContain("try again in a moment");
    expect(toast.message.toLowerCase()).not.toContain("temporarily");
    // And the server's own developer-facing sentence is not forwarded verbatim.
    expect(toast.message).not.toContain("Request body or parameters failed validation.");
  });

  it("the BADGE the reader actually sees does not say TEMPORARILY UNAVAILABLE", async () => {
    // The two tests above read `errorKind` off the hook, which is a PROXY: it is
    // the value handed to the view, not the thing the view renders. This repo
    // has shipped exactly that mistake — a constant pinned byte-exact while the
    // function that EMITS the header kept a bypass, with the full suite green.
    // So this one renders the real screen and reads the real DOM.
    vi.mocked(isLiveMode).mockReturnValue(true);
    vi.mocked(askQuestion).mockRejectedValue(VALIDATION_422);
    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
    act(() => {
      fireEvent.click(container.querySelector(".cta-button") as HTMLElement);
    });
    const input = container.querySelector(".chat-input") as HTMLInputElement;
    act(() => {
      fireEvent.change(input, { target: { value: "a long question" } });
      fireEvent.keyDown(input, { key: "Enter" });
    });
    await settle();

    const badge = container.querySelector(".notice-badge");
    expect(badge, "a rejected question rendered no notice badge at all").not.toBeNull();
    expect(badge!.textContent).toBe("⚠ NOT ACCEPTED");
    expect(
      container.textContent,
      "the screen still tells the reader this is a passing outage",
    ).not.toContain("TEMPORARILY UNAVAILABLE");
    expect(
      container.textContent,
      "a validation failure must not wear the content-refusal badge either (#120)",
    ).not.toContain("NO SOURCE");
  });

  it("after a 422 the composer is EMPTY, and the copy does not pretend otherwise", async () => {
    // THE CELL THE OTHER REFUSAL CELLS HAVE AND THIS ONE DID NOT. Every other
    // refusal in this file asserts what the box holds afterwards — empty submit
    // keeps the whitespace, in-flight keeps the question, over-length keeps the
    // long text. The 422 path had no such assertion, and it is the one path
    // where the text is genuinely GONE: `submitChat` clears before the request.
    //
    // This does not pretend that is good. It pins the real behaviour so the copy
    // above can be held to it, and so the day someone restores the text this
    // test reddens and the copy gets revisited in the same change.
    vi.mocked(isLiveMode).mockReturnValue(true);
    vi.mocked(askQuestion).mockRejectedValue(VALIDATION_422);
    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
    act(() => {
      fireEvent.click(container.querySelector(".cta-button") as HTMLElement);
    });
    const input = container.querySelector(".chat-input") as HTMLInputElement;
    // Under the client limit, so the composer does NOT refuse it — the 422 has
    // to come from the server for this cell to be measuring the 422 path at all.
    const question = "z".repeat(MAX_QUESTION_LENGTH - 1);
    act(() => {
      fireEvent.change(input, { target: { value: question } });
      fireEvent.keyDown(input, { key: "Enter" });
    });
    await settle();

    expect(
      container.querySelectorAll(".message.user-msg").length,
      "the question never went out, so this is not the 422 path",
    ).toBe(1);
    expect(
      (container.querySelector(".chat-input") as HTMLInputElement).value,
      "the box is no longer empty after a 422 — revisit the 422 copy, which assumes it is",
    ).toBe("");
    const said = container.textContent ?? "";
    expect(said, "the 422 copy asks the user to shorten text the box no longer holds").not.toContain(
      "Shorten it and send it again",
    );
  });

  it("a 401 is described as permanent too, not as a passing blip", async () => {
    // Review finding on this PR: dropping the verbatim catch-all left 401 on the
    // generic "try again in a moment" — the same false-transience the 422 fix
    // removes, at a different status. Fly v6 really did 401 every call on this
    // site for about an hour.
    vi.mocked(askQuestion).mockRejectedValue(
      new ApiClientError("Missing bearer token.", 401, {
        request_id: "r",
        status: "error",
        error: { code: "auth_required", message: "Missing bearer token." },
      }),
    );
    const { result } = liveHook();
    act(() => result.current.send("a question against a misconfigured deploy"));
    await settle();
    const toast = result.current.toasts[0];
    expect(
      toast.message.toLowerCase(),
      "a 401 still tells the visitor to wait for it to pass",
    ).not.toContain("try again in a moment");
    expect(toast.message.toLowerCase()).toContain("retrying will not help");
    expect(result.current.state.messages[1].errorKind).toBe("rejected");
    // Operator detail stays with the operator.
    for (const leak of ["token", "key", "bearer", "401"]) {
      expect(toast.message.toLowerCase(), `the copy leaks "${leak}"`).not.toContain(leak);
    }
  });

  it("no transport failure puts client internals in front of the user", async () => {
    // Every unmapped failure, driven through the real mapper. Each entry is a
    // string `apiFetch` genuinely constructs — see `lib/api.ts`.
    const LEAKY: Array<[string, ApiClientError]> = [
      ["network", new ApiClientError("Network error — is the backend running?", 0, "Network error")],
      ["timeout", new ApiClientError("Request timed out — the server took too long to respond.", 0, "t")],
      ["no envelope", new ApiClientError("Request failed with status 418.", 418, "I'm a teapot")],
    ];
    for (const [label, err] of LEAKY) {
      vi.mocked(askQuestion).mockReset().mockRejectedValue(err);
      const { result, unmount } = liveHook();
      act(() => result.current.send(`question ${label}`));
      await settle();
      const shown = [result.current.toasts[0].message, result.current.state.messages[1].text].join(" || ");
      expect(shown.toLowerCase(), `${label}: names an internal component`).not.toContain("backend");
      expect(shown, `${label}: forwards the raw client string`).not.toContain(err.message);
      expect(shown, `${label}: shows a bare HTTP status number`).not.toMatch(/status \d{3}/i);
      // The partner: something WAS said. A guard that only checks for absence
      // passes on an empty string.
      expect(shown.length, `${label}: the user was told nothing at all`).toBeGreaterThan(20);
      unmount();
    }
  });
});

// ---------------------------------------------------------------------------
// 6. AuthModal — every mode, the in-flight guard that had no test at all
// ---------------------------------------------------------------------------

function renderModal(initialMode?: AuthModalMode) {
  const trigger = document.createElement("button");
  trigger.setAttribute("data-test-trigger", "");
  document.body.appendChild(trigger);
  const triggerRef = createRef<HTMLElement>();
  // @ts-expect-error -- assigning to a ref's .current outside React for the fixture
  triggerRef.current = trigger;
  return render(
    <AuthModal triggerRef={triggerRef} onClose={vi.fn()} initialMode={initialMode} />,
  );
}

/**
 * The four modes, each with the call it makes and how to reach it from a fresh
 * modal. `set-password` needs a signed-in user in the store, because the SHAPE
 * of that form is decided from `user.has_password`.
 */
interface AuthMode {
  /** TYPED as the component's own union, not `string` — see `AUTH_MODES`. */
  label: AuthModalMode;
  /** Drive the modal into this mode and fill the fields it requires. */
  arrive: (user: ReturnType<typeof userEvent.setup>) => Promise<void>;
  /**
   * Drive the modal into this mode and fill NOTHING — the empty-submit cell.
   * Separate from `arrive` because `set-password` needs store state set before
   * the first render to get its form SHAPE, which is setup, not typing.
   */
  arriveEmpty: () => void;
  /** The mocked call that must be made exactly once, and be left hanging. */
  pending: () => { mockImplementation: (f: () => Promise<never>) => unknown; mock: { calls: unknown[] } };
  submitLabel: string;
  /**
   * How many inputs this mode's form renders. Named per mode so the sweep loops
   * below cannot be truncated silently: every input in every mode is compliant,
   * so `continue`->`break`, or checking only `inputs[0]`, would otherwise pass.
   */
  inputCount: number;
}

// Same shape `getCurrentUser` returns — mirrors `AuthModal.test.tsx`'s fixture.
// `has_password: true` is what makes `set-password` render its two-field
// ("Change password") shape, which is the one with the most fields to sweep.
const WITH_PASSWORD = {
  request_id: "req_1",
  user_id: "usr_a",
  email: "a@example.com",
  anonymous: false,
  providers: ["github"],
  has_password: true,
  password_step_up: false,
};

/**
 * `satisfies Record<AuthModalMode, …>` is the whole point: it makes the sweep's
 * completeness a TYPE obligation. Add a fifth member to `AuthModalMode` and this
 * object is missing a key, which `tsc -b` rejects in CI. Nothing here has to be
 * remembered.
 */
const AUTH_MODES_BY_LABEL = {
  login: {
    label: "login",
    submitLabel: "Sign in",
    inputCount: 2,
    arriveEmpty: () => renderModal("login"),
    arrive: async (user) => {
      renderModal("login");
      const dialog = screen.getByRole("dialog");
      await user.type(within(dialog).getByLabelText("Email"), "a@b.com");
      await user.type(within(dialog).getByLabelText("Password"), "hunter2hunter2");
    },
    pending: () => vi.mocked(login) as never,
  },
  register: {
    label: "register",
    submitLabel: "Create account",
    inputCount: 2,
    arriveEmpty: () => renderModal("register"),
    arrive: async (user) => {
      renderModal("register");
      const dialog = screen.getByRole("dialog");
      await user.type(within(dialog).getByLabelText("Email"), "a@b.com");
      await user.type(within(dialog).getByLabelText("Password"), "hunter2hunter2");
    },
    pending: () => vi.mocked(register) as never,
  },
  "magic-link": {
    label: "magic-link",
    submitLabel: "Send link",
    inputCount: 1,
    arriveEmpty: () => renderModal("magic-link"),
    arrive: async (user) => {
      renderModal("magic-link");
      const dialog = screen.getByRole("dialog");
      await user.type(within(dialog).getByLabelText("Email"), "a@b.com");
    },
    pending: () => vi.mocked(requestMagicLink) as never,
  },
  "set-password": {
    label: "set-password",
    submitLabel: "Save password",
    inputCount: 2,
    arriveEmpty: () => {
      vi.mocked(getCurrentUser).mockResolvedValue(WITH_PASSWORD as never);
      __testOnly.setState({ status: "signed-in", user: WITH_PASSWORD as never });
      renderModal("set-password");
    },
    arrive: async (user) => {
      // Both halves are needed: the store state decides the form SHAPE on the
      // first render, and `getCurrentUser` keeps the bootstrap from resetting it
      // back to anonymous a tick later.
      vi.mocked(getCurrentUser).mockResolvedValue(WITH_PASSWORD as never);
      __testOnly.setState({ status: "signed-in", user: WITH_PASSWORD as never });
      renderModal("set-password");
      const dialog = screen.getByRole("dialog");
      await user.type(within(dialog).getByLabelText("Current password"), "oldpassword1");
      await user.type(within(dialog).getByLabelText("New password"), "newpassword1");
    },
    pending: () => vi.mocked(updatePassword) as never,
  },
} satisfies Record<AuthModalMode, AuthMode>;

const AUTH_MODES: AuthMode[] = Object.values(AUTH_MODES_BY_LABEL);

describe("§8b cells — AuthModal, all four modes", () => {
  /**
   * CELLS DELIBERATELY NOT SWEPT HERE, named because §8b's own rule is "where a
   * cell is meaningless for a surface, say so; do not leave it unmentioned" —
   * and the first draft of this file broke that rule in the same commit that
   * wrote it:
   *
   *   - PASTE-ONLY. These fields have no submit-on-keystroke path at all; the
   *     only way to submit is the button, which the empty/in-flight cells below
   *     already drive. Pasting into a field and clicking is the same code path
   *     as typing into it and clicking, so a paste cell here would be a second
   *     copy of a test that exists.
   *   - WHITESPACE-ONLY. There is no trim on this form and there should not be:
   *     `"   "` is a legitimate substring of a password, and `required` /
   *     `valueMissing` are both false for it BY SPEC. The server decides — it
   *     is the one that knows a whitespace password is merely a bad password
   *     rather than a malformed request. So this cell is not "untested", it is
   *     "not applicable, and asserting it would encode a behaviour we do not
   *     want". The composers are where whitespace-is-empty holds, and they
   *     sweep it above.
   */
  it("covers every mode the component can be in", () => {
    // TIED TO THE COMPONENT'S OWN UNION, not to a literal in this file. The
    // first version of this test compared a local array to a local literal and
    // its comment claimed a fifth `AuthModalMode` member would redden it —
    // which was FALSE: nothing referenced the type at all, so the only change
    // that could fail it was editing the array six lines above. That is the
    // hand-kept-list failure mode this file exists to avoid, reintroduced ten
    // lines from the registry that avoids it properly.
    //
    // `AUTH_MODES_BY_LABEL` below is `satisfies Record<AuthModalMode, ...>`, so
    // a fifth member of the union is now a COMPILE error (missing key) and a
    // row for a mode that no longer exists is a compile error too. `tsc -b`
    // runs in CI, so this is enforced, not decorative.
    // ONE assertion, and it is the one that can fail. An earlier draft also
    // compared `AUTH_MODES.length` to `Object.keys(AUTH_MODES_BY_LABEL).length`
    // under the message "a mode was dropped from the sweep" — but `AUTH_MODES`
    // IS `Object.values(AUTH_MODES_BY_LABEL)`, so that is true of every object
    // in existence. A check that cannot fail, carrying a message about a
    // failure, is worse than no check: it reads as coverage.
    //
    // The real enforcement is `satisfies Record<AuthModalMode, AuthMode>` on the
    // object itself, which `tsc -b` checks in CI — a fifth union member is a
    // missing key. This runtime line adds the one thing the type cannot: that
    // each entry's `label` actually matches the key it is filed under, so a
    // copy-paste row cannot sweep the same mode twice under two keys.
    for (const [key, mode] of Object.entries(AUTH_MODES_BY_LABEL)) {
      expect(mode.label, `the entry filed under "${key}" describes mode "${mode.label}"`).toBe(key);
    }
    expect(AUTH_MODES.length, "the sweep ran over no modes at all").toBeGreaterThan(0);
  });

  for (const mode of AUTH_MODES) {
    describe(mode.label, () => {
      // CELL: submit-while-in-flight AND rapid double-submit. `submitting ||`
      // in the disabled expression had NO test in ANY mode — a grep for the
      // in-flight label "Working…" found it only in the component itself.
      it("in-flight: the submit button disables and a second click sends nothing more", async () => {
        const user = userEvent.setup();
        const call = mode.pending();
        call.mockImplementation(() => new Promise<never>(() => {}));
        await mode.arrive(user);
        const dialog = screen.getByRole("dialog");
        const submit = within(dialog).getByRole("button", { name: mode.submitLabel });
        expect(submit, `${mode.label}: the button was disabled before anything was submitted`).not.toBeDisabled();

        await user.click(submit);
        expect(call.mock.calls.length, `${mode.label}: the first submit never fired`).toBe(1);

        // THE ASSERTION `submitting ||` EXISTS FOR. `coolingDown` cannot be
        // producing it: it is false in every mode but magic-link, and in
        // magic-link it is only set after a SUCCESSFUL send — this one hangs.
        const busy = within(dialog).getByRole("button", { name: "Working…" });
        expect(busy, `${mode.label}: the button stayed live while a request was in flight`).toBeDisabled();

        // Rapid double-submit: clicking again changes nothing.
        await user.click(busy);
        await user.click(busy);
        expect(
          call.mock.calls.length,
          `${mode.label}: clicking the busy button submitted again`,
        ).toBe(1);
      });

      // CELL: empty submit. BEHAVIOURAL, not an attribute check.
      //
      // This test was wrong TWICE and the second version is why §8b now carries
      // a measured table instead of a recollection:
      //
      //   1. It asserted `toBeRequired()` — the markup, not the behaviour — on
      //      the belief that jsdom does no constraint validation.
      //   2. It was upgraded to `checkValidity()` per input, on the belief that
      //      jsdom does not validate AS PART OF SUBMISSION because
      //      `HTMLFormElement.prototype.submit` is `notImplemented`.
      //
      // Both beliefs were wrong, and the second one mattered. `submit()` never
      // validates in ANY browser — that is the spec, not a jsdom gap. The
      // validating entry points are `requestSubmit()` and submit-button
      // activation, and jsdom implements both INCLUDING the validation step:
      // `HTMLFormElement-impl.js` `requestSubmit()` runs
      // `if (!this.hasAttributeNS(null, "novalidate") && !this.reportValidity()) return;`
      // BEFORE firing `submit`. This form has no `noValidate`.
      //
      // So the real thing is provable here, and this is it: click the submit
      // button on an empty form and the request is never made. Measured on the
      // real component — empty gives 0 submit events and 0 calls, filled gives
      // 1 of each.
      it("empty submit: clicking submit on an empty form makes NO request", async () => {
        const user = userEvent.setup();
        const call = mode.pending();
        call.mockImplementation(() => new Promise<never>(() => {}));
        // `arriveEmpty`, not `arrive` — the latter fills the fields, which is
        // the opposite of this cell.
        mode.arriveEmpty();
        const dialog = screen.getByRole("dialog");
        const form = dialog.querySelector("form");
        expect(form, `${mode.label}: no form`).not.toBeNull();
        expect(
          form!.hasAttribute("novalidate"),
          `${mode.label}: the form opted OUT of validation — nothing below proves anything`,
        ).toBe(false);

        const inputs = Array.from(dialog.querySelectorAll("input"));
        expect(inputs.length, `${mode.label}: wrong field count — the sweep is on the wrong form`).toBe(
          mode.inputCount,
        );
        for (const input of inputs) {
          expect(input, `${mode.label}: a field is submittable while empty`).toBeRequired();
          expect(input.value, `${mode.label}: the form did not start empty`).toBe("");
        }

        await user.click(within(dialog).getByRole("button", { name: mode.submitLabel }));
        expect(
          call.mock.calls.length,
          `${mode.label}: an EMPTY form sent a request`,
        ).toBe(0);

        // THE PARTNER. Without it this passes on a button that does nothing at
        // all, a broken locator, or a form that can never submit — absence read
        // as success, which is this repo's most-repeated scar.
        //
        // `cleanup()` first: `arrive` renders its own modal, and two live
        // dialogs make every `getByRole("dialog")` below throw on ambiguity.
        cleanup();
        document.querySelectorAll("[data-test-trigger]").forEach((el) => el.remove());
        await mode.arrive(user);
        const filled = screen.getByRole("dialog");
        await user.click(within(filled).getByRole("button", { name: mode.submitLabel }));
        expect(
          call.mock.calls.length,
          `${mode.label}: a FILLED form did not submit either — the test proves nothing`,
        ).toBe(1);
      });
    });
  }

  // CELL: over-length, EVERY field in EVERY mode.
  //
  // A surviving mutant found by the review's independent verifier: the ceiling
  // could be stripped from `set-password`'s current-password field and nothing
  // reddened. The two tests below measured the email field and the
  // login/register password field; set-password's two fields were typed into
  // and never measured, and the backend parity guard pins AuthModal's CONSTANTS
  // but not that any input USES them. So a whole mode's bounds were unguarded.
  //
  // This sweeps every rendered input in every mode instead of naming three of
  // five by hand — the same reason the surface registry is derived rather than
  // listed.
  it("over-length: every auth field in every mode carries its server bound", async () => {
    const user = userEvent.setup();
    const seen: string[] = [];
    for (const mode of AUTH_MODES) {
      await mode.arrive(user);
      const dialog = screen.getByRole("dialog");
      const inputs = Array.from(dialog.querySelectorAll("input"));
      expect(inputs.length, `${mode.label}: wrong field count`).toBe(mode.inputCount);
      for (const input of inputs) {
        const kind = input.getAttribute("type");
        const max = input.getAttribute("maxlength");
        expect(max, `${mode.label}: a ${kind} field has NO ceiling`).not.toBeNull();
        expect(max, `${mode.label}: the ${kind} ceiling does not match the server`).toBe(
          kind === "email" ? "255" : "128",
        );
        seen.push(`${mode.label}/${kind}`);
      }
      cleanup();
      document.querySelectorAll("[data-test-trigger]").forEach((el) => el.remove());
    }
    // The partner: every field of every mode was actually reached. A loop that
    // broke early, or a mode that rendered nothing, would otherwise pass.
    expect(seen).toEqual([
      "login/email",
      "login/password",
      "register/email",
      "register/password",
      "magic-link/email",
      "set-password/password",
      "set-password/password",
    ]);
  });

  it("over-length: email carries the server's 255 ceiling in every mode that asks for one", async () => {
    const user = userEvent.setup();
    for (const mode of AUTH_MODES.filter((m) => m.label !== "set-password")) {
      await mode.arrive(user);
      const dialog = screen.getByRole("dialog");
      const email = within(dialog).getByLabelText("Email");
      expect(email, `${mode.label}: email had no ceiling`).toHaveAttribute("maxlength", "255");
      expect(email, `${mode.label}: email had no floor`).toHaveAttribute("minlength", "3");
      cleanup();
      document.querySelectorAll("[data-test-trigger]").forEach((el) => el.remove());
    }
  });

  it("over-length: password carries 128, and the LOGIN field keeps no 8-char floor", async () => {
    const user = userEvent.setup();
    renderModal("login");
    let dialog = screen.getByRole("dialog");
    // The floor is deliberately absent on sign-in: `LoginRequest.password` is
    // `min_length=1`, because an account created before the rule must still be
    // able to sign in. A `minLength={8}` here would lock those accounts out of
    // their own recovery path.
    expect(within(dialog).getByLabelText("Password")).toHaveAttribute("maxlength", "128");
    expect(within(dialog).getByLabelText("Password")).not.toHaveAttribute("minlength");
    await user.click(within(dialog).getByText("Need an account? Register"));
    dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText("Password")).toHaveAttribute("minlength", "8");
    expect(within(dialog).getByLabelText("Password")).toHaveAttribute("maxlength", "128");
  });
});

// ---------------------------------------------------------------------------
// §8b cells — AnswerActions (the feedback forms, ADR-0005 §6)
// ---------------------------------------------------------------------------
//
// Four surfaces: #0 the reason radios, #1 the report comment, #2 the source
// link, #3 the source note. Cell by cell:
//
// - Empty submit / whitespace-only: EVERY field here is optional, so there is no
//   "refuse the empty submit" cell. The rule that applies is that empty and
//   whitespace-only mean "not given": the request goes out WITHOUT the field
//   (asserted below), never with "" or "   " (the server refuses an empty URL).
// - Over-length: refused at submit, announced, text kept; no length attribute.
//   Counted as code points, like the server (the emoji case below).
// - Paste-only: the value arrives by an input event with no keystroke; submit
//   still trims and sends it.
// - Rapid double-submit and submit-while-in-flight: one request at a time.
// - Submit-while-disabled: NOT APPLICABLE. No control in these forms is ever
//   disabled; the in-flight guard refuses in the handler instead (tested).
// - IME: the link field blocks Enter while composing (isSubmitKey); the two
//   textareas never submit on Enter; the radios are not typed into.

describe("§8b cells — AnswerActions", () => {
  const REF = { sessionId: "s1", messageId: "m1", question: "How?" };
  const status = () => screen.getByTestId("answer-actions-status").textContent;
  const fetchMock = () => vi.mocked(apiFetch);

  beforeEach(() => {
    fetchMock().mockReset().mockResolvedValue({ request_id: "r" } as never);
  });

  const openReport = async () => {
    render(<AnswerActions {...REF} />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Not helpful" })));
    return screen.getByLabelText("What is wrong? (optional)") as HTMLTextAreaElement;
  };
  const openSource = async () => {
    render(<AnswerActions {...REF} refusal />);
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Request this source" })),
    );
    return {
      link: screen.getByLabelText("Link to the page (optional)") as HTMLInputElement,
      note: screen.getByLabelText("Anything else? (optional)") as HTMLTextAreaElement,
    };
  };
  const submit = async (name: string) =>
    act(async () => fireEvent.click(screen.getByRole("button", { name })));
  const sentBody = (i = 0) => JSON.parse(String(fetchMock().mock.calls[i][1]?.body));

  it("no length attribute on any of the four fields", async () => {
    // Turns red if: a maxLength attribute (silent clamping) comes back.
    const { link, note } = await openSource();
    cleanup();
    const comment = await openReport();
    for (const el of [link, note, comment]) expect(el.hasAttribute("maxlength")).toBe(false);
  });

  it("whitespace-only is 'not given': the request goes out without the field", async () => {
    // Turns red if: "   " is sent as a comment instead of being dropped.
    const comment = await openReport();
    fireEvent.change(comment, { target: { value: "   \t " } });
    await submit("Send report");
    expect(sentBody()).toEqual({ rating: "down", reason: "wrong" });
  });

  it("over-length comment: refused at submit, announced, text kept; the limit itself is accepted", async () => {
    // Turns red if: the limit is not checked, is checked off by one, or the text
    // is cleared on refusal.
    const comment = await openReport();
    fireEvent.change(comment, { target: { value: "x".repeat(MAX_FEEDBACK_COMMENT + 1) } });
    await submit("Send report");
    expect(fetchMock()).not.toHaveBeenCalled();
    expect(status()).toBe(
      `The comment is too long: ${MAX_FEEDBACK_COMMENT + 1} of ${MAX_FEEDBACK_COMMENT} characters. Please shorten it.`,
    );
    expect(comment.value).toHaveLength(MAX_FEEDBACK_COMMENT + 1);
    fireEvent.change(comment, { target: { value: "x".repeat(MAX_FEEDBACK_COMMENT) } });
    await submit("Send report");
    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  it("over-length counts code points, like the server: 1,000 emoji are allowed", async () => {
    // "😀" is 2 UTF-16 units and 1 code point. Turns red if: `.length` is used.
    const comment = await openReport();
    fireEvent.change(comment, { target: { value: "😀".repeat(MAX_FEEDBACK_COMMENT) } });
    await submit("Send report");
    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  it("over-length link and note are refused and kept", async () => {
    // Turns red if: either source-request field skips its limit.
    const { link, note } = await openSource();
    fireEvent.change(link, { target: { value: "https://d/" + "a".repeat(MAX_SOURCE_URL) } });
    await submit("Send request");
    expect(fetchMock()).not.toHaveBeenCalled();
    expect(status()).toMatch(/^The link is too long/);
    fireEvent.change(link, { target: { value: "" } });
    fireEvent.change(note, { target: { value: "n".repeat(MAX_SOURCE_NOTE + 1) } });
    await submit("Send request");
    expect(fetchMock()).not.toHaveBeenCalled();
    expect(status()).toMatch(/^The note is too long/);
    expect(note.value).toHaveLength(MAX_SOURCE_NOTE + 1);
  });

  it("paste-only: a pasted link with no keystroke is trimmed and sent", async () => {
    // Turns red if: the value is read from a key handler rather than the field.
    const { link } = await openSource();
    fireEvent.input(link, { target: { value: "  https://d/p  " } });
    fireEvent.change(link, { target: { value: "  https://d/p  " } });
    await submit("Send request");
    expect(sentBody()).toEqual({ question: "How?", message_id: "m1", requested_url: "https://d/p" });
  });

  it("rapid double-submit: two clicks in one tick send ONE request", async () => {
    // Turns red if: the in-flight guard reads state instead of a ref.
    let release: () => void = () => {};
    fetchMock().mockImplementation(() => new Promise((r) => (release = () => r({} as never))));
    await openReport();
    const btn = screen.getByRole("button", { name: "Send report" });
    await act(async () => {
      fireEvent.click(btn);
      fireEvent.click(btn);
    });
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    await act(async () => release());
  });

  it("submit-while-in-flight: refused, announced, and the text kept", async () => {
    // Turns red if: a second submit during a pending request sends, or is silent.
    let release: () => void = () => {};
    fetchMock().mockImplementation(() => new Promise((r) => (release = () => r({} as never))));
    const comment = await openReport();
    fireEvent.change(comment, { target: { value: "first" } });
    await submit("Send report");
    fireEvent.change(comment, { target: { value: "second thoughts" } });
    await submit("Send report");
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(status()).toBe("Still sending. Please wait a moment.");
    expect(comment.value).toBe("second thoughts");
    await act(async () => release());
  });

  it("IME: Enter while composing in the link field does not submit", async () => {
    // Turns red if: the link field's Enter guard is removed (the browser would
    // submit the form mid-composition).
    const { link } = await openSource();
    const composing = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    Object.defineProperty(composing, "isComposing", { value: true });
    link.dispatchEvent(composing);
    expect(composing.defaultPrevented).toBe(true);
    const plain = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    link.dispatchEvent(plain);
    expect(plain.defaultPrevented).toBe(false); // partner: a plain Enter is left alone
  });
});

