/**
 * ConnectedAccountsDrawer — link GitHub/Google to the signed-in account
 * (ADR-0004 PR 13).
 *
 * Lazy-loaded from ``AccountMenu`` and portal-rendered into ``document.body``,
 * mirroring ``HistoryDrawer`` exactly (see that module's docstring for the
 * ``backdrop-filter`` containing-block reason the portal exists). The
 * "Connect" buttons live HERE, not inside ``AccountMenu``'s ``role="menu"``
 * dropdown: a menuitem that performs a top-level navigation would be an
 * ARIA mismatch nothing else in this codebase has, and ``AccountMenu`` is
 * eagerly bundled, so every visitor would pay for this UI's weight.
 *
 * "Connect" is a real navigation (``window.location.href``), not an
 * ``apiFetch`` — same reasoning as ``AuthModal``'s OAuth buttons: the
 * provider's consent screen and the redirect back only work as a top-level
 * navigation. The backend requires a FRESH session (created within
 * ``oauth_connect_max_session_age_seconds``) and redirects back with
 * ``?connect=error&reason=session`` otherwise; ``LandingPage`` turns that
 * into a toast. The footnote below tells the user up front.
 */
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { API_BASE_URL } from "../lib/api";
import { GitHubIcon, GoogleIcon } from "./icons/ProviderIcons";

interface ConnectedAccountsDrawerProps {
  triggerRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
  /** ``AuthUserResponse.providers`` — which providers are already linked. */
  providers: string[];
}

const PROVIDERS = [
  { id: "github", label: "GitHub", Icon: GitHubIcon },
  { id: "google", label: "Google", Icon: GoogleIcon },
] as const;

export function ConnectedAccountsDrawer({ triggerRef, onClose, providers }: ConnectedAccountsDrawerProps) {
  const dialogRef = useRef<HTMLDivElement>(null);

  // Move focus INTO the dialog on open (the menuitem that opened it has just
  // been unmounted with the menu, so focus would otherwise fall to <body> and
  // the next Tab would land on the page behind the backdrop -- review
  // finding), and restore it to the trigger on unmount. Same contract as
  // AuthModal; no form fields, so no Tab trap.
  useEffect(() => {
    dialogRef.current?.focus();
    const trigger = triggerRef.current;
    return () => {
      trigger?.focus();
    };
  }, [triggerRef]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

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
        aria-label="Connected accounts"
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
          <h2 style={{ margin: 0, fontSize: "18px" }}>Connected accounts</h2>
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

        <p style={{ color: "var(--muted, #666)", fontSize: "14px", marginTop: "8px" }}>
          Connect GitHub or Google so you can sign in with it too — a backup if you ever
          forget your password.
        </p>

        <ul style={{ listStyle: "none", padding: 0, margin: "16px 0 0", display: "flex", flexDirection: "column", gap: "8px" }}>
          {PROVIDERS.map(({ id, label, Icon }) => {
            const connected = providers.includes(id);
            return (
              <li
                key={id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "10px",
                  background: "var(--surface-2, #f5f5f5)",
                  border: "1px solid var(--border, #e5e7eb)",
                  borderRadius: "8px",
                  padding: "10px 12px",
                }}
              >
                <Icon />
                <span style={{ flex: 1, fontSize: "14px", fontWeight: 600 }}>{label}</span>
                {connected ? (
                  <span style={{ fontSize: "13px", color: "var(--muted, #666)" }}>Connected ✓</span>
                ) : (
                  <button
                    type="button"
                    onClick={() => {
                      window.location.href = `${API_BASE_URL}/v1/auth/oauth/${id}/connect/start`;
                    }}
                    style={{
                      background: "var(--ink, #111)",
                      color: "var(--text-inverse, #fff)",
                      border: "none",
                      borderRadius: "6px",
                      padding: "6px 12px",
                      font: "inherit",
                      fontSize: "13px",
                      fontWeight: 600,
                      cursor: "pointer",
                    }}
                  >
                    Connect {label}
                  </button>
                )}
              </li>
            );
          })}
        </ul>

        <p style={{ color: "var(--muted, #666)", fontSize: "12px", marginTop: "16px" }}>
          For security, connecting only works shortly after you sign in. If it has been a
          while, sign out and back in first.
        </p>
      </div>
    </div>,
    document.body,
  );
}

export default ConnectedAccountsDrawer;
