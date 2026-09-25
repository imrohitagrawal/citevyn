/**
 * "Download my history" in the History drawer (ADR-0005 §6 trust must-haves).
 * The drawer is a lazy chunk, so none of this reaches the eager bundle.
 */
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { HistoryDrawer } from "./HistoryDrawer";
import { apiFetch, listMySessions } from "../lib/api";

vi.mock("../lib/api", () => ({ listMySessions: vi.fn(), apiFetch: vi.fn() }));

const ONE = {
  request_id: "r",
  sessions: [
    {
      session_id: "s1",
      created_at: "2026-09-01T00:00:00Z",
      expires_at: "2026-09-08T00:00:00Z",
      current_product_area: "claude_code",
      message_count: 2,
    },
  ],
};

let blobs: Blob[];
let clicked: string[];
let connected: boolean[];
beforeEach(() => {
  vi.clearAllMocks();
  blobs = [];
  clicked = [];
  connected = [];
  URL.createObjectURL = vi.fn((b: Blob) => {
    blobs.push(b);
    return "blob:x";
  }) as never;
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    clicked.push(this.download);
    connected.push(this.isConnected);
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// jsdom's Blob has no .text() and is not Node's Blob, so read it the jsdom way.
const readBlob = (b: Blob) =>
  new Promise<string>((resolve) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result));
    fr.readAsText(b);
  });

function renderDrawer() {
  const triggerRef = createRef<HTMLElement>();
  return render(<HistoryDrawer triggerRef={triggerRef} onClose={() => {}} onResume={() => {}} />);
}

describe("downloading history", () => {
  it("downloads the Markdown export as a .md file", async () => {
    // Turns red if: the button asks for the wrong format, or the text is not
    // what gets saved.
    vi.mocked(listMySessions).mockResolvedValueOnce(ONE);
    vi.mocked(apiFetch).mockResolvedValueOnce("# CiteVyn conversation history\n" as never);
    renderDrawer();
    const btn = await screen.findByRole("button", { name: "Download as Markdown" });
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(apiFetch).toHaveBeenCalledWith("/v1/me/export?format=markdown");
    expect(await readBlob(blobs[0])).toBe("# CiteVyn conversation history\n");
    expect(clicked[0]).toMatch(/^citevyn-history-\d{4}-\d{2}-\d{2}\.md$/);
    expect(screen.getByTestId("history-export-status").textContent).toBe("Download started.");
  });

  it("downloads the JSON export as a .json file", async () => {
    // Turns red if: the parsed JSON is saved as "[object Object]".
    vi.mocked(listMySessions).mockResolvedValueOnce(ONE);
    vi.mocked(apiFetch).mockResolvedValueOnce({ sessions: [] } as never);
    renderDrawer();
    const btn = await screen.findByRole("button", { name: "Download as JSON" });
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(apiFetch).toHaveBeenCalledWith("/v1/me/export?format=json");
    expect(JSON.parse(await readBlob(blobs[0]))).toEqual({ sessions: [] });
    expect(clicked[0]).toMatch(/\.json$/);
  });

  it("says so when the download fails", async () => {
    // Turns red if: a failed export is silent.
    vi.mocked(listMySessions).mockResolvedValueOnce(ONE);
    vi.mocked(apiFetch).mockRejectedValueOnce(new Error("boom"));
    renderDrawer();
    const btn = await screen.findByRole("button", { name: "Download as JSON" });
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(screen.getByTestId("history-export-status").textContent).toBe(
      "Could not download your history. Please try again.",
    );
    expect(clicked).toEqual([]);
  });

  it("offers no download when there is nothing to export", async () => {
    // Turns red if: an empty history offers a file of nothing.
    vi.mocked(listMySessions).mockResolvedValueOnce({ request_id: "r", sessions: [] });
    renderDrawer();
    expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download as JSON" })).toBeNull();
  });
});

describe("download robustness", () => {
  it("clicks a link that is IN the page, and does not revoke its URL at once", async () => {
    // Older Firefox ignores click() on a detached link, and some Safari versions
    // drop a download whose object URL is revoked immediately. Turns red if: the
    // link is not attached while clicked, or the URL is revoked synchronously.
    vi.mocked(listMySessions).mockResolvedValueOnce(ONE);
    vi.mocked(apiFetch).mockResolvedValueOnce("# x\n" as never);
    renderDrawer();
    const btn = await screen.findByRole("button", { name: "Download as Markdown" });
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(connected).toEqual([true]);
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    expect(document.querySelector("a[download]")).toBeNull(); // partner: removed after the click
  });
});

