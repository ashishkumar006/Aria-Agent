# Aria Agent Console — Target Plan

> **Goal:** grow `http://localhost:8500/console` from one Chat page into a
> full operator console for the Aria agent, in our in-house design language
> (and extending it where Aria needs more).
> **Style authority:** `llm_gatewayV9/static/console-theme.css`
> (near-black surfaces, hairline borders, refined violet, Inter/JetBrains
> Mono, 4–6px rectangles, ⌘K palette, content-in motion). Every new page
> reuses these — no new visual language.
> **Old UI:** `web/index.html` stays untouched until the console reaches
> parity; then it is retired, not maintained in parallel.

---

## 0. Where we are (done)

- [x] Console shell: rail (Chat/Runs/Memory/Scheduler/Skills/Ledger/Settings) + thread/DAG canvas (`web/console.*`, `/console`)
- [x] Live runs: `started` frame, 2s light-graph polling, layered DAG (ports, orthogonal edges), node inspector (OVERVIEW/OUTPUT/STATS), replay bar
- [x] Old-chat access: minted conversation ids for every run, legacy adopt endpoint
- [x] Server endpoints supporting it: `POST /api/chat` (+`started`), `GET /api/sessions`, `/graph?light`, `/nodes/{id}`, `/conversations/adopt`, `/api/health`, `/api/cost`
- [x] Runs page: list + detail + compare + `/api/runs/summary` (`web/runs.*`, `/runs`)
- [x] Research page: brief → DAG pipeline → deliverable doc + copy/download (`web/research.*`, `/research`)
- [x] Health is cached server-side (30s TTL, background refresh) — page loads no longer stall on the gateway probe
- [x] Memory page: drawer browser + chunk cards + remember/wipe bridges (`POST|DELETE /api/memory`)
- [x] Scheduler page: jobs CRUD board
- [x] Skills page: 5 capability packs (Web Research, Messaging, Calendar & Scheduling, Workspace Files, Integrations) over the 26-tool catalog + per-tool withhold (`GET|POST /api/config/tools`, `state/tools_disabled.json`)
- [x] Ledger page: per-skill spend + turn history (`GET /api/cost/by_skill`)
- [x] Settings page: environment status + read-only config
- [x] Desktop shell: Electron wrapper with autostart, single-instance, `aria://` links, child cleanup (`project3/agent-desktop/`)
- [x] Gateway console precedent: Overview/Connections/System/Ledger pages, Keys UI, deploy self-tests, tool ledger

## 1. UI/UX rules every new page follows

1. **Shell:** slim icon rail (200px, groups) + context pane + canvas. Rail items: Chat, Runs, Memory, Scheduler, Skills, Ledger, Settings. No dead links — a section ships only with a working page behind it.
2. **Density:** 13px base, 10–11px micro-labels in tracked caps, compact tables with tabular numerals, 6px scrollbars, sticky table heads.
3. **States:** every async pane shows loading → data → empty → error. No silent blank panels. Pollers skip re-render on identical payloads (signature guard) — no flicker.
4. **No pills:** 4–6px rectangles everywhere; violet reserved for active/focus/live; green/red/amber semantics only.
5. **Motion:** `content-in` fade-rise on cards, 30ms row stagger, press-scale buttons, `prefers-reduced-motion` respected.
6. **Safety:** all server-rendered strings escaped client-side; secrets never rendered (names + set/missing only); destructive actions confirm inline.
7. **Keyboard:** global ⌘K palette on every page (pages + buttons auto-discovered); Enter-to-send in composers; Esc closes overlays.
8. **Tests per page:** structure audit (no missing handlers/ids/dupes), `node --check`, live 200s, endpoint-shape check, one real user-flow smoke. No page merges without all five green.

## 2. Page specs

### P1. Chat (SHIPPED — harden only)
- [x] Thread, streaming logs, markdown answers, resume, cost badge, search
- [ ] Remaining: attachment support (needs gateway `POST /v1/files`), TTS playback per answer (`POST /api/tts`), multi-session tabs — stop-generation button done via §4.7

### P2. Runs (next)
A run-list + DAG inspector against our session store, with compare mode.
- Run list (from `GET /api/sessions`): status dot, query, node/skill counts, duration, cost — filter by status/skill, search
- Run detail: full DAG canvas (reuse console renderer, extracted to shared `console-dag.js`), inspector, replay bar, log tail
- Compare mode: two runs side-by-side (durations, cost, node counts)
- Backend: EXISTS (`/graph`, `/nodes/{id}`, adopt). New: `GET /api/runs/summary?since=` rollup for the list (node counts without per-session reads)
- Acceptance: open any historical run, replay it end-to-end, inspect any node

### P3. Memory
Chunk-inspector view over the gateway memory plane (our RAG-Documents equivalent).
- Drawer browser (working/episode/fact/playbook/policy/audit/document + legacy) with counts from `GET /v1/memory/stats`
- Chunk cards per item (descriptor, keywords, drawer badge, selected violet outline) + bottom metadata drawer (id, session, sources,Go? timestamps, vector dim)
- Search box → `POST /v1/memory/search` with drawer filter + top_k; write form (remember) + session wipe with confirm
- Backend: EXISTS on gateway; agent needs NOTHING new except the page. Plus per-item "why recalled" (which query terms/vector hit matched)
- Acceptance: write → search → find → wipe roundtrip from the page

### P4. Scheduler
Cron board over the agent's in-process scheduler.
- Job table (query, schedule, next fire, history), new-job dialog, trigger-now, delete with confirm
- Backend: EXISTS (`/api/schedule` CRUD). New: `GET /api/schedule/{sid}/runs` history per job
- Acceptance: create → trigger → history shows → delete

### P5. Skills
Tool/skill store: the 26 MCP tools as cards (name, group, description, fail-soft status via dry-run probe), per-skill enable/disable persisted to `agent_config.yaml` guard list.
- Backend: EXISTS (`/api/tools`, `/api/config`). New: `POST /api/config/skills` toggle + `POST /v1/control/tool-test` dry-run probe per tool on the gateway
- Acceptance: disable a tool → planner stops offering it → re-enable

### P6. Ledger (agent-side spend)
Gateway has the call ledger; the agent needs the turn ledger surfaced: per-conversation spend table (already `/api/cost`), per-skill cost attribution (needs `turn_costs.json` rollup endpoint — small), budget badges vs caps.
- Backend: EXISTS (`/api/cost`, `/api/audit`). New: `GET /api/cost/by_skill?conversation_id=`
- Acceptance: numbers on this page match gateway `/v1/cost/by_agent` for the same session

### P7. Settings
Models/routing/endpooints: gateway URL + health, provider order view (read `GATEWAY_V9` config endpoint — new tiny `GET /v1/config/routing` on gateway), agent knobs (temperature caps, max nodes) with file-backed save + restart notice, keys shortcut links into gateway Keys pages (keys NEVER live here).
- Acceptance: change → persists → effective after restart, visibly versioned

### P8. Voice (deferred until used)
Mic capture → `POST /api/stt` → chat → `POST /api/tts` auto-play, waveform bars, transcript cards. Backend EXISTS. Build only when voice becomes a real workflow.

## 3. Shared upgrades (apply across console + gateway UI)

- [ ] Extract `console-dag.js` (layout, render, inspector, replay) shared by Chat + Runs
- [ ] Extract `console-palette.js` (⌘K + toasts) shared by all agent pages — gateway already has `gateway.js`; keep the two shells in sync
- [x] Cancellable runs: `POST /api/chat/cancel {conversation_id|session_id}` + server-side abort flag checked between nodes; stop button in composer (Research) and Runs rows — done, `tests/test_cancel.py` + live SSE E2E green
- [ ] Request IDs end-to-end: `X-Request-ID` from console → agent → gateway → provider, shown in meta lines and ledger rows (pairs with gateway trace-ID work)
- [ ] Attachment pipeline: file picker → gateway `POST /v1/files` → `file_id` chip in composer → vision/code skills consume it
- [ ] Empty/error/offline states audit on every page (gateway down banner with retry, not a dead console)

## 4. Backend build list (agent server unless noted)

| # | Endpoint / change | Serves | Notes |
|---|---|---|---|
| 4.1 | `GET /api/runs/summary?since=` | P2 list | rollup without per-session reads |
| 4.2 | `POST /api/config/skills` toggle | P5 | guard list beside agent_config.yaml |
| 4.3 | `POST /v1/control/tool-test` (gateway) | P5 probes | dry-run per MCP tool via gateway |
| 4.4 | `GET /api/cost/by_skill` | P6 | rollup turn_costs.json by skill |
| 4.5 | `GET /v1/config/routing` (gateway) | P7 | order + pool sizes, names only |
| 4.6 | `POST /v1/files` (gateway) + skill support | P1 attach, P3 docs | multipart → file_id; chat/vision/embed accept it |
| 4.7 | `POST /api/chat/cancel` + node abort flag | stop buttons | done: `Executor.run(should_cancel=...)`, `_ACTIVE_RUNS`, `done.cancelled` |
| 4.8 | Trace IDs console→provider | debugging | pairs with gateway trace work |

## 5. Phasing (each phase merges only when §1.8 is green)

- **Phase A (done):** P2 Runs + style pass + name scrub
- **Phase B (done):** P3 Memory + P4 Scheduler (+ remember/wipe bridges)
- **Phase C (open):** shared dag/palette extracts, trace IDs (4.8), per-turn cost already live via P6 (cancellable runs 4.7 now done)
- **Phase D (open):** remaining 4.3 tool-test + 4.5 routing-config endpoints
- **Phase E (open):** P1 hardening (attachments via 4.6 files endpoint, TTS playback, tabs) + P8 Voice if needed
- **Someday:** compare-mode analytics, run diffing, scheduled-run calendar view

## 6. Non-goals (explicitly not building)

- A second visual language; public/multi-user auth (single-operator localhost console — matches gateway posture, revisited only if exposed)
- Re-implementing gateway pages inside the agent (Keys, providers, channels stay gateway-side; agent links out)
- Electron packaging (browser console until distribution demands it)
- Editing skills/prompts in-browser (files stay the source of truth; toggles only)

## 7. Desktop track (Electron shell)

`project3/agent-desktop/` — wrap, don't rewrite.
- [x] Phase 1: Electron shell over `/console` (health-gated load, waiting
      page with exact start commands, app menu, global shortcut, external
      links to system browser). Verified: syntax, `npm install`, binary runs.
      Launch needs a display: `npm start` with agent+gateway up.
- [ ] Tray icon, backend auto-start from the shell, `aria://` deep links
- [ ] Phase 2: Vite + React + Tailwind island per screen (Runs DAG via
      React Flow first), shell keeps serving the rest; tokens already match
- [ ] Phase 3: full React shell; static pages become the offline fallback

## 8. Definition of done (whole track)

Every page: §1.8 five greens. Every backend row in §4: unit test + live probe + Help/API-table row. Console never regresses the classic UI (untouched `index.html` until parity vote). Gateway safety track (auth, budgets, hardening) stays parked per operator direction — this plan spends zero on it.
