/**
 * #358 — proves the below-the-fold marketing strip really left the eager
 * bundle, by reading the BUILD OUTPUT rather than the source.
 *
 * Why not assert on the source. `lazy(() => import("./landing-strip"))` being
 * present in LandingPage.tsx proves nothing about what the browser downloads:
 * rollup chunks per MODULE, so a stray eager `import { Footer } from
 * "./landing-strip"` anywhere else pulls the whole module back into the entry
 * chunk while every `lazy(` call site still reads exactly as it does today. The
 * only honest witness is the emitted graph, which is what this file inspects —
 * the same rule src/test/emittedArtifact.test.ts follows for the #365 guard.
 *
 * Two independent witnesses, because either alone can pass for a wrong reason:
 *
 *   1. STRUCTURE — Vite's own manifest must file landing-strip.tsx under the
 *      entry's `dynamicImports` and NOT anywhere in the transitive closure of
 *      its static `imports`. That is exactly the split scripts/bundle-budget.mjs
 *      measures, so this pins the thing the budget gate is counting.
 *
 *   2. CONTENT — the eager chunk's actual bytes must not contain the deferred
 *      sections' copy, and the lazy chunk's bytes must. A structural check
 *      alone would still pass if the module were duplicated into both chunks.
 *
 * And a PARTNER for every absence check (an "is not present" assertion that
 * counts nothing passes trivially once the string is renamed): each string
 * asserted absent from the eager chunk is asserted PRESENT in the lazy chunk,
 * and vice versa. If a marketing string is reworded, this file goes red rather
 * than quietly measuring nothing.
 *
 * BUILDS INTO ITS OWN OUTPUT DIRECTORY. src/test/emittedArtifact.test.ts runs
 * `npm run build` into frontend/dist, and vitest runs test files in parallel
 * (nothing in vite.config.ts sets fileParallelism:false), so sharing dist/
 * would be a race — one file's build deleting the other's output mid-read.
 * Reproduced while writing this: after a full `npm test` run, dist/ held a
 * manifest-less build. Hence mkdtemp + --outDir.
 */
import { execFileSync } from "node:child_process";
import { gzipSync } from "node:zlib";
import { mkdtempSync, readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  LAZY_BUDGET_KEY,
  buildCommand,
  eagerChunkFilesFromManifest,
  nonEagerChunkFilesFromManifest,
  parseBudget,
} from "./bundle-budget.mjs";

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

/** The module that must be lazy, as Vite names it in the manifest. */
const STRIP_MODULE = "src/components/landing-strip.tsx";

/**
 * Copy that lives in the DEFERRED strip, and copy that must stay EAGER.
 * Short, distinctive literals that minification cannot rewrite (they are string
 * contents, not identifiers). Kept deliberately few: each one is a maintenance
 * cost when the marketing copy changes, and three is enough to distinguish a
 * real split from a duplicated module.
 */
const DEFERRED_COPY = [
  "Questions, answered.", // FAQ  <h2>
  "Answers from official docs only.", // Footer
  "01 · JUST CURIOUS", // Personas card tag
];
const EAGER_COPY = [
  "sources-strip-inner", // SourcesStrip — sits under the Hero, must not move
  "demo-q-btn", // InteractiveDemo — deliberately kept eager (#358)
];

let outDir = "";
let manifest;
let eagerText = "";
let eagerBytes = 0;
let stripFile = "";
let stripGzip = 0;

beforeAll(() => {
  outDir = mkdtempSync(join(tmpdir(), "citevyn-strip-guard-"));
  const { cmd, args, env } = buildCommand();
  // --emptyOutDir because the directory is outside the project root, where Vite
  // otherwise refuses to clean and prints a warning instead of building.
  execFileSync(cmd, [...args, "--outDir", outDir, "--emptyOutDir"], {
    cwd: frontendRoot,
    stdio: "pipe",
    env: { ...process.env, ...env },
  });

  manifest = JSON.parse(readFileSync(join(outDir, ".vite", "manifest.json"), "utf8"));

  const eagerFiles = eagerChunkFilesFromManifest(manifest);
  const eagerBuffers = eagerFiles.map((f) => readFileSync(join(outDir, f)));
  eagerText = eagerBuffers.map((b) => b.toString("utf8")).join("\n");
  eagerBytes = eagerBuffers.reduce((n, b) => n + gzipSync(b).length, 0);

  stripFile = manifest[STRIP_MODULE]?.file ?? "";
  // Fail HERE, naming the cause, rather than letting every test that reads the
  // chunk hit `readFileSync(join(outDir, ""))` — which reads a DIRECTORY and
  // reports "EISDIR: illegal operation on a directory", pointing at the
  // filesystem instead of at the regression. Found in review by mutating the
  // split away and reading what the failures actually said.
  if (!stripFile) {
    throw new Error(
      `${STRIP_MODULE} is no longer a chunk of its own — the eager/lazy split has been undone. ` +
        `Manifest keys: ${Object.keys(manifest).join(", ")}`,
    );
  }
  stripGzip = gzipSync(readFileSync(join(outDir, stripFile))).length;
}, 180_000);

afterAll(() => {
  if (outDir) rmSync(outDir, { recursive: true, force: true });
});

describe("the below-the-fold landing strip is not in the eager bundle (#358)", () => {
  it("is a chunk of its own, reached only through a dynamic import", () => {
    // Fails if landing-strip.tsx is inlined back into the entry (no manifest
    // record of its own) — i.e. if the physical split is undone.
    expect(manifest[STRIP_MODULE], `manifest has no record for ${STRIP_MODULE}`).toBeDefined();
    expect(stripFile).toMatch(/^assets\/landing-strip-[^/]+\.js$/);
    expect(existsSync(join(outDir, stripFile))).toBe(true);

    const entryKeys = Object.keys(manifest).filter((k) => manifest[k].isEntry === true);
    expect(entryKeys).toHaveLength(1);
    expect(manifest[entryKeys[0]].dynamicImports ?? []).toContain(STRIP_MODULE);
  });

  it("is absent from the eager graph the bundle gate measures", () => {
    // The exact set scripts/bundle-budget.mjs counts: entry + transitive static
    // imports, dynamicImports NOT followed. If someone adds a plain
    // `import { Footer } from "./landing-strip"` anywhere in the eager half,
    // rollup folds the module in and this goes red.
    const eagerFiles = eagerChunkFilesFromManifest(manifest);
    expect(eagerFiles).not.toContain(stripFile);

    // PARTNER for that absence: the eager graph is not empty, and the file the
    // assertion above says is missing genuinely exists and is worth deferring.
    expect(eagerFiles.length).toBeGreaterThan(0);
    expect(eagerBytes).toBeGreaterThan(50_000);
    expect(stripGzip).toBeGreaterThan(3_000);

    // AND AN UPPER BOUND, because the lower one cannot tell WHICH BUILD this is.
    //
    // `--mode production` does not set NODE_ENV, vitest sets it to "test", and
    // React's package exports branch on it — so until buildCommand() pinned
    // NODE_ENV=production this whole file measured a React DEVELOPMENT build
    // and said it was measuring the shipping chunk. `> 50_000` passed happily
    // on both: measured 62,974 B gzip for the shipping entry and 118,897 B for
    // the dev one. A BAND catches that; a floor never could.
    //
    // Deliberately a band, not a pin: 90,000 B is ~43% above today's shipping
    // graph (so ordinary work can never reach it) and ~24% below the dev build
    // (so the environment cannot drift back without this going red).
    //
    // TURNS RED IF: the `NODE_ENV: "production"` pair is dropped from
    // buildCommand()'s `env` — the entry then measures 118,897 B.
    expect(eagerBytes).toBeLessThan(90_000);
  });

  /**
   * #372 — the OTHER side of that inequality, against the REAL build.
   *
   * scripts/bundle-budget.test.mjs proves the per-chunk ceiling works on
   * fixtures. This proves the number in bundle-budget.json is the right one for
   * the chunk that actually ships: the strip is the largest lazy chunk, and it
   * is what `lazyChunkGzipMaxBytes` was derived from (largest + the 1,606 B
   * headroom the 68,000 decision chose, and the 64,479 ratchet preserved at the
   * moment it landed).
   *
   * "THE CHUNK THAT ACTUALLY SHIPS" IS A CLAIM WITH A HISTORY. The first
   * version of this assertion was false: vitest sets NODE_ENV=test, this file
   * builds through buildCommand(), and `--mode production` does not set
   * NODE_ENV — so it measured a React DEVELOPMENT build. The strip was 6,067 B
   * there against a 6,328 B ceiling: a 261 B real margin while `npm run
   * check:bundle` reported 1,606 B in the SAME CI job, and ~262 B of growth
   * would have turned `npm test` red citing a ceiling the shipping build was
   * nowhere near. buildCommand() now pins NODE_ENV=production, and the partner
   * that keeps it pinned is the eager-graph BAND in the test above.
   *
   * IT IS MOSTLY A BOUND, BUT TWO OF ITS THREE ASSERTIONS ARE TRIPWIRES that a
   * SHRINKING strip can turn red, and that is deliberate rather than an
   * oversight — say so rather than claiming "shrinking can never turn it red",
   * which this docblock did:
   *   - `lazyMax < stripGzip * 2` reddens below 3,164 B, because a ceiling more
   *     than double the largest real chunk has stopped bounding anything.
   *   - `Math.max(...sizes) === stripGzip` reddens if the strip drops below the
   *     next-largest lazy chunk (AuthModal, 2,894 B today), because the
   *     derivation in bundle-budget.json's `_comment` would then name the wrong
   *     chunk and needs redoing.
   * Only `stripGzip <= lazyMax` is a pure one-sided bound.
   */
  it("is under the per-chunk lazy ceiling, and is the chunk that ceiling was set from (#372)", () => {
    const lazyMax = parseBudget(
      readFileSync(join(frontendRoot, "bundle-budget.json"), "utf8"),
      LAZY_BUDGET_KEY,
    );
    expect(stripGzip).toBeLessThanOrEqual(lazyMax);

    // PARTNER: the ceiling is not vacuously large. `stripGzip > 3_000` above
    // already proves the chunk is real; this proves the ceiling is within a
    // change or two of it rather than an unreachable number.
    expect(lazyMax).toBeLessThan(stripGzip * 2);

    // And the strip really is the largest lazy chunk, which is the premise the
    // ceiling was derived from. If some other chunk overtakes it, the derivation
    // in bundle-budget.json's `_comment` needs redoing and this says so.
    const { files } = nonEagerChunkFilesFromManifest(manifest);
    expect(files).toContain(stripFile);
    const sizes = files.map((f) => gzipSync(readFileSync(join(outDir, f))).length);
    expect(Math.max(...sizes)).toBe(stripGzip);
  });

  it("keeps the deferred sections' copy out of the bytes the browser downloads first", () => {
    // `.includes(...)` compared as a BOOLEAN, not `.toContain` on the text: a
    // failing toContain prints the whole 190 kB chunk as its diff.
    const stripText = readFileSync(join(outDir, stripFile), "utf8");
    for (const phrase of DEFERRED_COPY) {
      // PARTNER FIRST: prove the phrase still exists in this build at all, so
      // a reworded string cannot turn the absence check below into a no-op.
      expect(stripText.includes(phrase), `"${phrase}" is not in the lazy chunk — has the copy changed?`).toBe(true);
      expect(eagerText.includes(phrase), `"${phrase}" is still in the eager chunk`).toBe(false);
    }
  });

  it("keeps the at-the-fold sections eager, so the split did not overshoot", () => {
    const stripText = readFileSync(join(outDir, stripFile), "utf8");
    for (const phrase of EAGER_COPY) {
      expect(eagerText.includes(phrase), `"${phrase}" left the eager chunk`).toBe(true);
      expect(stripText.includes(phrase), `"${phrase}" was duplicated into the lazy chunk`).toBe(false);
    }
  });
});
