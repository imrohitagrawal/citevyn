import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { HistoryDrawer } from "./HistoryDrawer";

vi.mock("../lib/api", () => ({
  listMySessions: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
});

function renderDrawer(onClose = vi.fn(), onResume = vi.fn()) {
  const trigger = document.createElement("button");
  trigger.textContent = "History";
  document.body.appendChild(trigger);
  trigger.focus();
  const triggerRef = createRef<HTMLElement>();
  // @ts-expect-error -- assigning to a ref's .current outside React for the test fixture
  triggerRef.current = trigger;
  const utils = render(<HistoryDrawer triggerRef={triggerRef} onClose={onClose} onResume={onResume} />);
  return { ...utils, trigger, onClose, onResume };
}

describe("HistoryDrawer", () => {
  it("shows a loading state, then the sessions once fetched", async () => {
    const { listMySessions } = await import("../lib/api");
    vi.mocked(listMySessions).mockResolvedValueOnce({
      request_id: "r",
      sessions: [
        {
          session_id: "sess_1",
          created_at: "2026-09-01T00:00:00Z",
          expires_at: "2026-09-08T00:00:00Z",
          current_product_area: "claude_code",
          message_count: 4,
        },
      ],
    });
    renderDrawer();
    expect(await screen.findByText("claude_code")).toBeInTheDocument();
    expect(screen.getByText(/4 messages/)).toBeInTheDocument();
  });

  it("shows an empty state with zero sessions", async () => {
    const { listMySessions } = await import("../lib/api");
    vi.mocked(listMySessions).mockResolvedValueOnce({ request_id: "r", sessions: [] });
    renderDrawer();
    expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
  });

  it("shows an error state when the fetch fails", async () => {
    const { listMySessions } = await import("../lib/api");
    vi.mocked(listMySessions).mockRejectedValueOnce(new Error("network down"));
    renderDrawer();
    expect(await screen.findByText(/Couldn't load your history/)).toBeInTheDocument();
  });

  it("clicking a session calls onResume with its id", async () => {
    const { listMySessions } = await import("../lib/api");
    vi.mocked(listMySessions).mockResolvedValueOnce({
      request_id: "r",
      sessions: [
        {
          session_id: "sess_42",
          created_at: "2026-09-01T00:00:00Z",
          expires_at: "2026-09-08T00:00:00Z",
          current_product_area: null,
          message_count: 2,
        },
      ],
    });
    const user = userEvent.setup();
    const { onResume } = renderDrawer();
    await user.click(await screen.findByText("Conversation"));
    expect(onResume).toHaveBeenCalledWith("sess_42");
  });

  it("Escape calls onClose", async () => {
    const { listMySessions } = await import("../lib/api");
    vi.mocked(listMySessions).mockResolvedValueOnce({ request_id: "r", sessions: [] });
    const user = userEvent.setup();
    const { onClose } = renderDrawer();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("clicking the backdrop closes; clicking inside the dialog does not", async () => {
    // `ConnectedAccountsDrawer` has exactly this test and this drawer did not --
    // the same one-guarded/one-not asymmetry between the two drawers that let
    // #290 exist in the first place. Deleting the
    // `if (e.target === e.currentTarget) onClose();` line used to leave all 336
    // tests green.
    const { listMySessions } = await import("../lib/api");
    vi.mocked(listMySessions).mockResolvedValueOnce({ request_id: "r", sessions: [] });
    const user = userEvent.setup();
    const { onClose, trigger } = renderDrawer();
    await user.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled(); // partner: an inside click must NOT close
    await user.click(screen.getByRole("presentation"));
    expect(onClose).toHaveBeenCalledTimes(1);
    trigger.remove();
  });

  it("names the dialog for a screen reader", async () => {
    // The accessible name was unguarded: deleting `aria-label` left every test
    // green, including the focus test above, because `getByRole("dialog")` does
    // not care what the dialog is called. A reader would then hear an anonymous
    // dialog. The sibling drawer's name is guarded only incidentally, by its
    // `getByRole("dialog", { name })` locators; this says it outright.
    const { listMySessions } = await import("../lib/api");
    vi.mocked(listMySessions).mockResolvedValueOnce({ request_id: "r", sessions: [] });
    const { trigger } = renderDrawer();
    expect(screen.getByRole("dialog", { name: "Chat history" })).toBeInTheDocument();
    trigger.remove();
  });

  it("takes focus on open and restores it to the trigger on unmount", async () => {
    // Both halves, and the FIRST is what makes the second mean anything.
    //
    // The previous version of this test asserted only the restore, and it was
    // decorative: `renderDrawer` focuses the trigger, nothing moved focus into
    // the dialog, so "focus returns to the trigger" held trivially. Proven --
    // deleting the unmount cleanup outright left all 6 tests green.
    //
    // RED if the mount effect stops calling `dialogRef.current?.focus()`, if the
    // dialog loses `tabIndex={-1}` (an element with no tabindex cannot take
    // programmatic focus, so focus silently stays on <body> and the next Tab
    // walks the page BEHIND the backdrop -- #290), or if the unmount cleanup
    // stops calling `trigger?.focus()`.
    const { listMySessions } = await import("../lib/api");
    vi.mocked(listMySessions).mockResolvedValueOnce({ request_id: "r", sessions: [] });
    const { trigger, unmount } = renderDrawer();
    expect(screen.getByRole("dialog")).toContainElement(document.activeElement as HTMLElement);
    expect(trigger).not.toHaveFocus();
    unmount();
    expect(trigger).toHaveFocus();
    trigger.remove();
  });
});

/**
 * #331. The drawer is `role="dialog"` + `aria-modal="true"` behind a backdrop
 * that blocks the MOUSE, yet the keyboard reached the page underneath: 3
 * forward Tabs, or ONE Shift+Tab, measured in real Chromium. `renderDrawer`
 * appends a real `History` button to `document.body` outside the dialog, so
 * "escaped" here means landing on an actual page control, not a proxy for one.
 */
describe("HistoryDrawer traps Tab (#331)", () => {
  async function openWithSessions() {
    const { listMySessions } = await import("../lib/api");
    vi.mocked(listMySessions).mockResolvedValueOnce({
      request_id: "r",
      sessions: [
        {
          session_id: "sess_1",
          created_at: "2026-09-01T00:00:00Z",
          expires_at: "2026-09-08T00:00:00Z",
          current_product_area: "claude_code",
          message_count: 4,
        },
        {
          session_id: "sess_2",
          created_at: "2026-09-02T00:00:00Z",
          expires_at: "2026-09-09T00:00:00Z",
          current_product_area: "codex",
          message_count: 2,
        },
      ],
    });
    const fixture = renderDrawer();
    await screen.findByText("claude_code");
    return fixture;
  }

  const inDialog = () =>
    screen.getByRole("dialog").contains(document.activeElement) ? true : false;

  it("keeps focus inside through many forward Tabs, never reaching the page behind", async () => {
    const user = userEvent.setup();
    const { trigger } = await openWithSessions();
    // The dialog holds 3 controls; 8 presses is more than a full cycle, so a
    // trap that only wrapped once would still be caught.
    for (let i = 0; i < 8; i += 1) {
      await user.tab();
      expect(inDialog()).toBe(true);
    }
    expect(trigger).not.toHaveFocus();
    trigger.remove();
  });

  it("keeps focus inside through many backward Tabs", async () => {
    const user = userEvent.setup();
    const { trigger } = await openWithSessions();
    for (let i = 0; i < 8; i += 1) {
      await user.tab({ shift: true });
      expect(inDialog()).toBe(true);
    }
    expect(trigger).not.toHaveFocus();
    trigger.remove();
  });

  // The exact reported case: focus starts on the dialog element itself
  // (`tabIndex={-1}`), which is inside the dialog but is not one of its
  // focusable controls, so neither wrap edge matched and ONE press escaped.
  it("does not escape on the very first Shift+Tab after opening", async () => {
    const user = userEvent.setup();
    const { trigger } = await openWithSessions();
    expect(screen.getByRole("dialog")).toHaveFocus();
    await user.tab({ shift: true });
    expect(inDialog()).toBe(true);
    expect(trigger).not.toHaveFocus();
    trigger.remove();
  });

  // Partner assertion: proves the page control is genuinely reachable by Tab
  // when the dialog is NOT there. Without this, every test above could pass
  // because `trigger` was unfocusable for some unrelated reason.
  it("the page control behind it IS tabbable once the drawer is gone", async () => {
    const user = userEvent.setup();
    const { trigger, unmount } = await openWithSessions();
    unmount();
    // Earlier tests in this file append their own trigger to document.body and
    // do not always remove it, so Tab would otherwise land on the FIRST
    // leftover rather than this one. Clear them, so this asserts what it says.
    for (const stray of Array.from(document.body.querySelectorAll("button"))) {
      if (stray !== trigger) stray.remove();
    }
    (document.activeElement as HTMLElement | null)?.blur();
    await user.tab();
    expect(trigger).toHaveFocus();
    trigger.remove();
  });
});
