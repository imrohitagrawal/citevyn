import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PasswordNudge } from "./Nudge";
import { __testOnly } from "../lib/authStore";

/**
 * #447: the nudge's "Set one" lazy-loads AuthModal. When that chunk never
 * arrives, the rejected import used to unmount the whole React root.
 *
 * SEPARATE FILE ON PURPOSE: AuthModal is mocked to reject for the whole file,
 * and React caches a rejected lazy for the life of the module instance.
 *
 * RED if the LazyChunkBoundary around the nudge's AuthModal Suspense is
 * removed (the sibling vanishes with the root), or if its onError stops
 * resetting the open state / reporting through onOpenFailed.
 */
vi.mock("./AuthModal", () => {
  throw new Error("Failed to fetch dynamically imported module: /assets/AuthModal-gone.js");
});

const PASSWORDLESS = {
  request_id: "req_1",
  user_id: "usr_a",
  email: "a@example.com",
  anonymous: false,
  providers: ["google"],
  has_password: false,
  password_step_up: false,
};

vi.mock("../lib/api", () => ({
  API_BASE_URL: "",
  getCurrentUser: vi.fn(() => Promise.resolve(PASSWORDLESS)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
}));

let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  __testOnly.setState({ status: "signed-in", user: PASSWORDLESS });
  __testOnly.resetBootstrapped();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("PasswordNudge: the password dialog's chunk fails to load (#447)", () => {
  it("keeps the page and the card, leaves 'Set one' usable, and reports the failure", async () => {
    const onOpenFailed = vi.fn();
    const user = userEvent.setup();
    render(
      <div>
        <p>the transcript so far</p>
        <PasswordNudge onOpenFailed={onOpenFailed} />
      </div>,
    );

    await user.click(await screen.findByRole("button", { name: "Set one" }));
    await waitFor(() => expect(errorSpy).toHaveBeenCalled());
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("status")).toHaveTextContent("Add a password?");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      errorSpy.mock.calls.some((c: unknown[]) => String(c[0]).includes("[auth-modal]")),
    ).toBe(true);

    await user.click(screen.getByRole("button", { name: "Set one" }));
    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledTimes(2));
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set one" })).toBeEnabled();
  });
});
