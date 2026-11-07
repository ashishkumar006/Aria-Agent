/** Per-pane hiding: users remove the console chrome they do not want.
 *
 *  Three facts shaped this file, and each is load-bearing enough that
 *  ignoring it produces a support mystery rather than a bug:
 *
 *  1. There is no stable selector to hang this on. The console has zero
 *     `data-testid` attributes, and several side rails carry no aria-label,
 *     role or id at all — only a heading string. So a pane is named by an
 *     explicit slug here, never derived from visible text: someone rewording
 *     "Scope" to "Filter" must not silently orphan a saved layout.
 *
 *  2. State lives at module scope, not in the component. KeepAliveHost keeps
 *     every visited section mounted and hides it with display:none, so each
 *     view has its own live `<Pane>` elements at all times. Per-component
 *     state would desync across them, and reading the current URL would make a
 *     background view apply the foreground view's layout. Both are the reason
 *     the nav rail's pin works the way it does (ui.tsx) and the reason the view
 *     slug comes out of the pane's own id.
 *
 *  3. The flag is consulted here and nowhere else. `ui.pane_hiding` off has to
 *     mean every pane renders exactly as before with no residue, and that is
 *     only true while no view branches on it. It also fails OPEN: a flag read
 *     that errors must never hide a user's UI.
 */
import { useSyncExternalStore, type ReactNode } from 'react';
import { PanelLeftClose } from 'lucide-react';
import { api } from '../api';

export const PANE_FLAG = 'ui.pane_hiding';
const LAYOUT_KEY = 'aria.panes.v1';

export type PaneMode = 'hide' | 'collapse';
export type PaneSpec = { id: string; label: string; mode: PaneMode };

/** What a user may remove, per view. Absent from this list means the pane is
 *  load-bearing and is never offered — the default, because the survey found
 *  that panes which look decorative frequently hold the only retry, undo,
 *  delete or pagination path in the view. */
export const PANE_REGISTRY: Record<string, PaneSpec[]> = {
  authoring: [
    { id: 'authoring.suggestions', label: '“Try one of these” examples', mode: 'hide' },
    { id: 'authoring.pills', label: 'Warning and file-count badges', mode: 'hide' },
    { id: 'authoring.discussRail', label: 'Documents rail in Discuss', mode: 'collapse' },
    // The Run log is deliberately absent. It is a role=tabpanel, and a visible
    // role=tab still carries aria-controls="panel-log", so hiding the panel
    // alone leaves the tab pointing at nothing. Tabs and their panels have to
    // leave together, which means the tab strip itself has to consult this
    // registry — a separate change, not something to half-do here.
  ],
  research: [
    { id: 'research.status', label: 'Run status pill', mode: 'hide' },
    { id: 'research.session', label: 'Session id block', mode: 'hide' },
    { id: 'research.hints', label: 'Footer hint strip', mode: 'hide' },
    // Collapse, not hide: the Files row holds the only in-view path to a
    // produced artifact, and the Topics list holds the only way to reopen a
    // saved topic, the only "+ New topic" and the only pagination.
    { id: 'research.files', label: 'Produced files row', mode: 'collapse' },
    { id: 'research.topics', label: 'Topics list', mode: 'collapse' },
    // Never offered: the composer (only input), Stop (only cancel), the
    // answer/log group, the stored report section, the note readout (it carries
    // the error and "stopped." text) and Research depth — hiding depth would
    // pin every run to `standard` with no way to change it from this view.
  ],
  runs: [
    { id: 'runs.badge', label: 'Run count badge', mode: 'hide' },
    { id: 'runs.filters', label: 'Filter and status filter', mode: 'hide' },
    { id: 'runs.totals', label: 'Totals chip', mode: 'hide' },
    { id: 'runs.shells', label: 'Empty-sessions footnote', mode: 'hide' },
    { id: 'runs.failures', label: 'Failure count footer', mode: 'hide' },
    // The list is the only run selector and the Inspector is the only place
    // node output and one-click "Run again" appear, so both collapse.
    { id: 'runs.list', label: 'Runs list', mode: 'collapse' },
    { id: 'runs.inspector', label: 'Node inspector', mode: 'collapse' },
  ],
  parts: [
    { id: 'parts.stats', label: 'Saved and versions count', mode: 'hide' },
    { id: 'parts.search', label: 'Part search box', mode: 'hide' },
    { id: 'parts.metrics', label: 'Metrics grid', mode: 'hide' },
    // The View toolbar is the only writer of the server-persisted part view,
    // and the sidebar is the only part picker — collapse keeps both reachable.
    { id: 'parts.viewbar', label: 'View toolbar', mode: 'collapse' },
    { id: 'parts.sidebar', label: 'Saved parts list', mode: 'collapse' },
    // Not yet offered, because a registry entry with no <Pane> around anything
    // is a toggle that does nothing: the Backends block, the Slicer result and
    // the agent run log still need wrapping.
  ],
  apps: [
    { id: 'apps.header', label: 'App detail header', mode: 'hide' },
    { id: 'apps.filter', label: 'Item filter', mode: 'hide' },
    // Collapse, not hide: the rail holds the only app-openers, the "New app"
    // button, the load retry and the in-flight build indicator.
    { id: 'apps.rail', label: 'Apps list', mode: 'collapse' },
  ],
  settings: [
    { id: 'settings.env', label: 'Environment column', mode: 'hide' },
    { id: 'settings.dump', label: 'Read-only config dump', mode: 'hide' },
    { id: 'settings.links', label: 'Gateway deep links', mode: 'hide' },
    // The FLAGS & CONFIG block is never offered: it holds this feature's own
    // master switch and the only revert path. The API TOKEN card is never
    // offered either — without it the whole console 401s with no way back.
    // The config text filter is out for the same reason as the tool filter:
    // it is the only writer of its own query, and the "N hits" readout only
    // exists while that query is set.
  ],
  mission: [
    { id: 'mission.stats', label: 'Feed line counters', mode: 'hide' },
    { id: 'mission.filter', label: 'Event filter', mode: 'hide' },
    { id: 'mission.note', label: 'Poll cadence note', mode: 'hide' },
    // Collapse: the level buttons are the only way to isolate errors.
    { id: 'mission.sources', label: 'Sources column', mode: 'collapse' },
  ],
  computer: [
    { id: 'computer.badge', label: 'Pending count badge', mode: 'hide' },
    { id: 'computer.footer', label: 'Apps and running counts', mode: 'hide' },
    { id: 'computer.banners', label: 'Off / dry-run notices', mode: 'hide' },
    // Almost nothing here is offerable. Each of the three tab sections is the
    // only route to its state, the refresh button is the only manual retry,
    // the flash line is the sole confirmation of approve/reject/replay, and
    // the DRIVER stat tile is the only place the capability detail appears.
  ],
  ledger: [
    { id: 'ledger.stats', label: 'Spend and call tiles', mode: 'hide' },
    { id: 'ledger.note', label: 'Poll cadence note', mode: 'hide' },
    { id: 'ledger.turns', label: 'Turn history', mode: 'hide' },
    // These two labels match the text hardcoded in each view's PaneStrip, so a
    // collapsed strip and its Layout row read the same way.
    { id: 'ledger.scope', label: 'Scope', mode: 'collapse' },
    { id: 'ledger.reliability', label: 'Node reliability', mode: 'collapse' },
    // Never offered: SPEND BY SKILL (primary content), the scope buttons, the
    // conversation picker and manual id input, and every retry banner.
  ],
  skills: [
    { id: 'skills.rail', label: 'Capabilities column', mode: 'hide' },
    { id: 'skills.stats', label: 'Pack and tool counts', mode: 'hide' },
    // Never offered: the toggleError and guardError alerts (the guard block
    // carries the only retry), the pack grid and the tools table, which are
    // the only permission controls in the app — and the tool filter, which is
    // the only writer of the query that narrows both lists. Hiding a filter
    // would leave the table filtered with nothing on screen to clear it.
  ],
  scheduler: [
    { id: 'scheduler.stats', label: 'Job counts', mode: 'hide' },
    // Collapse: the rail duplicates the jobs table but also holds the only
    // load retry, so the strip has to keep it reachable.
    { id: 'scheduler.rail', label: 'Jobs', mode: 'collapse' },
  ],
  chat: [
    { id: 'chat.starters', label: 'Starter prompts', mode: 'hide' },
    { id: 'chat.jump', label: 'Jump to latest', mode: 'hide' },
    // Collapse: the rail is the only route to a past conversation, and it
    // holds the engine switch, the delete buttons and the load retry.
    { id: 'chat.threads', label: 'Threads list', mode: 'collapse' },
    // Not offered: the thread filter, the voice toggle and the character
    // counter. Each is the only writer of its own state (or, for the counter,
    // the target of the textarea's aria-describedby), so hiding it would leave
    // a value on screen with nothing to change or describe it.
  ],
  memory: [
    // All three collapse. The kinds panel is the only way to isolate
    // preferences, the provenance bar carries "clear search", and the
    // observations block holds the only Delete observation and Save as document.
    { id: 'memory.kinds', label: 'Memory types', mode: 'collapse' },
    { id: 'memory.provenance', label: 'Search provenance bar', mode: 'collapse' },
    { id: 'memory.noticed', label: 'What we’ve noticed', mode: 'collapse' },
    // Not offered: the "hide superseded" checkbox (only writer of that
    // filter), the tab strip, the persona block, the add-memory form and its
    // trigger, the results list, Load more and every Forget button.
  ],
  documents: [
    { id: 'documents.progress', label: 'Index progress bar', mode: 'hide' },
    { id: 'documents.blocked', label: 'Blocked / per-file error note', mode: 'hide' },
    { id: 'documents.scope', label: 'Scope summary chips', mode: 'hide' },
    { id: 'documents.nofolders', label: 'Empty folders hint', mode: 'hide' },
    // Collapse, not hide: the results block is the only surface that ever
    // shows search hits, so hiding it would make the top bar's search look
    // dead. Same for the folders rail, which holds create/rename/delete.
    { id: 'documents.results', label: 'Search results', mode: 'collapse' },
    { id: 'documents.folders', label: 'Document folders', mode: 'collapse' },
    // Not offered: the folder-error status line (the only surface for a failed
    // create/rename/delete), the upload zone, the row action bar, the load
    // retry and the document list itself.
  ],
};

/* ------------------------------------------------------------------ layout */

type Layout = Record<string, { hidden: string[]; collapsed: string[] }>;

function readLayout(): Layout {
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object') return {};
    const out: Layout = {};
    for (const [view, st] of Object.entries(parsed as Record<string, unknown>)) {
      const o = st as { hidden?: unknown; collapsed?: unknown };
      out[view] = {
        hidden: Array.isArray(o?.hidden) ? o.hidden.filter((x): x is string => typeof x === 'string') : [],
        collapsed: Array.isArray(o?.collapsed) ? o.collapsed.filter((x): x is string => typeof x === 'string') : [],
      };
    }
    return out;
  } catch { return {}; }
}

let layout: Layout = readLayout();
const layoutListeners = new Set<() => void>();

function commitLayout(next: Layout): void {
  layout = next;
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(next)); } catch { /* storage off */ }
  for (const f of layoutListeners) f();
}

const subscribeLayout = (f: () => void) => { layoutListeners.add(f); return () => { layoutListeners.delete(f); }; };
const getLayout = () => layout;

/** Non-hook read of the stored layout, for callers outside React — the tests
 *  and any future import-time reconciliation. `usePaneLayout` is the reactive
 *  form; this one does not subscribe. */
export const peekLayout = (): Layout => layout;

/** `shown` is the absence of both records, so it clears rather than stores. */
export function setPaneState(id: string, state: 'shown' | 'hidden' | 'collapsed'): void {
  const view = id.split('.')[0];
  const cur = layout[view] || { hidden: [], collapsed: [] };
  const next: Layout = {
    ...layout,
    [view]: {
      hidden: state === 'hidden' ? [...new Set([...cur.hidden, id])] : cur.hidden.filter((x) => x !== id),
      collapsed: state === 'collapsed' ? [...new Set([...cur.collapsed, id])] : cur.collapsed.filter((x) => x !== id),
    },
  };
  commitLayout(next);
}

export function resetView(view: string): void {
  if (!layout[view]) return;
  const next = { ...layout };
  delete next[view];
  commitLayout(next);
}

/* -------------------------------------------------------------------- gate */

/** null = not yet known. Every consumer treats unknown as enabled. */
let gate: boolean | null = null;
const gateListeners = new Set<() => void>();

async function refreshGate(): Promise<void> {
  let next = true;
  try {
    const r = await api.flags(true);
    const row = (r.flags || []).find((f) => f.name === PANE_FLAG);
    // An absent row means an older backend: the feature behaves as designed.
    next = row ? row.value !== false : true;
  } catch {
    next = true;   // fail open: never hide UI because a read failed
  }
  if (gate !== next) {
    gate = next;
    for (const f of gateListeners) f();
  }
}

let gateStarted = false;
function subscribeGate(f: () => void) {
  gateListeners.add(f);
  if (!gateStarted) {
    gateStarted = true;
    void refreshGate();
    // Focus rather than an interval: the switch is flipped in Settings in this
    // tab or another one, and the next look at the console should reflect it.
    window.addEventListener('focus', () => { void refreshGate(); });
  }
  return () => { gateListeners.delete(f); };
}
const getGate = () => gate !== false;

export function usePanesEnabled(): boolean {
  return useSyncExternalStore(subscribeGate, getGate, getGate);
}

export function usePaneLayout(): Layout {
  return useSyncExternalStore(subscribeLayout, getLayout, getLayout);
}

/* ------------------------------------------------------------------ <Pane> */

/** A collapsed pane leaves a strip behind on purpose. A pane that vanishes
 *  with no trace depends on the Layout control being reachable to get back,
 *  and Code's zen mode unmounts its whole top bar — so collapse is self-restoring
 *  and hide is the mode reserved for content duplicated elsewhere. */
export function PaneStrip({ id, label }: { id: string; label: string }) {
  return (
    <button
      type="button"
      onClick={() => setPaneState(id, 'shown')}
      title={`Show ${label}`}
      aria-label={`Show ${label}`}
      className="flex w-8 flex-none flex-col items-center gap-2 rounded-[10px] border
                 border-white/10 bg-[#0e0e12] py-2 text-zinc-500 transition-colors
                 hover:border-violet-400/50 hover:text-zinc-200"
    >
      <PanelLeftClose size={12} className="flex-none rotate-180" />
      <span className="text-[10.5px] font-semibold [writing-mode:vertical-rl]">{label}</span>
    </button>
  );
}

/** The current state of one pane, with the flag already applied.
 *
 *  `<Pane>` is the right shape for a small block. For a whole sidebar — sixty
 *  or seventy lines of existing JSX — wrapping it would re-indent the entire
 *  block for no behavioural reason, which buries a two-line change in noise and
 *  invites a transcription error. This hook lets the view keep the block byte
 *  for byte and branch at its two boundaries instead. Both read the same
 *  registry and the same gate, so neither can drift from the other.
 */
export function usePaneMode(id: string): 'shown' | 'hidden' | 'collapsed' {
  const view = id.split('.')[0];
  const enabled = usePanesEnabled();
  const stored = usePaneLayout();
  if (!enabled) return 'shown';
  const spec = (PANE_REGISTRY[view] || []).find((p) => p.id === id);
  if (!spec) return 'shown';
  const st = stored[view];
  if (st?.hidden?.includes(id)) return 'hidden';
  if (spec.mode === 'collapse' && st?.collapsed?.includes(id)) return 'collapsed';
  return 'shown';
}

export function Pane({ id, children }: {
  id: string;
  /** How this pane may be removed comes from PANE_REGISTRY alone, so a call
   *  site cannot drift away from what the Layout control offers. An id that is
   *  not registered renders unconditionally. */
  children: ReactNode;
}) {
  const state = usePaneMode(id);
  // There is deliberately no wrapper element when shown. These panes are flex
  // children of their view's layout, so an extra layer would change the very
  // arrangement the user is trying to adjust — and would do so only when the
  // flag was off, which is the definition of residue.
  if (state === 'hidden') return null;
  if (state === 'collapsed') {
    const spec = (PANE_REGISTRY[id.split('.')[0]] || []).find((p) => p.id === id);
    return <PaneStrip id={id} label={spec?.label || id} />;
  }
  return <>{children}</>;
}

/* -------------------------------------------------------------- LayoutMenu */

/** The restore control. Mounted by the view's top bar, never by a `<Pane>`,
 *  so the feature cannot hide its own escape hatch. A `<details>` rather than
 *  a modal: this console's rule is no blocking dialogs, and the flag board in
 *  Settings already uses this shape. */
export function LayoutMenu({ view }: { view: string }) {
  const enabled = usePanesEnabled();
  const stored = usePaneLayout();
  const specs = PANE_REGISTRY[view] || [];
  if (!enabled || specs.length === 0) return null;

  const st = stored[view] || { hidden: [], collapsed: [] };
  const off = st.hidden.length + st.collapsed.length;

  return (
    <details className="relative">
      <summary
        title="Choose which panes this view shows"
        className={`flex h-6 cursor-pointer list-none items-center gap-1 rounded-md px-1.5
                   text-[11px] transition-colors hover:bg-glass-hover
                   ${off ? 'border border-white/10 text-zinc-200' : 'text-zinc-500'}`}
      >
        <PanelLeftClose size={12} className="flex-none" />
        Layout
        {off > 0 && <span className="tabular-nums text-violet-300">{off}</span>}
      </summary>
      <div className="absolute right-0 z-30 mt-1 w-[268px] rounded-[10px] border border-white/10
                      bg-[#101014] p-2 shadow-xl">
        <p className="mb-1.5 px-1 text-[10.5px] uppercase tracking-[0.12em] text-zinc-muted">
          Panes in this view
        </p>
        <ul className="space-y-1">
          {specs.map((p) => {
            const hidden = st.hidden.includes(p.id);
            const collapsed = st.collapsed.includes(p.id);
            const shown = !hidden && !collapsed;
            const verb = p.mode === 'collapse' ? (shown ? 'Collapse' : 'Show') : (shown ? 'Hide' : 'Show');
            return (
              <li key={p.id}>
                <button
                  type="button"
                  onClick={() => setPaneState(p.id, shown ? (p.mode === 'collapse' ? 'collapsed' : 'hidden') : 'shown')}
                  className="flex w-full items-center gap-1.5 rounded-md px-1.5 py-1 text-left
                             hover:bg-white/5"
                >
                  <span className="min-w-0 flex-1 truncate text-[11.5px]">{p.label}</span>
                  <span className={`flex-none text-[10px] ${shown ? 'text-zinc-500' : 'text-violet-300'}`}>
                    {shown ? 'shown' : hidden ? 'hidden' : 'collapsed'}
                  </span>
                  <span className="flex-none rounded border border-white/10 px-1 text-[10px] text-zinc-400">
                    {verb}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
        {off > 0 && (
          <button
            type="button"
            onClick={() => resetView(view)}
            className="mt-1.5 w-full rounded-md border border-white/10 px-1.5 py-1 text-[11px]
                       text-zinc-300 hover:border-violet-400/50"
          >
            Reset this view
          </button>
        )}
        <p className="mt-1.5 px-1 text-[10px] leading-snug text-zinc-600">
          Saved in this browser. Navigation, tabs and the main input are never
          listed here.
        </p>
      </div>
    </details>
  );
}
