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
  revokeKey: vi.fn(),
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

async function revoke(name: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: `Revoke ${name}` }));
  await user.click(await screen.findByRole("button", { name: `Really revoke ${name}` }));
}

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
    // Codex: the key goes in an environment variable, never in config.toml
    // (Codex's documented bearer_token_env_var).
    expect(screen.getByTestId("snippet-codex-env").textContent).toBe(
      `export CITEVYN_API_KEY=cvk_${"a".repeat(32)}_${"b".repeat(64)}`,
    );
    const codex = screen.getByTestId("snippet-codex").textContent ?? "";
    expect(codex).not.toContain("cvk_");
    expect(codex).not.toContain("export");
    expect(codex).toContain("[mcp_servers.citevyn]");
    expect(codex).toContain('bearer_token_env_var = "CITEVYN_API_KEY"');
    expect(codex).toMatch(/url = "https?:\/\/[^"]+\/v1\/mcp"/);
  });

  it("revokes a key after a second, confirming click", async () => {
    // Turns red if: revoke is sent on the first click, not sent on the second,
    // or the row stays.
    vi.mocked(revokeKey).mockResolvedValue();
    vi.mocked(listKeys).mockResolvedValueOnce([KEY]).mockResolvedValueOnce([]);
    open();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: `Revoke ${KEY.name}` }));
    expect(revokeKey).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: `Really revoke ${KEY.name}` }));
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


describe("review round", () => {
  it("a failed list says so, and never claims there are no keys", async () => {
    // Turns red if: a list failure shows "No keys yet".
    vi.mocked(listKeys).mockRejectedValue(new Error("down"));
    open();
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText(/no keys yet/i)).toBeNull();
  });

  it("a list that fails after a key is made does not say there are no keys", async () => {
    // Turns red if: the "No keys yet" line ignores a failed list.
    vi.mocked(listKeys).mockResolvedValueOnce([]).mockRejectedValueOnce(new Error("down"));
    vi.mocked(createKey).mockResolvedValue({ ...KEY, key: "cvk_x" });
    open();
    await screen.findByText(/no keys yet/i);
    await userEvent.setup().click(screen.getByRole("button", { name: "Make a key" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText(/no keys yet/i)).toBeNull();
  });

  it("a list that fails keeps the keys already shown", async () => {
    // Turns red if: a failed list empties the shown keys.
    vi.mocked(listKeys).mockResolvedValueOnce([KEY]).mockRejectedValueOnce(new Error("down"));
    vi.mocked(createKey).mockResolvedValue({ ...KEY, key_id: "k2", key: "cvk_x" });
    open();
    await screen.findByRole("button", { name: `Revoke ${KEY.name}` });
    await userEvent.setup().click(screen.getByRole("button", { name: "Make a key" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("button", { name: `Revoke ${KEY.name}` })).toBeTruthy();
  });

  it("revoking the key just made removes it from the screen", async () => {
    // Turns red if: the dead key and its snippets stay shown after revoke.
    vi.mocked(revokeKey).mockResolvedValue();
    vi.mocked(listKeys).mockResolvedValueOnce([]).mockResolvedValueOnce([KEY]).mockResolvedValueOnce([]);
    vi.mocked(createKey).mockResolvedValue({ ...KEY, key: "cvk_" + "a".repeat(32) + "_" + "b".repeat(64) });
    open();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Make a key" }));
    await screen.findByTestId("new-key");
    await revoke(KEY.name);
    expect(await screen.findByText(/no keys yet/i)).toBeTruthy();
    expect(screen.queryByTestId("new-key")).toBeNull();
  });

  it("making a key twice quickly sends one request", async () => {
    // Turns red if: the busy guard is dropped.
    vi.mocked(listKeys).mockResolvedValue([]);
    let finish: (v: Awaited<ReturnType<typeof createKey>>) => void = () => undefined;
    vi.mocked(createKey).mockReturnValue(new Promise((r) => (finish = r)));
    open();
    const user = userEvent.setup();
    const make = await screen.findByRole("button", { name: "Make a key" });
    await user.click(make);
    await user.click(make);
    expect(createKey).toHaveBeenCalledTimes(1);
    finish({ ...KEY, key: "cvk_x" });
  });

  it("a revoked key leaves the list even when the refresh fails", async () => {
    // Turns red if: the revoked row stays and can be revoked a second time.
    vi.mocked(revokeKey).mockResolvedValue();
    vi.mocked(listKeys).mockResolvedValueOnce([KEY]).mockRejectedValueOnce(new Error("down"));
    open();
    await revoke(KEY.name);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByRole("button", { name: `Revoke ${KEY.name}` })).toBeNull();
  });

  it("while one revoke is running, another key cannot be revoked", async () => {
    // Turns red if: the busy guard in revoke is dropped.
    const OTHER = { ...KEY, key_id: "k2", name: "Other key" };
    vi.mocked(listKeys).mockResolvedValue([KEY, OTHER]);
    vi.mocked(revokeKey).mockReturnValue(new Promise(() => undefined));
    open();
    const user = userEvent.setup();
    await revoke(KEY.name);
    await user.click(screen.getByRole("button", { name: `Revoke ${OTHER.name}` }));
    const again = screen.queryByRole("button", { name: `Really revoke ${OTHER.name}` });
    if (again) await user.click(again);
    expect(revokeKey).toHaveBeenCalledTimes(1);
  });

  it("making a key cancels a pending 'Really revoke?'", async () => {
    // Turns red if: the armed revoke survives a make.
    vi.mocked(listKeys).mockResolvedValue([KEY]);
    vi.mocked(createKey).mockResolvedValue({ ...KEY, key_id: "k2", key: "cvk_x" });
    open();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: `Revoke ${KEY.name}` }));
    await user.click(screen.getByRole("button", { name: "Make a key" }));
    await screen.findByTestId("new-key");
    expect(screen.getByRole("button", { name: `Revoke ${KEY.name}` })).toBeTruthy();
  });

  it("after a revoke, focus stays in the drawer", async () => {
    // Turns red if: focus falls to the page when the revoked row disappears.
    vi.mocked(revokeKey).mockResolvedValue();
    vi.mocked(listKeys).mockResolvedValueOnce([KEY]).mockResolvedValueOnce([]);
    open();
    await revoke(KEY.name);
    await screen.findByText(/no keys yet/i);
    const dialog = screen.getByRole("dialog", { name: "API keys" });
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});
