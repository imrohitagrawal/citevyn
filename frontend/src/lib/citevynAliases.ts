/**
 * CiteVyn name recognition for the OFFLINE/demo path.
 *
 * This is a deliberate mirror of the backend guardrail
 * (`backend/app/guardrails/domain.py`: `_CITEVYN_ALIASES` / `_CITEVYN_RE`).
 * The two exist because the demo/offline path never reaches the backend, so
 * without this the same question is recognized live and refused offline —
 * exactly the divergence tracked on #84 item 4.
 *
 * Any change here MUST be made in `domain.py` too (and vice versa). The lists
 * are kept byte-comparable on purpose: `frontend/src/lib/citevynAliases.test.ts`
 * pins the alias list, and `backend/tests/test_guardrails_domain.py` parses THIS
 * file and pins the same strings, so a one-sided edit fails a test on one side or
 * the other rather than drifting silently.
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

// An alias inside a hostname, URL, email, ticket id or filename is an IDENTIFIER
// the user is asking about, not the product name. Symmetric guards, same as the
// backend: an alias may be the trailing segment ("docs.sitewin") or the leading
// one ("sitewin@example.com", "SITEWIN-1234"). Sentence-final "sitewin." still
// matches, because no word character follows the ".".
//
// These apply to the ALIASES ONLY — the canonical spelling keeps an un-guarded
// `\bcitevyn\b` branch, mirroring the backend, so this cannot narrow behaviour
// that already worked ("is citevyn.com free?").
const IDENTIFIER_GUARD_BEFORE = "(?<![\\w./@:=-])";
const IDENTIFIER_GUARD_AFTER = "(?![\\w./@:=-]*\\w)";

const CITEVYN_RE = new RegExp(
  "\\bcitevyn\\b" +
    "|" +
    IDENTIFIER_GUARD_BEFORE +
    "\\b(?:" +
    CITEVYN_ALIASES.join("|") +
    ")\\b" +
    IDENTIFIER_GUARD_AFTER,
  "i",
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
