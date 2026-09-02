import { beforeEach, describe, expect, it, vi } from "vitest";
import { requestMagicLink, updatePassword } from "./authActions";
import { getAuthSnapshot, __testOnly } from "./authStore";

/**
 * ADR-0004 PR 14: the exact wire shapes the backend contract expects
 * (docs/API_SPEC.md §4c), and that a successful password update applies the
 * refreshed identity to the store.
 */
vi.mock("./api", () => ({
  apiFetch: vi.fn(),
  getCurrentUser: vi.fn(() => Promise.resolve(null)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
}));

const USER = {
  request_id: "req_1",
  user_id: "usr_a",
  email: "a@example.com",
  anonymous: false,
  providers: [],
  has_password: true,
};

beforeEach(() => {
  __testOnly.setState(__testOnly.initialState);
});

describe("authActions", () => {
  it("requestMagicLink POSTs the email to the request route", async () => {
    // RED if the path or body key changes -- the backend would 404/422.
    const { apiFetch } = await import("./api");
    vi.mocked(apiFetch).mockResolvedValueOnce({ request_id: "r", status: "accepted" });
    await requestMagicLink("a@example.com");
    expect(apiFetch).toHaveBeenCalledWith("/v1/auth/magic-link/request", {
      method: "POST",
      body: JSON.stringify({ email: "a@example.com" }),
    });
  });

  it("updatePassword omits current_password entirely on a first-time set", async () => {
    // RED if a first-time set sends current_password: undefined/null -- the
    // server treats presence as irrelevant, but the contract is "absent".
    const { apiFetch } = await import("./api");
    vi.mocked(apiFetch).mockResolvedValueOnce(USER);
    await updatePassword("new passphrase 12345");
    expect(apiFetch).toHaveBeenCalledWith("/v1/auth/me/password", {
      method: "POST",
      body: JSON.stringify({ new_password: "new passphrase 12345" }),
    });
  });

  it("updatePassword sends both fields on a change and applies the refreshed identity", async () => {
    // RED if applyIdentity is not called: has_password in the store would
    // stay stale and the nudge / drawer would keep offering "Set a password".
    const { apiFetch } = await import("./api");
    __testOnly.setState({ status: "signed-in", user: { ...USER, has_password: false } });
    vi.mocked(apiFetch).mockResolvedValueOnce(USER);
    await updatePassword("new passphrase 12345", "old passphrase");
    expect(apiFetch).toHaveBeenCalledWith("/v1/auth/me/password", {
      method: "POST",
      body: JSON.stringify({ current_password: "old passphrase", new_password: "new passphrase 12345" }),
    });
    expect(getAuthSnapshot()).toEqual({ status: "signed-in", user: USER });
  });
});
