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

  it("restores focus to the trigger on unmount", async () => {
    const { listMySessions } = await import("../lib/api");
    vi.mocked(listMySessions).mockResolvedValueOnce({ request_id: "r", sessions: [] });
    const { trigger, unmount } = renderDrawer();
    unmount();
    expect(trigger).toHaveFocus();
    trigger.remove();
  });
});
