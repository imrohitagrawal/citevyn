import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { AuthModal } from "./AuthModal";
import { __testOnly } from "../lib/authStore";

// AuthModal renders through useAuth -> authStore -> api.ts. Mock the wire
// layer only; authStore and useAuth run for real, same as authStore.test.ts.
vi.mock("../lib/api", () => ({
  API_BASE_URL: "",
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

afterEach(() => {
  cleanup();
  document.querySelectorAll("[data-test-trigger]").forEach((el) => el.remove());
});

function renderModal(onClose = vi.fn()) {
  const trigger = document.createElement("button");
  trigger.textContent = "Sign in";
  trigger.setAttribute("data-test-trigger", "");
  document.body.appendChild(trigger);
  trigger.focus();
  const triggerRef = createRef<HTMLElement>();
  // @ts-expect-error -- assigning to a ref's .current outside React for the test fixture
  triggerRef.current = trigger;

  const utils = render(<AuthModal triggerRef={triggerRef} onClose={onClose} />);
  return { ...utils, trigger, onClose };
}

describe("AuthModal accessibility", () => {
  it("moves focus into the dialog on open", () => {
    renderModal();
    const emailField = screen.getByLabelText("Email");
    expect(emailField).toHaveFocus();
  });

  it("restores focus to the trigger element on unmount", () => {
    const { trigger, unmount } = renderModal();
    unmount();
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it("Escape calls onClose", async () => {
    const user = userEvent.setup();
    const { onClose } = renderModal();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("traps Tab focus within the dialog — wraps from the last to the first element", async () => {
    const user = userEvent.setup();
    renderModal();
    const dialog = screen.getByRole("dialog");
    const focusable = dialog.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled])',
    );
    const last = focusable[focusable.length - 1];
    last.focus();
    expect(last).toHaveFocus();

    await user.keyboard("{Tab}");
    expect(focusable[0]).toHaveFocus();
  });

  it("traps Shift+Tab from the first element back to the last", async () => {
    const user = userEvent.setup();
    renderModal();
    const dialog = screen.getByRole("dialog");
    const focusable = dialog.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled])',
    );
    // The dialog's first focusable element in DOM order is the Close (×)
    // button, which sits above the form — not the email field, which is
    // only the INITIAL-focus target (a separate concern, covered by "moves
    // focus into the dialog on open" above). The trap's wrap edge is the
    // true DOM-first element.
    const first = focusable[0];
    first.focus();
    expect(first).toHaveFocus();

    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(focusable[focusable.length - 1]).toHaveFocus();
  });

  it("clicking the backdrop calls onClose; clicking inside the dialog does not", async () => {
    const user = userEvent.setup();
    const { onClose } = renderModal();
    await user.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();

    const backdrop = screen.getByRole("presentation");
    await user.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("AuthModal form", () => {
  it("submitting login calls useAuth's signIn with the entered credentials", async () => {
    const { login } = await import("../lib/api");
    vi.mocked(login).mockResolvedValueOnce({
      request_id: "req_1",
      user_id: "usr_a",
      email: "a@example.com",
      anonymous: false,
    });
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderModal(onClose);

    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Email"), "a@example.com");
    await user.type(within(dialog).getByLabelText("Password"), "correct horse battery");
    await user.click(within(dialog).getByRole("button", { name: "Sign in" }));

    expect(login).toHaveBeenCalledWith({ email: "a@example.com", password: "correct horse battery" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("a failed submit shows an inline error and does NOT close the modal", async () => {
    const { login } = await import("../lib/api");
    const { ApiClientError } = await import("../lib/types");
    vi.mocked(login).mockRejectedValueOnce(
      new ApiClientError("Invalid email or password.", 401, "Invalid email or password."),
    );
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderModal(onClose);

    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Email"), "a@example.com");
    await user.type(within(dialog).getByLabelText("Password"), "wrong password");
    await user.click(within(dialog).getByRole("button", { name: "Sign in" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Invalid email or password.");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("toggling to register mode changes the heading and submit label", async () => {
    const user = userEvent.setup();
    renderModal();
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByText("Need an account? Register"));
    expect(within(dialog).getByRole("heading", { name: "Create an account" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Create account" })).toBeInTheDocument();
  });
});

describe("AuthModal OAuth buttons (ADR-0004 PR 12)", () => {
  const originalLocation = window.location;

  beforeEach(async () => {
    // @ts-expect-error -- jsdom's window.location is not directly assignable
    delete window.location;
    // @ts-expect-error -- a minimal stand-in is enough; only .href is read
    window.location = { ...originalLocation, href: "" };
    const { login } = await import("../lib/api");
    vi.mocked(login).mockClear();
  });

  afterEach(() => {
    // @ts-expect-error -- restoring jsdom's original window.location
    window.location = originalLocation;
  });

  it("clicking 'Continue with GitHub' is a real navigation, not an apiFetch call", async () => {
    const { login } = await import("../lib/api");
    const user = userEvent.setup();
    renderModal();
    const dialog = screen.getByRole("dialog");

    await user.click(within(dialog).getByRole("button", { name: "Continue with GitHub" }));

    expect(window.location.href).toMatch(/\/v1\/auth\/oauth\/github\/start$/);
    expect(login).not.toHaveBeenCalled();
  });

  it("clicking 'Continue with Google' is a real navigation, not an apiFetch call", async () => {
    const { login } = await import("../lib/api");
    const user = userEvent.setup();
    renderModal();
    const dialog = screen.getByRole("dialog");

    await user.click(within(dialog).getByRole("button", { name: "Continue with Google" }));

    expect(window.location.href).toMatch(/\/v1\/auth\/oauth\/google\/start$/);
    expect(login).not.toHaveBeenCalled();
  });
});
