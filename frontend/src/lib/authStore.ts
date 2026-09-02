/**
 * authStore — module-level external store for the caller's identity
 * (ADR-0004 PR 8).
 *
 * A plain subscribe/notify store, not a library: the ADR deliberately
 * rejects an auth library for one modal + one drawer, and this repo's own
 * ``useLandingState`` sets the precedent of hand-rolled state over a
 * dependency. React 18's ``useSyncExternalStore`` (see ``hooks/useAuth.ts``)
 * is the standard, dependency-free way to subscribe a component tree to
 * state that lives outside React — the same shape Redux/Zustand build on
 * top of, without the bundle cost of either for something this small.
 *
 * Only one auth-relevant network path is NOT routed through here:
 * ``resolve_principal`` mints an anonymous cookie transparently on the
 * FIRST session/message call, before this store's ``bootstrap()`` has
 * necessarily resolved. That is fine — this store's ``status`` starts
 * "unknown" and settles to "anonymous" or "signed-in" once ``GET
 * /v1/auth/me`` returns; nothing here blocks the chat UI on that.
 */

import { getCurrentUser, login as apiLogin, logout as apiLogout, onUnauthorized, register as apiRegister } from "./api";
import type { AuthUserResponse } from "./types";
import { ApiClientError } from "./types";

export type AuthStatus = "unknown" | "loading" | "signed-in" | "anonymous";

export interface AuthState {
  status: AuthStatus;
  user: AuthUserResponse | null;
}

const initialState: AuthState = { status: "unknown", user: null };
let state: AuthState = initialState;
const listeners = new Set<() => void>();

function setState(next: AuthState): void {
  state = next;
  for (const listener of listeners) listener();
}

/** For ``useSyncExternalStore``. Do not call outside ``hooks/useAuth.ts``. */
export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** For ``useSyncExternalStore``. Do not call outside ``hooks/useAuth.ts``. */
export function getAuthSnapshot(): AuthState {
  return state;
}

function stateFor(user: AuthUserResponse): AuthState {
  return { status: user.anonymous ? "anonymous" : "signed-in", user };
}

let bootstrapped = false;

// Guards against a real race a review caught: bootstrapAuth() fires on
// mount and hits GET /v1/auth/me with whatever cookie existed at that
// moment. If the user submits the login form before that request
// resolves, login() can finish FIRST (setting signed-in), and the slow
// bootstrap response — reflecting the pre-login cookie — would then land
// and silently stomp signed-in back to anonymous. Every identity-
// resolving call bumps this token; bootstrapAuth() (the only PASSIVE,
// background one) discards its result if a newer call has since started.
// login/register/logout are explicit user actions and always apply their
// own result — they only need to bump the token so a slower, already
// in-flight bootstrap becomes stale relative to them.
let latestIdentityToken = 0;

/**
 * Resolve the caller's current identity. Idempotent and safe to call from
 * every ``useAuth()`` consumer's mount effect (``LandingPage``,
 * ``AccountMenu`` both do) — only the FIRST call in the page's lifetime
 * actually hits the network; the rest are no-ops, because there is
 * exactly one page-load identity to resolve, not one per component.
 */
export async function bootstrapAuth(): Promise<void> {
  if (bootstrapped) return;
  bootstrapped = true;
  const token = ++latestIdentityToken;
  setState({ status: "loading", user: state.user });
  try {
    const user = await getCurrentUser();
    if (token !== latestIdentityToken) return; // superseded by login/register/logout
    setState(user ? stateFor(user) : { status: "anonymous", user: null });
  } catch {
    // getCurrentUser() already normalizes a clean 401 to null; this catches
    // everything else (network failure, backend unreachable, timeout).
    // Fail safe to "anonymous", the same as a genuine 401 — a caller
    // stuck on "loading" forever would leave AccountMenu rendering its
    // blank placeholder permanently, and an unswallowed rejection here
    // would be an unhandled promise rejection (this runs from a fire-
    // and-forget `void bootstrapAuth()` in useAuth's mount effect).
    if (token !== latestIdentityToken) return;
    setState({ status: "anonymous", user: null });
  }
}

/**
 * Apply the identity an explicit user action is about to receive from the
 * server. The token bump happens synchronously, BEFORE awaiting, so a
 * bootstrap already in flight is stale relative to this action whichever
 * order the two responses land in. Shared by login/register and (ADR-0004
 * PR 14) ``lib/authActions.updatePassword``.
 */
export async function applyIdentityFrom(request: Promise<AuthUserResponse>): Promise<void> {
  latestIdentityToken++;
  setState(stateFor(await request));
}

export function login(email: string, password: string): Promise<void> {
  return applyIdentityFrom(apiLogin({ email, password }));
}

export function register(email: string, password: string): Promise<void> {
  return applyIdentityFrom(apiRegister({ email, password }));
}

export async function logout(): Promise<void> {
  latestIdentityToken++;
  try {
    await apiLogout();
  } catch {
    // Swallowed, not just caught-and-rethrown: always drop to signed-out
    // locally even if the network call itself failed (e.g. offline) — the
    // cookie the browser holds may already be stale server-side by the
    // time this runs, and there is no worse outcome than the UI claiming
    // "signed in" when it cannot act on it. Callers (AccountMenu) invoke
    // this as a fire-and-forget `void signOut()`, so a logout() that could
    // still reject would surface as an unhandled promise rejection for a
    // failure mode this function exists specifically to paper over.
  } finally {
    setState({ status: "anonymous", user: null });
  }
}

/** Re-export so ``ApiClientError`` callers (the AuthModal form) don't need a second import. */
export { ApiClientError };

// The 401 interceptor (api.ts) fires for ANY endpoint, not just this
// store's own calls -- e.g. a stale AccountMenu action racing a session
// expiry. Registered once at module load, which runs exactly once per
// PRODUCTION page load (ES modules are singletons there). Under Vite
// HMR in dev, a hot-reload of this file DOES re-run this line against
// api.ts's still-live listener Set, adding a second subscriber -- but
// the resulting duplicate setState({status:"anonymous"}) call is
// idempotent (subscribeAuth's listeners just re-render), so it is a
// harmless dev-only quirk, not a guard this code needs to add.
onUnauthorized(() => {
  if (state.status === "signed-in" || state.status === "unknown") {
    setState({ status: "anonymous", user: null });
  }
});

export const __testOnly = {
  setState,
  initialState,
  resetBootstrapped: () => {
    bootstrapped = false;
  },
};
