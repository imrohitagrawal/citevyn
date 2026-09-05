/**
 * Guards that the BUILD-TOOLING guards are actually selected and actually run.
 *
 * WHY THIS FILE IS IN src/ AND NOT scripts/
 * -----------------------------------------
 * `scripts/bundle-budget.test.mjs` holds 40-odd assertions protecting the eager
 * bundle budget gate (#323). Vitest only runs them because `vite.config.ts`'s
 * `test.include` was widened to cover `scripts/**`. Revert that one array
 * element and the whole file is silently deselected: `npm test` goes from 377
 * tests to 341, exits 0, and reports nothing wrong — the bundle gate is
 * unguarded again with no visible signal. (Reproduced in review.)
 *
 * A guard against that cannot live in `scripts/`, because the same revert would
 * deselect the guard along with everything it guards. It lives here, under the
 * ORIGINAL `src/**` glob, which is the only place it survives.
 *
 * This is the repeated defect in this project: a check that is written but
 * never executed. `--list` counting skipped Playwright tests, a suite whose
 * every test skipped while the job exited 0, a workflow whose job ran nothing.
 */
import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const frontendRoot = join(__dirname, "..", "..");

describe("the build-tooling test suite is selected by vitest", () => {
  const viteConfig = () => readFileSync(join(frontendRoot, "vite.config.ts"), "utf8");

  it("vite.config.ts's test.include covers scripts/", () => {
    // Matched loosely on the glob's shape rather than byte-exactly, so
    // reformatting does not fail the build, but dropping scripts/ does.
    expect(viteConfig()).toMatch(/include:\s*\[[^\]]*["']scripts\/\*\*[^"']*["']/s);
  });

  it("still covers src/, so widening did not replace the app tests", () => {
    expect(viteConfig()).toMatch(/include:\s*\[[^\]]*["']src\/\*\*[^"']*["']/s);
  });

  it("the file that glob selects exists", () => {
    expect(existsSync(join(frontendRoot, "scripts", "bundle-budget.test.mjs"))).toBe(true);
  });

  // A second vitest config file would take precedence over vite.config.ts's
  // `test` block and could quietly narrow `include` back down.
  it.each(["vitest.config.ts", "vitest.config.js", "vitest.config.mjs", "vitest.workspace.ts"])(
    "no competing %s overrides the include",
    (name) => {
      expect(existsSync(join(frontendRoot, name))).toBe(false);
    },
  );
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
