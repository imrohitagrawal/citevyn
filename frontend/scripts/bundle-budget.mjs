/**
 * Pure logic for the eager-bundle gzip budget gate. No side effects, no build,
 * no process.exit — so every branch below is reachable from a test.
 *
 * The runner (check-bundle-size.mjs) builds the shipping variant and calls
 * these. Splitting them is the point: the previous version was one top-level
 * script that ran a 7-second `vite build` on import, so nothing could test it,
 * and two guard defects (#323) sat in it unnoticed.
 *
 * WHAT THIS MEASURES: the JS the browser fetches before first paint, taken from
 * VITE'S OWN BUILD MANIFEST (`dist/.vite/manifest.json`) — the entry chunk plus
 * the transitive closure of its STATIC `imports`. `dynamicImports` are excluded,
 * which is precisely the eager/lazy split.
 *
 * WHY THE MANIFEST AND NOT index.html: the first fix for #323 read the entry
 * `<script type="module">` plus every `<link rel="modulepreload">` out of the
 * built index.html. Review reproduced the same under-reporting defect that fix
 * was written to remove: with `build.modulePreload: false`, Vite emits NO
 * preload links at all, so a statically-imported vendor chunk vanished from the
 * measurement. Measured on this app: index.html named only the 19.25 kB entry
 * while the browser also had to fetch a 45.69 kB vendor chunk — a 45,690 B
 * under-report, gate green. Preload links are a HINT; the manifest is the
 * build's own statement of the module graph. Reading it also removes an entire
 * class of HTML-parsing hazards (attribute quoting, `rel` token lists,
 * `data-src` shadowing a real `src`, comments, <noscript>/<template>, query
 * strings, off-origin URLs, and regex backtracking on a large file).
 *
 * WHAT IT DOES NOT MEASURE, deliberately:
 *   - CSS. The budget key is about JS; the stylesheet is a separate ~8.9 kB
 *     gzip that no version of this gate has ever counted. Counting it now would
 *     silently consume the whole headroom.
 *   - Lazily-imported chunks (AuthModal, HistoryDrawer, Nudge, ...), which the
 *     manifest lists under `dynamicImports`.
 */
import { resolve, sep } from "node:path";

export const BUDGET_KEY = "eagerChunkGzipMaxBytes";

/**
 * A build that measures near-zero is a broken build, not a win. Without this,
 * an empty or truncated entry chunk reports "headroom 65,980 B" and exits 0 —
 * a check that counts nothing and calls it success. The real graph is ~65 kB,
 * so this floor cannot be reached by any healthy build of this app.
 */
export const MIN_PLAUSIBLE_GZIP = 1000;

/**
 * Read and VALIDATE the budget.
 *
 * The validation is the whole point (#323). The previous version did
 * `const max = budget.eagerChunkGzipMaxBytes` and compared with `gz > max`.
 * Mistype or rename that key — a typo, a refactor, a bad merge — and `max` is
 * `undefined`, `gz > undefined` is `false`, the "EXCEEDED" branch never fires,
 * and the gate prints `bundle budget OK ... headroom NaN B` and exits 0 for a
 * bundle of ANY size. Measured on this repo before the fix: a 65,516 B chunk
 * reported `budget undefined B, headroom NaN B`, exit code 0.
 *
 * So: anything that is not a positive integer is a hard failure, not a pass.
 */
export function parseBudget(text) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    throw new Error(`bundle-budget.json is not valid JSON: ${err.message}`);
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("bundle-budget.json must contain a JSON object");
  }
  const max = parsed[BUDGET_KEY];
  // Number.isInteger rejects undefined, null, "66000", 6.5, NaN and Infinity.
  // The `> 0` rejects 0 and negatives, either of which would make the gate
  // either permanently red (useless) or meaningless.
  if (!Number.isInteger(max) || max <= 0) {
    throw new Error(
      `bundle-budget.json: ${BUDGET_KEY} must be a positive integer, got ${JSON.stringify(max)}. ` +
        `A missing or mistyped key used to read as \`undefined\` and pass the gate silently (#323).`,
    );
  }
  return max;
}

/**
 * The exact build this gate measures, as data so a test can pin it.
 *
 * Every argument here is load-bearing and none of them was covered by a test in
 * the first version of this fix (review finding):
 *   --mode production        matches `npm run build`
 *   --config vite.config.ts  `tsc -b` emits a COMPILED vite.config.js beside the
 *                            source and Vite resolves `.js` FIRST, so without
 *                            this the gate can build against a stale config —
 *                            an edit to vite.config.ts had no effect on the
 *                            output at all while #323 was being fixed
 *   --manifest               emits dist/.vite/manifest.json, which IS the
 *                            measurement input
 *   VITE_API_LIVE=true       matches infra/docker/Dockerfile.api; a build
 *                            without it is ~860 B larger and is not what ships
 */
export function buildCommand() {
  return {
    cmd: "npx",
    args: ["vite", "build", "--mode", "production", "--config", "vite.config.ts", "--manifest"],
    env: { VITE_API_LIVE: "true" },
  };
}

/**
 * The eager JS graph, from Vite's build manifest: every entry chunk plus the
 * transitive closure of its static `imports`. `dynamicImports` are NOT
 * followed — that is the lazy half, and following it would count AuthModal,
 * HistoryDrawer and Nudge as if they blocked first paint.
 *
 * Returns manifest-relative file paths (e.g. "assets/index-abc.js").
 */
export function eagerChunkFilesFromManifest(manifest) {
  if (manifest === null || typeof manifest !== "object" || Array.isArray(manifest)) {
    throw new Error("manifest.json must contain a JSON object");
  }

  const entries = Object.keys(manifest).filter((k) => manifest[k] && manifest[k].isEntry === true);
  if (entries.length === 0) {
    // Fail CLOSED. No entry means the build shape changed under us; reporting
    // "0 B, lots of headroom" would be the same silent pass this file exists
    // to remove.
    throw new Error(
      "manifest.json declares no entry chunk (no record with isEntry: true) — " +
        "the build output shape changed and the eager graph cannot be determined.",
    );
  }

  const files = [];
  const seen = new Set();
  const visit = (key) => {
    if (seen.has(key)) return;
    seen.add(key);
    const record = manifest[key];
    if (!record) {
      throw new Error(`manifest.json references an unknown chunk: ${key}`);
    }
    if (typeof record.file !== "string" || record.file === "") {
      throw new Error(`manifest.json record has no file: ${key}`);
    }
    files.push(record.file);
    // `imports` only. `dynamicImports` is deliberately not followed.
    for (const imported of record.imports ?? []) visit(imported);
  };
  for (const entry of entries) visit(entry);

  // Deduplicate on the emitted FILE: two manifest keys can point at one file.
  return [...new Set(files)];
}

/**
 * Resolve a manifest-relative file under distDir and refuse anything that
 * escapes it.
 *
 * `realpath` is injected rather than imported so this stays pure and testable,
 * and because it is the part that matters: without resolving symlinks, a link
 * inside dist/ pointing outside it passed containment and the gate happily
 * measured (and passed) a file from elsewhere — reproduced in review.
 *
 * `resolve` rather than `normalize`: resolve() strips a trailing separator (a
 * `--dist foo/` argument otherwise made containment reject every legitimate
 * asset) and makes a relative dist path absolute.
 */
export function resolveAssetPath({ distDir, file, realpath }) {
  const distRoot = realpath(resolve(distDir));
  const candidate = resolve(distRoot, file);
  // realpath the candidate too, so a symlink inside dist/ cannot point out of
  // it. The file may not exist yet, in which case there is nothing to follow
  // and the lexical check below is the whole guard.
  let resolved;
  try {
    resolved = realpath(candidate);
  } catch {
    resolved = candidate;
  }
  if (!resolved.startsWith(distRoot + sep)) {
    throw new Error(`manifest references an asset outside dist/: ${file}`);
  }
  return resolved;
}

/**
 * Gzip each eager file and sum. Summing per-file gzip sizes (rather than
 * gzipping the concatenation) is the correct model: the browser fetches N
 * separately-compressed responses.
 */
export function measureEagerGraph({ files, readAsset, gzipSize }) {
  const measured = files.map((file) => {
    const bytes = readAsset(file);
    return { file, raw: bytes.length, gzip: gzipSize(bytes) };
  });
  const totalGzip = measured.reduce((sum, f) => sum + f.gzip, 0);
  const totalRaw = measured.reduce((sum, f) => sum + f.raw, 0);
  return { files: measured, totalGzip, totalRaw };
}

/**
 * Decide pass/fail and render the report line.
 *
 * `ok` is the positive form (`totalGzip <= max`) so that an `undefined` budget
 * yields ok:false and fails closed, where the old `!(gz > max)` yielded true.
 * That is NOT a substitute for parseBudget's validation — JS would still coerce
 * a string budget (`65516 <= "999999"` is true), which is exactly why the
 * budget is validated as an integer before it ever reaches here.
 */
export function evaluate({ measurement, max }) {
  const { files, totalGzip, totalRaw } = measurement;
  if (totalGzip < MIN_PLAUSIBLE_GZIP) {
    throw new Error(
      `eager graph measured only ${totalGzip} B gzip, below the ${MIN_PLAUSIBLE_GZIP} B ` +
        `plausibility floor — the build output shape changed and this is not a real measurement.`,
    );
  }
  const headroom = max - totalGzip;
  const names = files.map((f) => `${f.file} (${f.gzip} B)`).join(" + ");
  const line =
    `eager graph ${files.length === 1 ? "" : `${files.length} files `}${names}: ` +
    `${totalRaw} B raw / ${totalGzip} B gzip total (budget ${max} B, headroom ${headroom} B)`;
  return { ok: totalGzip <= max, line, totalGzip, headroom };
}

export const EXCEEDED_ADVICE = `
This is a deliberate decision, not a formality. Either bring the eager graph back
under the line (lazy-load the new surface, as AuthModal/HistoryDrawer/Nudge
already do), or raise ${BUDGET_KEY} in frontend/bundle-budget.json IN THIS PR and
say why in the PR body.`;
