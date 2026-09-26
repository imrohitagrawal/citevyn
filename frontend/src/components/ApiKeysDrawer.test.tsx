/**
 * ADR-0005 Phase 6B-2: the API keys drawer and the MCP install snippets.
 * Keys are made with an automatic name (no text input: TEST_STRATEGY §8b), the
 * key is shown ONCE with a copy button, revoking needs a click. Each test says
 * which change turns it red.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ApiKeysDrawer from "./ApiKeysDrawer";
import { createKey, listKeys, revokeKey } from "../lib/apiKeys";
import { ApiClientError } from "../lib/types";

vi.mock("../lib/apiKeys", () => ({
  listKeys: vi.fn(),
  createKey: vi.fn(),
  revokeKey: vi.fn(() => Promise.resolve()),
}));

const KEY = {
  key_id: "a".repeat(32),
  name: "Key made 2026-09-26",
  prefix: "cvk_aaaaaaaa",
  scope: "mcp",
  created_at: "2026-09-26T10:00:00+00:00",
  last_used_at: null,
};

function open() {
  const trigger = { current: document.createElement("button") };
  return render(<ApiKeysDrawer triggerRef={trigger} onClose={vi.fn()} />);
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("ApiKeysDrawer", () => {
  it("lists your keys by name and first characters, never the key", async () => {
    // Turns red if: the list is missing, or shows more than the prefix.
    vi.mocked(listKeys).mockResolvedValue([KEY]);
    open();
    expect(await screen.findByText(KEY.name)).toBeTruthy();
    expect(screen.getByText(/cvk_aaaaaaaa…/)).toBeTruthy();
  });

  it("makes a key with an automatic name and shows it once, with install snippets", async () => {
    // Turns red if: the new key is not shown, or the snippet does not carry it.
    vi.mocked(listKeys).mockResolvedValue([]);
    vi.mocked(createKey).mockResolvedValue({ ...KEY, key: "cvk_" + "a".repeat(32) + "_" + "b".repeat(64) });
    open();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Make a key" }));
    expect(createKey).toHaveBeenCalledWith(expect.stringMatching(/^Key made \d{4}-\d{2}-\d{2}$/));
    const shown = await screen.findByTestId("new-key");
    expect(shown.textContent).toContain("b".repeat(64));
    expect(screen.getByText(/only time/i)).toBeTruthy();
    expect(screen.getByTestId("snippet-claude").textContent).toContain(`Bearer cvk_${"a".repeat(32)}_`);
  });

  it("revokes a key", async () => {
    // Turns red if: revoke is not called, or the row stays.
    vi.mocked(listKeys).mockResolvedValueOnce([KEY]).mockResolvedValueOnce([]);
    open();
    await userEvent.setup().click(await screen.findByRole("button", { name: `Revoke ${KEY.name}` }));
    expect(revokeKey).toHaveBeenCalledWith(KEY.key_id);
    expect(await screen.findByText(/no keys yet/i)).toBeTruthy();
  });

  it("a free account is told keys need Pro", async () => {
    // Turns red if: a plan_required refusal is shown as a generic failure.
    vi.mocked(listKeys).mockResolvedValue([]);
    vi.mocked(createKey).mockRejectedValue(
      new ApiClientError("x", 403, {
        request_id: "r",
        status: "error",
        error: { code: "plan_required", message: "x" },
      }),
    );
    open();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Make a key" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/Pro/);
  });

  it("the ten-key limit is said plainly", async () => {
    // Turns red if: too_many_api_keys is shown as a generic failure.
    vi.mocked(listKeys).mockResolvedValue([]);
    vi.mocked(createKey).mockRejectedValue(
      new ApiClientError("x", 409, {
        request_id: "r",
        status: "error",
        error: { code: "too_many_api_keys", message: "x" },
      }),
    );
    open();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Make a key" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/revoke one/i);
  });

  it("is a labelled dialog", async () => {
    // Turns red if: the drawer loses its dialog role or name.
    vi.mocked(listKeys).mockResolvedValue([]);
    open();
    const dialog = await screen.findByRole("dialog", { name: "API keys" });
    expect(within(dialog).getByRole("button", { name: "Close" })).toBeTruthy();
  });
});
