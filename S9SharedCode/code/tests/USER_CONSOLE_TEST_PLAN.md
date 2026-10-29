# Agent Console User Testing Plan

## Overview
Comprehensive testing of the Aria agent console as an actual end-user would experience it.
This plan covers functional, performance, and UX testing across all major user workflows.

---

## 📋 TODO: Pre-Test Setup

- [ ] 1.1 Verify agent server is running: `GET /api/health` returns `agent: "ready"`
- [ ] 1.2 Verify gateway is accessible: health endpoint shows `gateway_up: true`
- [ ] 1.3 Authenticate with valid token (or test-token for local dev)
- [ ] 1.4 Check test data/permissions: ensure sample documents exist if needed
- [ ] 1.5 Open browser console and clear any existing errors

---

## 🔍 TODO: Basic UI Navigation Tests

### 2.1 Page Load & Initial State
- [ ] 2.1.1 Verify console loads within reasonable time (<10s)
- [ ] 2.1.2 Check that the main layout renders correctly (sidebar + main content)
- [ ] 2.1.3 Verify no JavaScript errors in browser console
- [ ] 2.1.4 Check that all primary navigation elements are visible:
  - [ ] Chat/Research tab
  - [ ] Runs history
  - [ ] Settings/Preferences
  - [ ] Document management
- [ ] 2.1.5 Verify responsive layout on different viewport sizes

### 2.2 Navigation Flow
- [ ] 2.2.1 Click through each primary navigation tab
- [ ] 2.2.2 Verify each view loads without errors
- [ ] 2.2.3 Check that browser back/forward buttons work correctly
- [ ] 2.2.4 Verify session state persists across navigation
- [ ] 2.2.5 Test that refreshing the page preserves current state

---

## 💬 TODO: Chat Functionality Tests

### 3.1 Basic Chat Interaction
- [ ] 3.1.1 Type a simple greeting ("Hello") and send
- [ ] 3.1.2 Verify response appears within reasonable latency (<10s for simple queries)
- [ ] 3.1.3 Check that the message bubble renders correctly
- [ ] 3.1.4 Verify typing indicators appear during response generation
- [ ] 3.1.5 Test sending a multi-line message
- [ ] 3.1.6 Verify message history scrolls properly

### 3.2 Chat Message Formatting
- [ ] 3.2.1 Send a message containing markdown (bold, italic, lists)
- [ ] 3.2.2 Verify markdown renders correctly in responses
- [ ] 3.2.3 Check code block formatting (if applicable)
- [ ] 3.2.4 Verify images/attachments display correctly (if sent)
- [ ] 3.2.5 Test unicode/special characters in messages

### 3.3 Chat Persistence & State
- [ ] 3.3.1 Start a conversation, refresh the page, verify messages persist
- [ ] 3.3.2 Close and reopen browser, verify conversation history
- [ ] 3.3.3 Test multiple concurrent conversations (if supported)
- [ ] 3.3.4 Verify message timestamps are accurate
- [ ] 3.3.5 Test that very long conversations don't break the UI

---

## 🔬 TODO: Research Mode Tests

### 4.1 Research Query Submission
- [ ] 4.1.1 Submit a simple research query ("What is quantum computing?")
- [ ] 4.1.2 Verify "Research" mode activates (different UI indication?)
- [ ] 4.1.3 Check that the query goes to the research pipeline (not chat)
- [ ] 4.1.4 Verify progress indicators show during research

### 4.2 Research Depth Options
- [ ] 4.2.1 Submit research with "Quick" depth
- [ ] 4.2.2 Submit research with "Standard" depth
- [ ] 4.2.3 Submit research with "Deep" depth
- [ ] 4.2.4 Compare response quality/length across depths
- [ ] 4.2.5 Verify depth selection UI is intuitive

### 4.3 Research Output Quality
- [ ] 4.3.1 Verify research report contains structured headings
- [ ] 4.3.2 Check that sources are properly cited
- [ ] 4.3.3 Verify key figures/data points are included
- [ ] 4.3.4 Check that findings are not condensed (preserve completeness)
- [ ] 4.3.5 Verify report length scales with depth setting

### 4.4 Research Session Persistence
- [ ] 4.4.1 Start a research session, refresh page during research
- [ ] 4.4.2 Verify research resumes correctly after interruption
- [ ] 4.4.3 Check that partial results are preserved
- [ ] 4.4.4 Verify ability to view completed research later
- [ ] 4.4.5 Test viewing research results in "Runs" history

---

## 📄 TODO: Document Creation Tests

### 5.1 Document Authoring
- [ ] 5.1.1 Request document creation ("Write a 5-page report on X")
- [ ] 5.1.2 Verify document renders correctly
- [ ] 5.1.3 Check word count matches expected size
- [ ] 5.1.4 Verify document sections are properly structured
- [ ] 5.1.5 Test different document formats (PDF, DOCX, etc. if available)

### 5.2 Document Editing/Refinement
- [ ] 5.2.1 Request edits to a generated document
- [ ] 5.2.2 Verify edits are applied correctly
- [ ] 5.2.3 Check that document history/versioning works
- [ ] 5.2.4 Test expanding/contracting document sections
- [ ] 5.2.5 Verify document statistics (word count, sections) update correctly

### 5.3 Document Download/Sharing
- [ ] 5.3.1 Download a generated document
- [ ] 5.3.2 Verify downloaded file opens correctly in appropriate app
- [ ] 5.3.3 Check file naming convention
- [ ] 5.3.4 Verify file size is reasonable
- [ ] 5.3.5 Test document sharing features (if available)

---

## 📁 TODO: Document Management Tests

### 6.1 Document Library
- [ ] 6.1.1 Navigate to document library
- [ ] 6.1.2 Check that documents are listed correctly
- [ ] 6.1.3 Verify search/filter functionality
- [ ] 6.1.4 Test document deletion (if available)
- [ ] 6.1.5 Verify document metadata displays correctly

### 6.2 Document Toggle
- [ ] 6.2.1 Test "docs on/off" toggle in chat
- [ ] 6.2.2 Verify docs-off mode excludes documents from search
- [ ] 6.2.3 Verify docs-on mode includes documents
- [ ] 6.2.4 Check that toggle preference persists across sessions
- [ ] 6.2.5 Test per-request document disabling

---

## 🎨 TODO: A2UI Component Tests

### 7.1 A2UI Generation
- [ ] 7.1.1 Request A2UI component generation via API
- [ ] 7.1.2 Verify generated components match request intent
- [ ] 7.1.3 Check that generated surfaces validate correctly
- [ ] 7.1.4 Test various A2UI component types (Table, Chart, Text, etc.)
- [ ] 7.1.5 Verify error messages for invalid requests

### 7.2 A2UI Rendering
- [ ] 7.2.1 Verify generated A2UI surfaces render in console
- [ ] 7.2.2 Test responsive behavior of A2UI components
- [ ] 7.2.3 Check accessibility of generated components
- [ ] 7.2.4 Verify data binding works correctly
- [ ] 7.2.5 Test component interaction (if applicable)

---

## ⚡ TODO: Performance Tests

### 8.1 Response Time
- [ ] 8.1.1 Measure latency for simple chat queries (<5s target)
- [ ] 8.1.2 Measure latency for research queries (varies by depth)
- [ ] 8.1.3 Measure document generation times
- [ ] 8.1.4 Check that streaming updates appear promptly
- [ ] 8.1.5 Verify timeout handling for long-running requests

### 8.2 Concurrent Usage
- [ ] 8.2.1 Launch multiple concurrent chat requests
- [ ] 8.2.2 Verify each gets independent responses
- [ ] 8.2.3 Check that system handles load gracefully
- [ ] 8.2.4 Verify no cross-contamination between sessions
- [ ] 8.2.5 Test resource usage under load

### 8.3 Memory & Resource Usage
- [ ] 8.3.1 Monitor browser memory usage during extended sessions
- [ ] 8.3.2 Check for memory leaks after many interactions
- [ ] 8.3.3 Verify garbage collection works properly
- [ ] 8.3.4 Test with very long conversation histories
- [ ] 8.3.5 Verify performance degrades gracefully

---

## ❌ TODO: Error Handling Tests

### 9.1 Network Errors
- [ ] 9.1.1 Test behavior when gateway is unresponsive
- [ ] 9.1.2 Verify proper error message display
- [ ] 9.1.3 Test offline/reconnect scenarios
- [ ] 9.1.4 Verify graceful degradation

### 9.2 Input Validation
- [ ] 9.2.1 Submit empty query
- [ ] 9.2.2 Submit extremely long query
- [ ] 9.2.3 Submit query with special characters
- [ ] 9.2.4 Submit query with potential injection attempts
- [ ] 9.2.5 Test invalid parameter combinations

### 9.3 Authentication
- [ ] 9.3.1 Test with missing authentication token
- [ ] 9.3.2 Test with invalid authentication token
- [ ] 9.3.3 Verify redirect to login on auth failure
- [ ] 9.3.4 Test session expiration handling
- [ ] 9.3.5 Verify secure handling of credentials

---

## 🎯 TODO: Cross-Session & State Tests

### 10.1 Session Continuity
- [ ] 10.1.1 Complete a task, close browser, return later - verify state
- [ ] 10.1.2 Test with browser in background tab (visibility change)
- [ ] 10.1.3 Verify WebSocket/streaming reconnection logic
- [ ] 10.1.4 Test state after browser crashes/restarts
- [ ] 10.1.5 Verify data integrity after unexpected shutdowns

### 10.2 Mode Switching
- [ ] 10.2.1 Switch from chat to research mode mid-conversation
- [ ] 10.2.2 Switch from research to chat mode
- [ ] 10.2.3 Verify context preservation across mode switches
- [ ] 10.2.4 Test that mode switching doesn't corrupt state
- [ ] 10.2.5 Verify UI updates correctly when mode changes

---

## 🎨 TODO: UI/UX Quality Tests

### 11.1 Visual Design
- [ ] 11.1.1 Check layout consistency across views
- [ ] 11.1.2 Verify color scheme/accessibility compliance
- [ ] 11.1.3 Check typography hierarchy
- [ ] 11.1.4 Verify icons are clear and consistent
- [ ] 11.1.5 Test dark/light mode (if available)

### 11.2 Interaction Design
- [ ] 11.2.1 Test all clickable elements have hover states
- [ ] 11.2.2 Verify button labels are clear and actionable
- [ ] 11.2.3 Check that loading states are indicated
- [ ] 11.2.4 Verify drag-and-drop (if applicable)
- [ ] 11.2.5 Test keyboard navigation

### 11.3 Feedback & Notifications
- [ ] 11.3.1 Verify success/error notifications appear
- [ ] 11.3.2 Check that notifications are dismissible
- [ ] 11.3.3 Verify notification duration is appropriate
- [ ] 11.3.4 Test that errors are actionable
- [ ] 11.3.5 Verify progress indicators are clear

---

## 📱 TODO: Mobile Responsiveness Tests

### 12.1 Viewport Adaptation
- [ ] 12.1.1 Test on iPhone SE viewport size
- [ ] 12.1.2 Test on iPad viewport size
- [ ] 12.1.3 Test on standard mobile phone size
- [ ] 12.1.4 Test on standard tablet size
- [ ] 12.1.5 Verify layout adjustments are smooth

### 12.2 Touch Interactions
- [ ] 12.2.1 Test tap targets are appropriately sized
- [ ] 12.2.2 Verify touch scrolling works smoothly
- [ ] 12.2.3 Test pinch-to-zoom behavior
- [ ] 12.2.4 Verify virtual keyboard doesn't break layout
- [ ] 12.2.5 Test copy/paste functionality on mobile

---

## 🧪 TODO: Integration Tests

### 13.1 External Integrations
- [ ] 13.1.1 Test document upload and processing
- [ ] 13.1.2 Verify citation/source extraction
- [ ] 13.1.3 Test integration with external document repositories
- [ ] 13.1.4 Verify data export/import functionality
- [ ] 13.1.5 Test third-party tool integrations

### 13.2 API Integration
- [ ] 13.2.1 Test all documented API endpoints
- [ ] 13.2.2 Verify rate limiting behavior
- [ ] 13.2.3 Test pagination of results
- [ ] 13.2.4 Verify proper HTTP status codes
- [ ] 13.2.5 Test API error handling

---

## 📊 TODO: Edge Case Tests

### 14.1 Unusual Input Scenarios
- [ ] 14.1.1 Submit a query in a different language
- [ ] 14.1.2 Submit a query with very specific domain terminology
- [ ] 14.1.3 Submit a query with ambiguous intent
- [ ] 14.1.4 Submit a query with conflicting requirements
- [ ] 14.1.5 Submit a follow-up to a question from a previous session

### 14.2 Boundary Conditions
- [ ] 14.2.1 Submit maximum length query
- [ ] 14.2.2 Submit minimum viable query
- [ ] 14.2.3 Test with maximum concurrent users
- [ ] 14.2.4 Test with maximum document size
- [ ] 14.2.5 Test session timeout behavior

---

## 📝 TODO: Test Data Collection

### 15.1 Metrics to Record
- [ ] 15.1.1 Page load time
- [ ] 15.1.2 First meaningful paint
- [ ] 15.1.3 Time to first response (TTFB)
- [ ] 15.1.4 Total response time for queries
- [ ] 15.1.5 Error rate and types

### 15.2 User Experience Metrics
- [ ] 15.2.1 Task completion rate
- [ ] 15.2.2 User satisfaction (if possible to measure)
- [ ] 15.2.3 Number of errors encountered
- [ ] 15.2.4 Time spent per task
- [ ] 15.2.5 Frustration points encountered

---

## 🐛 TODO: Bug Reporting

### 16.1 Bug Documentation Template
- [ ] 16.1.1 Record steps to reproduce
- [ ] 16.1.2 Record expected vs actual behavior
- [ ] 16.1.3 Capture screenshots/error messages
- [ ] 16.1.4 Note browser/system configuration
- [ ] 16.1.5 Assign severity/priority level

### 16.2 Known Issues Tracking
- [ ] 16.2.1 Document A2UI validation issue (422 response)
- [ ] 16.2.2 Track any performance bottlenecks
- [ ] 16.2.3 Note any UX friction points
- [ ] 16.2.4 Document any inconsistent behaviors
- [ ] 16.2.5 Track intermittent issues

---

## ✅ TODO: Final Verification

### 17.1 Smoke Tests
- [ ] 17.1.1 Run critical path test: Login → Chat → Research → Document
- [ ] 17.1.2 Verify all major features work end-to-end
- [ ] 17.1.3 Confirm no regressions from any fixes
- [ ] 17.1.4 Verify system is stable under basic load
- [ ] 17.1.5 Confirm all TODO items addressed

### 17.2 Test Summary
- [ ] 17.2.1 Count total tests executed
- [ ] 17.2.2 Count tests passed/failed
- [ ] 17.2.3 Document any new issues found
- [ ] 17.2.4 Summarize user experience quality
- [ ] 17.2.5 Recommend any UX improvements

---

## 📊 Results — first real-user pass (2026-10-29)

Scope: Chromium through the desktop-browser tool, against the built SPA served
by `agent_server.py` (`http://127.0.0.1:8500`) and `llm_gatewayV9`
(`http://127.0.0.1:8109`). Viewports were 1440×900 and 390×844. This was a
defect-finding pass over the highest-traffic journeys, **not** the full
17-area matrix above; the unchecked boxes still describe real gaps.

### Verified end to end

| Journey | Evidence | Result |
|---|---|---|
| Rail navigation, deep links, back/forward, refresh | Sections visited in this pass load; deep links land on the right route; Back restores both the page and its tab title; refresh keeps the current section | Pass, no console errors |
| Revisiting a section | `Runs → Memory → Runs` made **0** API calls, kept the Runs selection, and preserved scroll/selection through the switch | Pass |
| Loading honesty | Documents/Scheduler/Ledger/Apps tiles and the Runs/Research/Chat badges show `—` until data arrives, never a false `0` | Pass |
| Mobile rail | `/settings` and `/computer` deep links scroll the rail before paint (`scrollLeft` 276 / 234); active item is visible; tap targets ≥ 24 px | Pass |
| Documents upload | Optimistic row visible at **636 ms**, before the list GET returned; list merge kept it; index reached `ready` | Pass |
| Documents download | Downloaded `console-qa-notes.md` (382 B, `text/markdown`) with the expected contents | Pass |
| Chat send | "Reply with exactly the word pong." completed (`done in 6.9s`); no console errors | Pass |
| Chat cancel | Stop mid-generation produced `Stopped.`; no late tokens and no errors | Pass |
| Chat persistence | "Tell me a joke about cats" restored with 6,054 characters before and after reload | Pass |
| Background tab | Sent a prompt, immediately focused another tab; the turn completed server-side (`background-ok`, 7.9 s) and the answer appeared on return with no errors. Reattachment can take a few seconds, and a detached bubble has no elapsed badge | Pass with caveat |
| A2UI in Chat | "Three Largest Planets" surface rendered a Jupiter/Saturn/Uranus table in 7.3 s; heading and table survived reload | Pass |
| A2UI in Apps | `/api/apps/a2ui-test/a2ui` returned 200; preview rendered a table with no console errors | Pass |
| Command palette | Ctrl+K focuses the input and locks body scroll; `doc` filters to Documents/Reload console; Enter navigates to `/documents`; Escape closes and restores scroll; focus never leaves the dialog | Pass (Tab is inert; see known issues) |
| Gateway down / Retry | With 8109 killed: `?fresh=1` returned false in 1.01 s; the next stale poll returned false in 37 ms; banner appeared immediately; Retry showed `Checking…` for ~1 s and kept the banner; after restart the banner cleared and the GATEWAY tile read `up` | Pass |
| Accessibility / best practices | Lighthouse accessibility **1.0** and best-practices **1.0** on `/console`, `/documents`, `/runs`, `/settings`; console errors 0 | Pass |

### Defects found and fixed

1. **Section switches flashed black.** `KeepAlive` unmounted the outgoing view
   and replayed a fade on the incoming one, so every mount refetched its list
   and the content area was empty for the first ~80–220 ms of each hop (a quick
   `Runs → Memory → Runs` flashed and re-queried). Polling is now gated to the
   active section, list reads keep a 10 s TTL, and each section has its own
   Suspense boundary. The switch itself is now an **instant swap**, the way
   VS Code swaps editor tabs: the outgoing view is `display: none`d and the
   incoming one shown in the same commit, with no animation, no opacity fade
   and no held overlay. Three animated designs were measured and rejected
   first: a fade from `opacity: 0` left the content area empty for 80–220 ms,
   a crossfade blended two text-dense pages (the Runs list stayed legible
   through the Chat transcript), and a `clip-path` wipe read as a sweep; a
   fade through black dimmed the whole content area on every hop. Verified
   live on the served bundle: immediately after the click and at every hop
   (`Runs → Chat → Settings → Documents → Runs → Chat`) exactly one visible
   section at `opacity: 1`, one `#main` where the view defines it, zero
   `view-*` animations, no console errors.
2. **False empty states while loading.** Several panels rendered `0` or
   "unreadable" before their first response. Tiles and badges now render `—`
   until the request lands (`Stat loading` and call-site gates).
3. **Mobile deep links landed with the active rail item off-screen.** The rail
   now scrolls to the active item before paint; verified at 390 px for
   `/settings` and `/computer`.
4. **Uploaded documents were invisible until the list refreshed.** The new row
   is inserted optimistically and merged after the stale list read; visible in
   under a second.
5. **Gateway death was reported as "up" for ~30 s, and Retry could false-clear
   the banner.** `/api/health` served a background-refreshed 30 s cache; Retry
   called the same cached value. The probe now fails fast on a connection error
   (one 1 s attempt, no three-path multiplier), the health TTL is 8 s, a stale
   value is refreshed on the request path, and Retry sends `?fresh=1`, which
   always probes. Covered by `tests/test_health_probe.py` (4 tests).
6. **`aria-sort` was on the sort button.** ARIA only allows it on the
   columnheader; Lighthouse failed `/console` at 0.95 because of it. Moved to
   `<th scope="col">`. Covered by `A2ui.a11y.test.ts`.
7. **YAML comments failed contrast.** `text-zinc-600` on the config viewer's
   `#0a0a0d` background measured 2.56:1 at 11.5 px. Comments are content, so
   they now use `text-zinc-400` (~7.7:1), as do null/dash tokens. Covered by
   the contrast case in `yaml.test.ts`; `/settings` is now 1.0.
8. **Back left the tab title on the previous section.** Keep-alive hides
   visited views instead of unmounting them, and `TopBar` set
   `document.title` in an effect keyed only on `crumb`. `Documents → Runs →
   Back` showed the Documents page under an "Aria · Runs" tab. The effect now
   also depends on `useSectionActive()`, and hidden sections skip the write.
   Covered by `ui.title.test.ts`; verified in the browser in both directions.
9. **Memory IDs failed contrast.** The monospace `#…` id on each memory card
   used `text-zinc-600` on `#0e0e12` (2.49:1 at 11 px), which Lighthouse failed
   the whole `/memory` page on. It is content — users quote it to forget a
   record — so it is now `text-zinc-400`; `/memory` is back to 1.0.

### Verification commands and results

- Frontend: `npx tsc --noEmit` clean; `npx --yes vitest run src` → **97 passed**
  (18 files); `npm run build` clean. Served bundle:
  `dist/assets/index-DSewBoDW.js`.
- Backend targeted: `tests/test_health_probe.py` +
  `tests/test_agent_server_api.py` → 35 passed;
  `tests/test_agent_gateway_auth_v2.py` + `tests/test_health_probe.py` → 45
  passed.
- Backend full: `uv run python -m pytest tests/ -q` → **1813 passed, 39
  skipped** (416 s).
- Gateway full: `uv run python -m pytest -q` → **1222 passed, 2 skipped**
  (236 s).

### Known issues and accepted behavior

- **Private desktop is not observable in this harness.** With
  `ARIA_PRIVATE_DESKTOP=true`, the console never exposed `aria-private`; the
  flag path needs a native-shell/manual check.
- **A dead gateway is noticed on the next poll**, not instantly: health is
  cached for 8 s and the console polls every 15 s, so the banner can lag by up
  to one poll unless Retry is pressed. This is a deliberate cost/latency
  trade-off, not a correctness issue.
- **Sending a chat while the banner says "down" can succeed.** Chat calls
  `ensure_gateway()`, which starts the gateway if it is not running. The banner
  describes the state at the last probe; the send is also a legitimate
  recovery path.
- **Tab is inert in the command palette.** cmdk items are not focusable, so Tab
  leaves focus on the input; Arrow keys navigate, Enter selects, Escape closes.
  Accepted as the documented keyboard model, since focus never escapes the
  dialog.
- **A backgrounded chat tab reconciles late.** The turn keeps running
  server-side, but the tab falls back to "still running on the server…" and
  updates when it is visible again (observed a few seconds, with no elapsed
  badge on the reattached bubble). The answer is not lost; the live stream is.
- **SEO 0.6 is expected.** The console is token-gated and has no crawlable
  content; only accessibility and best-practices are meaningful scores.
- **The full 17-area matrix is not complete.** Not yet covered: missing/invalid
  token and session-expiry flows; browser crash / process-kill data integrity;
  stream reconnection after a network drop; offline and throttled networks;
  very long conversations and very large uploads; Firefox/WebKit and a manual
  screen-reader pass; mobile virtual keyboard, pinch-zoom and drag-and-drop;
  scheduler execution and external integrations.

