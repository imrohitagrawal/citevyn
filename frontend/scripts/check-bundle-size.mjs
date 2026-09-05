/**
 * Fails the build if the EAGER GRAPH exceeds the gzip budget.
 *
 * Three things this guards that a human eye did not:
 *   1. The ceiling is a NUMBER IN A FILE, not prose in a docs row. It was raised
 *      twice in two PRs by the same person who recorded it, with nothing checking.
 *   2. It refuses to measure the WRONG ARTIFACT. The bundle must be built with
 *      VITE_API_LIVE=true (what the Dockerfile sets); a build without it is ~860 B
 *      larger and is not what ships. Rather than trust the caller to have set it,
 *      this builds the shipping variant itself.
 *   3. It measures the eager module graph from Vite's own build manifest, not a
 *      filename pattern and not preload hints. See bundle-budget.mjs.
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
  resolveAssetPath,
  measureEagerGraph,
  evaluate,
  EXCEEDED_ADVICE,
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

const max = parseBudget(readFileSync(budgetPath, "utf8"));
const manifest = JSON.parse(readFileSync(join(distDir, ".vite", "manifest.json"), "utf8"));
const files = eagerChunkFilesFromManifest(manifest);

const measurement = measureEagerGraph({
  files,
  readAsset: (file) =>
    readFileSync(resolveAssetPath({ distDir, file, realpath: realpathSync })),
  gzipSize: (bytes) => gzipSync(bytes).length,
});

const { ok, line } = evaluate({ measurement, max });

if (!ok) {
  console.error(`BUNDLE BUDGET EXCEEDED\n  ${line}\n${EXCEEDED_ADVICE}`);
  process.exit(1);
}
console.log(`bundle budget OK — ${line}`);
