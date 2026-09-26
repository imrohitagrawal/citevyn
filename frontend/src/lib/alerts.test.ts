/** ADR-0005 Phase 7D: the digest and watch calls. Turns red if a documented route or method changes. */
import { afterEach, expect, it, vi } from "vitest";
import { apiFetch } from "./api";
import { addWatch, getDigest, listWatches, removeWatch, setDigest } from "./alerts";

vi.mock("./api", () => ({ apiFetch: vi.fn() }));
afterEach(() => vi.resetAllMocks());

it("uses the documented routes and methods", async () => {
  vi.mocked(apiFetch).mockResolvedValue({});
  await getDigest();
  await setDigest(true);
  await listWatches();
  await addWatch("term", "--x");
  await removeWatch("a/b");
  expect(apiFetch).toHaveBeenNthCalledWith(1, "/v1/me/digest");
  expect(apiFetch).toHaveBeenNthCalledWith(2, "/v1/me/digest", { method: "PUT", body: JSON.stringify({ subscribed: true }) });
  expect(apiFetch).toHaveBeenNthCalledWith(3, "/v1/me/watches");
  expect(apiFetch).toHaveBeenNthCalledWith(4, "/v1/me/watches", {
    method: "POST",
    body: JSON.stringify({ kind: "term", value: "--x" }),
  });
  expect(apiFetch).toHaveBeenNthCalledWith(5, "/v1/me/watches/a%2Fb", { method: "DELETE" });
});
