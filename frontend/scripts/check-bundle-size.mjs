/**
 * Fails the build if the EAGER GRAPH exceeds its gzip budget, or if ANY SINGLE
 * LAZY CHUNK exceeds its own per-chunk gzip budget (#372). It also RECORDS, on
 * every run and on both the pass and the fail path, three numbers that nothing
 * gates: the per-chunk lazy table, the lazy SUM, and the all-JS TOTAL.
 *
 * That reporting IS the fix for #372. The complaint there was not a missing
 * ceiling — it was that this command reported a 3,521 B eager WIN for #358
 * while desktop first-paint JS grew 1,200 B, and no output said so. The
 * countervailing totals are now PRODUCED by the same command, on both verdict
 * paths. They are not, and this is the honest limit of it, IN THE DIFF: they go
 * to a CI log that nothing asserts, so a PR body quoting an eager-only win is
 * as available as it ever was. Closing that would take a recorded-measurements
 * file this gate rewrites — a separate work package. A total-JS ceiling was
 * considered and rejected; the four reasons are in
 * frontend/bundle-budget.json's `_comment`.
 *
 * Four things this guards that a human eye did not:
 *   1. The ceiling is a NUMBER IN A FILE, not prose in a docs row. It was raised
 *      twice in two PRs by the same person who recorded it, with nothing checking.
 *   2. It refuses to measure the WRONG ARTIFACT. The bundle must be built with
 *      VITE_API_LIVE=true (what the Dockerfile sets); a build without it is ~860 B
 *      larger and is not what ships. Rather than trust the caller to have set it,
 *      this builds the shipping variant itself.
 *   3. It measures the eager module graph from Vite's own build manifest, not a
 *      filename pattern and not preload hints. See bundle-budget.mjs.
 *   4. The lazy set is a SET DIFFERENCE against that eager closure, never a walk
 *      out from `dynamicImports` — the real manifest's shared chunks declare a
 *      back-edge to the entry, and a walk mis-files the entry chunk as lazy.
 *
 * All decision logic lives in ./bundle-budget.mjs so it is testable without a
 * 7-second build; see bundle-budget.test.mjs, which drives this file too.
 */
import { gzipSync } from "node:zlib";
import { readFileSync, realpathSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import {
  parseBudget,
  buildCommand,
  eagerChunkFilesFromManifest,
  nonEagerChunkFilesFromManifest,
  resolveAssetPath,
  measureEagerGraph,
  evaluate,
  evaluateLazyChunks,
  renderRecordedReport,
  EXCEEDED_ADVICE,
  LAZY_BUDGET_KEY,
  LAZY_EXCEEDED_ADVICE,
} from "./bundle-budget.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

/**
 * `--dist <dir>` / `--budget <file>` exist so the test suite can drive this
 * script end-to-end (real process, real exit code) against fixtures without
 * paying for a vite build. They are NOT for CI: with no arguments this builds
 * the shipping variant itself, which is the only mode `npm run check:bundle`
 * uses. bundle-budget.test.mjs pins package.json's script string AND the
 * workflow step (anchored, whole-line, and asserted not to be
 * `continue-on-error`), so neither seam can be wired into CI quietly.
 */
function parseArgs(argv) {
  const opts = { dist: null, budget: null };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--dist") opts.dist = argv[i + 1] ?? null;
    else if (argv[i] === "--budget") opts.budget = argv[i + 1] ?? null;
  }
  return opts;
}

const { dist: distOverride, budget: budgetOverride } = parseArgs(process.argv.slice(2));

if (distOverride === null) {
  // Build the SHIPPING variant here rather than trusting whatever is in dist/.
  //
  // The first version of this script tried to DETECT the mode from the built
  // output. That guard silently never fired: Vite inlines
  // `import.meta.env.VITE_API_LIVE` and then constant-folds the comparison away
  // entirely, so neither "true" nor "false" survives in the bundle to look for.
  // It only looked like it worked because the demo build is ~860 B larger and
  // tripped the size check instead — a guard passing for the wrong reason.
  //
  // The argv and env come from buildCommand() so a test can pin them; every
  // flag in there is load-bearing and is explained at its definition.
  const { cmd, args, env } = buildCommand();
  execFileSync(cmd, args, { cwd: root, stdio: "inherit", env: { ...process.env, ...env } });
}

const distDir = distOverride === null ? join(root, "dist") : distOverride;
const budgetPath = budgetOverride === null ? join(root, "bundle-budget.json") : budgetOverride;

const budgetText = readFileSync(budgetPath, "utf8");
const max = parseBudget(budgetText);
// BOTH ceilings go through the SAME validated path, so a missing, mistyped or
// renamed lazy key is a hard failure too rather than an `undefined` that every
// comparison quietly passes (#323, reproduced one function over in #372).
const lazyMax = parseBudget(budgetText, LAZY_BUDGET_KEY);

const manifest = JSON.parse(readFileSync(join(distDir, ".vite", "manifest.json"), "utf8"));
const files = eagerChunkFilesFromManifest(manifest);
const { files: lazyFiles, skippedNonJs } = nonEagerChunkFilesFromManifest(manifest);

const readAsset = (file) =>
  readFileSync(resolveAssetPath({ distDir, file, realpath: realpathSync }));
const gzipSize = (bytes) => gzipSync(bytes).length;

const measurement = measureEagerGraph({ files, readAsset, gzipSize });
// Same gzip-each-and-sum over a different file list. Reused rather than
// duplicated so one arithmetic bug cannot be fixed in one place and not the
// other; the sum it computes is RECORDED, never gated.
const lazyMeasurement = measureEagerGraph({ files: lazyFiles, readAsset, gzipSize });

// Printed BEFORE either verdict is COMPUTED, not merely before it is acted on.
// #372's actual complaint is that a win was reported without these numbers, and
// both `evaluate` (the plausibility floor) and `evaluateLazyChunks` (the empty
// collection, the zero-byte chunk) THROW — so with the verdicts first, the run
// that most needs these numbers printed nothing at all.
//
// It is still not literally "every run": a failure EARLIER than the
// measurement — an unreadable asset, a manifest with no entry, a symlink that
// escapes dist/ — throws above this line and prints nothing. That is stated
// rather than claimed away.
for (const reportLine of renderRecordedReport({
  eager: measurement,
  lazy: lazyMeasurement,
  max: lazyMax,
  skippedNonJs,
})) {
  console.log(reportLine);
}

const { ok, line } = evaluate({ measurement, max });
const lazy = evaluateLazyChunks({ chunks: lazyMeasurement.files, max: lazyMax });

if (!ok) {
  console.error(`BUNDLE BUDGET EXCEEDED\n  ${line}\n${EXCEEDED_ADVICE}`);
}
if (!lazy.ok) {
  const named = lazy.violators
    .map((c) => `  ${c.file} — ${c.gzip} B gzip, over the ${lazyMax} B ceiling by ${c.gzip - lazyMax} B`)
    .join("\n");
  console.error(`LAZY CHUNK BUDGET EXCEEDED\n${named}\n${LAZY_EXCEEDED_ADVICE}`);
}
if (!ok || !lazy.ok) {
  process.exit(1);
}
console.log(`bundle budget OK — ${line}`);
// The verdict, NOT a second copy of `lazy.line` — the recorded report above
// already printed that line, and it used to appear twice on the pass path.
console.log(
  `lazy chunk budget OK — all ${lazyMeasurement.files.length} lazy chunks are within ` +
    `the ${lazyMax} B per-chunk ceiling`,
);
