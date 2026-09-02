import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { ConnectedAccountsDrawer } from "./ConnectedAccountsDrawer";
import { __testOnly } from "../lib/authStore";

/**
 * ADR-0004 PR 13. Each test names the change that turns it red.
 */
vi.mock("../lib/api", () => ({
  API_BASE_URL: "http://api.test",
  // The password row lazy-loads AuthModal, whose useAuth() bootstraps identity.
  getCurrentUser: vi.fn(() => Promise.resolve(null)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
}));

const originalLocation = window.location;

beforeEach(() => {
  __testOnly.setState(__testOnly.initialState);
  __testOnly.resetBootstrapped();
  // @ts-expect-error -- jsdom's window.location is not directly assignable
  delete window.location;
  // @ts-expect-error -- a minimal stand-in; only .href is written
  window.location = { ...originalLocation, href: "" };
});

afterEach(() => {
  cleanup();
  // @ts-expect-error -- restoring jsdom's original window.location
  window.location = originalLocation;
});

function renderDrawer(providers: string[], onClose = vi.fn(), hasPassword = true) {
  const trigger = document.createElement("button");
  trigger.textContent = "Sign-in methods";
  document.body.appendChild(trigger);
  trigger.focus();
  const triggerRef = createRef<HTMLElement>();
  // @ts-expect-error -- assigning to a ref's .current outside React for the test fixture
  triggerRef.current = trigger;
  const utils = render(
    <ConnectedAccountsDrawer
      triggerRef={triggerRef}
      onClose={onClose}
      user={{ providers, has_password: hasPassword }}
    />,
  );
  return { ...utils, trigger, onClose };
}

describe("ConnectedAccountsDrawer", () => {
  it("'Connect GitHub' is a real navigation to the connect/start route, not an apiFetch call", async () => {
    // RED if the button targets .../oauth/github/start (the LOGIN route) or
    // uses apiFetch -- the mock exports no apiFetch, so a fetch would throw.
    const user = userEvent.setup();
    renderDrawer([]);
    await user.click(screen.getByRole("button", { name: "Connect GitHub" }));
    expect(window.location.href).toBe("http://api.test/v1/auth/oauth/github/connect/start");
  });

  it("'Connect Google' navigates to Google's connect/start route", async () => {
    const user = userEvent.setup();
    renderDrawer([]);
    await user.click(screen.getByRole("button", { name: "Connect Google" }));
    expect(window.location.href).toBe("http://api.test/v1/auth/oauth/google/connect/start");
  });

  it("shows 'Connected ✓' instead of a button for an already-linked provider", () => {
    // RED if the row ignores `providers` and always renders the button.
    renderDrawer(["github"]);
    expect(screen.queryByRole("button", { name: "Connect GitHub" })).not.toBeInTheDocument();
    expect(screen.getByText("Connected ✓")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Google" })).toBeInTheDocument();
  });

  it("shows both as connected when both are linked", () => {
    renderDrawer(["github", "google"]);
    expect(screen.queryAllByRole("button", { name: /^Connect (GitHub|Google)$/ })).toHaveLength(0);
    expect(screen.getAllByText("Connected ✓")).toHaveLength(2);
  });

  it("takes focus on open, Escape closes, and focus returns to the trigger on unmount", async () => {
    // RED if the mount effect stops calling dialogRef.current?.focus() (focus
    // would stay on the trigger, making the restore assertion vacuous -- the
    // review caught exactly that in an earlier version of this test), or if
    // the unmount cleanup stops calling trigger?.focus().
    const user = userEvent.setup();
    const { trigger, onClose, unmount } = renderDrawer([]);
    expect(screen.getByRole("dialog")).toContainElement(document.activeElement as HTMLElement);
    expect(trigger).not.toHaveFocus();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    unmount();
    expect(trigger).toHaveFocus();
  });

  it("clicking the backdrop closes; clicking inside the dialog does not", async () => {
    const user = userEvent.setup();
    const { onClose } = renderDrawer([]);
    await user.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();
    await user.click(screen.getByRole("presentation"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("ConnectedAccountsDrawer password row (ADR-0004 PR 14)", () => {
  it("offers 'Set a password' to a passwordless account and opens the modal in set-password mode", async () => {
    // RED if the row ignores hasPassword, or if the button opens the modal in
    // its default login mode.
    const user = userEvent.setup();
    renderDrawer([], vi.fn(), false);
    expect(screen.queryByText("Set ✓")).not.toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Set a password" });
    expect(button).toHaveAttribute("aria-haspopup", "dialog");
    await user.click(button);
    const modal = await screen.findByRole("dialog", { name: "Set a password" });
    expect(within(modal).getByLabelText("New password")).toBeInTheDocument();
  });

  it("offers 'Change password' to an account that already has one", () => {
    renderDrawer([], vi.fn(), true);
    expect(screen.getByText("Set ✓")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Change password" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set a password" })).not.toBeInTheDocument();
  });

  it("Escape closes the password modal without also closing the drawer underneath", async () => {
    // RED if the drawer's Escape handler stays registered while the modal is
    // open: one keypress would close both.
    const user = userEvent.setup();
    const { onClose } = renderDrawer([], vi.fn(), false);
    await user.click(screen.getByRole("button", { name: "Set a password" }));
    await screen.findByRole("dialog", { name: "Set a password" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Set a password" })).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Sign-in methods" })).toBeInTheDocument();
  });
});
