#!/usr/bin/env python3
"""Check next-session prompts, and keep them out of this public repository.

A next-session prompt holds a session's approvals, spending limits and plans,
so it lives in the owner's private notes
(`$PRIVATE_NOTES_DIR/citevyn/NEXT-SESSION-PROMPT.md`), never here. See
`.claude/skills/citevyn-delivery/SKILL.md`, "End of session".

Two modes:

- `--file PATH` (what `make prompt-check` runs, and the private notes' CI): the prompt
  must exist, have a paragraph that opens with `**Process:**` followed by text,
  and show each level-2 heading in `REQUIRED_SECTIONS`. A heading counts when
  its text is the section name, or starts with it followed by a space, a comma
  or a colon (`## Items, in order` counts as Items). It must also not tell a
  session to copy the owner's own words anywhere but the private notes (see
  `owner_words_lines`).
- `--root DIR` (the default, run in this repository's CI): fails when any
  `.md` file at the top of DIR has "prompt" in its name, in any case. A prompt
  committed here would publish its approvals and plans.

For the sections, only text a reader sees counts. Fenced code blocks (``` or ~~~, also after a
list marker) and HTML comments that start a line are removed first, including
one left open, which hides the rest of the file. This is line-based, not a full
Markdown parser: it guards against a session forgetting a section, not against
someone hiding one on purpose inside raw HTML. Setext headings (text underlined
with `---`) are not recognised; write `## `.

Standard library only. Exit 1 on any problem.
"""

from __future__ import annotations

import argparse
import bisect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The level-2 headings every prompt must show (order is not checked).
REQUIRED_SECTIONS: tuple[str, ...] = (
    "Read first",
    "Items",
    "Approvals",
    "End of session",
)

#: A fence opens at the start of a line, or right after a list marker
#: (`1. ```` or `- ~~~`), since a fence inside a list item still hides what follows.
_FENCE = re.compile(r"^ {0,3}(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)*(`{3,}|~{3,})")
#: A closing fence has no list marker: inside a fence, `- ```` is code.
_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}##[ \t]+(.*?)[ \t]*#*[ \t]*$")
_PROCESS = re.compile(r"^ {0,3}\*\*Process:\*\*[ \t]*(\S.*)?$")

#: Phrases that tell a session to copy the owner's own words. "verbatim" followed
#: by a number cites an entry in the private register ("verbatim 44"), so it is
#: not one.
_OWNER_WORDS = re.compile(
    r"verbatim(?! \d)|word[- ]for[- ]word|exact words|in the owner['’]?s (?:own )?words"
    r"|quote this message|citing this pasted message",
    re.IGNORECASE,
)
#: The sentence must be about the owner's words, not, say, a test's output.
_ABOUT_OWNER = re.compile(
    r"\bowner|\bdecision|\bmessage|\bCHG\b|change[- ]?log", re.IGNORECASE
)
#: The ONE allowed sentence about the owner's words, every part from a fixed set:
#: an optional list marker and "For issue #N,", one verb (record, keep, save,
#: store, write, add, put, copy), at most four plain words, the phrase, at most
#: two plain words, then in/into/to/under a private-notes place, and the sentence
#: ends: "Record the owner's exact words in `citevyn/decisions-verbatim/`."
#: Anything else that mentions copying the owner's words is refused: a list of
#: public places, verbs or objects can never be complete, so the check allows
#: one shape instead of guessing where the words go. A later sentence that
#: points back ("Copy them into DECISIONS.md.") is not caught: the check reads
#: one sentence at a time.
_PRIVATE_PATH = re.compile(r"decisions-verbatim|PRIVATE_NOTES_DIR")
#: A private place: a path whose segment is decisions-verbatim (not under docs/,
#: no #, URL, comma or plus), the private-notes variable, or the words.
_PRIVATE_PLACE = (
    r"(?:[`'\"]?(?!docs/)(?![^\s`'\"]*(?:/docs/|\.\.|://))(?:\$?PRIVATE_NOTES_DIR/|~/|[\w.$-]+/)*"
    r"(?:decisions-verbatim(?:\.md)?|\$?PRIVATE_NOTES_DIR)(?:/[\w.-]*)*[`'\"]?"
    r"|(?:the\s+)?(?:owner['\u2019]s\s+)?private (?:notes|register))"
)
_PLAIN_WORD = r"(?!(?:in|into|to|on|onto|from|with|using|citing|quoting|as|and|or|plus|then)\b)[A-Za-z][A-Za-z'\u2019-]*"
_LEAD = (
    r"(?:\d{1,9}[.)]\s+|[-*+]\s+)?(?:for\s+(?:issue\s+)?#?\d+,\s+)?"
    r"(?:record|keep|save|store|write|add|put|copy)\s+(?:" + _PLAIN_WORD + r"\s+){0,4}?"
)
#: A putting verb: a private path next to one is being moved, not just named.
_SHARE = re.compile(
    r"\b(?:cop(?:y|ies)|paste|attach|move|post|put|include|add|send|share|publish"
    r"|upload|quote|cite|append)\b",
    re.IGNORECASE,
)
#: "never quote this message", "do not record the owner's message verbatim",
#: "you cannot copy ... verbatim", "without quoting it verbatim",
#: "plainly rather than verbatim": a negation up to four words before the phrase,
#: with no punctuation and no "but" in between ("not paraphrased but verbatim"
#: still asks for the words).
_NEGATED = re.compile(
    r"(?:\b(?:never|not|no|cannot|without|nor)|n['’]t|\brather than|\binstead of)"
    r"(?:\s+(?!but\b)[\w'’]+){0,4}\s+$",
    re.IGNORECASE,
)
#: How far back a negation is looked for; bounds the work on a very long sentence.
_NEGATION_REACH = 80
_ALLOWED_SHAPE = re.compile(
    _LEAD
    + r"(?:"
    + _OWNER_WORDS.pattern
    + r")"
    + r"(?:\s+"
    + _PLAIN_WORD
    + r"){0,2}?"
    + r"\s+(?:in|into|to|under)\s+"
    + _PRIVATE_PLACE
    + r"[\s`'\")]*[.;]?\s*$",
    re.IGNORECASE,
)
#: A list item or a heading starts a new block; other lines continue the one above.
_BLOCK_START = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)]|#{1,6})\s")
_SENTENCE_END = re.compile(r"[.;!?](?=\s)")


def visible_lines(text: str) -> list[str]:
    """The lines outside fenced code blocks and HTML comments."""
    text = text.removeprefix("\ufeff")
    # A comment hides text only where Markdown makes one: a line that starts with
    # `<!--` (to its `-->`, or to the end of the file when left open). A `<!--`
    # later in a line, such as one inside a code span, is ordinary text.
    no_comments = re.sub(r"(?m)^ {0,3}<!--(?s:.*?)(?:-->|\Z)", "", text)
    out: list[str] = []
    fence: str | None = None
    # Split on newlines only: splitlines() also splits on U+2028 and friends,
    # which Markdown treats as ordinary characters inside a paragraph.
    for line in no_comments.replace("\r\n", "\n").split("\n"):
        m = _FENCE.match(line)
        # A backtick fence's info string may not contain a backtick, so
        # ```make prompt-check``` on a line is inline code, not a fence.
        if m and m.group(1)[0] == "`" and "`" in line[m.end() :]:
            m = None
        if fence is None:
            if m:
                fence = m.group(1)
                continue
            out.append(line)
        elif (
            (c := _CLOSE.match(line))
            and c.group(1)[0] == fence[0]
            and len(c.group(1)) >= len(fence)
            and not line[c.end() :].strip()
        ):
            fence = None
    return out


def headings(lines: list[str]) -> list[str]:
    return [m.group(1) for line in lines if (m := _HEADING.match(line))]


def shows_section(found: list[str], section: str) -> bool:
    return any(
        h == section or (h.startswith(section) and h[len(section)] in " ,:")
        for h in found
    )


def has_process_line(lines: list[str]) -> bool:
    """A `**Process:**` paragraph with text after the label, on its line or the
    next. A next line that starts a heading or a block quote is not the
    paragraph's text: it ends the paragraph. (A fence line never reaches here:
    visible_lines has already removed it.)"""
    for i, line in enumerate(lines):
        m = _PROCESS.match(line)
        if not m:
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if m.group(1) or (nxt and not nxt.startswith(("#", ">"))):
            return True
    return False


def owner_words_lines(text: str) -> list[int]:
    """Line numbers where a sentence tells a session to copy the owner's own
    words somewhere other than the private notes.

    The owner's words live only in the private notes (the delivery skill's
    "Public repository" section); a prompt that says "record verbatim as the
    next CHG row" sends them into a public change log. A sentence is refused when
    it holds an `_OWNER_WORDS` phrase that is not negated and is about the owner
    (`_ABOUT_OWNER`), unless the whole sentence is the one allowed shape
    (`_ALLOWED_SHAPE`): "Record the owner's exact words in
    `<project>/decisions-verbatim/`." on its own. "Copy the owner's exact words
    from decisions-verbatim into DECISIONS.md", and every other shape, is refused.
    A private path that merely contains "verbatim" is not a phrase, unless the
    sentence also puts it somewhere (`_SHARE`).

    Prompts are hard-wrapped, so wrapped lines are joined into one block and judged
    by sentence: where the wrap falls cannot change the result. The raw text is
    read, fences and comments included, because an agent follows those too. Like the section check,
    this guards against a session's mistake, not against wording chosen to slip
    past it.
    """
    blocks: list[list[tuple[int, str]]] = []
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        if not line.strip():
            blocks.append([])
        elif _BLOCK_START.match(line) or not blocks:
            blocks.append([(number, line)])
            if line.lstrip().startswith("#"):
                # A heading is a block of its own: its words must not exempt
                # the paragraph under it.
                blocks.append([])
        else:
            blocks[-1].append((number, line))
    flagged: list[int] = []
    for block in blocks:
        joined, offsets, numbers = "", [], []
        for number, line in block:
            offsets.append(len(joined))
            numbers.append(number)
            joined += line.strip() + " "
        begin = 0
        for end in [m.end() for m in _SENTENCE_END.finditer(joined)] + [len(joined)]:
            sentence = joined[begin:end]
            if _ABOUT_OWNER.search(sentence):
                if _ALLOWED_SHAPE.match(sentence.strip()):
                    begin = end
                    continue
                private = [m.span() for m in _PRIVATE_PATH.finditer(sentence)]
                shares = _SHARE.search(sentence) is not None
                for m in _OWNER_WORDS.finditer(sentence):
                    in_path = any(a <= m.start() < b for a, b in private)
                    if in_path and not shares:
                        continue  # a private path that merely contains "verbatim"
                    if not _NEGATED.search(
                        sentence[max(0, m.start() - _NEGATION_REACH) : m.start()]
                    ):
                        at = begin + m.start()
                        flagged.append(numbers[bisect.bisect_right(offsets, at) - 1])
                        break
            begin = end
    return sorted(set(flagged))


def file_problems(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = visible_lines(text)
    found: list[str] = []
    if not has_process_line(lines):
        found.append(f"{path.name} has no visible '**Process:** ...' paragraph")
    shown = headings(lines)
    for section in REQUIRED_SECTIONS:
        if not shows_section(shown, section):
            found.append(f"{path.name} has no visible '## {section}' heading")
    found.extend(
        f"{path.name} line {number}: may tell a session to copy the owner's own words; "
        "restate the decision plainly, or use the one allowed sentence on its own: "
        "'Record the owner's exact words in <project>/decisions-verbatim/.' "
        "(about test or tool output? say 'exact output' instead)"
        for number in owner_words_lines(text)
    )
    return found


def root_prompts(root: Path) -> list[str]:
    """Problems for a public repository root: any prompt file there."""
    if not root.is_dir():
        return [f"{root} is not a directory"]
    return [
        f"{p.name} is a prompt file in a public repository; next-session prompts "
        "live in the owner's private notes"
        for p in sorted(root.iterdir())
        if p.is_file() and p.name.lower().endswith(".md") and "prompt" in p.name.lower()
    ]


def prompt_problems(path: Path) -> list[str]:
    """Problems for one prompt file; it must exist (the check cannot pass on
    nothing)."""
    if not path.is_file():
        return [f"no prompt at {path}"]
    return file_problems(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check next-session prompts.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--file", type=Path, help="a prompt file to check")
    mode.add_argument(
        "--root", type=Path, help="a public repository root (default: this repo)"
    )
    args = parser.parse_args(argv)
    if args.file is not None:
        found, label = prompt_problems(args.file), f"Prompt check ({args.file.name})"
    else:
        found, label = (
            root_prompts(args.root or ROOT),
            "No prompt files in the public repo",
        )
    for message in found:
        print(f"  - {message}")
    print(f"{label}: " + ("FAILED" if found else "PASSED"))
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
