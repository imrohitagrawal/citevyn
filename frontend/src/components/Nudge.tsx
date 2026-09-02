/**
 * PasswordNudge — the one-time "set a password" prompt after a passwordless
 * sign-in (ADR-0004 PR 14).
 *
 * Mounted by ``LandingPage`` only on the ``?auth=ok`` return trip (a
 * magic-link or OAuth login — both land there), and lazy-loaded so the
 * eager bundle carries nothing but the mount condition. It decides for
 * itself whether to show: the signed-in user must have no password, and
 * the visitor must not have dismissed it on this device.
 *
 * Copy is deliberately GENERIC: the trigger cannot tell a magic-link login
 * from an OAuth one, so magic-link-specific wording ("so you don't need
 * email each time") would read wrong for OAuth users.
 *
 * "Not now" is remembered in ``localStorage`` -- no server-side flag,
 * because ``has_password`` itself is the permanent, cross-device
 * suppression signal once the user actually sets one. The prompt is a
 * ``role="status"`` card, never a modal: fresh off an email click, possibly
 * on an unfamiliar device, is a bad moment to FORCE creating a credential.
 * "Set one" opens ``AuthModal`` in ``set-password`` mode from a real
 * clicked button, which satisfies the modal's ``triggerRef`` contract.
 *
 * ADR-0004 PR 15 (#293): the card also shows, with different copy, when the
 * account HAS a password but this session just redeemed a magic link
 * (``password_step_up``) -- that is the forgotten-password user, and the
 * card is how they find the one-shot "set a new password without the old
 * one" path before the window closes.
 *
 * The FILE is named ``Nudge`` rather than ``PasswordNudge`` on purpose: the
 * lazy chunk's file name is one of the very few strings this feature adds
 * to the eager bundle, which sits within bytes of its 63.5 kB gzip ceiling
 * (``docs/BACKLOG.md`` #270) -- measured, not guessed: the longer name put
 * the eager chunk 8 bytes over the line, this one 4 bytes under.
 */
import { lazy, Suspense, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useAuth } from "../hooks/useAuth";

const AuthModal = lazy(() => import("./AuthModal"));

export const PASSWORD_NUDGE_DISMISSED_KEY = "citevyn.password-nudge-dismissed";

function readDismissed(): boolean {
  try {
    return window.localStorage.getItem(PASSWORD_NUDGE_DISMISSED_KEY) === "1";
  } catch {
    return false;
  }
}

export function PasswordNudge() {
  const { status, user } = useAuth();
  const [dismissed, setDismissed] = useState(readDismissed);
  const [modalOpen, setModalOpen] = useState(false);
  const setButtonRef = useRef<HTMLButtonElement>(null);

  // Stay mounted while the modal is open even once has_password flips to
  // true (the modal is our child; unmounting it mid-"Password saved" would
  // yank the confirmation away). It disappears on the modal's close.
  const stepUp = status === "signed-in" && user !== null && user.has_password && user.password_step_up;
  const applies = status === "signed-in" && user !== null && (!user.has_password || stepUp);
  if (!modalOpen && (dismissed || !applies)) return null;

  const dismiss = () => {
    try {
      window.localStorage.setItem(PASSWORD_NUDGE_DISMISSED_KEY, "1");
    } catch {
      // Private mode / blocked storage: the nudge still hides for this page load.
    }
    setDismissed(true);
  };

  return createPortal(
    <div
      role="status"
      style={{
        position: "fixed",
        bottom: "20px",
        left: "20px",
        zIndex: 1000,
        maxWidth: "min(360px, calc(100vw - 40px))",
        background: "var(--surface, #fff)",
        color: "var(--ink, #111)",
        border: "1px solid var(--border, #e5e7eb)",
        borderLeft: "4px solid #2563eb",
        borderRadius: "10px",
        padding: "12px 14px",
        boxShadow: "0 8px 24px -12px rgba(0,0,0,0.4)",
        fontSize: "14px",
        lineHeight: 1.45,
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: "2px" }}>{stepUp ? "Forgot your password?" : "Add a password?"}</div>
      <div style={{ opacity: 0.85 }}>
        {stepUp
          ? "You just signed in with an email link, so you can set a new password now without the old one."
          : "Set a password as a backup way to sign in."}
      </div>
      <div style={{ display: "flex", gap: "8px", marginTop: "10px" }}>
        <button ref={setButtonRef} type="button" onClick={() => setModalOpen(true)} style={primaryStyle}>
          Set one
        </button>
        <button type="button" onClick={dismiss} style={secondaryStyle}>
          Not now
        </button>
      </div>
      {modalOpen && (
        <Suspense fallback={null}>
          <AuthModal triggerRef={setButtonRef} onClose={() => setModalOpen(false)} initialMode="set-password" />
        </Suspense>
      )}
    </div>,
    document.body,
  );
}

const primaryStyle: React.CSSProperties = {
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

const secondaryStyle: React.CSSProperties = {
  background: "transparent",
  color: "var(--muted, #666)",
  border: "1px solid var(--border, #e5e7eb)",
  borderRadius: "6px",
  padding: "6px 12px",
  font: "inherit",
  fontSize: "13px",
  cursor: "pointer",
};

export default PasswordNudge;
