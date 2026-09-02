/**
 * ConnectedAccountsDrawer — the "Sign-in methods" drawer: link GitHub/Google
 * to the signed-in account (ADR-0004 PR 13) and set or change its password
 * (ADR-0004 PR 14).
 *
 * Lazy-loaded from ``AccountMenu`` and portal-rendered into ``document.body``,
 * mirroring ``HistoryDrawer`` exactly (see that module's docstring for the
 * ``backdrop-filter`` containing-block reason the portal exists). The
 * "Connect" buttons live HERE, not inside ``AccountMenu``'s ``role="menu"``
 * dropdown: a menuitem that performs a top-level navigation would be an
 * ARIA mismatch nothing else in this codebase has, and ``AccountMenu`` is
 * eagerly bundled, so every visitor would pay for this UI's weight. The
 * password entry point lives here for the second reason too -- it opens
 * the (also lazy) ``AuthModal`` in ``set-password`` mode, so a user who
 * dismissed the post-sign-in nudge always has a path back to it.
 *
 * "Connect" is a real navigation (``window.location.href``), not an
 * ``apiFetch`` — same reasoning as ``AuthModal``'s OAuth buttons: the
 * provider's consent screen and the redirect back only work as a top-level
 * navigation. The backend requires a FRESH session (created within
 * ``oauth_connect_max_session_age_seconds``) and redirects back with
 * ``?connect=error&reason=session`` otherwise; ``LandingPage`` turns that
 * into a toast. The footnote below tells the user up front.
 */
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { API_BASE_URL } from "../lib/api";
import type { AuthUserResponse } from "../lib/types";
import { GitHubIcon, GoogleIcon } from "./icons/ProviderIcons";

const AuthModal = lazy(() => import("./AuthModal"));

interface ConnectedAccountsDrawerProps {
  triggerRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
  /**
   * The signed-in identity: ``providers`` says which OAuth providers are
   * already linked, ``has_password`` decides "Set a password" vs "Change
   * password". One prop, not two, keeps the eager AccountMenu call site
   * minimal (bundle budget).
   */
  user: Pick<AuthUserResponse, "providers" | "has_password">;
}

const PROVIDERS = [
  { id: "github", label: "GitHub", Icon: GitHubIcon },
  { id: "google", label: "Google", Icon: GoogleIcon },
] as const;

export function ConnectedAccountsDrawer({ triggerRef, onClose, user }: ConnectedAccountsDrawerProps) {
  const { providers, has_password: hasPassword } = user;
  const dialogRef = useRef<HTMLDivElement>(null);
  const passwordButtonRef = useRef<HTMLButtonElement>(null);
  const [passwordOpen, setPasswordOpen] = useState(false);

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

  // Escape closes the drawer -- unless the password modal is open on top of
  // it, in which case the modal's own Escape handler closes the modal and
  // this one must not ALSO close the drawer underneath in the same keypress.
  useEffect(() => {
    if (passwordOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, passwordOpen]);

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
        aria-label="Sign-in methods"
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
          <h2 style={{ margin: 0, fontSize: "18px" }}>Sign-in methods</h2>
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
          Keep more than one way to sign in — a password, GitHub or Google — so losing
          one never locks you out.
        </p>

        <ul style={{ listStyle: "none", padding: 0, margin: "16px 0 0", display: "flex", flexDirection: "column", gap: "8px" }}>
          <li style={rowStyle}>
            <span style={{ flex: 1, fontSize: "14px", fontWeight: 600 }}>Password</span>
            {hasPassword && <span style={{ fontSize: "13px", color: "var(--muted, #666)" }}>Set ✓</span>}
            <button
              ref={passwordButtonRef}
              type="button"
              aria-haspopup="dialog"
              onClick={() => setPasswordOpen(true)}
              style={actionStyle}
            >
              {hasPassword ? "Change password" : "Set a password"}
            </button>
          </li>
          {PROVIDERS.map(({ id, label, Icon }) => {
            const connected = providers.includes(id);
            return (
              <li key={id} style={rowStyle}>
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
                    style={actionStyle}
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

        {passwordOpen && (
          <Suspense fallback={null}>
            <AuthModal triggerRef={passwordButtonRef} onClose={() => setPasswordOpen(false)} initialMode="set-password" />
          </Suspense>
        )}
      </div>
    </div>,
    document.body,
  );
}

const rowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "10px",
  background: "var(--surface-2, #f5f5f5)",
  border: "1px solid var(--border, #e5e7eb)",
  borderRadius: "8px",
  padding: "10px 12px",
};

const actionStyle: React.CSSProperties = {
  background: "var(--ink, #111)",
  color: "var(--text-inverse, #fff)",
  border: "none",
  borderRadius: "6px",
  padding: "6px 12px",
  font: "inherit",
  fontSize: "13px",
  fontWeight: 600,
  cursor: "pointer",
};

export default ConnectedAccountsDrawer;
