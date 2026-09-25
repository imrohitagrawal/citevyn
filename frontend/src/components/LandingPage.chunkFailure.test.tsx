import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LandingPage } from "./LandingPage";
import { CHUNK_FAILED_MESSAGE } from "./LazyChunkBoundary";
import { getCurrentUser, isLiveMode } from "../lib/api";
import { __testOnly } from "../lib/authStore";

/**
 * #447, from the reader's side: when a dialog's code cannot be fetched, the
 * page stays, and the reader is TOLD, through the page's own toast. The
 * component tests (AccountMenu / Nudge .chunkFailure) prove containment at
 * each site; this file proves the message actually reaches the screen through
 * the real LandingPage -> Header -> AccountMenu and LandingPage -> PasswordNudge
 * wiring.
 *
 * RED if LandingPage stops passing `onOpenFailed` to either Header or
 * PasswordNudge (no toast), or if a boundary is removed (the page goes blank).
 *
 * SEPARATE FILE ON PURPOSE: AuthModal is mocked to reject for the whole file,
 * and React caches a rejected lazy for the life of the module instance.
 */
vi.mock("./AuthModal", () => {
  throw new Error("Failed to fetch dynamically imported module: /assets/AuthModal-gone.js");
});

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

const PASSWORDLESS = {
  request_id: "req_1",
  user_id: "usr_a",
  email: "a@example.com",
  anonymous: false,
  providers: ["google"],
  has_password: false,
  password_step_up: false,
};

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.mocked(isLiveMode).mockReturnValue(false);
  vi.mocked(getCurrentUser).mockResolvedValue(null);
  __testOnly.setState(__testOnly.initialState);
  __testOnly.resetBootstrapped();
  window.history.replaceState(null, "", "/");
  // The hero typewriter re-renders every 24 ms for as long as the landing is
  // mounted (see LandingPage.authToast.test.tsx, #344). Every wait below is on
  // a DOM insertion, which the MutationObserver in findBy* still sees.
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

async function toastSaysReload() {
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Couldn't open that");
  expect(alert).toHaveTextContent(CHUNK_FAILED_MESSAGE);
  return alert;
}

describe("LandingPage: a dialog that cannot load (#447)", () => {
  it("Sign in: the landing stays, a toast says to reload, and a second click is contained too", async () => {
    const user = userEvent.setup({ delay: null });
    render(<LandingPage theme="light" onThemeChange={() => {}} />);

    await user.click(await screen.findByRole("button", { name: "Sign in" }));
    await toastSaysReload();
    expect(screen.getAllByRole("button", { name: "Try the demo" }).length).toBeGreaterThan(0);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    // Dismiss, then click again: the toast must come back, which also proves
    // the button was not left dead.
    await user.click(within(screen.getByRole("alert")).getByRole("button"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await toastSaysReload();
    expect(screen.getAllByRole("button", { name: "Try the demo" }).length).toBeGreaterThan(0);
  });

  it("the password nudge's 'Set one': the nudge stays and a toast says to reload", async () => {
    window.history.replaceState(null, "", "/?auth=ok");
    vi.mocked(getCurrentUser).mockResolvedValue(PASSWORDLESS);
    __testOnly.setState({ status: "signed-in", user: PASSWORDLESS });
    const user = userEvent.setup({ delay: null });
    render(<LandingPage theme="light" onThemeChange={() => {}} />);

    await user.click(await screen.findByRole("button", { name: "Set one" }));
    const alert = await screen.findByText(CHUNK_FAILED_MESSAGE);
    expect(alert).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set one" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Try the demo" }).length).toBeGreaterThan(0);
  });
});
