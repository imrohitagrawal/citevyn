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
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { load } from "js-yaml";

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
