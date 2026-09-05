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
import { readFileSync, existsSync } from "node:fs";
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
    // 60 s, not the 5 s default: Vite bundles the config through esbuild, and
    // the rest of the suite is running. This is a budget for a genuinely heavy
    // operation in a NEW test, not a raise on an existing one (#344).
  }, 60_000);
});
