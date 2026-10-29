# UI/UX Improvement Plan for Aria Console

## Goal
Refine `S9SharedCode/code/console-frontend/` for smoother, cleaner UX. Focus on loading states, transitions, mobile polish, consistency, accessibility, and visual refinement.

## Current Architecture (as of latest codebase)

- **React 19 + TypeScript + Vite + Tailwind 4**
- **React Router 7** with client-side routing; all 15 views share one SPA
- **Code splitting:** All views are `React.lazy`; `Suspense` with shell-level skeleton fallback (`App.tsx:64-96`)
- **Keep-alive:** `KeepAlive.tsx` mounts views once, hides inactive with `hidden` attribute; `useSectionActive` stops polling on hidden views
- **Idle preloader:** `lib/prefetch.ts` warms chunks on idle; `NEVER_PRELOADED = ['/code']` excludes Monaco from idle prefetch
- **Rail:** `ui.tsx` Rail component is a horizontal strip below `lg:1000px`, vertical column above; **NEW:** rail pinning with localStorage persistence, `useSyncExternalStore`, `inert` attribute; collapse button in brand row and top bar
- **Health polling:** Single `HealthProvider` polls gateway; `GatewayDownBanner` shown on all breakpoints
- **CSS variables:** Surface hierarchy, semantic colors, IDE scope (`.ide`), motion tokens, reduced-motion support
- **Components:** `Stat` (CLS-fixed with `whitespace-nowrap`), `Skel` (shimmer cards), `Pill` (color-coded states), `Empty` (fade-in icon + title + sub), `SkipLink` (jumps to `#main`)

## Key Changes Since Initial Plan

### New Components & Features
1. **Rail pinning** (`ui.tsx:113-156`) - collapsible rail with edge hover reveal, Escape close, persisted state
2. **AgentPanel** (`AgentPanel.tsx`) - full conversation view with tool call steps, streaming, model picker
3. **MonacoEditor** (`MonacoEditor.tsx`) - VS Code-style editor with worker setup, language mapping, IDE theme scoping
4. **CodeIntel** component - code intelligence features
5. **TerminalDock** component - terminal integration
6. **DiffMarks library** (`lib/diffMarks.ts`) - for tracking code changes
7. **YAML rendering** (`lib/yaml.tsx`) - for settings/config display
8. **Palette lib** (`lib/palette.ts`) - for opening command palette

### Test Infrastructure Added
- `App.router.test.ts` - Verifies router shell structure
- `api.cache.test.ts` - Verifies cache invalidation on writes
- `ui.railPin.test.ts` - Verifies rail pinning behavior
- `e2e/chat-ux-audit.test.ts` - Comprehensive chat UX audit test suite
- `e2e/parts.spec.ts` - Parts (3D mesh optimization) E2E tests
- `e2e/test-code-section.spec.ts` - Code section E2E tests

## Action Plan

### 1. Shell Skeleton / Fallback Fix (Critical - failing test)
- **File:** `App.tsx:64-96` (Fallback component)
- **Issue:** Playwright test "route fallback keeps the shell visible while a chunk is in-flight" fails (got null)
- **Action:** The Fallback component returns a complete shell structure (rail + header + content skeleton). Need to verify the `Suspense` boundary is working correctly. The issue may be that `KeepAliveHost` is not inside `Suspense` properly, or the fallback is being replaced by keep-alive content.
- **Fix:** Ensure `<Suspense fallback={<Fallback />}>` wraps `<KeepAliveHost>` correctly. Check if `KeepAliveHost` renders `null` for unvisited sections.

### 2. Rail Pinning Accessibility
- **File:** `ui.tsx:113-356` 
- **New feature:** Rail collapses with overlay, edge hover reveal, top-bar toggle
- **Check:** 
  - `aria-expanded` on toggle button (`ui.tsx:275`)
  - `aria-controls="primary-rail"` present (`ui.tsx:276`)
  - `inert` attribute applied when closed (`ui.tsx:227`)
  - Focus trap: closed rail takes items out of tab order via `inert` (`ui.tsx:209`)
  - Edge hover trigger reveals rail under stationary pointer (`ui.tsx:216-224`)
  - Pointer position watch closes overlay on `clientX > 208` (`ui.tsx:195-200`)
  - Escape closes peek but not pin state (`ui.tsx:182-187`)
  - Top bar "Show navigation" button appears only when wide and unpinned (`ui.tsx:379-388`)

### 3. Agent Panel Integration
- **File:** `components/AgentPanel.tsx` (new, 1164 lines)
- **Check:** The panel has conversation history, tool steps visibility, streaming answers, diff proposals
- **Action:** Verify it integrates smoothly with Code view; check CSS class overrides in `.ide` scope (`index.css:770-929`)

### 4. Monaco Editor Theme Consistency
- **File:** `components/MonacoEditor.tsx`, `index.css:795-929`
- **Check:** Editor gets VS Code Dark Modern palette via `.ide` scope; violet classes remapped to blue
- **Issue spotted:** `.ide button.bg-accent:hover` uses `brightness(1.1)` but non-accent buttons may need theme consistency

### 5. Chat UX Audit Findings
- **File:** `e2e/chat-ux-audit.test.ts`
- **Test scenarios covered:** Normal chat, empty message, long message, double-click while streaming, refresh mid-response, new chat while streaming, rapid fire, Ctrl+K, console errors, navigation during chat
- **Action:** Run the test suite to identify any failing scenarios; address any UX friction points

### 6. Consistent Empty States
- All views use `<Empty />` from `ui.tsx:429-439` - verify all views use it consistently
- **Views with custom empty states to check:**
  - `Research.tsx:861-871` (inline custom empty)
  - `Memory.tsx` - uses `<Empty />`
  - `Ledger.tsx:356-365` - inline empty states

### 7. Stat Component Consistency
- **File:** `ui.tsx:404-427` (Stat component)
- **Check:** Parts.tsx uses its own local `Stat` (`Parts.tsx:388-395`) instead of the shared one
- **Action:** Consider unifying Parts.tsx to use the shared Stat component for consistency

### 8. Transition Smoothness
- CSS animations defined in `index.css:150-231`:
  - `fade-in-up`, `fade-in`, `slide-in-right`, `scale-in`, `pulse-glow`
  - `msg-in` (180ms for chat bubbles), `surface-in` (220ms), `view-in` (160ms), `row-in` (120ms)
- `transition-duration: 150ms` baseline for native controls (`index.css:241-246`)
- `motion` (Framer Motion) used in Rail for `layoutId` active indicator
- **Action:** Verify Authoring view and other view transitions use consistent animation vocabulary

### 9. Focus Management
- `useFocusTrap` in `lib/a11y.ts:19-73` - used by modals/drawers
- `SkipLink` in `ui.tsx:484-499` - jumps to `#main`
- `useMainId` returns `'main'` only for active section
- **Action:** Verify all modals (Chat ReportCard, Documents detail/preview, Authoring file list) use focus traps

### 10. Mobile Rail Responsiveness
- Rail is horizontal strip below 1000px, vertical column above
- `max-h-[34vh]` on the rail's left sidebar on mobile
- `lg:hidden` brand row hidden, `lg:block` sidebar shown
- **Action:** Verify all views respect the `lg:` breakpoint consistently

## Validation Plan

1. Run unit tests: `cd S9SharedCode/code/console-frontend && npx vitest run`
2. Run TypeScript check: `npx tsc --noEmit`
3. Run CSS build: `npx tailwindcss -i ./src/index.css -o /dev/null` (validate)
4. Run E2E chat UX tests (requires backend running)
5. Visual regression check on route transitions

## Out of Scope
- Backend changes to `agent_server.py` or `llm_gatewayV9/`
- Electron shell changes
- New feature development — this is a refinement pass only
