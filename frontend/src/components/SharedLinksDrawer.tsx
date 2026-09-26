/**
 * SharedLinksDrawer — the answers you have shared, and a way to stop sharing
 * each one (ADR-0005 Phase 6C-2). Opened from the account menu, only with the
 * access model on. Listing and revoking need only an account, so a lapsed Pro
 * user can still take their links down.
 *
 * Same rules as the API keys drawer: a failed list never claims "nothing
 * shared"; stopping a share asks first ("Really stop sharing?"); one request at a time;
 * focus stays in the drawer after a revoke.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { listShares, revokeShare, shareLink, type ShareView } from "../lib/shares";

const FAILED = "That didn't work. Please try again later.";

export default function SharedLinksDrawer({
  triggerRef,
  onClose,
}: {
  triggerRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const [shares, setShares] = useState<ShareView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  // Announced in a polite live region: copy results, the confirm step, a stop.
  const [note, setNote] = useState("");

  // A failed first load leaves `shares` null, so the drawer never claims
  // "nothing shared" when it does not know. (Unlike the API keys drawer there
  // is no "make" here: the list only reaches [] by stopping your last share.)
  const refresh = (failed = FAILED) =>
    listShares().then(setShares, () => setError(failed));

  useEffect(() => {
    dialogRef.current?.focus();
    void refresh();
    const trigger = triggerRef.current;
    return () => trigger?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on open
  }, [triggerRef]);
  useFocusTrap(dialogRef, { onEscape: onClose });

  // Two clicks: "Stop sharing" asks, "Really stop sharing?" does it.
  const revoke = async (share: ShareView) => {
    if (busy) return; // one revoke at a time
    if (confirming !== share.share_id) {
      setConfirming(share.share_id);
      setNote("Press again to stop sharing.");
      return;
    }
    setBusy(true);
    setConfirming(null);
    setError(null);
    setNote(""); // "Press again…" is done; never leave it after a refused stop
    try {
      await revokeShare(share.share_id);
      // Gone even if the refresh below fails, so it cannot be revoked twice.
      setShares((prev) => prev?.filter((s) => s.share_id !== share.share_id) ?? prev);
      setNote("Stopped sharing. The link no longer works.");
      // The stop worked; a failed refresh must not say it did not.
      await refresh("Stopped sharing, but the list could not be refreshed.");
    } catch {
      setError(FAILED);
    } finally {
      setBusy(false);
      dialogRef.current?.focus(); // the revoked row and its button are gone
    }
  };

  const copy = async (text: string) => {
    const copied = await navigator.clipboard?.writeText(text).then(
      () => true,
      () => false,
    );
    setNote(copied ? "Link copied." : "Could not copy the link. Use Open instead.");
  };

  return createPortal(
    <div
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      className="drawer-backdrop"
    >
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Shared links" tabIndex={-1} className="drawer-panel">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h2 style={{ margin: 0, fontSize: "18px" }}>Shared links</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="drawer-close">
            ×
          </button>
        </div>
        <p className="drawer-note">
          Anyone with one of these links can see that question and answer. Deleting the chat
          does not remove the link; stop sharing it here.
        </p>
        {error && <p role="alert">{error}</p>}
        <p aria-live="polite" className="drawer-note" data-testid="drawer-status">
          {note}
        </p>

        {shares !== null && shares.length === 0 && (
          <p className="drawer-note">You have not shared any answers.</p>
        )}
        <ul style={{ listStyle: "none", padding: 0 }}>
          {(shares ?? []).map((s) => {
            const link = shareLink(s, window.location.origin);
            return (
              <li key={s.share_id} className="drawer-row">
                <span style={{ flex: 1, minWidth: 0 }}>
                  <span style={{ overflowWrap: "anywhere" }}>{s.question}</span>
                  <br />
                  {`Shared ${s.created_at.slice(0, 10)}`}
                  {link && (
                    <>
                      {" · "}
                      <a href={link} target="_blank" rel="noopener noreferrer">
                        Open
                      </a>
                    </>
                  )}
                </span>
                {link && (
                  <button type="button" onClick={() => void copy(link)} aria-label={`Copy link: ${s.question}`}>
                    Copy link
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => void revoke(s)}
                  aria-disabled={busy}
                  aria-label={
                    confirming === s.share_id ? `Really stop sharing: ${s.question}` : `Stop sharing: ${s.question}`
                  }
                >
                  {confirming === s.share_id ? "Really stop sharing?" : "Stop sharing"}
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </div>,
    document.body,
  );
}
