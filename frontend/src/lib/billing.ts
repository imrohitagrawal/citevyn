/**
 * The billing calls (ADR-0005 Phase 5B-2; backend: docs/API_SPEC.md §7c). Used
 * only by the lazy pricing section and usage meter, so the eager bundle never
 * carries them.
 *
 * Checkout and the portal are Stripe pages: the server returns their address
 * and the browser goes there. Only an https Stripe address is followed, so a
 * compromised or confused response cannot send the visitor somewhere else.
 */
import { apiFetch } from "./api";
import { ApiClientError } from "./types";

export interface Usage {
  kind: "trial" | "monthly" | "none";
  used: number;
  limit: number;
  remaining: number;
  resets_at: string | null;
  verified: boolean;
}

export interface Membership {
  tier: "free" | "pro";
  usage: Usage;
}

export function getMembership(): Promise<Membership> {
  return apiFetch<Membership>("/v1/billing/membership");
}

function goToStripe(url: string): void {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error("Unexpected billing address.");
  }
  if (parsed.protocol !== "https:" || !/(^|\.)stripe\.com$/.test(parsed.hostname)) {
    throw new Error("Unexpected billing address.");
  }
  window.location.assign(url);
}

export async function openPortal(): Promise<void> {
  const { url } = await apiFetch<{ url: string }>("/v1/billing/portal", { method: "POST" });
  goToStripe(url);
}

/** Send the browser to Stripe Checkout; an account that already has Pro goes
 * to the portal instead (the server refuses a second checkout: double billing). */
export async function startCheckout(interval: "month" | "year"): Promise<void> {
  let url: string;
  try {
    ({ url } = await apiFetch<{ url: string }>("/v1/billing/checkout", {
      method: "POST",
      body: JSON.stringify({ interval }),
    }));
  } catch (err) {
    if (err instanceof ApiClientError && err.errorCode() === "already_subscribed") {
      await openPortal();
      return;
    }
    throw err;
  }
  goToStripe(url);
}
