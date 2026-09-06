/**
 * @vitest-environment jsdom
 *
 * This file needs a REAL HTML PARSER, so it runs in the jsdom environment and
 * uses the global `DOMParser`. That is why it is a separate file from
 * `buildGuards.test.ts`, which is pinned to `@vitest-environment node` because
 * jsdom's `TextEncoder` trips esbuild's startup invariant and its #343 guard
 * loads the Vite config through esbuild. Splitting the two lets each have the
 * environment it actually needs, with no new dependency.
 */
import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const frontendRoot = join(__dirname, "..", "..");

/**
 * #365: nothing in the EMITTED page's render-blocking path may be off-origin.
 *
 * WHAT WENT WRONG. `index.html` carried a plain
 * `rel=stylesheet` link to fonts.googleapis.com. A render-blocking stylesheet
 * also blocks `<script type="module">` from EXECUTING, and this app is one
 * module entry — so a slow or unreachable Google left the visitor on a BLANK
 * page: no spinner, no text, no error. Measured on this app at f171696: 191-323
 * ms to first rendered UI normally, 40 150 ms with that host stalled 40 s. It
 * hit CI too (#364, run 34050016261: 30 s of one identical blank video frame).
 *
 * WHY THIS READS `dist/`, NOT `index.html`. The guard above it reads the SOURCE
 * and says so in its own header: "ANY plugin's `transformIndexHtml` can inject a
 * tag this never looks at ... a real residual gap, not a theoretical one." This
 * repo has a scar for exactly that shape — a CSP constant pinned byte-exact and
 * mutation-proved, while a reviewer moved the bypass into the function that
 * EMITS the header and the full suite stayed green with inline script
 * executing. So this one builds and asserts what the BROWSER receives.
 *
 * WHY IT RUNS `npm run build` AND PINS THE WHOLE CHAIN. The first version of
 * this guard built with the bundle gate's `buildCommand()` — `vite build
 * --mode production --config vite.config.ts --manifest` — reasoning that
 * sharing the gate's flags stopped the two drifting apart. That was guarding a
 * PROXY, and adversarial review landed it: `infra/docker/Dockerfile.api` ships
 * `npm run build`, WITHOUT `--manifest`. A `vite.config.ts` plugin with
 * `apply: "build"` and a `transformIndexHtml` hook that returns early when
 * `config.build.manifest` is set therefore injects a third-party stylesheet
 * into the SHIPPED image while this guard, `check:bundle`, the whole vitest
 * suite (523 passed), pytest (1946 passed) and the Playwright specs all stay
 * green. `grep -c googleapis dist/index.html` was 1 for the shipped build and 0
 * for the guard's. Reproduced end to end, not theorised.
 *
 * So the build below is the SHIPPING one, and the two links in the chain are
 * pinned as tests: `package.json`'s `build` script, and the fact that the
 * Dockerfile invokes that script. Changing either now has to change this file.
 * This is the repo's own "a fix can repeat the defect" lesson — both of my
 * first two attempts at #365's guard keyed on something adjacent to the
 * artifact rather than on the artifact.
 *
 * It also follows the emitted stylesheet INTO the CSS, because the tag is not
 * the only way back to a third party: `@import url(https://...)` at the top of
 * a bundled stylesheet is render-blocking in exactly the same way, and a
 * `src: url(https://...)` in an `@font-face` puts the font files back on
 * someone else's network. Neither appears anywhere in `index.html`.
 *
 * WHAT IT DELIBERATELY DOES NOT DO: strip HTML comments before scanning. A
 * parser that honours comments can be defeated by a real tag disguised as one,
 * and counting a commented-out tag is a false POSITIVE — the safe direction.
 * The cost is that a third-party URL merely MENTIONED in a comment turns this
 * red; `index.html` carries a note saying so, because the first draft of that
 * note did exactly that.
 *
 * WHAT IT CANNOT SEE, stated rather than implied:
 *   - a URL assembled at runtime by script, or a stylesheet injected by JS
 *     (neither is render-blocking for the module entry, which is the defect).
 *   - lazily-loaded CSS chunks that no eager stylesheet links.
 *   - whether the app actually MOUNTS when fonts fail. That is
 *     `tests/fonts-offline.spec.ts`, in a real browser, and it is the half of
 *     this that a static check cannot supply.
 */
describe("the EMITTED page's render-blocking path reaches no third party (#365)", () => {
  const distDir = join(frontendRoot, "dist");
  let emittedHtml = "";
  let emittedCss: { file: string; text: string }[] = [];

  /**
   * The env the Dockerfile's frontend stage builds under, spelled out.
   *
   * `PATH`/`HOME` are inherited because npm and node need them; NOTHING ELSE
   * is. That is the whole point: the first version of this guard passed
   * `{...process.env, VITE_API_LIVE: "true"}`, and review defeated it with a
   * one-line plugin — `if (process.env.VITEST) return html;`. vitest sets
   * `VITEST`, `VITEST_WORKER_ID`, `VITEST_POOL_ID` and `NODE_ENV=test`; the
   * Dockerfile sets none of them and does set `VITE_API_DEMO_KEY`. So the
   * argv matched while the PROCESS did not, and a third-party stylesheet
   * reached `/app/frontend_dist/index.html` with all 30 tests here green.
   *
   * Fixing the flag and leaving the environment was guarding a proxy for the
   * third time in this change. The values below come from
   * `infra/docker/Dockerfile.api`'s `ARG` defaults, and the test after
   * `beforeAll` asserts they still match that file.
   */
  const SHIPPED_BUILD_ENV: Record<string, string> = {
    PATH: process.env.PATH ?? "",
    HOME: process.env.HOME ?? "",
    VITE_API_DEMO_KEY: "local-demo-key",
    VITE_API_LIVE: "true",
  };

  function runShippedBuild(extraEnv: Record<string, string> = {}) {
    return spawnSync("npm", ["run", "build"], {
      cwd: frontendRoot,
      encoding: "utf8",
      env: { ...SHIPPED_BUILD_ENV, ...extraEnv },
    });
  }

  /**
   * Build EXACTLY what ships — same argv AND same environment — and read what
   * it produced.
   */
  beforeAll(async () => {
    const r = runShippedBuild();
    // Fail LOUDLY on a broken build. A silent skip here would make every
    // assertion below vacuous, which is this file's own standing complaint.
    if (r.status !== 0) {
      throw new Error(
        `the production build this guard measures failed (status ${r.status}):\n` +
          `${r.stdout ?? ""}\n${r.stderr ?? ""}`,
      );
    }
    emittedHtml = readFileSync(join(distDir, "index.html"), "utf8");

    // Follow only the stylesheets the EMITTED HTML actually links, resolved
    // from dist/. Globbing dist/assets/*.css instead would also sweep in lazy
    // chunks nothing render-blocks, and would miss a stylesheet emitted
    // somewhere else entirely.
    emittedCss = linkedStylesheetHrefs(emittedHtml)
      .filter((h) => h.startsWith("/"))
      .map((h) => {
        const file = join(distDir, h.replace(/^\//, "").split(/[?#]/)[0]);
        return { file, text: existsSync(file) ? readFileSync(file, "utf8") : "" };
      });
  }, 120_000);

  it("the artifact this guard builds is the artifact the image ships", () => {
    // THE PIN, half one: the COMMAND. Without it the `beforeAll` above is only
    // *claimed* to build what ships, and review demonstrated the cost — a build
    // plugin branching on `--manifest` put a third-party stylesheet in the
    // Docker image while every gate in the repo stayed green.
    const pkg = JSON.parse(readFileSync(join(frontendRoot, "package.json"), "utf8"));
    expect(
      pkg.scripts.build,
      "the `build` script changed — this guard runs `npm run build` precisely because " +
        "that is what Dockerfile.api runs, so the two must be re-checked together",
    ).toBe("tsc -b && vite build");

    // ...and the Dockerfile really invokes THAT script, read by parsing its
    // frontend stage rather than by grepping the whole file. A substring check
    // was the first version and review defeated it in one edit: replacing the
    // build stanza with `RUN npx vite build --manifest --mode staging` while
    // leaving the words "npm run build" in a COMMENT above it kept the guard
    // green with the image building something else entirely.
    const stage = frontendStageOf(dockerfileText());
    expect(
      stage.runCommands.join(" && "),
      "Dockerfile.api's frontend stage no longer runs `npm run build`, so this guard " +
        "measures an artifact the image does not ship",
    ).toMatch(/\bnpm run build\b/);
    expect(
      stage.runCommands.some((c) => /\bvite build\b/.test(c)),
      "the frontend stage calls `vite build` directly, bypassing the `build` script " +
        "this guard pins",
    ).toBe(false);

    // ...and it copies THAT stage's dist into the image, otherwise the pin is
    // about a build whose output goes nowhere.
    expect(dockerfileText()).toMatch(
      /COPY --from=frontend[^\n]*\/fe\/dist\s+\/app\/frontend_dist/,
    );

    // THE PIN, half two: the ENVIRONMENT the Dockerfile builds under. The
    // values this guard passes must be the ARG defaults that file declares.
    for (const [name, value] of Object.entries({
      VITE_API_DEMO_KEY: "local-demo-key",
      VITE_API_LIVE: "true",
    })) {
      expect(
        dockerfileText(),
        `Dockerfile.api no longer declares ARG ${name}=${value}, so SHIPPED_BUILD_ENV is stale`,
      ).toMatch(new RegExp(`ARG\\s+${name}=${value}\\b`));
      expect(SHIPPED_BUILD_ENV[name]).toBe(value);
    }
  });

  it("and the build does not depend on anything the image will not have", () => {
    // The assertion the previous two rounds were missing. Pinning the argv and
    // then pinning the env are both still PROXIES — what actually matters is
    // that the output does not vary with the parent process. So: build twice
    // under deliberately different, hostile parent environments and require
    // byte-identical HTML.
    //
    // The second env carries exactly the variables review used to smuggle a tag
    // past this guard (`VITEST`, `VITEST_WORKER_ID`, `NODE_ENV=test`) plus a
    // couple of CI markers. A `transformIndexHtml` that branches on ANY of them
    // now produces two different files and fails here, whichever way round the
    // branch is written.
    const a = runShippedBuild();
    expect(a.status, `control build failed:\n${a.stderr ?? ""}`).toBe(0);
    const underShipped = readFileSync(join(distDir, "index.html"), "utf8");

    const b = runShippedBuild({
      VITEST: "true",
      VITEST_WORKER_ID: "1",
      VITEST_POOL_ID: "1",
      NODE_ENV: "test",
      CI: "true",
    });
    expect(b.status, `probe build failed:\n${b.stderr ?? ""}`).toBe(0);
    const underVitest = readFileSync(join(distDir, "index.html"), "utf8");

    // Content hashes are masked before comparing, and that is a real
    // concession rather than a convenience: `NODE_ENV=test` legitimately
    // changes the JS bundle's bytes (React builds differently, and dead-code
    // elimination differs), so the hash in `/assets/index-<hash>.js` moves.
    // Measured: that one attribute is the ONLY difference between the two
    // builds today. What must not vary is the SHAPE — which tags exist and
    // which URLs they name — so that is what is compared.
    //
    // Worth noting what this incidentally proved: before this fix the guard
    // inherited vitest's `NODE_ENV=test`, so it was measuring a different
    // bundle from the one that ships, quite apart from the injected-tag
    // bypass.
    const maskHashes = (html: string) => html.replace(/-[A-Za-z0-9_-]{8}\.(js|css)/g, "-HASH.$1");
    expect(
      maskHashes(underVitest),
      "the emitted HTML changes with the parent environment, so what this guard reads " +
        "is not necessarily what the Docker image ships. A build plugin branching on " +
        "VITEST/NODE_ENV/CI is exactly how a third-party stylesheet was smuggled into " +
        "the image while every test here stayed green.",
    ).toBe(maskHashes(underShipped));

    // Partner for the masking: it must not be so aggressive that it would hide
    // an injected tag. Prove it changes ONLY hash-shaped filenames.
    expect(maskHashes('<link href="https://evil.example/a.css">')).toBe(
      '<link href="https://evil.example/a.css">',
    );
    expect(maskHashes('<script src="/assets/index-BVQztSHY.js">')).toBe(
      '<script src="/assets/index-HASH.js">',
    );

    // Partner: the two builds really ran and really produced a page, so the
    // equality above is not two empty strings.
    expect(underShipped.length).toBeGreaterThan(500);
    expect(underShipped).toContain("<script type=\"module\"");

    // Leave dist/ as the shipping build for the assertions that follow.
    expect(runShippedBuild().status).toBe(0);
  }, 180_000);

  /**
   * PARSE THE HTML WITH A REAL PARSER, AND RESOLVE URLS WITH A REAL URL PARSER.
   *
   * This block hand-rolled both for two rounds, and adversarial review broke it
   * both times — each round I fixed the exact spellings that were demonstrated
   * and missed the next class. The demonstrated bypasses, all of which real
   * Chromium fetches off-origin:
   *
   *   `<link rel=stylesheet a=x" b="c>d" href="http://evil/x.css">`
   *        one stray quote in an unquoted value shifts the quote parity for the
   *        REST OF THE DOCUMENT, so every later tag boundary is wrong.
   *   `http:<TAB>//host/x`, `http:<LF>//host/x`, `ht<TAB>tp://host/x`
   *        WHATWG strips ASCII tab, LF and CR from ANYWHERE in a URL.
   *   `<C0>http://host/x`      leading control characters are stripped.
   *   `http:&#x2F;&#x2F;host/x`  the HTML parser decodes character references
   *        before the URL parser ever sees the value.
   *   `http:host/x`            a scheme with no slashes still names a host when
   *        the base scheme differs.
   *
   * Chasing those with a bigger regex is the same mistake a third time. The
   * repo's own rule is "resolve what the SYSTEM resolved", so:
   *
   *   - `jsdom` parses the emitted HTML. It is already a devDependency and
   *     vitest already runs in the `jsdom` environment, so this costs nothing.
   *     `querySelectorAll` reads the DOM the browser would build, which settles
   *     tag boundaries, attribute quoting, character references, comments,
   *     `<template>`/`<noscript>` and case-insensitivity in one move.
   *   - `new URL(href, BASE).origin` decides off-origin. That is literally the
   *     algorithm the browser uses to pick a host, so it cannot disagree with
   *     the browser about one.
   *
   * `rel` is read via `el.relList`, the DOM's own token list, rather than a
   * split on whitespace.
   *
   * WHAT THIS STILL CANNOT SEE, stated rather than implied:
   *   - a URL assembled at runtime by script, or a stylesheet injected by JS.
   *     Neither is render-blocking for the module entry, which is the defect,
   *     but neither is visible here either.
   *   - a resource fetched by CSS that no emitted stylesheet links.
   */
  const BASE = "https://citevyn.example/";

  const dockerfileText = () =>
    readFileSync(join(frontendRoot, "..", "infra", "docker", "Dockerfile.api"), "utf8");

  /**
   * The `RUN` commands of the Dockerfile's `frontend` stage, with comments and
   * line continuations resolved.
   *
   * Parsed rather than grepped because a whole-file substring check is not a
   * guard: review kept the words "npm run build" in a comment while the stage
   * actually ran `npx vite build --manifest --mode staging`, and the check
   * stayed green.
   */
  function frontendStageOf(text: string): { runCommands: string[] } {
    const lines = text.split("\n");
    const start = lines.findIndex((l) => /^FROM\s+\S+\s+AS\s+frontend\s*$/i.test(l.trim()));
    expect(start, "Dockerfile.api has no `FROM ... AS frontend` stage any more").toBeGreaterThan(-1);
    let end = lines.findIndex((l, i) => i > start && /^FROM\s/i.test(l.trim()));
    if (end === -1) end = lines.length;

    const runCommands: string[] = [];
    let buffer: string | null = null;
    for (const raw of lines.slice(start, end)) {
      const line = raw.replace(/\s+#.*$/, "");
      if (/^\s*#/.test(raw)) continue; // a whole-line comment is not a command
      if (buffer !== null) {
        buffer += " " + line.trim().replace(/\\$/, "").trim();
        if (!/\\\s*$/.test(line)) {
          runCommands.push(buffer.trim());
          buffer = null;
        }
        continue;
      }
      const m = /^\s*RUN\s+(.*)$/i.exec(line);
      if (!m) continue;
      if (/\\\s*$/.test(line)) buffer = m[1].replace(/\\$/, "").trim();
      else runCommands.push(m[1].trim());
    }
    expect(runCommands.length, "parsed no RUN command from the frontend stage").toBeGreaterThan(0);
    return { runCommands };
  }

  /**
   * The emitted HTML, parsed by a REAL HTML parser.
   *
   * `DOMParser` is a global here because `vite.config.ts` runs vitest with
   * `environment: "jsdom"` — so this adds no dependency and no import, and it
   * is the same parser the app's own component tests run against. (`jsdom` is
   * NOT imported directly: it ships no type declarations, so a direct import
   * fails `tsc -b`, which is a required CI job.)
   */
  function domOf(html: string): Document {
    return new DOMParser().parseFromString(html, "text/html");
  }

  /**
   * True when `href` resolves to a different HOST than the page it sits on.
   *
   * The `http:`/`https:` condition is not decoration and is not
   * belt-and-braces — it was a real FALSE POSITIVE this test caught on its
   * first run: a `data:` URL has the opaque origin `"null"`, which is `!==`
   * the base origin, so a bare origin comparison reported every inlined
   * `data:` URI in the emitted CSS as a third-party host. Vite inlines small
   * assets as `data:` by default, so that would have turned this guard red for
   * an innocent change. A `data:`/`blob:`/`about:` URL contacts no host at all,
   * which is the property this guard is actually about.
   */
  function isOffOrigin(href: string): boolean {
    let url: URL;
    try {
      url = new URL(href, BASE);
    } catch {
      // An unparseable URL fetches nothing. Reporting it would be a false red.
      return false;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return false;
    return url.origin !== new URL(BASE).origin;
  }

  /**
   * Every stylesheet the document links, as the DOM sees them.
   *
   * `relList.contains("stylesheet")` is NOT enough: `DOMTokenList.contains` is
   * case-SENSITIVE, while HTML matches `rel` ASCII-case-insensitively — so
   * `rel='STYLESHEET'` is a stylesheet to a browser and was invisible to the
   * first version of this line. Caught by this file's own corpus on its first
   * run, and the same class as the repo's recorded "a dict is case-sensitive,
   * CSP directive names are not" scar. `relList` is still what supplies the
   * TOKENS, so `rel="alternate stylesheet"` and `rel="stylesheet "` are handled
   * by the DOM rather than by a split of my own.
   */
  function linkedStylesheetHrefs(html: string): string[] {
    return [...domOf(html).querySelectorAll("link")]
      .filter((el) => [...el.relList].some((t) => t.toLowerCase() === "stylesheet"))
      .map((el) => el.getAttribute("href") ?? "")
      .filter(Boolean);
  }

  /** Every URL-bearing attribute on every tag that can name a host. */
  function referencedUrls(html: string): string[] {
    const doc = domOf(html);
    const out: string[] = [];
    for (const el of doc.querySelectorAll("link,script,img,iframe,video,audio,source,object,embed,use,form")) {
      for (const name of ["href", "src", "data", "action", "xlink:href"]) {
        const v = el.getAttribute(name);
        if (v) out.push(v);
      }
    }
    return out;
  }

  /**
   * Every URL an emitted stylesheet fetches: `url()` targets and `@import`
   * targets.
   *
   * The `@import` half is parsed as "everything up to the terminating `;`, then
   * any quoted string in it" rather than as `@import\s+"..."`. That is not
   * fussiness — it is a bypass this guard actually had, found by mutation:
   * the source `@import url("https://x.example/a.css");` is MINIFIED by the
   * build to `@import"https://x.example/a.css";`, with no whitespace and no
   * `url()` wrapper. A render-blocking third-party @import shipped in
   * dist/assets/*.css and this guard stayed green. Reading the prelude also
   * covers `@import layer(base) "x";` and `@import url(x) supports(...)`.
   *
   * CSS ESCAPES are not decoded here, and do not need to be: this reads the
   * EMITTED stylesheet, and the build normalises them — measured, a source
   * `url("\/\/x.example/bg.png")` is emitted as `url(//x.example/bg.png)`.
   * The hand-written `public/about.css` is NOT built, so its guard in
   * `backend/tests/test_about_page_tokens.py` does have to decode them.
   */
  function cssRefs(text: string): string[] {
    const raw: string[] = [];
    for (const m of text.matchAll(/url\(\s*("[^"]*"|'[^']*'|[^)]*)\s*\)/gi)) raw.push(m[1]);
    for (const m of text.matchAll(/@import\b([^;]*);/gi)) {
      for (const q of m[1].matchAll(/("[^"]*"|'[^']*')/g)) raw.push(q[1]);
    }
    // Deduplicated: `@import url("x")` is matched by BOTH passes above.
    return [...new Set(raw.map((v) => v.replace(/^["']|["']$/g, "").trim()))];
  }

  it("no stylesheet the emitted HTML links is off-origin", () => {
    const linked = linkedStylesheetHrefs(emittedHtml);
    // PARTNER FIRST. The assertion below counts toward zero and an empty or
    // broken match satisfies it for free. The build always emits exactly one
    // eager stylesheet link, so seeing none means the probe broke.
    expect(
      linked.length,
      "found no <link rel=stylesheet> in the emitted HTML at all — the probe is " +
        "broken, so the off-origin check below would be vacuous",
    ).toBeGreaterThan(0);
    expect(
      linked.filter(isOffOrigin),
      "the emitted dist/index.html render-blocks on a third-party stylesheet. That " +
        "also blocks <script type=module> from EXECUTING, so a slow or blocked host " +
        "leaves the visitor on a blank page (#365). Self-host it.",
    ).toEqual([]);
  });

  it("nor does anything else the emitted HTML fetches before paint", () => {
    // Wider than stylesheets on purpose: `preconnect`/`dns-prefetch` to a font
    // CDN are the fingerprint of the pattern coming back, and an off-origin
    // `preload`/`modulepreload` is a third-party fetch in the critical path
    // even though it is not a stylesheet. `href` covers <link>; `src` covers
    // <script> and <img>.
    //
    // The tag list is wider than `link|script|img` because the name of this
    // test is a claim about the whole page: `iframe`, `video`, `audio`,
    // `source`, `object`, `embed`, SVG `use` and a `form action` are all ways
    // to name a third-party host. Every one of them is blocked by the CSP
    // today (they inherit `default-src 'self'`, and `form-action` is `'self'`),
    // so this is guard coverage rather than live exposure — but a guard whose
    // name overstates what it looks at is the shape this repo keeps getting
    // caught by.
    const refs = referencedUrls(emittedHtml);
    expect(refs.length, "no href/src found at all — the probe is broken").toBeGreaterThan(3);
    expect(
      refs.filter(isOffOrigin),
      "the emitted dist/index.html references a third-party host in <head> (#365)",
    ).toEqual([]);
  });

  it("and the emitted stylesheet pulls nothing off-origin either", () => {
    expect(
      emittedCss.length,
      "resolved no emitted stylesheet from dist/ — the checks below would be vacuous",
    ).toBeGreaterThan(0);
    for (const { file, text } of emittedCss) {
      expect(text.length, `${file} is empty or missing`).toBeGreaterThan(1000);
      const refs = cssRefs(text);
      expect(
        refs.filter(isOffOrigin),
        `${file} loads a third-party URL. An @import is render-blocking like the ` +
          `link that caused #365; a font src puts the files back on someone else's ` +
          `network. Self-host it and keep style-src/font-src at 'self'.`,
      ).toEqual([]);
      // Partner for the emptiness above: the file really does contain url()s,
      // so the filter had something to look at. The self-hosted @font-face
      // rules guarantee at least two.
      expect(refs.length, `${file} contains no url() at all — the filter is vacuous`).toBeGreaterThan(
        1,
      );
    }
  });

  it("the emitted CSS really declares the self-hosted faces, and their files shipped", () => {
    // The checks above are all "nothing bad is present", which deleting
    // `fonts.css` satisfies perfectly. This is the positive half: the faces are
    // declared, they point at this origin, and the bytes are in dist/ where the
    // StaticFiles mount will find them. `test_frontend_assets.py` covers hrefs
    // written in index.html; nothing covered a CSS url() before this.
    const all = emittedCss.map((c) => c.text).join("\n");
    expect(all).toContain("@font-face");
    for (const family of ["Geist", "JetBrains Mono"]) {
      expect(all, `no @font-face declares ${family}`).toContain(family);
    }
    const srcs = [...all.matchAll(/url\(\s*("[^"]*"|'[^']*'|[^)]*)\s*\)/gi)]
      .map((m) => m[1].replace(/^["']|["']$/g, "").trim())
      .filter((u) => u.endsWith(".woff2"));
    expect(srcs.length, "the emitted CSS names no .woff2 file").toBeGreaterThan(1);
    for (const src of srcs) {
      expect(src.startsWith("/"), `${src} is not a root-relative same-origin path`).toBe(true);
      const onDisk = join(distDir, src.replace(/^\//, ""));
      expect(existsSync(onDisk), `${src} is declared but ${onDisk} did not ship`).toBe(true);
    }
  });

  it("the parser and the URL resolver agree with the browser on every known form", () => {
    // Partner for four assertions that all count toward zero, and the record of
    // every form that has ever been demonstrated against this guard.
    //
    // OFF-ORIGIN column verified against the URL parser itself (`new URL(form,
    // base).origin`), not asserted from memory — an earlier revision of this
    // file claimed `https:\host` reaches the host and enshrined that as a test.
    // It does NOT: WHATWG sends a single slash-or-backslash after a special
    // scheme into relative state, which restores the BASE's host. Two or more
    // reach a new authority. That mistake is why this table now names the
    // expected origin instead of just a boolean.
    const BS = String.fromCharCode(92);
    const TAB = String.fromCharCode(9);
    const LF = String.fromCharCode(10);
    const CR = String.fromCharCode(13);
    const C0 = String.fromCharCode(1);
    const EVIL = "fonts.googleapis.com";

    const offOrigin = [
      `https://${EVIL}/css2`,
      `//${EVIL}/css2`,
      `https:${BS}${BS}${EVIL}/css2`,
      `${BS}${BS}${EVIL}/css2`,
      `https:////${EVIL}/css2`,
      `http:${TAB}//${EVIL}/x.css`,
      `http:${LF}//${EVIL}/x.css`,
      `http:${CR}//${EVIL}/x.css`,
      `ht${TAB}tp://${EVIL}/x.css`,
      `${C0}http://${EVIL}/x.css`,
      `/${LF}/${EVIL}/x.css`,
      `http:${EVIL}/x.css`,
    ];
    for (const href of offOrigin) {
      expect(isOffOrigin(href), `not seen as off-origin: ${JSON.stringify(href)}`).toBe(true);
    }

    const sameOrigin = [
      "/fonts/geist-latin.woff2",
      "/assets/index-abc.css",
      "about.css",
      "data:font/woff2;base64,AAA",
      "blob:https://citevyn.example/8f2c",
      "about:blank",
      // ONE backslash. Kept in the must-NOT-report list precisely because a
      // previous revision asserted the opposite.
      `https:${BS}${EVIL}/css2`,
      `${BS}${EVIL}/css2`,
    ];
    for (const href of sameOrigin) {
      expect(isOffOrigin(href), `false positive on: ${JSON.stringify(href)}`).toBe(false);
    }

    // And the HTML shapes. Each is a real tag a browser links a stylesheet
    // from; the first four defeated a `rel="stylesheet"` string match, the
    // fifth defeated `<link\b[^>]*>`, and the sixth defeated quote-parity
    // tokenising for the rest of the document.
    const linkedForms = [
      `<link rel=stylesheet href=https://${EVIL}/css2>`,
      `<link rel="stylesheet " href="https://${EVIL}/css2">`,
      `<link rel="alternate stylesheet" href="//${EVIL}/css2">`,
      `<link rel='STYLESHEET' href='https://${EVIL}/css2'>`,
      `<link rel="stylesheet" title="Brand > Fonts" href="https://${EVIL}/css2">`,
      `<link rel=stylesheet a=x" b="c>d" href="https://${EVIL}/css2">`,
      `<link rel="stylesheet" href="http:&#x2F;&#x2F;${EVIL}/css2">`,
    ];
    for (const html of linkedForms) {
      const hrefs = linkedStylesheetHrefs(html);
      expect(hrefs.length, `not recognised as a stylesheet link: ${html}`).toBe(1);
      expect(hrefs.every(isOffOrigin), `not recognised as off-origin: ${html}`).toBe(true);
    }

    // Not stylesheets, so not reported by the stylesheet reader — but the wider
    // sweep must still see their URLs.
    expect(linkedStylesheetHrefs(`<link rel="preload" as="style" href="//x/a.css">`)).toEqual([]);
    expect(referencedUrls(`<link rel="preload" as="style" href="//x/a.css">`)).toEqual(["//x/a.css"]);
    expect(referencedUrls(`<iframe src="https://x/f"></iframe>`)).toEqual(["https://x/f"]);
    // A real parser reads adjacent tags as two, and reads a commented-out tag
    // as a comment — which is a DELIBERATE change from the regex version. A
    // browser does not fetch a commented tag, so reporting it was a false red.
    expect(linkedStylesheetHrefs(`<link rel="a"><link rel="stylesheet" href="//x/a.css">`)).toEqual([
      "//x/a.css",
    ]);
    expect(
      linkedStylesheetHrefs(`<!-- <link rel="stylesheet" href="//x/a.css"> -->`),
      "a commented-out tag is not fetched, so it must not be reported",
    ).toEqual([]);
  });

  it("and the CSS reader catches every @import form the MINIFIER can produce", () => {
    // The bypass this guard actually had. `@import url("https://x/a.css");` in
    // the source is emitted as `@import"https://x/a.css";` — no whitespace, no
    // `url()` — so an `@import\s+"..."` matcher missed it and a render-blocking
    // third-party @import shipped with the suite green. Found by mutation, not
    // by reading; every line here is a real emitted form.
    const forms = [
      '@import"https://x.example/a.css";', // what the minifier writes
      '@import url("https://x.example/a.css");', // what the source writes
      "@import url(https://x.example/a.css);", // unquoted url()
      "@import 'https://x.example/a.css';", // single-quoted
      '@import layer(base) "https://x.example/a.css";', // URL is not the first token
    ];
    for (const css of forms) {
      expect(cssRefs(css).filter(isOffOrigin), `slips past the CSS reader: ${css}`).toEqual([
        "https://x.example/a.css",
      ]);
    }
    // The other way back to a third party: the font FILE, not the stylesheet.
    expect(cssRefs('@font-face{src:url("https://x.example/f.woff2")}').filter(isOffOrigin)).toEqual([
      "https://x.example/f.woff2",
    ]);
    // Same-origin forms must NOT be reported.
    expect(cssRefs('@font-face{src:url("/fonts/geist-latin.woff2")}').filter(isOffOrigin)).toEqual(
      [],
    );
    expect(cssRefs('@import "/a.css";').filter(isOffOrigin)).toEqual([]);
  });
});
