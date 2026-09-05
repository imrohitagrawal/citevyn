/**
 * Pure logic for the eager-bundle gzip budget gate. No side effects, no build,
 * no process.exit — so every branch below is reachable from a test.
 *
 * The runner (check-bundle-size.mjs) builds the shipping variant and calls
 * these. Splitting them is the point: the previous version was one top-level
 * script that ran a 7-second `vite build` on import, so nothing could test it,
 * and two guard defects (#323) sat in it unnoticed.
 *
 * WHAT THIS MEASURES: the JS the browser fetches before first paint — the entry
 * `<script type="module">` plus every `<link rel="modulepreload">`, read out of
 * the built `index.html`.
 *
 * WHAT IT DOES NOT MEASURE, deliberately:
 *   - CSS. The budget key is about JS; the stylesheet is a separate ~8.9 kB
 *     gzip that no version of this gate has ever counted. Counting it now would
 *     silently consume the whole headroom.
 *   - Lazily-imported chunks (AuthModal, HistoryDrawer, Nudge, ...). They are
 *     not in index.html, which is exactly why deriving from index.html is the
 *     right source: it distinguishes eager from lazy by construction rather
 *     than by filename convention.
 */

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
export const BUDGET_KEY = "eagerChunkGzipMaxBytes";

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
 * Extract the eager JS graph's URLs from a built index.html.
 *
 * Comments are stripped FIRST. This repo's index.html carries a long comment
 * block about fonts that already contains tag-shaped text (`<strong>`, `<b>`);
 * a future comment showing an example `<script type="module">` would otherwise
 * be measured as if it shipped.
 *
 * Attribute order is not assumed — Vite writes
 * `<script type="module" crossorigin src=...>` today, but that is Rollup's
 * formatting, not a contract.
 */
export function eagerScriptUrlsFromHtml(html) {
  const withoutComments = html.replace(/<!--[\s\S]*?-->/g, "");

  const attr = (tag, name) => {
    const m = tag.match(new RegExp(`\\b${name}=["']([^"']*)["']`, "i"));
    return m ? m[1] : null;
  };

  const entries = [...withoutComments.matchAll(/<script\b[^>]*>/gi)]
    .map((m) => m[0])
    .filter((tag) => /\btype=["']module["']/i.test(tag))
    .map((tag) => attr(tag, "src"))
    .filter((src) => src !== null && src !== "");

  const preloads = [...withoutComments.matchAll(/<link\b[^>]*>/gi)]
    .map((m) => m[0])
    .filter((tag) => /\brel=["']modulepreload["']/i.test(tag))
    .map((tag) => attr(tag, "href"))
    .filter((href) => href !== null && href !== "");

  if (entries.length === 0) {
    // Fail CLOSED. An index.html with no module entry means the build shape
    // changed under us; reporting "0 B, lots of headroom" would be the same
    // class of silent pass this file exists to remove.
    throw new Error(
      'index.html contains no <script type="module" src="..."> entry — ' +
        "the build output shape changed and the eager graph cannot be determined.",
    );
  }

  // Deduplicate while preserving order: a chunk that is both the entry and
  // separately modulepreloaded must be counted once, not twice.
  return [...new Set([...entries, ...preloads])];
}

/**
 * Gzip each eager file and sum. Summing per-file gzip sizes (rather than
 * gzipping the concatenation) is the correct model: the browser fetches N
 * separately-compressed responses.
 */
export function measureEagerGraph({ urls, readAsset, gzipSize }) {
  const files = urls.map((url) => {
    const bytes = readAsset(url);
    return { url, raw: bytes.length, gzip: gzipSize(bytes) };
  });
  const totalGzip = files.reduce((sum, f) => sum + f.gzip, 0);
  const totalRaw = files.reduce((sum, f) => sum + f.raw, 0);
  return { files, totalGzip, totalRaw };
}

/**
 * Decide pass/fail and render the report line.
 *
 * `ok` is computed as `totalGzip <= max`, NOT as `!(totalGzip > max)`. With a
 * validated integer budget the two agree, but the positive form cannot be
 * satisfied by a non-number the way the old `gz > max` was.
 */
export function evaluate({ measurement, max }) {
  const { files, totalGzip, totalRaw } = measurement;
  const headroom = max - totalGzip;
  const names = files.map((f) => `${f.url} (${f.gzip} B)`).join(" + ");
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
