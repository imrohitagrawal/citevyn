/**
 * DeleteAccountDialog — delete your own account (#546). Opened from the
 * account menu, with the access model on or off (accounts exist either way).
 *
 * Deleting needs a fresh email confirmation, decided by the server from this
 * session's own row (``password_step_up`` on /me is the same fact): a password
 * alone, or a laptop left signed in, is not enough. So the dialog has three
 * shapes:
 *
 * - no stored address (some GitHub/Google sign-ins): it cannot be confirmed
 *   here, so the person is told to email us;
 * - not confirmed yet: "Email me a link", then come back;
 * - confirmed: what is deleted, and the button that does it.
 *
 * A live subscription is refused by the server with a plain message; this
 * dialog never cancels anything at the payment provider.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { deleteMyAccount, requestMagicLink } from "../lib/authActions";
import { signedOutLocally } from "../lib/authStore";
import { ApiClientError } from "../lib/types";
import type { AuthUserResponse } from "../lib/types";

/** Where a deletion request goes when the dialog cannot do it (privacy page). */
export const DELETION_CONTACT = "rohit@stackclimb.com";

type Stage = "explain" | "link-sent" | "deleting";

function deleteRefusal(err: unknown): string {
  if (err instanceof ApiClientError) {
    const code = err.errorCode();
    // The server's message names the next step (cancel the subscription first).
    if (code === "already_subscribed") return err.message;
  }
  return "Could not delete your account. Nothing was deleted. Try again shortly.";
}

function linkRefusal(err: unknown): string {
  if (err instanceof ApiClientError && err.isRateLimited()) {
    return "A link was sent moments ago. Check your inbox, or try again in a minute.";
  }
  if (err instanceof ApiClientError && err.status === 404) {
    return `Email links are not available right now. To delete your account, email ${DELETION_CONTACT}.`;
  }
  return "Could not send the link. Try again shortly.";
}

export default function DeleteAccountDialog({
  user,
  triggerRef,
  onClose,
  onDeleted,
}: {
  user: AuthUserResponse;
  triggerRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
  /** Fires once the account is gone, before the menu drops to signed-out. */
  onDeleted: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const [stage, setStage] = useState<Stage>("explain");
  // The server is the judge; a 403 here means the confirmation ran out.
  const [confirmed, setConfirmed] = useState(user.password_step_up);
  const [expired, setExpired] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    dialogRef.current?.focus();
    const trigger = triggerRef.current;
    return () => trigger?.focus();
  }, [triggerRef]);
  useFocusTrap(dialogRef, { onEscape: onClose });

  const sendLink = async () => {
    if (!user.email) return;
    setError(null);
    try {
      await requestMagicLink(user.email);
      setStage("link-sent");
    } catch (err) {
      setError(linkRefusal(err));
    }
  };

  const remove = async () => {
    if (stage === "deleting") return;
    setError(null);
    setStage("deleting");
    try {
      await deleteMyAccount();
    } catch (err) {
      setStage("explain");
      if (err instanceof ApiClientError && err.errorCode() === "verification_required") {
        setConfirmed(false);
        setExpired(true);
        return;
      }
      setError(deleteRefusal(err));
      return;
    }
    onDeleted();
    signedOutLocally();
  };

  let body: React.ReactNode;
  if (!user.email) {
    body = (
      <p>
        {`Your account has no email address, so we cannot confirm it is you here. To delete it, email ${DELETION_CONTACT} and say which GitHub or Google account you sign in with.`}
      </p>
    );
  } else if (!confirmed) {
    body =
      stage === "link-sent" ? (
        <p role="status">
          {`We sent a sign-in link to ${user.email}. Open it in this browser, then, within a few minutes, come back to this menu and choose Delete account again.`}
        </p>
      ) : (
        <>
          {expired && <p role="alert">Your confirmation has run out. Get a new link.</p>}
          <p>
            {`To confirm it is you, we will email a sign-in link to ${user.email}. Open it in this browser, then come back here within a few minutes.`}
          </p>
          <button type="button" onClick={() => void sendLink()}>
            Email me a link
          </button>
        </>
      );
  } else {
    body = (
      <>
        <p>
          This deletes your account and everything in it: your conversations, shared links, API keys, watches and
          sign-in methods. It cannot be undone.
        </p>
        <p className="drawer-note">
          Copies in our backups are deleted within 30 days. If you pay for a plan, cancel it first.
        </p>
        <button type="button" onClick={() => void remove()} aria-disabled={stage === "deleting"}>
          {stage === "deleting" ? "Deleting…" : "Delete my account"}
        </button>{" "}
        <button type="button" onClick={onClose}>
          Cancel
        </button>
      </>
    );
  }

  return createPortal(
    <div
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      className="drawer-backdrop"
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Delete account"
        tabIndex={-1}
        className="drawer-panel"
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h2 style={{ margin: 0, fontSize: "18px" }}>Delete account</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="drawer-close">
            ×
          </button>
        </div>
        {body}
        {error && <p role="alert">{error}</p>}
      </div>
    </div>,
    document.body,
  );
}
