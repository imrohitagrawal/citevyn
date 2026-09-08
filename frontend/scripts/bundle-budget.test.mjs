/**
 * Tests for the eager-bundle budget gate (#323).
 *
 * The gate is a GUARD, and the defect it had was that it did not guard: a
 * mistyped budget key made `gz > undefined` evaluate to `false`, so it printed
 * "bundle budget OK" and exited 0 for a bundle of any size. So these tests are
 * written the way a guard's tests have to be — one block drives the REAL script
 * as a REAL process and asserts the EXIT CODE and the printed NUMBERS, because
 * the exit code is the only thing CI actually consumes and a report line that
 * is always "0 B" tells nobody anything.
 *
 * NOTE ON WHERE THE SELECTION GUARD LIVES: the assertion that vitest is
 * configured to run THIS file cannot live in THIS file — reverting the config
 * would deselect the guard along with everything it guards. It lives in
 * src/test/buildGuards.test.ts, which the original `src/**` glob selects.
 */
import { describe, it, expect } from "vitest";
import { gzipSync } from "node:zlib";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { load } from "js-yaml";

import {
  BUDGET_KEY,
  LAZY_BUDGET_KEY,
  MIN_LAZY_CHUNKS,
  MIN_PLAUSIBLE_GZIP,
  parseBudget,
  buildCommand,
  eagerChunkFilesFromManifest,
  nonEagerChunkFilesFromManifest,
  resolveAssetPath,
  measureEagerGraph,
  evaluate,
  evaluateLazyChunks,
  renderRecordedReport,
} from "./bundle-budget.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");
const scriptPath = join(here, "check-bundle-size.mjs");

/** The shape Vite 6 actually emits — verified against a real build of this app. */
const MANIFEST_SINGLE = {
  "index.html": { file: "assets/index-abc.js", name: "index", isEntry: true, css: ["assets/index-abc.css"] },
};
/** A real split build: entry + a statically-imported vendor chunk + lazy chunks. */
const MANIFEST_SPLIT = {
  "index.html": {
    file: "assets/index-abc.js",
    isEntry: true,
    imports: ["_vendor-xyz.js"],
    dynamicImports: ["src/components/AuthModal.tsx"],
  },
  "_vendor-xyz.js": { file: "assets/vendor-xyz.js" },
  "src/components/AuthModal.tsx": { file: "assets/AuthModal-lazy.js" },
};

/**
 * THE REAL MANIFEST'S SHAPE, reduced (#372) — and the reason the lazy set is a
 * set difference rather than a graph walk.
 *
 * Verified against a real manifest emitted by this app's own build: the entry
 * declares NO `imports` key at all, and each `_`-prefixed shared chunk declares
 * `imports: ["index.html"]` — a BACK-EDGE pointing at the ENTRY. MANIFEST_SPLIT
 * above does not model that, so it cannot catch the defect: an implementation
 * that starts at `dynamicImports` and follows `imports` reaches "index.html",
 * emits the 62,974 B entry chunk as "lazy", and hands it the per-chunk
 * allowance. This fixture is the test such a reimplementation fails.
 */
const MANIFEST_BACKEDGE = {
  "index.html": {
    file: "assets/index-abc.js",
    isEntry: true,
    dynamicImports: ["src/components/AuthModal.tsx"],
  },
  "_shared-xyz.js": { file: "assets/shared-xyz.js", imports: ["index.html"] },
  "src/components/AuthModal.tsx": {
    file: "assets/AuthModal-lazy.js",
    isDynamicEntry: true,
    imports: ["index.html", "_shared-xyz.js"],
  },
};

/**
 * Entry + exactly one lazy chunk — the smallest shape the lazy gate accepts,
 * and the default for the process-level fixtures below. MANIFEST_SINGLE has no
 * lazy chunk at all, which since #372 is a hard failure (MIN_LAZY_CHUNKS), so a
 * test about the EAGER number cannot use it without failing for a reason it is
 * not about.
 */
const MANIFEST_LAZY = {
  "index.html": {
    file: "assets/index-abc.js",
    isEntry: true,
    dynamicImports: ["src/components/Nudge.tsx"],
    css: ["assets/index-abc.css"],
  },
  "src/components/Nudge.tsx": { file: "assets/Nudge-lazy.js", isDynamicEntry: true },
};

/**
 * A budget file carrying BOTH enforced keys.
 *
 * The gate parses both since #372, so a fixture holding only the eager key
 * fails for a reason no test here is about. The lazy ceiling defaults
 * deliberately HIGH: a test about the EAGER number must not be able to go red
 * on the lazy one, and vice versa.
 */
const budgetFile = (eagerMax, lazyMax = 1_000_000) =>
  `{"${BUDGET_KEY}": ${eagerMax}, "${LAZY_BUDGET_KEY}": ${lazyMax}}`;

describe("parseBudget — a missing or malformed budget must FAIL, not pass silently (#323)", () => {
  it("returns the ceiling for a well-formed budget file", () => {
    expect(parseBudget(`{"${BUDGET_KEY}": 66000}`)).toBe(66000);
  });

  // This is the #323 defect itself. Before the fix the mistyped key produced
  // `undefined`, and `gz > undefined` is false, so the gate passed.
  it("throws when the key is mistyped, rather than yielding undefined", () => {
    expect(() => parseBudget(`{"${BUDGET_KEY}TYPO": 66000}`)).toThrow(/must be a positive integer/);
  });

  it("throws when the key is absent entirely", () => {
    expect(() => parseBudget(`{"_comment": ["a note"]}`)).toThrow(/must be a positive integer/);
  });

  it.each([
    ["a string ceiling", `{"${BUDGET_KEY}": "66000"}`],
    ["null", `{"${BUDGET_KEY}": null}`],
    ["zero", `{"${BUDGET_KEY}": 0}`],
    ["a negative number", `{"${BUDGET_KEY}": -1}`],
    ["a float", `{"${BUDGET_KEY}": 66000.5}`],
    ["a boolean", `{"${BUDGET_KEY}": true}`],
  ])("throws for %s", (_label, text) => {
    expect(() => parseBudget(text)).toThrow(/must be a positive integer/);
  });

  it("throws on invalid JSON instead of crashing opaquely", () => {
    expect(() => parseBudget("{not json")).toThrow(/not valid JSON/);
  });

  it("throws when the file is a JSON array or scalar rather than an object", () => {
    expect(() => parseBudget("[66000]")).toThrow(/must contain a JSON object/);
    expect(() => parseBudget("null")).toThrow(/must contain a JSON object/);
  });

  it("accepts the budget file that actually ships", () => {
    expect(parseBudget(readFileSync(join(frontendRoot, "bundle-budget.json"), "utf8"))).toBeGreaterThan(0);
  });
});

/**
 * #372 routed the second ceiling through the SAME `parseBudget`, on purpose:
 * the validation the #323 defect cost us is written once and covers both keys.
 * These re-run the whole #323 matrix against BOTH of them, so a future third
 * key added by copy-paste cannot quietly skip the validation.
 */
describe("parseBudget over BOTH enforced keys (#372)", () => {
  const keys = [[BUDGET_KEY], [LAZY_BUDGET_KEY]];

  it.each(keys)("reads %s when it is a well-formed positive integer", (key) => {
    expect(parseBudget(`{"${key}": 1234}`, key)).toBe(1234);
  });

  it.each(keys)("throws when %s is absent entirely", (key) => {
    expect(() => parseBudget(`{"_comment": ["a note"]}`, key)).toThrow(/must be a positive integer/);
  });

  it.each(keys)("throws when %s is mistyped rather than yielding undefined", (key) => {
    expect(() => parseBudget(`{"${key}TYPO": 6328}`, key)).toThrow(/must be a positive integer/);
  });

  it.each([
    ["a string ceiling", `"6328"`],
    ["null", `null`],
    ["zero", `0`],
    ["a negative number", `-1`],
    ["a float", `6328.5`],
    ["a boolean", `true`],
  ])("throws for %s, for each key", (_label, value) => {
    for (const [key] of keys) {
      expect(() => parseBudget(`{"${key}": ${value}}`, key)).toThrow(/must be a positive integer/);
    }
  });

  // The error must NAME the key it was looking for. Without this the two keys
  // produce an identical message and a reader cannot tell which one is wrong.
  it("names the key it could not read", () => {
    expect(() => parseBudget(`{}`, LAZY_BUDGET_KEY)).toThrow(new RegExp(LAZY_BUDGET_KEY));
    expect(() => parseBudget(`{}`, BUDGET_KEY)).toThrow(new RegExp(BUDGET_KEY));
  });

  // The default argument is what keeps every pre-#372 call site working.
  it("defaults to the eager key when no key is given", () => {
    expect(parseBudget(`{"${BUDGET_KEY}": 64479}`)).toBe(64479);
    expect(() => parseBudget(`{"${LAZY_BUDGET_KEY}": 6328}`)).toThrow(/must be a positive integer/);
  });

  it("reads BOTH ceilings out of the budget file that actually ships", () => {
    const text = readFileSync(join(frontendRoot, "bundle-budget.json"), "utf8");
    expect(parseBudget(text, BUDGET_KEY)).toBeGreaterThan(0);
    expect(parseBudget(text, LAZY_BUDGET_KEY)).toBeGreaterThan(0);
  });
});

/**
 * Every one of these flags was uncovered in the first version of this fix: a
 * reviewer deleted `--config vite.config.ts` (which the commit message called
 * "load-bearing") and all 36 tests still passed.
 */
describe("buildCommand — the flags that decide WHICH artifact gets measured", () => {
  it("builds the production mode that `npm run build` builds", () => {
    const { args } = buildCommand();
    expect(args.join(" ")).toContain("--mode production");
  });

  it("pins the TypeScript config, so a stale compiled vite.config.js cannot win", () => {
    expect(buildCommand().args.join(" ")).toContain("--config vite.config.ts");
  });

  it("asks for the manifest, which is the measurement input", () => {
    expect(buildCommand().args).toContain("--manifest");
  });

  it("builds the LIVE variant, which is what the Dockerfile ships", () => {
    expect(buildCommand().env.VITE_API_LIVE).toBe("true");
  });

  /**
   * `--mode production` does NOT set NODE_ENV, and React's package exports
   * branch on it. Vitest sets NODE_ENV=test, so every caller of buildCommand()
   * that runs UNDER vitest — scripts/lazy-landing-strip.test.mjs does — was
   * building react-dom's DEVELOPMENT copy and calling it "the chunk that
   * actually ships". Measured with the same flags into the same outDir:
   * NODE_ENV=test gives a 118,897 B gzip entry and a 6,067 B strip;
   * NODE_ENV=production gives 62,974 B and 4,722 B, matching `npm run
   * check:bundle` content hash for content hash.
   *
   * TURNS RED IF: the `NODE_ENV: "production"` pair is dropped from
   * buildCommand()'s `env`.
   */
  it("pins NODE_ENV=production, so a caller under vitest cannot measure a React DEV build", () => {
    expect(buildCommand().env.NODE_ENV).toBe("production");
  });

  it("invokes vite build and nothing else", () => {
    const { cmd, args } = buildCommand();
    expect(cmd).toBe("npx");
    expect(args.slice(0, 2)).toEqual(["vite", "build"]);
  });
});

describe("eagerChunkFilesFromManifest — eager is what the manifest says, not what preload hints say", () => {
  it("returns the entry chunk for an unsplit build", () => {
    expect(eagerChunkFilesFromManifest(MANIFEST_SINGLE)).toEqual(["assets/index-abc.js"]);
  });

  // The defect that survived the FIRST fix: with build.modulePreload:false Vite
  // emits no <link rel="modulepreload">, so an index.html-derived gate missed a
  // statically-imported 45.69 kB vendor chunk entirely. The manifest does not.
  it("follows static imports, which no preload link may exist for", () => {
    expect(eagerChunkFilesFromManifest(MANIFEST_SPLIT)).toEqual([
      "assets/index-abc.js",
      "assets/vendor-xyz.js",
    ]);
  });

  it("does NOT follow dynamicImports — that is the lazy half", () => {
    expect(eagerChunkFilesFromManifest(MANIFEST_SPLIT)).not.toContain("assets/AuthModal-lazy.js");
  });

  it("excludes CSS even when the entry declares it", () => {
    expect(eagerChunkFilesFromManifest(MANIFEST_SINGLE).some((f) => f.endsWith(".css"))).toBe(false);
  });

  it("walks the graph transitively, not one level", () => {
    const m = {
      "index.html": { file: "a.js", isEntry: true, imports: ["b"] },
      b: { file: "b.js", imports: ["c"] },
      c: { file: "c.js" },
    };
    expect(eagerChunkFilesFromManifest(m)).toEqual(["a.js", "b.js", "c.js"]);
  });

  it("terminates on an import cycle instead of hanging", () => {
    const m = {
      "index.html": { file: "a.js", isEntry: true, imports: ["b"] },
      b: { file: "b.js", imports: ["index.html"] },
    };
    expect(eagerChunkFilesFromManifest(m)).toEqual(["a.js", "b.js"]);
  });

  it("counts a file once when two manifest keys emit it", () => {
    const m = {
      "index.html": { file: "a.js", isEntry: true, imports: ["b", "c"] },
      b: { file: "shared.js" },
      c: { file: "shared.js" },
    };
    expect(eagerChunkFilesFromManifest(m)).toEqual(["a.js", "shared.js"]);
  });

  it("includes every entry when a build has more than one", () => {
    const m = {
      "index.html": { file: "a.js", isEntry: true },
      "admin.html": { file: "b.js", isEntry: true },
    };
    expect(eagerChunkFilesFromManifest(m)).toEqual(["a.js", "b.js"]);
  });

  // Fail CLOSED in every shape-changed case: measuring 0 B and reporting
  // headroom is the silent pass this file exists to remove.
  it("throws when no chunk is marked isEntry", () => {
    expect(() => eagerChunkFilesFromManifest({ "_x.js": { file: "x.js" } })).toThrow(/no entry chunk/);
  });

  it("throws when an import names a chunk the manifest does not contain", () => {
    const m = { "index.html": { file: "a.js", isEntry: true, imports: ["_missing.js"] } };
    expect(() => eagerChunkFilesFromManifest(m)).toThrow(/unknown chunk/);
  });

  it("throws when a record carries no file", () => {
    expect(() => eagerChunkFilesFromManifest({ "index.html": { isEntry: true } })).toThrow(/no file/);
  });

  it("throws when the manifest is not an object", () => {
    expect(() => eagerChunkFilesFromManifest([])).toThrow(/must contain a JSON object/);
    expect(() => eagerChunkFilesFromManifest(null)).toThrow(/must contain a JSON object/);
  });
});

/**
 * #372. The lazy set is a SET DIFFERENCE against the eager closure, and every
 * test here exists because the obvious alternative — walk out from
 * `dynamicImports`, follow `imports` — is wrong on the manifest this app really
 * emits.
 */
describe("nonEagerChunkFilesFromManifest — the lazy set is a set difference, never a walk", () => {
  it("returns only the lazy chunk for a split build", () => {
    // Turns red if the eager closure is subtracted wrongly: the entry or the
    // statically-imported vendor chunk appearing here means a lazy allowance is
    // being handed to bytes that block first paint.
    expect(nonEagerChunkFilesFromManifest(MANIFEST_SPLIT).files).toEqual([
      "assets/AuthModal-lazy.js",
    ]);
  });

  it("classifies the ENTRY as eager even though a shared chunk declares a back-edge to it", () => {
    // THE TEST A TRAVERSAL-BASED REIMPLEMENTATION FAILS. `_shared-xyz.js`
    // declares `imports: ["index.html"]`, which is what Vite really emits here.
    // A walk out from the lazy roots reaches the entry through that edge.
    const { files } = nonEagerChunkFilesFromManifest(MANIFEST_BACKEDGE);
    expect(files).not.toContain("assets/index-abc.js");
    // PARTNER for that absence: the shared chunk on the far side of the
    // back-edge IS lazy, so the check above is not passing because the
    // classifier returned nothing at all.
    expect(files.sort()).toEqual(["assets/AuthModal-lazy.js", "assets/shared-xyz.js"]);
    expect(eagerChunkFilesFromManifest(MANIFEST_BACKEDGE)).toEqual(["assets/index-abc.js"]);
  });

  it("excludes a chunk reachable BOTH statically from the entry and dynamically from a lazy chunk", () => {
    // Budgeted once, by the eager key — it blocks first paint, so the per-chunk
    // lazy allowance must not also apply to it.
    const m = {
      "index.html": {
        file: "assets/index-abc.js",
        isEntry: true,
        imports: ["_shared.js"],
        dynamicImports: ["lazy.tsx"],
      },
      "_shared.js": { file: "assets/shared.js" },
      "lazy.tsx": { file: "assets/lazy.js", imports: ["_shared.js"] },
    };
    expect(nonEagerChunkFilesFromManifest(m).files).toEqual(["assets/lazy.js"]);
  });

  it("counts a file once when two manifest keys emit it", () => {
    const m = {
      "index.html": { file: "assets/index-abc.js", isEntry: true, dynamicImports: ["a", "b"] },
      a: { file: "assets/shared-lazy.js" },
      b: { file: "assets/shared-lazy.js" },
    };
    expect(nonEagerChunkFilesFromManifest(m).files).toEqual(["assets/shared-lazy.js"]);
  });

  it("skips a top-level non-.js record and REPORTS the count, so no skip is silent", () => {
    // The CSS record comes FIRST on purpose: with `continue` turned into
    // `break` the lazy chunk after it would vanish and the collection would be
    // empty. A one-record fixture cannot tell those apart.
    const m = {
      "style.css": { file: "assets/style-abc.css", src: "style.css" },
      "index.html": { file: "assets/index-abc.js", isEntry: true, dynamicImports: ["lazy.tsx"] },
      "lazy.tsx": { file: "assets/lazy.js" },
    };
    const { files, skippedNonJs } = nonEagerChunkFilesFromManifest(m);
    expect(files).toEqual(["assets/lazy.js"]);
    // The partner for a count: it is 1 because a CSS record really is there,
    // not because the counter is stuck.
    expect(skippedNonJs).toBe(1);
    expect(nonEagerChunkFilesFromManifest(MANIFEST_SPLIT).skippedNonJs).toBe(0);
  });

  it("throws on a record with no usable file, rather than skipping it silently", () => {
    const m = {
      "index.html": { file: "assets/index-abc.js", isEntry: true, dynamicImports: ["lazy.tsx"] },
      "lazy.tsx": { file: "assets/lazy.js" },
      "broken.tsx": { name: "broken" },
    };
    expect(() => nonEagerChunkFilesFromManifest(m)).toThrow(/no usable file/);
  });

  it("inherits the eager walker's fail-closed no-entry throw", () => {
    expect(() => nonEagerChunkFilesFromManifest({ "_x.js": { file: "assets/x.js" } })).toThrow(
      /no entry chunk/,
    );
  });
});

/**
 * #372. The per-chunk verdict, written in the FAIL-CLOSED form. `c.gzip > max`
 * with an `undefined` ceiling yields an EMPTY violator list and passes silently
 * — that is #323 exactly, one function over.
 */
describe("evaluateLazyChunks — every violator, fail-closed, with a partner for the empty set", () => {
  const chunk = (file, gzip) => ({ file, raw: gzip * 3, gzip });

  it("passes when every chunk is under the ceiling", () => {
    const r = evaluateLazyChunks({ chunks: [chunk("a.js", 100), chunk("b.js", 200)], max: 500 });
    expect(r.ok).toBe(true);
    expect(r.violators).toEqual([]);
  });

  it("passes when a chunk sits exactly ON the ceiling", () => {
    expect(evaluateLazyChunks({ chunks: [chunk("a.js", 500)], max: 500 }).ok).toBe(true);
  });

  it("FAILS when a chunk is one byte over", () => {
    expect(evaluateLazyChunks({ chunks: [chunk("a.js", 501)], max: 500 }).ok).toBe(false);
  });

  // C4: the guard iterates a COLLECTION, so a single-item fixture cannot tell
  // a `break` from a `continue`, nor a first-only from a last-only check.
  it("FAILS on a LATER chunk when the FIRST one is compliant", () => {
    const r = evaluateLazyChunks({ chunks: [chunk("ok.js", 10), chunk("fat.js", 900)], max: 500 });
    expect(r.ok).toBe(false);
    expect(r.violators.map((c) => c.file)).toEqual(["fat.js"]);
  });

  it("FAILS on the FIRST chunk when a LATER one is compliant (the mirror)", () => {
    const r = evaluateLazyChunks({ chunks: [chunk("fat.js", 900), chunk("ok.js", 10)], max: 500 });
    expect(r.ok).toBe(false);
    expect(r.violators.map((c) => c.file)).toEqual(["fat.js"]);
  });

  it("names EVERY violator, not the first", () => {
    const r = evaluateLazyChunks({
      chunks: [chunk("a.js", 900), chunk("ok.js", 10), chunk("b.js", 800)],
      max: 500,
    });
    expect(r.violators.map((c) => c.file)).toEqual(["a.js", "b.js"]);
  });

  // THE FAIL-CLOSED FORM ITSELF. Written `c.gzip > max` this returns ok:true.
  it("FAILS CLOSED when the ceiling is undefined, instead of finding no violators", () => {
    const r = evaluateLazyChunks({ chunks: [chunk("a.js", 10)], max: undefined });
    expect(r.ok).toBe(false);
    expect(r.violators).toHaveLength(1);
  });

  // C5: a maximum over an empty collection passes for a bundle of ANY shape.
  it("THROWS on an empty lazy collection rather than reporting no violators", () => {
    expect(() => evaluateLazyChunks({ chunks: [], max: 500 })).toThrow(/no lazy chunks found/);
    expect(MIN_LAZY_CHUNKS).toBeGreaterThan(0);
  });

  // The partner for that throw: the smallest non-empty collection passes, so
  // the check above is a guard and not a blanket refusal.
  it("accepts a single compliant chunk", () => {
    expect(evaluateLazyChunks({ chunks: [chunk("only.js", 10)], max: 500 }).ok).toBe(true);
  });

  /**
   * The eager side has had MIN_PLAUSIBLE_GZIP since #323; this side had no size
   * floor at all. Reproduced before the fix: two zero-byte lazy chunks reported
   * `20 B gzip (headroom 6308 B)` and exited 0 — a truncated build reading as
   * generous headroom.
   *
   * A blanket GZIP floor is not available: the shipping build legitimately
   * emits a 66 B raw / 79 B gzip shared chunk, so any floor above 79 B would go
   * red on a healthy build. `raw > 0` is the only floor true of every real
   * chunk.
   *
   * TURNS RED IF: the `chunks.filter((c) => !(c.raw > 0))` throw is removed
   * from evaluateLazyChunks (a 0-byte chunk then returns ok:true).
   */
  it("THROWS on a lazy chunk emitted with zero raw bytes, rather than reporting headroom", () => {
    expect(() =>
      evaluateLazyChunks({ chunks: [{ file: "empty.js", raw: 0, gzip: 20 }], max: 500 }),
    ).toThrow(/no bytes at all/);
    // Named, not just counted — a report that says "a chunk" costs a round trip.
    expect(() =>
      evaluateLazyChunks({
        chunks: [chunk("real.js", 100), { file: "empty.js", raw: 0, gzip: 20 }],
        max: 500,
      }),
    ).toThrow(/empty\.js/);
  });

  // PARTNER for that floor, in BOTH directions: it must not fire on the
  // smallest chunk this app really emits (66 B raw / 79 B gzip, measured), and
  // `raw` is checked, not `gzip` — a gzip floor at 79 B would reject it.
  it("accepts the 66 B raw shared chunk the real build emits, so the floor is not a blanket", () => {
    const r = evaluateLazyChunks({
      chunks: [{ file: "assets/index-C-L.js", raw: 66, gzip: 79 }],
      max: 6328,
    });
    expect(r.ok).toBe(true);
  });

  it("reports the LARGEST chunk, and the real numbers, in its line", () => {
    const r = evaluateLazyChunks({
      chunks: [chunk("small.js", 10), chunk("big.js", 400), chunk("mid.js", 200)],
      max: 500,
    });
    expect(r.largest.file).toBe("big.js");
    expect(r.line).toContain("big.js");
    expect(r.line).toContain("400 B gzip");
    expect(r.line).toContain("ceiling 500 B");
    expect(r.line).toContain("headroom 100 B");
  });
});

/**
 * #372's other half: the numbers nothing gates, printed anyway. A ceiling that
 * fires on the wrong change was rejected; a number the reader sees was not.
 */
describe("renderRecordedReport — the real numbers, not constants", () => {
  const measured = (sizes) =>
    measureEagerGraph({
      files: sizes.map((_, i) => `assets/c${i}.js`),
      readAsset: (f) => Buffer.alloc(sizes[Number(f.match(/c(\d+)\.js/)[1])], "x"),
      gzipSize: (bytes) => bytes.length, // identity, so the arithmetic is visible
    });

  const report = () =>
    renderRecordedReport({
      eager: measured([60000]),
      lazy: measured([4000, 1500]),
      max: 6328,
      skippedNonJs: 2,
    }).join("\n");

  it("prints the per-chunk table, largest first, with each chunk's headroom", () => {
    const text = report();
    expect(text).toContain("lazy chunks: 2, ceiling 6328 B each");
    expect(text).toContain("assets/c0.js — 4000 B gzip (headroom 2328 B)");
    expect(text).toContain("assets/c1.js — 1500 B gzip (headroom 4828 B)");
    expect(text.indexOf("assets/c0.js")).toBeLessThan(text.indexOf("assets/c1.js"));
    expect(text).toContain("largest lazy chunk assets/c0.js (4000 B gzip)");
  });

  it("prints the lazy SUM and the all-JS TOTAL, both labelled RECORDED, NOT GATED", () => {
    const text = report();
    expect(text).toContain("RECORDED, NOT GATED — lazy gzip SUM 5500 B over 2 chunks");
    // "(manifest chunks only)" is load-bearing, not decoration: dist/about-theme
    // .js comes from frontend/public/ and is in neither term, so an unqualified
    // "all-JS" would name more than the arithmetic covers.
    expect(text).toContain(
      "RECORDED, NOT GATED — all-JS gzip TOTAL (manifest chunks only) 65500 B over 3 chunks " +
        "(eager 60000 + lazy 5500)",
    );
  });

  it("prints the count of records skipped as non-.js, so a silent skip is impossible", () => {
    expect(report()).toContain("manifest records skipped as non-.js: 2");
  });

  // The partner every report assertion needs: change the inputs and EVERY
  // number changes. A report hardcoded to constants passes each assertion above
  // in isolation and fails here.
  it("moves with its inputs rather than being hardcoded", () => {
    const other = renderRecordedReport({
      eager: measured([1000]),
      lazy: measured([70]),
      max: 99,
      skippedNonJs: 0,
    }).join("\n");
    expect(other).toContain("lazy chunks: 1, ceiling 99 B each");
    expect(other).toContain("assets/c0.js — 70 B gzip (headroom 29 B)");
    expect(other).toContain("RECORDED, NOT GATED — lazy gzip SUM 70 B over 1 chunks");
    expect(other).toContain(
      "RECORDED, NOT GATED — all-JS gzip TOTAL (manifest chunks only) 1070 B over 2 chunks",
    );
    expect(other).toContain("manifest records skipped as non-.js: 0");
    expect(other).not.toContain("6328");
    expect(other).not.toContain("5500");
  });
});

describe("resolveAssetPath — containment, with symlinks resolved", () => {
  const identity = (p) => p;

  it("resolves a normal asset under dist", () => {
    expect(resolveAssetPath({ distDir: "/d", file: "assets/x.js", realpath: identity })).toBe(
      join("/d", "assets", "x.js"),
    );
  });

  // A trailing slash made the first version reject EVERY legitimate asset,
  // because normalize() preserves it while the containment test appended sep.
  it("tolerates a trailing separator on distDir", () => {
    expect(resolveAssetPath({ distDir: `/d${sep}`, file: "assets/x.js", realpath: identity })).toBe(
      join("/d", "assets", "x.js"),
    );
  });

  it.each([
    ["parent traversal", "../outside.js"],
    ["deep traversal", "../../../../etc/hosts"],
    ["an absolute path", "/etc/hosts"],
    ["dist itself", "."],
  ])("refuses %s", (_label, file) => {
    expect(() => resolveAssetPath({ distDir: "/d", file, realpath: identity })).toThrow(
      /outside dist/,
    );
  });

  it("refuses a sibling directory that merely shares the dist prefix", () => {
    expect(() =>
      resolveAssetPath({ distDir: "/d/dist", file: "../dist-evil/x.js", realpath: identity }),
    ).toThrow(/outside dist/);
  });

  // The containment check is lexical; without realpath a symlink INSIDE dist
  // pointing outside it passed, and the gate measured a file from elsewhere.
  it("refuses a symlink that escapes dist, using the REAL path", () => {
    const fakeRealpath = (p) => (p.endsWith("link.js") ? "/somewhere/else/big.js" : p);
    expect(() =>
      resolveAssetPath({ distDir: "/d", file: "assets/link.js", realpath: fakeRealpath }),
    ).toThrow(/outside dist/);
  });

  it("allows a symlink that stays inside dist", () => {
    const fakeRealpath = (p) => (p.endsWith("link.js") ? join("/d", "assets", "real.js") : p);
    expect(resolveAssetPath({ distDir: "/d", file: "assets/link.js", realpath: fakeRealpath })).toBe(
      join("/d", "assets", "real.js"),
    );
  });
});

describe("measureEagerGraph + evaluate", () => {
  const graphOf = (sizes) =>
    measureEagerGraph({
      files: sizes.map((_, i) => `assets/c${i}.js`),
      readAsset: (f) => Buffer.alloc(sizes[Number(f.match(/c(\d+)\.js/)[1])], "x"),
      gzipSize: (bytes) => bytes.length, // identity, so the arithmetic is visible
    });

  it("sums every eager file rather than taking the largest", () => {
    const m = graphOf([10000, 25000, 3000]);
    expect(m.totalGzip).toBe(38000);
    expect(m.files).toHaveLength(3);
  });

  it("passes when the total is under the ceiling", () => {
    expect(evaluate({ measurement: graphOf([10000]), max: 20000 }).ok).toBe(true);
  });

  it("passes when the total exactly equals the ceiling", () => {
    expect(evaluate({ measurement: graphOf([20000]), max: 20000 }).ok).toBe(true);
  });

  it("FAILS when the total exceeds the ceiling by one byte", () => {
    const r = evaluate({ measurement: graphOf([20001]), max: 20000 });
    expect(r.ok).toBe(false);
    expect(r.headroom).toBe(-1);
  });

  // The defect in miniature: two chunks that each fit but together do not.
  it("FAILS when the chunks individually fit but the graph does not", () => {
    expect(evaluate({ measurement: graphOf([15000, 15000]), max: 20000 }).ok).toBe(false);
  });

  it("reports the real numbers in the line, not a constant", () => {
    const r = evaluate({ measurement: graphOf([12000, 8000]), max: 25000 });
    expect(r.line).toContain("20000 B gzip total");
    expect(r.line).toContain("budget 25000 B");
    expect(r.line).toContain("headroom 5000 B");
    expect(r.line).toContain("assets/c0.js (12000 B)");
  });

  // A check that counts nothing must not read as success.
  it("throws rather than passing when the graph measures implausibly small", () => {
    expect(() => evaluate({ measurement: graphOf([0]), max: 66000 })).toThrow(/plausibility floor/);
    expect(() => evaluate({ measurement: graphOf([MIN_PLAUSIBLE_GZIP - 1]), max: 66000 })).toThrow(
      /plausibility floor/,
    );
  });

  it("accepts a graph exactly at the plausibility floor", () => {
    expect(evaluate({ measurement: graphOf([MIN_PLAUSIBLE_GZIP]), max: 66000 }).ok).toBe(true);
  });

  it("really gzips when handed a real gzip function", () => {
    const m = measureEagerGraph({
      files: ["assets/a.js"],
      readAsset: () => Buffer.from("compress me ".repeat(500)),
      gzipSize: (bytes) => gzipSync(bytes).length,
    });
    expect(m.totalGzip).toBeGreaterThan(0);
    expect(m.totalGzip).toBeLessThan(m.totalRaw);
  });
});

/**
 * End-to-end: run the REAL script as a REAL process and assert the exit code.
 *
 * Everything above tests functions. CI consumes an exit code, and the #323
 * defect was precisely that a broken gate still exited 0.
 */
describe("check-bundle-size.mjs as a process — the exit code is what CI consumes", () => {
  // Big enough to clear MIN_PLAUSIBLE_GZIP with real gzip: random-ish bytes,
  // not a repeated character (which compresses to almost nothing).
  const incompressible = (n, seed) => {
    let x = seed;
    return Buffer.from(
      Array.from({ length: n }, () => {
        x = (x * 1103515245 + 12345) & 0x7fffffff;
        return x % 256;
      }),
    );
  };

  function fixture({ budgetJson, manifest, assets }) {
    const dir = mkdtempSync(join(tmpdir(), "bundle-gate-"));
    const dist = join(dir, "dist");
    mkdirSync(join(dist, "assets"), { recursive: true });
    mkdirSync(join(dist, ".vite"), { recursive: true });
    writeFileSync(join(dist, ".vite", "manifest.json"), JSON.stringify(manifest));
    for (const [name, content] of Object.entries(assets)) {
      writeFileSync(join(dist, "assets", name), content);
    }
    const budgetPath = join(dir, "budget.json");
    writeFileSync(budgetPath, budgetJson);
    return { dir, dist, budgetPath };
  }

  const ENTRY_BYTES = incompressible(40000, 7);
  const VENDOR_BYTES = incompressible(40000, 99);
  const LAZY_BYTES = incompressible(2000, 11);
  const ENTRY_GZ = gzipSync(ENTRY_BYTES).length;
  const VENDOR_GZ = gzipSync(VENDOR_BYTES).length;
  const LAZY_GZ = gzipSync(LAZY_BYTES).length;

  // Since #372 the gate refuses a build with NO lazy chunks (MIN_LAZY_CHUNKS),
  // so the default fixture carries one. Tests about the EAGER number get a
  // deliberately generous lazy ceiling from `budgetFile`, and vice versa.
  function runGate({
    budgetJson,
    manifest = MANIFEST_LAZY,
    assets = { "index-abc.js": ENTRY_BYTES, "Nudge-lazy.js": LAZY_BYTES },
  }) {
    const { dist, budgetPath } = fixture({ budgetJson, manifest, assets });
    const r = spawnSync(process.execPath, [scriptPath, "--dist", dist, "--budget", budgetPath], {
      encoding: "utf8",
    });
    return { status: r.status, stdout: r.stdout ?? "", stderr: r.stderr ?? "" };
  }

  it("exits 0 for a graph under budget, and prints the REAL measured numbers", () => {
    const r = runGate({ budgetJson: budgetFile(ENTRY_GZ + 500) });
    expect(r.status).toBe(0);
    expect(r.stdout).toMatch(/bundle budget OK/);
    // Pin the arithmetic the reader relies on: a report line hardcoded to zeros
    // would otherwise pass every other assertion here.
    expect(r.stdout).toContain(`${ENTRY_GZ} B gzip total`);
    expect(r.stdout).toContain(`budget ${ENTRY_GZ + 500} B`);
    expect(r.stdout).toContain("headroom 500 B");
  });

  it("exits 1 for a graph over budget", () => {
    const r = runGate({ budgetJson: budgetFile(ENTRY_GZ - 1) });
    expect(r.status).toBe(1);
    expect(r.stderr).toMatch(/BUNDLE BUDGET EXCEEDED/);
  });

  // THE #323 REGRESSION. Before the fix this exited 0 and printed
  // "bundle budget OK — ... (budget undefined B, headroom NaN B)". The LAZY key
  // is present and valid, so the only thing that can fail this is the eager one.
  it("exits NON-ZERO when the budget key is mistyped", () => {
    const r = runGate({ budgetJson: `{"${BUDGET_KEY}TYPO": 1, "${LAZY_BUDGET_KEY}": 1000000}` });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
    expect(`${r.stdout}${r.stderr}`).not.toMatch(/NaN/);
  });

  // C3, the #372 half: the SAME defect one function over. The eager key is
  // present and valid here, so only the lazy key can fail this.
  it.each([
    ["mistyped", `{"${BUDGET_KEY}": 10000000, "${LAZY_BUDGET_KEY}TYPO": 6328}`],
    ["absent entirely", `{"${BUDGET_KEY}": 10000000}`],
    ["a string", `{"${BUDGET_KEY}": 10000000, "${LAZY_BUDGET_KEY}": "6328"}`],
    ["zero", `{"${BUDGET_KEY}": 10000000, "${LAZY_BUDGET_KEY}": 0}`],
    ["a float", `{"${BUDGET_KEY}": 10000000, "${LAZY_BUDGET_KEY}": 6328.5}`],
  ])("exits NON-ZERO when the lazy chunk key is %s, with no NaN in the output", (_label, json) => {
    const r = runGate({ budgetJson: json });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
    expect(`${r.stdout}${r.stderr}`).not.toMatch(/NaN/);
    expect(r.stderr).toContain(LAZY_BUDGET_KEY);
  });

  it("exits NON-ZERO when the manifest declares no entry", () => {
    const r = runGate({
      budgetJson: budgetFile(10000000),
      manifest: { "_x.js": { file: "assets/index-abc.js" } },
    });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
  });

  it("exits NON-ZERO when the manifest points at a missing asset", () => {
    const r = runGate({ budgetJson: budgetFile(10000000), assets: {} });
    expect(r.status).not.toBe(0);
  });

  // C5 at the PROCESS level: a build with no lazy chunks at all must not report
  // "no violators" and exit 0.
  it("exits NON-ZERO when the build has no lazy chunks at all", () => {
    const r = runGate({
      budgetJson: budgetFile(10000000),
      manifest: MANIFEST_SINGLE,
      assets: { "index-abc.js": ENTRY_BYTES },
    });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
    expect(r.stderr).toMatch(/no lazy chunks found/);
  });

  it("exits NON-ZERO when there is no manifest at all", () => {
    const dir = mkdtempSync(join(tmpdir(), "bundle-gate-nom-"));
    const dist = join(dir, "dist");
    mkdirSync(dist, { recursive: true });
    const budgetPath = join(dir, "b.json");
    writeFileSync(budgetPath, budgetFile(10000000));
    const r = spawnSync(process.execPath, [scriptPath, "--dist", dist, "--budget", budgetPath], {
      encoding: "utf8",
    });
    expect(r.status).not.toBe(0);
  });

  it("exits NON-ZERO for an implausibly small graph rather than reporting headroom", () => {
    const r = runGate({
      budgetJson: budgetFile(66000),
      // The lazy asset is present so the ONLY thing that can fail this is the
      // plausibility floor, not a missing file.
      assets: { "index-abc.js": Buffer.alloc(0), "Nudge-lazy.js": LAZY_BYTES },
    });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
    expect(r.stderr).toMatch(/plausibility floor/);

    // AND THE RECORDED NUMBERS SURVIVE A THROWING VERDICT. `evaluate` throws
    // here, and with the print loop below the verdict calls (where it used to
    // be) stdout was EMPTY on exactly the run whose numbers a reader most
    // needs. "Recorded on the fail path" has to mean the THROW path too.
    //
    // TURNS RED IF: the `renderRecordedReport` print loop in
    // check-bundle-size.mjs is moved back below the `evaluate` call.
    expect(r.stdout).toMatch(/RECORDED, NOT GATED — lazy gzip SUM/);
    expect(r.stdout).toMatch(/manifest records skipped as non-\.js:/);
  });

  /**
   * The other throw, on the LAZY side: a build with no lazy chunks at all makes
   * `evaluateLazyChunks` throw, and `renderRecordedReport` calls it too. Without
   * the empty guard inside that function, moving the print loop up would have
   * swallowed the whole report here instead of printing it.
   *
   * TURNS RED IF: the `if (sorted.length > 0)` guard around the largest-chunk
   * push in `renderRecordedReport` is removed (the report then throws before
   * any line is returned), or if the print loop moves back below the verdicts.
   */
  it("still records the numbers when the LAZY verdict itself throws", () => {
    const r = runGate({
      budgetJson: budgetFile(10000000),
      manifest: MANIFEST_SINGLE,
      assets: { "index-abc.js": ENTRY_BYTES },
    });
    expect(r.status).not.toBe(0);
    expect(r.stderr).toMatch(/no lazy chunks found/);
    expect(r.stdout).toContain("lazy chunks: 0, ceiling");
    expect(r.stdout).toMatch(/RECORDED, NOT GATED — lazy gzip SUM 0 B over 0 chunks/);
    // PARTNER: with no lazy chunks there IS no largest, and the report must not
    // invent one — nor die trying to compute it.
    expect(r.stdout).not.toContain("largest lazy chunk");
  });

  /**
   * THE SECOND THROW, which the first version of the reorder did not cover.
   *
   * `evaluateLazyChunks` throws twice: on an empty collection (the test above)
   * and on a zero-byte chunk. `renderRecordedReport` used to CALL it to build
   * the largest-chunk line, guarded by `sorted.length > 0` — a guard that sees
   * only the first throw. So a truncated build exited 1 with ZERO bytes of
   * stdout: the reorder made to stop the report being swallowed had re-created
   * the swallow one throw over, while three files claimed otherwise. The report
   * now reads `sorted[0]` directly and computes no verdict at all.
   *
   * TURNS RED IF: renderRecordedReport goes back to calling evaluateLazyChunks
   * (verified — restoring that call makes this exit 1 with an empty stdout).
   */
  it("still records the numbers when a lazy chunk is truncated to zero bytes", () => {
    const r = runGate({
      budgetJson: budgetFile(10000000),
      manifest: MANIFEST_LAZY,
      assets: { "index-abc.js": ENTRY_BYTES, "Nudge-lazy.js": Buffer.alloc(0) },
    });
    // The zero-byte floor still bites — that is not what this test relaxes.
    expect(r.status).not.toBe(0);
    expect(r.stderr).toMatch(/no bytes at all/);
    // ...and the numbers a reader needs on exactly this run are still printed.
    expect(r.stdout).toContain("lazy chunks: 1, ceiling");
    expect(r.stdout).toMatch(/RECORDED, NOT GATED — lazy gzip SUM \d+ B over 1 chunks/);
    expect(r.stdout).toContain("largest lazy chunk assets/Nudge-lazy.js");
  });

  // Uses a REAL symlink to a REAL file, so the only thing that can fail it is
  // the containment guard. An earlier version of this test pointed at a
  // nonexistent path and died on ENOENT — it stayed green with the guard
  // deleted, which mutation testing caught.
  it("refuses a symlinked asset that escapes dist, even though it reads fine", () => {
    const { dir, dist, budgetPath } = fixture({
      budgetJson: budgetFile(10000000),
      manifest: MANIFEST_SINGLE,
      assets: {},
    });
    const outsider = join(dir, "outside-big.js");
    writeFileSync(outsider, VENDOR_BYTES);
    expect(readFileSync(outsider).length).toBe(VENDOR_BYTES.length); // really readable
    symlinkSync(outsider, join(dist, "assets", "index-abc.js"));

    const r = spawnSync(process.execPath, [scriptPath, "--dist", dist, "--budget", budgetPath], {
      encoding: "utf8",
    });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
    expect(r.stderr).toMatch(/outside dist/);
  });

  /**
   * THE SAME CONTAINMENT, ON THE LAZY READ PATH (#372 review finding).
   *
   * The test above escapes on the EAGER chunk. Containment holds for the lazy
   * list today only because ONE shared `readAsset` closure serves both
   * measurements — which no test could see. Split those two reads in a future
   * refactor and the eager test stays green while the lazy path reads whatever
   * a symlink points at. So: a real symlink on the LAZY chunk, with the eager
   * asset a real file, so the only thing that can fail this is containment on
   * the second list.
   *
   * TURNS RED IF: `resolveAssetPath` is dropped from the `readAsset` used for
   * `lazyMeasurement` in check-bundle-size.mjs (e.g. a second, unguarded reader
   * introduced for the lazy list).
   */
  it("refuses a symlinked LAZY chunk that escapes dist, not just an eager one", () => {
    const { dir, dist, budgetPath } = fixture({
      budgetJson: budgetFile(10000000),
      manifest: MANIFEST_LAZY,
      assets: { "index-abc.js": ENTRY_BYTES },
    });
    const outsider = join(dir, "outside-lazy.js");
    writeFileSync(outsider, LAZY_BYTES);
    expect(readFileSync(outsider).length).toBe(LAZY_BYTES.length); // really readable
    symlinkSync(outsider, join(dist, "assets", "Nudge-lazy.js"));

    const r = spawnSync(process.execPath, [scriptPath, "--dist", dist, "--budget", budgetPath], {
      encoding: "utf8",
    });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
    expect(r.stderr).toMatch(/outside dist/);
    // PARTNER: name the LAZY file, so this cannot be passing on the eager
    // chunk's containment check (which the test above already covers).
    expect(r.stderr).toMatch(/Nudge-lazy\.js/);
  });

  it("works when --dist is given with a trailing separator", () => {
    const { dist, budgetPath } = fixture({
      budgetJson: budgetFile(ENTRY_GZ + 10),
      manifest: MANIFEST_LAZY,
      assets: { "index-abc.js": ENTRY_BYTES, "Nudge-lazy.js": LAZY_BYTES },
    });
    const r = spawnSync(
      process.execPath,
      [scriptPath, "--dist", `${dist}${sep}`, "--budget", budgetPath],
      { encoding: "utf8" },
    );
    expect(r.status).toBe(0);
  });

  // The manualChunks / modulePreload:false blind spot, end to end: each chunk
  // fits on its own and only the SUM exceeds the ceiling, so this can only pass
  // if the vendor chunk is genuinely counted.
  it("counts a statically-imported chunk, so a split graph can exceed the budget", () => {
    const assets = {
      "index-abc.js": ENTRY_BYTES,
      "vendor-xyz.js": VENDOR_BYTES,
      "AuthModal-lazy.js": LAZY_BYTES,
    };
    const budget = Math.max(ENTRY_GZ, VENDOR_GZ) + 100;
    expect(budget).toBeLessThan(ENTRY_GZ + VENDOR_GZ); // the sum is what fails
    const over = runGate({
      budgetJson: budgetFile(budget),
      manifest: MANIFEST_SPLIT,
      assets,
    });
    expect(over.status).toBe(1);
    expect(over.stderr).toContain(`${ENTRY_GZ + VENDOR_GZ} B gzip total`);

    const under = runGate({
      budgetJson: budgetFile(ENTRY_GZ + VENDOR_GZ),
      manifest: MANIFEST_SPLIT,
      assets,
    });
    expect(under.status).toBe(0);
  });

  it("does not count the lazy chunk the manifest lists under dynamicImports", () => {
    // The 400 kB lazy asset is the point: if it were in the EAGER number the
    // eager budget below would blow. It therefore needs a lazy ceiling of its
    // own that it clears, or this test would go red for the #372 gate rather
    // than for the thing it encodes — that lazy bytes are excluded from the
    // EAGER number.
    const LAZY_HUGE = incompressible(400000, 3);
    const r = runGate({
      budgetJson: budgetFile(ENTRY_GZ + VENDOR_GZ + 100, gzipSync(LAZY_HUGE).length + 1),
      manifest: MANIFEST_SPLIT,
      assets: {
        "index-abc.js": ENTRY_BYTES,
        "vendor-xyz.js": VENDOR_BYTES,
        "AuthModal-lazy.js": LAZY_HUGE,
      },
    });
    expect(r.status).toBe(0);
    // The EAGER line must not name it...
    const eagerLine = r.stdout.split("\n").find((l) => l.startsWith("bundle budget OK"));
    expect(eagerLine).toBeDefined();
    expect(eagerLine).not.toContain("AuthModal-lazy.js");
    // ...and the PARTNER: the RECORDED table must, or "not in the eager line"
    // would also be satisfied by the gate never seeing the chunk at all.
    expect(r.stdout).toContain("assets/AuthModal-lazy.js —");
  });

  // #372, the enforced half, end to end. The eager graph is comfortably under
  // its ceiling, so the ONLY thing that can fail this is the per-chunk lazy key.
  it("exits 1 when the eager graph fits but a LAZY chunk is over its ceiling", () => {
    const assets = {
      "index-abc.js": ENTRY_BYTES,
      "vendor-xyz.js": VENDOR_BYTES,
      "AuthModal-lazy.js": LAZY_BYTES,
    };
    const over = runGate({
      budgetJson: budgetFile(ENTRY_GZ + VENDOR_GZ + 1000, LAZY_GZ - 1),
      manifest: MANIFEST_SPLIT,
      assets,
    });
    expect(over.status).toBe(1);
    expect(over.stderr).toMatch(/LAZY CHUNK BUDGET EXCEEDED/);
    expect(over.stderr).toContain("assets/AuthModal-lazy.js");
    expect(over.stderr).toContain(`over the ${LAZY_GZ - 1} B ceiling by 1 B`);
    // The eager gate must NOT have fired — otherwise this passes for the wrong
    // reason and would still pass with the lazy gate deleted.
    expect(over.stderr).not.toMatch(/BUNDLE BUDGET EXCEEDED/);

    // The reverse: both under, exit 0. Without this the assertion above is
    // satisfied by a gate that fails on every input.
    const under = runGate({
      budgetJson: budgetFile(ENTRY_GZ + VENDOR_GZ + 1000, LAZY_GZ),
      manifest: MANIFEST_SPLIT,
      assets,
    });
    expect(under.status).toBe(0);
    expect(under.stdout).toMatch(/lazy chunk budget OK/);
  });

  it("names EVERY over-ceiling lazy chunk in the failure, not the first", () => {
    const FAT_A = incompressible(9000, 21);
    const FAT_B = incompressible(9000, 22);
    const r = runGate({
      budgetJson: budgetFile(ENTRY_GZ + 1000, LAZY_GZ),
      manifest: {
        "index.html": {
          file: "assets/index-abc.js",
          isEntry: true,
          dynamicImports: ["a.tsx", "b.tsx", "c.tsx"],
        },
        "a.tsx": { file: "assets/a-lazy.js" },
        "b.tsx": { file: "assets/b-lazy.js" },
        "c.tsx": { file: "assets/c-lazy.js" },
      },
      assets: {
        "index-abc.js": ENTRY_BYTES,
        "a-lazy.js": FAT_A,
        "b-lazy.js": LAZY_BYTES, // compliant, and it sits BETWEEN the two violators
        "c-lazy.js": FAT_B,
      },
    });
    expect(r.status).toBe(1);
    expect(r.stderr).toContain("assets/a-lazy.js");
    expect(r.stderr).toContain("assets/c-lazy.js");
    // The compliant chunk must NOT be listed as a violator.
    const violatorBlock = r.stderr.split("LAZY CHUNK BUDGET EXCEEDED")[1].split("\n\n")[0];
    expect(violatorBlock).not.toContain("assets/b-lazy.js");
  });

  // The RECORDED half of #372, proved through the REAL process: the totals
  // appear on the pass path AND on the fail path, with the real numbers.
  it.each([
    ["the pass path", 0, ENTRY_GZ + 1000],
    ["the fail path", 1, ENTRY_GZ - 1],
  ])("records the lazy SUM, the all-JS TOTAL and the skip count on %s", (_label, status, eagerMax) => {
    const r = runGate({ budgetJson: budgetFile(eagerMax) });
    expect(r.status).toBe(status);
    expect(r.stdout).toContain("lazy chunks: 1, ceiling 1000000 B each");
    expect(r.stdout).toContain(`assets/Nudge-lazy.js — ${LAZY_GZ} B gzip`);
    expect(r.stdout).toContain(`RECORDED, NOT GATED — lazy gzip SUM ${LAZY_GZ} B over 1 chunks`);
    expect(r.stdout).toContain(
      `RECORDED, NOT GATED — all-JS gzip TOTAL (manifest chunks only) ` +
        `${ENTRY_GZ + LAZY_GZ} B over 2 chunks (eager ${ENTRY_GZ} + lazy ${LAZY_GZ})`,
    );
    expect(r.stdout).toContain("manifest records skipped as non-.js: 0");

    // The largest-lazy-chunk line appears ONCE, not twice. It used to be
    // printed by the recorded report AND repeated verbatim in the pass-path
    // footer, so a reader saw the same sentence twice and could not tell which
    // one was the verdict.
    const largestLines = r.stdout.split("\n").filter((l) => l.includes("largest lazy chunk"));
    expect(largestLines).toHaveLength(1);
  });

  // The SHIPPING budget file, through the real process and both keys. Nothing
  // else here reads it end to end: every other fixture writes its own.
  it("drives the gate with the bundle-budget.json that actually ships", () => {
    const { dist } = fixture({
      budgetJson: "{}", // unused — the real file is passed below
      manifest: MANIFEST_LAZY,
      assets: { "index-abc.js": ENTRY_BYTES, "Nudge-lazy.js": LAZY_BYTES },
    });
    const shipped = JSON.parse(readFileSync(join(frontendRoot, "bundle-budget.json"), "utf8"));
    // The fixture must genuinely fit under BOTH shipped ceilings, or a pass
    // here would say nothing about them.
    expect(ENTRY_GZ).toBeLessThan(shipped[BUDGET_KEY]);
    expect(LAZY_GZ).toBeLessThan(shipped[LAZY_BUDGET_KEY]);
    const r = spawnSync(
      process.execPath,
      [scriptPath, "--dist", dist, "--budget", join(frontendRoot, "bundle-budget.json")],
      { encoding: "utf8" },
    );
    expect(r.status).toBe(0);
    expect(r.stdout).toMatch(/bundle budget OK/);
    expect(r.stdout).toMatch(/lazy chunk budget OK/);
    expect(`${r.stdout}${r.stderr}`).not.toMatch(/NaN/);
  });
});

/**
 * Guard the guard's INVOCATION.
 *
 * This block PARSES the workflow instead of matching its text. Two earlier
 * versions did match text and both were defeated in review:
 *   - a substring grep for "npm run check:bundle" stayed green with the step
 *     commented out entirely;
 *   - an anchored whole-line match plus a 3-line window above the `run:` was
 *     defeated three ways — `if:` written AFTER `run:` (YAML mapping key order
 *     is not semantic, so Actions honours it identically), `if:` written above
 *     `run:` but pushed out of the window by comment lines, and a JOB-level
 *     `if:` that skips the gate along with everything else in the job.
 *
 * A YAML document's meaning is its parse, not its layout, so the only honest
 * way to assert what GitHub Actions will do is to read the same structure
 * Actions reads. That is what js-yaml is a devDependency for.
 */
describe("the CI invocation itself", () => {
  const workflowPath = join(frontendRoot, "..", ".github", "workflows", "frontend.yml");
  const workflow = () => load(readFileSync(workflowPath, "utf8"));
  const buildJob = () => workflow().jobs.build;
  const gateSteps = () =>
    (buildJob().steps ?? []).filter((s) => typeof s.run === "string" && s.run.trim() === "npm run check:bundle");

  it("runs the script with no arguments, so CI always builds the shipping variant", () => {
    const pkg = JSON.parse(readFileSync(join(frontendRoot, "package.json"), "utf8"));
    expect(pkg.scripts["check:bundle"]).toBe("node scripts/check-bundle-size.mjs");
  });

  it("invokes the gate exactly once in the build job, with no extra arguments", () => {
    // `run` compared whole: `npm run check:bundle --dist x` or a trailing
    // `|| true` is a different string and fails here.
    expect(gateSteps()).toHaveLength(1);
  });

  it("does not run the gate conditionally, at step OR job level", () => {
    expect(gateSteps()[0].if).toBeUndefined();
    // A job-level `if:` skips the gate along with the whole job — the bypass
    // that the previous window-based assertion could not see at all.
    expect(buildJob().if).toBeUndefined();
  });

  it("lets a failing gate actually fail the job, at step OR job level", () => {
    expect(gateSteps()[0]["continue-on-error"]).toBeUndefined();
    expect(buildJob()["continue-on-error"]).toBeUndefined();
  });

  /**
   * The job NAME is the required status check's context string on `main`.
   * Rename the job and the required context never reports again, which hangs
   * every PR at "Expected — waiting for status to be reported" (#326).
   */
  it("keeps the job name that main's branch protection requires", () => {
    expect(buildJob().name).toBe("type-check + unit tests + build");
    expect(workflow().jobs["demo-e2e"].name).toBe("Demo-mode Playwright (no visual snapshots)");
  });

  it("keeps the workflow unfiltered, so a required check always reports (#326)", () => {
    // A path-filtered workflow that does not trigger reports NOTHING, and a
    // required context then hangs forever. Checked on the parsed triggers, so
    // a `paths:` at any indentation under either trigger is caught.
    const on = workflow().on;
    expect(on.pull_request?.paths).toBeUndefined();
    expect(on.pull_request?.["paths-ignore"]).toBeUndefined();
    expect(on.push?.paths).toBeUndefined();
    expect(on.push?.["paths-ignore"]).toBeUndefined();
  });
});
