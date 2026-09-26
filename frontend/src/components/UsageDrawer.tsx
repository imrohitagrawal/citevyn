/**
 * UsageDrawer — your answered questions this month against your allowance
 * (ADR-0005 Phase 6D-2, "usage insights"). Opened from the account menu, only
 * with the access model on. Pro; a Free account is told so.
 *
 * The numbers come straight from `GET /v1/me/usage`, whose days add up to the
 * allowance's count, so this never shows a total the meter disagrees with.
 * A table, not a chart: every number is readable by a screen reader, and the
 * bars are decoration beside them.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { formatDocsDate } from "../lib/answerMarkdown";
import { getUsage, type UsageInsights } from "../lib/usage";
import { ApiClientError } from "../lib/types";

const n = (v: number) => v.toLocaleString("en-US");

function refusal(err: unknown): string {
  const code = err instanceof ApiClientError ? err.errorCode() : null;
  if (code === "plan_required") return "Usage insights are part of Pro.";
  return "Could not load your usage. Please try again later.";
}

export default function UsageDrawer({
  triggerRef,
  onClose,
}: {
  triggerRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const [data, setData] = useState<UsageInsights | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    dialogRef.current?.focus();
    getUsage().then(setData, (e: unknown) => setError(refusal(e)));
    const trigger = triggerRef.current;
    return () => trigger?.focus();
  }, [triggerRef]);
  useFocusTrap(dialogRef, { onEscape: onClose });

  const used = data ? data.days.filter((d) => d.chat + d.mcp > 0) : [];
  const busiest = Math.max(1, ...used.map((d) => d.chat + d.mcp));

  return createPortal(
    <div
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      className="drawer-backdrop"
    >
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Usage" tabIndex={-1} className="drawer-panel">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h2 style={{ margin: 0, fontSize: "18px" }}>Usage</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="drawer-close">
            ×
          </button>
        </div>
        {error && <p role="alert">{error}</p>}
        {!data && !error && <p className="drawer-note">Loading…</p>}
        {data && (
          <>
            <p data-testid="usage-summary">
              {`${n(data.usage.used)} of ${n(data.usage.limit)} answered questions used this month`}
              {data.usage.resets_at ? `; resets ${formatDocsDate(data.usage.resets_at.slice(0, 10))}.` : "."}
            </p>
            <p className="drawer-note" data-testid="usage-channels">
              {`In the chat: ${n(data.totals.chat)}. From AI agents (MCP): ${n(data.totals.mcp)}.`}
            </p>
            {used.length === 0 ? (
              <p className="drawer-note">No answered questions yet this month.</p>
            ) : (
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <caption className="drawer-note" style={{ textAlign: "left" }}>
                  Days with answered questions (UTC)
                </caption>
                <thead>
                  <tr>
                    <th scope="col" style={{ textAlign: "left" }}>
                      Day
                    </th>
                    <th scope="col">Chat</th>
                    <th scope="col">MCP</th>
                    {/* The bar column is decoration: hidden from screen readers,
                        header and cells alike, so no one hears a column they
                        cannot reach. The numbers carry the meaning. */}
                    <th aria-hidden="true" />
                  </tr>
                </thead>
                <tbody>
                  {used.map((d) => (
                    <tr key={d.date}>
                      <th scope="row" style={{ textAlign: "left", fontWeight: "normal" }}>
                        {formatDocsDate(d.date)}
                      </th>
                      <td style={{ textAlign: "center" }}>{n(d.chat)}</td>
                      <td style={{ textAlign: "center" }}>{n(d.mcp)}</td>
                      <td style={{ width: "40%" }} aria-hidden="true">
                        <span
                          className="usage-bar"
                          style={{ width: `${Math.round(((d.chat + d.mcp) / busiest) * 100)}%` }}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </div>,
    document.body,
  );
}
