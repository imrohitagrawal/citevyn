import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { LandingPage } from "./LandingPage";
import { getCurrentUser, isLiveMode } from "../lib/api";
import { __testOnly } from "../lib/authStore";

/**
 * #447: the password nudge is itself a lazy chunk, mounted on its own after an
 * email-link or OAuth sign-in (`?auth=ok`). No click is involved, so like the
 * #358 strip it must fail QUIETLY: the reader loses an optional prompt, not the
 * page.
 *
 * RED if the LazyChunkBoundary around PasswordNudge's Suspense in
 * LandingPage.tsx is removed: the rejected import unmounts the whole root.
 *
 * SEPARATE FILE ON PURPOSE: ./Nudge is mocked to reject for the whole file.
 */
vi.mock("./Nudge", () => {
  throw new Error("Failed to fetch dynamically imported module: /assets/Nudge-gone.js");
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

let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  vi.mocked(isLiveMode).mockReturnValue(false);
  vi.mocked(getCurrentUser).mockResolvedValue(PASSWORDLESS);
  __testOnly.setState({ status: "signed-in", user: PASSWORDLESS });
  __testOnly.resetBootstrapped();
  window.history.replaceState(null, "", "/?auth=ok");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("LandingPage: the password nudge's own chunk fails (#447)", () => {
  it("keeps the landing page and logs the failure", async () => {
    render(<LandingPage theme="light" onThemeChange={() => {}} />);

    // Partner to "the page survived": the chunk really did fail, and the
    // boundary (not something else) caught it.
    await waitFor(() =>
      expect(
        errorSpy.mock.calls.some((c: unknown[]) => String(c[0]).includes("[password-nudge]")),
      ).toBe(true),
    );
    expect(screen.getAllByRole("button", { name: "Try the demo" }).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Set one" })).not.toBeInTheDocument();
  });
});
