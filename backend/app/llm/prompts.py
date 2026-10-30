"""System prompt used by both the stub and the Anthropic client.

Slice 6 may move this into a template store; for now it lives here as
a single string constant so the citation contract is in one place.

Citation contract:

* Every factual claim MUST be followed by a ``[n]`` marker that
  references an evidence bullet from the user message.
* Markers are 1-indexed and correspond to the order the orchestrator
  listed the evidence.
* When the user message contains no evidence bullets, the model MUST
  refuse with the no-answer paragraph below and emit no markers.
* One marker per bracket: ``[1][2]``, never ``[1, 2]`` (#554). A grouped
  marker that slips through is split by
  :func:`app.llm.validation.expand_grouped_markers` for display, so each
  index gets its marker and source card, but a grouped index never counts
  toward confidence, the uncited-answer gate, the exact-lookup retry or a
  comparison's "does not cover" note: only markers written singly do.
* A sentence saying the evidence does not contain the answer carries no
  marker (#554). This rule is enforced by THIS PROMPT ONLY; no code can tell
  such a sentence from any other. The answer also never says "based on the
  provided evidence", which a judged-eval case checks.

Formatting contract (#303):

* The answer may use ONLY ``**bold**``, ```inline code``` and ``- `` bullet
  lines. The client parses exactly that subset (``frontend/src/lib/answerFormat.ts``)
  and renders anything else as literal text, so unconstrained markdown reaches the
  reader as visible punctuation.
* The trailing "respond with exactly ... and nothing else" clause must stay LAST
  and byte-exact: ``_is_no_answer_refusal`` compares against it and
  ``knowledgeBase.ts``'s ``GENERIC_REFUSAL`` mirrors it.
"""

from __future__ import annotations

# Token-efficient refusal copy. The orchestrator is responsible for
# mapping this to the ``no_answer: true`` response flag.
NO_ANSWER_REFUSAL = "I do not have credible source material in this assistant to answer that."

SYSTEM_PROMPT = (
    "You are CiteVyn, a documentation assistant for Claude, Claude Code, "
    "Codex, and the Gemini API.\n"
    "Answer ONLY using the evidence bullets in the user message. Every factual "
    "claim must be followed by a bracketed citation marker like [1] that "
    "references the matching evidence bullet. Do not invent facts, links, or "
    "commands that are not present in the evidence.\n"
    "Put each citation in its own brackets, like [1][2], never grouped like "
    "[1, 2]. Do not cite a sentence that says the evidence does not contain the "
    'answer. Never write "based on the provided evidence" or similar phrasing.\n'
    "Formatting: write plain prose. The ONLY markup you may use is **bold** for "
    "emphasis, `backticks` for inline code, flags or file names, and lines "
    'beginning with "- " (or "* ") for bullet points. Do not use headings, tables, links, '
    "images, block quotes or code fences — the reader's client renders that subset "
    "only, and anything else appears verbatim as punctuation.\n"
    "If the user message "
    "contains no evidence bullets, respond with exactly: "
    f'"{NO_ANSWER_REFUSAL}" and nothing else.'
)
