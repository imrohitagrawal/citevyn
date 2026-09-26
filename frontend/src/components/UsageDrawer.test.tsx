/**
 * ADR-0005 Phase 6D-2: the Usage drawer. Each test says which change turns it red.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import UsageDrawer from "./UsageDrawer";
import { getUsage, type UsageInsights } from "../lib/usage";
import { ApiClientError } from "../lib/types";

vi.mock("../lib/usage", () => ({ getUsage: vi.fn() }));

const DATA: UsageInsights = {
  usage: { kind: "monthly", used: 1234, limit: 2000, remaining: 766, resets_at: "2026-10-01T00:00:00+00:00", verified: true },
  period_start: "2026-09-01T00:00:00+00:00",
  days: [
    { date: "2026-09-01", chat: 0, mcp: 0 },
    { date: "2026-09-02", chat: 3, mcp: 1 },
    { date: "2026-09-03", chat: 0, mcp: 0 },
    { date: "2026-09-04", chat: 0, mcp: 1 },
  ],
  totals: { chat: 3, mcp: 2, total: 5 }, // chat and MCP differ, so a swap shows
};

function open() {
  const trigger = { current: document.createElement("button") };
  return render(<UsageDrawer triggerRef={trigger} onClose={vi.fn()} />);
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("UsageDrawer", () => {
  it("shows the allowance, the reset date and the channel totals", async () => {
    // Turns red if: used/limit, the reset day or a channel total is missing or wrong.
    vi.mocked(getUsage).mockResolvedValue(DATA);
    open();
    expect((await screen.findByTestId("usage-summary")).textContent).toBe(
      "1,234 of 2,000 answered questions used this month; resets 1 Oct 2026.",
    );
    expect(screen.getByTestId("usage-channels").textContent).toBe(
      "In the chat: 3. From AI agents (MCP): 2.",
    );
  });

  it("lists only the days with use, with each day's chat and MCP numbers", async () => {
    // Turns red if: empty days are listed, a day is dropped, or chat and MCP swap.
    vi.mocked(getUsage).mockResolvedValue(DATA);
    open();
    const rows = within(await screen.findByRole("table")).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.textContent)).toEqual(["2 Sep 202631", "4 Sep 202601"]);
  });

  it("the busiest day gets the full bar and the others a share of it", async () => {
    // Turns red if: bars are not scaled to the busiest day.
    vi.mocked(getUsage).mockResolvedValue(DATA);
    open();
    await screen.findByRole("table");
    const widths = Array.from(document.querySelectorAll<HTMLElement>(".usage-bar")).map((b) => b.style.width);
    expect(widths).toEqual(["100%", "25%"]);
  });

  it("a month with no use says so instead of an empty table", async () => {
    // Turns red if: an empty table (or nothing) shows for a new month.
    vi.mocked(getUsage).mockResolvedValue({ ...DATA, days: [{ date: "2026-09-01", chat: 0, mcp: 0 }] });
    open();
    expect(await screen.findByText("No answered questions yet this month.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("a Free account is told usage insights are part of Pro", async () => {
    // Turns red if: plan_required gets the generic failure copy.
    vi.mocked(getUsage).mockRejectedValue(
      new ApiClientError("x", 403, { request_id: "r", status: "error", error: { code: "plan_required", message: "x" } }),
    );
    open();
    expect((await screen.findByRole("alert")).textContent).toBe("Usage insights are part of Pro.");
  });

  it("any other failure says it could not load, and shows no numbers", async () => {
    // Turns red if: a failure is silent, or claims zero use.
    vi.mocked(getUsage).mockRejectedValue(new Error("down"));
    open();
    expect((await screen.findByRole("alert")).textContent).toBe("Could not load your usage. Please try again later.");
    expect(screen.queryByTestId("usage-summary")).toBeNull();
    expect(screen.queryByText(/no answered questions/i)).toBeNull();
  });
});
