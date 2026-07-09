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

### 1.2 Typography

Fonts are loaded from Google Fonts in `main.tsx`.

#### Font Stack

- **Headings / Body:** `Geist` (Google Fonts), weights 400–800
- **Labels / Kickers / URLs / Badges:** `JetBrains Mono`, weights 400–700
- **Italic accents:** `Newsreader` italic (serif), used sparingly

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
- Click selects and streams the answer

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

**Composer:**
- Input + send button (ink, 44×44px, border-radius 10px)
- Disclaimer: `JetBrains Mono` 11px, "DEMO — canned responses"

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
- Chat's "Back to landing" returns to landing and scrolls to top

### Nav Anchors
- Links smooth-scroll to sections by ID
- Work from both views (landing + chat)
- Offset: `-72px` for the sticky header (implemented via `scroll-margin-top: 76px` on sections)

### Hero Input (`/` shortcut)
- Pressing `/` anywhere focuses the hero input
- Ignored when another input already has focus
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
│   ├── landing-sections.tsx         # Ticker, Sources, Personas, HowItWorks, etc.
│   └── LandingPage.tsx              # Orchestrator for the landing view
├── styles/
│   ├── tokens.css                   # Design tokens (light + dark themes)
│   ├── reset.css                    # Minimal CSS reset
│   └── landing.css                  # Component styles for the landing page
```

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

### Future entries

> **All future UI changes must be recorded here.** Add a new row with the
> date, a brief description of the change, and the reason.

Example:
```
| 2026-07-06 | Added video embed to How It Works step 2 | Marketing requested product demo clip |
```