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
    {
      id: "codex",
      label: "Codex (~/.codex/config.toml)",
      text: `[mcp_servers.citevyn]\nurl = "${url}"\nhttp_headers = { Authorization = "Bearer ${key}" }`,
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
  const [created, setCreated] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = () =>
    listKeys().then(setKeys, (e: unknown) => {
      setKeys([]);
      setError(refusal(e));
    });

  useEffect(() => {
    dialogRef.current?.focus();
    void refresh();
    const trigger = triggerRef.current;
    return () => trigger?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on open
  }, [triggerRef]);
  useFocusTrap(dialogRef, { onEscape: onClose });

  const make = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const name = `Key made ${new Date().toISOString().slice(0, 10)}`;
      setCreated((await createKey(name)).key);
      await refresh();
    } catch (e) {
      setError(refusal(e));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (key: ApiKeyView) => {
    setError(null);
    try {
      await revokeKey(key.key_id);
      await refresh();
    } catch (e) {
      setError(refusal(e));
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
              {created}
            </code>
            <button type="button" onClick={() => copy(created)}>
              Copy key
            </button>
            {snippets(created, window.location.origin).map((s) => (
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

        {keys !== null && keys.length === 0 && <p className="drawer-note">No keys yet.</p>}
        <ul style={{ listStyle: "none", padding: 0 }}>
          {(keys ?? []).map((k) => (
            <li key={k.key_id} className="drawer-row">
              <span style={{ flex: 1 }}>
                <span>{k.name}</span>
                <br />
                <code>{k.prefix}…</code>
                {k.last_used_at ? ` · last used ${k.last_used_at.slice(0, 10)}` : " · never used"}
              </span>
              <button type="button" onClick={() => void revoke(k)} aria-label={`Revoke ${k.name}`}>
                Revoke
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>,
    document.body,
  );
}
