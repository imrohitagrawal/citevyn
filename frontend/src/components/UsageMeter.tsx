/**
 * UsageMeter — "n of N left", under the chat composer (ADR-0005 Phase 5B-2).
 * Mounted only with the access model on and the caller signed in. Re-reads the
 * membership after each answer. A failed read renders nothing: the chat must
 * never break, or show wrong numbers, because the meter could not load.
 *
 * Not a live region: it changes after every answer, and the chat's own
 * announcer (#356) already speaks the answer.
 */
import { useEffect, useState } from "react";
import { getMembership, startCheckout, type Usage } from "../lib/billing";

export default function UsageMeter({ answered }: { answered: number }) {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    getMembership().then(
      (m) => live && setUsage(m.usage),
      () => live && setUsage(null),
    );
    return () => {
      live = false;
    };
  }, [answered]);

  if (!usage || usage.kind === "none") return null;
  const n = (v: number) => v.toLocaleString("en-US");
  let text: string;
  if (usage.kind === "trial" && !usage.verified) {
    text = "Verify your email to start your free questions.";
  } else if (usage.kind === "trial") {
    text = `${n(usage.remaining)} of ${n(usage.limit)} free questions left`;
  } else {
    text = `${n(usage.remaining)} of ${n(usage.limit)} questions left this month`;
  }
  return (
    <div className="usage-meter">
      <span>{text}</span>
      {usage.kind === "trial" && usage.verified && (
        <button
          type="button"
          aria-disabled={busy}
          onClick={() => {
            if (busy) return;
            setBusy(true);
            startCheckout("month").finally(() => setBusy(false));
          }}
        >
          Upgrade
        </button>
      )}
    </div>
  );
}
