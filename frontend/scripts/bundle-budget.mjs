/**
 * Pure logic for the eager-bundle gzip budget gate. No side effects, no build,
 * no process.exit — so every branch below is reachable from a test.
 *
 * The runner (check-bundle-size.mjs) builds the shipping variant and calls
 * these. Splitting them is the point: the previous version was one top-level
 * script that ran a 7-second `vite build` on import, so nothing could test it,
 * and two guard defects (#323) sat in it unnoticed.
 *
 * WHAT THIS MEASURES. Two numbers, and only two are ENFORCED:
 *   1. `eagerChunkGzipMaxBytes` — the JS the browser fetches before first paint,
 *      taken from VITE'S OWN BUILD MANIFEST (`dist/.vite/manifest.json`): the
 *      entry chunk plus the transitive closure of its STATIC `imports`.
 *      `dynamicImports` are excluded.
 *   2. `lazyChunkGzipMaxBytes` (#372) — a PER-CHUNK gzip ceiling on every other
 *      emitted `.js` chunk. Not a sum: a per-chunk line, so a single new lazy
 *      surface crossing it has to be argued for, exactly as an eager one does.
 *
 * The lazy set is a SET DIFFERENCE — every emitted `.js` file in the manifest,
 * minus `eagerChunkFilesFromManifest`. It is NOT a graph walk out from
 * `dynamicImports`, and that is load-bearing: in this app's real manifest the
 * three `_`-prefixed shared chunks each declare `"imports": ["index.html"]`, a
 * BACK-EDGE to the entry. Any implementation that walks out from the lazy roots
 * and follows `imports` reaches the entry and files the 62,974 B entry chunk as
 * "lazy", handing it a 6,328 B allowance it clears by being mis-classified.
 *
 * EXCLUDING `dynamicImports` FROM THE EAGER NUMBER IS THE STATIC/DYNAMIC SPLIT,
 * and this file used to claim it was "precisely the eager/lazy split". It is
 * not, and a reviewer landed the difference: an UNCONDITIONAL module-scope
 * `import()` is filed under `dynamicImports` and excluded from the eager
 * number, while the browser still fetches it on every single load — an 80,020 B
 * gzip chunk moved that number by 2 B. #372 made this blind spot WORSE, not
 * better, and the honesty costs one sentence: such a chunk now lands in the
 * LAZY set, so it is handed a 6,328 B per-chunk allowance and passes. It went
 * from uncounted to SANCTIONED. The two splits coincide only while every
 * dynamic import sits behind a user action or a scroll, which is true of all
 * five of this app's today (AuthModal, HistoryDrawer, ConnectedAccountsDrawer,
 * Nudge, landing-strip) and is worth re-checking when a sixth lands.
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
 * WHAT IT DOES NOT MEASURE. This list used to say "lazily-imported chunks", and
 * #372 falsified that: they are now bounded INDIVIDUALLY. What is still
 * unbounded, measured on the shipping build at the time of writing:
 *   - CSS. The budget keys are about JS; the stylesheet is a separate 9,169 B
 *     gzip that no version of this gate has ever counted. Counting it now would
 *     silently consume the whole headroom.
 *   - The SUM of the lazy chunks (13,117 B over 8 chunks) and their COUNT. Both
 *     are RECORDED on every run by check-bundle-size.mjs and gated by nothing.
 *     A per-chunk ceiling actively REWARDS splitting one chunk into two, which
 *     costs one more round trip, and nothing here counts round trips.
 *   - Anything in frontend/public/ (about-theme.js, 1,512 B raw) — outside the
 *     module graph entirely. src/test/buildGuards.test.ts pins the single
 *     `<script>` in index.html, which is what closes the demonstrated path.
 *   - The self-hosted fonts, and the desktop first-paint total (62,974 + 4,722
 *     = 67,696 B today), which no key bounds — deliberately, because asserting
 *     it would go red the day desktop STARTS deferring, an improvement.
 *
 * See frontend/bundle-budget.json's `_comment` for the full blind-spot list and
 * for why a total-JS ceiling and a first-paint ceiling were both REJECTED.
 */
import { resolve, sep } from "node:path";

export const BUDGET_KEY = "eagerChunkGzipMaxBytes";

/**
 * #372: the PER-CHUNK gzip ceiling for everything that is NOT in the eager
 * closure. Same validated `parseBudget` path as BUDGET_KEY, so the #323
 * regression tests cover it too — a missing or mistyped key is a hard failure,
 * never a silent pass.
 */
export const LAZY_BUDGET_KEY = "lazyChunkGzipMaxBytes";

/**
 * A maximum taken over an EMPTY collection passes for a bundle of any shape:
 * delete every dynamic import and the lazy gate reports "0 violators" and goes
 * green. So an empty lazy collection is an error, not a pass.
 *
 * It is the partner for a check that otherwise counts nothing, and it can only
 * go red if lazy loading is abandoned WHOLESALE — never when it improves,
 * because splitting more out only makes this collection bigger.
 */
export const MIN_LAZY_CHUNKS = 1;

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
export function parseBudget(text, key = BUDGET_KEY) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    throw new Error(`bundle-budget.json is not valid JSON: ${err.message}`);
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("bundle-budget.json must contain a JSON object");
  }
  const max = parsed[key];
  // Number.isInteger rejects undefined, null, "66000", 6.5, NaN and Infinity.
  // The `> 0` rejects 0 and negatives, either of which would make the gate
  // either permanently red (useless) or meaningless.
  if (!Number.isInteger(max) || max <= 0) {
    throw new Error(
      `bundle-budget.json: ${key} must be a positive integer, got ${JSON.stringify(max)}. ` +
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
 * The LAZY set (#372): every emitted `.js` file in the manifest that is NOT in
 * the eager closure. A SET DIFFERENCE, deliberately — never a graph walk.
 *
 * WHY NOT A WALK. The obvious implementation is "start at the entry's
 * `dynamicImports` and follow `imports`". It is wrong on this app's REAL
 * manifest: the three `_`-prefixed shared chunks each declare
 * `"imports": ["index.html"]`, a BACK-EDGE pointing at the entry. A walk
 * therefore reaches the entry, files the 62,974 B entry chunk under "lazy", and
 * clears the 6,328 B per-chunk ceiling only because it is measuring the wrong
 * thing. The set difference cannot do that: whatever
 * `eagerChunkFilesFromManifest` claims is subtracted, by construction.
 *
 * Records whose `file` is not `.js` are SKIPPED and COUNTED. The count is
 * printed on every run, so "the gate silently stopped looking at something" is
 * not a state this can be in without saying so. A chunk emitted with some other
 * extension (`.mjs`, say) would land in that count rather than under the
 * ceiling — which is why the number is reported rather than assumed to be 0.
 *
 * Returns manifest-relative file paths plus that skipped count.
 */
export function nonEagerChunkFilesFromManifest(manifest) {
  // Validates the manifest shape and fails closed on a missing entry, so this
  // function inherits both guards rather than re-implementing them.
  const eagerFiles = new Set(eagerChunkFilesFromManifest(manifest));

  const files = [];
  const seen = new Set();
  let skippedNonJs = 0;
  for (const key of Object.keys(manifest)) {
    const record = manifest[key];
    if (!record || typeof record.file !== "string" || record.file === "") {
      // Fail CLOSED, exactly as the eager walker does: a record shape this does
      // not understand must not read as "nothing to measure here".
      throw new Error(`manifest.json record has no usable file: ${key}`);
    }
    if (!record.file.endsWith(".js")) {
      skippedNonJs += 1;
      continue;
    }
    if (eagerFiles.has(record.file)) continue;
    if (seen.has(record.file)) continue;
    seen.add(record.file);
    files.push(record.file);
  }
  return { files, skippedNonJs };
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

/**
 * The per-chunk lazy verdict (#372).
 *
 * FAIL-CLOSED FORM, and it is not a style choice. Written `c.gzip > max`, an
 * `undefined` ceiling makes every comparison false, the violator list comes back
 * EMPTY, and the gate passes for a bundle of any shape — #323 exactly, one
 * function over. Written `!(c.gzip <= max)`, an `undefined` ceiling makes every
 * chunk a violator and the build fails. `parseBudget` should have rejected the
 * bad ceiling long before this runs; this is the second line of defence, and
 * the reason there is one is that the first line is what failed last time.
 *
 * EVERY violator is named, not the first: a report that stops at one turns a
 * two-chunk regression into two review rounds.
 */
export function evaluateLazyChunks({ chunks, max }) {
  if (chunks.length < MIN_LAZY_CHUNKS) {
    throw new Error(
      `no lazy chunks found, but at least ${MIN_LAZY_CHUNKS} is required — a per-chunk ` +
        `maximum over an EMPTY collection passes for a bundle of any shape. Either the ` +
        `manifest shape changed, or every dynamic import has been removed.`,
    );
  }
  const violators = chunks.filter((c) => !(c.gzip <= max));
  const largest = chunks.reduce((a, b) => (b.gzip > a.gzip ? b : a));
  const line =
    `largest lazy chunk ${largest.file} (${largest.gzip} B gzip) ` +
    `(ceiling ${max} B each, headroom ${max - largest.gzip} B)`;
  return { ok: violators.length === 0, violators, largest, line };
}

/**
 * The numbers #372 is actually about, printed on EVERY run and on BOTH the pass
 * and the fail path.
 *
 * The complaint in #372 is not that a ceiling was missing — it is that the gate
 * REPORTED A WIN. #358 freed 3,521 B from the eager chunk and the command said
 * so, while desktop first-paint JS grew by 1,200 B and all-JS grew too, and
 * neither number appeared anywhere. A PR can no longer quote the eager delta
 * without the countervailing totals sitting in the same command's output.
 *
 * RECORDED, NOT GATED is the literal label, because a total-JS ceiling was
 * considered and REJECTED — see bundle-budget.json's `_comment` for the four
 * reasons, the first of which is that such a ceiling would have gone RED for
 * #358, a change that improved phone first-paint JS by 3,521 B.
 */
export function renderRecordedReport({ eager, lazy, max, skippedNonJs }) {
  const sorted = [...lazy.files].sort((a, b) => b.gzip - a.gzip);
  const allJsGzip = eager.totalGzip + lazy.totalGzip;
  const lines = [`lazy chunks: ${lazy.files.length}, ceiling ${max} B each`];
  for (const c of sorted) {
    lines.push(`  ${c.file} — ${c.gzip} B gzip (headroom ${max - c.gzip} B)`);
  }
  lines.push(`  ${evaluateLazyChunks({ chunks: lazy.files, max }).line}`);
  lines.push(
    `RECORDED, NOT GATED — lazy gzip SUM ${lazy.totalGzip} B over ${lazy.files.length} chunks`,
  );
  lines.push(
    `RECORDED, NOT GATED — all-JS gzip TOTAL ${allJsGzip} B over ` +
      `${eager.files.length + lazy.files.length} chunks ` +
      `(eager ${eager.totalGzip} + lazy ${lazy.totalGzip})`,
  );
  lines.push(`manifest records skipped as non-.js: ${skippedNonJs}`);
  return lines;
}

export const LAZY_EXCEEDED_ADVICE = `
A lazy chunk is off the critical path, not free: a reader who opens that surface
pays for it in one round trip. Either split the surface further, or raise
${LAZY_BUDGET_KEY} in frontend/bundle-budget.json IN THIS PR and say why in the
PR body. Note what raising it does NOT do: nothing bounds the SUM of the lazy
chunks or their COUNT, and both are printed above precisely so that a raise has
to be argued next to them.`;
