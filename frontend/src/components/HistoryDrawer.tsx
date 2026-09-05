/**
 * HistoryDrawer — the signed-in session history list (ADR-0004 PR 10).
 *
 * Lazy-loaded (see ``AccountMenu``), same reasoning as ``AuthModal``: most
 * visitors never open it, so its list-rendering/fetch code should not cost
 * them anything. Portal-rendered into ``document.body`` for the same
 * reason ``AuthModal`` is (see that module's docstring) — anything with a
 * ``position: fixed`` full-viewport backdrop, mounted anywhere inside
 * ``Header``, resolves against the header's own box instead of the
 * viewport, because ``.header`` sets ``backdrop-filter``.
 *
 * A resumed conversation's citations are exactly what the live answer had
 * (migration 0009) — but a historical no-answer/refusal message currently
 * carries no persisted flag distinguishing it from an ordinary reply
 * (``Message`` has no ``unsupported``/``no_answer`` column, only
 * ``citations``), so a resumed refusal renders as plain text without its
 * "NO SOURCE — REFUSED" badge. Recorded here rather than guessed around:
 * inferring refusal from "zero citations" would also mislabel a genuine
 * greeting reply, which legitimately has none.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { listMySessions } from "../lib/api";
import { useFocusTrap } from "../hooks/useFocusTrap";
import type { SessionSummary } from "../lib/types";

interface HistoryDrawerProps {
  triggerRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
  onResume: (sessionId: string) => void;
}

type LoadState = "loading" | "error" | "ready";

export function HistoryDrawer({ triggerRef, onClose, onResume }: HistoryDrawerProps) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    listMySessions()
      .then((resp) => {
        if (cancelled) return;
        setSessions(resp.sessions);
        setLoadState("ready");
      })
      .catch(() => {
        if (!cancelled) setLoadState("error");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Move focus INTO the dialog on open, then restore it to the trigger on
  // unmount. Same contract as AuthModal and ConnectedAccountsDrawer, including
  // the Tab trap as of #331 -- the old note here said "no form fields, so no
  // Tab trap", but the hazard is escaping the dialog, not moving between
  // inputs.
  //
  // The open half was missing (#290): AccountMenu's "History" menuitem unmounts
  // with the menu when clicked, so focus fell to <body> and the next Tab walked
  // the page controls BEHIND the backdrop before reaching this drawer's own
  // buttons -- while the menuitem advertised `aria-haspopup="dialog"`. It needs
  // `tabIndex={-1}` on the dialog below to be focusable at all.
  useEffect(() => {
    dialogRef.current?.focus();
    const trigger = triggerRef.current;
    return () => {
      trigger?.focus();
    };
  }, [triggerRef]);

  // Escape-to-close AND the Tab focus trap (#331). This dialog is
  // `aria-modal="true"` with a backdrop, so the keyboard must not reach the
  // page behind it — it previously did, in 3 forward presses or ONE Shift+Tab.
  useFocusTrap(dialogRef, { onEscape: onClose });

  return createPortal(
    <div
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(28, 27, 25, 0.45)",
        display: "flex",
        justifyContent: "flex-end",
        zIndex: 1100,
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Chat history"
        tabIndex={-1}
        style={{
          background: "var(--surface, #fff)",
          color: "var(--ink, #111)",
          borderLeft: "1px solid var(--border, #e5e7eb)",
          width: "min(340px, 100%)",
          height: "100%",
          overflowY: "auto",
          padding: "20px",
          boxShadow: "-24px 0 60px -30px rgba(0,0,0,0.5)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h2 style={{ margin: 0, fontSize: "18px" }}>History</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            style={{
              background: "transparent",
              border: "none",
              cursor: "pointer",
              fontSize: "18px",
              lineHeight: 1,
              color: "var(--muted, #666)",
              padding: "4px",
            }}
          >
            ×
          </button>
        </div>

        {loadState === "loading" && (
          <p style={{ color: "var(--muted, #666)", fontSize: "14px" }}>Loading…</p>
        )}
        {loadState === "error" && (
          <p style={{ color: "var(--color-error, #c25b4e)", fontSize: "14px" }}>
            Couldn't load your history. Try again in a moment.
          </p>
        )}
        {loadState === "ready" && sessions.length === 0 && (
          <p style={{ color: "var(--muted, #666)", fontSize: "14px" }}>
            No conversations yet — ask something to get started.
          </p>
        )}
        {loadState === "ready" && sessions.length > 0 && (
          <ul style={{ listStyle: "none", padding: 0, margin: "16px 0 0", display: "flex", flexDirection: "column", gap: "8px" }}>
            {sessions.map((s) => (
              <li key={s.session_id}>
                <button
                  type="button"
                  onClick={() => onResume(s.session_id)}
                  style={{
                    display: "block",
                    width: "100%",
                    textAlign: "left",
                    background: "var(--surface-2, #f5f5f5)",
                    border: "1px solid var(--border, #e5e7eb)",
                    borderRadius: "8px",
                    padding: "10px 12px",
                    cursor: "pointer",
                    font: "inherit",
                    color: "var(--ink, #111)",
                  }}
                >
                  <div style={{ fontSize: "13px", fontWeight: 600 }}>
                    {s.current_product_area ?? "Conversation"}
                  </div>
                  <div style={{ fontSize: "12px", color: "var(--muted, #666)" }}>
                    {s.message_count} message{s.message_count === 1 ? "" : "s"} ·{" "}
                    {new Date(s.created_at).toLocaleDateString()}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>,
    document.body,
  );
}

export default HistoryDrawer;
