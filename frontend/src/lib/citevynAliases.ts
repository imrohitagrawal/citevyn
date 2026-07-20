/**
 * CiteVyn name recognition for the OFFLINE/demo path.
 *
 * This is a deliberate mirror of the backend guardrail
 * (`backend/app/guardrails/domain.py`: `_CITEVYN_ALIASES` / `_CITEVYN_RE`).
 * The two exist because the demo/offline path never reaches the backend, so
 * without this the same question is recognized live and refused offline —
 * exactly the divergence tracked on #84 item 4.
 *
 * Any change here MUST be made in `domain.py` too (and vice versa). Three
 * separate pins keep the copy honest rather than trusting the comment:
 *   1. the alias LIST — `citevynAliases.test.ts` pins it here, and
 *      `backend/tests/test_guardrails_domain.py` parses THIS file and pins the
 *      same strings against `_CITEVYN_ALIASES`;
 *   2. the BEHAVIOUR — `citevynAliases.cases.json` is a single shared corpus of
 *      question/expected-match pairs that BOTH suites run, so a structural
 *      rewrite on either side that changes an answer fails on the other side;
 *   3. the refusal copy — see `GENERIC_REFUSAL` in `../data/knowledgeBase.ts`.
 *
 * The regex SOURCE deliberately differs from the Python: see WORD_CHAR (JS's
 * `\w` is ASCII-only) and the no-lookbehind note below. Equivalence is proven by
 * the shared corpus, not asserted by copying characters.
 *
 * WHY the list is only single non-word tokens (and why "site win" is absent):
 * the backend proved over three adversarial review rounds that a phrase built
 * from two ordinary English words ("may the best site win!") cannot be
 * separated from ordinary English by regex context. Live, the two-word forms
 * are handled by an LLM intent check (`backend/app/answer/alias_intent.py`);
 * offline there is no model to ask, so they stay a deliberate miss here.
 */

/**
 * Speech-to-text manglings of "CiteVyn". Mirrors `_CITEVYN_ALIASES`.
 * Entries are regex source, not literals — the separated "* vyn" spellings
 * allow a space, tab or hyphen between the two halves.
 */
export const CITEVYN_ALIASES: readonly string[] = [
  "citevin",
  "citewin",
  "sitevyn",
  "sitevin",
  "sitewin",
  "sightvyn",
  "sightvin",
  "sightwin",
  // "vyn" is not a word in any language a user of this tool is likely to type,
  // so the separated spellings are safe here in a way "* vin" is not.
  "cite[ \\t-]vyn",
  "site[ \\t-]vyn",
  "sight[ \\t-]vyn",
];

// WORD_CHAR is this file's stand-in for Python's `\w`, and it is deliberately
// NOT JavaScript's `\w`.
//
// JavaScript's `\w` and `\b` are ASCII-only — permanently, even under the `u`
// flag — while Python's are Unicode-aware. Written with `\w`, this "mirror"
// fired on four classes of input the backend rejects ("sitewinа" with a Cyrillic
// а, "cafésitewin", "sitewiné", "аsitewin"): JS sees a non-word char adjacent to
// the alias and lets the identifier guard pass, Python sees a word char and
// blocks. That is a false POSITIVE offline — the expensive direction, since a
// match rewrites the user's text and answers it from the CiteVyn docs.
//
// `\p{L}\p{N}_` is the Unicode class Python's `\w` actually means (str.isalnum()
// is true for L*, Nd, Nl and No), so with the `u` flag below the two agree. The
// residual gap is any character Python counts as alphanumeric that is in neither
// \p{L} nor \p{N} — an empty set at every code point the shared corpus covers.
const WORD_CHAR = "\\p{L}\\p{N}_";

// An alias inside a hostname, URL, email, ticket id or filename is an IDENTIFIER
// the user is asking about, not the product name. Symmetric guards, same as the
// backend: an alias may be the trailing segment ("docs.sitewin") or the leading
// one ("sitewin@example.com", "SITEWIN-1234"). Sentence-final "sitewin." still
// matches, because no word character follows the ".".
//
// These apply to the ALIASES ONLY — the canonical spelling keeps an un-guarded
// branch, mirroring the backend, so this cannot narrow behaviour that already
// worked ("is citevyn.com free?").
const IDENTIFIER_CHAR = WORD_CHAR + "./@:=-";

// NO LOOKBEHIND ANYWHERE IN THIS FILE — not even quoted in a comment, because
// the tests assert on the source text and a quoted one would defeat them.
//
// The backend writes its leading guards as a word boundary plus a negative
// lookbehind over the identifier characters. A lookbehind is below this
// project's browser baseline: there is no browserslist, so the floor is Vite 6's
// default build target ("baseline-widely-available" — safari16.0), and Safari
// only gained lookbehind support in 16.4. Worse, the pattern is built with
// `new RegExp` at module load, which esbuild cannot see and so cannot warn about
// or downlevel; on such a browser this file throws a SyntaxError while
// EVALUATING and the whole bundle fails to boot rather than this one matcher
// degrading.
//
// A consumed alternation `(?:^|[^X])` is exactly equivalent to a negative
// lookbehind on X for a boolean test: it accepts at start-of-input or after a
// non-X character, same as the lookbehind. Consuming one leading character
// cannot hide a second occurrence either, because that character is by
// construction NOT a word character, so it can never be the tail of another
// alias. Lookaheads are kept — they are ES3 and universally supported.
//
// The trailing `\b` the backend spells after each alias is subsumed by the AFTER
// guard: it already fails when the very next character is a word character.
const CITEVYN_RE = new RegExp(
  // Branch 1 — the canonical spelling, un-guarded (see IDENTIFIER_CHAR above).
  `(?:^|[^${WORD_CHAR}])citevyn(?![${WORD_CHAR}])` +
    // Branch 2 — the speech-to-text aliases, identifier-guarded on both sides.
    `|(?:^|[^${IDENTIFIER_CHAR}])(?:${CITEVYN_ALIASES.join("|")})(?![${IDENTIFIER_CHAR}]*[${WORD_CHAR}])`,
  // `u` is required for `\p{...}` to be a property escape rather than a literal
  // "p"; without it the classes silently rot into a class matching "p{L}" chars.
  "iu",
);

/**
 * True when `text` names CiteVyn — canonically or via a recognized mangling.
 *
 * Non-global regex on purpose: a `/g/` regex carries `lastIndex` between calls
 * and would answer differently on every other invocation.
 */
export function mentionsCitevyn(text: string): boolean {
  return CITEVYN_RE.test(text);
}
