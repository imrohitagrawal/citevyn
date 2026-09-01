/**
 * AccountMenu — the Header's identity control (ADR-0004 PR 8).
 *
 * Always mounted (it is tiny — a button, a tiny dropdown), unlike
 * ``AuthModal``, which this component lazy-loads on demand: the ADR's
 * "no auth library" decision is paired with "auth ships as a lazy-loaded
 * modal" specifically so the majority of visitors, who never click
 * "Sign in", never pay for the modal's form/validation code.
 */
import { lazy, Suspense, useRef, useState } from "react";
import { useAuth } from "../hooks/useAuth";

const AuthModal = lazy(() => import("./AuthModal"));

export function AccountMenu() {
  const { status, user, signOut } = useAuth();
  const [modalOpen, setModalOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);

  if (status === "unknown" || status === "loading") {
    // No layout shift once resolved: reserve the space, show nothing yet.
    return <span style={{ width: "84px", display: "inline-block" }} aria-hidden="true" />;
  }

  if (status === "signed-in" && user) {
    return (
      <div style={{ position: "relative" }}>
        <button
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
                void signOut();
              }}
              style={{
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
              }}
            >
              Sign out
            </button>
          </div>
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
          <AuthModal triggerRef={triggerRef} onClose={() => setModalOpen(false)} />
        </Suspense>
      )}
    </>
  );
}
