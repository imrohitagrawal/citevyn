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

  function setSearch(search: string) {
    // @ts-expect-error -- jsdom's window.location is not directly assignable
    delete window.location;
    // @ts-expect-error -- a minimal stand-in is enough; only .search/.pathname are read
    window.location = { ...originalLocation, search, pathname: "/" };
  }

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
});
