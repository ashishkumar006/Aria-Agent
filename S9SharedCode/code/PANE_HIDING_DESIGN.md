# Console pane hiding — design

**Status:** design only. No implementation until "continue".
**Surveyed:** all 15 console sections except `/computer` (see §9).
**Feature flag:** `ui.pane_hiding` — one master switch, off means zero residue (§3).

---

## 1. What this is

Let a user remove panes they do not want from a console view, per view, reversibly, without me deciding for them. Three modes per pane, because "optional" turns out to be a property of the **controls inside** a pane, not of the pane:

| Mode | Meaning | Used for |
|---|---|---|
| **HIDE** | Pane is not rendered at all. Restore from the Layout control. | Panes whose content is duplicated elsewhere or purely decorative. |
| **COLLAPSE** | Pane shrinks to a thin labelled strip. Its controls stay reachable. | Panes that are visually heavy but hold a control that exists nowhere else. |
| **NEVER** | Not offered to the user. | Navigation, the primary input, tab strips, and every sole recovery/undo path. |

The COLLAPSE tier is what makes this safe. Without it, roughly a third of the panes that look hideable would strand the user — the survey found a lot of them hiding the only path to something.

---

## 2. Prerequisites — three things must land first

### 2.1 There are no stable selectors

**Zero `data-testid` attributes exist anywhere in `console-frontend/src`.** Verified across all views and `components/ui.tsx`, `Inspector.tsx`, `DagCanvas.tsx`. The only `data-*` hook in the area is `div[data-editor-host]` ([Code.tsx:103](file:///C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/console-frontend/src/views/Code.tsx#L103)).

Targeting is therefore down to `aria-label`, `role`, `id`, heading text and Tailwind className signatures. Several left rails have **no** `aria-label`, `role` or `id` at all and are identifiable only by a heading string — Runs' sidebar ("Runs"), Research' topics ("Topics"), Ledger' scope ("Scope"), Skills' capabilities ("Capabilities"), Scheduler' jobs ("Jobs"), Settings' environment ("Environment"), Mission' sources ("Sources"), Memory' kinds ("Memory types").

Heading text is not a contract; someone rewording "Scope" to "Filter" silently breaks a hidden-pane record. So every offerable pane gets an explicit hook:

```tsx
<Pane id="runs.list" label="Runs list" mode="collapse">   // lib/panes.tsx
```

`id` is a stable slug, never derived from visible text. This is the single biggest piece of work in the feature and it is mechanical.

### 2.2 Authoring's Discuss rail holds its own exit

The Documents rail in the Discuss workspace ([Authoring.tsx:1407-1442](file:///C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/console-frontend/src/views/Authoring.tsx#L1407)) contains **both** the only document switcher for a multi-file run **and** the only exit from Discuss mode — "Back to the run" at 1438-1441.

Hiding that rail as it stands traps the user in Discuss. So before it can be offered:

- move "Back to the run" into the tab strip, which already renders in both layouts via `tabStrip()` ([Authoring.tsx:1285](file:///C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/console-frontend/src/views/Authoring.tsx#L1285)) and is never hideable;
- then the rail becomes `collapse` (multi-file runs keep the switcher as icons) rather than `hide`.

This is also the direct answer to the open question on Discuss: you get to hide the rail yourself, and a run with one file never shows it.

### 2.3 Code already has the affordances and forgets them

`Code.tsx` already carries fifteen UI-visibility booleans — `zen` (908), `explorerOn` (890), `agentOn` (896), `agentOpen` (2004), `surface` (1003), `panel` (949), `outlineOn` (930), `minimapOn` (906), `wrap` (907), `previewMd` (1117), `find.open`, `search.open`, plus palette/goto/shortcuts/diff/intel.

**Only two of them survive a reload:** `aria.code.agentWidth` (81, read 901, written 1048) and `aria.code.surface` (1005/1009). The rest re-derive from `window.innerWidth` and reset.

So for `/code` the job is not to add a new mechanism — it is to persist what already exists and surface it in one place. Building a parallel pane-hiding layer over Code would give users two competing systems for the same panes.

---

## 3. The kill switch

You asked to be able to disable the whole feature later. The machinery already exists and needs one line of real work.

**Add to `DEFINITIONS` in [flags.py:27-136](file:///C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/flags.py#L27):**

```python
"ui.pane_hiding": {
    "type": "bool",
    "default": True,
    "description": "Let users hide or collapse non-essential console panes, per view.",
},
```

That is the entire backend change. It takes effect immediately because:

- `Settings.tsx:286` renders whatever `all_flags()` returns, so the toggle appears on the FLAGS & CONFIG board with no new UI;
- `state/flags.json` is a sparse override map — deleting the key returns to `default`;
- `_read_overrides()` re-reads the file on every call, so there is no cache to invalidate server-side.

**The residue requirement.** "Off with no residue" means: flag off ⇒ every pane renders exactly as today, the Layout control disappears, and the stored layout is preserved but ignored so re-enabling restores your arrangement. That is only achievable if the gate is read in **one place**. So:

```tsx
// lib/panes.tsx — the ONLY consumer of the flag
export function usePanes(routeId: string) {
  const enabled = useFlag("ui.pane_hiding");      // fail-open: unknown => true
  const state = useStoredLayout(routeId);          // read regardless
  if (!enabled) return { enabled: false, modeOf: () => "shown", toggle: () => {} };
  return { enabled: true, modeOf: (id) => state[id] ?? "shown", toggle: (id, next) => write(routeId, id, next) };
}
```

and `<Pane>` consults it. **No view may branch on the flag itself.** If hiding logic were spread through fifteen files, switching the feature off would mean editing fifteen files — which is the whole thing you were trying to avoid.

**Two known traps this design has to dodge:**

1. **The dead-flag precedent.** `apps.views.table`, `apps.views.cards` and `apps.views.prefab` are declared in `flags.py:67-84` and asserted in `tests/test_flags.py:68-73`, but **no production code reads them**. A flag that exists but is never consulted looks wired up and does nothing. §7 has tests specifically for this.
2. **The 30-second lag.** `api.flags()` ([api.ts:1349](file:///C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/console-frontend/src/api.ts#L1349)) passes no `ttlMs`, so it is served from the 30s read-through cache (`DEFAULT_TTL_MS`, `lib/getCache.ts:37`). Flipping the switch would leave the console in the old state for half a minute. The gate must request `cache: 'no-store'` — precedent at `api.trackerApp` ([api.ts:1356-1358](file:///C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/console-frontend/src/api.ts#L1356)).

**Fail-open, deliberately.** If flags cannot be read, the feature is treated as ON but the layout is treated as EMPTY, i.e. everything shows. A broken flag read must never hide a user's UI.

---

## 4. Mechanism

### 4.1 One store, copied from the rail

`components/ui.tsx:130-145` already solved the exact problem this feature has. The nav rail's pinned state lives at **module scope**, not component state, with a hand-rolled listener set consumed through `useSyncExternalStore` — the comment at 126-128 explains why: every KeepAlive section mounts its own `<Rail/>`, so per-component state desyncs across the fifteen simultaneously-mounted views.

Same constraint, same solution:

```
lib/panes.tsx
  PANE_REGISTRY        routeId -> PaneSpec[]        (curated, see §5)
  readLayout()         localStorage 'aria.panes.v1'
  writeLayout()        atomic-ish setItem, try/catch
  subscribe/notify     module-scope listeners
  usePanes(routeId)    the single gate + store hook (§3)
  <Pane id label mode> renders children, a collapse strip, or null
```

`KeepAliveHost` keeps every visited section mounted and hides rather than unmounts (`KeepAlive.tsx:126-146`, `hidden={!active}` + `display:none` + `aria-hidden` + `inert`), so a hidden pane in a background view must not be re-mounted on return — the `<Pane>` wrapper handles that by returning the same element tree.

### 4.2 Storage

```
key:   'aria.panes.v1'
shape: { "/authoring": { hidden: ["brief.suggestions"], collapsed: ["documents.rail"] }, ... }
```

- Keyed by **route id**, which is what `SECTIONS` ([App.tsx:136-152](file:///C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/console-frontend/src/App.tsx#L136)) and the KeepAlive host already use as their identity.
- Naming follows the dominant convention `aria.<view>.<thing>`; `.v1` suffix follows `aria.authoring.runs.v1` and `aria.code.drafts.v1`.
- Every access wrapped in `try { … } catch { return default }` — that is the uniform convention in this codebase, for private-mode safety.
- **Client-side, not server-side.** The survey confirms there is no per-user or per-session preference store anywhere: `flags.py` is single-global, `SessionStore` is per-run, and every existing UI preference in the console is localStorage. Layout is therefore per browser, consistent with how Chat mode, Code surface, voice and folder scope already behave.

### 4.3 The restore control — where it can never be hidden

Every view renders `<TopBar>` and the survey classifies it load-bearing in all of them, so the recovery affordance goes there:

```
TopBar:  [Layout ▾]   aria-label="Layout"   — rendered only when the flag is on
                      lists this route's panes with Hide/Show, plus "Reset this view"
```

It is mounted by `TopBar` itself, not by a `<Pane>`, so it cannot be hidden by its own feature. This mirrors `aria-label="Show navigation"` ([ui.tsx:408-418](file:///C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/console-frontend/src/components/ui.tsx#L408)) — the rail's restore button, which is the one place in this console where a user already hides chrome and reliably gets it back.

**One hole:** Code unmounts its entire TopBar in zen mode (`!zen &&` at [Code.tsx:2014](file:///C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/console-frontend/src/views/Code.tsx#L2014)). So in `/code` the Layout control must additionally appear in the status bar (which is *not* gated by zen and already hosts the only zen exit, 2842-2846) and as commands in the existing palette (`aria-label="Command palette"`, 2051-2056, whose `CMDS` list at 1565-1582 already holds Toggle Explorer/Minimap/Zen).

Collapse strips are self-restoring: a hidden-by-collapse pane leaves a labelled strip with `aria-label="Show {label}"`, so it never depends on the Layout control being reachable.

### 4.4 Accessibility constraints

- **Focus traps are safe.** `useFocusTrap` ([lib/a11y.ts:19](file:///C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/console-frontend/src/lib/a11y.ts#L19)) is used only by dialogs — DocumentModal:54, Chat:377, Code:800, Code:2935. No persistent pane owns a trap.
- **Tab strips are load-bearing and never hideable.** Roving-tabindex handling exists in six files: AgentPanel, Authoring, Code, DocSetup, Memory, Parts. Hiding a `role=tab` whose `role=tabpanel` is visible, or vice versa, breaks `aria-controls`/`aria-labelledby` and the Home/End walk (`TAB_ORDER`, `edgeTab` in Authoring). Tabs stay; panels are only hidden when the tab itself is also gone.
- **Duplicate labels are a real selector hazard.** `aria-label="Output view"` exists twice with different meanings — `role="group"` in Research:922 and `role="tablist"` in Authoring:1287. `"Stop the run"` appears in both Research:1131 and Authoring:1644. Two panes carry `id="panel-discuss"` in Authoring (1468 and 1919), only one mounted at a time. This is another reason §2.1's explicit `id` beats label-derived selection.
- **Landmarks.** Hiding the only `<nav>` or `<complementary>` in a view is acceptable; `id="main"` and the SkipLink are never hideable. `useMainId()` returns `'main'` only for the active section, so ids don't collide.
- **Live regions.** A pane is not offerable as HIDE while it is the only `role="status"`/`role="alert"` surface for an action. Several are: Authoring's `err` alert (1896-1900), Skills' `toggleError` (253-257), Parts' error alert (1125-1130), Apps' build-error (288-296), Mission's feed-error (121-127). All marked NEVER.

---

## 5. The registry

Curated from the survey. **NEVER is the default** — a pane appears here only because someone checked what lives inside it.

### /authoring
| Pane | id | Mode | Why |
|---|---|---|---|
| "Try one of these" | `brief.suggestions` | hide | Already a `<details>` (1669-1688), pure shortcuts |
| Warning / file-count pills | `output.pills` | hide | Summary chips (1327-1330) |
| Per-file meta line | `files.meta` | hide | `fileMeta` 1841-1848 |
| Run log tab panel | `output.log` | hide | Logs, also on Runs (1901-1917) |
| Pre-run empty state | `output.empty` | hide | 1804-1816 |
| Discuss Documents rail | `discuss.rail` | collapse | Only switcher + only exit — needs §2.2 first |
| Document modal | `output.modal` | hide | `Get` still downloads |
| Brief card, Recent documents, tab strip, Files rows, Preview/Result panels, err alert, Stop/Author | — | **never** | Sole input for the main action; only way back to a past paid document; only download path; only cancel |

### /research
| Pane | id | Mode | Why |
|---|---|---|---|
| SESSION metadata | `side.session` | hide | Duplicates sid at 1153; already `hidden lg:block` |
| Footer hint strip | `foot.hints` | hide | Gateway state duplicates the Rail dot |
| Status pill | `top.status` | hide | `note` says the same |
| Folder scope select | `composer.scope` | hide | Folders managed in Documents |
| Feedback widget | `turn.feedback` | hide | Supplementary |
| Topics sidebar | `side.topics` | collapse | Only way to open a saved topic |
| Files row | `out.files` | collapse | Only in-view artifact path |
| Composer, Stop/submit, answer/log strip, Stored report, Turn transcript, "+ New topic", "load older topics", note readout, **Research depth** | — | **never** | Depth is the only control for `depth`; hiding it silently pins every run to `standard` |

### /runs
| Pane | id | Mode | Why |
|---|---|---|---|
| Count badge, filters, totals chip, failure footer, hidden-shells footnote, Empty states | `list.*` | hide | Chips and filters |
| Runs list sidebar | `side.runs` | collapse | Only run selector |
| Inspector | `out.inspector` | collapse | Only node output + only one-click rerun |
| "load older runs", Stop this run, retry banners, graph pane, note line | — | **never** | Only pagination; only cancel; only recovery; note carries cancel outcome and graph failure |

### /code — persist, don't duplicate
| Pane | id | Mode | Why |
|---|---|---|---|
| Status text cluster, breadcrumbs bar, workspace search, Find widget, zen crumb, markdown preview | `code.*` | hide | Transient or duplicated by shortcuts |
| Explorer, Agent panel, Symbol outline, bottom panel | `code.*` | collapse | Explorer holds the only Open folder / New playground / New file / reset |
| Status bar, Open editors strip, editor, agent-changed Revert/Keep bar, stale-source notice, DiffView, EditPermissionDialog, surface switch, palette | — | **never** | Status bar hosts the only exit from zen; Revert/Keep is the undo path |

Work here is **persisting the existing fifteen booleans** into `aria.panes.v1` and adding palette commands, not new toggles.

### /parts
| Pane | id | Mode | Why |
|---|---|---|---|
| Metrics grid, stats line, viewport overlays, Backends block, slicer block, run log | `parts.*` | hide | 8 Stat cells repeat sidebar + History |
| View toolbar | `parts.viewbar` | collapse | **Only writer of the server-persisted `partSetView`** — hiding freezes camera/overlay |
| Saved parts sidebar, issues list | `parts.*` | collapse | Only part picker; only enumerated issue list |
| Header Viewport/Blender toggle + version select, inspector tab strip, Analyse/Repair/Optimise, History Restore, error alert, Import panel, Aria textarea + Plan | — | **never** | Restore is the only undo for a bad version; the Aria tab is the only part-scoped DAG input |

### /chat
| Pane | id | Mode | Why |
|---|---|---|---|
| Starter prompts, voice select/toggle/note, char counter, hints, sr-only help, Speak+Feedback row, jump-to-latest | `chat.*` | hide | Shortcuts and metadata |
| Threads rail, attach | `chat.*` | collapse | Only thread selector; upload duplicated in Documents |
| Engine switch, composer, send, Stop, thread rows + delete, retry banners, doc-scope select, Start new chat, queued chip | — | **never** | Engine switch is the only route to DAG threads; queued chip is the only surface/discard for `queued` |

### /memory
| Pane | id | Mode | Why |
|---|---|---|---|
| "hide superseded", provenance bar, sr-only live region | `mem.*` | hide | Filters and status |
| Kind panel | `mem.kinds` | collapse | Only way to isolate preferences |
| "WHAT WE'VE NOTICED" | `mem.noticed` | collapse | Contains the only Delete observation and Save as document |
| Tab strip, persona block, add-memory form + trigger, results list, **Load more**, card Forget, error alert | — | **never** | Load more is the only next page; Forget is delete with no undo |

### /documents
| Pane | id | Mode | Why |
|---|---|---|---|
| SEARCH RESULTS, index progress bar, blocked note, scoped chips, folderErr, "No folders yet" | `docs.*` | hide | Transient/duplicated |
| Folders rail, detail drawer | `docs.*` | collapse | Only create/rename/delete folder; only CHUNKS, parser warnings and folder-move |
| Upload zone, row action bar, retry, document list | — | **never** | Only ingestion; only enable/re-index/delete |

### /ledger
| Pane | id | Mode | Why |
|---|---|---|---|
| Stat row, refresh footer note | `led.*` | hide | Summary chips |
| Node reliability table, Turn history, Scope rail | `led.*` | collapse | Reliability has its own retry; Turn history is the only listing of turns; Scope is the only route to per-conversation spend |
| Spend by skill, conversation picker, retry banners | — | **never** | Primary content and recovery |

### /skills
| Pane | id | Mode | Why |
|---|---|---|---|
| **Capabilities rail**, rail footer, filter, Stat row | `skills.*` | hide | Clean duplicate of the pack grid + stats — genuinely safe |
| Pack grid, tools table, both headers, toggleError, guardError | — | **never** | Only pack and per-tool permission controls; only write-failure surface |

### /scheduler
| Pane | id | Mode | Why |
|---|---|---|---|
| **Jobs rail**, Stat row, footer note | `sched.*` | hide | Read-only duplicate of the jobs table, no actions |
| Create form, jobs table, headers, retry | — | **never** | Only job creation and enable/cancel/delete |

### /apps
| Pane | id | Mode | Why |
|---|---|---|---|
| Detail header card, actionError/detail.error lines | `apps.*` | hide | Metadata |
| Apps rail | `apps.rail` | collapse | Only way to open an existing app |
| TopBar refresh/delete, empty hero, A2UI surface, tracker table, build-error alert, "Building…" status, new-app modal | — | **never** | Build error and in-flight build are the only surfaces for background builds |

### /settings
| Pane | id | Mode | Why |
|---|---|---|---|
| Environment rail, config filter, config dump, gateway deep links, "No flags defined." | `set.*` | hide | Duplicated health readout and a raw dump |
| **FLAGS & CONFIG block**, API TOKEN card, flag error, agent-unreachable banner | — | **never** | The kill switch lives here — hiding it would hide the escape hatch; no token means the whole console 401s with no recovery |

### /mission
| Pane | id | Mode | Why |
|---|---|---|---|
| Stat row, filter, poll footer note | `mis.*` | hide | Counts feed lines held, not system totals (comment 108-110) |
| Sources rail | `mis.sources` | collapse | Only way to isolate `err` level |
| Pause/resume, feed-error alert, EVENT FEED | — | **never** | Resume is the only way to restart polling |

---

## 6. What this does to the Discuss question you asked

You asked whether the Documents rail should show in Discuss. Under this design you don't have to decide globally: it ships as `collapse`, so you can fold it to an icon strip or hide it outright, and a run that produced one file can hide it completely while a multi-file run keeps the switcher. §2.2 is the prerequisite that makes hiding it safe.

---

## 7. Tests

1. **Gate is actually consulted** — guards the `apps.views.*` dead-flag pattern. Assert `ui.pane_hiding` appears in `lib/panes.tsx` source, and behaviourally: flag on + pane hidden → pane absent; flag off → pane present.
2. **No residue when off** — with the flag off and a populated `aria.panes.v1`, every pane renders and no Layout control exists.
3. **Layout survives a disable/enable cycle** — off then on restores the same hidden set.
4. **Sole-control invariant** — for every registry entry with mode `hide`, assert its rendered content contains no `role="alert"` and no `aria-label` from a per-view protected list. This is the test that stops a future edit making the wrong pane hideable.
5. **Restore always reachable** — hide every offerable pane in all views; assert the Layout control still renders, and in `/code` with `zen` on assert it renders in the status bar.
6. **Flag read failure fails open** — `api.flags()` rejecting must show all panes.
7. **localStorage unavailable** — private mode returns "all shown", never throws.
8. **Tablist integrity** — no `role=tab` is hidden while its panel is visible, in the six files with roving tabindex.
9. **Code parity** — the existing fifteen toggles round-trip through `aria.panes.v1` and survive reload.

---

## 8. Rollout

| Slice | Contents | State |
|---|---|---|
| 1 | `lib/panes.tsx`, `<Pane>`, TopBar Layout control, flag, Authoring | **shipped** — 3 panes |
| 2 | Research, Runs, Parts | **shipped** — 5 + 7 + 5 panes, live-verified |
| 3 | Chat, Memory, Documents, Ledger, Skills, Scheduler, Apps, Settings, Mission, Computer | **shipped** — 34 panes |
| 4 | Code | **shipped as persistence, not new panes** — see below |

**54 panes across 14 views.** `/code` registers nothing on purpose: it already
had fifteen working visibility toggles and its palette already lists Toggle
Explorer/Minimap/Zen, so a second system would give users two controls for the
same pane. What it lacked was memory — only the agent's *width* and the chosen
surface survived a reload. `aria.code.ui.v1` now persists
`explorerOn, agentOn, minimapOn, wrap, outlineOn`. **`zen` is excluded
deliberately**: it unmounts the top bar and the tab strip, so a stale `true`
restored at the wrong moment is the one state that reads as a broken app.

Each slice is independently shippable and each is independently reversible with the flag.

**What changed while building, against this document:**

- `<Pane>` no longer takes a `mode` prop. The registry is the single source of
  truth, so a call site cannot drift from what the Layout control offers.
- Added `usePaneMode(id)` and an exported `PaneStrip`. Wrapping a 70-line sidebar
  in `<Pane>` means re-indenting 70 lines for no behavioural reason; the hook
  form lets the view branch at the block's two boundaries and leave the body
  byte for byte. Both read the same registry and gate, so they cannot disagree.
- The Run log is **not** offered in Authoring. It is a `role=tabpanel` whose
  visible `role=tab` still carries `aria-controls="panel-log"`, so hiding the
  panel alone leaves the tab pointing at nothing. Tabs and panels must leave
  together, which requires the tab strip itself to consult the registry.
- Parts' Backends block, Slicer result and agent run log are registered nowhere
  yet. A registry entry with no `<Pane>` around anything is a Layout row that
  says "hidden" while the pane stays on screen — worse than not offering it.
  Two tests now fail if that happens: `wraps every pane it advertises` and
  `mounts a Layout control in every view that registers panes`.
- Several panes this document listed as hideable were **dropped after
  inspection**, because "optional" is a property of the controls inside, not of
  the pane. The tool filter, the config text filter, the thread filter, the
  voice toggle, the character counter, "hide superseded" and the folder-error
  status line are each the only writer of their own state, or the target of an
  `aria-describedby` — hiding one would leave a value applied with nothing on
  screen to change it. Their `<Pane>` wrappers stay in the JSX as inert no-ops
  (an unregistered id renders unconditionally), so they go live if someone
  solves the stuck-state question later.
- `documents.results` and all three Memory panes were downgraded from hide to
  **collapse**: the results block is the only surface that ever shows search
  hits, and the observations block holds the only Delete observation.

---

## 9. Open items and honest gaps

- **`/computer` offers only three panes, and that is the correct outcome, not
  an oversight:** each of its three tab sections is the only route to its state
  (approvals, audit trail, recorded trajectories with the only replay), the
  refresh button is the only manual retry, the flash line is the sole
  confirmation of approve/reject/replay, and the DRIVER stat tile is the only
  place the capability detail appears — so the whole Stat row is not offered.
  The OFF and DRY-RUN banners share an identical className, so their boundary
  keys on text, not markup.
- **No server-side preference store exists.** Layout is per browser. If you
  later want it to follow the user across machines, that needs a new backend
  table — it is not a small flag.
- **Registry maintenance is the real cost.** Every new pane in every new view is
  a decision. The alternative — a universal click-to-remove picker — ships in a
  day and then converts every layout mistake into "some of my UI is missing and
  I can't explain it."
- **`collapse` needs a visual pass.** The strip is a 32px labelled column; it
  reads well beside a document or a sidebar and has not been looked at beside a
  wide toolbar or in the stacked sub-`lg` layout.
- **Pre-existing, unrelated:** `vitest run` at the repo root also loads the
  16 `e2e/*.spec.ts` Playwright specs and errors on every one, because
  Playwright's `describe` cannot run inside vitest — 18 files fail while zero
  tests fail. Needs a `test.exclude`.
- Nothing here changes Authoring's existing mode-driven hiding (`discussing` at
  1396) — that stays as is; this feature adds user-driven hiding on top.
