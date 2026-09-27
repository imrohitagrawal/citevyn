/**
 * AlertsDrawer — "what changed" emails (ADR-0005 Phase 7D). Opened from the
 * account menu, only with the access model on.
 *
 * - The weekly digest: an on/off switch. It needs a verified address; an
 *   unverified account is told so instead of failing silently.
 * - Watches (Pro): watch one of the listed vendor pages, or a term (a flag, a
 *   model id, a command), and get an email when it changes. A term's length is
 *   checked at submit, with the text kept (TEST_STRATEGY §8b: no length
 *   attribute clamps a paste). Removing a watch is one click: it is easy to add
 *   back.
 *
 * Results are announced in a polite live region; failures use role="alert".
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useFocusTrap } from "../hooks/useFocusTrap";
import {
  addWatch,
  getDigest,
  listWatches,
  removeWatch,
  setDigest,
  type DigestState,
  type WatchablePage,
  type WatchView,
} from "../lib/alerts";
import { isSubmitKey, questionLength } from "../lib/composerInput";
import { ApiClientError } from "../lib/types";

const FAILED = "That didn't work. Please try again later.";

function refusal(err: unknown): string {
  const code = err instanceof ApiClientError ? err.errorCode() : null;
  const status = err instanceof ApiClientError ? err.status : 0;
  if (code === "plan_required") return "Watch alerts are part of Pro.";
  if (code === "verification_required") return "Verify your email address first.";
  if (code === "too_many_watches") return "You have the most watches allowed. Remove one first.";
  if (status === 422) return "That can't be watched. Check what you entered.";
  return FAILED;
}

export default function AlertsDrawer({
  triggerRef,
  onClose,
}: {
  triggerRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const [digest, setDigestState] = useState<DigestState | null>(null);
  const [watches, setWatches] = useState<WatchView[] | null>(null);
  const [pages, setPages] = useState<WatchablePage[]>([]);
  const [kind, setKind] = useState<"page" | "term">("page");
  const [page, setPage] = useState("");
  const [term, setTerm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  // A failed first load stays reported; run() clears only an action's error.
  const [loadFailed, setLoadFailed] = useState(false);
  const busy = useRef(false);

  useEffect(() => {
    dialogRef.current?.focus();
    getDigest().then(setDigestState, () => setLoadFailed(true));
    listWatches().then(
      (res) => {
        setWatches(res.watches);
        setPages(res.pages);
        setPage(res.pages[0]?.url ?? "");
      },
      () => setLoadFailed(true),
    );
    const trigger = triggerRef.current;
    return () => trigger?.focus();
  }, [triggerRef]);
  useFocusTrap(dialogRef, { onEscape: onClose });

  /** One request at a time; announce the result. */
  const run = async (work: () => Promise<void>) => {
    if (busy.current) {
      setNote("Still sending. Please wait a moment.");
      return;
    }
    busy.current = true;
    setError(null);
    setNote("");
    try {
      await work();
    } catch (e) {
      setError(refusal(e));
    } finally {
      busy.current = false;
    }
  };

  const toggleDigest = () =>
    run(async () => {
      if (!digest) return;
      const next = await setDigest(!digest.subscribed);
      setDigestState(next);
      setNote(next.subscribed ? "The weekly digest is on." : "The weekly digest is off.");
    });

  const add = () =>
    run(async () => {
      const value = kind === "page" ? page : term.trim();
      if (kind === "page" && !value) {
        setError("No pages are available to watch right now.");
        return;
      }
      // Counted the server's way (code points): 40 emoji are 40 characters.
      const n = questionLength(value);
      if (kind === "term" && (n < 2 || n > 64)) {
        setError(`A term is 2 to 64 characters; this one is ${n}.`);
        return;
      }
      const made = await addWatch(kind, value);
      setWatches((prev) =>
        prev?.some((w) => w.watch_id === made.watch_id) ? prev : [...(prev ?? []), made],
      );
      if (kind === "term") setTerm("");
      setNote(`Watching ${made.label}.`);
    });

  const remove = (w: WatchView) =>
    run(async () => {
      try {
        await removeWatch(w.watch_id);
      } catch (e) {
        // Already gone (another tab, or the alert email's unsubscribe link):
        // that is what the user asked for, not an error.
        if (!(e instanceof ApiClientError && e.status === 404)) throw e;
      }
      setWatches((prev) => prev?.filter((x) => x.watch_id !== w.watch_id) ?? prev);
      setNote(`Stopped watching ${w.label}.`);
      dialogRef.current?.focus(); // the removed row and its button are gone
    });

  return createPortal(
    <div
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      className="drawer-backdrop"
    >
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Alerts" tabIndex={-1} className="drawer-panel">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h2 style={{ margin: 0, fontSize: "18px" }}>Alerts</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="drawer-close">
            ×
          </button>
        </div>
        {error && <p role="alert">{error}</p>}
        {loadFailed && (
          <p role="alert">Your alert settings could not be loaded. Please try again later.</p>
        )}
        <p aria-live="polite" className="drawer-note" data-testid="alerts-status">
          {note}
        </p>

        <h3 style={{ fontSize: "15px", margin: "12px 0 4px" }}>Weekly digest</h3>
        {digest && (
          <>
            <label className="drawer-row">
              <input
                type="checkbox"
                checked={digest.subscribed}
                disabled={!digest.verified && !digest.subscribed}
                aria-describedby={!digest.verified && !digest.subscribed ? "digest-verify-note" : undefined}
                onChange={() => void toggleDigest()}
              />
              <span>Email me once a week when the Claude, Codex or Gemini docs change</span>
            </label>
            {!digest.verified && !digest.subscribed && (
              <p id="digest-verify-note" className="drawer-note">
                Verify your email address to get the digest.
              </p>
            )}
          </>
        )}

        <h3 style={{ fontSize: "15px", margin: "16px 0 4px" }}>Watches (Pro)</h3>
        <p className="drawer-note">Get an email when a page you watch, or a flag, model or command, changes.</p>
        <form
          className="drawer-row"
          style={{ flexWrap: "wrap" }}
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void add();
          }}
        >
          <select aria-label="What to watch" value={kind} onChange={(e) => setKind(e.target.value as "page" | "term")}>
            <option value="page">A page</option>
            <option value="term">A term</option>
          </select>
          {kind === "page" ? (
            <select aria-label="Page" value={page} onChange={(e) => setPage(e.target.value)} style={{ flex: 1, minWidth: 0 }}>
              {pages.map((p) => (
                <option key={p.url} value={p.url}>
                  {`${p.product}: ${p.title}`}
                </option>
              ))}
            </select>
          ) : (
            <input
              aria-label="Term to watch"
              value={term}
              placeholder="--permission-mode"
              onChange={(e) => setTerm(e.target.value)}
              // Enter submits the form, but not while an IME is composing: that
              // Enter picks a candidate (TEST_STRATEGY §8b, isSubmitKey).
              onKeyDown={(e) => {
                if (e.key === "Enter" && !isSubmitKey(e)) e.preventDefault();
              }}
              style={{ flex: 1, minWidth: 0 }}
            />
          )}
          <button type="submit">Watch</button>
        </form>

        {watches !== null && watches.length === 0 && <p className="drawer-note">You are not watching anything.</p>}
        <ul style={{ listStyle: "none", padding: 0 }}>
          {(watches ?? []).map((w) => (
            <li key={w.watch_id} className="drawer-row">
              <span style={{ flex: 1, overflowWrap: "anywhere" }}>{w.label}</span>
              <button type="button" onClick={() => void remove(w)} aria-label={`Stop watching ${w.label}`}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>,
    document.body,
  );
}
