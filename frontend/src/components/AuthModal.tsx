/**
 * AuthModal — sign in / register (ADR-0004 PR 8).
 *
 * Lazy-loaded (see ``AuthModal.lazy``): the ADR chose "no auth library" and
 * a lazy-loaded modal specifically so the majority of visitors — who never
 * open it — pay nothing for its bundle weight. Kept dependency-free and
 * inline-styled, same idiom as ``ToastHost``.
 *
 * Accessibility contract (verified live in a browser, not just asserted —
 * see the PR body): focus moves into the dialog on open, Tab/Shift+Tab
 * cycle WITHIN it (a true trap, not just an initial focus), Escape closes
 * it, and closing restores focus to the element that opened it. All four
 * are easy to get structurally right and behaviorally wrong.
 *
 * Renders via ``createPortal`` into ``document.body``, NOT in place —
 * found live, not by the (otherwise thorough) jsdom test suite, which has
 * no layout engine and can't see this. ``.header`` sets
 * ``backdrop-filter: blur(12px)`` for its sticky glass effect, and
 * ``backdrop-filter`` (like ``transform``/``filter``/``will-change``)
 * creates a new containing block for descendant ``position: fixed``
 * elements per the CSS spec. Rendered in place (AccountMenu lives inside
 * Header), this modal's ``position: fixed; inset: 0`` backdrop resolved
 * against the 64px-tall header instead of the viewport — the dialog
 * rendered real, focusable, tab-trappable DOM, just squeezed into a
 * 64px-tall box and mostly off-screen. The portal is what makes "verified
 * live in a browser" not just a note about testing methodology.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useAuth } from "../hooks/useAuth";
import { API_BASE_URL } from "../lib/api";
import { ApiClientError } from "../lib/types";

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

interface AuthModalProps {
  /** Element to restore focus to on close — the button that opened the modal. */
  triggerRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
  /**
   * Called right before ``onClose`` on a SUCCESSFUL sign-in/register only
   * (never on close-without-submitting). ADR-0004 PR 9: lets the caller
   * (LandingPage, which owns the toast host and the current chat's
   * message count) confirm "your conversation is saved" — this component
   * has no access to that state itself, by design (auth and chat state
   * are deliberately independent modules).
   */
  onAuthenticated?: () => void;
}

type Mode = "login" | "register";

export function AuthModal({ triggerRef, onClose, onAuthenticated }: AuthModalProps) {
  const { signIn, signUp } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const dialogRef = useRef<HTMLDivElement>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);
  const titleId = "auth-modal-title";

  // Focus the first field on open, restore focus to the trigger on close.
  // The cleanup function runs on unmount (Escape, backdrop click, or a
  // successful submit all unmount this component), which is what makes
  // restoration automatic instead of something every close path has to
  // remember to call.
  useEffect(() => {
    firstFieldRef.current?.focus();
    const trigger = triggerRef.current;
    return () => {
      trigger?.focus();
    };
  }, [triggerRef]);

  // Escape-to-close and the Tab focus trap. One listener, not two: both
  // are keydown handlers scoped to the same dialog, and splitting them
  // would just be two effects doing the same subscribe/unsubscribe dance.
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab") return;

      const container = dialogRef.current;
      if (!container) return;
      // No ``offsetParent``/layout-based visibility filter: nothing in this
      // modal is ever conditionally hidden, and ``offsetParent`` is always
      // null under jsdom (no layout engine), which silently emptied this
      // list and disabled the trap under test — found by the trap tests
      // themselves, not a manual browser check.
      const focusable = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) return;

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;

      // Wrap at both ends — this is what makes it a TRAP, not just an
      // initial-focus courtesy. Without the wrap, Tab from the last
      // field walks focus out into the page behind the modal.
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      if (mode === "login") {
        await signIn(email, password);
      } else {
        await signUp(email, password);
      }
      onAuthenticated?.();
      onClose();
    } catch (err) {
      setError(
        err instanceof ApiClientError
          ? err.message
          : "Something went wrong. Try again.",
      );
    } finally {
      setSubmitting(false);
    }
  };

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
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1100,
        padding: "20px",
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        style={{
          background: "var(--surface, #fff)",
          color: "var(--ink, #111)",
          border: "1px solid var(--border, #e5e7eb)",
          borderRadius: "14px",
          padding: "28px",
          width: "min(380px, 100%)",
          // A short viewport (found live, browser check) otherwise clips the
          // top of the dialog off-screen: the flex-centered backdrop
          // centers around the vertical MIDPOINT regardless of overflow, so
          // a dialog taller than the viewport pushes equally off both ends
          // rather than scrolling into view.
          maxHeight: "calc(100vh - 40px)",
          overflowY: "auto",
          boxShadow: "0 24px 60px -20px rgba(0,0,0,0.5)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h2 id={titleId} style={{ margin: 0, fontSize: "20px" }}>
            {mode === "login" ? "Sign in" : "Create an account"}
          </h2>
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
          Optional and free. Keeps your chat history across visits — the demo
          works fully without this.
        </p>

        {/*
          ADR-0004 PR 12: a real browser navigation, not an apiFetch call —
          the backend's redirect to the provider's consent screen (and its
          own redirect back) only works as a top-level navigation, not an
          XHR/fetch. `dialogRef`'s existing focus trap covers these two
          buttons automatically since they sit inside the same container.
        */}
        <div style={{ display: "flex", flexDirection: "column", gap: "8px", marginTop: "16px" }}>
          <button
            type="button"
            onClick={() => {
              window.location.href = `${API_BASE_URL}/v1/auth/oauth/github/start`;
            }}
            style={oauthButtonStyle}
          >
            <GitHubIcon />
            Continue with GitHub
          </button>
          <button
            type="button"
            onClick={() => {
              window.location.href = `${API_BASE_URL}/v1/auth/oauth/google/start`;
            }}
            style={oauthButtonStyle}
          >
            <GoogleIcon />
            Continue with Google
          </button>
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "10px",
            margin: "16px 0",
            color: "var(--muted, #666)",
            fontSize: "12px",
          }}
        >
          <span style={{ flex: 1, height: "1px", background: "var(--border, #e5e7eb)" }} />
          or
          <span style={{ flex: 1, height: "1px", background: "var(--border, #e5e7eb)" }} />
        </div>

        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          <label style={{ display: "flex", flexDirection: "column", gap: "4px", fontSize: "13px" }}>
            Email
            <input
              ref={firstFieldRef}
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              style={inputStyle}
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: "4px", fontSize: "13px" }}>
            Password
            <input
              type="password"
              required
              minLength={mode === "register" ? 8 : undefined}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              style={inputStyle}
            />
          </label>

          {error && (
            <p role="alert" style={{ color: "var(--color-error, #c25b4e)", fontSize: "13px", margin: 0 }}>
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={submitting}
            style={{
              background: "var(--ink, #111)",
              color: "var(--text-inverse, #fff)",
              border: "none",
              borderRadius: "8px",
              padding: "10px 16px",
              fontWeight: 600,
              cursor: submitting ? "default" : "pointer",
              opacity: submitting ? 0.7 : 1,
            }}
          >
            {submitting ? "Working…" : mode === "login" ? "Sign in" : "Create account"}
          </button>
        </form>

        <button
          type="button"
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setError(null);
          }}
          style={{
            marginTop: "14px",
            background: "transparent",
            border: "none",
            color: "var(--muted, #666)",
            fontSize: "13px",
            cursor: "pointer",
            textDecoration: "underline",
            padding: 0,
          }}
        >
          {mode === "login" ? "Need an account? Register" : "Have an account? Sign in"}
        </button>
      </div>
    </div>,
    document.body,
  );
}

const inputStyle: React.CSSProperties = {
  padding: "8px 10px",
  borderRadius: "8px",
  border: "1px solid var(--border, #e5e7eb)",
  background: "var(--surface, #fff)",
  color: "var(--ink, #111)",
  font: "inherit",
};

const oauthButtonStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  gap: "8px",
  padding: "9px 16px",
  borderRadius: "8px",
  border: "1px solid var(--border, #e5e7eb)",
  background: "var(--surface, #fff)",
  color: "var(--ink, #111)",
  font: "inherit",
  fontWeight: 500,
  cursor: "pointer",
};

/** Inline glyph, no icon library — matches this file's zero-dependency idiom. */
function GitHubIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

/** Inline glyph, no icon library — matches this file's zero-dependency idiom. */
function GoogleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.9c1.7-1.57 2.7-3.87 2.7-6.62Z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.9-2.26c-.8.54-1.84.86-3.06.86-2.35 0-4.34-1.59-5.05-3.72H.98v2.33A9 9 0 0 0 9 18Z"
      />
      <path
        fill="#FBBC05"
        d="M3.95 10.7A5.4 5.4 0 0 1 3.67 9c0-.59.1-1.16.28-1.7V4.97H.98A9 9 0 0 0 0 9c0 1.45.35 2.83.98 4.03l2.97-2.33Z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.32 0 2.51.46 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .98 4.97l2.97 2.33C4.66 5.17 6.65 3.58 9 3.58Z"
      />
    </svg>
  );
}

export default AuthModal;
