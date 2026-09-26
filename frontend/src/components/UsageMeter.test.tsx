/**
 * ADR-0005 Phase 5B-2: the usage meter under the chat composer ("n of N
 * left"). Each test says which change turns it red.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import UsageMeter from "./UsageMeter";
import { getMembership, openPortal, startCheckout } from "../lib/billing";

vi.mock("../lib/billing", () => ({
  getMembership: vi.fn(),
  startCheckout: vi.fn(() => Promise.resolve()),
  openPortal: vi.fn(() => Promise.resolve()),
}));

function membership(usage: Record<string, unknown>) {
  return { tier: usage.kind === "monthly" ? "pro" : "free", usage } as unknown as Awaited<
    ReturnType<typeof getMembership>
  >;
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("UsageMeter", () => {
  it("shows the free questions left, with an upgrade button", async () => {
    // Turns red if: remaining/limit are not shown or the upgrade is missing.
    vi.mocked(getMembership).mockResolvedValue(
      membership({ kind: "trial", used: 3, limit: 25, remaining: 22, resets_at: null, verified: true }),
    );
    render(<UsageMeter answered={0} />);
    expect(await screen.findByText("22 of 25 free questions left")).toBeTruthy();
    await userEvent.setup().click(screen.getByRole("button", { name: "Upgrade" }));
    expect(startCheckout).toHaveBeenCalledWith("month");
  });

  it("shows Pro's month without an upgrade button", async () => {
    // Turns red if: Pro is told to upgrade, or the month is not named.
    vi.mocked(getMembership).mockResolvedValue(
      membership({ kind: "monthly", used: 10, limit: 1000, remaining: 990, resets_at: "2026-11-01T00:00:00+00:00", verified: false }),
    );
    render(<UsageMeter answered={0} />);
    expect(await screen.findByText("990 of 1,000 questions left this month")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Upgrade" })).toBeNull();
  });

  it("tells an unverified account to verify first", async () => {
    // Turns red if: an unverified account is shown "0 of 0 left".
    vi.mocked(getMembership).mockResolvedValue(
      membership({ kind: "trial", used: 0, limit: 0, remaining: 0, resets_at: null, verified: false }),
    );
    render(<UsageMeter answered={0} />);
    expect(await screen.findByText(/verify your email/i)).toBeTruthy();
  });

  it("refreshes after each answer", async () => {
    // Turns red if: the count goes stale after asking.
    vi.mocked(getMembership)
      .mockResolvedValueOnce(membership({ kind: "trial", used: 3, limit: 25, remaining: 22, resets_at: null, verified: true }))
      .mockResolvedValueOnce(membership({ kind: "trial", used: 4, limit: 25, remaining: 21, resets_at: null, verified: true }));
    const { rerender } = render(<UsageMeter answered={0} />);
    await screen.findByText("22 of 25 free questions left");
    rerender(<UsageMeter answered={1} />);
    expect(await screen.findByText("21 of 25 free questions left")).toBeTruthy();
  });

  it("renders nothing if the membership cannot be read", async () => {
    // Turns red if: a failed read crashes the chat or shows wrong numbers.
    vi.mocked(getMembership).mockRejectedValue(new Error("down"));
    const { container } = render(<UsageMeter answered={0} />);
    await new Promise((r) => setTimeout(r, 0));
    expect(getMembership).toHaveBeenCalled(); // partner: it did try to read
    expect(container.textContent).toBe("");
  });
});


it("a failed upgrade from the meter is said plainly", async () => {
  // Found in review: the rejection was unhandled and the click did nothing
  // visible. Turns red if: the failure is swallowed.
  vi.mocked(getMembership).mockResolvedValue(
    membership({ kind: "trial", used: 3, limit: 25, remaining: 22, resets_at: null, verified: true }),
  );
  vi.mocked(startCheckout).mockRejectedValueOnce(new Error("down"));
  render(<UsageMeter answered={0} />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "Upgrade" }));
  expect((await screen.findByRole("alert")).textContent).toMatch(/couldn't open billing/i);
});


it("gives a Pro account a way to manage or cancel billing", async () => {
  // Found while drafting the Refund Policy: the app had no way for a paying
  // user to reach the billing portal to cancel. Turns red if: Pro has no
  // Manage billing button, or it does not open the portal.
  vi.mocked(getMembership).mockResolvedValue(
    membership({ kind: "monthly", used: 1, limit: 1000, remaining: 999, resets_at: null, verified: true }),
  );
  render(<UsageMeter answered={0} />);
  await userEvent.setup().click(await screen.findByRole("button", { name: "Manage billing" }));
  expect(openPortal).toHaveBeenCalled();
});

it("partner: a trial account is not offered Manage billing", async () => {
  // Turns red if: the portal button shows for an account with no subscription.
  vi.mocked(getMembership).mockResolvedValue(
    membership({ kind: "trial", used: 1, limit: 25, remaining: 24, resets_at: null, verified: true }),
  );
  render(<UsageMeter answered={0} />);
  await screen.findByText("24 of 25 free questions left");
  expect(screen.queryByRole("button", { name: "Manage billing" })).toBeNull();
});
