/**
 * Toast notification system.
 *
 * A single region in the top-right that hosts a stack of toasts.
 * Each toast auto-dismisses after a few seconds (longer for
 * rate-limit, since the reviewer will want to read it) and can
 * be dismissed manually with the × button.
 *
 * The shape is intentionally small: a tiny in-memory store
 * keyed off ``useState`` in the parent. We don't need a
 * notification library for this — a single reducer is enough.
 */

import { useEffect, useState } from "react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ToastKind = "info" | "warning" | "error";

export interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  message: string;
  /** Auto-dismiss after this many ms. 0 = sticky. */
  durationMs: number;
  /** Optional secondary line, e.g. a request id. */
  requestId?: string;
}

interface ToastRegionProps {
  toasts: Toast[];
  onDismiss: (id: number) => void;
}

export function ToastRegion({ toasts, onDismiss }: ToastRegionProps) {
  return (
    <div className="toast-region" aria-live="polite" aria-relevant="additions">
      {toasts.map((t) => (
        <ToastCard key={t.id} toast={t} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

function ToastCard({ toast, onDismiss }: { toast: Toast; onDismiss: (id: number) => void }) {
  const [closing, setClosing] = useState(false);

  useEffect(() => {
    if (toast.durationMs <= 0) return;
    const t = window.setTimeout(() => setClosing(true), toast.durationMs);
    return () => window.clearTimeout(t);
  }, [toast.durationMs]);

  useEffect(() => {
    if (!closing) return;
    const t = window.setTimeout(() => onDismiss(toast.id), 200);
    return () => window.clearTimeout(t);
  }, [closing, onDismiss, toast.id]);

  const icon = toast.kind === "error" ? "⚠" : toast.kind === "warning" ? "⏱" : "ℹ";

  return (
    <div
      className={`toast toast--${toast.kind}` + (closing ? " toast--closing" : "")}
      role="status"
    >
      <span className="toast__icon" aria-hidden="true">{icon}</span>
      <div className="toast__body">
        <span className="toast__title">{toast.title}</span>
        <span className="toast__message">{toast.message}</span>
        {toast.requestId && (
          <span className="toast__message tiny muted mono">request: {toast.requestId}</span>
        )}
      </div>
      <button
        type="button"
        className="toast__close"
        aria-label="Dismiss"
        onClick={() => setClosing(true)}
      >
        ×
      </button>
    </div>
  );
}