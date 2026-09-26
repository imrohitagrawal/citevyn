/**
 * The browser half of the sign-up bot check (ADR-0005 Phase 5). The server
 * (``backend/app/core/pow.py``) signs a challenge; this searches for the number
 * behind it with WebCrypto and returns the ``X-CiteVyn-Bot-Check`` header that
 * register and magic-link request need. Each solution pays for one attempt, so
 * it is solved fresh for every submit.
 *
 * Imported only by the lazy sign-in dialog, so the eager bundle never carries
 * it. No input, no widget, no third-party script: the Content-Security-Policy
 * stays ``'self'``.
 */
import { apiFetch } from "./api";
import { loadClientConfig } from "./clientConfig";

export interface Challenge {
  algorithm: string;
  salt: string;
  challenge: string;
  maxnumber: number;
  signature: string;
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** The number whose SHA-256(salt + number) is the challenge. */
export async function solveChallenge(c: Challenge): Promise<number> {
  for (let n = 0; n <= c.maxnumber; n++) {
    if ((await sha256Hex(`${c.salt}${n}`)) === c.challenge) return n;
  }
  throw new Error("No solution to the sign-up check.");
}

/** ``{}`` while the check is off; otherwise one freshly solved challenge.
 * Waits for the config: a snapshot read before it loaded sent no header, and the
 * server refused the sign-up (found in review). */
export async function botCheckHeaders(): Promise<Record<string, string>> {
  if (!(await loadClientConfig()).bot_check) return {};
  const { challenge } = await apiFetch<{ challenge: Challenge }>("/v1/auth/challenge");
  const number = await solveChallenge(challenge);
  const json = JSON.stringify({ ...challenge, number });
  const b64 = btoa(json).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return { "X-CiteVyn-Bot-Check": b64 };
}
