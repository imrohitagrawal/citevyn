/**
 * The browser half of the sign-up bot check (ADR-0005 Phase 5; server:
 * backend/app/core/pow.py). Solves a real challenge with WebCrypto and returns
 * the header. Each test says which change turns it red.
 */
import { createHash } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "./api";
import { botCheckHeaders, solveChallenge } from "./botCheck";
import { loadClientConfig } from "./clientConfig";

vi.mock("./api", () => ({ apiFetch: vi.fn() }));
vi.mock("./clientConfig", () => ({ loadClientConfig: vi.fn() }));

function challengeFor(number: number, maxnumber = 30) {
  const salt = "abc123?expires=9999999999";
  const challenge = createHash("sha256").update(`${salt}${number}`).digest("hex");
  return { algorithm: "SHA-256", salt, challenge, maxnumber, signature: "sig" };
}

function decode(header: string): Record<string, unknown> {
  const b64 = header.replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(atob(b64)) as Record<string, unknown>;
}

afterEach(() => vi.resetAllMocks());

describe("solveChallenge", () => {
  it("finds the number behind the challenge, hashing exactly as the server does", async () => {
    // Turns red if: the salt and number are joined or hashed differently.
    expect(await solveChallenge(challengeFor(17))).toBe(17);
    expect(await solveChallenge(challengeFor(0))).toBe(0);
  });

  it("gives up past maxnumber instead of searching forever", async () => {
    // Turns red if: the search ignores maxnumber.
    await expect(solveChallenge(challengeFor(40, 30))).rejects.toThrow(/no solution/i);
  });
});

describe("botCheckHeaders", () => {
  it("returns no header, and makes no request, when the check is off", async () => {
    // Turns red if: the challenge is fetched while the access model is off
    // (today's page must make no extra request).
    vi.mocked(loadClientConfig).mockResolvedValue({ access_model: false, bot_check: null });
    expect(await botCheckHeaders()).toEqual({});
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("fetches a challenge and returns the solved one as base64url JSON", async () => {
    // Turns red if: the header name, the encoding or the payload shape changes.
    // Found in review: reading a snapshot that had not loaded yet sent no
    // header. It must WAIT for the config. Turns red if: it reads a snapshot.
    vi.mocked(loadClientConfig).mockResolvedValue({ access_model: true, bot_check: { kind: "pow" } });
    const c = challengeFor(9);
    vi.mocked(apiFetch).mockResolvedValue({ request_id: "r", challenge: c });
    const headers = await botCheckHeaders();
    expect(apiFetch).toHaveBeenCalledWith("/v1/auth/challenge");
    const value = headers["X-CiteVyn-Bot-Check"];
    expect(value).toMatch(/^[A-Za-z0-9_-]+$/); // base64url, no padding or '+/'
    expect(decode(value)).toEqual({ ...c, number: 9 });
  });
});
