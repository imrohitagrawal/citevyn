/** ADR-0005 Phase 6C-2: the shared-answer calls. Each test says which change turns it red. */
import { afterEach, expect, it, vi } from "vitest";
import { apiFetch } from "./api";
import { listShares, revokeShare, shareAnswer, shareLink } from "./shares";

vi.mock("./api", () => ({ apiFetch: vi.fn() }));
afterEach(() => vi.resetAllMocks());

const ID = "0123456789abcdef0123456789abcdef";

it("shares, lists and revokes on the documented routes", async () => {
  // Turns red if: a path or method changes (docs/API_SPEC.md "Shareable answers"),
  // or the list is not unwrapped from {shares}.
  const share = { share_id: ID, url: `/s/${ID}`, question: "q", created_at: "d" };
  vi.mocked(apiFetch).mockResolvedValueOnce(share).mockResolvedValueOnce({ shares: [share] });
  expect(await shareAnswer("s1", "m1")).toEqual(share);
  expect(await listShares()).toEqual([share]);
  await revokeShare(ID);
  expect(apiFetch).toHaveBeenNthCalledWith(1, "/v1/sessions/s1/messages/m1/share", { method: "POST" });
  expect(apiFetch).toHaveBeenNthCalledWith(2, "/v1/me/shares");
  expect(apiFetch).toHaveBeenNthCalledWith(3, `/v1/me/shares/${ID}`, { method: "DELETE" });
});

it("builds a link only from /s/<32 hex>", () => {
  // Turns red if: the shape check is loosened or unanchored.
  const origin = "https://citevyn.example";
  expect(shareLink({ url: `/s/${ID}` }, origin)).toBe(`${origin}/s/${ID}`);
  for (const url of [`https://evil.example/s/${ID}`, `/s/${ID}/x`, `/s/${ID.toUpperCase()}`, `x/s/${ID}`, "/s/abc"]) {
    expect(shareLink({ url }, origin)).toBeNull();
  }
});
