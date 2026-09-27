/**
 * ADR-0005 Phase 7D: the Alerts drawer (weekly digest + watches).
 * Each test says which change turns it red.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AlertsDrawer from "./AlertsDrawer";
import { addWatch, getDigest, listWatches, removeWatch, setDigest } from "../lib/alerts";
import { ApiClientError } from "../lib/types";

vi.mock("../lib/alerts", () => ({
  getDigest: vi.fn(),
  setDigest: vi.fn(),
  listWatches: vi.fn(),
  addWatch: vi.fn(),
  removeWatch: vi.fn(),
}));

const PAGES = [
  { url: "https://platform.claude.com/docs/en/api/overview.md", title: "API overview", product: "the Claude API" },
  { url: "https://code.claude.com/docs/en/settings.md", title: "Settings", product: "Claude Code" },
];
const WATCH = {
  watch_id: "w1",
  kind: "term" as const,
  value: "--permission-mode",
  label: "--permission-mode",
  created_at: "2026-09-27T00:00:00Z",
};

function apiError(status: number, code: string) {
  return new ApiClientError("x", status, { request_id: "r", status: "error", error: { code, message: "x" } });
}

function open(opts: { verified?: boolean; subscribed?: boolean; watches?: (typeof WATCH)[] } = {}) {
  vi.mocked(getDigest).mockResolvedValue({ subscribed: opts.subscribed ?? false, verified: opts.verified ?? true });
  vi.mocked(listWatches).mockResolvedValue({ watches: opts.watches ?? [], pages: PAGES });
  const trigger = { current: document.createElement("button") };
  return render(<AlertsDrawer triggerRef={trigger} onClose={vi.fn()} />);
}

const status = () => screen.getByTestId("alerts-status").textContent;

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("the weekly digest switch", () => {
  it("turns the digest on and says so", async () => {
    // Turns red if: the switch does not PUT, or the result is not announced.
    vi.mocked(setDigest).mockResolvedValue({ subscribed: true, verified: true });
    open();
    await userEvent.setup().click(await screen.findByRole("checkbox"));
    expect(setDigest).toHaveBeenCalledWith(true);
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
    expect(status()).toBe("The weekly digest is on.");
  });

  it("an unverified account cannot turn it on and is told why", async () => {
    // Turns red if: the switch is enabled without a verified address, or the
    // reason is hidden.
    open({ verified: false });
    const box = await screen.findByRole("checkbox");
    expect((box as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("Verify your email address to get the digest.")).toBeTruthy();
  });

  it("an unverified account that is somehow subscribed can still turn it off", async () => {
    // Turns red if: turning it OFF is blocked by the verification rule.
    open({ verified: false, subscribed: true });
    expect(((await screen.findByRole("checkbox")) as HTMLInputElement).disabled).toBe(false);
  });
});

describe("watches", () => {
  it("watches a picked page, listing it with its label", async () => {
    // Turns red if: the picked page is not sent, or the new watch is not listed.
    vi.mocked(addWatch).mockResolvedValue({ ...WATCH, watch_id: "w2", kind: "page", value: PAGES[1].url, label: "Claude Code: Settings" });
    open();
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "Page" }), PAGES[1].url);
    await user.click(screen.getByRole("button", { name: "Watch" }));
    expect(addWatch).toHaveBeenCalledWith("page", PAGES[1].url);
    expect(within(screen.getByRole("list")).getByText("Claude Code: Settings")).toBeTruthy();
    expect(status()).toBe("Watching Claude Code: Settings.");
  });

  it("watches a trimmed term", async () => {
    // Turns red if: the term is not trimmed, or not sent as a term.
    vi.mocked(addWatch).mockResolvedValue(WATCH);
    open();
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "What to watch" }), "term");
    await user.type(screen.getByRole("textbox", { name: "Term to watch" }), "  --permission-mode  ");
    await user.click(screen.getByRole("button", { name: "Watch" }));
    expect(addWatch).toHaveBeenCalledWith("term", "--permission-mode");
  });

  it("a term of the wrong length is refused at submit and the text is kept", async () => {
    // Turns red if: a 1-character term is sent, or the typed text is cleared.
    open();
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "What to watch" }), "term");
    await user.type(screen.getByRole("textbox", { name: "Term to watch" }), "x");
    await user.click(screen.getByRole("button", { name: "Watch" }));
    expect(addWatch).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toBe("A term is 2 to 64 characters; this one is 1.");
    expect((screen.getByRole("textbox", { name: "Term to watch" }) as HTMLInputElement).value).toBe("x");
  });

  it.each([
    [403, "plan_required", "Watch alerts are part of Pro."],
    [409, "too_many_watches", "You have the most watches allowed. Remove one first."],
    [422, "validation_error", "That can't be watched. Check what you entered."],
    [500, "internal_error", "That didn't work. Please try again later."],
  ])("a %i %s gets its own message", async (code, errorCode, message) => {
    // Turns red if: the mapping for this refusal is dropped.
    vi.mocked(addWatch).mockRejectedValue(apiError(code, errorCode));
    open();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Watch" }));
    expect(screen.getByRole("alert").textContent).toBe(message);
  });

  it("removes a watch in one click and keeps focus in the drawer", async () => {
    // Turns red if: remove is not sent, the row stays, or focus falls to the page.
    vi.mocked(removeWatch).mockResolvedValue();
    open({ watches: [WATCH] });
    await userEvent.setup().click(await screen.findByRole("button", { name: "Stop watching --permission-mode" }));
    expect(removeWatch).toHaveBeenCalledWith("w1");
    expect(await screen.findByText("You are not watching anything.")).toBeTruthy();
    expect(screen.getByRole("dialog", { name: "Alerts" }).contains(document.activeElement)).toBe(true);
  });

  it("one request at a time", async () => {
    // Turns red if: the in-flight guard is dropped.
    vi.mocked(addWatch).mockReturnValue(new Promise(() => undefined));
    open();
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Watch" });
    await user.click(button);
    await user.click(button);
    expect(addWatch).toHaveBeenCalledTimes(1);
  });

  it("a failed load says so and never claims there are no watches", async () => {
    // Turns red if: a load failure shows "not watching anything".
    vi.mocked(getDigest).mockResolvedValue({ subscribed: false, verified: true });
    vi.mocked(listWatches).mockRejectedValue(new Error("down"));
    render(<AlertsDrawer triggerRef={{ current: document.createElement("button") }} onClose={vi.fn()} />);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText("You are not watching anything.")).toBeNull();
  });

  it("a click while a request runs is announced, not silently dropped", async () => {
    // Turns red if: the busy branch returns without telling the user.
    vi.mocked(addWatch).mockReturnValue(new Promise(() => undefined));
    open();
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Watch" });
    await user.click(button);
    await user.click(button);
    expect(status()).toBe("Still sending. Please wait a moment.");
  });

  it("with no pages to pick, Watch says so instead of sending an empty page", async () => {
    // Turns red if: an empty page is sent (a 422 would blame the user).
    vi.mocked(getDigest).mockResolvedValue({ subscribed: false, verified: true });
    vi.mocked(listWatches).mockResolvedValue({ watches: [], pages: [] });
    render(<AlertsDrawer triggerRef={{ current: document.createElement("button") }} onClose={vi.fn()} />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Watch" }));
    expect(addWatch).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toBe("No pages are available to watch right now.");
  });

  it("a failed load stays reported after another action", async () => {
    // Turns red if: the next action's run() clears the load failure.
    vi.mocked(getDigest).mockResolvedValue({ subscribed: false, verified: true });
    vi.mocked(listWatches).mockRejectedValue(new Error("down"));
    vi.mocked(setDigest).mockResolvedValue({ subscribed: true, verified: true });
    render(<AlertsDrawer triggerRef={{ current: document.createElement("button") }} onClose={vi.fn()} />);
    await userEvent.setup().click(await screen.findByRole("checkbox"));
    expect(await screen.findByText("Your alert settings could not be loaded. Please try again later.")).toBeTruthy();
  });

  it("removing a watch that is already gone counts as removed", async () => {
    // Turns red if: a 404 (removed elsewhere) shows an error and keeps the row.
    vi.mocked(removeWatch).mockRejectedValue(apiError(404, "not_found"));
    open({ watches: [WATCH] });
    await userEvent.setup().click(await screen.findByRole("button", { name: "Stop watching --permission-mode" }));
    expect(await screen.findByText("You are not watching anything.")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a remove that fails for another reason keeps the row and says so", async () => {
    // Partner: only a 404 is treated as removed.
    vi.mocked(removeWatch).mockRejectedValue(apiError(500, "internal_error"));
    open({ watches: [WATCH] });
    await userEvent.setup().click(await screen.findByRole("button", { name: "Stop watching --permission-mode" }));
    expect((await screen.findByRole("alert")).textContent).toBe("That didn't work. Please try again later.");
    expect(screen.getByText("--permission-mode")).toBeTruthy();
  });

  it("the disabled digest switch points at why", async () => {
    // Turns red if: a screen reader cannot hear why the switch is off.
    open({ verified: false });
    const box = await screen.findByRole("checkbox");
    expect(box.getAttribute("aria-describedby")).toBe("digest-verify-note");
    expect(document.getElementById("digest-verify-note")?.textContent).toMatch(/verify your email/i);
  });

  it("watching something already watched does not list it twice", async () => {
    // Turns red if: the list is not deduped by watch_id (the server returns the existing one).
    vi.mocked(addWatch).mockResolvedValue(WATCH);
    open({ watches: [WATCH] });
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByRole("combobox", { name: "What to watch" }), "term");
    await user.type(screen.getByRole("textbox", { name: "Term to watch" }), "--PERMISSION-MODE");
    await user.click(screen.getByRole("button", { name: "Watch" }));
    expect(within(screen.getByRole("list")).getAllByRole("listitem")).toHaveLength(1);
  });

  it("a later success clears an earlier error", async () => {
    // Turns red if: an old error stays on screen after an action works.
    vi.mocked(addWatch).mockRejectedValueOnce(apiError(500, "internal_error")).mockResolvedValueOnce(WATCH);
    open();
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Watch" });
    await user.click(button);
    expect(screen.getByRole("alert")).toBeTruthy();
    await user.click(button);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a failed digest load is reported too", async () => {
    // Turns red if: only the watches load failure is reported.
    vi.mocked(getDigest).mockRejectedValue(new Error("down"));
    vi.mocked(listWatches).mockResolvedValue({ watches: [], pages: PAGES });
    render(<AlertsDrawer triggerRef={{ current: document.createElement("button") }} onClose={vi.fn()} />);
    expect(await screen.findByText("Your alert settings could not be loaded. Please try again later.")).toBeTruthy();
  });
});
