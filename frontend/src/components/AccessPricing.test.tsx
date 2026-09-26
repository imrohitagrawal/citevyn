/**
 * ADR-0005 Phase 5B-2: the pricing section with the access model on. Free /
 * Pro ($9 a month or $91 a year) / Team (coming soon), the allowance numbers
 * from the server's settings, and working upgrade buttons. Each test says which
 * change turns it red.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AccessPricing from "./AccessPricing";
import { startCheckout } from "../lib/billing";
import { getAuthSnapshot, subscribeAuth } from "../lib/authStore";
import { ApiClientError } from "../lib/types";

vi.mock("../lib/billing", () => ({ startCheckout: vi.fn(() => Promise.resolve()) }));
// A STABLE snapshot, like the real store: useSyncExternalStore loops forever
// if every read returns a new object.
const ANON = { status: "anonymous", user: null };
const SIGNED_IN = { status: "signed-in", user: { user_id: "usr_1", email: "a@x.com" } };
vi.mock("../lib/authStore", () => ({
  getAuthSnapshot: vi.fn(() => ANON),
  subscribeAuth: vi.fn(() => () => {}),
}));
vi.mock("./AuthModal", () => ({
  default: ({ initialMode }: { initialMode?: string }) => <div role="dialog">auth:{initialMode}</div>,
}));

const ALLOWANCE = { free_trial_answers: 25, pro_monthly_answers: 1000 };

function signedIn() {
  vi.mocked(getAuthSnapshot).mockReturnValue(SIGNED_IN as ReturnType<typeof getAuthSnapshot>);
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  vi.mocked(getAuthSnapshot).mockReturnValue(ANON as ReturnType<typeof getAuthSnapshot>);
  vi.mocked(subscribeAuth).mockReturnValue(() => {});
});

describe("AccessPricing", () => {
  it("shows the three plans with the numbers from the server's settings", () => {
    // Turns red if: 25 or 1,000 is hard-coded, or a plan is missing.
    render(<AccessPricing allowance={{ free_trial_answers: 7, pro_monthly_answers: 300 }} onOpenChat={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Free" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Pro" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Team" })).toBeTruthy();
    expect(screen.getByText(/7 answered questions/)).toBeTruthy();
    expect(screen.getByText(/300 answered questions a month/)).toBeTruthy();
    expect(screen.getByText(/refusals never count/i)).toBeTruthy();
  });

  it("shows $9 a month, and $91 a year after choosing yearly", async () => {
    // Turns red if: the toggle does not change the price, or the prices change.
    const user = userEvent.setup();
    render(<AccessPricing allowance={ALLOWANCE} onOpenChat={vi.fn()} />);
    const monthly = screen.getByRole("button", { name: "Monthly" });
    expect(monthly.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText("$9")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Yearly" }));
    expect(screen.getByText("$91")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Yearly" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("the Team plan is not buyable", () => {
    // Turns red if: Team gets a working button (teams are designed, not built).
    render(<AccessPricing allowance={ALLOWANCE} onOpenChat={vi.fn()} />);
    const team = screen.getByRole("heading", { name: "Team" }).closest("div")!;
    expect(within(team).getByRole("button", { name: /coming soon/i }).hasAttribute("disabled")).toBe(true);
  });

  it("signed out, Upgrade opens registration instead of checkout", async () => {
    // Turns red if: an anonymous visitor is sent to checkout (the server would
    // refuse: checkout needs an account).
    const user = userEvent.setup();
    render(<AccessPricing allowance={ALLOWANCE} onOpenChat={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Upgrade to Pro" }));
    expect(await screen.findByText("auth:register")).toBeTruthy();
    expect(startCheckout).not.toHaveBeenCalled();
  });

  it("signed in, Upgrade starts checkout for the chosen interval", async () => {
    // Turns red if: the interval chosen is not the one sent.
    signedIn();
    const user = userEvent.setup();
    render(<AccessPricing allowance={ALLOWANCE} onOpenChat={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Yearly" }));
    await user.click(screen.getByRole("button", { name: "Upgrade to Pro" }));
    expect(startCheckout).toHaveBeenCalledWith("year");
  });

  it("a billing outage is said plainly, and the button works again", async () => {
    // Turns red if: the failure is swallowed or the button stays busy.
    signedIn();
    vi.mocked(startCheckout).mockRejectedValueOnce(
      new ApiClientError("x", 503, {
        request_id: "r",
        status: "error",
        error: { code: "billing_unavailable", message: "x" },
      }),
    );
    const user = userEvent.setup();
    render(<AccessPricing allowance={ALLOWANCE} onOpenChat={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Upgrade to Pro" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/billing isn't available/i);
    expect(screen.getByRole("button", { name: "Upgrade to Pro" }).getAttribute("aria-disabled")).not.toBe("true");
  });

  it("the free plan's button starts a chat when signed in, and registration when not", async () => {
    // Turns red if: the free CTA does nothing, or skips sign-up for a visitor.
    const onOpenChat = vi.fn();
    const user = userEvent.setup();
    const { unmount } = render(<AccessPricing allowance={ALLOWANCE} onOpenChat={onOpenChat} />);
    await user.click(screen.getByRole("button", { name: "Create a free account" }));
    expect(await screen.findByText("auth:register")).toBeTruthy();
    unmount();
    signedIn();
    render(<AccessPricing allowance={ALLOWANCE} onOpenChat={onOpenChat} />);
    await user.click(screen.getByRole("button", { name: "Start asking" }));
    expect(onOpenChat).toHaveBeenCalled();
  });
});
