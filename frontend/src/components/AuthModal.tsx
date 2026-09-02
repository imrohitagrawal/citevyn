/**
 * AuthModal — sign in / register / magic link / set password (ADR-0004 PR 8, PR 14).
 *
 * Lazy-loaded (see ``AuthModal.lazy``): the ADR chose "no auth library" and
 * a lazy-loaded modal specifically so the majority of visitors — who never
 * open it — pay nothing for its bundle weight. Kept dependency-free and
 * inline-styled, same idiom as ``ToastHost``. PR 14 widened the mode union
 * instead of inventing a new page-level surface: every new form here rides
 * this already-lazy chunk, so the eager bundle stays flat.
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
import { requestMagicLink, updatePassword } from "../lib/authActions";
import { ApiClientError } from "../lib/types";
import { GitHubIcon, GoogleIcon } from "./icons/ProviderIcons";

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Mirrors the backend's _PASSWORD_MIN_LENGTH / _PASSWORD_MAX_LENGTH so the
// browser rejects what the server would reject, before a round trip.
const PASSWORD_MIN_LENGTH = 8;
const PASSWORD_MAX_LENGTH = 128;

export type AuthModalMode = "login" | "register" | "magic-link" | "set-password";

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
  /**
   * ADR-0004 PR 14: open straight into a mode. ``"set-password"`` is the
   * only one callers actually pass (the post-sign-in nudge and the
   * sign-in-methods drawer); the default is the classic sign-in form.
   */
  initialMode?: AuthModalMode;
}

export function AuthModal({ triggerRef, onClose, onAuthenticated, initialMode = "login" }: AuthModalProps) {
  const { signIn, signUp, user } = useAuth();
  const [mode, setMode] = useState<AuthModalMode>(initialMode);
  const [email, setEmail] = useState("");
  const [password, setPassword_] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  // A SUCCESS message that keeps the dialog open ("check your email",
  // "password saved"). role="status", never the error slot's role="alert":
  // announcing a success as a warning is exactly the mis-signal the
  // ToastHost error→alert / else→status split already avoids.
  const [notice, setNotice] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const dialogRef = useRef<HTMLDivElement>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);
  const doneButtonRef = useRef<HTMLButtonElement>(null);
  const titleId = "auth-modal-title";

  // Whether the account already has a password decides the SHAPE of the
  // set-password form (current + new, or new only). The server makes the
  // same decision from its own stored row and ignores/requires the field
  // accordingly -- this is display logic, not the authorization decision.
  const hasPassword = user?.has_password === true;

  // Restore focus to the trigger on close. The cleanup function runs on
  // unmount (Escape, backdrop click, or a successful submit all unmount
  // this component), which is what makes restoration automatic instead of
  // something every close path has to remember to call.
  useEffect(() => {
    const trigger = triggerRef.current;
    return () => {
      trigger?.focus();
    };
  }, [triggerRef]);

  // Focus the first field on open AND on every mode switch: the button that
  // switched modes (e.g. "Email me a sign-in link") unmounts with the mode
  // it belonged to, and focus would otherwise fall to <body> -- from where
  // the next Tab lands on the page behind the backdrop, outside the trap.
  useEffect(() => {
    firstFieldRef.current?.focus();
  }, [mode]);

  // The success screen replaces the whole form (and whatever field held
  // focus) with the notice + Done button; without this, focus falls to
  // <body> inside a still-open aria-modal dialog and the next Tab leaves it
  // (review finding, reproduced under jsdom).
  useEffect(() => {
    if (done) doneButtonRef.current?.focus();
  }, [done]);

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

  const switchMode = (next: AuthModalMode) => {
    setMode(next);
    setError(null);
    setNotice(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setNotice(null);
    setSubmitting(true);
    try {
      if (mode === "login") {
        await signIn(email, password);
      } else if (mode === "register") {
        await signUp(email, password);
      } else if (mode === "magic-link") {
        await requestMagicLink(email);
        // Deliberately generic: the server never says whether the address is
        // registered, so neither can this copy.
        setNotice(
          "If that email has an account, a sign-in link is on its way. It works once and expires in 10 minutes.",
        );
        return;
      } else {
        await updatePassword(password, hasPassword ? currentPassword : undefined);
        setNotice("Password saved. Any other devices were signed out.");
        setDone(true);
        return;
      }
      onAuthenticated?.();
      onClose();
    } catch (err) {
      setError(
        err instanceof ApiClientError
          ? err.status === 404 && mode === "magic-link"
            ? "Email sign-in isn't available right now."
            : err.message
          : "Something went wrong. Try again.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  // Frozen once saved: has_password flips to true in the store the moment
  // the request succeeds, which would otherwise re-title this screen
  // "Change password" under a "Password saved" notice.
  const title = done
    ? "Password saved"
    : mode === "login"
      ? "Sign in"
      : mode === "register"
        ? "Create an account"
        : mode === "magic-link"
          ? "Email me a sign-in link"
          : hasPassword
            ? "Change password"
            : "Set a password";

  const intro =
    mode === "magic-link"
      ? "We'll email you a link that signs you in — no password needed."
      : mode === "set-password"
        ? hasPassword
          ? "Enter your current password, then choose a new one. Other devices will be signed out."
          : "Add a password as a backup way to sign in. You'll stay signed in here; other devices will be signed out."
        : "Optional and free. Keeps your chat history across visits — the demo works fully without this.";

  const submitLabel =
    mode === "login"
      ? "Sign in"
      : mode === "register"
        ? "Create account"
        : mode === "magic-link"
          ? "Send link"
          : "Save password";

  const showsEmail = mode !== "set-password";
  const showsPassword = mode !== "magic-link";
  const showsCurrentPassword = mode === "set-password" && hasPassword;
  const showsAlternatives = mode === "login" || mode === "register";

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
            {title}
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

        {!done && (
          <p style={{ color: "var(--muted, #666)", fontSize: "14px", marginTop: "8px" }}>{intro}</p>
        )}

        {showsAlternatives && (
          <>
            {/*
              ADR-0004 PR 12: a real browser navigation, not an apiFetch call —
              the backend's redirect to the provider's consent screen (and its
              own redirect back) only works as a top-level navigation, not an
              XHR/fetch. `dialogRef`'s existing focus trap covers these
              buttons automatically since they sit inside the same container.
              PR 14 adds "Email me a sign-in link" alongside them: all three
              are "skip the password form" alternatives and belong together.
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
              <button type="button" onClick={() => switchMode("magic-link")} style={oauthButtonStyle}>
                Email me a sign-in link
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
          </>
        )}

        {done ? (
          <>
            {notice && (
              <p role="status" style={{ fontSize: "13px", margin: "16px 0 0" }}>
                {notice}
              </p>
            )}
            <button ref={doneButtonRef} type="button" onClick={onClose} style={{ ...submitStyle, marginTop: "16px" }}>
              Done
            </button>
          </>
        ) : (
          <form
            onSubmit={handleSubmit}
            style={{ display: "flex", flexDirection: "column", gap: "12px", marginTop: showsAlternatives ? 0 : "16px" }}
          >
            {showsEmail && (
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
            )}
            {showsCurrentPassword && (
              <label style={{ display: "flex", flexDirection: "column", gap: "4px", fontSize: "13px" }}>
                Current password
                <input
                  ref={firstFieldRef}
                  type="password"
                  required
                  maxLength={PASSWORD_MAX_LENGTH}
                  autoComplete="current-password"
                  value={currentPassword}
                  onChange={(e) => setCurrentPassword(e.target.value)}
                  style={inputStyle}
                />
              </label>
            )}
            {showsPassword && (
              <label style={{ display: "flex", flexDirection: "column", gap: "4px", fontSize: "13px" }}>
                {mode === "set-password" ? "New password" : "Password"}
                <input
                  ref={mode === "set-password" && !hasPassword ? firstFieldRef : undefined}
                  type="password"
                  required
                  minLength={mode === "login" ? undefined : PASSWORD_MIN_LENGTH}
                  maxLength={PASSWORD_MAX_LENGTH}
                  autoComplete={mode === "login" ? "current-password" : "new-password"}
                  value={password}
                  onChange={(e) => setPassword_(e.target.value)}
                  style={inputStyle}
                />
              </label>
            )}

            {error && (
              <p role="alert" style={{ color: "var(--color-error, #c25b4e)", fontSize: "13px", margin: 0 }}>
                {error}
              </p>
            )}
            {notice && (
              <p role="status" style={{ fontSize: "13px", margin: 0 }}>
                {notice}
              </p>
            )}

            <button
              type="submit"
              disabled={submitting}
              style={{ ...submitStyle, cursor: submitting ? "default" : "pointer", opacity: submitting ? 0.7 : 1 }}
            >
              {submitting ? "Working…" : submitLabel}
            </button>
          </form>
        )}

        {(mode === "login" || mode === "register") && (
          <button
            type="button"
            onClick={() => switchMode(mode === "login" ? "register" : "login")}
            style={linkButtonStyle}
          >
            {mode === "login" ? "Need an account? Register" : "Have an account? Sign in"}
          </button>
        )}
        {mode === "magic-link" && (
          <button type="button" onClick={() => switchMode("login")} style={linkButtonStyle}>
            Back to sign in
          </button>
        )}
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

const submitStyle: React.CSSProperties = {
  background: "var(--ink, #111)",
  color: "var(--text-inverse, #fff)",
  border: "none",
  borderRadius: "8px",
  padding: "10px 16px",
  fontWeight: 600,
  cursor: "pointer",
  font: "inherit",
};

const linkButtonStyle: React.CSSProperties = {
  marginTop: "14px",
  background: "transparent",
  border: "none",
  color: "var(--muted, #666)",
  fontSize: "13px",
  cursor: "pointer",
  textDecoration: "underline",
  padding: 0,
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

export default AuthModal;
