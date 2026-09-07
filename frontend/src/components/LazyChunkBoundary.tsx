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
 * It logs once via console.error so the failure is visible in a session replay
 * or a bug report rather than being silently swallowed.
 *
 * SCOPE. Applied to the #358 strip only. The four older lazy surfaces
 * (AuthModal, HistoryDrawer, ConnectedAccountsDrawer, Nudge) have the same
 * unguarded shape; that is pre-existing and is tracked separately rather than
 * being rewritten here.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** Rendered instead of `children` once a descendant has thrown. */
  fallback?: ReactNode;
  /** Named in the log line so a report says WHICH chunk failed. */
  label: string;
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
  }

  render() {
    return this.state.failed ? this.props.fallback ?? null : this.props.children;
  }
}
