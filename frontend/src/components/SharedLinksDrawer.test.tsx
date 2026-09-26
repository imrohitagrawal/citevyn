/**
 * ADR-0005 Phase 6C-2: the Shared links drawer. Lists your shared answers with
 * their links; stopping a share asks first. Each test says which change turns
 * it red.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SharedLinksDrawer from "./SharedLinksDrawer";
import { listShares, revokeShare } from "../lib/shares";

vi.mock("../lib/shares", async (orig) => ({
  ...(await orig<typeof import("../lib/shares")>()),
  listShares: vi.fn(),
  revokeShare: vi.fn(),
}));

const ID = "0123456789abcdef0123456789abcdef";
const SHARE = {
  share_id: ID,
  url: `/s/${ID}`,
  question: "How do I set the model?",
  created_at: "2026-09-26T10:00:00+00:00",
};
const OTHER = { ...SHARE, share_id: "f".repeat(32), url: `/s/${"f".repeat(32)}`, question: "Other one?" };

function open() {
  const trigger = { current: document.createElement("button") };
  return render(<SharedLinksDrawer triggerRef={trigger} onClose={vi.fn()} />);
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

async function stop(question: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: `Stop sharing: ${question}` }));
  await user.click(await screen.findByRole("button", { name: `Really stop sharing: ${question}` }));
}

describe("SharedLinksDrawer", () => {
  it("lists each share with its question, date and link", async () => {
    // Turns red if: the question, the date or the link is missing.
    vi.mocked(listShares).mockResolvedValue([SHARE]);
    open();
    expect(await screen.findByText(SHARE.question)).toBeTruthy();
    expect(screen.getByText(/Shared 2026-09-26/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open" }).getAttribute("href")).toBe(
      `${window.location.origin}/s/${ID}`,
    );
  });

  it("says who can see a link, and that deleting a chat does not remove it", async () => {
    // Turns red if: the drawer stops explaining what a shared link exposes.
    vi.mocked(listShares).mockResolvedValue([]);
    open();
    const dialog = await screen.findByRole("dialog", { name: "Shared links" });
    expect(dialog.textContent).toMatch(/anyone with one of these links can see/i);
    expect(dialog.textContent).toMatch(/deleting the chat does not remove the link/i);
  });

  it("stops sharing only after a second, confirming click", async () => {
    // Turns red if: the first click revokes, or the second does not.
    vi.mocked(revokeShare).mockResolvedValue();
    vi.mocked(listShares).mockResolvedValueOnce([SHARE]).mockResolvedValueOnce([]);
    open();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: `Stop sharing: ${SHARE.question}` }));
    expect(revokeShare).not.toHaveBeenCalled();
    expect(screen.getByTestId("drawer-status").textContent).toBe("Press again to stop sharing.");
    await user.click(await screen.findByRole("button", { name: `Really stop sharing: ${SHARE.question}` }));
    expect(revokeShare).toHaveBeenCalledWith(ID);
    expect(await screen.findByText(/not shared any answers/i)).toBeTruthy();
  });

  it("a failed list says so, and never claims nothing is shared", async () => {
    // Turns red if: a list failure shows the empty-state line.
    vi.mocked(listShares).mockRejectedValue(new Error("down"));
    open();
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText(/not shared any answers/i)).toBeNull();
  });

  it("a stopped share leaves the list even when the refresh fails", async () => {
    // Turns red if: the stopped row stays and can be revoked twice.
    vi.mocked(revokeShare).mockResolvedValue();
    vi.mocked(listShares).mockResolvedValueOnce([SHARE]).mockRejectedValueOnce(new Error("down"));
    open();
    await stop(SHARE.question);
    // The stop worked, so the alert must not say it did not.
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Stopped sharing, but the list could not be refreshed.",
    );
    expect(screen.queryByRole("button", { name: `Stop sharing: ${SHARE.question}` })).toBeNull();
  });

  it("while one stop is running, another cannot start", async () => {
    // Turns red if: the busy guard is dropped.
    vi.mocked(listShares).mockResolvedValue([SHARE, OTHER]);
    vi.mocked(revokeShare).mockReturnValue(new Promise(() => undefined));
    open();
    const user = userEvent.setup();
    await stop(SHARE.question);
    await user.click(screen.getByRole("button", { name: `Stop sharing: ${OTHER.question}` }));
    const again = screen.queryByRole("button", { name: `Really stop sharing: ${OTHER.question}` });
    if (again) await user.click(again);
    expect(revokeShare).toHaveBeenCalledTimes(1);
  });

  it("after a stop, focus stays in the drawer", async () => {
    // Turns red if: focus falls to the page when the row disappears.
    vi.mocked(revokeShare).mockResolvedValue();
    vi.mocked(listShares).mockResolvedValueOnce([SHARE]).mockResolvedValueOnce([]);
    open();
    await stop(SHARE.question);
    await screen.findByText(/not shared any answers/i);
    const dialog = screen.getByRole("dialog", { name: "Shared links" });
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("a share whose url is not /s/<id> gets no link or copy button", async () => {
    // Turns red if: the link is built from the server's url without the check.
    vi.mocked(listShares).mockResolvedValue([{ ...SHARE, url: "https://evil.example/x" }]);
    open();
    await screen.findByText(SHARE.question);
    expect(screen.queryByRole("link", { name: "Open" })).toBeNull();
    expect(screen.queryByRole("button", { name: /copy link/i })).toBeNull();
    expect(screen.getByRole("button", { name: `Stop sharing: ${SHARE.question}` })).toBeTruthy(); // partner
  });
});


describe("review round", () => {
  it("a stop the server refuses keeps the row and says so", async () => {
    // Turns red if: the row is dropped before the server confirms (the link would
    // look gone while still public), or the failure is silent.
    vi.mocked(listShares).mockResolvedValue([SHARE]);
    vi.mocked(revokeShare).mockRejectedValue(new Error("down"));
    open();
    await stop(SHARE.question);
    expect((await screen.findByRole("alert")).textContent).toBe("That didn't work. Please try again later.");
    expect(screen.getByRole("button", { name: `Stop sharing: ${SHARE.question}` })).toBeTruthy();
    expect(listShares).toHaveBeenCalledTimes(1);
  });

  it("Copy link copies the full link and says so", async () => {
    // Turns red if: the button is missing, copies the relative path, or is silent.
    vi.mocked(listShares).mockResolvedValue([SHARE]);
    open();
    const user = userEvent.setup();
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await user.click(await screen.findByRole("button", { name: `Copy link: ${SHARE.question}` }));
    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/s/${ID}`);
    expect(screen.getByTestId("drawer-status").textContent).toBe("Link copied.");
  });

  it("a copy that fails says so", async () => {
    // Turns red if: a blocked clipboard is reported as copied.
    vi.mocked(listShares).mockResolvedValue([SHARE]);
    open();
    const user = userEvent.setup();
    const writeText = vi.fn(() => Promise.reject(new Error("blocked")));
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await user.click(await screen.findByRole("button", { name: `Copy link: ${SHARE.question}` }));
    expect(screen.getByTestId("drawer-status").textContent).toBe("Could not copy the link. Use Open instead.");
  });
});
