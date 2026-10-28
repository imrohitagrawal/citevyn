/**
 * #546 PR 2: the "Delete account" dialog. The server decides whether this
 * session is freshly confirmed by email (`password_step_up`); the dialog only
 * chooses what to show, and never deletes without that confirmation. Each test
 * says which change turns it red.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DeleteAccountDialog, { DELETION_CONTACT } from "./DeleteAccountDialog";
import { deleteMyAccount, requestMagicLink } from "../lib/authActions";
import { signedOutLocally } from "../lib/authStore";
import { getCurrentUser } from "../lib/api";
import { ApiClientError } from "../lib/types";

vi.mock("../lib/authActions", () => ({
  deleteMyAccount: vi.fn(),
  requestMagicLink: vi.fn(),
}));
vi.mock("../lib/authStore", () => ({ signedOutLocally: vi.fn() }));
vi.mock("../lib/api", () => ({ getCurrentUser: vi.fn(() => Promise.resolve(null)) }));

const USER = {
  request_id: "req_1",
  user_id: "usr_a",
  email: "a@example.com",
  anonymous: false,
  providers: [],
  has_password: true,
  password_step_up: false,
};

function open(user: Omit<Partial<typeof USER>, "email"> & { email?: string | null } = {}) {
  const onDeleted = vi.fn();
  const onClose = vi.fn();
  const trigger = { current: document.createElement("button") };
  render(
    <DeleteAccountDialog
      user={{ ...USER, ...user }}
      triggerRef={trigger}
      onClose={onClose}
      onDeleted={onDeleted}
    />,
  );
  return { onDeleted, onClose };
}

function apiError(status: number, code: string, message = "server says") {
  return new ApiClientError(message, status, {
    request_id: "r",
    status: "error",
    error: { code, message, details: null },
  } as never);
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  vi.mocked(getCurrentUser).mockResolvedValue(null);
});

describe("DeleteAccountDialog", () => {
  it("asks for an email confirmation first and offers no delete button", async () => {
    // Turns red if: a session that is not freshly confirmed sees the delete button.
    open();
    expect(screen.getByRole("button", { name: "Email me a link" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete my account" })).toBeNull();
  });

  it("emails a link to the account's own address", async () => {
    // Turns red if: the link goes nowhere, or to a typed address.
    vi.mocked(requestMagicLink).mockResolvedValue();
    open();
    await userEvent.setup().click(screen.getByRole("button", { name: "Email me a link" }));
    expect(requestMagicLink).toHaveBeenCalledWith("a@example.com");
    expect(await screen.findByRole("status")).toHaveProperty(
      "textContent",
      expect.stringContaining("We sent a sign-in link to a@example.com"),
    );
  });

  it("explains a rate-limited link request plainly", async () => {
    // Turns red if: a 429 shows a generic failure.
    vi.mocked(requestMagicLink).mockRejectedValue(apiError(429, "rate_limited"));
    open();
    await userEvent.setup().click(screen.getByRole("button", { name: "Email me a link" }));
    expect((await screen.findByRole("alert")).textContent).toContain("sent moments ago");
  });

  it("deletes a confirmed account, confirms it, then signs out", async () => {
    // Turns red if: the delete call, the confirmation or the local sign-out is
    // dropped, or they run in the other order (the confirmation is meant to be
    // queued while the person is still shown as signed in).
    const order: string[] = [];
    vi.mocked(deleteMyAccount).mockResolvedValue();
    vi.mocked(signedOutLocally).mockImplementation(() => order.push("signed-out"));
    const { onDeleted } = open({ password_step_up: true });
    onDeleted.mockImplementation(() => order.push("confirmed"));
    await userEvent.setup().click(screen.getByRole("button", { name: "Delete my account" }));
    expect(deleteMyAccount).toHaveBeenCalledTimes(1);
    expect(order).toEqual(["confirmed", "signed-out"]);
  });

  it("goes back to the email step when the confirmation has run out", async () => {
    // Turns red if: a 403 verification_required is shown as a dead end.
    vi.mocked(deleteMyAccount).mockRejectedValue(apiError(403, "verification_required"));
    const { onDeleted } = open({ password_step_up: true });
    await userEvent.setup().click(screen.getByRole("button", { name: "Delete my account" }));
    expect((await screen.findByRole("alert")).textContent).toContain("run out");
    expect(screen.getByRole("button", { name: "Email me a link" })).toBeTruthy();
    expect(onDeleted).not.toHaveBeenCalled();
    expect(signedOutLocally).not.toHaveBeenCalled();
  });

  it("shows the server's 'cancel your subscription first' message", async () => {
    // Turns red if: a live-subscription refusal reads like a generic error.
    const message = "Your subscription can still bill. Cancel it first.";
    vi.mocked(deleteMyAccount).mockRejectedValue(apiError(409, "already_subscribed", message));
    const { onDeleted } = open({ password_step_up: true });
    await userEvent.setup().click(screen.getByRole("button", { name: "Delete my account" }));
    expect((await screen.findByRole("alert")).textContent).toBe(message);
    expect(onDeleted).not.toHaveBeenCalled();
  });

  it("says nothing was deleted when the server refused before writing", async () => {
    // Turns red if: a known no-write refusal (Redis down) reads as uncertain.
    vi.mocked(deleteMyAccount).mockRejectedValue(apiError(503, "rate_limiter_unavailable"));
    open({ password_step_up: true });
    await userEvent.setup().click(screen.getByRole("button", { name: "Delete my account" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Nothing was deleted");
    expect(signedOutLocally).not.toHaveBeenCalled();
  });

  it("does not claim nothing was deleted after an unknown failure", async () => {
    // Review finding: a 500 can come after the deletion committed. Turns red
    // if: an unknown failure says "Nothing was deleted".
    vi.mocked(deleteMyAccount).mockRejectedValue(apiError(500, "internal_error"));
    open({ password_step_up: true });
    await userEvent.setup().click(screen.getByRole("button", { name: "Delete my account" }));
    const text = (await screen.findByRole("alert")).textContent ?? "";
    expect(text).toContain("could not confirm");
    expect(text).not.toContain("Nothing was deleted");
  });

  it("re-reads the confirmation from the server, so the original tab works", async () => {
    // Review finding: the emailed link signs in a NEW tab; this tab loaded
    // password_step_up=false before that. Turns red if: the dialog trusts
    // only the identity it was opened with.
    vi.mocked(getCurrentUser).mockResolvedValue({ ...USER, password_step_up: true });
    open({ password_step_up: false });
    expect(await screen.findByRole("button", { name: "Delete my account" })).toBeTruthy();
  });

  it("sends one delete for a double click", async () => {
    // Turns red if: the in-flight guard is dropped.
    let finish: () => void = () => {};
    vi.mocked(deleteMyAccount).mockImplementation(() => new Promise<void>((r) => (finish = r)));
    open({ password_step_up: true });
    const user = userEvent.setup();
    const button = screen.getByRole("button", { name: "Delete my account" });
    await user.click(button);
    await user.click(screen.getByRole("button", { name: "Deleting…" }));
    expect(deleteMyAccount).toHaveBeenCalledTimes(1);
    finish();
  });

  it("tells an account with no address to email us, with no button", async () => {
    // Turns red if: an address-less account is offered a link it cannot receive.
    open({ email: null, password_step_up: true });
    expect(screen.getByText(new RegExp(DELETION_CONTACT))).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Email me a link" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete my account" })).toBeNull();
  });
});
