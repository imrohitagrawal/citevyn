/**
 * ADR-0005 Phase 5B-2: the billing calls the pricing section and the usage
 * meter make. Each test says which change turns it red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "./api";
import { getMembership, startCheckout } from "./billing";
import { ApiClientError } from "./types";

vi.mock("./api", () => ({ apiFetch: vi.fn() }));

const assign = vi.fn();
beforeEach(() => {
  vi.stubGlobal("location", { ...window.location, assign });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe("startCheckout", () => {
  it("posts the interval and sends the browser to Stripe Checkout", async () => {
    // Turns red if: the body or the navigation changes.
    vi.mocked(apiFetch).mockResolvedValue({ url: "https://checkout.stripe.com/c/pay/cs_1" });
    await startCheckout("year");
    expect(apiFetch).toHaveBeenCalledWith("/v1/billing/checkout", {
      method: "POST",
      body: JSON.stringify({ interval: "year" }),
    });
    expect(assign).toHaveBeenCalledWith("https://checkout.stripe.com/c/pay/cs_1");
  });

  it("an account that already has Pro is sent to the billing portal instead", async () => {
    // Turns red if: already_subscribed is shown as an error instead of opening
    // the portal (the server refuses a second checkout to avoid double billing).
    vi.mocked(apiFetch)
      .mockRejectedValueOnce(
        new ApiClientError("x", 409, {
          request_id: "r",
          status: "error",
          error: { code: "already_subscribed", message: "x" },
        }),
      )
      .mockResolvedValueOnce({ url: "https://billing.stripe.com/p/session/1" });
    await startCheckout("month");
    expect(apiFetch).toHaveBeenLastCalledWith("/v1/billing/portal", { method: "POST" });
    expect(assign).toHaveBeenCalledWith("https://billing.stripe.com/p/session/1");
  });

  it.each(["http://checkout.stripe.com/x", "https://evil.example/stripe.com", "javascript:alert(1)"])(
    "never navigates to an address that is not Stripe over https: %s",
    async (url) => {
      // Turns red if: the address is not checked before navigating.
      vi.mocked(apiFetch).mockResolvedValue({ url });
      await expect(startCheckout("month")).rejects.toThrow();
      expect(assign).not.toHaveBeenCalled();
    },
  );
});

describe("getMembership", () => {
  it("reads the membership view", async () => {
    // Turns red if: the path changes.
    vi.mocked(apiFetch).mockResolvedValue({ tier: "free" });
    await getMembership();
    expect(apiFetch).toHaveBeenCalledWith("/v1/billing/membership");
  });
});
