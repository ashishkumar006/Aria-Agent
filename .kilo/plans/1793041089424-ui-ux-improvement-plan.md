# UI/UX Improvement Plan for Aria Console

## Goal
Smooth and clean UX improvements to `S9SharedCode/code/console-frontend/`.

## Summary of Findings

The codebase is a React 19 + TypeScript + Vite + Tailwind 4 application with 15 views. Key infrastructure:
- Route-based code splitting with idle preloading (`lib/prefetch.ts`)
- Keep-alive pattern via `KeepAlive.tsx` (no remounts on nav)
- Shared shell: `Rail`, `TopBar`, `Stat`, `Skel`, `Pill`, `Empty` components
- Typed REST+SSE API client (`api.ts`)

## Action Plan

### 1. Loading States — Fix Failing Shell Skeleton Test
- **File:** `App.tsx:64-96` (Fallback component)
- The Playwright test "route fallback keeps the shell visible while a chunk is in-flight" fails (got null instead of skeleton).
- **Fix:** Verify `Suspense` fallback is properly triggered; ensure `ChunkLoader` boundary renders the shell skeleton consistently across all 15 routes.

### 2. Consistent Loading Skeletons Across Views
- **File:** `components/ui.tsx:277-288` (Skel component)
- Several views use generic `<Skel n={3} />` or `<Skel n={4} />` for table loading states.
- **Action:** Add view-specific skeleton variants where content structure differs (e.g., Scheduler table vs. Cards grid vs. Memory cards).
- **Files affected:** `Runs.tsx`, `Research.tsx`, `Ledger.tsx`, `Documents.tsx`, `Scheduler.tsx`

### 3. Mobile Rail UX
- **File:** `components/ui.tsx:113-209` (Rail component)
- Mobile rail is a scrollable icon strip. The "swipe →" cue exists in Memory but not consistently across other views.
- **Action:** Apply the same swipe cue pattern to any view whose primary navigation is in the rail and may be missed on mobile.

### 4. Empty States Visual Consistency
- All views use `<Empty />` from `ui.tsx`, but some views have custom empty states inline (e.g., `Research.tsx:861-871`, `Ledger.tsx:390`).
- **Action:** Standardize on the shared `<Empty />` component for consistency. Remove duplicate empty state implementations.

### 5. Stat Card Consistency
- **File:** `components/ui.tsx:240-263` (Stat component)
- Stat cards have CLS prevention (fixed min-width, `tabular-nums`).
- **Action:** Audit all view usages to ensure `whitespace-nowrap` and min-width constraints are applied consistently. Check `Mission.tsx:108-119`, `Computer.tsx:208-220`.

### 6. Tab Navigation Smoothness
- **File:** `views/Authoring.tsx:773-791` (stepTab/edgeTab)
- Authoring view already has keyboard navigation for tabs with disabled state handling.
- **Action:** Audit other tab-based navigation (Research report/log tabs at `Research.tsx:771-783`) and apply consistent keyboard support.

### 7. Focus Management on Dynamic Content
- **File:** `lib/a11y.ts` (useFocusTrap)
- Reports: `Inspector.tsx`, `Chat.tsx`, `ReportCard` use focus traps.
- **Action:** Ensure all modals/drawers (Documents detail/preview, Authoring file list) use `useFocusTrap` consistently.

### 8. Transition Smoothness
- `motion` (Framer Motion) is used in `CommandPalette.tsx` for spring animations.
- **Action:** Audit views for jarring transitions between states — apply consistent `transition` classes or `motion` animations to:
  - Shell skeleton → view content swap
  - Empty → loaded state changes
  - Tab switches

### 9. Accessibility Audit
- **File:** `lib/a11y.ts:1-50` (useFocusTrap)
- `SkipLink` exists at `ui.tsx:320-335`
- **Action:** Verify all views render `<SkipLink />` and `<main id={mainId}>` consistently. Check `Mission.tsx:66-67`, `Runs.tsx:233`, `Computer.tsx` — some views may be missing these.

### 10. Gateway Down Handling
- **File:** `components/ui.tsx:57-80` (GatewayDownBanner)
- `HealthProvider` polls gateway health once globally.
- **Action:** Ensure all views handle `useHealth() === false` gracefully — show banner, disable actions, don't retry aggressively.

## Validation Plan
- Run Playwright test for shell skeleton: `npm run test` (target: "route fallback keeps shell visible")
- Visual regression checks on route transitions
- Verify mobile layout on Chrome DevTools responsive mode
- Check accessibility with Lighthouse

## Out of Scope
- Backend changes to `agent_server.py` or `llm_gatewayV9/`
- Electron shell changes
- New feature development — this is a refinement pass only
