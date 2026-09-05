import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { ConnectedAccountsDrawer } from "./ConnectedAccountsDrawer";
import { __testOnly } from "../lib/authStore";

/**
 * SEPARATE FILE ON PURPOSE. `AuthModal` is `React.lazy`, and React caches the
 * resolved module for the lifetime of the module instance — so once any test in
 * a file has awaited the modal, every later test in that file mounts it
 * synchronously and the cold window is unreachable. Vitest isolates modules per
 * FILE, so these tests get a genuinely unresolved lazy component. (Proven: with
 * them appended to ConnectedAccountsDrawer.test.tsx instead, the password field
 * was already in the document and the precondition below failed.)
 */
vi.mock("../lib/api", () => ({
  API_BASE_URL: "http://api.test",
  getCurrentUser: vi.fn(() => Promise.resolve(null)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
}));

beforeEach(() => {
  __testOnly.setState(__testOnly.initialState);
  __testOnly.resetBootstrapped();
});
afterEach(cleanup);

function renderDrawer(onClose = vi.fn()) {
  const trigger = document.createElement("button");
  trigger.textContent = "Sign-in methods";
  document.body.appendChild(trigger);
  trigger.focus();
  const triggerRef = createRef<HTMLElement>();
  // @ts-expect-error -- assigning to a ref's .current outside React for the test fixture
  triggerRef.current = trigger;
  render(
    <ConnectedAccountsDrawer
      triggerRef={triggerRef}
      onClose={onClose}
      user={{ providers: [], has_password: false }}
    />,
  );
  return { trigger, onClose };
}

/**
 * THE COLD WINDOW (#331, found in review).
 *
 * `AuthModal` is `React.lazy`, so the FIRST "Set a password" of a page load
 * flips `passwordOpen` while the chunk is still being fetched. The first design
 * passed `enabled: !passwordOpen` to the trap, which therefore stood the drawer
 * down at that instant — with nothing yet mounted in its place. Measured in
 * that window: a Tab dispatched from a page button behind the backdrop was NOT
 * prevented, focus stayed outside, and Escape did nothing.
 *
 * These tests deliberately do NOT await the lazy resolution: `fireEvent.click`
 * is synchronous, so the assertions run inside the gap. The existing stacking
 * test awaits `findByRole`, which is precisely why it could not see this.
 */
describe("ConnectedAccountsDrawer cold-start window (#331)", () => {
  it("still traps Tab while the lazy password modal is loading", async () => {
    const { trigger } = renderDrawer();
    // Synchronous: the modal is requested but its chunk has not resolved.
    fireEvent.click(screen.getByRole("button", { name: /set a password/i }));
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();

    trigger.focus();
    expect(trigger).toHaveFocus();
    const event = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    document.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
    expect(
      screen.getByRole("dialog", { name: "Sign-in methods" }).contains(document.activeElement),
    ).toBe(true);
    trigger.remove();
  });

  it("still closes on Escape while the lazy password modal is loading", async () => {
    const onClose = vi.fn();
    const { trigger } = renderDrawer(onClose);
    fireEvent.click(screen.getByRole("button", { name: /set a password/i }));
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    trigger.remove();
  });
});
