# CiteVyn UI Design — v2

> **Canonical reference for all front-end work.** Every pixel, token, and
> interaction documented here. Future changes must be recorded in the
> changelog at the bottom.

---

## Table of Contents

1. [Design Tokens](#1-design-tokens)
   - [Color System](#11-color-system)
   - [Typography](#12-typography)
   - [Spacing](#13-spacing)
   - [Radii](#14-radii)
   - [Shadows](#15-shadows)
   - [Motion](#16-motion)
2. [Section-by-Section Spec](#2-section-by-section-spec)
   - [Header](#21-header)
   - [Hero](#22-hero)
   - [Question Ticker](#23-question-ticker)
   - [Sources Strip](#24-sources-strip)
   - [Personas (`#who`)](#25-personas)
   - [How It Works (`#how`)](#26-how-it-works)
   - [Why It's Different (`#why`)](#27-why-its-different)
   - [Interactive Demo (`#demo`)](#28-interactive-demo)
   - [Pricing (`#pricing`)](#29-pricing)
   - [FAQ (`#faq`)](#210-faq)
   - [CTA Banner](#211-cta-banner)
   - [Footer](#212-footer)
3. [Chat View](#3-chat-view)
4. [Interaction Behaviors](#4-interaction-behaviors)
5. [File Structure](#5-file-structure)
6. [Intentional Design Decisions](#6-intentional-design-decisions)
7. [Changelog](#7-changelog)

---

## 1. Design Tokens

All tokens are CSS custom properties defined in `frontend/src/styles/tokens.css`.
Two theme scopes: `[data-theme="light"]` (default) and `[data-theme="dark"]`.
Theme toggling swaps the root `data-theme` attribute with a `.25s` transition
on `background` and `color`.

### 1.1 Color System

#### Light Theme

| Token | Value | Purpose |
|-------|-------|---------|
| `--bg` | `#faf9f6` | Page background, warm off-white |
| `--surface` | `#ffffff` | Cards, primary surfaces |
| `--surface-2` | `#f3f1ea` | Inset panels, header pills, secondary surfaces |
| `--ink` | `#1c1b19` | Primary text, dark buttons |
| `--muted` | `#6b6862` | Secondary text |
| `--faint` | `#9a978f` | Tertiary text, mono labels |
| `--border` | `#e7e3da` | Primary borders |
| `--border-2` | `#dcd7cc` | Secondary, stronger borders |
| `--hl` | `#ffd75e` | Highlighter yellow — brand accent |
| `--hl-soft` | `#fbe9b0` | Soft yellow for refusal badges |
| `--code` | `#efece4` | Code/terminal backgrounds |

#### Dark Theme

| Token | Value |
|-------|-------|
| `--bg` | `#161618` |
| `--surface` | `#1e1e21` |
| `--surface-2` | `#26262b` |
| `--ink` | `#f0efe9` |
| `--muted` | `#a4a19a` |
| `--faint` | `#6f6d67` |
| `--border` | `#323238` |
| `--border-2` | `#3d3d44` |
| `--hl` | `#f6c453` |
| `--hl-soft` | `#3a3320` |
| `--code` | `#2a2a30` |

#### Semantic Colors

| Purpose | Light | Dark |
|---------|-------|------|
| Success | `#1c9a5f` | `#4ade80` |
| Error (invented) | `#c25b4e` / `#b0503f` | `#f87171` |
| Refusal amber | `#b4732a` on `--hl-soft` | `#fbbf24` on `--hl-soft` |

#### Selection

```css
::selection {
  background: var(--hl);
  color: #1c1b19;
}
```

Declared once, in `landing.css`. `reset.css` used to carry a second, dead copy
pointing at `--accent` (see Focus below); it was removed rather than repointed,
so there is one selection rule instead of two that must agree.

#### Focus

```css
/* tokens.css, on bare :root */
--focus-ring: var(--ink);

/* reset.css */
:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}
```

`--focus-ring` is declared **once**, in the `:root, [data-theme="light"]` block.
The `:root` half matches `<html>` whatever its `data-theme` is, and `var(--ink)`
substitutes the value `--ink` has *cascaded to* on that same element — so the
theme flip carries the ring for free and the dark blocks do not repeat it.

**Why `--ink` and not `--border-focus`.** Measured in real Chromium:

| Ring | `--bg` | `--surface` | `--surface-2` | `--code` | `--hl` | `--ink` |
|---|---|---|---|---|---|---|
| `--ink` light `#1c1b19` | 16.35 | 17.21 | 15.23 | 14.58 | 12.40 | **1.00** |
| `--ink` dark `#f0efe9` | 15.69 | 14.43 | 13.07 | 12.38 | 1.41 | **1.00** |
| `--border-focus` light `#ffd75e` | **1.32** | **1.39** | **1.23** | **1.18** | **1.00** | 12.40 |

The yellow `--border-focus` is *worse than the browser's own default ring* in
light mode, which is the trap the `/about` work caught first
(`frontend/public/about.css`). `--border-focus` remains declared in `tokens.css`
and is consumed by nothing.

Read the `--hl` column carefully: `--ink` on `--hl` is **1.41 in dark**, so the
guarantee is "≥ 3:1 on every surface a ring is *drawn over*", not "on every
token". `outline-offset: 2px` puts the ring on the PARENT of the control, and
the two focusable elements that paint `--hl` — `.citation-chip` and `.cta-pill`
— sit on `--surface` and on the re-pointed `.cta-banner` respectively, so `--hl`
is never a ring backdrop.

**Why it is a token and not a literal.** One flat colour is not enough.
`.cta-banner` paints `--ink` as its **background**, and a `--ink` ring measured
**1.00:1** on `.cta-pill` there — invisible on the one surface it is drawn over.
That panel re-declares `--focus-ring: var(--bg)` and the whole subtree inherits
it. Any future inverted surface does the same in one line, without reset.css
knowing anything about it.

**Two deliberate exemptions.** `#hero-input` and `.chat-input` carry
`outline: none` and signal focus through a `:focus-within` border on their box
(`.hero-input-box` / `.composer-box`, both `border-color: var(--ink)`). The
exemption list lives in `tests/focus-ring.spec.ts` and both exempted boxes are
asserted separately, in both directions — adding an `outline: none` anywhere
else turns the sweep red.

The guard is a Playwright spec, not a unit test: vitest runs with `css: false`,
so nothing in the unit suite can observe a computed style, and a guard asserting
the CSS *text* would not have caught #355 either — the declaration was present
the whole time and simply did not compute.

### 1.2 Typography

Fonts are **self-hosted from this origin**, not fetched from Google (#365).
`frontend/public/fonts/` holds the two `latin` woff2 subsets (60,628 B, both SIL
OFL 1.1 — the licence ships beside them as `/fonts/OFL.txt`, which the licence
requires). `frontend/src/styles/fonts.css` declares the `@font-face` rules and is
imported first in `main.tsx`, so they ride in the app's own emitted stylesheet
and add no request of their own. The server-rendered `/about` page cannot import
a content-hashed bundle, so `frontend/public/about.css` carries the same three
rules; `backend/tests/test_about_page_tokens.py` fails if the two copies drift.

This is an availability fix, not a preference. `index.html` used to load them
with a render-blocking `<link rel="stylesheet">` pointing at
`fonts.googleapis.com`, and a render-blocking stylesheet also blocks
`<script type="module">` from EXECUTING — so anyone on a network that cannot
reach Google, and everyone during a Google Fonts slowdown, got a BLANK page
rather than an unstyled one. Measured on the production build: **193–245 ms to
first rendered UI normally, and >45 s (timed out, blank throughout) with that
host stalled**. After the fix the same three conditions are 59–157 ms, flat.
The usual `<link media="print" onload="this.media='all'">` workaround was not
available: the CSP sets `script-src 'self'` with no `'unsafe-inline'`.

Because nothing off-origin is left in either page's critical path, the CSP's
`style-src` and `font-src` are now `'self'` only.

#### Font Stack

- **Headings / Body:** `Geist` (self-hosted, `/fonts/geist-latin.woff2`), one
  variable face covering 400–700
- **Labels / Kickers / URLs / Badges:** `JetBrains Mono` (self-hosted,
  `/fonts/jetbrains-mono-latin.woff2`), weights **400 and 500 only** — those are
  the two `@font-face` rules that exist, and they point at the same file. A
  heavier weight is browser-synthesised, not a real cut. (`landing.css` asks for
  600 and 700 in two places; both are synthesised today and were before this
  change too.)
- **Italic accents:** a serif italic, rendered by the reader's `serif` fallback.
  This line used to name `Newsreader`. **That face has never been loaded** —
  no revision of `frontend/index.html` has ever contained the string, so nothing
  has ever fetched it, and the only mention in the code is `landing.css:801`'s
  `font-family: "Newsreader", serif`, whose first entry has therefore always
  been unavailable. (The check is `git log -S"Newsreader" -- frontend/index.html`,
  which is empty. A `git log -S` across `src/styles/` DOES return one commit —
  the one that added that `font-family` line — so cite the narrower command:
  an earlier draft of this paragraph cited the wider one and misreported what it
  returns.) Checked while fixing #365 rather than assumed; the doc was
  describing an intent, not the build.

**Only the default theme uses these two faces.** The `data-style` skins in
`tokens.css` name `Outfit`, `Inter` and `Reenie Beanie`, none of which is
loaded — #316 removed their font links, and nothing sets `data-style`, so those
skins are unreachable. Reviving one means wiring up its faces first.

#### Type Scale

| Element | Size | Weight | Tracking | Leading |
|---------|------|--------|----------|---------|
| H1 | `clamp(38px, 4.6vw, 62px)` | 700 | `-0.035em` | 1.02 |
| H2 (section) | `clamp(28px, 3.2vw, 40px)` | 700 | `-0.03em` | 1.08 |
| H3 | `19px` | 700 | `-0.015em` | — |
| Body | `15px` | 400 | — | 1.6 |
| Large body | `16–19px` | 400 | — | 1.6 |
| Mono label | `10–12px` | 500–600 | `0.06–0.14em` | — |

#### Mono Labels & Uppercase

```css
.mono-label {
  font-family: "JetBrains Mono", monospace;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--faint);
}
```

### 1.3 Spacing

| Token | Value | Usage |
|-------|-------|-------|
| `--space-1` | `4px` | Tiny gaps |
| `--space-2` | `8px` | Compact gaps |
| `--space-3` | `12px` | Small gaps |
| `--space-4` | `16px` | Default gap |
| `--space-5` | `20px` | Medium gap |
| `--space-6` | `24px` | Section gaps |
| `--space-8` | `32px` | Large gap |
| `--space-10` | `40px` | Extra-large gap |
| `--space-12` | `48px` | Card gaps |
| `--space-16` | `64px` | Section padding top |
| `--space-20` | `80px` | Large section padding |
| `--space-24` | `96px` | XL section padding |

### 1.4 Radii

| Token | Value | Usage |
|-------|-------|-------|
| `--radius-sm` | `4px` | Small elements |
| `--radius-md` | `8px` | Buttons, inputs |
| `--radius-lg` | `12px` | Cards |
| `--radius-xl` | `16–18px` | Large cards |
| `--radius-2xl` | `24px` | Panels, CTA |
| `--radius-full` | `9999px` | Pills, chips |

### 1.5 Shadows

```css
--shadow-sm: 0 1px 2px rgba(28, 27, 25, 0.06);
--shadow-md: 0 8px 24px rgba(28, 27, 25, 0.08);
--shadow-lg: 0 16px 48px rgba(28, 27, 25, 0.12);
--shadow-xl: 0 24px 64px rgba(28, 27, 25, 0.16);
```

Card hover shadow: `0 16px 36px -24px rgba(0, 0, 0, 0.35)` (inline, not tokenized).

### 1.6 Motion

| Token | Value |
|-------|-------|
| `--duration-fast` | `150ms` |
| `--duration-base` | `300ms` |
| `--duration-slow` | `480ms` |

Easing: `cubic-bezier(0.4, 0, 0.2, 1)` (standard Material-like ease).
Hover states: `transition: transform 0.18s ease, box-shadow 0.18s ease`.

---

## 2. Section-by-Section Spec

### 2.1 Header

**Structure:** `header.header > div.header-container`

**Sticky** at `top: 0`, `z-index: 50`, with `backdrop-filter: blur(12px)` and
`background: color-mix(in srgb, var(--bg) 82%, transparent)`. Height: `64px`.

**Left:** Logo "CiteVyn" with yellow `01` superscript badge
(`JetBrains Mono` 9px, background `--hl`, border-radius 4px).

**Center:** `nav.nav` with links:
- Who it's for (`#who`)
- How it works (`#how`)
- Demo (`#demo`)
- Pricing (`#pricing`)
- FAQ (`#faq`)

Each link has `padding: 8px 12px`, border-radius 8px, and on hover gets
`background: var(--surface-2)` with `color: var(--ink)`.

**Right:** Two buttons:
1. **Theme toggle** — pill-shaped (`border-radius: 999px`), height `36px`.
   Label is the mode it switches TO:
   - Light mode → shows `☾ DARK`
   - Dark mode → shows `☀ LIGHT`
   On hover: `border-color: var(--ink)`, `color: var(--ink)`.

2. **"Try the demo"** — solid ink button (`background: var(--ink)`,
   `color: var(--bg)`, `border-radius: 999px`). Opens chat view.

### 2.2 Hero

**Layout:** Two-column grid (`1.06fr / 0.94fr`, gap `56px`) inside a
`max-width: 1160px` container with `padding: 52px 28px 36px`.

**Left column:**

1. **Status badge:** Green dot + `CITED ANSWERS FOR AI DEV TOOLS` in
   `JetBrains Mono` 11.5px, uppercase, border-radius `999px`.

2. **H1:** `Answers you can [check,] not just believe.`
   - `"check,"` gets the yellow highlighter effect:
     `background: linear-gradient(transparent 60%, var(--hl) 60%)`
   - Size: `clamp(38px, 4.6vw, 62px) / 1.02`, weight 700, letter-spacing `-0.035em`

3. **Body copy:** 19px, line-height 1.6, color `var(--muted)`, max-width 540px.
   Italic emphasis: `"straight from the makers' own guides"` in `Newsreader`
   italic.

4. **Input box:** Rounded 12px, border `1px solid var(--border-2)`,
   contains:
   - `›` glyph in faint mono
   - Text input (flex: 1)
   - `/` shortcut badge (clickable, monospace 11px, border-radius 6px)
   - "Ask →" button (ink background, border-radius 9px)
   - Focus: border color → `--ink`
   - Shake on empty submit: `animation: cv-shake 0.4s ease`,
     border color → amber `#b4732a`
   - Inline warning: amber text, 13px, fades up in `.25s`

5. **"TRY:" chips:** Row of 3 pill buttons. On hover, border → `--ink`,
   text → `--ink`.

**Right column — Hero auto-play answer card:**

- macOS-style window: background `--surface`, border `--border-2`,
  border-radius 18px, shadow `0 24px 60px -30px rgba(0,0,0,0.28)`
- Header: traffic light dots (red `#e06c5a`, yellow `#e0b44a`, green `#5bab6b`),
  title "CiteVyn — live session", "AUTO" badge with green dot
- Body: `min-height: 322px`, flex column
- Shows Q/A format with avatar circles
- Answer streams word-by-word (~26ms/word) with blinking caret
- Sources fade up after streaming completes (`opacity/translateY 8px, .4s`)
- Progress dots at bottom; active dot stretches to 22px pill
- Cycles 3 canned Q&As, pauses 4.6s between each

**Auto-play sequence:**
1. Load → start first Q&A
2. Stream answer word-by-word (~26ms/word)
3. When done, show sources with fade-up animation
4. Wait 4.6s
5. Advance to next Q&A (loop)

### 2.3 Question Ticker

**Full-bleed strip** with `border-top` and `border-bottom` (1px `--border`).
Background `--surface`.

- Infinite marquee: `translateX(0 → -50%)`, 60s linear, duplicated list
- Contains 8 pill buttons, each with a yellow mono tag (USAGE / SETUP /
  PRICING / HONESTY / HOW-TO / COMPARE / OUT OF SCOPE / EXACT LOOKUP)
  + question text
- Hover pauses animation (`animation-play-state: paused`)
- 80px gradient fade masks on both edges (using `::before`/`::after`
  pseudo-elements)

### 2.4 Sources Strip

**Layout:** Horizontal strip with `border-bottom: 1px solid --border`.
Container: `max-width: 1160px`, `padding: 22px 28px 8px`.

- Left: Mono label "GROUNDED IN OFFICIAL DOCUMENTATION FROM"
- Right: 4 tool items, each with:
  - 28px badge (border-radius 7px, `JetBrains Mono` 10px)
    containing `CL`, `CC`, `CX`, or `GM`
  - Tool name (15px, weight 600)

**Note:** Monograms are placeholders — swap in licensed product logos when
available. No images are used in the current implementation.

### 2.5 Personas (`#who`)

**ID:** `id="who"`, with `scroll-margin-top: 76px` (header offset).

- Kicker + H2: "Whoever you are, [just ask.]" — "just ask." in Newsreader italic
- 3 persona cards in a 3-column grid (gap 20px):
  1. **JUST CURIOUS** — "Exploring AI tools"
  2. **SHIPPING DAILY** — "Building with these tools"
  3. **CHOOSING FOR A TEAM** — "Evaluating & deciding"

Each card:
- Border `1px solid --border`, border-radius 16px, padding 24px
- Mono tag (10.5px, letter-spacing 0.1em)
- Title (19px, weight 700)
- Body (14px, line-height 1.6, min-height 66px)
- Dashed divider (`border-top: 1px dashed --border`)
- "ASK THIS ↓" label + 2 question buttons
- Hover: `translateY(-3px)` + `box-shadow: 0 16px 36px -24px rgba(0,0,0,0.35)`,
  transition `0.18s ease`

### 2.6 How It Works (`#how`)

**ID:** `id="how"`, `scroll-margin-top: 76px`.

- 3 step cards in a 3-column grid
- Each card has:
  1. **Illustrative inset panel** (min-height 180px, `--surface-2` bg,
     border-radius 12px) showing the step visually
  2. **Step number** (01/02/03) in `JetBrains Mono` 24px, weight 700,
     color `--faint`
  3. **Title** (18px, weight 700)
  4. **Description** (14px, line-height 1.6)

**Step 1 panel:** Typing preview with blinking cursor + tool chips (Claude,
Codex, Gemini pills).

**Step 2 panel:** Document preview with skeleton lines, highlighted
"Use --model to pick a model per run." in yellow, "Found the exact part ✓"
checkmark.

**Step 3 panel:** Quoted answer with "From the official guide ✓" badge,
plus "Not covered? It says so — no guessing." line.

### 2.7 Why It's Different (`#why`)

**ID:** `id="why"`, `scroll-margin-top: 76px`.

- Centered H2: "Built to say [I don't know.]" — highlighted phrase
- Side-by-side comparison (2 columns, gap 20px):

**Left — "A generic chatbot":**
- Dashed avatar border
- "0 SOURCES" badge (red, `#b0503f`)
- Answer with red dotted underlines on invented claims
- Footer: `✗ Sounds right. Isn't. And there's nothing to check.`

**Right — "CiteVyn":**
- Ink border + shadow (`0 24px 50px -30px rgba(0,0,0,0.35)`)
- "1 SOURCE" badge (yellow, `--hl`)
- Answer with yellow highlighter on the correct claim + superscript citation chip
- Source card inline (numbered, with title + URL)
- Footer: `✓ One claim, one source — open it and check for yourself.`

**Stats row:** 3-column grid, border-radius 18px:
- ≥95% citation correctness
- 100% guardrail
- ≥95% retrieval hit rate

**Feature cards:** 4-column grid:
- Citation on every claim
- Refuses out-of-scope
- Exact lookup
- Clean follow-ups

### 2.8 Interactive Demo (`#demo`)

**ID:** `id="demo"`, `scroll-margin-top: 76px`.

- Outer wrapper: `--surface-2` background, border-radius 24px, padding 8px
- Inner panel: border-radius 18px, background `--surface`, border `--border`
- Two-column grid (`300px / 1fr`):

**Left rail (300px):**
- `--surface-2` background, border-right
- "Live demo" kicker + "Ask a question." H3 + description
- 4 selectable question buttons (each with title + mono tag)
- Active state: `--ink` border + shadow
- Click selects and streams the answer. Picking a second question **stops** the
  first stream (#353) — without that both wrote the same slot, the paragraph
  flipped identity ~83x/second, and the longer answer could win outright and be
  left sitting under the other question's header.
- Leaving the landing screen stops the stream and **snaps the answer to its
  complete text** (#329). It does not freeze where it was and does not replay
  from the start: freezing leaves a truncated paragraph under a caret blinking
  `infinite` (measured stuck at 40 of 298 characters, permanently), and
  replaying costs a full 3.6s re-stream that runs entirely below the fold. The
  snapped state is the same shape a first-time visitor already sees.

**Right stage:**
- Question row: avatar + question text, dashed border separator
- Answer area: bot avatar + streaming text + sources
- Refusal questions: amber `⚠ NO SOURCE — REFUSED` badge
- Completed answers: SOURCES list + "Continue in full chat →" button

### 2.9 Pricing (`#pricing`)

**ID:** `id="pricing"`, `scroll-margin-top: 76px`.

- 3 tiers in a row (gap 20px):
  1. **Demo** — $0/forever, outlined CTA
  2. **Pro** — $12/month, **featured** (ink border, yellow top bar,
     "POPULAR" badge, filled CTA). Clicking "Get Pro" opens chat with a
     specific Pro-related question.
  3. **Enterprise** — Custom, outlined CTA (intentionally no-op in demo)

Each tier card:
- Title, price (38px bold), unit, description
- CTA button (44px height, border-radius 10px)
- Feature list with ✓ checkmarks

### 2.10 FAQ (`#faq`)

**ID:** `id="faq"`, `scroll-margin-top: 76px`.
Container: `max-width: 820px`.

- 6 items, accordion style
- Top border only (`border-top: 1px solid --border`)
- Each item: `border-bottom: 1px solid --border`
- Toggle: `+` / `−` in `JetBrains Mono` 20px
- One item open at a time (first open by default)
- Answer: `padding: 0 40px 22px 4px`

### 2.11 CTA Banner

- Inverted panel: `background: var(--ink)`, border-radius 24px
  (intentionally dark in light mode; inverts to light in dark mode)
- H2: "Stop guessing. Start [citing.]" — highlighted word
- Subtext: 16px, muted (uses `color-mix(in srgb, var(--bg) 72%, transparent)`)
- Yellow pill button "Ask your first question →"
  - On hover: `transform: scale(1.04)`
- Footnote: `JetBrains Mono` 11.5px, `NO ACCOUNT · NO SETUP · FIRST ANSWER IN SECONDS`

### 2.12 Footer

- `border-top: 1px solid --border`
- Three columns (flex-wrap):
  1. Logo "CiteVyn" with `01` badge
  2. "MVP" pill + "Claude · Claude Code · Codex · Gemini"
  3. "© 2026 CiteVyn. Answers from official docs only."

---

## 3. Chat View

**Container:** `[data-screen-label="Chat"]` — max-width 820px, fills viewport
below the 64px header.

**Top bar:**
- `← Back to landing` pill button (returns to landing + scrolls to top)
- Mono disclaimer: "DEMO — canned responses"

**Message list:**
- Flex column, gap 22px, `overflow-y: auto`
- Auto-scrolls to newest message
- Pinned to bottom while streaming

**Empty state:**
- "CV" logo mark (44×44px, dark bg, light text, border-radius 12px)
- Heading: "Ask about your AI tools"
- 4 suggestion buttons

**Messages:**
- **User:** right-aligned (max-width 78%), ink background,
  border-radius 16/16/4/16
- **Bot:** left-aligned (max-width 88%), with CV avatar, bordered bubble
  (border-radius 16/16/16/4)
- Streaming: blinking caret (8×16px ink block, `cv-blink 1s steps(1) infinite`)
- Sources: rendered only after completion, fade-up animation
- Refusals: amber `⚠ NO SOURCE — REFUSED` badge
- **Waiting (live only):** a bot bubble holding three pulsing dots
  (`cv-pending`) and the label "Searching the docs…", inside
  `role="status"` in an `aria-live="polite"` region, so a screen reader is
  told the question was accepted. It scrolls with the rest of the list.
  It is shown while ANY request is in flight — the state is a count, not a
  flag, so a second unanswered question keeps it up after the first answer
  lands. It hands over to the answer bubble in a single batched render (they
  never both show, and there is never a frame showing neither). It renders
  **regardless of whether the transcript is empty**: it is the only thing that
  explains why the send button is refusing, so it has to be present whenever
  there is something to explain.
  Its label uses `--muted`, not `--faint` (5.27:1 / 7.01:1 rather than
  2.77:1 / 3.49:1) — the same AA failure and the same fix #303 accepted for
  the citation legend.

**Answer formatting (#303).** Answers render a deliberately tiny markdown
subset — `**bold**`, `` `inline code` ``, and `- ` bullet lines — parsed by
`frontend/src/lib/answerFormat.ts`. Everything else (headings, tables, links,
images, raw HTML) shows as literal text. The parser returns DATA, never an HTML
string, so React escapes every value; there is no sanitiser to bypass. The
system prompt constrains the model to the same subset.

**Citation chips (#303).**
- Each `[n]` in the prose renders as a small chip linking to its source (new
  tab, `title` = source name). The brackets are real characters, so copied text
  keeps the plain `[n]` form.
- A marker with no matching source renders as plain `[n]`, never a dead link.
- Source cards are collapsed **per document**: one card per URL, its badge
  listing every marker it backs (`1, 3`).
- Clicking a chip highlights its card; hovering a card highlights every chip
  occurrence it backs.
- A one-time-per-session legend — "Numbers link each sentence to its source
  below." — sits under the first answer that renders chips. Demo answers carry
  sources but no markers, so they show no chips and no legend.
- The reader's own question renders verbatim: no markdown, no chips.

**Composer:**
- Input + send button (ink, 44×44px, border-radius 10px)
- Disclaimer: `JetBrains Mono` 11px, "DEMO — canned responses"
- Takes focus when the chat screen mounts (`preventScroll: true`, so it cannot
  fight the duplicate-question scroll), so the chat opens ready to type
- **While a live answer is in flight** (#62) the send button is `aria-disabled`
  and dimmed to 0.7 opacity with `cursor: default` (both matching `AuthModal`'s
  submit button, which is the same "busy for one request" state); the submit is
  refused for both routes (Enter and click). Live path only — demo answers are
  instant and never in flight, so the composer is never gated there, and a
  demo-mode test asserts that negative.
- A question asked from a **landing entry point** while an answer is in flight
  is **parked in the composer**: the reader lands in the chat, sees the loader,
  and finds their question waiting to send.
  - A draft already in the box is the reader's own and is never overwritten —
    and in that one case the clicked question **is dropped**. Stated plainly
    rather than called "never dropped": it is the recoverable half of the
    trade, because the rail pill or chip that produced it is still one click
    away on the screen the reader came from, whereas typed text once
    overwritten is gone. A toast saying so measured +62 B gzip against 66 B of
    remaining budget; recorded in #356 instead of spent.
  - The check is made both when the question is submitted **and again inside
    the 60 ms hand-over delay**, because the in-flight count is only raised
    once the request actually starts.
  - **Focus does not move, and the input is never disabled.** `aria-disabled`
    rather than `disabled` because the state flips for the ~1s of a request,
    while the reader is most likely focused on that very button (they just
    clicked it): `disabled` would drop focus to `<body>` and the restore a
    second later would put it nowhere. The reader keeps typing the next
    question while they wait.
  - The refusal happens **before** the composer is cleared, so a type-ahead
    question is held, not eaten.
  - The reason is announced by a **persistent** `role="status"` region beside
    the composer, empty when idle. The visible loader is not a live region: a
    region created together with its own text is unreliable across screen
    readers, it sat inside the scrolling list where a reader who had scrolled up
    could not see it, and the bot avatar's literal "CV" was inside it and got
    announced too.
  - **The refusal itself is announced, not only the state** (#356 gap 2). The
    same region says "Not sent — CiteVyn is still answering your last question.
    Your text is kept." It takes the region over rather than living in a second
    one: the copy is a superset of the in-flight sentence, so nothing is lost,
    and there is no restore timer to get wrong — the window closes on its own
    when the answer lands and the region hands back to the arrival
    announcement. The signal is a counter bumped at the `inFlight` **ref**, in
    the hook, never derived from the rendered `pending`: `pending` is a render
    behind the ref by construction, so it would announce refusals that never
    happened and miss ones that did.
  - **Stated limitation:** a *second* refusal inside the *same* in-flight window
    is silent. `role="status"` announces on a text CHANGE, the state has not
    changed, and the reader has already been told. A refusal in a **later**
    window does announce, because the region returns to empty in between.
  - An EMPTY submit is never announced. Enter on an empty box is not a refused
    question, so the empty check runs ahead of the gate — otherwise the reader
    would be told "your text is kept" about a box with no text.
  - The send button's handler is wired **unconditionally**. `aria-disabled` and
    the 0.7 dim carry the state; the hook is the single gate. Wiring it
    conditionally (`onClick={pending ? undefined : onSendClick}`) made the click
    path unobservable — it never reached the hook, so nothing could announce it
    — and made the view a second, less accurate gate keyed on `pending`.

---

## 4. Interaction Behaviors

### Theme Toggle
- Action-labeled: shows the mode it switches TO (never current state)
- Stores preference in `localStorage` under `citevyn:theme`
- Falls back to `prefers-color-scheme: dark` media query
- Transitions: `background 0.25s ease, color 0.25s ease`

### View Switching
- Client-side state: `view` = `"landing" | "chat"`
- No routing required
- "Try the demo" button, hero input "Ask →", and any question entry
  transitions to chat view
- Five landing entry points submit a question: the hero box, the hero TRY chips,
  the ticker/marquee pills, the "Who it's for" persona buttons, and Pricing's
  "Get Pro". (The "Live demo" chips do NOT enter chat — they swap the in-place
  demo answer.)
- Entering chat hands over to the composer: anything still typed in the hero box
  moves into the chat input, and the hero box is cleared. A hero SUBMIT carries
  nothing across, since its text became the question.
- Chat's "Back to landing" returns to landing and scrolls to top

### Nav Anchors
- Links smooth-scroll to sections by ID
- Work from both views (landing + chat)
- Offset: `-72px` for the sticky header (implemented via `scroll-margin-top: 76px` on sections)

### Hero Input (`/` shortcut)
- Pressing `/` on the landing screen focuses the hero input
- Ignored when another input already has focus
- Ignored while a modal dialog is open (#331) — the shortcut is a `window`
  listener the dialog's focus trap never sees, so without this it focused the
  hero input *behind* the backdrop
- On the chat screen there is no `hero-input`, so the handler focuses nothing —
  and, since #356, calls no `preventDefault()` either. It used to consume the
  keystroke and do nothing with it
- The `/` badge is clickable as a fallback
- Enter or "Ask →" button enters chat with typed question
- Empty submit: input shakes, border turns amber, 3s inline warning appears

### Hero Auto-Demo
- Cycles 3 canned Q&As on load
- Answers stream word-by-word (~26ms/word)
- Sources render only after completion with fade-up animation
- Pauses 4.6s between Q&As
- Progress dots at bottom; active dot stretches to 22px pill

### Question Ticker
- Infinite marquee (translateX 0→−50%, 60s linear, duplicated list)
- Pauses on hover
- Clicking any chip enters chat with that question

### Chat Streaming
- Split answer on whitespace, reveal one token per ~26ms interval
- Blinking caret (`steps(1)` 1s blink) while streaming
- Sources render only after completion with fade-up animation
- Auto-scrolls to newest message, stays pinned to bottom while streaming

### Duplicate-Question Guard
- Re-asking a question already answered in the session does NOT create a new entry
- Chat smooth-scrolls back to the original user bubble
- Pulses a yellow ring around it 3 times (~1.7s)
- The scroll is owned by `ChatView` (a layout effect keyed on the highlighted
  bubble's `domId`), not by the landing hook, so it works when a LANDING entry
  point re-asks and the chat is only just mounting
- While the highlight is active it HOLDS the list: the stick-to-bottom latch is
  disarmed and cannot re-arm, or the passive re-pin would drag the reader off the
  answer within a frame (#302). Scrolling away still hands control back
- Honours `prefers-reduced-motion`: the jump is instant rather than smooth

### "Get Pro" Flow
- Clicking "Get Pro" (pricing) opens chat
- Adds a user bubble: "What do I get with CiteVyn Pro?"
- Streams an honest bot reply that Pro isn't live yet in the MVP demo
- Repeat clicks dedupe to the existing entry

### "Contact Sales"
- Intentionally a no-op in the demo (Enterprise tier)

---

## 5. File Structure

```
frontend/src/
├── App.tsx                          # Root component, theme state
├── main.tsx                         # Entry, font imports, stylesheet imports
├── types.ts                         # Shared types (View, Message, etc.)
├── data/
│   └── knowledgeBase.ts             # Canned KB, matchKB(), marquee data
├── components/
│   ├── Header.tsx                   # Sticky header with nav + controls
│   ├── Hero.tsx                     # Hero section with input + auto-play card
│   ├── ChatView.tsx                 # Full-screen demo chat
│   ├── landing-sections.tsx         # EAGER sections: Ticker, Sources, InteractiveDemo
│   ├── landing-strip.tsx            # LAZY below-the-fold strip (#358), one chunk
│   └── LandingPage.tsx              # Orchestrator for the landing view
├── lib/
│   ├── useDeferredReveal.ts         # IntersectionObserver gate for the lazy strip
│   └── scrollToSection.ts           # Anchor scroll that waits for a lazy target
├── styles/
│   ├── tokens.css                   # Design tokens (light + dark themes)
│   ├── reset.css                    # Minimal CSS reset
│   └── landing.css                  # Component styles for the landing page

frontend/public/                     # copied verbatim into dist/ (unhashed URLs)
├── about.css                        # Styles for the API-rendered /about page
└── about-theme.js                   # Mirrors the SPA's stored theme onto /about
```

`/about` is the one page the React app does not render. It is served by the API
(`backend/app/services/about_page.py`) from the corpus documents that cite it, so
the two files above are the only way it can be styled: everything under `src/` is
content-hashed by Vite, and the app-wide CSP forbids inline `<style>`/`<script>`.
`backend/tests/test_about_page_tokens.py` fails if `about.css` drifts from
`tokens.css`.

### 5.1 The deferred marketing strip (#358)

The landing sections are split across two modules by WHEN THEY MUST BE ON SCREEN,
not by what they are about. Rollup chunks per module, so the split has to be
physical — `React.lazy` over named exports of one module frees nothing.

| module | sections | loaded |
|---|---|---|
| `landing-sections.tsx` | QuestionTicker, SourcesStrip, InteractiveDemo | eager, in the entry chunk |
| `landing-strip.tsx` | Personas, HowItWorks, WhyDifferent, Pricing, FAQ, CTABanner, Footer | one chunk, on scroll |

Measured on the shipping build (`npm run check:bundle`): the eager graph goes
**66,394 B → 62,873 B gzip (−3,521 B)**, and `frontend/bundle-budget.json` was
ratcheted 68,000 → 64,479 in the same change so the win is not spent silently.

`QuestionTicker` and `SourcesStrip` stay eager because they sit immediately under
the Hero, where a `fallback={null}` boundary is a visible jump on first paint.
`InteractiveDemo` stays eager because it is the product demo, the target of the
header's "Demo" link, and — decisively — it sits BETWEEN the two deferred DOM
runs, so keeping it put is what lets the deferred half be one module mounted at
two points. Measured cost of that choice: **406 B** — two throwaway variants
built back to back before implementation, `InteractiveDemo` deferred 61,878 B vs
`InteractiveDemo` eager 62,284 B. (Neither figure is the shipped one: both
predate the gate itself, which costs a further ~242 B.)

**Three things had to be true for this not to be a regression**, and each has a
test:

1. **The nav still works.** Four of the five header links (`#who`, `#how`,
   `#pricing`, `#faq`) point into the deferred chunk, and `scrollToId` used to
   do `if (!el) return` — a silent no-op that made those links look dead.
   `LandingPage` reveals the strip on the click and `scrollToSection` waits for the
   target to be inserted (a `MutationObserver`, bounded by a 10 s clock — an
   earlier frame counter was reproduced failing at a 3 s chunk delay).
2. **A deep link still lands.** `/#pricing` reveals the strip and scrolls once.
3. **No IntersectionObserver means SHOW, never hide.** `useDeferredReveal` starts
   revealed when the API is absent — jsdom implements none, and neither do old
   browsers. "Cannot observe" must never mean "hide the page".

**An honest limit, measured, not assumed. On desktop this change makes first-paint
JS BIGGER.** The lead margin is 400 px and the sentinel sits at 854 px on Desktop
Chrome 1280×720 — inside `720 + 400` — so a desktop reader's observer fires on
first paint and the chunk is fetched right away. Adding what that reader actually
downloads:

| | entry | + strip | first-paint total |
|---|---|---|---|
| before | 66,394 | — | **66,394 B** |
| after | 62,873 | 4,721 | **67,594 B (+1,200 B)** |

So on desktop the 3,521 B is a *reallocation*, not a saving: the entry chunk
parses and paints sooner and the strip arrives on a second, non-blocking request,
but the byte total moves the wrong way by 1,200 B. The real byte win is on
phone-sized viewports, where the sentinel is at 1458 px — outside `844 + 400` —
and a reader who does not scroll never fetches the 4,721 B at all. That is the
right place to want it, and it is the trade this change makes deliberately.

`frontend/tests/lazy-strip.spec.ts` pins the phone side as an assertion (the
sentinel must start outside the lead margin, and no chunk request may happen
before the scroll) and *records* the desktop geometry as a test attachment
rather than asserting it — asserting `sentinelTop < viewportHeight + 400` would
go red if a taller Hero made desktop start deferring too, which is an
improvement, and a check that fails when the behaviour gets better locks in the
weaker behaviour.

**Accessibility, measured.** Nothing becomes unreachable: any viewport scroll of
~214 px or more mounts the strip, and a real keyboard walk at 390×844 reaches
every deferred control in the right order (45 tab stops, strip mounts at stop 15).
Measured CLS is 0.0001 with the chunk instant and 0.0001 with it delayed 3 s —
the browser's scroll anchoring absorbs the ~5,150 px inserted above the viewport,
and 13 sampled viewport rows are byte-identical across the mount. Two honest
costs remain, both phone-only:

- **Heading navigation can skip.** At 390×844 the DOM holds 2 headings at load
  and 21 once mounted. The 12 inserted by `LandingStripTop` land *above* the
  eager demo's heading, so a virtual cursor already parked on the demo when the
  insertion happens will not announce them on the next "next heading" (shift+H
  recovers). The mitigation, if this is ever judged worse than the bytes, is to
  move the sentinel above `SourcesStrip` so the strip mounts before a cursor can
  reach the insertion point.
- **Find-in-page** cannot match deferred copy until the reader scrolls.

**If a section's chunk never arrives**, `LazyChunkBoundary` renders nothing in its
place and the rest of the page keeps working. Without it a rejected `lazy()` —
a 404 after a deploy swaps the hashed assets, or a dropped connection — unmounts
the whole React root: reproduced as a white screen on the ordinary scroll path.
`Suspense` does not catch that; it handles the pending state, not the rejected one.

**One coupling to know about.** Roughly 30 assertions in `behavior.spec.ts` and
`fidelity.spec.ts` touch deferred sections without scrolling first, and pass only
because the desktop sentinel currently sits inside the lead margin. If the Hero
grows past ~320 px they will red for a reason that does not name #358.

---

## 6. Intentional Design Decisions

### Fixed-Height Hero Card
The hero answer card uses `min-height: 322px` so it never resizes between
answers during the auto-play cycle. This prevents layout shift and keeps
the hero stable.

### Inverted CTA Panel
The CTA banner uses `background: var(--ink)` which is intentionally dark
in light mode. In dark mode, `var(--ink)` becomes `#f0efe9` (near-white),
so it naturally inverts to a light panel. This creates visual impact
without conditional logic.

### CL/CC/CX/GM Monogram Placeholders
The tool badges in the sources strip use text monograms (`CL`, `CC`, `CX`,
`GM`) as placeholders. These should be swapped for licensed product logos
when available. The badges are in separate components for easy replacement.

### Action-Labeled Theme Toggle
The theme toggle always shows the mode it switches TO (e.g., "☾ DARK" in
light mode). This is a deliberate UX choice — the label communicates the
action, not the current state. The glyph changes with theme.

### Knowledge Base as Data Module
The canned responses in `knowledgeBase.ts` are structured as a data module
with `matchKB()` keyword routing. This is intentional — in production, this
module is replaced by the real retrieval API (POST `/v1/sessions/{id}/messages`).

### No Routing Library
View switching (landing ↔ chat) uses React state, not a router. This keeps
the demo self-contained in a single file and avoids routing overhead for
what is fundamentally a two-view marketing page.

---

## 7. Changelog

| Date | Change | Reason |
|------|--------|--------|
| 2026-07-05 | Initial implementation from design handoff v2 | First production implementation of the CiteVyn landing page + demo chat, recreating the `CiteVyn Landing v2.dc.html` prototype in React with the project's established patterns |
| 2026-09-03 | Duplicate-question scroll moved into `ChatView` and made to hold the list; composer focused on chat entry; unsent hero text carried into the composer; reduced-motion honoured | #302 — re-asking an answered question from a landing section left the reader at the newest message. The one-shot scroll was overwritten by the stick-to-bottom re-pin within ~24ms |
| 2026-09-03 | Markdown subset (bold / inline code / `- ` bullets) rendered in answers; `[n]` markers become chips linking to their source; source cards collapsed per document; one-time citation legend | #303 — the live model emitted markdown that showed as literal characters, `[n]` markers were inert and unexplained, and one page cited five times produced five identical cards |
| 2026-09-04 | Dropped the Anton, Satoshi, Outfit and Reenie Beanie font links, and both Fontshare hosts from the CSP | #316 — Anton and Satoshi were referenced by **nothing** in the codebase; Outfit and Reenie Beanie only by `[data-style]` skins that nothing can activate. Three render-blocking stylesheet requests become one, three preconnects become two. **Zero pixels changed** — all 22 visual baselines pass unregenerated |
| 2026-09-04 | Geist is now actually delivered (added to the Google Fonts request as a variable font, `wght@400..700`) | #316 — `--font-sans`/`--font-display` have asked for Geist since the design port, but it was never in any font link, so every heading and paragraph silently fell through to `system-ui`. The design handoff's README specifies Geist explicitly and its own HTML loads it; the React port copied the CSS and dropped the link |
| 2026-09-04 | The account trigger falls back to the linked provider (then a generic word) when no email is stored | #288 — an OAuth account whose provider email was unverified has `email: null`, so `{user.email}` rendered an empty button with no accessible name, on the only control that reaches History and Connected accounts |
| 2026-09-04 | `HistoryDrawer` takes focus when it opens (`tabIndex={-1}` + `dialogRef.current?.focus()`) | #290 — the menuitem that opens it unmounts with the menu, so focus fell to `<body>` and the next Tab walked the page controls *behind* the backdrop, while the menuitem advertised `aria-haspopup="dialog"` |
| 2026-09-04 | Landing hero animation and placeholder rotation now stop while the chat screen is up; `chatView` memoised | #312 — the hero kept dispatching into an unmounted landing DOM, re-rendering the app ~40x/second and handing `ChatView` a fresh `messages` identity each time. Measured 197 `scrollTop` writes in 12 IDLE seconds → **0** (a reviewer independently measured 213 → 0). Returning to the landing page RESTARTS the hero from its first item rather than resuming |
| 2026-09-04 | Stick-to-bottom now verifies the DOM before pinning, instead of trusting a latch that an async `scroll` event disarms | #311 — a streamed chunk landing between a reader's scroll and its scroll event silently erased the scroll and re-armed the latch. Found by the demo Playwright suite on its first CI run; systematic on slower hardware |
| 2026-09-04 | `AccountMenu`'s trigger renamed from `.theme-toggle` to `.account-button` (same styling, now via explicit selector lists) | #311 — it shared a class with the Header's real light/dark toggle, so every `locator(".theme-toggle")` matched two elements and 39 demo Playwright runs died on a strict-mode violation. No visual change |
| 2026-09-05 | Both drawers now trap Tab, via a shared `useFocusTrap` hook extracted from `AuthModal`; the `/` shortcut no longer reaches past an open dialog | #331 — `HistoryDrawer` and `ConnectedAccountsDrawer` set `aria-modal="true"` and put a backdrop over the page, so the mouse could not reach the controls behind them, but the keyboard could: measured in real Chromium, **3 forward Tabs reached `BODY` and then the page nav, and ONE Shift+Tab landed on a page button**. Both files had documented the omission as *"no form fields, so no Tab trap"* — but the hazard is escaping the dialog, not moving between inputs. Review then found the same hazard reached by a **different key**: the global `/` shortcut is a `window` listener the trap never sees, so it focused the hero input *behind* the backdrop and a typed question submitted and navigated the app. Both are fixed and both re-measured in the same browser: Tab cycles inside indefinitely; `/` with a drawer open leaves focus in the dialog and a full typed question never reaches the page (hero input still empty, drawer still open), while `/` with no dialog open still focuses the hero input as before. Only the top-most dialog reacts, so the drawer keeps trapping during the lazy password modal's cold load rather than standing down before its replacement exists. **No visual change** — keyboard behaviour only |
| 2026-09-06 | New `/about` page, served by the API and rendered from the corpus documents that cite it; styled by `frontend/public/about.css` + `about-theme.js` | #84 item 6 — every CiteVyn-about-itself answer cites `/about`, and the frontend renders it as a real `<a href>`. Measured against production: `GET /about` returned the API's **JSON 404 envelope**, so clicking a citation dropped the reader onto `{"status":"error"...}`. The URL is persisted on `documents.source_url`, so changing the corpus would have fixed nothing already indexed without a re-ingest; serving the URL repairs every stored citation with a deploy. The page renders `citevyn.md` **and** `concepts.md` — both carry `source_url: "/about"` — so the link resolves to the source text itself rather than to yet another restatement of it (the copy #84 item 4 tracks as duplicated). Styling had to be an external same-origin stylesheet: the CSP has no `'unsafe-inline'` on `style-src` or `script-src`, measured in real Chromium. Tokens are redeclared in `about.css` (a hashed `tokens.css` cannot be linked by hand) and a pytest guard fails if any value drifts |
| 2026-09-06 | The two `LandingPage.authToast` sign-in tests fake `setInterval` only, and type short credentials | #344 — they were the two slowest of 443 tests (2439 ms / 1099 ms; third-slowest 673 ms) and blew vitest's 5 s per-test timeout under load, in the REQUIRED `type-check + unit tests + build` check. The issue blamed inter-keystroke macrotasks; profiling found that was a third of it. The real cost is `useLandingState`'s hero typewriter — a 24 ms `setInterval` dispatching a React state update for as long as the landing screen is mounted — so the bill is proportional to how long a test LIVES, a feedback loop that explains why only the two long tests degrade and why they pass in isolation. Faking `setTimeout` as well deadlocks the file (Testing Library cannot advance vitest's clock inside `waitFor`; all four assertions hung to the full 5 s even when idle), so only the interval is faked. Typing 38 characters cost 2537 ms in one field under load vs 92 ms for 8. Measured, six INTERLEAVED before/after full-suite pairs on the same machine: before red in 4 of 6, after green in 6 of 6, and the after arm was faster in **12 of 12** paired test timings (e.g. 5097→2620 ms, 5069→2017 ms). **No timeout raised and no assertion relaxed** — both tests still fail if `handleAuthenticated` picks the wrong toast copy, verified by mutation in both directions |
| 2026-09-06 | `useToast` arms auto-dismiss from rendered state and cancels pending timers on unmount | Found while measuring #344, and PRE-EXISTING on `main` (reproduced there in 1 of 6 full-suite runs). The 5 s auto-dismiss `setTimeout` was fire-and-forget, so a tree unmounted inside that window left the callback queued to call `setToasts` on a component that was gone. In the browser that is a wasted wakeup; in a vitest worker it lands after the environment is torn down and **vitest exits 1 while reporting all tests as passed** — the required check going red with a green-looking report. The first fix armed in `addToast` and cleared on unmount; adversarial review MEASURED that this breaks under React StrictMode, which runs effect setup → cleanup → setup on mount: state survives that simulated remount but the cleared timer does not, so a toast added by a mount effect (the `?auth=ok` / `?connect=ok` return trips) kept its state, lost its timer and **sat on screen forever in `npm run dev`** — 2 toasts, 1 timer. Arming from rendered state instead means the next render re-arms anything that lost a timer, and an `addToast` landing after unmount schedules nothing at all because React drops the state update. Four tests, each mutation-proved; the StrictMode one reproduces the reviewer's 2-toasts/1-timer measurement exactly |
| 2026-09-06 | `tsconfig.node.json` emits to `node_modules/.tmp/tsnode`; ten compiled-config `.gitignore` patterns deleted | #343 — `composite: true` FORCES emit, and with no `outDir` `tsc -b` wrote `vite.config.js` beside `vite.config.ts`. Vite's `DEFAULT_CONFIG_FILES` lists `.js` BEFORE `.ts`, so `npm run dev` and `npm run preview` (neither of which runs `tsc -b` first) loaded the compiled copy — and `npm run dev` is what `playwright.config.ts`'s `webServer.command` runs, making the dev server's port, proxy target and live-stub plugin come from a stale file. Cost real diagnosis time in #323. Contrary to the issue text, **Playwright itself is NOT affected** — `playwright/lib/common/index.js:1535` tries `.ts` first — so those compiled files were inert clutter, not a second instance. Guarded in `buildGuards.test.ts` by asking `ts.getOutputFileNames` (the compiler's own emit-path resolution) and then asking VITE which config it resolves; the first draft spawned a real `tsc -b --force` and was rewritten after measurement showed it pushed the suite from 36 s to 55 s and made #344 WORSE. **No visual change** |
| 2026-09-06 | The chat composer refuses a submit while a live answer is in flight; the send button is `aria-disabled` + dimmed, the input stays enabled and focus never moves | #62 — the issue's stated mechanism no longer exists (PR #88 replaced the shared `chatTimer` and last-message targeting; its body has been rewritten). What survived is ATTRIBUTION: a user bubble is appended on submit, a bot bubble when its request RESOLVES, so two questions back to back render `[Q1][Q2][A1][A2]` and the reader credits A1 to Q2. Gating the composer ALONE did not make that unreachable, and the first version of this row claimed it did: four reviewers reproduced it in three clicks — ask, Back, ask again from the hero — and measured the answers arriving [Q1][Q2][A2][A1], worse than described, because the reader credits the SECOND answer to the FIRST question. The landing entry points now PARK the question in the composer instead of racing one in flight. The FIRST version of that fix did not close the path either: it read the in-flight count synchronously while the send is deferred 60 ms, so two entry points fired inside that window both read zero and both sent — a skeptic measured `asks=2` and the same [Q1][Q2][A2][A1] ordering. The check is now also made inside the deferred callback, which closes it, and two tests pin both the 30 ms-apart and same-tick cases. When the composer already holds a draft the clicked question is dropped rather than clobbering it; that is stated, not glossed. Two narrower paths are recorded rather than claimed closed: a chat suggestion chip on a resumed zero-message transcript, and `resumeSession` mid-flight. The guard is a REF, not `state.pending`: two Enter presses inside one React batch both read the state rendered before either ran, and a state guard passes both (measured: 2 requests, gate on). `aria-disabled` over `disabled` because the flip lands while the reader is focused on that button; Playwright's own actionability honours `aria-disabled`, so the e2e cover must force the click or it silently waits 7s and clicks the re-enabled button. **14 mutants, all killed.** |
| 2026-09-06 | The "Searching the docs…" indicator counts in-flight requests instead of being a flag, and `sendLive` owns its whole lifecycle in one `finally` | #62 — it had TWO owners (`streamBot`'s first chunk, and `sendLive`'s catch), and `streamBot` has no idea which request it belongs to. With two questions overlapping, the first answer's opening chunk cleared the indicator belonging to the second, still-unanswered one. One start, one end: the `finally` also runs after the answer bubble is appended, so React batches them and there is no frame showing neither. `backToLanding`'s reset was REMOVED with it — with the gate reading a ref the reset does not touch, it would have left the loader gone while the gate was still shut and swallowed the next question with nothing on screen explaining why. |
| 2026-09-06 | Picking a second demo-rail question stops the first stream; leaving the landing screen stops the demo stream and snaps the answer complete | #353 + #329 — `selectDemo` overwrote `timers.current.demoTimer` without stopping it, so the replaced handle was unreachable and BOTH streams wrote `state.demo.text`: measured 100 backward length jumps and 197 attribution flips over 2352 ms, and — the winner being whichever stream finishes last, a pure length race — a second click on a shorter answer under `24 × (ticks₁ − ticks₂)` ms settles on the FIRST answer permanently, under the second question's header (on the `laptop` entry, its amber NO SOURCE — REFUSED badge over a fully cited answer). #329 was the same handle missing from #312's screen-gated cleanup: measured 129 `SET_DEMO` dispatches over 3096 ms into an unmounted landing DOM for the 298-char `claude-code` answer (a reviewer re-measuring with the shorter `codex-flag` answer counted 85 ticks — the count is answer-length-specific; both go to 0). Gating alone was measured to be WORSE — stuck at 40 of 298 chars with the caret blinking `infinite` — so the cleanup snaps to the full text, the shape the initial state already has. Real-browser walkthrough after the fix: 0 backward jumps, header and answer agree, 32 → 204 chars on return, 0 carets in `#demo`. |
| 2026-09-06 | `waitStreamDone` (Playwright helper) now waits for the pending bubble to clear as well as the caret | Found by this work: on the live path there is a window between submit and the first chunk in which NO caret exists yet, so the helper returned IMMEDIATELY and its caller typed the next question while the previous one was still in flight. Nothing depended on that until the composer gate did, at which point a live test that "waits for the answer" was shown to have been waiting for nothing. No-op in demo mode, which has no pending bubble. |
| 2026-09-06 | Review round: the request timeout now covers the response BODY, the loader renders on an empty transcript too, landing entry points park instead of racing, and the busy send button moved to 0.7 opacity / `cursor: default` | Six parallel reviewers, 5 findings taken as blocking. **The one CRITICAL_BLOCKER was in `lib/api.ts`, not in the feature:** `clearTimeout` ran between the fetch and `response.text()`, so a server that sends headers and then stalls the body left the promise pending with nothing left to abort it — measured still pending after 120,000 ms of a 50 ms timeout, with a partner proving the same timeout DOES fire while the headers are outstanding. Harmless before (a stuck spinner); once the composer is gated on the request finishing it wedges the composer shut for the session, recoverable only by reloading. Two reviewers disagreed about this and the second was wrong — it had only tested the headers case. The loader was rendering inside `ChatView`'s non-empty branch, so resuming a zero-message session mid-request left the send button `aria-disabled` with nothing on screen saying why. `--faint` -> `--muted` on `.pending-label`: 2.77:1 / 3.49:1 is the identical AA failure, and the identical fix, that #303 already accepted for the citation legend. |
| 2026-09-06 | The in-flight count is now MIRRORED from its ref rather than tracked by parallel deltas, and a persistent `role="status"` region does the announcing | The reducer applied a delta and the ref applied a delta — two sources of truth held in step by convention, and the hook exports `dispatch`, so the convention was already breakable from outside (this change's own test broke it). Assigning the ref's value makes drift structurally impossible, lets a stray dispatch self-heal, and is fewer bytes. The clamp moved onto the REF, which is the half the gate reads: a negative ref is TRUTHY, so clamping only the rendered copy was backwards. It is an unreachable backstop and is the one mutant of 23 that survives — recorded here rather than papered over with a test that cannot fail. The live region is now persistent and empty when idle: one created together with its own text is unreliable across screen readers, it sat inside the scrolling list where a scrolled-up reader could not see it, and the avatar's literal "CV" was inside it and got announced. |
| 2026-09-06 | Deleted a vitest test that could not fail, and pinned the `state.pending > 0` seam | Found by the test-quality reviewer, both are the repo's own recorded traps. The focus test passed with the whole #62 change absent AND with `disabled={pending}` — the exact design its docblock said it rejected — because jsdom does not blur an element when it becomes disabled; the property is real, so it moved to the live browser test. Separately, `pending={false}` and `state.pending > 1` — cutting the seam entirely, and never showing the loader — both survived the full suite, because the only test on that seam asserted an ABSENCE. A positive test now drives a held request and watches the loader appear and clear. The demo-rail sampler's two hidden preconditions (`300 % 24 == 12`, and FIRST longer than SECOND) are asserted rather than assumed: with two incidental constants changed it went green on broken code. |
| 2026-09-06 | Skeptic round: the demo-mode negative control now bites, the landing-entry-point parking closed a 60 ms race, and the AA contrast fix got a guard | An adversarial pass over the FIX diff, because in this repo the last three packages each hid their next defect inside a review fix. It found two. **(1) The demo negative control reported green on the exact mutation its own docblock named** — re-keying the gate onto `messages.some(m => m.streaming)` — because every assertion in it was an auto-retrying `expect` that simply polled until the ~2 s demo stream ended and the gate opened; measured in a real browser at 5.1 s instead of 1.5 s, still passing. It now takes ONE synchronous snapshot with the caret count read in the same `evaluate`, which is the partner that makes the other fields mean "mid-stream". Re-run against that mutant: red. This mattered more than it looks — it is the only cover for #62 on the REQUIRED check, since the live e2e is advisory. **(2) The parking fix left a 60 ms hole:** `enterChat` read the in-flight count synchronously but defers the send 60 ms, and the count only rises when the request starts, so two entry points inside that window both sent — `asks=2`, [Q1][Q2][A2][A1]. Re-checking inside the deferred callback closes it. Also: the focus assertion was testing that an aria-disabled button is focusable, not that focus SURVIVES the flip — it now submits by clicking, which is the actual scenario, and it failed the first time it was written correctly. `.pending-label`'s token change had no guard at all (vitest runs `css: false`, so reverting it left the whole suite green); the live test now resolves `--muted` at runtime and compares. `.pending-dot` moved to `--muted` too — it was left at 2.77:1, the same finding half-fixed. |
| 2026-09-06 | `:focus-visible` draws a real ring again, through a new `--focus-ring` token; the inverted CTA panel re-points it; the hero box gains the `:focus-within` border it already had a transition for; reset.css's dead `::selection` removed | #355 — `reset.css` asked for `var(--accent)`, which is defined ONLY under the three `[data-style]` skins nothing can activate. The var was undefined, the `outline` shorthand was invalid at computed-value time, every longhand became `unset`, `outline-style` computed to `none` — and because an author rule outranks the UA stylesheet, the declaration **removed** Chrome's default ring rather than replacing it. Reproduced in real Chromium on `main` by driving real Tab presses: **24 consecutive keyboard stops in light mode and 17 in dark, every one matching `:focus-visible`, every one `outline-style: none`** (WCAG 2.4.7 AA, site-wide). The naive fix repeats the defect: `--ink` is right on every surface (16.35:1 light / 15.69:1 dark on `--bg`, ≥ 9.35:1 everywhere else) EXCEPT `.cta-banner`, which paints `--ink` as its background — a flat `--ink` ring measured **1.00:1** on `.cta-pill` in both themes, invisible on the one surface it is drawn over. So the ring is a TOKEN: `--focus-ring: var(--ink)` on bare `:root`, re-declared as `var(--bg)` on `.cta-banner`, inherited by the subtree. Declared once, not per-theme: the `:root` half of `:root, [data-theme="light"]` matches `<html>` whatever its theme, and `var()` substitutes the CASCADED `--ink` — verified in a real browser in both themes rather than reasoned about. `--border-focus`, the token that looks like the answer, measures 1.32:1 on `--bg` in light mode, WORSE than the default ring it would replace; it is still consumed by nothing. Two controls stay exempt and are asserted separately in both directions: `#hero-input` and `.chat-input` carry `outline: none` and signal through their box's `:focus-within` border — the hero box had declared `transition: border-color` since the design port for a focus state that was never written, and now has it. Guarded by `tests/focus-ring.spec.ts` in the REQUIRED demo-e2e job, which drives real Tab presses, reads resolved `outline-style`/`outline-color`, composites the actual painted backdrop through a canvas (a naive `rgb()` regex returns null for the header's `color-mix` and would have scored it 0), and computes the ratio. Nothing in it reads a stylesheet — vitest runs `css: false`, so no unit test could ever see this, and a string guard would not have caught it because the declaration was there all along. **11 mutants, all killed** (`frontend/scripts/mutate-focus-ring.sh`), including three META-mutants proving the sweep cannot be made vacuous. **Zero pixels changed at rest** — focus states only |
| 2026-09-06 | An arriving answer is announced, through the persistent `role="status"` region the composer already renders — keyed on the answer bubble finishing, NOT on the request settling; the chat landmark gains an accessible name | #356 gap 1 — measured on the success path, a settling request removes the pending region, inserts the answer bubble and flips `aria-disabled`, none of it in a live region, so a screen-reader user was never told the answer had come back. The error path already announced, via ToastHost's `role="alert"`. Deliberately NOT `aria-live` on `#chat-list`: a live region on the scrolling list announces every mutation in it — the bot avatar's literal "CV", the reader's own echoed question, the refusal badge and the whole source-card list, on every streamed chunk — the trap already documented on the pending bubble. Chromium's AX tree confirms the region carries `live="polite"` and **`atomic=true`** (implicit on `role="status"`), so a full replacement is announced whole and an explicit `aria-atomic` would change nothing. **The first version of this keyed on `pending` and was wrong in three ways at once, all three reproduced independently by three reviewers, one live in a browser.** `sendLive` calls `streamBot()` and then `markInFlight(-1)` in the very next `finally`, so React batches them and `pending` drops in the SAME commit that appends the bubble — and `ADD_MESSAGE` carries `sources: []`, the real citations arriving later at `FINISH_MESSAGE` under `finalSources`. So (1) `last.sources.length` was 0 every time and the " N sources cited" clause was **unreachable in production**, while four unit tests asserted it because they hand-built an already-settled list and drove the prop pair directly; (2) it announced "Answer ready" as the answer STARTED streaming — measured at t=865 ms with three citation chips still typing out; and (3) `pending` is only ever non-zero on the LIVE path, so in demo mode the region said **nothing at all**, which is also why no browser test could see any of it — the REQUIRED Playwright job runs in demo mode. Keying on the bot bubble's `streaming` going true→false fixes all three: it is the moment the answer is readable AND the moment `sources` is populated, on both paths, and it makes the feature coverable end-to-end in the required job against the real hook and a real stream. An `armed` ref means a transcript that was already finished on mount — returning to the chat screen, or a resumed session — is not announced as if it had just arrived. **15 mutants killed** (`frontend/scripts/mutate-a11y-guards.sh`). `<main>` also gained `aria-label="Chat"`: measured `name=""` in the AX tree, on the only landmark of the screen |
| 2026-09-06 | The toast PR #357 cut for 62 bytes is back: clicking a landing question while your own draft sits in the composer now says the question was dropped — and `useToast` collapses identical toasts and caps the stack at three | #356 — the draft outranks the clicked question, correctly, but the click then produced literally nothing a reader could perceive: no DOM change, no announcement, the question simply gone. ToastHost renders `aria-live="polite"` / `role="status"`, so this reaches assistive tech as well as the screen, and it names the dropped question or the reader cannot tell which click did nothing. Restored because the ceiling it was traded against has been re-set deliberately, not because the trade was close: a budget that makes WCAG work the thing you give up is measuring the wrong quantity. Costs **+104 B gzip measured**, not the 62 B the PR body quoted — the message is longer, and this time that is a copy decision rather than a byte decision. **Two defects found in review, both introduced by the first version of this.** (1) `park()` read `state.chatInput` from a closure while running inside a 60 ms timeout, so with three landing entry points fired in that window the second parked question was silently OVERWRITTEN by the third, and — worse — with a draft the reader then SENT inside the window, the toast fired claiming "your unsent text is still in the box" about a box that was by then empty. A false statement is worse than the silence it replaced. The composer value is now mirrored in a ref the way the in-flight count already is, written wherever `SET_CHAT_INPUT` is dispatched and re-synced from rendered state so a future dispatch self-heals. (2) Ten rapid clicks stacked **ten byte-identical cards, a 1,385 px column in a 900 px viewport with four of them above the top of the screen**, unreachable and undismissable, covering the right-hand column for five seconds — `ToastHost` is `position: fixed` with no scroll. `addToast` now collapses an identical toast (re-arming its timer with a fresh id) and caps distinct ones at three. That changed the StrictMode double-invoke from two cards to one, so that guard's partner moved from a count to the toast id, which is the only witness that `addToast` really ran twice |
| 2026-09-06 | `/` no longer swallows the keystroke when there is nothing to focus | #356 gap 3 — `preventDefault()` ran before the lookup, so on the chat screen (where `hero-input` does not exist) the key was consumed and did nothing at all: it reached neither the page nor the reader's own text. Now `preventDefault()` only runs when a target exists. Narrower than the issue states, and worth knowing before writing a test: the handler already bails when `activeElement` is an INPUT, and the composer is focused on mount, so this only bit once focus had left the composer — the Back button, a citation chip, the account button, or `<body>`. The `isModalDialogOpen()` guard from #331 is untouched. Partnered by a test asserting `preventDefault` IS still called on the landing screen, without which deleting the call outright would satisfy the fix and let a bare `/` be typed into the field it just focused. **+5 B gzip measured** |
| 2026-09-06 | The eager-bundle ceiling re-set from 66,000 to 68,000 B, with the reasoning written into `frontend/bundle-budget.json` | Not a raise-because-we-hit-it. 66,000 was never chosen — it was the last raise, and against a 65,931 B graph it left **69 B, 0.10%**, so every frontend change had become a budget negotiation. The cost was real: PR #357 cut an accessibility announcement because it measured +62 B. The graph was audited first, by decoding the build's own sourcemap: it is ONE file (no `manualChunks`, the entry declares no static `imports`), of which **react-dom is 130,102 B raw / ~41.9 kB gzip — 63.6%** — and ALL application code is 59,276 B raw / ~19 kB gzip. Five lazy-loading variants were BUILT and measured, not estimated: ChatView −2,903 B, the whole below-Hero landing strip −4,669 B, AccountMenu −865 B, ToastHost −390 B. **None was taken**, for product reasons rather than byte reasons — ChatView is the primary CTA's target and deferring it needs a prefetch added purely to serve a number; the landing strip IS defensible but the measured variant also moved `QuestionTicker`/`SourcesStrip` from just under the Hero, so the honest version needs the file split, an IntersectionObserver, and its own pass over the 22 visual baselines (tracked, with these numbers, in `docs/BACKLOG.md`); AccountMenu pops in inside the header; ToastHost would add a round-trip to show the error toast that fires when the network is already unhappy. 68,000 leaves 1,763 B (2.7%) against the 66,237 B this PR ships — about five more changes the size of this one, and smaller than any new eagerly-imported surface (the app's existing lazy chunks are 1.2–2.9 kB gzip), which is exactly the step change the gate exists to force a conversation about. The gate is re-proven to bite at the new number: a real oversize build fails (`headroom -3138 B`), a mistyped budget key fails loudly rather than passing silently (#323's actual defect), and the repo's own harness reports **43/43 mutants killed** |
| 2026-09-06 | The marquee TRACK is raised above its own edge fades while focus is inside it, and the focus sweep now judges by RENDERED PIXELS rather than by composited computed colours | Skeptic rounds on the #355 fix, twice. **The product half:** `.ticker-strip::before/::after` are 80 px `linear-gradient(to right, var(--bg), transparent)` fades at `z-index: 1`, and a `.ticker-chip` is a plain non-positioned `<button>` in the tab order, so the fade paints OVER its focus ring. Measured on a NATURAL Tab walk in light theme, nothing forced: **1.85:1 at the first ticker stop**, and **1.47:1** pixel-sampled deeper into the fade, while the guard reported a confident 17.21:1. The first fix put `z-index: 2` on the CHIP and was still wrong — `.ticker-track` carries the scrolling `transform`, which makes it a STACKING CONTEXT, so a chip's z-index is scoped inside the track and can never rise above the strip's fades; one chip still painted at 2.31:1. The rule is on the TRACK, under `:focus-within`, which lifts every chip in it. Focus state only: no layout shift, no stacking context at rest, all 22 visual baselines pass unregenerated. Painted ring after the fix, sampled from a screenshot: **8.96:1**. **The guard half, rewritten from the root:** two rounds of this sweep composited `getComputedStyle` background colours up the ancestor chain, and a skeptic broke each in turn — first a gradient ancestor (`background-color` is transparent, so the walk fell through to the page canvas), then `::before`/`::after` overlays (invisible to `getComputedStyle(n)` entirely), then **six more shapes**: an absolutely-positioned SIBLING over the control, a `transform` moving the control onto a non-ancestor panel, `filter: invert(0.5)`, `backdrop-filter: contrast(0)`, and `opacity` on an ancestor — each producing a **measured 1.00:1** ring the guard passed at 16.35:1. Every one of those fixes closed the demonstrated instance and left the CLASS open, because they keyed on a PROXY. The sweep now screenshots each control focused, blurs it, screenshots again, and diffs: the changed pixels ARE the indicator, whatever draws it, and zero changed pixels means focusing this control changes nothing a sighted keyboard user can perceive. That answers all eight shapes in one measurement, and each is a test. Judging by the STRONGEST pixels rather than their mean is load-bearing and was itself measured: raising the track legitimately un-fades neighbouring chips, and averaging a black ring with a neighbour's yellow tag produced rgb(187,164,94) and a false 2.35:1 verdict. Animations are paused WHERE THEY ARE rather than via Playwright's `animations: "disabled"`, which rewinds them and moved a marquee item between the rect read and the capture; and only the marquee is paused, because a global pause froze the reveal-on-scroll animation at `opacity: 0` and made a whole section report "focusing changes 0 pixels" — a guard inventing the defect it then reported |
| 2026-09-06 | The arrival announcement latches on the streaming bubble's stable `msgId`, not on a boolean "something is unfinished" flag | Skeptic round on the #356 fix — the fix round hid the next defect, as it has in every package so far. The `armed` boolean armed whenever the tail was unfinished, INCLUDING on an empty transcript (`!last`), and `HistoryDrawer` lives on the chat screen, so `resumeSession` fires under a mounted `ChatView` at any time (#62 gated the composer, not the drawer). Reproduced through the real hook: a signed-in reader opens the empty chat screen, picks a 3-day-old conversation from History, and a screen reader is told **"Answer ready. 2 sources cited."** — a false statement to an AT user, the exact harm this feature exists to prevent. Dropping the `!last` arm alone is NOT enough: resuming while the tail is the reader's own in-flight question fires the same way. The same tail-position assumption also meant two answers finishing out of order announced the first twice and the second never. All three are one mistake — position is not identity — so `ChatView` now tracks the set of bubble ids it has WATCHED stream and announces only when one of those finishes. `msgId` is a new prop carrying the hook's monotonic id: `domId` is `cv-msg-${index}` and a resumed transcript re-uses those positions, whereas `resumeSession` assigns fresh ids, so an awaited id can never collide. Four tests, three of them RED against the boolean version (both resume paths and the out-of-order case), with a partner proving an answer arriving AFTER a resume is still announced so the fix is not just silence |
| 2026-09-07 | The two web fonts are self-hosted from this origin, the Google Fonts stylesheet and both preconnects are gone from `index.html` AND from the server-rendered `/about` page, and the CSP's `style-src`/`font-src` are `'self'` only | #365. A render-blocking stylesheet also blocks `<script type="module">` from EXECUTING, and this app is one module entry, so a slow or unreachable `fonts.googleapis.com` left the visitor on a blank page — no spinner, no text, no error. Measured on the PRODUCTION build, three runs each: normally 193/194/245 ms to first rendered UI, **>45 s and blank (timed out) with that host stalled**, 61–82 ms with it hard-blocked (a blocked host fails fast; the STALL is the defect). After the fix the same conditions are 59–157 ms / 60–64 ms / 59–62 ms, and 66–72 ms with the app's OWN font files aborted too — the app mounts and renders in the fallback stack either way. Both `latin` subsets (60,628 B, SIL OFL 1.1, licence shipped at `/fonts/OFL.txt`) are byte-identical to what Google served, so all 22 darwin visual baselines pass **unregenerated** and the eager JS budget is unmoved at 66,394 B — `public/` assets are outside the module graph the gate measures, traced not assumed. `/about` had the identical defect from its own `<link>` and is fixed in the same change; its `about-theme.js` was being delayed by it too. Guarded by the EMITTED artifact (`dist/index.html` and the emitted CSS, built inside the test) plus a real-browser spec that stalls every third party and aborts every font file and requires a mount in under 10 s — both deliberately outside the #364 font stub, because a guard that runs THROUGH the stub cannot see the path the stub hides. 28 mutants, 28 killed plus one documented known-survivor (`frontend/scripts/mutate-font-guards.sh`); one of them found a live bypass in the guard itself — the minifier rewrites `@import url("…")` to `@import"…"`, so a render-blocking third-party `@import` shipped while the check stayed green |
| 2026-09-08 | `frontend/tsconfig.json` now includes `tests`, not the phantom `e2e`; seven pre-existing type errors in the Playwright suite cleared; a guard in `src/test/buildGuards.test.ts` asserts the compiler really loads every spec | #366 — there has never been a `frontend/e2e/`, and tsc ignores an `include` entry that matches nothing SILENTLY: no warning, no error, exit 0. So the required `type-check + unit tests + build` job was green while loading **zero** of the 9 spec files, `helpers.ts` and `fixtures.ts` — measured `npx tsc -p tsconfig.json --noEmit --listFiles \| grep -c "/frontend/tests/"` -> **0 before, 11 after**, with `src/` unchanged at 67. Playwright transpiles with esbuild, which erases types without checking them, so nothing else was looking either. **Not a second tsconfig project:** a referenced project needs `composite: true`, composite FORBIDS `noEmit` (TS6310) and FORCES emit — measured, it wrote 22 `.js`/`.d.ts` files into `frontend/tests/`, and Playwright's default `testMatch` `**/*.@(spec\|test).?(c\|m)[jt]s?(x)` matches `.js`, so every spec would have run twice, half of it from a stale snapshot. That is #343 with a bigger blast radius. One `noEmit` project including both directories collides over nothing: `test.globals` is `false`, jest-dom augments the `vitest` MODULE rather than a global, and the auto-included `@types` set is byte-identical. Of the seven errors, six are dead locals; `helpers.ts:387` returned an object missing `strongPixels`, which was a type gap and not a live defect only because `focus-ring.spec.ts` checks `ratio === null` three lines above the `undefined < 8` that would have failed the sweep OPEN. The guard asks `ts.parseJsonConfigFileContent` which files the config resolves to and compares against the list PLAYWRIGHT selects — a string check for `"tests"` is defeated by `"tests-old"`, by `"tests/helpers.ts"`, by a comment, and by an `exclude`; all four are mutants in `frontend/scripts/mutate-typecheck-guard.sh`. **No visual change** |
| 2026-09-08 | A REFUSED composer submit is announced: the persistent `role="status"` region says "Not sent — CiteVyn is still answering your last question. Your text is kept.", and the send button's handler is wired unconditionally | #356 gap 2, the last one left open on that issue. Measured with a MutationObserver over the whole body, the refusal produced **zero DOM mutations** on both the Enter path and the click path — a screen-reader user pressed Enter and nothing whatsoever happened. The signal is a `refusalTick` counter bumped at the `inFlight` **ref** inside `submitChat`, never derived from the rendered `pending`: `pending` is a render behind the ref by construction (#62), so a `pending`-derived announcement would announce refusals that never happened and miss ones that did — pinned by the ONE-React-batch test. **One region, not two.** The backlog framed the choice as "replace the in-flight sentence and restore it, or add a second region"; this is a third shape and cheaper than both — the refusal copy is a SUPERSET of the in-flight sentence, so nothing is lost while it holds the region, and the window closes on its own when `pending` drops, at which point the arrival announcement takes over. A second `role="status"` in `.composer` would also have broken 29 `getByRole("status")` calls (`getByRole` throws on multiple matches) and the live e2e's `.composer [role='status']` locator, for no gain. **The click path needed a product change:** `onClick={pending ? undefined : onSendClick}` meant a click while gated never reached the hook, so no prop this component has could tell a refused click from no click; wiring it unconditionally also makes the ref the single gate instead of the view second-guessing with a staler copy. **Stated limitation, pinned as a test rather than left as prose:** a second refusal inside the SAME window is silent, because `role="status"` fires on a text CHANGE and nothing changed; a refusal in a LATER window does announce, which its partner test proves. An EMPTY submit is never announced — telling a reader "your text is kept" about an empty box is a false statement, the same class the parked-question toast was fixed for. **+122 B gzip, measured** (62,873 -> 62,995; budget 64,479, headroom 1,484). 7 mutants added to `frontend/scripts/mutate-a11y-guards.sh`, and the live-e2e test reddens when the tick dispatch is removed — verified. **No visual change** |

### Future entries

> **All future UI changes must be recorded here.** Add a new row with the
> date, a brief description of the change, and the reason.

Example:
```
| 2026-07-06 | Added video embed to How It Works step 2 | Marketing requested product demo clip |
```