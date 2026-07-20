/**
 * Drift guard: the offline KB must not contradict the shipped corpus (#178).
 *
 * The offline/demo KB is the fourth place corpus content lives (after the worker
 * sources, the conftest fixture and — until #178 — db/seed). It cannot be
 * derived: it is TypeScript and cannot import Python, and its copy is
 * marketing-length prose, not chunks. So instead this reads the authoritative
 * markdown off disk at test time and fails if a command the KB tells a user to
 * paste is no longer in the doc it cites.
 *
 * Reading backend files from a frontend test is deliberate: the alternative is a
 * fourth hand-maintained copy that nothing checks, which is the bug #178 is
 * about. Nothing here is bundled — vitest runs in node and the import is a
 * `readFileSync`, not a module import.
 *
 * Scope: this catches the corpus LOSING or renaming a command the KB still tells
 * a user to paste. It cannot catch the corpus GAINING content the KB never
 * learned about — the #170 shape — because a KB that says less contradicts
 * nothing. That direction is guarded once, centrally, by the pinned content
 * digests in `backend/tests/corpus_mirror_manifest.json`
 * (`test_corpus_edits_are_reconciled_with_the_downstream_copies`), which names
 * this file as one of the copies to reconcile. Duplicating the digest here would
 * be a fifth copy to keep in lock-step.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { KB, matchKB } from "./knowledgeBase";

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCES = resolve(HERE, "../../../backend/app/worker/sources");

/** Collapse whitespace so a command still matches across a hard line wrap. */
const normalize = (text: string) => text.split(/\s+/).join(" ");

const sourceText = (file: string) =>
  normalize(readFileSync(resolve(SOURCES, file), "utf-8"));

/**
 * A claim must appear as a WHOLE token run, not as a prefix of a longer one.
 * Found by mutation-testing this guard: renaming the corpus command to
 * `@anthropic-ai/cc` still "matched", because the corpus mentions
 * `@anthropic-ai/claude-code@latest` elsewhere and a plain `toContain` found the
 * shorter string inside it. A guard a real drift walks through is worse than no
 * guard, because it reads as coverage.
 */
// Note ``.`` is NOT a continuation char: a command at the end of a sentence is
// followed by a full stop in prose, and treating that as "part of a longer
// token" would reject every claim that ends a sentence.
const CONTINUATION = "[\\w@/-]";
const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const containsToken = (haystack: string, claim: string) =>
  new RegExp(`(?<!${CONTINUATION})${escapeRe(claim)}(?!${CONTINUATION})`).test(haystack);

/**
 * Commands/identifiers each KB entry puts in front of a user, and the shipped
 * doc that has to still contain them. Hand-listed rather than regex-extracted:
 * the KB copy is prose, so an extractor would mostly find false positives.
 */
const CLAIMS: Array<{ key: string; file: string; claims: string[] }> = [
  {
    key: "claude-code-install",
    file: "claude_code.md",
    claims: [
      "curl -fsSL https://claude.ai/install.sh | bash",
      "irm https://claude.ai/install.ps1 | iex",
      "brew install --cask claude-code",
      "winget install Anthropic.ClaudeCode",
      "npm install -g @anthropic-ai/claude-code",
      "claude --version",
      "claude doctor",
    ],
  },
  {
    key: "codex-install",
    file: "codex.md",
    claims: ["npm install -g @openai/codex"],
  },
  {
    key: "codex-flag",
    file: "codex.md",
    claims: ["--model"],
  },
];

describe("offline KB vs the shipped corpus", () => {
  it.each(CLAIMS)("$key only states commands the corpus still contains", ({ key, file, claims }) => {
    const corpus = sourceText(file);
    const answer = normalize(KB[key].a);
    for (const claim of claims) {
      // The KB actually makes the claim...
      expect(containsToken(answer, claim), `${key} no longer states: ${claim}`).toBe(true);
      // ...and the corpus it cites still supports it.
      expect(containsToken(corpus, claim), `${file} no longer contains: ${claim}`).toBe(true);
    }
  });
});

describe("matchKB install routing", () => {
  it("routes a Claude Code install question to the install answer, not Permissions", () => {
    // The #170/#178 regression: this used to fall through to the catch-all
    // `claude` branch and return the Permissions answer, confidently cited.
    const hit = matchKB("How do I install Claude Code?");
    expect(hit).toBe(KB["claude-code-install"]);
    expect(hit.a).not.toMatch(/permission/i);
  });

  it("still routes a Codex install question to Codex", () => {
    expect(matchKB("How do I install the Codex CLI?")).toBe(KB["codex-install"]);
  });

  it("leaves non-install Claude Code questions on the overview answer", () => {
    expect(matchKB("What is Claude Code?")).toBe(KB["claude-code"]);
  });
});
