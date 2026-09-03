/**
 * Fails the build if the eager chunk exceeds the gzip budget.
 *
 * Two things this guards that a human eye did not:
 *   1. The ceiling is a NUMBER IN A FILE, not prose in a docs row. It was raised
 *      twice in two PRs by the same person who recorded it, with nothing checking.
 *   2. It refuses to measure the WRONG ARTIFACT. The bundle must be built with
 *      VITE_API_LIVE=true (what the Dockerfile sets); a build without it is ~860 B
 *      larger and is not what ships. Rather than trust the caller to have set it,
 *      this asserts the built output really is the live variant.
 */
import { gzipSync } from "node:zlib";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

// Build the SHIPPING variant here rather than trusting whatever is in dist/.
//
// The first version of this script tried to DETECT the mode from the built
// output. That guard silently never fired: Vite inlines
// `import.meta.env.VITE_API_LIVE` and then constant-folds the comparison away
// entirely, so neither "true" nor "false" survives in the bundle to look for.
// It only looked like it worked because the demo build is ~860 B larger and
// tripped the size check instead — a guard passing for the wrong reason.
//
// Building it ourselves removes the question: there is no wrong artifact to
// measure. `--mode production` matches `npm run build`; the env var matches
// infra/docker/Dockerfile.api, which is what actually ships.
execFileSync("npx", ["vite", "build", "--mode", "production"], {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env, VITE_API_LIVE: "true" },
});
const assets = join(root, "dist", "assets");
const budget = JSON.parse(readFileSync(join(root, "bundle-budget.json"), "utf8"));
const max = budget.eagerChunkGzipMaxBytes;

const entries = readdirSync(assets);

// The eager chunk is the largest index-*.js. Vite also emits a tiny index-*.js
// stub, so pick by size rather than by name order.
const candidates = entries
  .filter((f) => /^index-.*\.js$/.test(f))
  .map((f) => ({ f, bytes: statSync(join(assets, f)).size }))
  .sort((a, b) => b.bytes - a.bytes);
if (candidates.length === 0) {
  console.error("No dist/assets/index-*.js found — did the build succeed?");
  process.exit(1);
}
const { f: name } = candidates[0];
const raw = readFileSync(join(assets, name));

const gz = gzipSync(raw).length;
const headroom = max - gz;
const line = `eager chunk ${name}: ${raw.length} B raw / ${gz} B gzip (budget ${max} B, headroom ${headroom} B)`;

if (gz > max) {
  console.error(`BUNDLE BUDGET EXCEEDED\n  ${line}\n
This is a deliberate decision, not a formality. Either bring the chunk back under
the line (lazy-load the new surface, as AuthModal/HistoryDrawer/Nudge already do),
or raise eagerChunkGzipMaxBytes in frontend/bundle-budget.json IN THIS PR and say
why in the PR body.`);
  process.exit(1);
}
console.log(`bundle budget OK — ${line}`);
