# Universal Landing Page Testing Guide

## Overview
The UniversalLandingApp provides two distinct UI alternatives for the CiteVyn application:

1. **Browser-Core Modernism** - Technical DevTools aesthetic with gray/white palette and cyan accents
2. **Bold Editorial Studio** - Typography-first black/white design with motion-driven interactions

## How to Access

1. **In Development Mode**
   - Open the browser to `http://localhost:5173`
   - Click on the "Landing" style option in the style dock at the bottom

2. **Direct URL**
   - Add `?style=landing` to the URL: `http://localhost:5173/?style=landing`

## UI Alternatives

### Option 1: Browser-Core Modernism

**Visual Style:**
- Mimics a browser window with tabs and chrome elements
- Color palette: light gray backgrounds (#f3f4f6), white panels, cyan accents (#06B6D4)
- Typography: Inter for body, JetBrains Mono for technical elements
- Grid pattern background (20px lines)

**Key Features:**
- Browser chrome header with traffic lights and tabs
- Live demo window showing chunk-based Q&A
- Interactive process demo with steps
- How-it-works section with examples
- FAQ section using HTML details elements
- Custom scrollbars (10px width)

**Interactive Elements:**
- Theme toggle (light/dark)
- Demo questions that trigger process animations
- Chunk selection with visual feedback
- Animated transitions on hover states

### Option 2: Bold Editorial Studio

**Visual Style:**
- High-contrast black and white design
- Custom cursor with difference blend mode
- Extreme typography contrasts (12vw headlines)
- Smooth cubic-bezier animations
- Editorial grid layouts

**Key Features:**
- Fixed header with mix-blend-mode difference
- Hero section with staggered character reveals
- Infinite project marquee with asymmetrical cards
- Statistics grid with monospace labels
- Clean FAQ sections
- Dark footer

**Interactive Elements:**
- Custom cursor that scales 2.5x on hover
- Image hover states (grayscale to color)
- Marquee pausing on hover
- Smooth 500ms transitions
- No default browser cursor

## Testing Checklist

### General Features
- [ ] Theme toggle works (light/dark)
- [ ] Navigation tabs switch between sections
- [ ] Both UIs work in light and dark modes
- [ ] Responsive design (mobile/tablet/desktop)

### Browser-Core Modernism Specific
- [ ] Browser chrome header displays correctly
- [ ] Tab switching animations are smooth
- [ ] Demo window shows chunk selections
- [ ] Process demo runs with animation
- [ ] FAQ accordion opens/closes properly
- [ ] Custom scrollbars appear

### Bold Editorial Studio Specific
- [ ] Custom cursor appears and follows mouse
- [ ] Hero text has staggered reveal animation
- [ ] Marquee scrolls continuously
- [ ] Cursor scales on hover over links/buttons
- [ ] No browser cursor visible
- [ ] All animations use cubic-bezier(0.16, 1, 0.3, 1)

### Integration Features
- [ ] Chat interface loads correctly
- [ ] Messages display in selected UI style
- [ ] Back button returns to landing
- [ ] Style dock still accessible

## Troubleshooting

**If styles don't load:**
- Check that the CSS file path is correct
- Verify the component is imported in App.tsx
- Clear browser cache and hard refresh (Ctrl+Shift+R)

**If TypeScript errors occur:**
- Run `npm run dev` to start development server
- Check for missing imports in component files
- Verify all type definitions are correct

**If animations don't work:**
- Disable reduced motion in system preferences
- Check CSS property names
- Verify animation keyframes are defined

## Browser Compatibility

- Modern browsers with CSS Grid support
- JavaScript ES6+ features
- CSS custom properties (variables)
- Intersection Observer for scroll animations

## Performance Considerations

- CSS is minified in production build
- Images use lazy loading where applicable
- Animations use transform for GPU acceleration
- Large chunks of text are truncated