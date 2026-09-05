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
  MIN_PLAUSIBLE_GZIP,
  parseBudget,
  buildCommand,
  eagerChunkFilesFromManifest,
  resolveAssetPath,
  measureEagerGraph,
  evaluate,
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
  const ENTRY_GZ = gzipSync(ENTRY_BYTES).length;
  const VENDOR_GZ = gzipSync(VENDOR_BYTES).length;

  function runGate({
    budgetJson,
    manifest = MANIFEST_SINGLE,
    assets = { "index-abc.js": ENTRY_BYTES },
  }) {
    const { dist, budgetPath } = fixture({ budgetJson, manifest, assets });
    const r = spawnSync(process.execPath, [scriptPath, "--dist", dist, "--budget", budgetPath], {
      encoding: "utf8",
    });
    return { status: r.status, stdout: r.stdout ?? "", stderr: r.stderr ?? "" };
  }

  it("exits 0 for a graph under budget, and prints the REAL measured numbers", () => {
    const r = runGate({ budgetJson: `{"${BUDGET_KEY}": ${ENTRY_GZ + 500}}` });
    expect(r.status).toBe(0);
    expect(r.stdout).toMatch(/bundle budget OK/);
    // Pin the arithmetic the reader relies on: a report line hardcoded to zeros
    // would otherwise pass every other assertion here.
    expect(r.stdout).toContain(`${ENTRY_GZ} B gzip total`);
    expect(r.stdout).toContain(`budget ${ENTRY_GZ + 500} B`);
    expect(r.stdout).toContain("headroom 500 B");
  });

  it("exits 1 for a graph over budget", () => {
    const r = runGate({ budgetJson: `{"${BUDGET_KEY}": ${ENTRY_GZ - 1}}` });
    expect(r.status).toBe(1);
    expect(r.stderr).toMatch(/BUNDLE BUDGET EXCEEDED/);
  });

  // THE #323 REGRESSION. Before the fix this exited 0 and printed
  // "bundle budget OK — ... (budget undefined B, headroom NaN B)".
  it("exits NON-ZERO when the budget key is mistyped", () => {
    const r = runGate({ budgetJson: `{"${BUDGET_KEY}TYPO": 1}` });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
    expect(`${r.stdout}${r.stderr}`).not.toMatch(/NaN/);
  });

  it("exits NON-ZERO when the manifest declares no entry", () => {
    const r = runGate({
      budgetJson: `{"${BUDGET_KEY}": 10000000}`,
      manifest: { "_x.js": { file: "assets/index-abc.js" } },
    });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
  });

  it("exits NON-ZERO when the manifest points at a missing asset", () => {
    const r = runGate({ budgetJson: `{"${BUDGET_KEY}": 10000000}`, assets: {} });
    expect(r.status).not.toBe(0);
  });

  it("exits NON-ZERO when there is no manifest at all", () => {
    const dir = mkdtempSync(join(tmpdir(), "bundle-gate-nom-"));
    const dist = join(dir, "dist");
    mkdirSync(dist, { recursive: true });
    const budgetPath = join(dir, "b.json");
    writeFileSync(budgetPath, `{"${BUDGET_KEY}": 10000000}`);
    const r = spawnSync(process.execPath, [scriptPath, "--dist", dist, "--budget", budgetPath], {
      encoding: "utf8",
    });
    expect(r.status).not.toBe(0);
  });

  it("exits NON-ZERO for an implausibly small graph rather than reporting headroom", () => {
    const r = runGate({
      budgetJson: `{"${BUDGET_KEY}": 66000}`,
      assets: { "index-abc.js": Buffer.alloc(0) },
    });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
  });

  // Uses a REAL symlink to a REAL file, so the only thing that can fail it is
  // the containment guard. An earlier version of this test pointed at a
  // nonexistent path and died on ENOENT — it stayed green with the guard
  // deleted, which mutation testing caught.
  it("refuses a symlinked asset that escapes dist, even though it reads fine", () => {
    const { dir, dist, budgetPath } = fixture({
      budgetJson: `{"${BUDGET_KEY}": 10000000}`,
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

  it("works when --dist is given with a trailing separator", () => {
    const { dist, budgetPath } = fixture({
      budgetJson: `{"${BUDGET_KEY}": ${ENTRY_GZ + 10}}`,
      manifest: MANIFEST_SINGLE,
      assets: { "index-abc.js": ENTRY_BYTES },
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
    const assets = { "index-abc.js": ENTRY_BYTES, "vendor-xyz.js": VENDOR_BYTES };
    const budget = Math.max(ENTRY_GZ, VENDOR_GZ) + 100;
    expect(budget).toBeLessThan(ENTRY_GZ + VENDOR_GZ); // the sum is what fails
    const over = runGate({
      budgetJson: `{"${BUDGET_KEY}": ${budget}}`,
      manifest: MANIFEST_SPLIT,
      assets,
    });
    expect(over.status).toBe(1);
    expect(over.stderr).toContain(`${ENTRY_GZ + VENDOR_GZ} B gzip total`);

    const under = runGate({
      budgetJson: `{"${BUDGET_KEY}": ${ENTRY_GZ + VENDOR_GZ}}`,
      manifest: MANIFEST_SPLIT,
      assets,
    });
    expect(under.status).toBe(0);
  });

  it("does not count the lazy chunk the manifest lists under dynamicImports", () => {
    const r = runGate({
      budgetJson: `{"${BUDGET_KEY}": ${ENTRY_GZ + VENDOR_GZ + 100}}`,
      manifest: MANIFEST_SPLIT,
      assets: {
        "index-abc.js": ENTRY_BYTES,
        "vendor-xyz.js": VENDOR_BYTES,
        // Deliberately enormous: if it were counted, the budget above would blow.
        "AuthModal-lazy.js": incompressible(400000, 3),
      },
    });
    expect(r.status).toBe(0);
    expect(r.stdout).not.toContain("AuthModal-lazy.js");
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
