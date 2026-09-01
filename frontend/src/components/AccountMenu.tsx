/**
 * AccountMenu — the Header's identity control (ADR-0004 PR 8).
 *
 * Always mounted (it is tiny — a button, a tiny dropdown), unlike
 * ``AuthModal``, which this component lazy-loads on demand: the ADR's
 * "no auth library" decision is paired with "auth ships as a lazy-loaded
 * modal" specifically so the majority of visitors, who never click
 * "Sign in", never pay for the modal's form/validation code.
 */
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useAuth } from "../hooks/useAuth";

const AuthModal = lazy(() => import("./AuthModal"));
const HistoryDrawer = lazy(() => import("./HistoryDrawer"));

interface AccountMenuProps {
  /**
   * Whether the current tab already has chat messages (ADR-0004 PR 9) —
   * threaded down from LandingPage so a successful sign-in can confirm
   * "your conversation is saved" specifically when there was one to
   * save. Optional so existing call sites/tests need no change; treated
   * as `false` when omitted.
   */
  hasChatHistory?: boolean;
  /** Fires once, right after a successful sign-in/register. */
  onAuthenticated?: (hadChatHistory: boolean) => void;
  /** ADR-0004 PR 10: the caller picked a past session from the drawer. */
  onResumeSession?: (sessionId: string) => void;
}

export function AccountMenu({
  hasChatHistory = false,
  onAuthenticated,
  onResumeSession,
}: AccountMenuProps) {
  const { status, user, signOut } = useAuth();
  const [modalOpen, setModalOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close on an outside click — the only close path before this was the
  // "Sign out" action itself, so opening the menu and clicking anywhere
  // else on the page (found by review) left it stuck open.
  useEffect(() => {
    if (!menuOpen) return;
    const handlePointerDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [menuOpen]);

  if (status === "unknown" || status === "loading") {
    // No layout shift once resolved: reserve the space, show nothing yet.
    return <span style={{ width: "84px", display: "inline-block" }} aria-hidden="true" />;
  }

  if (status === "signed-in" && user) {
    return (
      <div ref={menuRef} style={{ position: "relative" }}>
        <button
          ref={triggerRef}
          type="button"
          onClick={() => setMenuOpen((v) => !v)}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          className="theme-toggle"
        >
          {user.email}
        </button>
        {menuOpen && (
          <div
            role="menu"
            style={{
              position: "absolute",
              right: 0,
              top: "calc(100% + 6px)",
              background: "var(--surface, #fff)",
              border: "1px solid var(--border, #e5e7eb)",
              borderRadius: "10px",
              boxShadow: "0 12px 30px -14px rgba(0,0,0,0.4)",
              padding: "6px",
              minWidth: "140px",
              zIndex: 1050,
            }}
          >
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                setHistoryOpen(true);
              }}
              style={menuItemStyle}
            >
              History
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                void signOut();
              }}
              style={menuItemStyle}
            >
              Sign out
            </button>
          </div>
        )}
        {historyOpen && (
          <Suspense fallback={null}>
            <HistoryDrawer
              triggerRef={triggerRef}
              onClose={() => setHistoryOpen(false)}
              onResume={(sessionId) => {
                setHistoryOpen(false);
                onResumeSession?.(sessionId);
              }}
            />
          </Suspense>
        )}
      </div>
    );
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setModalOpen(true)}
        className="theme-toggle"
      >
        Sign in
      </button>
      {modalOpen && (
        <Suspense fallback={null}>
          <AuthModal
            triggerRef={triggerRef}
            onClose={() => setModalOpen(false)}
            onAuthenticated={() => onAuthenticated?.(hasChatHistory)}
          />
        </Suspense>
      )}
    </>
  );
}

const menuItemStyle: React.CSSProperties = {
  display: "block",
  width: "100%",
  textAlign: "left",
  background: "transparent",
  border: "none",
  cursor: "pointer",
  padding: "8px 10px",
  borderRadius: "6px",
  color: "var(--ink, #111)",
  font: "inherit",
};
