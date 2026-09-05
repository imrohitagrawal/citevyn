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
 *   3. It measures what the BROWSER FETCHES before first paint, read out of the
 *      built index.html — not a filename pattern. See bundle-budget.mjs.
 *
 * All decision logic lives in ./bundle-budget.mjs so it is testable without a
 * 7-second build; see bundle-budget.test.mjs, which drives this file too.
 */
import { gzipSync } from "node:zlib";
import { readFileSync } from "node:fs";
import { join, dirname, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import {
  parseBudget,
  eagerScriptUrlsFromHtml,
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
 * uses. A test in bundle-budget.test.mjs pins that package.json script string
 * exactly, so wiring a flag into CI cannot happen quietly.
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
  // `--config vite.config.ts` is load-bearing: `tsc -b` emits a COMPILED
  // `vite.config.js` next to the source (both gitignored), and Vite resolves
  // `.js` before `.ts`. Running this script standalone would otherwise build
  // against whatever stale compiled config happened to be lying around — which
  // cost real time to diagnose while fixing #323, because an edit to
  // vite.config.ts had no effect on the output at all.
  execFileSync("npx", ["vite", "build", "--mode", "production", "--config", "vite.config.ts"], {
    cwd: root,
    stdio: "inherit",
    env: { ...process.env, VITE_API_LIVE: "true" },
  });
}

const distDir = distOverride === null ? join(root, "dist") : distOverride;
const budgetPath = budgetOverride === null ? join(root, "bundle-budget.json") : budgetOverride;

const max = parseBudget(readFileSync(budgetPath, "utf8"));
const html = readFileSync(join(distDir, "index.html"), "utf8");
const urls = eagerScriptUrlsFromHtml(html);

const measurement = measureEagerGraph({
  urls,
  readAsset: (url) => {
    // index.html carries absolute, root-relative URLs ("/assets/x.js"). Resolve
    // them under distDir and refuse anything that escapes it, so a crafted or
    // malformed index.html cannot make the gate read (and "measure") a file
    // outside the build output.
    const rel = url.replace(/^\/+/, "");
    const resolved = normalize(join(distDir, rel));
    if (resolved !== normalize(distDir) && !resolved.startsWith(normalize(distDir) + sep)) {
      throw new Error(`index.html references an asset outside dist/: ${url}`);
    }
    return readFileSync(resolved);
  },
  gzipSize: (bytes) => gzipSync(bytes).length,
});

const { ok, line } = evaluate({ measurement, max });

if (!ok) {
  console.error(`BUNDLE BUDGET EXCEEDED\n  ${line}\n${EXCEEDED_ADVICE}`);
  process.exit(1);
}
console.log(`bundle budget OK — ${line}`);
