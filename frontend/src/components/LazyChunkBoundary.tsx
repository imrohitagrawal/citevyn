/**
 * LazyChunkBoundary (#358) — keeps a failed lazy-chunk fetch from taking the
 * whole page down with it.
 *
 * WHY THIS EXISTS. `React.lazy` rejects with
 * `TypeError: Failed to fetch dynamically imported module: /assets/…js` when
 * the chunk 404s or the connection drops. `<Suspense fallback={null}>` does NOT
 * catch that — Suspense handles the PENDING state, not the REJECTED one — and
 * with no error boundary above it React unmounts the entire root. Reproduced in
 * review on the production build: answering the strip's chunk with a 404 and
 * scrolling took the page from a normal render to `bodyTextLen=0`, a white
 * screen with no message and no recovery short of a manual reload.
 *
 * That is realistic here, not theoretical: a deploy replaces the content-hashed
 * assets while a reader still holds the previous index.html, and this app is
 * deployed by rolling a Fly image. Before #358 the only dynamic imports sat
 * behind deliberate clicks (sign in, open history); the marketing strip is the
 * first one on the AUTOMATIC path of an ordinary visit, so an ordinary scroll
 * now reaches the failure. Widening that exposure without containing it would
 * have traded 3.9 kB for a white screen.
 *
 * WHAT IT DOES ON FAILURE: renders `fallback` (null by default) and lets the
 * rest of the page carry on. For below-the-fold marketing copy that is the
 * right trade — the reader loses the strip, not the product. It is deliberately
 * NOT a retry UI: the sections carry no state and no action the reader is in
 * the middle of, so a reload is the honest recovery and inventing a
 * "something went wrong" panel below the fold would be louder than the loss.
 *
 * It logs via console.error so the failure is visible in a session replay or a
 * bug report rather than being silently swallowed. Note that ONE chunk failure
 * produces TWO log lines in LandingPage: the deferred sections are not
 * contiguous in the DOM, so they mount behind two boundary instances.
 *
 * It also catches a render-time throw from INSIDE a section (a real bug in
 * Pricing, say) and reports it under a label that says "chunk failed". The
 * containment is still what you want there — the rest of the page survives —
 * but read the logged error, not the label, when diagnosing.
 *
 * SCOPE (#447). Every lazy surface in the app now sits behind one of these.
 * The strip (above) and the password nudge's mount in LandingPage are
 * automatic, so they fail quietly like the strip. The five that open on a
 * click — AuthModal, HistoryDrawer and ConnectedAccountsDrawer from
 * AccountMenu, and AuthModal again from the nudge and from the Sign-in methods
 * drawer — pass `onError`, which closes the dialog and tells the reader in
 * plain words (CHUNK_FAILED_MESSAGE). A click that silently does nothing is a
 * failure too. Closing the dialog also unmounts this boundary, so the next
 * click starts from a fresh one and the button is never left dead.
 *
 * NOT a retry. React caches a rejected `lazy()` for the life of the page, so
 * re-keying the boundary would just fail again; each later click is contained
 * the same way, and the message says to reload, which is the real recovery.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

/** What a reader is told when a dialog's code could not be fetched. */
export const CHUNK_FAILED_MESSAGE = "Part of the page didn't load. Reload the page and try again.";

interface Props {
  children: ReactNode;
  /** Rendered instead of `children` once a descendant has thrown. */
  fallback?: ReactNode;
  /** Named in the log line so a report says WHICH chunk failed. */
  label: string;
  /** Called once the failure is caught, e.g. to close the dialog that failed. */
  onError?: () => void;
}

interface State {
  failed: boolean;
}

export class LazyChunkBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Not swallowed. A chunk that never arrives is invisible in the DOM, so
    // without this the only symptom is "the page is short".
    console.error(`[${this.props.label}] lazy chunk failed to render`, error, info.componentStack);
    this.props.onError?.();
  }

  render() {
    return this.state.failed ? this.props.fallback ?? null : this.props.children;
  }
}
