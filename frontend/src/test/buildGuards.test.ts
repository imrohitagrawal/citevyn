/**
 * @vitest-environment node
 *
 * jsdom's TextEncoder does not produce a real Uint8Array, which trips esbuild's
 * startup invariant — and Vite's config loader (used by the #343 guard below)
 * bundles through esbuild. Nothing in this file touches the DOM, so it runs in
 * the node environment instead.
 */
/**
 * Guards that the BUILD-TOOLING guards are actually selected and actually run.
 *
 * WHY THIS FILE IS IN src/ AND NOT scripts/
 * -----------------------------------------
 * `scripts/bundle-budget.test.mjs` holds ~60 assertions protecting the eager
 * bundle budget gate (#323). Vitest only runs them because `vite.config.ts`'s
 * `test.include` was widened to cover `scripts/**`. Revert that one array
 * element and the whole file is silently deselected: `npm test` drops from 413
 * tests to 350, exits 0, and reports nothing wrong — the bundle gate is
 * unguarded again with no visible signal. (Reproduced in review.)
 *
 * A guard against that cannot live in `scripts/`, because the same revert would
 * deselect the guard along with everything it guards. It lives here, under the
 * ORIGINAL `src/**` glob, which is the only place it survives.
 *
 * WHY IT ASKS VITEST INSTEAD OF READING THE CONFIG
 * ------------------------------------------------
 * The first version of this file asserted that vite.config.ts's TEXT contained
 * a `scripts/**` glob. A reviewer defeated that four ways while it stayed
 * green, because test selection is decided by the RESOLVED config plus argv,
 * and a regex over the source sees neither:
 *   - adding `exclude: ["scripts/**"]` — `include` is untouched
 *   - narrowing the glob's extension (`scripts/**\/*.{test,spec}.ts`) — still
 *     contains the matched substring
 *   - moving the glob into a comment — still text
 *   - adding a `projects` key, which overrides selection entirely
 * So this now runs vitest's own file resolver and asserts the real answer. That
 * is the project's standing lesson: resolve what the system resolved, do not
 * grep for a string that suggests it.
 *
 * This is the repeated defect here — a check that is written but never
 * executed: `--list` counting skipped Playwright tests, a suite whose every
 * test skipped while the job exited 0, a workflow whose job ran nothing.
 */
import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join, dirname, basename } from "node:path";
import { spawnSync } from "node:child_process";
import { load } from "js-yaml";
import ts from "typescript";

const frontendRoot = join(__dirname, "..", "..");

type WorkflowFile = { jobs: { build: { steps?: { run?: unknown }[] } } };

/**
 * The files vitest ACTUALLY selects, straight from vitest. `list --filesOnly`
 * resolves the config and globs exactly as a real run does, but executes no
 * tests, so there is no recursion.
 *
 * Spawning a second vitest costs ~2 s alone but well over 5 s while the rest of
 * the suite is running, so it is done ONCE in `beforeAll` with an explicit
 * timeout rather than per test. Both were needed: without the shared call this
 * paid the cost twice, and without the timeout it tripped vitest's 5 s default
 * and failed the run — which is a flake in a REQUIRED check, and the exact
 * shape tracked as #344. A slow probe must not read as a broken guard.
 */
function listSelectedTestFiles(): string[] {
  const r = spawnSync("npx", ["vitest", "list", "--filesOnly"], {
    cwd: frontendRoot,
    encoding: "utf8",
    timeout: 120_000,
  });
  // Fail loudly rather than returning [] — an empty list would otherwise make
  // "the scripts suite is missing" and "the probe broke" indistinguishable,
  // and only one of those is a real defect.
  if (r.status !== 0) {
    throw new Error(`vitest list failed (status ${r.status}): ${r.stderr || r.stdout}`);
  }
  const files = (r.stdout || "")
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => /\.(test|spec)\.[cm]?[jt]sx?$/.test(l));
  if (files.length === 0) throw new Error(`vitest list returned no test files: ${r.stdout}`);
  return files;
}

describe("the build-tooling test suite is really selected by vitest", () => {
  let selected: string[] = [];
  // 120 s, not the 5 s default: this spawns a whole second vitest, and the
  // machine is already running the rest of the suite. The spawn itself also
  // throws on a non-zero exit or an empty list, so a broken probe fails loudly
  // here rather than making every assertion below vacuously true.
  beforeAll(() => {
    selected = listSelectedTestFiles();
  }, 120_000);

  it("vitest selects scripts/bundle-budget.test.mjs", () => {
    expect(selected).toContain("scripts/bundle-budget.test.mjs");
  });

  // The partner assertion: proves the probe can see files at all, so the check
  // above cannot pass vacuously on a broken or empty listing.
  it("and still selects the app suite, so widening did not replace src/", () => {
    expect(selected).toContain("src/test/buildGuards.test.ts");
    expect(selected.filter((f) => f.startsWith("src/")).length).toBeGreaterThan(10);
  });

  it("the file that glob selects exists on disk", () => {
    expect(existsSync(join(frontendRoot, "scripts", "bundle-budget.test.mjs"))).toBe(true);
  });
});

/**
 * `vitest list` resolves the CONFIG. It knows nothing about the argv CI passes,
 * so narrowing the command narrows the run while every config-level check above
 * stays green — `"test": "vitest run src"` drops 64 tests with vite.config.ts
 * untouched. Found by the skeptic round, which also noted its own suggested fix
 * did not cover it. Both levels are therefore pinned: the npm script, and the
 * workflow step that invokes it.
 */
describe("nothing narrows the test run at the command level", () => {
  it("package.json's test script runs the whole suite, unfiltered", () => {
    const pkg = JSON.parse(readFileSync(join(frontendRoot, "package.json"), "utf8"));
    expect(pkg.scripts.test).toBe("vitest run");
  });

  it("the workflow invokes it as a bare `npm test`, with no path argument", () => {
    const wf = load(
      readFileSync(join(frontendRoot, "..", ".github", "workflows", "frontend.yml"), "utf8"),
    ) as WorkflowFile;
    const runs = (wf.jobs.build.steps ?? [])
      .map((s) => s.run)
      .filter((r): r is string => typeof r === "string")
      .map((r) => r.trim());
    expect(runs).toContain("npm test");
    expect(runs.some((r) => r.startsWith("npm test ") || r.startsWith("npm test -"))).toBe(false);
  });
});

describe("index.html pulls in NOTHING the bundle gate cannot see", () => {
  // The gate measures Vite's module graph. A plain `<script src="/thing.js">`
  // pointing at `frontend/public/` is outside that graph entirely, so it is
  // render-blocking JS the ceiling cannot see. Reproduced by an adversarial
  // review: a 73,869 B gzip script in `<head>` took real eager JS to 2.06x the
  // ceiling while `check:bundle` reported 1,678 B of headroom and every test
  // stayed green.
  //
  // Teaching the gate to parse HTML re-opens a class of hazards its own header
  // documents (attribute quoting, `data-src` shadowing `src`, comments,
  // <noscript>/<template>, regex backtracking). Pinning the ONE script tag that
  // exists is cheaper and closes the demonstrated path: adding a second makes
  // this red, and the fix is either to import it through the module graph
  // (where the gate counts it) or to argue for it here.
  //
  // WHAT THIS STILL CANNOT SEE, stated rather than implied:
  //   - It reads the SOURCE `frontend/index.html`, not the emitted
  //     `dist/index.html`. `vite.config.ts` already loads `liveStubPlugin()`,
  //     and ANY plugin's `transformIndexHtml` can inject a tag this never
  //     looks at. That is this repo's "guard the emitted artifact" shape and it
  //     is a real residual gap, not a theoretical one.
  //   - A script written by another script at runtime.
  //   - A tag inside an HTML comment or <template> (it would be counted here
  //     but not fetched — a false POSITIVE, which is the safe direction).
  const html = readFileSync(join(frontendRoot, "index.html"), "utf8");

  it("declares exactly one <script>, and it is the module entry", () => {
    const scripts = [...html.matchAll(/<script\b[^>]*>/gi)].map((m) => m[0]);
    expect(scripts).toHaveLength(1);
    expect(scripts[0]).toMatch(/type=["']module["']/);
    expect(scripts[0]).toMatch(/src=["']\/src\/main\.tsx["']/);
  });

  it("preloads no script the gate would not have counted", () => {
    // `rel="preload" as="script"` and `rel="modulepreload"` both fetch JS
    // before first paint without being a <script> tag.
    //
    // The first version matched `rel=["'](modulepreload|preload)["']` and was
    // bypassed FOUR ways in review, each of which injects a heavy script while
    // the guard stays green: an unquoted `rel=preload`, `rel="preload "` with a
    // trailing space, `rel="preload alternate"` (rel is a space-separated TOKEN
    // LIST, not a string), and `as=script` unquoted. So: capture the value with
    // an optional quote, then TOKENIZE it, which is what the HTML spec says the
    // attribute is.
    const attr = (tag: string, name: string): string | null => {
      const m = new RegExp(`\\b${name}\\s*=\\s*("[^"]*"|'[^']*'|[^\\s>]+)`, "i").exec(tag);
      return m ? m[1].replace(/^["']|["']$/g, "").trim() : null;
    };
    const tokens = (v: string | null) => (v ? v.toLowerCase().split(/\s+/).filter(Boolean) : []);
    const preloads = [...html.matchAll(/<link\b[^>]*>/gi)]
      .map((m) => m[0])
      .filter((tag) => {
        const rel = tokens(attr(tag, "rel"));
        if (rel.includes("modulepreload")) return true;
        if (!rel.includes("preload") && !rel.includes("prefetch")) return false;
        // `as` decides what a preload fetches. Anything that is not plainly a
        // non-script resource counts, so an unrecognised or absent `as` fails
        // CLOSED rather than being waved through.
        const as = (attr(tag, "as") || "").toLowerCase();
        return !["style", "font", "image", "fetch", "document"].includes(as);
      });
    expect(preloads).toEqual([]);
  });

  it("the attribute parser handles the forms that bypassed its first version", () => {
    // Partner. The assertion above counts toward ZERO, so on its own it cannot
    // tell "nothing matched" from "the matcher is broken". These are the exact
    // four shapes a reviewer used to smuggle a script past it.
    const bypasses = [
      `<link rel=modulepreload href="/x.js">`,
      `<link rel="preload " as="script" href="/x.js">`,
      `<link rel="preload alternate" as="script" href="/x.js">`,
      `<link rel="preload" as=script href="/x.js">`,
    ];
    const attr = (tag: string, name: string): string | null => {
      const m = new RegExp(`\\b${name}\\s*=\\s*("[^"]*"|'[^']*'|[^\\s>]+)`, "i").exec(tag);
      return m ? m[1].replace(/^["']|["']$/g, "").trim() : null;
    };
    const tokens = (v: string | null) => (v ? v.toLowerCase().split(/\s+/).filter(Boolean) : []);
    for (const tag of bypasses) {
      const rel = tokens(attr(tag, "rel"));
      const as = (attr(tag, "as") || "").toLowerCase();
      const caught =
        rel.includes("modulepreload") ||
        ((rel.includes("preload") || rel.includes("prefetch")) &&
          !["style", "font", "image", "fetch", "document"].includes(as));
      expect(caught, `this form slips past the matcher: ${tag}`).toBe(true);
    }
    // And a legitimate font preload is NOT caught, so the rule is not "reject
    // every link".
    const fontTag = `<link rel="preload" as="font" href="/f.woff2" crossorigin>`;
    expect(tokens(attr(fontTag, "rel"))).toContain("preload");
    expect((attr(fontTag, "as") || "").toLowerCase()).toBe("font");
  });

  it("the probe can see the tags it is filtering, so the checks above are not vacuous", () => {
    // Partner for two assertions that both count toward zero. Without it, a
    // regex that matched nothing at all would satisfy both.
    expect(html).toMatch(/<link\b/i);
    expect([...html.matchAll(/<link\b[^>]*>/gi)].length).toBeGreaterThan(3);
  });
});

describe("the bundle gate is reachable from npm", () => {
  it("package.json defines check:bundle pointing at the real script", () => {
    const pkg = JSON.parse(readFileSync(join(frontendRoot, "package.json"), "utf8"));
    expect(pkg.scripts["check:bundle"]).toBe("node scripts/check-bundle-size.mjs");
    expect(existsSync(join(frontendRoot, "scripts", "check-bundle-size.mjs"))).toBe(true);
  });

  it("bundle-budget.json holds a positive integer ceiling", () => {
    const budget = JSON.parse(readFileSync(join(frontendRoot, "bundle-budget.json"), "utf8"));
    expect(Number.isInteger(budget.eagerChunkGzipMaxBytes)).toBe(true);
    expect(budget.eagerChunkGzipMaxBytes).toBeGreaterThan(0);
  });
});

/**
 * #343: `tsc -b` must not emit compiled configs into the frontend root.
 *
 * `tsconfig.node.json` is `"composite": true`, and composite FORCES emit. With
 * no `outDir` the emit landed next to the sources — `vite.config.js` beside
 * `vite.config.ts` — and Vite's DEFAULT_CONFIG_FILES
 * (node_modules/vite/dist/node/constants.js) lists `vite.config.js` BEFORE
 * `vite.config.ts`. `npm run build` regenerates first so it stays consistent,
 * but `npm run dev` and `npm run preview` do not, and `npm run dev` is exactly
 * what `playwright.config.ts`'s `webServer.command` runs. A stale compiled copy
 * therefore decided the dev server's port, proxy target and live-stub plugin,
 * and presented as an inexplicable test failure rather than as a config
 * problem. It was found the hard way in #323: an edit to `vite.config.ts`
 * changed the build output not at all.
 *
 * WHY IT ASKS THE COMPILER INSTEAD OF READING `outDir`
 * ----------------------------------------------------
 * Asserting that the JSON contains an `outDir` key proves nothing about where
 * bytes land — `outDir: "."` contains the key and reinstates the bug. So this
 * calls `ts.getOutputFileNames`, the function the compiler itself uses to
 * decide an emit path, and then asks VITE which config file it resolves. Two
 * different systems, each answering for its own half of the causal chain.
 *
 * WHY IT DOES NOT SPAWN `tsc`
 * ---------------------------
 * The first version spawned a real `tsc -b --force`. Measured: it pushed the
 * full suite from 36 s to 55 s under load and made #344's timeouts WORSE — a
 * guard against one flake must not manufacture another in the same required
 * check. `getOutputFileNames` is the same resolution without the process.
 */
describe("tsc -b keeps its emit out of the frontend root (#343)", () => {
  const configPath = join(frontendRoot, "tsconfig.node.json");
  const read = ts.readConfigFile(configPath, (f) => ts.sys.readFile(f));
  // Without this, a typo'd second argument returns an EMPTY config and every
  // check below silently inspects tsc's DEFAULTS instead of the real file —
  // which is how the first draft of this guard passed for the wrong reason.
  if (read.error) {
    throw new Error(
      `could not read ${configPath}: ${ts.flattenDiagnosticMessageText(read.error.messageText, " ")}`,
    );
  }
  // configFileName (5th arg) is load-bearing: without it `options.configFilePath`
  // is unset and `getOutputFileNames` throws a bare "Debug Failure" when it
  // tries to work out the common source directory.
  const parsed = ts.parseJsonConfigFileContent(read.config, ts.sys, frontendRoot, undefined, configPath);
  if (parsed.errors.length) {
    throw new Error(
      `tsconfig.node.json did not parse: ${parsed.errors
        .map((e) => ts.flattenDiagnosticMessageText(e.messageText, " "))
        .join("; ")}`,
    );
  }
  const outDir = parsed.options.outDir ?? "";
  // Only sources sitting directly in the frontend root can collide with a
  // config that Vite or Playwright would load from there.
  const rootSources = parsed.fileNames.filter((f) => dirname(f) === frontendRoot);

  it("the project still covers the configs this guard is about", () => {
    // Guards every loop below against going vacuous if `include` is emptied.
    expect(rootSources.length).toBeGreaterThanOrEqual(5);
    expect(rootSources.map((f) => basename(f))).toContain("vite.config.ts");
    expect(rootSources.map((f) => basename(f))).toContain("vite.liveStub.ts");
  });

  // THE PARTNER ASSERTION. The checks after it are all "X is absent"; this one
  // proves the compiler really does emit these files somewhere, so absence can
  // never be mistaken for success.
  it("tsc resolves a real emit path for every one of them, all under the outDir", () => {
    expect(outDir).not.toBe("");
    expect(outDir).not.toBe(frontendRoot);
    for (const src of rootSources) {
      const outputs = ts.getOutputFileNames(parsed, src, false);
      expect(outputs.length, `tsc emits nothing for ${src}`).toBeGreaterThan(0);
      for (const out of outputs) {
        expect(
          out.startsWith(outDir + "/"),
          `tsc would emit ${out} — outside the outDir ${outDir}`,
        ).toBe(true);
        expect(
          dirname(out),
          `tsc would emit ${out} into the frontend root, where Vite will find it`,
        ).not.toBe(frontendRoot);
      }
    }
  });

  it("no compiled .js or .d.ts is sitting beside the .ts sources", () => {
    for (const src of rootSources) {
      for (const ext of [".js", ".d.ts"]) {
        const stray = src.replace(/\.ts$/, ext);
        expect(
          existsSync(stray),
          `${stray} exists — tsc is emitting into the frontend root again, or ` +
            `it is a leftover from before #343 and should be deleted`,
        ).toBe(false);
      }
    }
  });

  /**
   * `loadConfigFromFile` models what Vite does when it is asked with no
   * explicit config. It knows nothing about the ARGV the npm scripts pass, so
   * `"dev": "vite --config vite.dev.config.ts"` would leave every other test in
   * this block green while the dev server loaded something else entirely.
   *
   * This is the same hole, in the same file, that the vitest-selection guard
   * above was already burned by ("`vitest list` resolves the CONFIG. It knows
   * nothing about the argv CI passes"). Found again by review, one describe
   * block down. Both levels get pinned, the same way.
   */
  it("npm run dev and npm run preview do not redirect Vite at another config", () => {
    const pkg = JSON.parse(readFileSync(join(frontendRoot, "package.json"), "utf8"));
    expect(pkg.scripts.dev).toBe("vite");
    expect(pkg.scripts.preview).toBe("vite preview --port 4173");
    // playwright.config.ts's webServer.command is what actually starts the dev
    // server in an e2e run; if it stops being `npm run dev` the pin above stops
    // covering it.
    expect(readFileSync(join(frontendRoot, "playwright.config.ts"), "utf8")).toContain(
      "npm run dev",
    );
  });

  /**
   * The stray-file check below is only non-vacuous once something has actually
   * run `tsc`. In `frontend.yml` that holds because Type-check precedes Unit
   * tests — but nothing asserted the ORDER, so a reordered workflow would make
   * that test quietly always-true. (It would still be backed by the emit-path
   * test above, which is order-independent; this keeps the second line of
   * defence real rather than theoretical.)
   */
  it("the required workflow type-checks BEFORE it runs the unit tests", () => {
    const wf = load(
      readFileSync(join(frontendRoot, "..", ".github", "workflows", "frontend.yml"), "utf8"),
    ) as WorkflowFile;
    const runs = (wf.jobs.build.steps ?? [])
      .map((step) => step.run)
      .filter((r): r is string => typeof r === "string")
      .map((r) => r.trim());
    const typeCheck = runs.indexOf("npm run type-check");
    const unitTests = runs.indexOf("npm test");
    expect(typeCheck, "no `npm run type-check` step").toBeGreaterThanOrEqual(0);
    expect(unitTests, "no `npm test` step").toBeGreaterThanOrEqual(0);
    expect(typeCheck).toBeLessThan(unitTests);
  });

  /**
   * The reason a stray compiled config is now VISIBLE is that #343 deleted the
   * `.gitignore` lines that hid it. Re-add one line and the whole class goes
   * back to being invisible in `git status`, with nothing to notice.
   *
   * This asks GIT what it resolves rather than reading `.gitignore` for a
   * string — the file has `pw*.config.js` in it, which a substring check would
   * confuse with `playwright.config.js`, and ignore rules can arrive from the
   * repo root, a parent, or a global excludes file that no single file shows.
   */
  it("git does not ignore the compiled configs any more", () => {
    const names = rootSources.flatMap((src) => [
      basename(src).replace(/\.ts$/, ".js"),
      basename(src).replace(/\.ts$/, ".d.ts"),
    ]);
    const r = spawnSync("git", ["check-ignore", ...names], {
      cwd: frontendRoot,
      encoding: "utf8",
    });
    // git check-ignore exits 1 when NOTHING matches, 0 when something does, and
    // >1 on a real error — so the error case cannot be mistaken for success.
    expect(r.error, `git check-ignore failed to run: ${r.error}`).toBeUndefined();
    expect(
      r.status,
      `these compiled configs are gitignored again, which re-hides #343:\n${r.stdout}`,
    ).toBe(1);
    // Partner: the harness really did ask about the right files, and git is
    // reachable at all. A pw*.config.js IS still ignored, by design.
    const stillIgnored = spawnSync("git", ["check-ignore", "pwverify.config.js"], {
      cwd: frontendRoot,
      encoding: "utf8",
    });
    expect(stillIgnored.status, "git check-ignore is not working here").toBe(0);
    expect(names).toContain("vite.config.js");
  });

  it("and Vite, asked the way `npm run dev` asks, loads the .ts source", async () => {
    // The end of the causal chain: not "no .js exists" but "the file Vite
    // chose". This also covers the .mjs/.cjs/.mts/.cts names that sit ahead of
    // .ts in DEFAULT_CONFIG_FILES and that a .js/.d.ts check would miss —
    // proven: a stray vite.config.mjs kills only this test.
    const { loadConfigFromFile } = await import("vite");
    const loaded = await loadConfigFromFile(
      { command: "serve", mode: "development" },
      undefined,
      frontendRoot,
    );
    expect(loaded, "Vite resolved no config file at all").not.toBeNull();
    expect(loaded!.path).toBe(join(frontendRoot, "vite.config.ts"));
    // ...and it is the real config, not an empty stand-in that would make the
    // path assertion the only thing holding this up.
    expect(loaded!.config.server?.port).toBe(3000);
    // The build's ENTRY must stay index.html, which is the file the guard above
    // reads. A reviewer set `build.rollupOptions.input` to a second HTML file
    // carrying a `<script src="/heavy.js">` and a 266,669 B `public/heavy.js`:
    // the build succeeded, `dist/` shipped both, `check:bundle` reported
    // "headroom 1672 B", and every test stayed green — because the script tag
    // lived in a file nothing looks at. Pinning "no custom input" is one line
    // and closes it; changing the entry now has to change this test too.
    expect(
      loaded!.config.build?.rollupOptions?.input,
      "a custom rollup input means index.html is no longer the entry the script guard reads",
    ).toBeUndefined();
    // 60 s, not the 5 s default: Vite bundles the config through esbuild, and
    // the rest of the suite is running. This is a budget for a genuinely heavy
    // operation in a NEW test, not a raise on an existing one (#344).
  }, 60_000);
});
/**
 * Every e2e spec must take `test` from `tests/fixtures.ts`, which stubs the
 * render-blocking `fonts.googleapis.com` stylesheet.
 *
 * WHY A GUARD AT ALL. `tests/harness.spec.ts` proves the stub WORKS, but it can
 * only prove it for itself. A new spec file that imports `test` straight from
 * `@playwright/test` gets no stub, every one of its navigations goes back to
 * waiting on a third-party request before the app mounts, and nothing anywhere
 * says so — which is precisely how run 34050016261 reddened `main`.
 *
 * WHY IT PARSES INSTEAD OF GREPPING. This repo's standing lesson is that a
 * guard asserting a STRING is absent is not a guard. A `grep '@playwright/test'`
 * over each spec is defeated by `import { test as t }`, by
 * `import * as pw` + `pw.test(...)`, and by a type-only import that is not a
 * bypass at all but would trip the check. So this walks the TypeScript AST and
 * asks the same question the module loader does: which module supplies the
 * `test` binding this file calls? The last test in the block runs the parser
 * against those exact forms, so its coverage is demonstrated, not asserted.
 *
 * WHAT IT CANNOT SEE, stated rather than implied:
 *   - a spec that re-exports `test` through a third module
 *     (`export { test } from "@playwright/test"` in some helper, imported here)
 *   - a spec that builds its own `browser.newContext()` by hand instead of
 *     using the `page` fixture; the stub lives on the fixture
 *   - whether `fixtures.ts` still routes anything. That is
 *     `tests/harness.spec.ts`'s job, from inside a real browser.
 */
describe("every e2e spec is insulated from the third-party font stylesheet", () => {
  const testsDir = join(frontendRoot, "tests");
  const specs = readdirSync(testsDir)
    .filter((f) => f.endsWith(".spec.ts"))
    .sort();

  /**
   * The module specifiers that could supply this source's `test` binding.
   *
   * Named imports are matched on the IMPORTED name (`propertyName ?? name`), so
   * `import { test as t }` is caught. A namespace import from `@playwright/test`
   * counts too, since `pw.test(...)` is the same bypass with extra steps.
   * Type-only imports and type-only specifiers are skipped: they erase at
   * compile time and bind nothing at runtime.
   */
  function testBindingSources(source: string): string[] {
    const sf = ts.createSourceFile("spec.ts", source, ts.ScriptTarget.Latest, true);
    const found: string[] = [];
    for (const st of sf.statements) {
      if (!ts.isImportDeclaration(st) || !st.importClause) continue;
      if (!ts.isStringLiteral(st.moduleSpecifier)) continue;
      const from = st.moduleSpecifier.text;
      const clause = st.importClause;
      if (clause.isTypeOnly) continue;
      const bindings = clause.namedBindings;
      if (!bindings) continue;
      if (ts.isNamespaceImport(bindings)) {
        if (/^@playwright\/test$/.test(from)) found.push(from);
        continue;
      }
      for (const el of bindings.elements) {
        if (el.isTypeOnly) continue;
        if ((el.propertyName ?? el.name).text === "test") found.push(from);
      }
    }
    return [...new Set(found)];
  }

  // PARTNER for every "equals ./fixtures" assertion below: without it, an empty
  // spec list or a parser that finds nothing would satisfy all of them.
  it("the parser finds a real `test` binding in every spec file", () => {
    expect(specs.length).toBeGreaterThanOrEqual(6);
    expect(specs).toContain("landing.spec.ts");
    expect(specs).toContain("visual.spec.ts");
    for (const spec of specs) {
      const sources = testBindingSources(readFileSync(join(testsDir, spec), "utf8"));
      expect(sources, `no import supplies \`test\` in tests/${spec}`).toHaveLength(1);
    }
  });

  it("every spec except visual.spec.ts takes `test` from ./fixtures", () => {
    for (const spec of specs) {
      if (spec === "visual.spec.ts") continue;
      expect(
        testBindingSources(readFileSync(join(testsDir, spec), "utf8")),
        `tests/${spec} does not use tests/fixtures.ts, so its navigations wait on ` +
          `fonts.googleapis.com before the app can mount — see tests/fixtures.ts`,
      ).toEqual(["./fixtures"]);
    }
  });

  it("visual.spec.ts is the ONE exception, and it is deliberate", () => {
    // Its 22 darwin baselines are pixels of real Geist at maxDiffPixelRatio
    // 0.02; system-ui moves far more than 2% of the text pixels. It is excluded
    // from the CI job by playwright.demo-ci.config.ts (#325) and keeps the real
    // fonts. Adding a SECOND exception has to change this line.
    expect(testBindingSources(readFileSync(join(testsDir, "visual.spec.ts"), "utf8"))).toEqual([
      "@playwright/test",
    ]);
  });

  it("the parser catches the forms a substring check would miss", () => {
    // Demonstrated coverage, not claimed coverage. Each of these binds a usable
    // `test` from @playwright/test while a naive grep for
    // `import { test, expect } from "@playwright/test"` sees nothing.
    const bypasses = [
      `import { test as t, expect } from "@playwright/test";`,
      `import * as pw from "@playwright/test";`,
      `import { expect, test } from "@playwright/test";`,
      `import {\n  test,\n} from "@playwright/test";`,
    ];
    for (const src of bypasses) {
      expect(testBindingSources(src), `this form slips past the parser: ${src}`).toEqual([
        "@playwright/test",
      ]);
    }
    // ...and forms that are NOT a bypass are not flagged, so the rule is not
    // "reject every @playwright/test import".
    expect(testBindingSources(`import type { Page } from "@playwright/test";`)).toEqual([]);
    expect(testBindingSources(`import { expect, type Locator } from "@playwright/test";`)).toEqual(
      [],
    );
    expect(testBindingSources(`import { test, expect } from "./fixtures";`)).toEqual([
      "./fixtures",
    ]);
  });

  it("tests/fixtures.ts exists and is what the specs import", () => {
    expect(existsSync(join(testsDir, "fixtures.ts"))).toBe(true);
  });
});
