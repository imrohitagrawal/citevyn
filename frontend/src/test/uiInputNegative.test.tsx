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
import { AuthModal } from "../components/AuthModal";
import { useLandingState } from "../hooks/useLandingState";
import { __testOnly } from "../lib/authStore";
import { isLiveMode, createSession, askQuestion, login, register, getCurrentUser } from "../lib/api";
import { requestMagicLink, updatePassword } from "../lib/authActions";
import { ApiClientError } from "../lib/types";
import { MAX_QUESTION_LENGTH, isSubmitKey } from "../lib/composerInput";

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
 * Every input surface in the app, as `file#ordinal (type=…)`.
 *
 * The ordinal is the element's position among the `<input>`/`<textarea>`
 * elements in its own file, in source order. It is deliberately positional
 * rather than keyed on an attribute: two of AuthModal's three fields are
 * `type="password"` and the third's `autoComplete` is a ternary, so there is no
 * static attribute that distinguishes all five. A positional key also fails
 * LOUDLY when an input is inserted in the middle, which is the behaviour wanted
 * — an inserted field should force someone to look at this list.
 */
const SURFACES = [
  "AuthModal.tsx#0 (type=email)",
  "AuthModal.tsx#1 (type=password)",
  "AuthModal.tsx#2 (type=password)",
  "ChatView.tsx#0 (type=text)",
  "Hero.tsx#0 (type=<none>)",
] as const;

const COMPONENTS_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "components");

/**
 * Walk every non-test component with the TypeScript compiler and return one
 * entry per `<input>` / `<textarea>` JSX element, in `SURFACES`' format.
 *
 * The compiler, not a regex: `<input` appears inside this repo's comments and
 * inside string literals, and a regex counting it would both over- and
 * under-report. `ts.forEachChild` sees only real JSX elements.
 */
function collectInputSurfaces(): string[] {
  const found: string[] = [];
  const componentFiles = readdirSync(COMPONENTS_DIR)
    .filter((f) => f.endsWith(".tsx") && !f.includes(".test."))
    .sort();
  expect(
    componentFiles.length,
    "no component files were read — the traversal is looking at the wrong directory",
  ).toBeGreaterThan(0);

  for (const file of componentFiles) {
    const source = ts.createSourceFile(
      file,
      readFileSync(join(COMPONENTS_DIR, file), "utf8"),
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TSX,
    );
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
          found.push(`${file}#${ordinal++} (type=${type})`);
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
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
    // Proves the guard above would bite, without editing a component: run the
    // same traversal over a synthetic file containing one extra input.
    const source = ts.createSourceFile(
      "Synthetic.tsx",
      `export const X = () => (<form>{/* <input type="fake" /> in a comment */}
         <label>a<input type="search" /></label>
         <textarea defaultValue="" />
       </form>);`,
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TSX,
    );
    const found: string[] = [];
    let ordinal = 0;
    const visit = (node: ts.Node): void => {
      const opening =
        ts.isJsxSelfClosingElement(node) ? node : ts.isJsxOpeningElement(node) ? node : null;
      if (opening) {
        const tag = opening.tagName.getText(source);
        if (tag === "input" || tag === "textarea") found.push(`#${ordinal++}:${tag}`);
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
    // Two elements, and the commented-out `<input>` is NOT one of them — which
    // is the whole reason this uses the compiler and not a regex.
    expect(found).toEqual(["#0:input", "#1:textarea"]);
  });
});

// ---------------------------------------------------------------------------
// 2. The composers: hero and chat, the same cells over both
// ---------------------------------------------------------------------------

interface Composer {
  label: string;
  open: (root: HTMLElement) => void;
  input: string;
}

const COMPOSERS: Composer[] = [
  { label: "hero", open: () => {}, input: "#hero-input" },
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

    // CELL: over-length. The attribute is the whole client-side mechanism, and
    // jsdom does NOT enforce it (see §8b) — so this asserts the attribute and
    // the backend boundary test asserts the behaviour that matters.
    it(`over-length: the box carries the server's ${MAX_QUESTION_LENGTH}-character ceiling`, () => {
      const { input } = renderComposer(c);
      expect(input, `${c.label}: no maxLength — the browser accepts what the server 422s`).toHaveAttribute(
        "maxlength",
        String(MAX_QUESTION_LENGTH),
      );
    });

    // CELL: paste-only. A question that arrives entirely by paste — no keydown
    // ever fires for the text — must still submit, and must still be trimmed.
    it("paste-only: text that arrived by paste submits normally", async () => {
      const { container, input } = renderComposer(c);
      act(() => {
        fireEvent.paste(input, { clipboardData: { getData: () => "  pasted question  " } });
        // React controlled inputs read `value` from the change event, not from
        // the paste event; the browser fires both, so the test does too.
        fireEvent.change(input, { target: { value: "  pasted question  " } });
        fireEvent.keyDown(input, { key: "Enter" });
      });
      await flush();
      expect(userMessages(container), `${c.label}: a pasted question was not sent`).toBe(1);
      expect(
        container.querySelector(".message.user-msg")!.textContent,
        `${c.label}: the pasted question was not trimmed`,
      ).toContain("pasted question");
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

describe("§8b: isSubmitKey is the one rule both composers use", () => {
  it("submits on a plain Enter and nothing else", () => {
    expect(isSubmitKey({ key: "Enter" })).toBe(true);
    expect(isSubmitKey({ key: "a" })).toBe(false);
    expect(isSubmitKey({ key: "Escape" })).toBe(false);
    expect(isSubmitKey({ key: " " })).toBe(false);
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
    expect(toast.message.toLowerCase()).toContain("shorten it");
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

function renderModal(initialMode?: "login" | "register" | "magic-link" | "set-password") {
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
  label: string;
  /** Drive the modal into this mode and fill the fields it requires. */
  arrive: (user: ReturnType<typeof userEvent.setup>) => Promise<void>;
  /** The mocked call that must be made exactly once, and be left hanging. */
  pending: () => { mockImplementation: (f: () => Promise<never>) => unknown; mock: { calls: unknown[] } };
  submitLabel: string;
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

const AUTH_MODES: AuthMode[] = [
  {
    label: "login",
    submitLabel: "Sign in",
    arrive: async (user) => {
      renderModal("login");
      const dialog = screen.getByRole("dialog");
      await user.type(within(dialog).getByLabelText("Email"), "a@b.com");
      await user.type(within(dialog).getByLabelText("Password"), "hunter2hunter2");
    },
    pending: () => vi.mocked(login) as never,
  },
  {
    label: "register",
    submitLabel: "Create account",
    arrive: async (user) => {
      renderModal("register");
      const dialog = screen.getByRole("dialog");
      await user.type(within(dialog).getByLabelText("Email"), "a@b.com");
      await user.type(within(dialog).getByLabelText("Password"), "hunter2hunter2");
    },
    pending: () => vi.mocked(register) as never,
  },
  {
    label: "magic-link",
    submitLabel: "Send link",
    arrive: async (user) => {
      renderModal("magic-link");
      const dialog = screen.getByRole("dialog");
      await user.type(within(dialog).getByLabelText("Email"), "a@b.com");
    },
    pending: () => vi.mocked(requestMagicLink) as never,
  },
  {
    label: "set-password",
    submitLabel: "Save password",
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
];

describe("§8b cells — AuthModal, all four modes", () => {
  it("covers every mode the component can be in", () => {
    // The registry partner: `AuthModalMode` has four members and all four are
    // swept. A fifth mode added without a row here reddens this.
    expect(AUTH_MODES.map((m) => m.label)).toEqual([
      "login",
      "register",
      "magic-link",
      "set-password",
    ]);
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

      // CELL: empty submit and whitespace-only.
      //
      // MEASURED, not assumed, because the first draft of this test asserted
      // only `toBeRequired()` on the strength of "jsdom can't do constraint
      // validation" — which is half wrong. jsdom DOES implement
      // `checkValidity()` and `ValidityState`, and an empty required field
      // really does report `valueMissing: true`. What jsdom does NOT do is run
      // that validation as part of a submit (`HTMLFormElement.prototype.submit`
      // is `notImplemented`) or show the native bubble.
      //
      // So this asserts the CONSTRAINT, not just the attribute: the attribute is
      // what the markup says, `checkValidity()` is what the DOM concludes from
      // it. The remaining gap — that a real browser BLOCKS the submit on that
      // conclusion — is a browser guarantee this suite does not exercise, and
      // §8b says so rather than pretending otherwise.
      it("empty submit: every required field is invalid while empty", async () => {
        const user = userEvent.setup();
        await mode.arrive(user);
        const dialog = screen.getByRole("dialog");
        const inputs = Array.from(dialog.querySelectorAll("input"));
        expect(inputs.length, `${mode.label}: the form rendered no inputs`).toBeGreaterThan(0);
        for (const input of inputs) {
          // `current_password` is the one field optional AT THE SCHEMA LEVEL,
          // but the form renders it only when the server WILL require it.
          expect(input, `${mode.label}: an input is submittable while empty`).toBeRequired();
          const before = input.value;
          input.value = "";
          expect(
            input.validity.valueMissing,
            `${mode.label}: an emptied field does not report valueMissing`,
          ).toBe(true);
          expect(input.checkValidity(), `${mode.label}: an empty field reports VALID`).toBe(false);
          // The partner: with content it must be valid again, so this cannot
          // pass on a field that is invalid for some unrelated reason.
          input.value = before || "x".repeat(12);
          expect(
            input.checkValidity(),
            `${mode.label}: a FILLED field still reports invalid — the check is measuring something else`,
          ).toBe(true);
        }
      });
    });
  }

  // CELL: over-length, at both bounds the server declares.
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
