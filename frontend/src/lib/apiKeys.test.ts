/** ADR-0005 Phase 6B-2: the API key calls. Each test says which change turns it red. */
import { afterEach, expect, it, vi } from "vitest";
import { apiFetch } from "./api";
import { createKey, listKeys, revokeKey } from "./apiKeys";

vi.mock("./api", () => ({ apiFetch: vi.fn() }));
afterEach(() => vi.resetAllMocks());

it("lists, makes and revokes on the documented routes", async () => {
  // Turns red if: a path, method or body changes (docs/API_SPEC.md "API keys").
  vi.mocked(apiFetch).mockResolvedValueOnce({ keys: [] }).mockResolvedValueOnce({ key: "k" });
  expect(await listKeys()).toEqual([]);
  await createKey("Key made 2026-09-26");
  await revokeKey("abc");
  expect(apiFetch).toHaveBeenNthCalledWith(1, "/v1/me/api-keys");
  expect(apiFetch).toHaveBeenNthCalledWith(2, "/v1/me/api-keys", {
    method: "POST",
    body: JSON.stringify({ name: "Key made 2026-09-26" }),
  });
  expect(apiFetch).toHaveBeenNthCalledWith(3, "/v1/me/api-keys/abc", { method: "DELETE" });
});
