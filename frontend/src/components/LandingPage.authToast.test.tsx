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

async function openAuthModalAndSignIn(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Sign in" }));
  const dialog = await screen.findByRole("dialog");
  await user.type(within(dialog).getByLabelText("Email"), "claim@example.com");
  await user.type(within(dialog).getByLabelText("Password"), "correct horse battery");
  await user.click(within(dialog).getByRole("button", { name: "Sign in" }));
}

describe("sign-in confirms claimed chat history (ADR-0004 PR 9)", () => {
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
    const user = userEvent.setup();
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
    const user = userEvent.setup();
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
