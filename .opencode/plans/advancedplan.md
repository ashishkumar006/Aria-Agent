# Advanced Plan: AG-UI / A2UI Integration Across Aria Console

## Overview
This document captures all identified opportunities to integrate **AG-UI (Agent User Interface)** and **A2UI (Aria 2 UI)** components across the Aria console. The goal is to standardize on AG-UI/A2UI for all structured model outputs, replacing ad-hoc markdown/html rendering with typed, composable UI surfaces.

---

## Current State Summary

### Already Implemented
- **A2UI Renderer** (`console-frontend/src/components/A2ui.tsx`) — 15+ components
- **A2UI Types** in `api.ts` (`A2uiSurface`, `A2uiComponent`, `A2uiProps`)
- **AG-UI Event Types** in `api.ts` (`AguiEvent`)
- **Tracker Apps** already consume A2UI surfaces via `/api/apps/:id/a2ui`
- **Apps.tsx** has A2UI toggle and renders surfaces via `<A2ui />`
- **AG-UI Event Streaming** used for tracker refreshes (`trackerRefreshStream`)

### A2UI Components Already Available (15+)
| Component | Status | Notes |
|-----------|--------|-------|
| `Text` | ✅ | Basic text |
| `CodeBlock` | ✅ | Code blocks |
| `Badge` | ✅ | Status badges |
| `KeyValue` | ✅ | Key-value pairs |
| `Divider` | ✅ | Separators |
| `Spacer` | ✅ | Spacing |
| `Link` | ✅ | Internal links (validated) |
| `Table` | ✅ | Search, sort, pagination built-in |
| `Sparkline` | ✅ | Time-series charts |
| `DataList` | ✅ | Key-value lists |
| `Action` | ⚠️ **Disabled** | Needs enablement for model-driven actions |
| `FilterChips` | ✅ | Filter chips |
| `StatusRow` | ✅ | Status badges |
| `Empty` | ✅ | Empty states |
| `Progress` | ✅ | Progress bars |
| `Card` | ✅ | Containers |
| `MetricRow` | ✅ | Metric displays |
| `Row` / `Column` | ✅ | Layout primitives |
| `Table` | ✅ | Full table with search/sort/pagination |

---

## Integration Opportunities by View

### 1. Chat View (`Chat.tsx`) — **HIGH PRIORITY**
**Current:** Renders markdown/html directly in message bubbles  
**AG-UI Opportunity:**
- Use **A2UI surfaces** for model responses instead of raw markdown
- Use A2UI components: `Table`, `DataList`, `Card`, `Progress`, `Table`, `StatusRow`, `MetricRow` for structured responses
- **Action** components for buttons with `onPress` handlers (requires enabling `Action` component)
- AG-UI event streaming for all responses (standardize on AG-UI event format)

**Example A2UI Surface for Chat Response:**
```json
{
  "id": "root", "type": "Column", "children": ["heading", "table", "actions"]
}
{ "id": "heading", "type": "Text", "variant": "heading", "text": { "$bind": "/title" } }
{ "id": "table", "type": "Table", "columns": [...], "rows": { "$bind": "/data" } }
{ "id": "actions", "type": "Row", "children": ["btn1", "btn2"] }
{ "id": "btn1", "type": "Button", "label": "Re-run", "onPress": { "action": "rerun", "args": { "$bind": "/rerun_args" } } }
```

---

### 2. Research View (`Research.tsx`) — **HIGH PRIORITY**
**Current:** Custom markdown extraction + markdown rendering  
**AG-UI Opportunity:**
- Render research reports as **A2UI surfaces** instead of markdown
- Use `MetricRow`, `Table`, `Sparkline`, `Card`, `StatusRow` for structured reports
- Live reasoning log → already uses AG-UI `ACTIVITY_SNAPSHOT` events
- Report sections → `Card` + `MetricRow` + `Table` + `Sparkline`

---

### 3. Documents View (`Documents.tsx`) — **MEDIUM PRIORITY**
**Current:** Custom table/list with manual sorting/filtering  
**AG-UI Opportunity:**
- Replace manual table with **A2UI `Table`** (search, sort, pagination built-in)
- Use `DataList` for document metadata display
- `Table` component already has search, sort, pagination built-in

---

### 4. Parts View (`Parts.tsx`) — **MEDIUM PRIORITY**
**Current:** Custom canvas + manual stats grid  
**AG-UI Opportunity:**
- Replace manual stats grid with **A2UI `MetricRow`** + `KeyValue`
- Use `Sparkline` for optimization history
- `Table` for pass history instead of custom list
- `Progress` for optimization progress

---

### 5. Computer View (`Computer.tsx`) — **MEDIUM PRIORITY**
**Current:** Custom desktop/screenshot rendering  
**AG-UI Opportunity:**
- Use A2UI `Card`, `Row`, `Column` for desktop layout
- `Progress` for operation progress
- `StatusRow` for status indicators
- `Action` components for approval buttons (requires enabling)

---

### 6. Chat — AG-UI Event Streaming — **HIGH PRIORITY**
**Current:** Custom SSE frame parsing in `chat_simple_stream`  
**AG-UI Opportunity:**
- Adopt **AG-UI event stream** format for all streaming
- Use `AguiEvent` types from `api.ts`
- Standardize on AG-UI event types:
  - `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT`, `TEXT_MESSAGE_END`
  - `TOOL_CALL_START`, `TOOL_CALL_ARGS`, `TOOL_CALL_END`
  - `STEP_STARTED`, `STEP_FINISHED`, `RUN_FINISHED`, `RUN_ERROR`

---

### 6. Runs / Ledger Views — **LOW PRIORITY**
- Run tables → A2UI `Table` with built-in search/sort/pagination
- Ledger entries → `DataList` or `Table`

---

## Critical Missing Piece: **Action Component Enablement**

The `Action` component in `A2ui.tsx` (lines 238-248) is **intentionally disabled** — it renders a disabled button with "Agent-generated actions are not enabled".

**To enable model-driven actions:**
1. Add `onAction` callback prop to `A2ui` component
2. Wire up action dispatch to agent's `computer_action` or custom action endpoints
3. Update `Action` case in `Leaf` to render enabled button with `onClick` handler

```tsx
// In A2ui.tsx - add to props
interface A2uiProps {
  surface: A2uiSurface;
  notes?: string[];
  onAction?: (action: string, args: Record<string, unknown>) => void;
}

// In Leaf component, case 'Action':
case 'Action':
  if (onAction) {
    return (
      <button
        type="button"
        onClick={() => onAction(c.props.action, c.props.args)}
        className="..."
      >
        {str(p.label)}
      </button>
    );
  }
  // fallback disabled state
```

---

## Implementation Priority Matrix

| Priority | View | Components | Effort | Dependencies |
|----------|------|------------|--------|--------------|
| **P0** | Enable `Action` component | `Action` enablement + `onAction` prop | Low | None |
| **P0** | Chat | A2UI surfaces, AG-UI streaming | High | Action enablement |
| **P0** | Research | A2UI surfaces, AG-UI events | High | Action enablement |
| **P1** | Documents | `Table`, `DataList` | Low | None |
| **P1** | Parts | `MetricRow`, `Sparkline`, `Table` | Low | None |
| **P2** | Computer | `Card`, `Row`, `Column`, `Progress` | Medium | Action enablement |
| **P2** | Chat Streaming | AG-UI event format | High | AG-UI types exist |
| **P2** | Runs/Ledger | `Table`, `DataList` | Low | None |

---

## Files to Modify

### Core Files
| File | Changes |
|------|---------|
| `console-frontend/src/components/A2ui.tsx` | Enable `Action` component, add `onAction` prop |
| `console-frontend/src/views/Chat.tsx` | Render A2UI surfaces, AG-UI streaming |
| `console-frontend/src/views/Research.tsx` | Render reports as A2UI surfaces |
| `console-frontend/src/views/Documents.tsx` | Replace table with A2UI `Table` |
| `console-frontend/src/views/Parts.tsx` | Use `MetricRow`, `Sparkline`, `Table` |
| `console-frontend/src/views/Computer.tsx` | Use A2UI layout components |
| `console-frontend/src/api.ts` | Extend `AguiEvent` types if needed |
| `console-frontend/src/components/A2ui.tsx` | Add `onAction` prop to `A2ui` component |

### New Files (Optional)
- `console-frontend/src/components/AGUIStream.tsx` — reusable AG-UI stream handler
- `console-frontend/src/hooks/useAGUIStream.ts` — hook for AG-UI event consumption

---

## AG-UI Event Types to Standardize On

From `api.ts` (extend as needed):
```typescript
interface AguiEvent {
  type: string;
  timestamp?: number;
  threadId?: string;
  runId?: string;
  stepName?: string;
  messageId?: string;
  activityType?: string;
  content?: { text?: string };
  result?: { app_id?: string; count?: number; error?: string | null };
  message?: string;
}
```

**Event Types to Standardize:**
| Event Type | Purpose |
|------------|---------|
| `TEXT_MESSAGE_START` | Start of text message |
| `TEXT_MESSAGE_CONTENT` | Streaming text chunk |
| `TEXT_MESSAGE_END` | End of text message |
| `TOOL_CALL_START` | Tool call initiated |
| `TOOL_CALL_ARGS` | Tool arguments streaming |
| `TOOL_CALL_END` | Tool call completed |
| `STEP_STARTED` | Step begun |
| `STEP_FINISHED` | Step completed |
| `RUN_FINISHED` | Run completed |
| `RUN_ERROR` | Run failed |

---

## Model Communication Protocol

Models should output A2UI surfaces as JSON in a structured format. Example:

```json
{
  "surface": {
    "surfaceId": "chat-response-123",
    "catalogId": "default",
    "rootComponent": "root",
    "components": [
      { "id": "root", "type": "Column", "props": { "children": ["heading", "table", "actions"], "gap": "normal" } },
      { "id": "heading", "type": "Text", "props": { "variant": "heading", "text": { "$bind": "/title" } } },
      { "id": "table", "type": "Table", "props": { "columns": [...], "rows": { "$bind": "/data" }, "searchable": true, "paginated": true } },
      { "id": "actions", "type": "Row", "props": { "children": ["btn1", "btn2"], "gap": "normal" } },
      { "id": "btn1", "type": "Button", "props": { "label": "Re-run", "onPress": { "action": "rerun", "args": { "$bind": "/rerun_args" } } } },
      { "id": "btn2", "type": "Button", "props": { "label": "Approve", "onPress": { "action": "approve", "args": { "$bind": "/approve_args" } } } }
    ]
  }
}
```

---

## Open Questions / Tradeoffs

1. **Action Execution Model**: Should actions be synchronous (blocking) or async with callbacks? Current A2UI renders disabled — need to decide async vs sync.

2. **Binding Syntax**: Use `$bind` for dynamic values (as shown above) or inline values? Current A2UI uses direct props; binding adds flexibility but complexity.

3. **Streaming vs Batch**: For chat, should A2UI surfaces stream incrementally or arrive as complete surface? AG-UI supports both.

4. **Backward Compatibility**: Non-A2UI chat responses must still render. Need fallback to markdown rendering.

5. **Catalog Versioning**: A2UI components are versioned via `catalogId`. Need versioning strategy for new components.

---

## Next Steps

1. **Approve plan** — confirm priorities and scope
2. **Enable `Action` component** in `A2ui.tsx` (P0, unblocks everything)
3. **Prototype Chat A2UI** — single message rendering as A2UI surface
4. **Prototype Research A2UI** — report rendering as surface
5. **Integrate AG-UI streaming** into chat pipeline
6. **Roll out to Documents, Parts, Computer**

---

## Estimated Effort

| Task | Effort |
|------|--------|
| Enable `Action` component | 1-2 days |
| Chat A2UI surface rendering | 3-5 days |
| Research A2UI surface rendering | 2-3 days |
| Documents `Table` integration | 1-2 days |
| Parts `MetricRow`/`Sparkline` integration | 1-2 days |
| Computer view A2UI integration | 2-3 days |
| AG-UI streaming integration | 3-5 days |
| **Total** | **~14-22 days** |

---

## Risks

| Risk | Mitigation |
|------|------------|
| Action security (model triggers arbitrary actions) | Whitelist allowed actions; require explicit approval for sensitive actions |
| Performance (A2UI rendering overhead) | Lazy-load surfaces; memoize heavily |
| Model hallucination of invalid components | Strict catalog validation server-side |
| Streaming complexity | Reuse existing SSE infrastructure; AG-UI events map 1:1 |

---

## Acceptance Criteria

- [ ] `Action` component enabled and callable from model
- [ ] Chat renders A2UI surfaces for structured responses
- [ ] Research reports render as A2UI surfaces
- [ ] Documents uses A2UI `Table` with search/sort/pagination
- [ ] Parts uses `MetricRow`, `Sparkline`, `Table`
- [ ] AG-UI event streaming works end-to-end for chat
- [ ] All existing tests pass; new tests added for A2UI rendering
- [ ] No regression in existing markdown/html rendering (fallback works)