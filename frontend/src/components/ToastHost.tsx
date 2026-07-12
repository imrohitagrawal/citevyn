/**
 * ToastHost — fixed-position stack that renders the transient
 * notifications produced by ``useToast``. Presentational only: it owns
 * no state, just maps the toast list to cards and reports dismissals.
 *
 * Used for the live-backend error path (rate limit / server error /
 * network failure). Kept dependency-free and inline-styled so it needs
 * no additions to ``landing.css`` and renders identically in the demo
 * and live builds.
 */
import type { Toast } from "../hooks/useToast";

interface ToastHostProps {
  toasts: Toast[];
  onDismiss: (id: string) => void;
}

const KIND_ACCENT: Record<Toast["kind"], string> = {
  info: "#2563eb",
  success: "#16a34a",
  warning: "#d97706",
  error: "#dc2626",
};

export function ToastHost({ toasts, onDismiss }: ToastHostProps) {
  if (toasts.length === 0) return null;

  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      style={{
        position: "fixed",
        bottom: "20px",
        right: "20px",
        display: "flex",
        flexDirection: "column",
        gap: "10px",
        zIndex: 1000,
        maxWidth: "min(360px, calc(100vw - 40px))",
      }}
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          role={toast.kind === "error" ? "alert" : "status"}
          style={{
            display: "flex",
            gap: "10px",
            alignItems: "flex-start",
            background: "var(--surface, #fff)",
            color: "var(--ink, #111)",
            border: "1px solid var(--border, #e5e7eb)",
            borderLeft: `4px solid ${KIND_ACCENT[toast.kind]}`,
            borderRadius: "10px",
            padding: "12px 14px",
            boxShadow: "0 8px 24px -12px rgba(0,0,0,0.4)",
            fontSize: "14px",
            lineHeight: 1.45,
          }}
        >
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600, marginBottom: "2px" }}>{toast.title}</div>
            <div style={{ opacity: 0.85 }}>{toast.message}</div>
          </div>
          <button
            type="button"
            onClick={() => onDismiss(toast.id)}
            aria-label="Dismiss notification"
            style={{
              flexShrink: 0,
              background: "transparent",
              border: "none",
              color: "inherit",
              cursor: "pointer",
              fontSize: "16px",
              lineHeight: 1,
              opacity: 0.6,
              padding: 0,
            }}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
