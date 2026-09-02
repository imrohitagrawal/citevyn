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

// ADR-0004 PR 14: the modal's magic-link / password calls (their own lazy-only
// module -- see lib/authActions.ts). Mocked at the same layer as the api.
vi.mock("../lib/authActions", () => ({
  requestMagicLink: vi.fn(),
  updatePassword: vi.fn(),
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
      providers: [],
      has_password: true,
    password_step_up: false,
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

describe("AuthModal magic-link mode (ADR-0004 PR 14)", () => {
  it("'Email me a sign-in link' switches to an email-only form and focuses the email field", async () => {
    // RED if the button is removed, if the password field survives the
    // switch, or if the mode-switch effect stops re-focusing the first
    // field (focus would fall to <body>, outside the trap).
    const user = userEvent.setup();
    renderModal();
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Email me a sign-in link" }));

    expect(within(dialog).getByRole("heading", { name: "Email me a sign-in link" })).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Password")).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText("Email")).toHaveFocus();
    expect(within(dialog).getByRole("button", { name: "Send link" })).toBeInTheDocument();
  });

  it("submitting requests a link, shows a role=status notice (not an alert) and keeps the dialog open", async () => {
    // RED if the notice is rendered with role="alert", if the modal closes
    // on success (the user still needs to read "check your email"), or if
    // the email is sent through login() instead of requestMagicLink().
    const { login } = await import("../lib/api");
    const { requestMagicLink } = await import("../lib/authActions");
    vi.mocked(requestMagicLink).mockResolvedValueOnce(undefined);
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderModal(onClose);
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Email me a sign-in link" }));
    await user.type(within(dialog).getByLabelText("Email"), "link@example.com");
    await user.click(within(dialog).getByRole("button", { name: "Send link" }));

    expect(requestMagicLink).toHaveBeenCalledWith("link@example.com");
    expect(login).not.toHaveBeenCalled();
    const status = await within(dialog).findByRole("status");
    expect(status).toHaveTextContent(/sign-in link is on its way/);
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("a 404 (no email provider configured) shows a specific inline error", async () => {
    // RED if the 404 branch is dropped -- the raw "Not found." would be
    // meaningless to a user.
    const { requestMagicLink } = await import("../lib/authActions");
    const { ApiClientError } = await import("../lib/types");
    vi.mocked(requestMagicLink).mockRejectedValueOnce(new ApiClientError("Not found.", 404, "Not found."));
    const user = userEvent.setup();
    renderModal();
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Email me a sign-in link" }));
    await user.type(within(dialog).getByLabelText("Email"), "link@example.com");
    await user.click(within(dialog).getByRole("button", { name: "Send link" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Email sign-in isn't available right now.");
  });

  it("'Back to sign in' returns to the password form", async () => {
    const user = userEvent.setup();
    renderModal();
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Email me a sign-in link" }));
    await user.click(within(dialog).getByText("Back to sign in"));
    expect(within(dialog).getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Password")).toBeInTheDocument();
  });

  it("password fields carry the backend's 128-character ceiling", async () => {
    // RED if maxLength is dropped: the browser would accept what the server
    // rejects with a 422.
    const user = userEvent.setup();
    renderModal();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText("Password")).toHaveAttribute("maxlength", "128");
    await user.click(within(dialog).getByText("Need an account? Register"));
    expect(within(dialog).getByLabelText("Password")).toHaveAttribute("maxlength", "128");
    expect(within(dialog).getByLabelText("Password")).toHaveAttribute("minlength", "8");
  });
});

describe("AuthModal set-password mode (ADR-0004 PR 14)", () => {
  const PASSWORDLESS = {
    request_id: "req_1",
    user_id: "usr_a",
    email: "a@example.com",
    anonymous: false,
    providers: ["github"],
    has_password: false,
  password_step_up: false,
  };
  const WITH_PASSWORD = { ...PASSWORDLESS, has_password: true, password_step_up: false };

  function renderSetPassword(user: typeof PASSWORDLESS, onClose = vi.fn()) {
    __testOnly.setState({ status: "signed-in", user });
    const trigger = document.createElement("button");
    trigger.setAttribute("data-test-trigger", "");
    document.body.appendChild(trigger);
    const triggerRef = createRef<HTMLElement>();
    // @ts-expect-error -- assigning to a ref's .current outside React for the test fixture
    triggerRef.current = trigger;
    render(<AuthModal triggerRef={triggerRef} onClose={onClose} initialMode="set-password" />);
    return { onClose };
  }

  it("a passwordless account gets a 'Set a password' form with only a new-password field", async () => {
    // RED if the form shape is derived from anything but user.has_password.
    const { getCurrentUser } = await import("../lib/api");
    const { updatePassword } = await import("../lib/authActions");
    vi.mocked(getCurrentUser).mockResolvedValue(PASSWORDLESS);
    vi.mocked(updatePassword).mockResolvedValueOnce(undefined);
    const user = userEvent.setup();
    const { onClose } = renderSetPassword(PASSWORDLESS);
    const dialog = screen.getByRole("dialog");

    expect(within(dialog).getByRole("heading", { name: "Set a password" })).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Current password")).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Email")).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Continue with GitHub" })).not.toBeInTheDocument();
    const field = within(dialog).getByLabelText("New password");
    expect(field).toHaveFocus();
    expect(field).toHaveAttribute("maxlength", "128");

    await user.type(field, "new passphrase 12345");
    await user.click(within(dialog).getByRole("button", { name: "Save password" }));

    // No current password at all for a first-time set.
    expect(updatePassword).toHaveBeenCalledWith("new passphrase 12345", undefined);
    expect(await within(dialog).findByRole("status")).toHaveTextContent(/Password saved/);
    expect(onClose).not.toHaveBeenCalled();
    // The success screen keeps focus INSIDE the still-open dialog (the form
    // that held focus unmounted) and does not re-title itself "Change
    // password" now that has_password is true -- both review findings.
    const done = within(dialog).getByRole("button", { name: "Done" });
    expect(done).toHaveFocus();
    expect(within(dialog).getByRole("heading", { name: "Password saved" })).toBeInTheDocument();
    expect(within(dialog).queryByText(/Enter your current password/)).not.toBeInTheDocument();
    await user.click(done);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("an account with a password gets a 'Change password' form that sends both fields", async () => {
    const { getCurrentUser } = await import("../lib/api");
    const { updatePassword } = await import("../lib/authActions");
    vi.mocked(getCurrentUser).mockResolvedValue(WITH_PASSWORD);
    vi.mocked(updatePassword).mockResolvedValueOnce(undefined);
    const user = userEvent.setup();
    renderSetPassword(WITH_PASSWORD);
    const dialog = screen.getByRole("dialog");

    expect(within(dialog).getByRole("heading", { name: "Change password" })).toBeInTheDocument();
    const current = within(dialog).getByLabelText("Current password");
    expect(current).toHaveFocus();
    await user.type(current, "old passphrase");
    await user.type(within(dialog).getByLabelText("New password"), "new passphrase 12345");
    await user.click(within(dialog).getByRole("button", { name: "Save password" }));

    expect(updatePassword).toHaveBeenCalledWith("new passphrase 12345", "old passphrase");
  });

  it("a rejected current password shows the server's message inline and keeps the dialog open", async () => {
    const { getCurrentUser } = await import("../lib/api");
    const { updatePassword } = await import("../lib/authActions");
    const { ApiClientError } = await import("../lib/types");
    vi.mocked(getCurrentUser).mockResolvedValue(WITH_PASSWORD);
    vi.mocked(updatePassword).mockRejectedValueOnce(
      new ApiClientError("Current password is incorrect.", 422, "Current password is incorrect."),
    );
    const user = userEvent.setup();
    const { onClose } = renderSetPassword(WITH_PASSWORD);
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Current password"), "wrong");
    await user.type(within(dialog).getByLabelText("New password"), "new passphrase 12345");
    await user.click(within(dialog).getByRole("button", { name: "Save password" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Current password is incorrect.");
    expect(within(dialog).queryByRole("button", { name: "Done" })).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("AuthModal magic-link step-up (ADR-0004 PR 15, #293)", () => {
  const STEP_UP = {
    request_id: "req_1",
    user_id: "usr_a",
    email: "a@example.com",
    anonymous: false,
    providers: [],
    has_password: true,
    password_step_up: true,
  };

  function renderStepUp(onClose = vi.fn()) {
    __testOnly.setState({ status: "signed-in", user: STEP_UP });
    const trigger = document.createElement("button");
    trigger.setAttribute("data-test-trigger", "");
    document.body.appendChild(trigger);
    const triggerRef = createRef<HTMLElement>();
    // @ts-expect-error -- assigning to a ref's .current outside React for the test fixture
    triggerRef.current = trigger;
    render(<AuthModal triggerRef={triggerRef} onClose={onClose} initialMode="set-password" />);
    return { onClose };
  }

  it("drops the current-password field for a fresh magic-link session and sends only the new one", async () => {
    // RED if the form shape ignores password_step_up: the forgotten-password
    // user would face a required field they cannot fill.
    const { getCurrentUser } = await import("../lib/api");
    const { updatePassword } = await import("../lib/authActions");
    vi.mocked(getCurrentUser).mockResolvedValue(STEP_UP);
    vi.mocked(updatePassword).mockResolvedValueOnce(undefined);
    const user = userEvent.setup();
    renderStepUp();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Set a new password" })).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Current password")).not.toBeInTheDocument();
    expect(within(dialog).getByText(/without the old one/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("New password"), "brand new passphrase");
    await user.click(within(dialog).getByRole("button", { name: "Save password" }));
    expect(updatePassword).toHaveBeenCalledWith("brand new passphrase", undefined);
  });

  it("reveals the current-password field when the server says the window has closed", async () => {
    // RED if the 422 fallback is dropped: a stale /me would leave the user
    // with an error and no field to type into.
    const { getCurrentUser } = await import("../lib/api");
    const { updatePassword } = await import("../lib/authActions");
    const { ApiClientError } = await import("../lib/types");
    vi.mocked(getCurrentUser).mockResolvedValue(STEP_UP);
    vi.mocked(updatePassword).mockRejectedValueOnce(
      new ApiClientError("Enter your current password.", 422, "Enter your current password."),
    );
    const user = userEvent.setup();
    renderStepUp();
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("New password"), "brand new passphrase");
    await user.click(within(dialog).getByRole("button", { name: "Save password" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Enter your current password.");
    const current = within(dialog).getByLabelText("Current password");
    expect(current).toBeInTheDocument();
    expect(current).toHaveFocus();
    expect(within(dialog).getByRole("heading", { name: "Change password" })).toBeInTheDocument();
  });
});
