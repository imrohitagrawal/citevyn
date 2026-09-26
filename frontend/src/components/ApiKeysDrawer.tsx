/**
 * ApiKeysDrawer — your API keys and how to use them with an AI agent over MCP
 * (ADR-0005 Phase 6B-2). Opened from the account menu, only with the access
 * model on.
 *
 * A key is made with an automatic name, so there is no text input (TEST_STRATEGY
 * §8b). The full key is shown ONCE, with a copy button and the install commands
 * already filled in; after that only its first characters are listed.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { createKey, listKeys, revokeKey, type ApiKeyView } from "../lib/apiKeys";
import { ApiClientError } from "../lib/types";

function refusal(err: unknown): string {
  const code = err instanceof ApiClientError ? err.errorCode() : null;
  if (code === "plan_required") return "API keys are part of Pro. Upgrade to make one.";
  if (code === "too_many_api_keys") return "You have the most keys allowed. Revoke one first.";
  return "That didn't work. Please try again later.";
}

function snippets(key: string, origin: string): { id: string; label: string; text: string }[] {
  const url = `${origin}/v1/mcp`;
  return [
    {
      id: "claude",
      label: "Claude Code",
      text: `claude mcp add --transport http citevyn ${url} --header "Authorization: Bearer ${key}"`,
    },
    // Codex reads the key from an environment variable (its documented
    // bearer_token_env_var), so the key is never written into config.toml.
    // Two snippets, because they are pasted into two different places.
    {
      id: "codex-env",
      label: "Codex, step 1: add to your shell profile (for example ~/.zshrc)",
      text: `export CITEVYN_API_KEY=${key}`,
    },
    {
      id: "codex",
      label: "Codex, step 2: add to ~/.codex/config.toml",
      text: `[mcp_servers.citevyn]\nurl = "${url}"\nbearer_token_env_var = "CITEVYN_API_KEY"`,
    },
  ];
}

export default function ApiKeysDrawer({
  triggerRef,
  onClose,
}: {
  triggerRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const [keys, setKeys] = useState<ApiKeyView[] | null>(null);
  const [created, setCreated] = useState<{ key_id: string; key: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [listFailed, setListFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);

  // A failed list keeps what was shown and says so; it never claims "No keys yet".
  const refresh = () =>
    listKeys().then(
      (next) => {
        setKeys(next);
        setListFailed(false);
      },
      (e: unknown) => {
        setListFailed(true);
        setError(refusal(e));
      },
    );

  useEffect(() => {
    dialogRef.current?.focus();
    void refresh();
    const trigger = triggerRef.current;
    return () => trigger?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on open
  }, [triggerRef]);
  useFocusTrap(dialogRef, { onEscape: onClose });

  const make = async () => {
    if (busy) return; // one make at a time
    setBusy(true);
    setConfirming(null); // a make cancels an armed revoke
    setError(null);
    try {
      const name = `Key made ${new Date().toISOString().slice(0, 10)}`;
      const made = await createKey(name);
      setCreated({ key_id: made.key_id, key: made.key });
      await refresh();
    } catch (e) {
      setError(refusal(e));
    } finally {
      setBusy(false);
    }
  };

  // Two clicks: "Revoke" asks, "Really revoke" does it. One request at a time.
  const revoke = async (key: ApiKeyView) => {
    if (busy) return; // one revoke or make at a time
    if (confirming !== key.key_id) {
      setConfirming(key.key_id);
      return;
    }
    setBusy(true);
    setConfirming(null);
    setError(null);
    try {
      await revokeKey(key.key_id);
      if (created?.key_id === key.key_id) setCreated(null);
      // Gone even if the refresh below fails, so it cannot be revoked twice.
      setKeys((prev) => prev?.filter((k) => k.key_id !== key.key_id) ?? prev);
      await refresh();
    } catch (e) {
      setError(refusal(e));
    } finally {
      setBusy(false);
      dialogRef.current?.focus(); // the revoked row and its button are gone
    }
  };

  const copy = (text: string) => {
    void navigator.clipboard?.writeText(text).catch(() => undefined);
  };

  return createPortal(
    <div
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      className="drawer-backdrop"
    >
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="API keys" tabIndex={-1} className="drawer-panel">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h2 style={{ margin: 0, fontSize: "18px" }}>API keys</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="drawer-close">
            ×
          </button>
        </div>
        <p className="drawer-note">
          Use a key to ask CiteVyn from an AI agent (Claude Code, Codex) over MCP. Answers
          count against your plan.
        </p>

        {created && (
          <div className="drawer-row" style={{ flexDirection: "column", alignItems: "stretch" }}>
            <strong>Your new key. This is the only time it is shown.</strong>
            <code data-testid="new-key" style={{ wordBreak: "break-all" }}>
              {created.key}
            </code>
            <button type="button" onClick={() => copy(created.key)}>
              Copy key
            </button>
            {snippets(created.key, window.location.origin).map((s) => (
              <div key={s.id}>
                <p style={{ margin: "8px 0 4px" }}>{s.label}</p>
                <pre data-testid={`snippet-${s.id}`} style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
                  {s.text}
                </pre>
                <button type="button" onClick={() => copy(s.text)}>
                  Copy
                </button>
              </div>
            ))}
          </div>
        )}

        <button type="button" onClick={() => void make()} aria-disabled={busy}>
          Make a key
        </button>
        {error && <p role="alert">{error}</p>}

        {keys !== null && keys.length === 0 && !listFailed && <p className="drawer-note">No keys yet.</p>}
        <ul style={{ listStyle: "none", padding: 0 }}>
          {(keys ?? []).map((k) => (
            <li key={k.key_id} className="drawer-row">
              <span style={{ flex: 1 }}>
                <span>{k.name}</span>
                <br />
                <code>{k.prefix}…</code>
                {k.last_used_at ? ` · last used ${k.last_used_at.slice(0, 10)}` : " · never used"}
              </span>
              <button
                type="button"
                onClick={() => void revoke(k)}
                aria-disabled={busy}
                aria-label={confirming === k.key_id ? `Really revoke ${k.name}` : `Revoke ${k.name}`}
              >
                {confirming === k.key_id ? "Really revoke?" : "Revoke"}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>,
    document.body,
  );
}
