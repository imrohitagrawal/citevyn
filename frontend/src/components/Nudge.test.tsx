import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PASSWORD_NUDGE_DISMISSED_KEY, PasswordNudge } from "./Nudge";
import { __testOnly, getAuthSnapshot } from "../lib/authStore";

/**
 * ADR-0004 PR 14. Each test names the change that turns it red.
 */
const PASSWORDLESS = {
  request_id: "req_1",
  user_id: "usr_a",
  email: "a@example.com",
  anonymous: false,
  providers: ["google"],
  has_password: false,
password_step_up: false,
};

vi.mock("../lib/api", () => ({
  API_BASE_URL: "",
  getCurrentUser: vi.fn(() => Promise.resolve(PASSWORDLESS)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
}));

// jsdom's Storage is shadowed by Node's own experimental `localStorage` global
// under vitest here (it reports "not available"), so install a tiny in-memory
// Storage the component reads through `window.localStorage`.
function memoryStorage(): Storage {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, String(value)),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: (index: number) => [...store.keys()][index] ?? null,
    get length() {
      return store.size;
    },
  } as Storage;
}

beforeEach(async () => {
  Object.defineProperty(window, "localStorage", { value: memoryStorage(), configurable: true });
  const { getCurrentUser } = await import("../lib/api");
  vi.mocked(getCurrentUser).mockResolvedValue(PASSWORDLESS);
  __testOnly.setState({ status: "signed-in", user: PASSWORDLESS });
  __testOnly.resetBootstrapped();
});

afterEach(() => {
  cleanup();
});

describe("PasswordNudge", () => {
  it("shows a generic role=status prompt for a signed-in user without a password", async () => {
    // RED if the card is rendered as role="alert", or if the copy assumes a
    // magic-link login (the trigger fires for OAuth logins too).
    render(<PasswordNudge />);
    const card = await screen.findByRole("status");
    expect(card).toHaveTextContent("Set a password as a backup way to sign in.");
    expect(card).not.toHaveTextContent(/email/i);
    expect(within(card).getByRole("button", { name: "Set one" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Not now" })).toBeInTheDocument();
  });

  it("renders nothing once the user has a password", async () => {
    // RED if the has_password check is dropped -- the nudge would nag forever.
    // useAuth's mount effect re-bootstraps identity (status -> "loading"),
    // so the assertion must wait for the store to SETTLE on signed-in; an
    // early assertion would pass vacuously while status is still loading
    // (review finding: the first version of this test did exactly that).
    const { getCurrentUser } = await import("../lib/api");
    const withPassword = { ...PASSWORDLESS, has_password: true, password_step_up: false };
    vi.mocked(getCurrentUser).mockResolvedValue(withPassword);
    __testOnly.setState({ status: "signed-in", user: withPassword });
    render(<PasswordNudge />);
    await waitFor(() => expect(getAuthSnapshot()).toEqual({ status: "signed-in", user: withPassword }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("renders nothing for an anonymous visitor", async () => {
    const { getCurrentUser } = await import("../lib/api");
    vi.mocked(getCurrentUser).mockResolvedValue(null);
    __testOnly.setState({ status: "anonymous", user: null });
    render(<PasswordNudge />);
    await waitFor(() => expect(getAuthSnapshot().status).toBe("anonymous"));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("'Not now' hides it and remembers the dismissal on this device", async () => {
    // RED if the dismissal is not persisted: the next page load would nag again.
    const user = userEvent.setup();
    const { unmount } = render(<PasswordNudge />);
    await user.click(await screen.findByRole("button", { name: "Not now" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(window.localStorage.getItem(PASSWORD_NUDGE_DISMISSED_KEY)).toBe("1");

    unmount();
    render(<PasswordNudge />);
    await Promise.resolve();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("'Set one' opens the auth modal directly in set-password mode", async () => {
    // RED if the modal opens in the default login mode.
    const user = userEvent.setup();
    render(<PasswordNudge />);
    await user.click(await screen.findByRole("button", { name: "Set one" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Set a password" })).toBeInTheDocument();
    expect(within(dialog).getByLabelText("New password")).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Email")).not.toBeInTheDocument();
  });
});

describe("PasswordNudge after a magic-link sign-in (ADR-0004 PR 15, #293)", () => {
  it("offers 'Forgot your password?' when the account has a password but the session is stepped up", async () => {
    // RED if the card keys on has_password alone: the forgotten-password user
    // would never learn about the one-shot path.
    const { getCurrentUser } = await import("../lib/api");
    const stepUp = { ...PASSWORDLESS, has_password: true, password_step_up: true };
    vi.mocked(getCurrentUser).mockResolvedValue(stepUp);
    __testOnly.setState({ status: "signed-in", user: stepUp });
    render(<PasswordNudge />);
    const card = await screen.findByRole("status");
    expect(card).toHaveTextContent("Forgot your password?");
    expect(card).toHaveTextContent(/without the old one/);
  });

  it("stays hidden for a password account whose session is NOT stepped up", async () => {
    const { getCurrentUser } = await import("../lib/api");
    const plain = { ...PASSWORDLESS, has_password: true, password_step_up: false };
    vi.mocked(getCurrentUser).mockResolvedValue(plain);
    __testOnly.setState({ status: "signed-in", user: plain });
    render(<PasswordNudge />);
    await waitFor(() => expect(getAuthSnapshot()).toEqual({ status: "signed-in", user: plain }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows 'Forgot your password?' even when an old 'Add a password?' dismissal is stored", async () => {
    // RED if the persisted dismissal also suppresses the recovery card
    // (review finding: the owner's only recovery path would vanish forever).
    window.localStorage.setItem(PASSWORD_NUDGE_DISMISSED_KEY, "1");
    const { getCurrentUser } = await import("../lib/api");
    const stepUp = { ...PASSWORDLESS, has_password: true, password_step_up: true };
    vi.mocked(getCurrentUser).mockResolvedValue(stepUp);
    __testOnly.setState({ status: "signed-in", user: stepUp });
    const user = userEvent.setup();
    render(<PasswordNudge />);
    const card = await screen.findByRole("status");
    expect(card).toHaveTextContent("Forgot your password?");
    // "Not now" on this variant hides it for the page load without touching storage.
    await user.click(within(card).getByRole("button", { name: "Not now" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(window.localStorage.getItem(PASSWORD_NUDGE_DISMISSED_KEY)).toBe("1");
  });

  it("shows the plain 'Add a password?' copy for a passwordless account on a link session", async () => {
    const { getCurrentUser } = await import("../lib/api");
    const passwordlessStepUp = { ...PASSWORDLESS, has_password: false, password_step_up: true };
    vi.mocked(getCurrentUser).mockResolvedValue(passwordlessStepUp);
    __testOnly.setState({ status: "signed-in", user: passwordlessStepUp });
    render(<PasswordNudge />);
    const card = await screen.findByRole("status");
    expect(card).toHaveTextContent("Add a password?");
    expect(card).not.toHaveTextContent("Forgot");
  });
});
