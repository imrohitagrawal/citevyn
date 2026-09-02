import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __testOnly,
  bootstrapAuth,
  getAuthSnapshot,
  login,
  logout,
  register,
  subscribeAuth,
} from "./authStore";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === null ? "" : JSON.stringify(body)),
  } as unknown as Response;
}

const ANON_401 = { request_id: "req_1", status: "error", error: { code: "auth_required", message: "Not authenticated." } };

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  __testOnly.setState(__testOnly.initialState);
  __testOnly.resetBootstrapped();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("bootstrapAuth", () => {
  it("resolves to anonymous when GET /v1/auth/me 401s", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(ANON_401, 401));
    await bootstrapAuth();
    expect(getAuthSnapshot()).toEqual({ status: "anonymous", user: null });
  });

  it("resolves to signed-in for a registered user", async () => {
    const user = { request_id: "req_1", user_id: "usr_a", email: "a@example.com", anonymous: false, providers: [], has_password: true };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(user));
    await bootstrapAuth();
    expect(getAuthSnapshot()).toEqual({ status: "signed-in", user });
  });

  it("is idempotent — a second call does not refetch", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(ANON_401, 401));
    await bootstrapAuth();
    await bootstrapAuth();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("fails safe to anonymous (not stuck on loading) when the backend is unreachable", async () => {
    // A raw network failure -- not a clean 401 -- is what happens when the
    // backend is down or offline. getCurrentUser() only normalizes a real
    // 401; this is bootstrapAuth()'s own fallback for everything else.
    // Found via a live browser check against no running backend (ADR-0004
    // PR 8): without this, the store stuck at "loading" and AccountMenu
    // rendered its blank placeholder forever.
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await bootstrapAuth();
    expect(getAuthSnapshot()).toEqual({ status: "anonymous", user: null });
  });
});

describe("login/register", () => {
  it("login sets signed-in state", async () => {
    const user = { request_id: "req_1", user_id: "usr_b", email: "b@example.com", anonymous: false, providers: [], has_password: true };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(user));
    await login("b@example.com", "correct horse battery");
    expect(getAuthSnapshot()).toEqual({ status: "signed-in", user });
  });

  it("register sets signed-in state", async () => {
    const user = { request_id: "req_1", user_id: "usr_c", email: "c@example.com", anonymous: false, providers: [], has_password: true };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(user, 201));
    await register("c@example.com", "correct horse battery");
    expect(getAuthSnapshot()).toEqual({ status: "signed-in", user });
  });

  it("a failed login leaves state unchanged and rejects", async () => {
    // AccountMenu only ever offers the "Sign in" trigger (and so only ever
    // calls login()) once bootstrap has resolved to "anonymous" — seed that
    // reachable precondition rather than the transient "unknown" state,
    // which the 401 interceptor (correctly) treats as eligible to settle
    // to "anonymous" on ANY 401, including this one.
    __testOnly.setState({ status: "anonymous", user: null });
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ request_id: "req_1", status: "error", error: { code: "auth_required", message: "Invalid email or password." } }, 401),
    );
    await expect(login("nobody@example.com", "wrong")).rejects.toThrow();
    expect(getAuthSnapshot()).toEqual({ status: "anonymous", user: null });
  });
});

describe("logout", () => {
  it("drops to anonymous even if the network call fails", async () => {
    __testOnly.setState({ status: "signed-in", user: { request_id: "req_1", user_id: "usr_a", email: "a@example.com", anonymous: false, providers: [], has_password: true } });
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError("network down"));
    await logout();
    expect(getAuthSnapshot()).toEqual({ status: "anonymous", user: null });
  });
});

describe("bootstrapAuth vs login race", () => {
  it("a slow bootstrap response arriving AFTER a fast login does not revert signed-in state", async () => {
    // Reproduces the race a review caught: bootstrapAuth() fires on mount
    // and hits GET /v1/auth/me with whatever cookie existed at that
    // moment. If login() finishes first, the slow bootstrap response --
    // reflecting the PRE-login cookie -- must not silently stomp the
    // signed-in state it set.
    let resolveBootstrapFetch!: (value: Response) => void;
    const bootstrapPromise = new Promise<Response>((resolve) => {
      resolveBootstrapFetch = resolve;
    });
    vi.mocked(fetch).mockReturnValueOnce(bootstrapPromise);

    const bootstrapCall = bootstrapAuth(); // does not resolve yet

    const loggedInUser = {
      request_id: "req_1",
      user_id: "usr_e",
      email: "e@example.com",
      anonymous: false,
      providers: [],
      has_password: true,
    };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(loggedInUser));
    await login("e@example.com", "correct horse battery");
    expect(getAuthSnapshot()).toEqual({ status: "signed-in", user: loggedInUser });

    // NOW the slow bootstrap response lands, reflecting the stale
    // pre-login (anonymous) cookie.
    resolveBootstrapFetch(jsonResponse(ANON_401, 401));
    await bootstrapCall;

    expect(getAuthSnapshot()).toEqual({ status: "signed-in", user: loggedInUser });
  });
});

describe("subscribeAuth", () => {
  it("notifies listeners on state change", async () => {
    const listener = vi.fn();
    const unsubscribe = subscribeAuth(listener);
    const user = { request_id: "req_1", user_id: "usr_d", email: "d@example.com", anonymous: false, providers: [], has_password: true };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(user));
    await login("d@example.com", "correct horse battery");
    expect(listener).toHaveBeenCalled();
    unsubscribe();
  });
});

describe("401 interceptor", () => {
  it("drops signed-in state to anonymous when ANY apiFetch call 401s", async () => {
    __testOnly.setState({
      status: "signed-in",
      user: { request_id: "req_1", user_id: "usr_a", email: "a@example.com", anonymous: false, providers: [], has_password: true },
    });
    // authStore subscribes to api.ts's real onUnauthorized registry at
    // module load (not mocked here) — importing authStore above already
    // registered it. Firing a 401 through a DIFFERENT endpoint than
    // authStore's own (exactSearch, not /v1/auth/*) proves the interceptor
    // is genuinely global, not just authStore catching its own calls.
    const { exactSearch } = await import("./api");
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(ANON_401, 401));
    await expect(exactSearch({ product_area: "claude_code", term: "--model" })).rejects.toThrow();
    expect(getAuthSnapshot()).toEqual({ status: "anonymous", user: null });
  });

  it("does not fire spuriously when already anonymous", async () => {
    __testOnly.setState({ status: "anonymous", user: null });
    const listener = vi.fn();
    const unsubscribe = subscribeAuth(listener);
    const { exactSearch } = await import("./api");
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(ANON_401, 401));
    await expect(exactSearch({ product_area: "claude_code", term: "--model" })).rejects.toThrow();
    // authStore's own interceptor only acts on "signed-in"/"unknown"; a 401
    // while already anonymous should not trigger a redundant notification.
    expect(listener).not.toHaveBeenCalled();
    unsubscribe();
  });
});
