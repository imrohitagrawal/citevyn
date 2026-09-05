/**
 * Tests for the eager-bundle budget gate (#323).
 *
 * The gate is a GUARD, and the defect it had was that it did not guard: a
 * mistyped budget key made `gz > undefined` evaluate to `false`, so it printed
 * "bundle budget OK" and exited 0 for a bundle of any size. So these tests are
 * written the way a guard's tests have to be — the last block drives the REAL
 * script as a REAL process and asserts the EXIT CODE, because the exit code is
 * the only thing CI actually consumes. Asserting on stdout text would pass for
 * a script that printed the right words and exited 0.
 */
import { describe, it, expect } from "vitest";
import { gzipSync } from "node:zlib";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

import {
  BUDGET_KEY,
  parseBudget,
  eagerScriptUrlsFromHtml,
  measureEagerGraph,
  evaluate,
} from "./bundle-budget.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");
const scriptPath = join(here, "check-bundle-size.mjs");

// The exact <head> shape Vite 6 emits today, comment block included. Kept
// verbatim rather than simplified: the comment is the reason the parser strips
// comments at all, and a simplified fixture would not exercise that.
const VITE_HTML = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <!--
      Icons live in frontend/public/. Note <strong>/<b> below — tag-shaped text
      inside a comment, which is why comments are stripped before scanning.
    -->
    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <script type="module" crossorigin src="/assets/index-abc.js"></script>
    <link rel="stylesheet" crossorigin href="/assets/index-abc.css">
  </head>
  <body><div id="root"></div></body>
</html>`;

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
    ['a string ceiling', `{"${BUDGET_KEY}": "66000"}`],
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
    const text = readFileSync(join(frontendRoot, "bundle-budget.json"), "utf8");
    expect(parseBudget(text)).toBeGreaterThan(0);
  });
});

describe("eagerScriptUrlsFromHtml — measure what the browser fetches, not a filename pattern", () => {
  it("finds the entry module in real Vite output", () => {
    expect(eagerScriptUrlsFromHtml(VITE_HTML)).toEqual(["/assets/index-abc.js"]);
  });

  // The manualChunks case from #323: code moves into vendor-*.js, which Vite
  // then modulepreloads. The old filename-glob gate stopped counting it and
  // under-reported by 45,690 B on this repo (measured).
  it("counts every modulepreloaded chunk, not just the entry", () => {
    const html = VITE_HTML.replace(
      '<link rel="stylesheet"',
      '<link rel="modulepreload" crossorigin href="/assets/vendor-xyz.js">\n    <link rel="stylesheet"',
    );
    expect(eagerScriptUrlsFromHtml(html)).toEqual([
      "/assets/index-abc.js",
      "/assets/vendor-xyz.js",
    ]);
  });

  it("excludes the stylesheet, the icon and the preconnect links", () => {
    const urls = eagerScriptUrlsFromHtml(VITE_HTML);
    expect(urls.some((u) => u.endsWith(".css"))).toBe(false);
    expect(urls.some((u) => u.includes("favicon"))).toBe(false);
    expect(urls.some((u) => u.includes("fonts.googleapis"))).toBe(false);
  });

  it("ignores a classic (non-module) script", () => {
    const html = VITE_HTML.replace(
      "<script type=\"module\"",
      '<script src="/assets/legacy.js"></script>\n    <script type="module"',
    );
    expect(eagerScriptUrlsFromHtml(html)).toEqual(["/assets/index-abc.js"]);
  });

  it("ignores a module script that is only inside an HTML comment", () => {
    const html = VITE_HTML.replace(
      "<!--",
      '<!-- example: <script type="module" src="/assets/NOT-SHIPPED.js"></script>\n      ',
    );
    expect(eagerScriptUrlsFromHtml(html)).toEqual(["/assets/index-abc.js"]);
  });

  it("does not assume Vite's attribute order", () => {
    const html = VITE_HTML.replace(
      '<script type="module" crossorigin src="/assets/index-abc.js">',
      "<script src='/assets/index-abc.js' crossorigin type='module'>",
    );
    expect(eagerScriptUrlsFromHtml(html)).toEqual(["/assets/index-abc.js"]);
  });

  it("counts a chunk once when it is both the entry and modulepreloaded", () => {
    const html = VITE_HTML.replace(
      '<link rel="stylesheet"',
      '<link rel="modulepreload" href="/assets/index-abc.js">\n    <link rel="stylesheet"',
    );
    expect(eagerScriptUrlsFromHtml(html)).toEqual(["/assets/index-abc.js"]);
  });

  // Fail CLOSED. "No entry found" must not measure 0 B and report headroom.
  it("throws when there is no module entry at all", () => {
    const html = VITE_HTML.replace(
      '<script type="module" crossorigin src="/assets/index-abc.js"></script>',
      "",
    );
    expect(() => eagerScriptUrlsFromHtml(html)).toThrow(/no <script type="module"/);
  });

  it("throws when the module entry has no src (an inline module is not a chunk)", () => {
    const html = VITE_HTML.replace(
      '<script type="module" crossorigin src="/assets/index-abc.js"></script>',
      '<script type="module">console.log("inline")</script>',
    );
    expect(() => eagerScriptUrlsFromHtml(html)).toThrow(/no <script type="module"/);
  });
});

describe("measureEagerGraph + evaluate", () => {
  const graphOf = (sizes) =>
    measureEagerGraph({
      urls: sizes.map((_, i) => `/assets/c${i}.js`),
      readAsset: (url) => Buffer.alloc(sizes[Number(url.match(/c(\d+)\.js/)[1])], "x"),
      gzipSize: (bytes) => bytes.length, // identity, so the arithmetic is visible
    });

  it("sums every eager file rather than taking the largest", () => {
    const m = graphOf([100, 250, 30]);
    expect(m.totalGzip).toBe(380);
    expect(m.files).toHaveLength(3);
  });

  it("passes when the total is under the ceiling", () => {
    expect(evaluate({ measurement: graphOf([100]), max: 200 }).ok).toBe(true);
  });

  it("passes when the total exactly equals the ceiling", () => {
    expect(evaluate({ measurement: graphOf([200]), max: 200 }).ok).toBe(true);
  });

  it("FAILS when the total exceeds the ceiling by one byte", () => {
    const r = evaluate({ measurement: graphOf([201]), max: 200 });
    expect(r.ok).toBe(false);
    expect(r.headroom).toBe(-1);
  });

  // The defect in miniature: two chunks that each fit but together do not.
  it("FAILS when the chunks individually fit but the graph does not", () => {
    expect(evaluate({ measurement: graphOf([150, 150]), max: 200 }).ok).toBe(false);
  });

  it("really gzips when handed a real gzip function", () => {
    const m = measureEagerGraph({
      urls: ["/assets/a.js"],
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
 * defect was precisely that a broken gate still exited 0. A test that never
 * runs the process cannot see that.
 */
describe("check-bundle-size.mjs as a process — the exit code is what CI consumes", () => {
  function fixture({ budgetJson, html, assets }) {
    const dir = mkdtempSync(join(tmpdir(), "bundle-gate-"));
    const dist = join(dir, "dist");
    mkdirSync(join(dist, "assets"), { recursive: true });
    writeFileSync(join(dist, "index.html"), html);
    for (const [name, content] of Object.entries(assets)) {
      writeFileSync(join(dist, "assets", name), content);
    }
    const budgetPath = join(dir, "budget.json");
    writeFileSync(budgetPath, budgetJson);
    return { dist, budgetPath };
  }

  function runGate({ budgetJson, html = VITE_HTML, assets = { "index-abc.js": "x".repeat(5000) } }) {
    const { dist, budgetPath } = fixture({ budgetJson, html, assets });
    const r = spawnSync(
      process.execPath,
      [scriptPath, "--dist", dist, "--budget", budgetPath],
      { encoding: "utf8" },
    );
    return { status: r.status, stdout: r.stdout ?? "", stderr: r.stderr ?? "" };
  }

  it("exits 0 for a graph under budget", () => {
    const r = runGate({ budgetJson: `{"${BUDGET_KEY}": 10000000}` });
    expect(r.status).toBe(0);
    expect(r.stdout).toMatch(/bundle budget OK/);
  });

  it("exits 1 for a graph over budget", () => {
    const r = runGate({ budgetJson: `{"${BUDGET_KEY}": 1}` });
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

  it("exits NON-ZERO when index.html has no module entry", () => {
    const r = runGate({
      budgetJson: `{"${BUDGET_KEY}": 10000000}`,
      html: "<!doctype html><html><head></head><body></body></html>",
    });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
  });

  it("exits NON-ZERO when index.html points at a missing asset", () => {
    const r = runGate({ budgetJson: `{"${BUDGET_KEY}": 10000000}`, assets: {} });
    expect(r.status).not.toBe(0);
  });

  // The escaped path must point at a file that EXISTS, or this test passes for
  // the wrong reason. The first version used "/../../../../etc/hosts", which
  // resolves to a path that is not there — so it died on ENOENT and stayed
  // green with the path guard deleted (caught by mutation). Writing a real file
  // one level above dist/ isolates the guard as the only thing that can fail it.
  it("refuses an index.html that reaches outside dist/, even to a file that exists", () => {
    const dir = mkdtempSync(join(tmpdir(), "bundle-gate-escape-"));
    const dist = join(dir, "dist");
    mkdirSync(join(dist, "assets"), { recursive: true });
    const outsider = join(dir, "outside.js");
    writeFileSync(outsider, "z".repeat(1000));
    expect(readFileSync(outsider, "utf8").length).toBe(1000); // it really is readable
    writeFileSync(
      join(dist, "index.html"),
      VITE_HTML.replace("/assets/index-abc.js", "/../outside.js"),
    );
    const budgetPath = join(dir, "budget.json");
    writeFileSync(budgetPath, `{"${BUDGET_KEY}": 10000000}`);

    const r = spawnSync(process.execPath, [scriptPath, "--dist", dist, "--budget", budgetPath], {
      encoding: "utf8",
    });
    expect(r.status).not.toBe(0);
    expect(r.stdout).not.toMatch(/bundle budget OK/);
    expect(r.stderr).toMatch(/outside dist/);
  });

  it("counts the modulepreloaded chunk, so a split graph can exceed the budget", () => {
    const html = VITE_HTML.replace(
      '<link rel="stylesheet"',
      '<link rel="modulepreload" href="/assets/vendor-xyz.js">\n    <link rel="stylesheet"',
    );
    const assets = { "index-abc.js": "a".repeat(5000), "vendor-xyz.js": "b".repeat(500000) };
    // Sized so the ENTRY alone passes and the GRAPH does not: exactly the
    // manualChunks blind spot the old filename-glob gate had.
    const entryOnly = gzipSync(Buffer.from("a".repeat(5000))).length;
    const budget = `{"${BUDGET_KEY}": ${entryOnly + 50}}`;
    expect(runGate({ budgetJson: budget, html, assets }).status).toBe(1);
    expect(runGate({ budgetJson: budget, html: VITE_HTML, assets }).status).toBe(0);
  });
});

/**
 * Guard the guard's INVOCATION. The --dist/--budget seam above exists for these
 * tests; if it ever appeared in the npm script, CI would stop building the
 * shipping variant and start measuring whatever was lying in dist/ — which is
 * the "wrong artifact" failure the script's docblock says it exists to prevent.
 */
describe("the CI invocation itself", () => {
  it("runs the script with no arguments, so CI always builds the shipping variant", () => {
    const pkg = JSON.parse(readFileSync(join(frontendRoot, "package.json"), "utf8"));
    expect(pkg.scripts["check:bundle"]).toBe("node scripts/check-bundle-size.mjs");
  });

  it("is wired into the frontend workflow", () => {
    const wf = readFileSync(
      join(frontendRoot, "..", ".github", "workflows", "frontend.yml"),
      "utf8",
    );
    expect(wf).toMatch(/run:\s*npm run check:bundle/);
  });
});
