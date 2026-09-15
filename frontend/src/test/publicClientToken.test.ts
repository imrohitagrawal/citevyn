/**
 * How the bundled client token resolves, now that one build arg carries it (#430).
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * `CITEVYN_DEMO_API_KEY` / `VITE_API_DEMO_KEY` were renamed to
 * `CITEVYN_PUBLIC_CLIENT_TOKEN` / `VITE_PUBLIC_CLIENT_TOKEN`. The value is PUBLIC
 * — Vite bakes it into the bundle, so every visitor can read it — but it is the
 * bearer on every `/v1/*` call, so getting the resolution wrong takes the site
 * down behind a green `/health`. That is exactly release v6 (#296): a mismatched
 * build argument, 401 on every browser call, about an hour.
 *
 * THE MIGRATION IS OVER. The Fly secret moved, and `infra/docker/Dockerfile.api`
 * declares ONE `ARG VITE_PUBLIC_CLIENT_TOKEN=local-demo-key`. The dual-name cases
 * went with the old argument; the retired spelling now has its own case proving
 * it is INERT, because "we deleted it" is an absence and an absence asserted in
 * prose is not asserted at all.
 *
 * WHY `||` AND NOT `??`
 * ---------------------
 * `??` fires only on null/undefined. Measured with docker, `--build-arg
 * VITE_PUBLIC_CLIENT_TOKEN=""` does NOT fall back to the `ARG` default — it
 * leaves the argument empty — so under `??` the bundle would carry `""`, the
 * browser would send a bare `Authorization: Bearer `, and production would 401
 * every call while `/health` stayed green. `||` treats `""` as absent, so the
 * worst an empty argument can bake is the published default, which
 * `scripts/check_bundle_key.sh` reports by name ("the bundle carries the PUBLIC
 * DEFAULT … the v6 shape").
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
 * * `the new name alone` — delete the operand from the chain.
 * * `an EMPTY new name reaches the published default` — change the `||` to `??`.
 *   This is the assertion that stops an empty build argument re-running #296;
 *   nothing else in either suite goes red for that one-character edit.
 * * `the retired name is inert` / `a retired name cannot rescue an empty new one`
 *   — re-add `import.meta.env.VITE_API_DEMO_KEY` to the chain.
 * * `neither set` — change the `"local-demo-key"` default.
 * * `the header is what the chain resolved` — the partner for all of the above:
 *   every case reads the token through the REQUEST the server receives, not
 *   through an exported constant, so a chain that resolves correctly into a
 *   variable nothing sends would still go red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const NEW_NAME = "VITE_PUBLIC_CLIENT_TOKEN";
/** The spelling #430 retired. Nothing reads it; the cases below prove that. */
const RETIRED_NAME = "VITE_API_DEMO_KEY";

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
  for (const name of [NEW_NAME, RETIRED_NAME]) {
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

describe("the bundled public client token reads one build-arg name (#430)", () => {
  it("sends the NEW name when it is set", async () => {
    expect(await bearerFor({ [NEW_NAME]: "new-name-token" })).toBe("new-name-token");
  });

  it("falls back to the published default when it is NOT set", async () => {
    expect(await bearerFor({})).toBe(PUBLISHED_DEFAULT);
  });

  it("reaches the published default on an EMPTY new name — never a bare Bearer", async () => {
    // `--build-arg VITE_PUBLIC_CLIENT_TOKEN=""` does not fall back to the ARG
    // default; docker leaves the argument empty. Under `??` this would bake ""
    // and 401 every browser call behind a green /health — #296, reached through
    // a different string. The `||` is what makes the default the worst case.
    expect(await bearerFor({ [NEW_NAME]: "" })).toBe(PUBLISHED_DEFAULT);
  });
});

describe("the retired VITE_API_DEMO_KEY spelling is inert (#430 step 2)", () => {
  it("is not read when it is the only name set", async () => {
    // Asserting the DEFAULT, not merely "not the old value": where the value
    // lands is the property, and "not X" would also hold for a typo'd fixture.
    expect(await bearerFor({ [RETIRED_NAME]: "old-name-token" })).toBe(PUBLISHED_DEFAULT);
  });

  it("cannot rescue an EMPTY new name", async () => {
    // The state a half-cleaned deploy is in. While the old operand existed this
    // returned "old-name-token"; it is the case that changes direction, so it is
    // the cheapest tripwire for an accidental restoration of the fallback.
    expect(await bearerFor({ [NEW_NAME]: "", [RETIRED_NAME]: "old-name-token" })).toBe(
      PUBLISHED_DEFAULT,
    );
  });

  it("cannot override a good new name either", async () => {
    expect(
      await bearerFor({ [NEW_NAME]: "new-name-token", [RETIRED_NAME]: "old-name-token" }),
    ).toBe("new-name-token");
  });
});

describe("non-vacuity partners", () => {
  it("distinguishes the two names, so no case can pass by reading the other", async () => {
    const viaNew = await bearerFor({ [NEW_NAME]: "sentinel-new" });
    const viaRetired = await bearerFor({ [RETIRED_NAME]: "sentinel-retired" });
    expect(viaNew).not.toBe(viaRetired);
    // Direction, not just difference: without this the pair is satisfied by a
    // chain that reads the retired name and ignores the new one.
    expect(viaNew).toBe("sentinel-new");
    expect(viaRetired).toBe(PUBLISHED_DEFAULT);
  });

  it("a token that is neither the operand nor the default can never be produced", async () => {
    // Partner for every `toBe` above: proves the helper reads the CHAIN and not
    // some constant that happens to match.
    const bearer = await bearerFor({ [NEW_NAME]: "only-this-one" });
    expect(bearer).not.toBe(PUBLISHED_DEFAULT);
    expect(bearer).not.toBe("");
  });
});
