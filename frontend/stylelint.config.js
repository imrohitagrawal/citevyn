/**
 * Stylelint config for CiteVyn — color guardrail.
 *
 * Primary rule: color-no-hex
 * Every content-facing colour must resolve through a CSS custom property (var(--…)).
 *
 * Box-shadow rgba() values (pure-black base) are exempt since they are visual
 * effects rather than content colours and cannot be meaningfully tokenised.
 */
export default {
  rules: {
    /** No raw hex colours anywhere. */
    "color-no-hex": true,

    /**
     * Disallow rgb() and hsl() in value lists.
     * We only check the value list (not the entire CSS declaration) because
     * color functions in var() references are fine.
     */
    "function-disallowed-list": ["rgb", "hsl"],
  },
};
