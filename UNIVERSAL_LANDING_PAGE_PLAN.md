# Universal Landing Page - Design Plan

> **Status:** Implementation Complete
> **Date:** 2026-07-01
> **Author:** Claude (AI Design Review)

## Executive Summary

Build a new Universal landing page for CiteVyn that appeals to a **global audience** — Engineers, PMs, Marketing experts, and general users. Replace the current UniversalApp with a modern, user-friendly design that emphasizes clarity over technical jargon.

---

## What Was Built

Two complete UI alternatives were implemented in a single component with a theme toggle:

### UI Option 1: Browser-Core Modernism
- DevTools aesthetic with macOS traffic lights, tabs, address bar
- Pattern grid background (#e5e7eb lines every 20px)
- JetBrains Mono for all technical labels
- Cyan (#06B6D4) accent color
- Tabbed navigation: "How it works" | "Try the demo" | "FAQ"

### UI Option 2: Bold Editorial Studio
- Black & white palette with extreme typography contrast
- 12vw display headline with staggered letter animation
- Custom `mix-blend-mode: difference` cursor
- 30s infinite marquee
- Typography-first editorial layout

**Both include:**
- Light/dark theme toggle
- "How It Works" section (3 steps with examples)
- Interactive demo with pre-built questions
- FAQ accordion
- Chat mode with "Back to landing"

---

## Files Created/Modified

| File | Action |
|------|--------|
| `frontend/src/components/UniversalLandingApp.tsx` | **CREATE** — 925 lines |
| `frontend/src/styles/universal-landing.css` | **CREATE** — 1,468 lines |
| `frontend/src/App.tsx` | **UPDATE** — fix showAlternateShell |
| `frontend/playwright.config.ts` | **CREATE** |
| `frontend/tests/landing.spec.ts` | **CREATE** — 399 lines |
| `frontend/UNIVERSAL_LANDING_TESTING.md` | **CREATE** |

---

## Bugs Found & Fixed

1. **showAlternateShell** — Landing page was gated behind `view === "chat"`, causing blank page
2. **Unused `...rest`** — Destructured but never referenced
3. **Unused `useMemo` import** — Removed
4. **`ApiClientError` import** — Changed to `import type`

---

## Verification

- [ ] `http://localhost:3000/?style=landing` renders both UI alternatives
- [ ] Theme toggle (light/dark) works on both UIs
- [ ] "How it works" 3-step section displays
- [ ] Interactive demo with question selector works
- [ ] FAQ accordion opens/closes
- [ ] Playwright tests pass: `npx playwright test tests/landing.spec.ts`

---

## Changelog

| Date | Change | Author |
|------|--------|--------|
| 2026-07-01 | Initial plan created | Claude |
| 2026-07-01 | Both UIs implemented | Claude |
| 2026-07-01 | Playwright test suite added | Claude |
