import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { ConnectedAccountsDrawer } from "./ConnectedAccountsDrawer";
import { CHUNK_FAILED_MESSAGE } from "./LazyChunkBoundary";
import { __testOnly } from "../lib/authStore";

/**
 * #447: the drawer's "Set a password" button lazy-loads AuthModal. When that
 * chunk never arrives (a deploy replaced it under an open tab), the rejected
 * import used to unmount the whole React root.
 *
 * SEPARATE FILE ON PURPOSE: AuthModal is mocked to reject for the whole file,
 * and React caches a rejected lazy for the life of the module instance.
 *
 * RED if the LazyChunkBoundary around the drawer's AuthModal Suspense is
 * removed (the sibling and the drawer vanish with the root), or if its onError
 * stops closing the modal state / showing the message.
 *
 * The message is shown INSIDE the drawer, not as a toast: the toast stack sits
 * under the drawer's backdrop and behind its panel.
 */
vi.mock("./AuthModal", () => {
  throw new Error("Failed to fetch dynamically imported module: /assets/AuthModal-gone.js");
});

vi.mock("../lib/api", () => ({
  API_BASE_URL: "http://api.test",
  getCurrentUser: vi.fn(() => Promise.resolve(null)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
}));

let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  __testOnly.setState(__testOnly.initialState);
  __testOnly.resetBootstrapped();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ConnectedAccountsDrawer: the password dialog's chunk fails to load (#447)", () => {
  it("keeps the page and the drawer, leaves the button usable, and tells the reader", async () => {
    const onClose = vi.fn();
    const triggerRef = createRef<HTMLElement>();
    const user = userEvent.setup();
    render(
      <div>
        <p>the transcript so far</p>
        <ConnectedAccountsDrawer
          triggerRef={triggerRef}
          onClose={onClose}
          user={{ providers: [], has_password: false }}
        />
      </div>,
    );

    await user.click(screen.getByRole("button", { name: "Set a password" }));
    await waitFor(() => expect(errorSpy).toHaveBeenCalled());
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
    // The partner to "no dialog": the reader is told, in the drawer.
    expect(await screen.findByRole("alert")).toHaveTextContent(CHUNK_FAILED_MESSAGE);
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
    // The drawer itself is untouched: only the dialog it tried to open is lost.
    expect(screen.getByRole("dialog", { name: "Sign-in methods" })).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(
      errorSpy.mock.calls.some((c: unknown[]) => String(c[0]).includes("[auth-modal]")),
    ).toBe(true);
    // Focus goes back to the button that was clicked, inside the drawer.
    expect(screen.getByRole("button", { name: "Set a password" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Set a password" }));
    // Contained again: the second rejection is logged by the boundary again.
    await waitFor(() =>
      expect(
        errorSpy.mock.calls.filter((c: unknown[]) => String(c[0]).includes("[auth-modal]")).length,
      ).toBeGreaterThanOrEqual(2),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(CHUNK_FAILED_MESSAGE);
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Sign-in methods" })).toBeInTheDocument();
  });
});
