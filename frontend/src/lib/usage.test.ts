/** ADR-0005 Phase 6D-2: the usage call. Turns red if the documented route changes. */
import { afterEach, expect, it, vi } from "vitest";
import { apiFetch } from "./api";
import { getUsage } from "./usage";

vi.mock("./api", () => ({ apiFetch: vi.fn() }));
afterEach(() => vi.resetAllMocks());

it("reads GET /v1/me/usage (docs/API_SPEC.md 'Usage insights')", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({ days: [] });
  expect(await getUsage()).toEqual({ days: [] });
  expect(apiFetch).toHaveBeenCalledWith("/v1/me/usage");
});
