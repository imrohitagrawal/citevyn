import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LandingPage } from "./LandingPage";
import { deleteMyAccount } from "../lib/authActions";
import { __testOnly } from "../lib/authStore";

/**
 * #546 PR 2: deleting the account from the menu, wired end to end in the page.
 * The menu drops to "Sign in" as soon as the account is gone, so the
 * confirmation is a page toast, and focus lands on the new "Sign in" button
 * rather than on a button that no longer exists (review findings: neither was
 * tested). The setInterval fake is the one LandingPage.authToast.test.tsx
 * explains (#344).
 */
const SIGNED_IN = {
  request_id: "req_1",
  user_id: "usr_a",
  email: "a@example.com",
  anonymous: false,
  providers: [],
  has_password: true,
  password_step_up: true,
};

vi.mock("../lib/api", () => ({
  isLiveMode: vi.fn(() => false),
  createSession: vi.fn(),
  askQuestion: vi.fn(),
  getCurrentUser: vi.fn(() => Promise.resolve(SIGNED_IN)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
}));
vi.mock("../lib/authActions", () => ({
  deleteMyAccount: vi.fn(() => Promise.resolve()),
  requestMagicLink: vi.fn(),
  updatePassword: vi.fn(),
}));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval"] });
  __testOnly.setState({ status: "signed-in", user: SIGNED_IN });
  __testOnly.resetBootstrapped();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Delete account, wired through the page", () => {
  it("confirms with a toast and moves focus to Sign in", async () => {
    // Turns red if: AccountMenu stops calling onAccountDeleted, Header or
    // LandingPage stops passing it, the toast copy is lost, or focus is left
    // on the account button that the sign-out removed.
    const user = userEvent.setup({ delay: null });
    render(<LandingPage theme="light" onThemeChange={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "a@example.com" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete account" }));
    await user.click(await screen.findByRole("button", { name: "Delete my account" }));
    expect(deleteMyAccount).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Account deleted")).toBeInTheDocument();
    const signIn = await screen.findByRole("button", { name: "Sign in" });
    expect(document.activeElement).toBe(signIn);
  });
});
