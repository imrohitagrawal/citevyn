import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LandingPage } from "./LandingPage";
import { isLiveMode, createSession, askQuestion, login } from "../lib/api";
import { __testOnly } from "../lib/authStore";

/**
 * ADR-0004 PR 9: "session claim wired client-side" — the backend already
 * reassigns an anonymous principal's chat history to a newly-authenticated
 * account transparently, via the shared session cookie (PR 6's
 * claim_and_login); the frontend never refetches anything for the CURRENT
 * tab, since the session_id doesn't change, only its owner. The one thing
 * that genuinely needed client-side wiring — because the auth module has
 * no visibility into chat state — is confirming to the user that it
 * happened: this test proves that confirmation actually fires, and fires
 * with the RIGHT copy depending on whether there was a conversation to save.
 */
vi.mock("../lib/api", () => ({
  isLiveMode: vi.fn(() => false),
  createSession: vi.fn(),
  askQuestion: vi.fn(),
  getCurrentUser: vi.fn(() => Promise.resolve(null)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
}));

beforeEach(() => {
  vi.mocked(isLiveMode).mockReturnValue(false);
  vi.mocked(createSession).mockReset();
  vi.mocked(askQuestion).mockReset();
  __testOnly.setState(__testOnly.initialState);
  __testOnly.resetBootstrapped();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/**
 * #344: these two tests were the two slowest of the whole 443-test suite
 * (2439 ms and 1099 ms; the third-slowest was 673 ms), and under CPU contention
 * they blew vitest's 5 s per-test timeout deterministically — in a REQUIRED
 * status check. Both costs below were MEASURED, not guessed; the issue's own
 * hypothesis (inter-keystroke macrotasks) turned out to be a third of the story.
 *
 * COST 1 — a 24 ms interval re-rendering React for the life of the test.
 * `useLandingState` drives the hero typewriter through `streamText`, a
 * `setInterval` that dispatches a state update every 24 ms for as long as the
 * landing screen is mounted; the demo answer streams the same way. Every test
 * in this file pays it, but the bill is proportional to how long the test
 * lives, so the eighteen short ones (36-193 ms) tick a handful of times while
 * these two tick for the whole sign-in interaction. That is a feedback loop —
 * a slower machine means more re-renders means a slower test — which is exactly
 * why they degrade under load and pass in isolation.
 *
 * COST 2 — one await per typed character. Under contention each await is a trip
 * through a scheduler shared with eight other jsdom workers; 38 characters cost
 * 2537 ms in the email field alone. See `openAuthModalAndSignIn`.
 *
 * THE FIX. Fake ONLY `setInterval`, scoped to this describe block, so the
 * typewriter never fires; and type short credentials. `delay: null` drops the
 * macrotask userEvent would otherwise yield between keystrokes, while every
 * keydown/keypress/input/keyup still fires, in order, through React's real
 * handlers. No timeout is raised and no assertion is relaxed — both tests still
 * fail if `handleAuthenticated` picks the wrong toast copy (verified by
 * mutation, in both directions).
 *
 * MEASURED, full suite, three runs each under identical induced load (30 busy
 * processes on 10 cores):
 *   before — 2 failed / 441 passed EVERY run, at 5128/5481, 5106/5267, 5018/5099 ms
 *   after  — 447 passed / 447 every run
 * Isolated, the same two tests went 2439 ms -> 516 ms and 1099 ms -> 295 ms.
 */
const signInUser = () => userEvent.setup({ delay: null });

/**
 * The credentials are deliberately SHORT. `login` is mocked and no assertion in
 * this file reads what was typed, but `user.type` awaits once per character and
 * every await is a trip through a scheduler shared with eight other jsdom
 * workers. Measured under identical induced load: 38 characters cost 2537 ms in
 * the email field alone; 8 characters cost 92 ms. The field constraints still
 * hold — the email is a valid address for `type="email" required`, and login
 * mode sets no `minLength` on the password (AuthModal.tsx:469 applies it to
 * register/set-password only), so nothing here is typing past a validation
 * rule that a real user would have to satisfy.
 */
async function openAuthModalAndSignIn(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Sign in" }));
  const dialog = await screen.findByRole("dialog");
  await user.type(within(dialog).getByLabelText("Email"), "a@b.co");
  await user.type(within(dialog).getByLabelText("Password"), "pw");
  await user.click(within(dialog).getByRole("button", { name: "Sign in" }));
}

describe("sign-in confirms claimed chat history (ADR-0004 PR 9)", () => {
  beforeEach(() => {
    // ONLY setInterval. Faking setTimeout as well deadlocks the suite:
    // @testing-library/dom advances a fake clock inside waitFor only when it
    // can see a JEST fake-timer install, which under vitest it cannot, so
    // every findBy* polls a clock nobody moves. Measured: all four sign-in
    // assertions hung to the full 5 s timeout even on an idle machine.
    // Faking the interval alone freezes the typewriter and leaves every
    // setTimeout — waitFor's polling, userEvent's internal waits, the toast's
    // auto-dismiss — running for real.
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  });

  afterEach(() => {
    // Back to real timers BEFORE the file-level cleanup() unmounts the tree,
    // so nothing downstream inherits a frozen clock.
    vi.useRealTimers();
  });

  it("shows the history-saved toast when the tab already has messages", async () => {
    vi.mocked(login).mockResolvedValueOnce({
      request_id: "req_1",
      user_id: "usr_a",
      email: "claim@example.com",
      anonymous: false,
      providers: [],
      has_password: true,
    password_step_up: false,
    });
    const user = signInUser();
    render(<LandingPage theme="light" onThemeChange={() => {}} />);

    // Enter chat and pick a suggested question -- this is what puts a real
    // message into state.messages, the signal handleAuthenticated reads to
    // decide which toast copy to show.
    await user.click(screen.getAllByRole("button", { name: "Try the demo" })[0]);
    await user.click(await screen.findByRole("button", { name: "What is Claude Code?" }));

    await openAuthModalAndSignIn(user);

    expect(await screen.findByText("Your conversation is saved to your account.")).toBeInTheDocument();
  });

  it("shows a plain welcome toast when there is no chat history yet", async () => {
    vi.mocked(login).mockResolvedValueOnce({
      request_id: "req_1",
      user_id: "usr_b",
      email: "claim@example.com",
      anonymous: false,
      providers: [],
      has_password: true,
    password_step_up: false,
    });
    const user = signInUser();
    render(<LandingPage theme="light" onThemeChange={() => {}} />);

    await openAuthModalAndSignIn(user);

    expect(await screen.findByText("Welcome to CiteVyn.")).toBeInTheDocument();
    expect(screen.queryByText("Your conversation is saved to your account.")).not.toBeInTheDocument();
  });
});

describe("OAuth return-trip toast (ADR-0004 PR 12)", () => {
  const originalLocation = window.location;

  afterEach(() => {
    // @ts-expect-error -- restoring jsdom's original window.location
    window.location = originalLocation;
  });

  function setSearch(search: string, hash = "") {
    // @ts-expect-error -- jsdom's window.location is not directly assignable
    delete window.location;
    // @ts-expect-error -- a minimal stand-in is enough; only .search/.pathname/.hash are read
    window.location = { ...originalLocation, search, pathname: "/", hash };
  }

  it("mounts the set-a-password nudge on ?auth=ok for a passwordless account (ADR-0004 PR 14)", async () => {
    // RED if LandingPage stops mounting PasswordNudge on the auth=ok return
    // trip, or if the nudge ignores has_password.
    const { getCurrentUser } = await import("../lib/api");
    const passwordless = {
      request_id: "req_1",
      user_id: "usr_ml",
      email: "ml@example.com",
      anonymous: false,
      providers: [],
      has_password: false,
    password_step_up: false,
    };
    vi.mocked(getCurrentUser).mockResolvedValue(passwordless);
    __testOnly.setState({ status: "signed-in", user: passwordless });
    setSearch("?auth=ok");

    render(<LandingPage theme="light" onThemeChange={() => {}} />);

    expect(await screen.findByText("Set a password as a backup way to sign in.")).toBeInTheDocument();
  });

  it("does not nudge an account that already has a password", async () => {
    const { getCurrentUser } = await import("../lib/api");
    const withPassword = {
      request_id: "req_1",
      user_id: "usr_pw",
      email: "pw@example.com",
      anonymous: false,
      providers: [],
      has_password: true,
    password_step_up: false,
    };
    vi.mocked(getCurrentUser).mockResolvedValue(withPassword);
    __testOnly.setState({ status: "signed-in", user: withPassword });
    setSearch("?auth=ok");

    render(<LandingPage theme="light" onThemeChange={() => {}} />);

    expect(await screen.findByText("Welcome to CiteVyn.")).toBeInTheDocument();
    expect(screen.queryByText("Set a password as a backup way to sign in.")).not.toBeInTheDocument();
  });

  it("shows a welcome toast for ?auth=ok and strips the param", async () => {
    setSearch("?auth=ok");
    const replaceSpy = vi.spyOn(window.history, "replaceState");

    render(<LandingPage theme="light" onThemeChange={() => {}} />);

    expect(await screen.findByText("Welcome to CiteVyn.")).toBeInTheDocument();
    expect(replaceSpy).toHaveBeenCalledWith(null, "", "/");
  });

  it("shows a failure toast for ?auth=error and strips the param", async () => {
    setSearch("?auth=error");
    const replaceSpy = vi.spyOn(window.history, "replaceState");

    render(<LandingPage theme="light" onThemeChange={() => {}} />);

    expect(await screen.findByText("Sign-in failed. Try again.")).toBeInTheDocument();
    expect(replaceSpy).toHaveBeenCalledWith(null, "", "/");
  });

  it("a second mount (simulating a refresh) does not re-fire the toast once the param is gone", async () => {
    setSearch(""); // the param has already been stripped by the first mount
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    expect(screen.queryByText("Welcome to CiteVyn.")).not.toBeInTheDocument();
    expect(screen.queryByText("Sign-in failed. Try again.")).not.toBeInTheDocument();
  });

  it("does nothing when there is no auth param at all", async () => {
    setSearch("?foo=bar");
    const replaceSpy = vi.spyOn(window.history, "replaceState");
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    expect(screen.queryByText("Welcome to CiteVyn.")).not.toBeInTheDocument();
    expect(screen.queryByText("Sign-in failed. Try again.")).not.toBeInTheDocument();
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  // ADR-0004 PR 13: the replaceState fix. RED if the cleanup goes back to
  // replaceState(null, "", pathname) -- utm_source would be lost.
  it("strips only its own params: an unrelated query param and the hash survive the cleanup", async () => {
    setSearch("?utm_source=newsletter&auth=ok&campaign=x", "#faq");
    const replaceSpy = vi.spyOn(window.history, "replaceState");
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    expect(await screen.findByText("Welcome to CiteVyn.")).toBeInTheDocument();
    expect(replaceSpy).toHaveBeenCalledWith(null, "", "/?utm_source=newsletter&campaign=x#faq");
  });
});

describe("account-linking return-trip toast (ADR-0004 PR 13)", () => {
  const originalLocation = window.location;

  afterEach(() => {
    // @ts-expect-error -- restoring jsdom's original window.location
    window.location = originalLocation;
  });

  function setSearch(search: string) {
    // @ts-expect-error -- jsdom's window.location is not directly assignable
    delete window.location;
    // @ts-expect-error -- a minimal stand-in is enough
    window.location = { ...originalLocation, search, pathname: "/", hash: "" };
  }

  it("?connect=ok&provider=github toasts success naming the provider and cleans all three params", async () => {
    // RED if the connect family is not read at all, or if `provider`/`reason`
    // are left behind in the URL.
    setSearch("?connect=ok&provider=github");
    const replaceSpy = vi.spyOn(window.history, "replaceState");
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    expect(await screen.findByText("GitHub is now connected to your account.")).toBeInTheDocument();
    expect(replaceSpy).toHaveBeenCalledWith(null, "", "/");
  });

  it("?connect=error&reason=already_linked explains the identity belongs to another account", async () => {
    setSearch("?connect=error&reason=already_linked&provider=google");
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    expect(
      await screen.findByText("Google is already connected to a different CiteVyn account."),
    ).toBeInTheDocument();
  });

  it("?connect=error&reason=session tells the user to sign in again", async () => {
    setSearch("?connect=error&reason=session&provider=github");
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    expect(await screen.findByText(/Sign out, sign in again, then retry/)).toBeInTheDocument();
  });

  it("?connect=error&reason=denied says the connection was cancelled, not that sign-in failed", async () => {
    // Found live: cancelling the provider's consent mid-connect must not
    // read as a failed sign-in to a user who is still signed in.
    setSearch("?connect=error&reason=denied&provider=github");
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    expect(await screen.findByText("Connection cancelled. Nothing was changed.")).toBeInTheDocument();
    expect(screen.queryByText("Sign-in failed. Try again.")).not.toBeInTheDocument();
  });

  it("?connect=error with an unknown reason falls back to a generic retry message", async () => {
    setSearch("?connect=error&reason=provider&provider=github");
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    expect(await screen.findByText("Couldn't connect GitHub. Try again.")).toBeInTheDocument();
  });

  it("an unrecognised provider value is never rendered raw", async () => {
    // `provider` is attacker-controllable via the URL bar.
    setSearch("?connect=ok&provider=%3Cscript%3Ealert(1)%3C%2Fscript%3E");
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    expect(await screen.findByText("Account connected.")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain("<script>alert(1)");
    expect(document.body.textContent).not.toContain("alert(1)");
  });

  it.each(["constructor", "__proto__", "toString"])(
    "?provider=%s does not resolve through Object.prototype (review finding A1)",
    async (key) => {
      // RED if PROVIDER_LABELS goes back to a plain object literal: the
      // inherited member is truthy and its source text ends up in the toast.
      setSearch(`?connect=ok&provider=${key}`);
      render(<LandingPage theme="light" onThemeChange={() => {}} />);
      expect(await screen.findByText("Account connected.")).toBeInTheDocument();
      expect(document.body.textContent).not.toContain("native code");
      cleanup();
    },
  );

  it("auth and connect params are toasted independently, not as one if/else chain", async () => {
    // RED if a shared chain drops one signal when both are present.
    setSearch("?auth=ok&connect=ok&provider=github");
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    expect(await screen.findByText("Welcome to CiteVyn.")).toBeInTheDocument();
    expect(await screen.findByText("GitHub is now connected to your account.")).toBeInTheDocument();
  });

  it("an unrelated query param survives the connect cleanup", async () => {
    setSearch("?utm_source=x&connect=ok&provider=github");
    const replaceSpy = vi.spyOn(window.history, "replaceState");
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    await screen.findByText("GitHub is now connected to your account.");
    expect(replaceSpy).toHaveBeenCalledWith(null, "", "/?utm_source=x");
  });
});
