/**
 * The two-name fallback for the bundled client token (#430).
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * `CITEVYN_DEMO_API_KEY` / `VITE_API_DEMO_KEY` are being renamed to
 * `CITEVYN_PUBLIC_CLIENT_TOKEN` / `VITE_PUBLIC_CLIENT_TOKEN`. The value is PUBLIC
 * — Vite bakes it into the bundle, so every visitor can read it — but it is the
 * bearer on every `/v1/*` call, so getting the fallback wrong takes the site down
 * behind a green `/health`. That is exactly release v6 (#296): a mismatched build
 * argument, 401 on every browser call, about an hour.
 *
 * `infra/docker/Dockerfile.api` declares BOTH build arguments during the
 * migration, so the bundle is built with both present and the precedence between
 * them is a real, shipped decision rather than a hypothetical.
 *
 * WHY `||` AND `??` ARE DIFFERENT OPERATORS HERE
 * -----------------------------------------------
 * The NEW argument's ARG default is EMPTY. `??` fires only on null/undefined, so
 * under `??` an unpassed new argument would bake `""` and BEAT a correctly-passed
 * old one — the rename would manufacture the v6 outage. `||` treats `""` as
 * absent, which is the only reason the two can coexist.
 *
 * The OLD argument keeps `??`, so its behaviour is byte-for-byte what ships
 * today: `--build-arg VITE_API_DEMO_KEY=""` still bakes `""`, NOT the published
 * default. `scripts/check_bundle_key.sh` exists to catch that exact bundle, and
 * "fixing" it here would blind the checker rather than fix the deploy.
 *
 * WHY THE MODULE IS RE-IMPORTED PER CASE
 * --------------------------------------
 * The token is resolved ONCE, at module load, into a `const`. A `vi.stubEnv`
 * after the import cannot be observed — a test written that way passes on every
 * case and proves nothing. So each case resets the module registry and imports
 * `../lib/api` fresh, which is the only way to exercise the expression that
 * actually ships.
 *
 * RED BITE (each assertion, and the one change that turns it red)
 * ---------------------------------------------------------------
 * * `new name alone` / `old name alone` — delete either operand from the chain.
 * * `the new name wins when both are set` — swap the two operands. With one name
 *   set they are indistinguishable, so only the both-set case can see the order.
 * * `an EMPTY new name falls through to the old one` — change the `||` to `??`.
 *   This is the assertion that stops the rename re-running #296; nothing else in
 *   either suite goes red for that one-character edit.
 * * `an EMPTY old name is still taken literally` — change the `??` to `||`.
 *   Turns red because the bundle would start carrying the published default
 *   where it used to carry `""`.
 * * `neither set` — change the `"local-demo-key"` default.
 * * `the header is what the chain resolved` — the partner for all of the above:
 *   every case reads the token through the REQUEST the server receives, not
 *   through an exported constant, so a chain that resolves correctly into a
 *   variable nothing sends would still go red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const NEW_NAME = "VITE_PUBLIC_CLIENT_TOKEN";
const OLD_NAME = "VITE_API_DEMO_KEY";

/** The published default, mirrored from `Settings.public_client_token`. */
const PUBLISHED_DEFAULT = "local-demo-key";

/**
 * Load `../lib/api` under exactly `env`, and return the bearer it sends.
 *
 * Observed through a stubbed `fetch`, i.e. through the header the SERVER would
 * receive. Reading an exported constant instead would pass on a chain whose
 * result never reaches a request — the failure mode this repo has hit before
 * ("assert what the consumer receives").
 *
 * `undefined` in `env` means the variable is NOT SET, which is a different case
 * from `""` and is the whole subject of two of the tests below.
 */
async function bearerFor(env: Record<string, string | undefined>): Promise<string> {
  vi.resetModules();
  vi.unstubAllEnvs();
  for (const name of [NEW_NAME, OLD_NAME]) {
    const value = env[name];
    if (value === undefined) {
      // `stubEnv(name, undefined)` DELETES the key, which is what "not passed"
      // means for a build argument Vite never saw.
      vi.stubEnv(name, undefined as unknown as string);
    } else {
      vi.stubEnv(name, value);
    }
  }
  // Live mode, so `apiFetch` actually issues the request whose header we read.
  vi.stubEnv("VITE_API_LIVE", "true");
  vi.stubEnv("VITE_API_BASE_URL", "");

  let seen: Headers | undefined;
  const fetchMock = vi.fn(async (_url: string, init: RequestInit) => {
    seen = new Headers(init.headers);
    return new Response(JSON.stringify({ session_id: "s1" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);

  const api = await import("../lib/api");
  await api.createSession();

  expect(fetchMock, "the module under test never issued a request").toHaveBeenCalled();
  const auth = seen?.get("Authorization");
  expect(auth, "no Authorization header was sent at all").toBeTruthy();
  // `\s*`, not a literal space. MEASURED: when the token resolves to "" the
  // header value is the bare word `Bearer` — `Headers` trims the trailing space —
  // so a `/^Bearer /` strip silently returns the string "Bearer" and an
  // empty-token case would assert against the wrong value. That bare `Bearer` IS
  // the wire shape of the #296 outage, so the helper has to render it as "".
  const bearer = (auth as string).replace(/^Bearer\s*/, "");
  expect(auth, "the header is not a Bearer credential at all").toMatch(/^Bearer\b/);
  return bearer;
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("the bundled public client token accepts both build-arg names (#430)", () => {
  it("sends the NEW name when only it is set", async () => {
    expect(await bearerFor({ [NEW_NAME]: "new-name-token" })).toBe("new-name-token");
  });

  it("sends the OLD name when only it is set — this is what makes it a migration", async () => {
    expect(await bearerFor({ [OLD_NAME]: "old-name-token" })).toBe("old-name-token");
  });

  it("prefers the NEW name when BOTH are set", async () => {
    // The state every machine is in for one deploy: the new build arg is passed
    // while the old one is still being passed too. If the OLD name won, the
    // cutover would silently never happen.
    expect(
      await bearerFor({ [NEW_NAME]: "new-name-token", [OLD_NAME]: "old-name-token" }),
    ).toBe("new-name-token");
  });

  it("falls through an EMPTY new name to a good old one (the #296 trap)", async () => {
    // The Dockerfile's new ARG defaults to EMPTY, so this is the DEFAULT state of
    // every deploy until the operator passes the new argument. Under `??` the
    // bundle would carry "" and 401 every browser call behind a green /health.
    expect(await bearerFor({ [NEW_NAME]: "", [OLD_NAME]: "old-name-token" })).toBe(
      "old-name-token",
    );
  });

  it("falls back to the published default when NEITHER is set", async () => {
    expect(await bearerFor({})).toBe(PUBLISHED_DEFAULT);
  });

  it("still takes an EMPTY old name literally, exactly as it does today", async () => {
    // NOT a bug being carried forward — the shape `check_bundle_key.sh` detects.
    // Turning it into the default here would hide it from the only checker that
    // looks at the SERVED bundle.
    expect(await bearerFor({ [OLD_NAME]: "" })).toBe("");
  });

  it("takes both-empty literally too, rather than resurrecting the default", async () => {
    expect(await bearerFor({ [NEW_NAME]: "", [OLD_NAME]: "" })).toBe("");
  });

  it("an EMPTY new name with NO old name still reaches the default", async () => {
    expect(await bearerFor({ [NEW_NAME]: "" })).toBe(PUBLISHED_DEFAULT);
  });
});

describe("non-vacuity partners", () => {
  it("distinguishes the two names, so no case can pass by reading the other", async () => {
    const viaNew = await bearerFor({ [NEW_NAME]: "sentinel-new" });
    const viaOld = await bearerFor({ [OLD_NAME]: "sentinel-old" });
    expect(viaNew).not.toBe(viaOld);
  });

  it("a token that is neither operand can never be produced", async () => {
    // Partner for every `toBe` above: proves the helper reads the CHAIN and not
    // some constant that happens to match.
    const bearer = await bearerFor({ [NEW_NAME]: "only-this-one" });
    expect(bearer).not.toBe(PUBLISHED_DEFAULT);
    expect(bearer).not.toBe("");
  });
});
