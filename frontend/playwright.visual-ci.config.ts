/**
 * Visual-snapshot Playwright config for CI (#325).
 *
 * THE EXACT COMPLEMENT OF `playwright.demo-ci.config.ts`
 * -----------------------------------------------------
 * `playwright.demo-ci.config.ts` runs the default suite MINUS `visual.spec.ts`.
 * This config runs `visual.spec.ts` AND NOTHING ELSE. Together the two are a
 * partition of what `playwright.config.ts` selects: every test runs in exactly
 * one CI job, none runs twice, and none runs nowhere. That partition is not
 * left to the reader — `.github/workflows/frontend.yml` asserts both halves
 * numerically, in each job's "Guard against a vacuous selection" step.
 *
 * WHY `testMatch` AND NOT `--grep "visual regression"`
 * ----------------------------------------------------
 * Same reason `playwright.demo-ci.config.ts` gives for choosing `testIgnore`:
 * the grep form silently stops matching if someone renames the describe block,
 * and here the failure would be a job that selects zero tests. It is caught
 * either way — `playwright test --list` exits 1 on an empty selection, and the
 * workflow's `visual_total -gt 0` line is the backstop — but a file-path rule
 * cannot be broken by a title edit in the first place.
 *
 * WHY THE BASELINES ARE PLATFORM-SPECIFIC, AND WHY THIS JOB RUNS IN A CONTAINER
 * ----------------------------------------------------------------------------
 * Playwright names a snapshot `<name>-<project>-<platform>.png`, so the darwin
 * baselines a developer regenerates locally are a different file set from the
 * linux ones CI compares against. Both belong in the repo. They are NOT expected
 * to be identical images: macOS rasterises text through CoreText and Linux
 * through FreeType/Skia, and measured across the 22 the two renders of the same
 * unchanged page differ by 0% to 2.68% of the section's pixels — `ticker` is
 * pixel-identical in both themes (it is masked), and seven are over the suite's
 * own 2% budget. That budget is never applied ACROSS platforms, so none of that
 * is a failure: each platform compares against its own file.
 *
 * AS OF THE COMMIT THAT ADDED THIS FILE the `-linux` set is generated but NOT
 * committed — it is held for the owner to look at the images first — so the
 * `visual-e2e` job fails every run with "snapshot missing" until it lands.
 *
 * The linux set is only reproducible if the browser is pinned, which is what
 * the job's `container:` buys. Measured while generating the first set (#325):
 *
 *   - two full runs in the same amd64 container produced BYTE-IDENTICAL PNGs
 *     for all 22 snapshots, `how-it-works` included;
 *   - Node 24.20.0 (the image's own) vs Node 26.8.2 (what `.nvmrc` resolves to)
 *     also produced byte-identical PNGs — Node is not an input to the render;
 *   - arm64 vs amd64 produced DIFFERENT pixels for 12 of the 22. The CPU
 *     architecture IS an input. GitHub's `ubuntu-latest` is x86_64, so the
 *     `-linux` baselines are amd64 baselines. Regenerating them on an
 *     Apple-silicon machine needs `docker run --platform linux/amd64`.
 *
 * WHICH BINARY ACTUALLY DRAWS THEM: `chromium_headless_shell`, not `chromium`.
 * Playwright launches the headless shell whenever `headless` is true, and
 * nothing in this repo sets it false (`playwright-core/lib/coreBundle.js`:
 * `options.headless ? "chromium-headless-shell" : "chromium"`). The two ship at
 * the same version — both `153.0.8010.12` at revision 1243 in this image — which
 * is why an earlier version of the CI job printed the wrong one and looked
 * right. It prints both now.
 *
 * REGENERATE THE LINUX BASELINES (from the repo root):
 *
 *   docker run --rm --platform linux/amd64 --ipc=host \
 *     -v "$PWD/frontend":/host:ro \
 *     -v "$PWD/frontend/tests/visual.spec.ts-snapshots":/out \
 *     mcr.microsoft.com/playwright:v1.63.0-noble bash -c '
 *       mkdir /work && cd /work &&
 *       tar -C /host --exclude=node_modules --exclude=dist --exclude=test-results \
 *           --exclude=playwright-report -cf - . | tar -xf - &&
 *       npm ci &&
 *       npx playwright test -c playwright.visual-ci.config.ts --update-snapshots=all &&
 *       cp tests/visual.spec.ts-snapshots/*-chromium-linux.png /out/'
 *
 * It copies the tree instead of working in a writable bind mount ON PURPOSE.
 * Mounting `frontend/` read-write and running `npm ci` inside overwrites your
 * host `node_modules` with linux-x64 binaries, and the next `npm run dev` on the
 * host then fails on esbuild/rollup with a platform error that says nothing
 * about docker. `:ro` makes that mistake impossible rather than merely unlikely.
 *
 * The image tag must match the `@playwright/test` version `package-lock.json`
 * RESOLVES — not the range `package.json` declares — or the browser that draws
 * the baselines is not the browser CI compares them with.
 * `backend/tests/test_gating_workflows.py` pins the two together.
 */
import base from "./playwright.config";
import { defineConfig } from "@playwright/test";

export default defineConfig({
  ...base,
  testMatch: /visual\.spec\.ts$/,
});
