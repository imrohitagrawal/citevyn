import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AccountMenu } from "./AccountMenu";
import { __testOnly } from "../lib/authStore";

/**
 * #447: what happens when a dialog's lazy chunk never arrives.
 *
 * The realistic cause is a deploy while a reader holds an open tab: the
 * content-hashed chunk their index.html names is gone, so the import rejects.
 * `<Suspense>` does not catch a REJECTED lazy import, only a pending one, and
 * with no error boundary above it React unmounts the whole root — the page
 * goes blank and the on-screen transcript is lost.
 *
 * SEPARATE FILE ON PURPOSE. The three lazy modules are mocked to reject for the
 * whole file, and React caches a rejected lazy for the lifetime of the module
 * instance, so these cannot share a file with the tests that open the dialogs
 * for real. Vitest isolates modules per file.
 *
 * Each test turns RED if the LazyChunkBoundary around ITS Suspense in
 * AccountMenu.tsx is removed (the sibling disappears with the root), or if the
 * boundary's onError stops resetting the open state / reporting the failure.
 */
const CHUNK_404 = "Failed to fetch dynamically imported module: /assets/gone.js";

vi.mock("./AuthModal", () => {
  throw new Error(CHUNK_404);
});
vi.mock("./HistoryDrawer", () => {
  throw new Error(CHUNK_404);
});
vi.mock("./ConnectedAccountsDrawer", () => {
  throw new Error(CHUNK_404);
});
vi.mock("./ApiKeysDrawer", () => {
  throw new Error(CHUNK_404);
});
vi.mock("./SharedLinksDrawer", () => {
  throw new Error(CHUNK_404);
});
// The access model on, so the API keys and Shared links items exist.
vi.mock("../lib/clientConfig", () => ({ useClientConfig: () => ({ access_model: true }) }));

const SIGNED_IN_USER = {
  request_id: "req_1",
  user_id: "usr_a",
  email: "a@example.com",
  anonymous: false,
  providers: [],
  has_password: true,
  password_step_up: false,
};

let currentUser: typeof SIGNED_IN_USER | null = null;

vi.mock("../lib/api", () => ({
  getCurrentUser: vi.fn(() => Promise.resolve(currentUser)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(() => Promise.resolve()),
  onUnauthorized: vi.fn(() => () => {}),
}));

let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  // React logs the caught error, and so does the boundary. Silence both so an
  // expected failure does not look like a broken run; the log is asserted below.
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  __testOnly.resetBootstrapped();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderWithSibling(onOpenFailed: () => void) {
  render(
    <div>
      <p>the transcript so far</p>
      <AccountMenu onOpenFailed={onOpenFailed} />
    </div>,
  );
}

/** Wait until React has seen the rejected import (it logs either way). */
async function chunkRejected() {
  await waitFor(() => expect(errorSpy).toHaveBeenCalled());
}

function boundaryLogs(label: string) {
  return errorSpy.mock.calls.filter((c: unknown[]) => String(c[0]).includes(`[${label}]`));
}

describe("AccountMenu: a dialog chunk that fails to load (#447)", () => {
  it("Sign in: the page survives, the button still works, and the reader is told", async () => {
    currentUser = null;
    __testOnly.setState({ status: "anonymous", user: null });
    const onOpenFailed = vi.fn();
    const user = userEvent.setup();
    renderWithSibling(onOpenFailed);

    const trigger = await screen.findByRole("button", { name: "Sign in" });
    await user.click(trigger);
    await chunkRejected();
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(boundaryLogs("auth-modal").length).toBeGreaterThan(0);

    // A second click must not blank the page either: contained again.
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledTimes(2));
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("History: the page survives, the account button still works, and the reader is told", async () => {
    currentUser = SIGNED_IN_USER;
    __testOnly.setState({ status: "signed-in", user: SIGNED_IN_USER });
    const onOpenFailed = vi.fn();
    const user = userEvent.setup();
    renderWithSibling(onOpenFailed);

    const account = await screen.findByRole("button", { name: "a@example.com" });
    await user.click(account);
    await user.click(screen.getByRole("menuitem", { name: "History" }));
    await chunkRejected();
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(boundaryLogs("history-drawer").length).toBeGreaterThan(0);
    // Focus goes back to the account button: the menuitem that was clicked
    // has been unmounted with the menu.
    expect(screen.getByRole("button", { name: "a@example.com" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "a@example.com" }));
    await user.click(screen.getByRole("menuitem", { name: "History" }));
    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledTimes(2));
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
  });

  it("Sign-in methods: the page survives, the account button still works, and the reader is told", async () => {
    currentUser = SIGNED_IN_USER;
    __testOnly.setState({ status: "signed-in", user: SIGNED_IN_USER });
    const onOpenFailed = vi.fn();
    const user = userEvent.setup();
    renderWithSibling(onOpenFailed);

    const account = await screen.findByRole("button", { name: "a@example.com" });
    await user.click(account);
    await user.click(screen.getByRole("menuitem", { name: "Sign-in methods" }));
    await chunkRejected();
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(boundaryLogs("connected-accounts").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "a@example.com" }));
    await user.click(screen.getByRole("menuitem", { name: "Sign-in methods" }));
    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledTimes(2));
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
  });

  it.each([
    ["API keys", "api-keys"],
    ["Shared links", "shared-links"],
  ])("%s: the page survives and the menu item works a second time", async (item, label) => {
    // Turns red if: openFailed does not reset this drawer's open state (the
    // second click then does nothing), or its boundary is removed.
    currentUser = SIGNED_IN_USER;
    __testOnly.setState({ status: "signed-in", user: SIGNED_IN_USER });
    const onOpenFailed = vi.fn();
    const user = userEvent.setup();
    renderWithSibling(onOpenFailed);

    await user.click(await screen.findByRole("button", { name: "a@example.com" }));
    await user.click(screen.getByRole("menuitem", { name: item }));
    await chunkRejected();
    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledTimes(1));
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
    expect(boundaryLogs(label).length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "a@example.com" }));
    await user.click(screen.getByRole("menuitem", { name: item }));
    await waitFor(() => expect(onOpenFailed).toHaveBeenCalledTimes(2));
  });
});
