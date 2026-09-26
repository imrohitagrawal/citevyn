/**
 * AccessPricing — the pricing section with the access model on (ADR-0005
 * Phase 5B-2). Replaces the planned-tiers section only when the server says the
 * model is on; with it off, today's section renders unchanged.
 *
 * Free: the one-time trial. Pro: $9 a month or $91 a year (locked decisions).
 * Team: coming soon, not buyable (designed, not built). The allowance numbers
 * come from the server's settings. No text inputs (TEST_STRATEGY §8b): the
 * month/year choice is two buttons.
 */
import { lazy, Suspense, useRef, useState, useSyncExternalStore } from "react";
import { getAuthSnapshot, subscribeAuth } from "../lib/authStore";
import { startCheckout } from "../lib/billing";
import { ApiClientError } from "../lib/types";
import { LazyChunkBoundary } from "./LazyChunkBoundary";

const AuthModal = lazy(() => import("./AuthModal"));

type Interval = "month" | "year";
const PRICE: Record<Interval, [string, string]> = { month: ["$9", "/month"], year: ["$91", "/year"] };

export default function AccessPricing({
  allowance,
  onOpenChat,
}: {
  allowance: { free_trial_answers: number; pro_monthly_answers: number };
  onOpenChat: () => void;
}) {
  const auth = useSyncExternalStore(subscribeAuth, getAuthSnapshot, getAuthSnapshot);
  const signedIn = auth.status === "signed-in";
  const [interval, setInterval] = useState<Interval>("month");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [registering, setRegistering] = useState(false);
  const trigger = useRef<HTMLButtonElement | null>(null);
  // Registering from Upgrade continues to checkout afterwards, with the
  // interval chosen before (found in review: the choice was dropped).
  const resumeCheckout = useRef(false);

  const register = (e: React.MouseEvent<HTMLButtonElement>, thenCheckout: boolean) => {
    trigger.current = e.currentTarget;
    resumeCheckout.current = thenCheckout;
    setRegistering(true);
  };

  const checkout = async () => {
    setBusy(true);
    setError(null);
    try {
      await startCheckout(interval);
    } catch (err) {
      setError(
        err instanceof ApiClientError && err.errorCode() === "billing_unavailable"
          ? "Billing isn't available right now. Please try again later."
          : "We couldn't start the upgrade. Please try again later.",
      );
    } finally {
      setBusy(false);
    }
  };

  const upgrade = (e: React.MouseEvent<HTMLButtonElement>) => {
    if (busy) return;
    if (!signedIn) return register(e, true);
    void checkout();
  };

  const [price, unit] = PRICE[interval];
  const n = (v: number) => v.toLocaleString("en-US");
  return (
    <section id="pricing" className="section">
      <div className="section-header centered">
        <span className="mono-label">Pricing</span>
        <h2>Start free. Upgrade when you rely on it.</h2>
        <p>Only answered questions count. Refusals never count.</p>
        <div className="interval-toggle">
          {(["month", "year"] as const).map((i) => (
            <button key={i} type="button" aria-pressed={interval === i} onClick={() => setInterval(i)}>
              {i === "month" ? "Monthly" : "Yearly"}
            </button>
          ))}
        </div>
      </div>
      <div className="pricing-grid">
        <div className="pricing-card">
          <h3>Free</h3>
          <div className="price-row">
            <span className="price">$0</span>
          </div>
          <p>{n(allowance.free_trial_answers)} answered questions to try it, once, on a verified account.</p>
          {signedIn ? (
            <button type="button" className="cta cta-outlined" onClick={onOpenChat}>
              Start asking
            </button>
          ) : (
            <button type="button" className="cta cta-outlined" onClick={(e) => register(e, false)}>
              Create a free account
            </button>
          )}
        </div>
        <div className="pricing-card featured">
          <h3>Pro</h3>
          <div className="price-row">
            <span className="price">{price}</span>
            <span className="unit">{unit}</span>
          </div>
          <p>{n(allowance.pro_monthly_answers)} answered questions a month. Cancel any time; Pro lasts to the end of what you paid for.</p>
          <button type="button" className="cta cta-filled" aria-disabled={busy} onClick={upgrade}>
            Upgrade to Pro
          </button>
          {error && <p role="alert">{error}</p>}
        </div>
        <div className="pricing-card">
          <h3>Team</h3>
          <p>Shared plans for teams.</p>
          <button type="button" className="cta cta-outlined" disabled>
            Coming soon
          </button>
        </div>
      </div>
      {registering && (
        <LazyChunkBoundary label="auth-modal" onError={() => setRegistering(false)}>
          <Suspense fallback={null}>
            <AuthModal
              triggerRef={trigger}
              initialMode="register"
              onAuthenticated={() => {
                if (resumeCheckout.current) void checkout();
              }}
              onClose={() => setRegistering(false)}
            />
          </Suspense>
        </LazyChunkBoundary>
      )}
    </section>
  );
}
