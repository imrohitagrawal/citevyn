/**
 * ADR-0005 Phase 5: the client learns whether the access model is on. Anything
 * but a clear "on" is OFF, so the page stays exactly as it is today. Each test
 * says which change turns it red.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch, isLiveMode } from "./api";
import { getClientConfig, loadClientConfig, resetClientConfig, subscribeClientConfig } from "./clientConfig";

vi.mock("./api", () => ({ apiFetch: vi.fn(), isLiveMode: vi.fn(() => true) }));

afterEach(() => {
  resetClientConfig();
  vi.resetAllMocks();
});

describe("loadClientConfig", () => {
  it("reads 'on' and tells subscribers", async () => {
    // Turns red if: the server's answer is not stored, or subscribers are not told.
    vi.mocked(isLiveMode).mockReturnValue(true);
    vi.mocked(apiFetch).mockResolvedValue({ access_model: true, bot_check: { kind: "pow" } });
    const heard = vi.fn();
    const stop = subscribeClientConfig(heard);
    await loadClientConfig();
    stop();
    expect(apiFetch).toHaveBeenCalledWith("/v1/config");
    expect(getClientConfig()).toEqual({ access_model: true, bot_check: { kind: "pow" } });
    expect(heard).toHaveBeenCalledTimes(1);
  });

  it("asks once, however many callers", async () => {
    // Turns red if: every caller fetches again.
    vi.mocked(isLiveMode).mockReturnValue(true);
    vi.mocked(apiFetch).mockResolvedValue({ access_model: false, bot_check: null });
    await Promise.all([loadClientConfig(), loadClientConfig()]);
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["a demo build (no backend)", () => vi.mocked(isLiveMode).mockReturnValue(false)],
    ["a network failure", () => vi.mocked(apiFetch).mockRejectedValue(new Error("down"))],
    ["a truthy non-boolean", () => vi.mocked(apiFetch).mockResolvedValue({ access_model: "true" })],
    ["no body", () => vi.mocked(apiFetch).mockResolvedValue(null)],
    [
      "a synchronous throw",
      () =>
        vi.mocked(apiFetch).mockImplementation(() => {
          throw new Error("boom");
        }),
    ],
  ])("stays OFF on %s", async (_label, arrange) => {
    // Turns red if: anything but a clear boolean "on" turns the model on, or
    // a failure escapes (the page would crash instead of staying as it is).
    vi.mocked(isLiveMode).mockReturnValue(true);
    arrange();
    await expect(loadClientConfig()).resolves.toEqual({ access_model: false, bot_check: null });
    expect(getClientConfig().access_model).toBe(false);
  });
});


it("a demo build makes no request at all", async () => {
  // Today's demo page must make no extra request. Turns red if: the live-mode
  // check is dropped (the result alone cannot show it: a failed fetch is OFF too).
  vi.mocked(isLiveMode).mockReturnValue(false);
  await loadClientConfig();
  expect(apiFetch).not.toHaveBeenCalled();
});
