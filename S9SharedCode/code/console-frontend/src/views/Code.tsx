/* Code — a browser editor over whatever folder you point it at.

   Scope, stated plainly because it matters:

   * The editor starts on an empty scratch workspace; Open Folder or New
     Playground re-roots it at a local directory (`/api/code/*`). Reads are
     path-confined to the workspace. The coding agent CAN write, and its edits
     apply straight to disk — each one is marked in the editor for a Keep or
     Revert review afterwards; the editor's own Save pays a confirm of its own.
   * Edits live in this browser tab (and localStorage, so a reload does not
     lose them). A dirty tab can be reverted to what the agent actually serves.
   * There is no language server: no diagnostics, completion or go-to-definition.
     The Problems panel says so instead of showing a fake zero.

   What it does have is the editor ergonomics that make VS Code pleasant:
   tabs, quick open, a command palette, find/replace with counts, go-to-line,
   a minimap, breadcrumbs, indent/comment/move-line/delete-line, multi-cursor
   via select-next-occurrence, bracket closing, and a real status bar.
*/

import {
  useCallback, useEffect, useMemo, useRef, useState, type ReactNode,
} from 'react';
import {
  ChevronDown, ChevronRight, Circle, CircleCheck, Code2,
  Eye, FileCode2, FileText, Folder, FolderPlus, FolderOpen, Globe, Loader2,
  Maximize2,
  PanelLeftClose, PanelLeftOpen, Play, Save, Search, Square, Terminal,
  Braces, TriangleAlert, WrapText, X, Keyboard, ListTree, CircleDot, Sparkles,
  ShieldQuestion, FilePlus,
} from 'lucide-react';
import { Rail, TopBar, Empty, Pill, SkipLink } from '../components/ui';
import { useFocusTrap } from '../lib/a11y';
import { useMainId, useSectionActive } from '../components/KeepAlive';
import { parseUnifiedMarks, type DiffMarks } from '../lib/diffMarks';
import { renderMarkdown } from '../components/markdown';
import MonacoEditor, {
  type CaretInfo, type EditorHandle, type EditorOp,
} from '../components/MonacoEditor';
import AgentPanel from '../components/AgentPanel';
import { EditPermissionDialog, type PendingEdit } from '../components/EditPermissionDialog';
import TerminalDock, { type TerminalStatus } from '../components/TerminalDock';
import {
  DiffView, IntelModal, lineDiff, useWordAtCursor,
} from '../components/CodeIntel';

/* A draft only ever exists in this browser - the server never receives it - so
   before anything destructive touches it, the user gets a copy they can keep.
   A revoke-style data URL rather than a blob URL: nothing to revoke, and it
   cannot outlive the click. */
function downloadText(name: string, text: string) {
  const url = `data:text/plain;charset=utf-8,${encodeURIComponent(text)}`;
  const a = document.createElement('a');
  a.href = url;
  a.download = name.replace(/\\/g, '_');
  a.click();
}

import { symbols as symbolsOf, type Symbol } from '../lib/lang';
import { useHistory, type Snapshot } from '../lib/useHistory';
import {
  api, type CodeEntry, type CodeFile, type CodeProblem, type CodeMatch,
  type CodeChecks,
} from '../api';

const STORE_KEY = 'aria.code.drafts.v1';
/* The agent's disk-writing tools (agent_server.py's own list). A successful
   result from any of these means the explorer's cached listings are stale. */
const WRITE_TOOLS = new Set([
  'create_file', 'update_file', 'edit_file', 'delete_file', 'apply_symbol_edit',
]);
const LANG_LABEL: Record<string, string> = {
  python: 'Python', typescript: 'TypeScript', typescriptreact: 'TypeScript React',
  javascript: 'JavaScript', json: 'JSON', yaml: 'YAML', toml: 'TOML',
  markdown: 'Markdown', css: 'CSS', html: 'HTML', shell: 'Shell', sql: 'SQL',
  text: 'Plain Text', ini: 'Config',
};

/* Is the focus inside the editor? Answered by containment, not identity:
   Monaco renders its own textarea inside the host div, so there is no node of
   ours to compare against. `e.target === ta.current` was the old way. */
const ED_HOST = 'div[data-editor-host]';
function edInFocus(t: EventTarget | null): boolean {
  if (!(t instanceof Node)) return false;
  const host = document.querySelector(ED_HOST);
  return !!host && host.contains(t);
}

/*  small helpers  */

/* The agent trace, boiled down to one line a human will read. The tool's full
   arguments and full result are NOT this: a JSON dump in the activity list is
   noise, and the identifying argument plus the outcome is what actually
   explains what happened. */
function briefArgs(args: unknown): string {
  if (!args || typeof args !== 'object') return '';
  const a = args as Record<string, unknown>;
  for (const k of ['path', 'pattern', 'file', 'name', 'query', 'symbol']) {
    if (typeof a[k] === 'string') return a[k] as string;
  }
  const first = Object.values(a).find((v) => typeof v === 'string');
  return typeof first === 'string' ? first.slice(0, 60) : '';
}

function outcomeBrief(r: { result?: string; detail?: string; error?: string }): string {
  const raw = String(r.error || r.detail || r.result || '');
  try {
    const o = JSON.parse(raw) as Record<string, unknown>;
    const bits: string[] = [];
    if (o.truncated === true) {
      bits.push(`truncated, lines ${o.start_line}-${o.end_line} of ${o.line_count}`);
    }
    if (Array.isArray(o.hits)) bits.push(`${o.hits.length} hit(s)`);
    if (typeof o.line === 'number') bits.push(`line ${o.line}`);
    if (typeof o.definition === 'string') bits.push(o.definition);
    if (typeof o.next_offset === 'number') bits.push(`continue at ${o.next_offset}`);
    if (typeof o.count === 'number') bits.push(`${o.count} match(es)`);
    return bits.join(' · ');
  } catch {
    return raw.replace(/\s+/g, ' ').trim().slice(0, 70);
  }
}

function loadDrafts(): Record<string, string> {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    const o = raw ? JSON.parse(raw) : {};
    return o && typeof o === 'object' ? (o as Record<string, string>) : {};
  } catch { return {}; }
}

function saveDrafts(d: Record<string, string>): boolean {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(d));
    return true;
  } catch { return false; /* quota or private mode: drafts stay in memory */ }
}

/** Subsequence match with a bonus for consecutive runs and word starts, the
    same shape of heuristic VS Code's quick open uses. Returns null when the
    needle is not a subsequence at all. */
function fuzzy(needle: string, hay: string): number | null {
  if (!needle) return 0;
  const n = needle.toLowerCase();
  const h = hay.toLowerCase();
  let score = 0;
  let hi = 0;
  let streak = 0;
  for (let ni = 0; ni < n.length; ni++) {
    const ch = n[ni];
    if (ch === ' ') { streak = 0; continue; }
    const found = h.indexOf(ch, hi);
    if (found === -1) return null;
    if (found === hi && ni > 0) { streak++; score += 6 + streak * 2; }
    else { streak = 0; score += 1; }
    if (found === 0 || /[\s/._-]/.test(h[found - 1] ?? '')) score += 8;
    hi = found + 1;
  }
  // Prefer shorter targets when scores tie: "app.py" should beat
  // "console-frontend/src/App.tsx" for the query "app".
  return score - Math.min(hay.length, 60) * 0.12;
}

/*  editor state  */

type Pane = { path: string; file: CodeFile; body: string; base: string };

interface Cursor { line: number; col: number }

function offsets(text: string): number[] {
  const starts = [0];
  for (let i = 0; i < text.length; i++) if (text[i] === '\n') starts.push(i + 1);
  return starts;
}

function posOf(text: string, off: number): Cursor {
  const starts = offsets(text);
  let lo = 0;
  let hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= off) lo = mid; else hi = mid - 1;
  }
  return { line: lo + 1, col: off - starts[lo] + 1 };
}

function offsetOf(text: string, c: Cursor): number {
  const starts = offsets(text);
  const i = Math.max(0, Math.min(starts.length - 1, c.line - 1));
  return Math.max(0, Math.min(text.length, starts[i] + c.col - 1));
}

/*  explorer  */

function Explorer({
  onOpen, activePath, onWorkspaceChange, reloadAt, changed,
}: {
  onOpen: (p: string) => void; activePath: string | null;
  onWorkspaceChange?: () => void;
  /** Bumped when the workspace changed on disk outside this component - a
   *  re-root, or the agent writing files. Re-reads the tree from disk. */
  reloadAt?: number;
  /** Client paths with an unreviewed agent change: a dot marks the row, so
   *  the user can see at a glance where the agent has been. */
  changed?: Set<string>;
}) {
  const [tree, setTree] = useState<Record<string, CodeEntry[]>>({});
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [roots, setRoots] = useState<string[]>([]);
  const [wsName, setWsName] = useState('');
  const [wsDefault, setWsDefault] = useState(true);
  const [wsBusy, setWsBusy] = useState(false);
  const [ask, setAsk] = useState(false);
  const [askPath, setAskPath] = useState('');
  const [newFile, setNewFile] = useState(false);
  const [newName, setNewName] = useState('');
  const [newBusy, setNewBusy] = useState(false);

  /* Read the live workspace: its roots (the tree's top level) and its
     name. Both come from the server, never a literal, so an Open Folder
     shows up here without a reload. */
  const refreshRoots = useCallback(async () => {
    const r = await api.codeRoots();
    if (r?.roots) setRoots(r.roots.map((x) => x.name));
    try {
      const w = await api.codeWorkspace();
      setWsName(w?.name || '');
      setWsDefault(w?.is_default !== false);
    } catch { /* the name is decoration; the roots are load-bearing */ }
  }, []);

  useEffect(() => {
    let live = true;
    refreshRoots().catch((e) => {
      /* Not swallowed: a failed root fetch used to leave the explorer on
         "reading workspace" forever, so broken and slow looked alike. */
      if (live) setErr(e instanceof Error ? e.message : 'could not read the workspace');
    });
    return () => { live = false; };
  }, [refreshRoots]);

  /* Point the workspace at a folder, or a fresh playground, then re-read.
     The tree and open-set are cleared first: keeping the old keys would
     make the reload effect walk directories that no longer exist. */
  const switchWs = useCallback(async (run: () => Promise<unknown>) => {
    setWsBusy(true);
    try {
      await run();
      setTree({});
      setOpen(new Set());
      await refreshRoots();
      setErr(null);
      /* The parent owns the quick-open index; it re-fetches on this. */
      onWorkspaceChange?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'could not open that folder');
    } finally { setWsBusy(false); }
  }, [refreshRoots, onWorkspaceChange]);

  /* Re-read the workspace after a failed root fetch. Without this the amber
     error just sat there with no way back short of a full reload. */
  const retryExplorer = useCallback(() => {
    setErr(null);
    refreshRoots().catch((e) =>
      setErr(e instanceof Error ? e.message : 'could not read the workspace'));
  }, [refreshRoots]);

  const load = useCallback(async (dir: string) => {
    setBusy(true);
    try {
      const r = await api.codeTree(dir);
      if (r?.error) setErr(r.error);
      else if (r?.entries) { setTree((t) => ({ ...t, [dir]: r.entries! })); setErr(null); }
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'could not read the workspace');
    } finally { setBusy(false); }
  }, []);

  useEffect(() => {
    for (const r of roots) {
      void load(r);
      setOpen((s) => new Set(s).add(r));
    }
  }, [roots, load]);

  /* Which directories are expanded, readable from effects that must not
     re-run (and re-load) every time the user toggles one. */
  const openRef = useRef(open);
  useEffect(() => { openRef.current = open; }, [open]);

  /* The agent writes straight to disk, outside this component's own create /
     save paths, so nothing re-read the tree: a file the agent had just
     created stayed invisible in the explorer (a folder already showing
     "empty" kept saying "empty") until the user collapsed and re-expanded
     it. The parent bumps `reloadAt` when a turn touched files; re-read the
     roots and every expanded directory. */
  useEffect(() => {
    if (!reloadAt) return;
    let live = true;
    (async () => {
      await refreshRoots().catch(() => { /* the reload below still helps */ });
      if (!live) return;
      for (const d of openRef.current) await load(d);
    })();
    return () => { live = false; };
  }, [reloadAt, refreshRoots, load]);

  /* Create one empty file and open it, so "New file" leaves the user somewhere
     to type. The console could not create anything before: a fresh playground
     is empty, "Open folder" needs a folder that already exists, and
     `/api/code/write` only saves a buffer that was already open. */
  const createFile = useCallback(async () => {
    const name = newName.trim();
    if (!name || newBusy) return;
    setNewBusy(true);
    try {
      /* Paths here are "root/sub/file" - `_code_abs` resolves against the
         workspace base, not against a root - so the file goes inside the first
         root the explorer is showing. A bare name resolved to the base, which
         is the parent of the roots and rejected as "outside the workspace". */
      const root = roots[0] || '';
      const rel = root ? `${root}/${name}` : name;
      const r = await api.codeNewFile(name, root);
      if (r?.error) setErr(r.error);
      else {
        setNewFile(false);
        setNewName('');
        setTree({});        // drop the cached listing so the entry appears
        setOpen((s) => new Set(s).add(root || '.'));
        await refreshRoots();
        await load(root || '.');
        onOpen(rel);
        setErr(null);
      }
    } catch (e) {
      const msg = (e as { message?: string })?.message || String(e);
      setErr(msg.replace(/^HTTP \d+:\s*/, ''));
    } finally { setNewBusy(false); }
  }, [newName, newBusy, roots, refreshRoots, load, onOpen]);

  const toggle = useCallback((dir: string) => {
    setOpen((s) => {
      const n = new Set(s);
      if (n.has(dir)) n.delete(dir); else { n.add(dir); void load(dir); }
      return n;
    });
  }, [load]);

  /* Auto-expand the folders on the path to the file the user just
     opened. Expanding alone is not enough: those dirs' entries
     were never fetched, so each one rendered "empty" until the
     user collapsed it and opened it again by hand. */
  useEffect(() => {
    if (!activePath) return;
    const parts = activePath.split('/');
    /* Only auto-open paths that are actually reachable. The roots are the
       workspace's own top-level folders, so a file at `code_workspace/a.py`
       yields the candidate `code_workspace` - reachable. For an opened
       folder, a file at `repo/src/x.py` yields `repo` - which is inside no
       root. Requesting it returned "path outside the code workspace", the
       error stuck forever (the dir never enters `tree`, so `!(d in tree)`
       stayed true and every tree change re-fired it), and it MASKED genuine
       failures: a 503 from /api/code/roots was correctly shown until opening
       a file replaced it with this. */
    const roots = Object.keys(tree);
    const need: string[] = [];
    for (let i = 1; i < parts.length; i++) {
      const d = parts.slice(0, i).join('/');
      const reachable = roots.length === 0
        || roots.some((r) => d === r || d.startsWith(`${r}/`));
      if (reachable) need.push(d);
    }
    setOpen((s) => {
      const n = new Set(s);
      let grew = false;
      for (const d of need) if (!n.has(d)) { n.add(d); grew = true; }
      return grew ? n : s;
    });
    for (const d of need) {
      if (!(d in tree)) void load(d);
    }
  }, [activePath, tree, load]);

  useEffect(() => {
    for (const d of Object.keys(tree)) void load(d);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [Object.keys(tree).length]);

  const renderDir = (dir: string, depth: number): ReactNode => (
    <DirNode
      key={dir}
      name={dir.split('/').pop() ?? dir}
      path={dir}
      depth={depth}
      open={open}
      toggle={toggle}
      tree={tree}
      busy={busy}
      onOpen={onOpen}
      activePath={activePath}
      changed={changed}
    />
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-none items-center gap-1.5 px-3 py-2 text-[10.5px] font-bold uppercase tracking-[0.14em] text-zinc-muted">
        Explorer
        <button
          type="button"
          title="Open a folder as the workspace"
          aria-label="Open folder"
          onClick={() => { setAskPath(''); setAsk(true); }}
          disabled={wsBusy}
          className="ml-auto rounded p-0.5 text-zinc-muted transition-colors hover:text-accent disabled:opacity-40"
        >
          <FolderOpen size={13} />
        </button>
        <button
          type="button"
          title="New empty playground"
          aria-label="New playground"
          onClick={() => void switchWs(() => api.newCodePlayground())}
          disabled={wsBusy}
          className="rounded p-0.5 text-zinc-muted transition-colors hover:text-accent disabled:opacity-40"
        >
          <FolderPlus size={13} />
        </button>
        {/* New file: the console had no way to create one (see createFile). */}
        <button
          type="button"
          title="New file in the workspace"
          aria-label="New file"
          onClick={() => { setNewName(''); setNewFile(true); }}
          disabled={wsBusy}
          className="rounded p-0.5 text-zinc-muted transition-colors hover:text-accent disabled:opacity-40"
        >
          <FilePlus size={13} />
        </button>
        {(busy || wsBusy) && <span className="text-[10px] normal-case text-accent/70">reading</span>}
        {/* The workspace name used to sit on its own line above the tree, at
            tree-row size and directly over `code_workspace`. It read as an
            extra, empty folder level. Reset now lives here; the root row below
            already names the folder. */}
        {!wsDefault && (
          <button
            type="button"
            onClick={() => void switchWs(() => api.resetCodeWorkspace())}
            title={`Reset ${wsName || 'this workspace'} to a fresh playground`}
            className="flex-none normal-case tracking-normal text-zinc-muted transition-colors hover:text-zinc-200"
          >
            reset
          </button>
        )}
      </div>
      {newFile && (
        <form
          className="flex flex-none items-center gap-1.5 px-3 pb-1.5"
          onSubmit={(e) => { e.preventDefault(); void createFile(); }}
        >
          <input
            autoFocus
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape') { setNewFile(false); setNewName(''); } }}
            placeholder="notes.md"
            aria-label="New file name"
            className="min-w-0 flex-1 rounded border border-white/15 bg-black/40 px-2 py-1 text-xs normal-case text-zinc-100 outline-none focus:border-accent"
          />
          <button
            type="submit"
            disabled={newBusy || !newName.trim()}
            className="rounded bg-accent px-2 py-1 text-[10.5px] font-bold text-black disabled:opacity-40"
          >
            {newBusy ? 'creating…' : 'Create'}
          </button>
          <button
            type="button"
            onClick={() => { setNewFile(false); setNewName(''); }}
            className="rounded px-1.5 py-1 text-[10.5px] normal-case text-zinc-muted hover:text-zinc-200"
          >
            cancel
          </button>
        </form>
      )}
      {ask && (
        <div className="flex flex-none items-center gap-1 px-3 pb-2">
          <input
            autoFocus
            value={askPath}
            onChange={(e) => setAskPath(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') { e.preventDefault(); setAsk(false); }
              if (e.key === 'Enter') {
                e.preventDefault();
                const p = askPath.trim();
                if (p) { setAsk(false); void switchWs(() => api.openCodeWorkspace(p)); }
              }
            }}
            placeholder="folder path, e.g. C:\\dev\\my-repo"
            aria-label="Folder to open"
            className="min-w-0 flex-1 rounded border border-glass-border bg-surface-2 px-2 py-1 text-[11px] normal-case text-zinc-100 outline-none focus:border-accent/60"
          />
          <button
            type="button"
            onClick={() => {
              const p = askPath.trim();
              if (p) { setAsk(false); void switchWs(() => api.openCodeWorkspace(p)); }
            }}
            className="flex-none rounded px-1.5 py-1 text-[10.5px] normal-case text-accent transition-colors hover:bg-accent/10"
          >
            Open
          </button>
        </div>
      )}
      {err && (
        <p className="px-3 py-1.5 text-[11px] text-amber-400/90">
          {err}{' '}
          <button onClick={retryExplorer} className="underline">retry</button>
        </p>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto pb-2">
        <ul>{roots.map((r) => renderDir(r, 0))}</ul>
        {!roots.length && !err && (
          <p className="px-3 py-2 text-[11px] text-zinc-muted">reading workspace</p>
        )}
      </div>
    </div>
  );
}

/* One recursive node for both folders and files.

   These were two components at first — a `renderDir` closure for the roots and
   a `DirRow` for everything nested — and `DirRow` returned null for files, so
   any file below the top level of a root was listed nowhere and could not be
   opened at all. One node type renders both, so a file cannot go missing. */
function DirNode({
  name, path, depth, open, toggle, tree, onOpen, activePath, busy, changed,
}: {
  name: string; path: string; depth: number; open: Set<string>;
  toggle: (d: string) => void; tree: Record<string, CodeEntry[]>;
  onOpen: (p: string) => void; activePath: string | null; busy: boolean;
  changed?: Set<string>;
}) {
  const isOpen = open.has(path);
  return (
    <li>
      <button
        type="button"
        onClick={() => toggle(path)}
        aria-expanded={isOpen}
        title={path}
        className="flex w-full items-center gap-1.5 rounded py-[3px] pr-2 text-left text-[12px] text-zinc-300 hover:bg-glass-hover"
        style={{ paddingLeft: 8 + depth * 12 }}
      >
        {isOpen
          ? <ChevronDown size={12} className="flex-none text-zinc-muted" />
          : <ChevronRight size={12} className="flex-none text-zinc-muted" />}
        {isOpen
          ? <FolderOpen size={12} className="flex-none text-zinc-500" />
          : <Folder size={12} className="flex-none text-zinc-500" />}
        <span className="truncate">{name}</span>
      </button>
      {isOpen && (
        <ul>
          {(tree[path] ?? []).map((e) => {
            const child = `${path}/${e.name}`;
            return e.dir ? (
              <DirNode key={child} name={e.name} path={child} depth={depth + 1}
                open={open} toggle={toggle} tree={tree} onOpen={onOpen}
                activePath={activePath} busy={busy} changed={changed} />
            ) : (
              <li key={child}>
                <button
                  type="button"
                  onClick={() => onOpen(child)}
                  title={changed?.has(child)
                    ? `${child} — changed by the agent, not yet reviewed`
                    : child}
                  aria-current={activePath === child ? 'true' : undefined}
                  className={`flex w-full items-center gap-1.5 rounded py-[3px] pr-2 text-left text-[12px] ${
                    activePath === child
                      ? 'bg-white/[0.06] text-zinc-100'
                      : 'text-zinc-400 hover:bg-glass-hover'
                  }`}
                  style={{ paddingLeft: 20 + depth * 12 }}
                >
                  {e.name.endsWith('.py')
                    ? <FileCode2 size={12} className="flex-none text-zinc-500" />
                    : /\.tsx?$/.test(e.name)
                      ? <FileCode2 size={12} className="flex-none text-zinc-400" />
                      : <FileText size={12} className="flex-none text-zinc-muted" />}
                  <span className="truncate">{e.name}</span>
                  {changed?.has(child) && (
                    <span aria-hidden="true"
                      className="ml-auto h-1.5 w-1.5 flex-none rounded-full"
                      style={{ background: '#d29922' }} />
                  )}
                </button>
              </li>
            );
          })}
          {!(tree[path] ?? []).length && (
            <li className="py-1 text-[11px] text-zinc-muted"
                style={{ paddingLeft: 20 + depth * 12 }}>
              {busy ? 'loading' : 'empty'}
            </li>
          )}
        </ul>
      )}
    </li>
  );
}

/*  find widget  */

interface FindState { open: boolean; query: string; replace: string; cs: boolean }

function FindWidget({ state, setState, body, editor, onReveal }: {
  state: FindState; setState: (s: FindState) => void;
  body: string; editor: EditorHandle | null;
  onReveal: (needle: string, cs: boolean, backwards: boolean, moveFocus?: boolean) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (state.open) ref.current?.focus();
  }, [state.open]);

  const total = useMemo(() => {
    if (!state.query) return 0;
    const hay = state.cs ? body : body.toLowerCase();
    const n = state.cs ? state.query : state.query.toLowerCase();
    if (!n) return 0;
    let c = 0;
    let i = hay.indexOf(n);
    while (i !== -1) { c++; i = hay.indexOf(n, i + Math.max(1, n.length)); }
    return c;
  }, [state.query, state.cs, body]);

  if (!state.open) return null;
  return (
    <div className="flex flex-none flex-col gap-1.5 border-b border-glass-border bg-surface-1 px-3 py-2">
      <div className="flex items-center gap-1.5">
        <Search size={12} className="flex-none text-zinc-muted" />
        <input
          ref={ref}
          value={state.query}
          placeholder="find"
          aria-label="Find"
          onChange={(e) => setState({ ...state, query: e.target.value })}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              /* The window capture handler owns closing Find (see there) -
               * it runs first and unmounts this input, so anything done
               * here would never execute. Just stop the event reaching the
               * editor's own Escape branch. */
              e.stopPropagation();
              return;
            }
            if (e.key === 'Enter') {
              e.preventDefault();
              onReveal(state.query, state.cs, e.shiftKey);
            }
          }}
          className="min-w-0 flex-1 rounded border border-glass-border bg-surface-0 px-2 py-1 font-mono text-[11.5px] text-zinc-100 outline-none focus:border-accent/50"
        />
        <button type="button" onClick={() => setState({ ...state, cs: !state.cs })}
          aria-pressed={state.cs} title="Match case"
          className={`rounded px-1.5 py-0.5 text-[10.5px] font-bold ${state.cs ? 'bg-accent/20 text-accent' : 'text-zinc-muted hover:bg-glass-hover'}`}>
          Aa
        </button>
        <span className="w-16 text-right text-[10.5px] tabular-nums text-zinc-muted">
          {state.query ? `${total} match${total === 1 ? '' : 'es'}` : 'no results'}
        </span>
        <button type="button" title="Previous (Shift+Enter)"
          onClick={() => onReveal(state.query, state.cs, true)}
          className="rounded p-0.5 text-zinc-muted hover:bg-glass-hover hover:text-zinc-200">
          <ChevronRight size={13} className="rotate-90" />
        </button>
        <button type="button" title="Next (Enter)"
          onClick={() => onReveal(state.query, state.cs, false)}
          className="rounded p-0.5 text-zinc-muted hover:bg-glass-hover hover:text-zinc-200">
          <ChevronDown size={13} className="-rotate-90" />
        </button>
        <button type="button" title="Close (Esc)"
          onClick={() => setState({ ...state, open: false })}
          className="rounded p-0.5 text-zinc-muted hover:bg-glass-hover hover:text-zinc-200">
          <X size={13} />
        </button>
      </div>
      <div className="flex items-center gap-1.5">
        <Braces size={12} className="flex-none text-zinc-muted" />
        <input
          value={state.replace}
          placeholder="replace"
          aria-label="Replace"
          onChange={(e) => setState({ ...state, replace: e.target.value })}
          className="min-w-0 flex-1 rounded border border-glass-border bg-surface-0 px-2 py-1 font-mono text-[11.5px] text-zinc-100 outline-none focus:border-accent/50"
        />
        <button type="button" disabled={!total}
          onClick={() => {
            const el = editor;
            if (!el || !state.query) return;
            const hay = state.cs ? body : body.toLowerCase();
            const n = state.cs ? state.query : state.query.toLowerCase();
            const at = hay.indexOf(n, el.selection().start);
            if (at === -1) return;
            const next = body.slice(0, at) + state.replace + body.slice(at + n.length);
            /* Advance the caret past the replacement so the next
               Replace walks on to the following match. The
               selection used to stay put, so every click
               re-found and re-replaced the SAME match and the
               walk never moved. */
            const end = Math.min(at + state.replace.length, next.length);
            el.apply(next, end, end);
          }}
          className="rounded border border-glass-border px-1.5 py-0.5 text-[10.5px] text-zinc-300 disabled:opacity-40 hover:bg-glass-hover">
          Replace
        </button>
        <button type="button" disabled={!total}
          onClick={() => {
            const el = editor;
            if (!el || !state.query) return;
            const cs = state.cs;
            const n = cs ? state.query : state.query.toLowerCase();
            const hay = cs ? body : body.toLowerCase();
            let out = '';
            let last = 0;
            let i = hay.indexOf(n);
            while (i !== -1) {
              out += body.slice(last, i) + state.replace;
              last = i + n.length;
              i = hay.indexOf(n, last);
            }
            out += body.slice(last);
            el.apply(out, 0, 0);
          }}
          className="rounded border border-glass-border px-1.5 py-0.5 text-[10.5px] text-zinc-300 disabled:opacity-40 hover:bg-glass-hover">
          All
        </button>
      </div>
    </div>
  );
}

/*  quick open / command palette  */

/* Rank `files` against `q`. Shared by the render and by the key handler: the
   handler must rank from what is actually typed, not from state that may not
   have flushed yet. Ranking from stale state meant that typing a command and
   hitting Enter immediately could run a DIFFERENT command - the top of the
   unfiltered list - because `q` was still empty when the key was handled. */
function rankFiles(files: string[], q: string, limit = 60): string[] {
  if (!q.trim()) return files.slice(0, limit);
  return files
    .map((f) => ({ f, s: fuzzy(q, f.split('/').pop()!) ?? -1 }))
    .filter((x) => x.s >= 0)
    .sort((a, b) => b.s - a.s)
    .slice(0, limit)
    .map((x) => x.f);
}

function Palette({ mode, files, truncated, onPick, onClose }: {
  mode: 'file' | 'cmd';
  files: string[];
  /** The server caps the file index; when that cap was hit the
      palette must say so instead of silently missing files. */
  truncated?: boolean;
  onPick: (v: string) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const ref = useRef<HTMLInputElement>(null);
  const trapRef = useRef<HTMLDivElement | null>(null);
  useFocusTrap(trapRef, { onEscape: onClose });

  useEffect(() => { ref.current?.focus(); }, []);

  const items = useMemo(() => rankFiles(files, q), [q, files]);

  /* Clamp rather than trust: `items` can shrink under the stored index. */
  const at = Math.max(0, Math.min(sel, items.length - 1));
  useEffect(() => { setSel(0); }, [q]);

  return (
    <div
      ref={trapRef}
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 pt-[12vh]"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div role="dialog" aria-modal="true" aria-label={mode === 'file' ? 'Go to file' : 'Command palette'}
           className="w-[min(680px,92vw)] overflow-hidden rounded-lg border border-glass-border bg-surface-1 shadow-2xl">
        <input
          ref={ref}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault();
              const live = rankFiles(files, ref.current?.value ?? q);
              setSel((s) => Math.min(live.length - 1, s + 1));
            }
            if (e.key === 'ArrowUp') {
              e.preventDefault();
              setSel((s) => Math.max(0, s - 1));
            }
            if (e.key === 'Enter') {
              e.preventDefault();
              /* Rank from the DOM value, not from `q`. */
              const live = rankFiles(files, ref.current?.value ?? q);
              const pick = live[Math.max(0, Math.min(sel, live.length - 1))];
              if (pick) onPick(pick);
            }
            if (e.key === 'Escape') onClose();
          }}
          placeholder={mode === 'file' ? 'Search files by name (Ctrl+P)' : 'Type a command (Ctrl+Shift+P)'}
          aria-label={mode === 'file' ? 'Search files by name' : 'Command'}
          className="w-full border-b border-glass-border bg-transparent px-3.5 py-2.5 text-[13px] text-zinc-100 outline-none placeholder:text-zinc-muted"
        />
        <ul className="max-h-[46vh] overflow-y-auto py-1">
          {items.map((f, i) => (
            <li key={f}>
              <button type="button"
                onMouseEnter={() => setSel(i)}
                onClick={() => onPick(f)}
                className={`flex w-full items-center gap-2 px-3.5 py-1.5 text-left text-[12px] ${
                  i === at ? 'bg-accent/15 text-zinc-100' : 'text-zinc-300' }`}>
                {mode === 'file'
                  ? <FileText size={12} className="flex-none text-zinc-muted" />
                  : <Play size={12} className="flex-none text-accent/70" />}
                <span className="truncate">{f}</span>
              </button>
            </li>
          ))}
          {!items.length && (
            <li className="px-3.5 py-3 text-[12px] text-zinc-muted">no matches</li>
          )}
          {truncated && items.length > 0 && (
            <li className="px-3.5 py-2 text-[11px] text-zinc-muted">
              list truncated — refine the search to narrow it down
            </li>
          )}
        </ul>
      </div>
    </div>
  );
}

/*  the editor  */

/* Monaco owns bracket logic, so this map is gone. It used to drive the
   hand-written Enter handler's "did the line end in an opener" test. */

export default function Code() {
  const mainId = useMainId();
  const sectionActive = useSectionActive();
  const [panes, setPanes] = useState<Pane[]>([]);
  /* The active tab is tracked by PATH, not by index. It was an index, and
     `open()` appended the new pane then set the active index to `active + 1`
     - which is only the new pane's index when the previous active tab was the
     last one. Opening a file while any other tab was selected left the new
     pane invisible: no editor, no textarea, just a tab that looked broken. */
  const [activePath, setActivePath] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>(() => loadDrafts());
  const [explorerOn, setExplorerOn] = useState(true);
  /* The agent is a pane, not a fixture: on a laptop it was taking more width
     than the editor it comments on (252px of editor at 1024 wide). Collapsible
     from the top bar, open by default only when there is room for both. */
  const [agentOn, setAgentOn] = useState(() => window.innerWidth >= 1280);
  /* Same reasoning as the agent: a 60px minimap leaves a 250px editor with
     almost no code. It stays available from the status bar. */
  const [minimapOn, setMinimapOn] = useState(() => window.innerWidth >= 1280);
  const [wrap, setWrap] = useState(false);
  const [zen, setZen] = useState(false);
  const [palette, setPalette] = useState<'file' | 'cmd' | null>(null);
  const [goto, setGoto] = useState<string | null>(null);
  const [find, setFind] = useState<FindState>({ open: false, query: '', replace: '', cs: false });
  const [cursor, setCursor] = useState<Cursor>({ line: 1, col: 1 });
  const [selLen, setSelLen] = useState(0);
  const [note, setNote] = useState<string | null>(null);
  const [known, setKnown] = useState<string[]>([]);
  /* Bumped when the workspace re-roots, so the quick-open index is
     re-read against the new folders instead of the old ones. */
  const [wsEpoch, setWsEpoch] = useState(0);
  /* The one path for "the workspace changed on disk outside this view":
     re-rooting, and the agent's own writes. Drops the quick-open/attach
     index and bumps the epoch so the explorer re-reads the tree too. */
  const refreshFromDisk = useCallback(() => {
    setKnown([]);
    setWsEpoch((n) => n + 1);
  }, []);
  /* The file index is capped server-side; when it was, quick
     open must say so instead of silently missing files. */
  const [filesTruncated, setFilesTruncated] = useState(false);
  const [folded, setFolded] = useState(0);
  const [outlineOn, setOutlineOn] = useState(false);
  /* Go-to-definition / find-references / rename. The editor becomes an IDE the
     moment the caret can ask where a symbol lives; everything below backs that
     with the AST the agent's own tools use. */
  const [intelOpen, setIntelOpen] = useState(false);
  const [intelWord, setIntelWord] = useState('');
  /* The save review. `open` shows the diff, `confirm` is the user having
     looked at it, and the note/error say what happened. A save button that
     writes blind is how a browser tab eats an afternoon. */
  const [saveDiff, setSaveDiff] = useState(
    { open: false, confirm: false, note: null as string | null, error: null as string | null });
  const [saving, setSaving] = useState(false);
  const [checkedIn, setCheckedIn] = useState<number | null>(null);
  const [problems, setProblems] = useState<CodeProblem[]>([]);
  /* One bottom panel, not two. Problems and Terminal used to render as two
     permanently expanded docks stacked on each other — ~170px of editor at
     1500px wide, ~330px at 1024 — while the Problems body was three lines of
     apology. VS Code's panel is a single strip of tabs, collapsed until it has
     something to say; this follows that. */
  const [panel, setPanel] = useState<'problems' | 'terminal' | null>(null);
  const [termStatus, setTermStatus] = useState<TerminalStatus>('connecting');
  const [termSeen, setTermSeen] = useState(false);
  /* Stable identity: TerminalSession recreates its WebSocket when onStatus
     changes, so an inline arrow here would drop the shell on every render. */
  const onTermStatus = useCallback((s: TerminalStatus) => {
    setTermStatus(s);
    setTermSeen(true);
  }, []);
  /* Problems go to Monaco as editor markers, which wants the full problem
     (line, column, message, severity) rather than the line numbers the old
     overlay-only path needed. */
  const problemLines = useMemo(() => problems, [problems]);
  /* Surface the panel when the buffer gains a problem, but never force it back
     open after the user closed it — `?? 'problems'` only fires from null. */
  useEffect(() => {
    if (problems.length > 0) setPanel((p) => p ?? 'problems');
  }, [problems.length]);
  const [checkNote, setCheckNote] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  /* The project's own checks, run for real. `problems` above is the BUFFER
     parsed with ast.parse — fast, and honest about being only syntax. These are
     pytest/tsc/npm test/ruff/cargo/go vet, which is the only signal that
     catches a change which parses cleanly but is wrong. */
  const [checks, setChecks] = useState<CodeChecks | null>(null);
  const [checksBusy, setChecksBusy] = useState(false);
  /* What is on DISK, as opposed to the drafts in this browser. This is the
     review step: a change to a file has to be looked at before it is trusted,
     and the browser buffer is not what anyone else will get. */
  const [gitView, setGitView] = useState<'status' | 'diff' | 'log'>('status');
  const [gitOut, setGitOut] = useState<{ text: string; ok: boolean
    | undefined; error?: string } | null>(null);
  const [gitBusy, setGitBusy] = useState(false);
  /* The AI half of the editor now lives in `AgentPanel`, which owns its own
     conversation, abort handle and busy state. Keeping a single-question
     `ask` slice here as well meant two sources of truth for the same thing.
     `aiModel` is the exception: the panel renders the picker but the choice has
     to reach the request closure below, so it stays here. Persisted because a
     reload that silently reverts the model is how you end up paying for an
     expensive model you did not mean to use. */
  const [aiModel, setAiModel] = useState(
    () => localStorage.getItem('aria.code.model') ?? '');
  /* A write the agent is parked on, waiting for a decision. The run cannot
     proceed until this clears, which is the whole point: the diff has to be on
     screen BEFORE the file can change. */
  const [pendingEdit, setPendingEdit] = useState<PendingEdit | null>(null);
  const [editBusy, setEditBusy] = useState(false);
  useEffect(() => {
    try {
      if (aiModel) localStorage.setItem('aria.code.model', aiModel);
      else localStorage.removeItem('aria.code.model');
    } catch { /* private mode: the picker still works for this session */ }
  }, [aiModel]);
  const [shortcuts, setShortcuts] = useState(false);
  const [search, setSearch] = useState<{ open: boolean; q: string; hits: CodeMatch[]; busy: boolean }>(
    { open: false, q: '', hits: [], busy: false });
  const searchTimer = useRef<number | undefined>(undefined);
  const searchAbort = useRef<AbortController | null>(null);
  const searchReq = useRef(0);
  useEffect(() => () => {
    window.clearTimeout(searchTimer.current);
    searchAbort.current?.abort();
  }, []);
  const [stale, setStale] = useState<string | null>(null);
  const [jump, setJump] = useState<{ path: string; line: number; col: number } | null>(null);
  /* What the agent changed on disk, per path: green/red line marks in the
     editor (parsed from the diff the permission dialog showed). Cleared
     when the user saves the file — at that point the change is theirs. */
  const [agentMarks, setAgentMarks] = useState<Record<string, DiffMarks>>({});
  /* Set when a write LANDS on disk (not when it is approved: the approval
     POST returns before the tool has run). The effect below brings the
     open pane in line with disk - or shows the stale banner, never
     clobbers a draft. */
  const [syncPath, setSyncPath] = useState<string | null>(null);
  /* The write this turn is parked on: its client path, tool name and marks,
     captured from the permission frame so the matching tool_result can pair
     with it (the frames carry different ids). */
  /* Write payloads waiting for their tool_result, in dispatch order. A queue,
     not a single slot: a turn can park a write and a run_checks together, and
     matching by tool name FIFO is what keeps the marks on the right file. A
     refused/failed write consumes its slot too, so it cannot be applied to a
     later successful call of the same tool. */
  const pendingWrites = useRef<{ path: string; tool: string; marks: DiffMarks }[]>([]);
  /* Markdown preview: VS Code's Open Preview, as a toggle. Applies to the
     active pane only while it is a markdown file. */
  const [previewMd, setPreviewMd] = useState(false);
  /* The editing surface is Monaco. Everything this section does by hand goes
     through this handle rather than a DOM node, which is what keeps Monaco,
     a future language server and a future Electron host swappable
     underneath. */
  const ed = useRef<EditorHandle | null>(null);

  const active = panes.findIndex((p) => p.path === activePath);
  const pane = active >= 0 ? panes[active] : null;
  const body = pane?.body ?? '';
  const lang = pane?.file.language ?? 'text';
  const dirty = !!pane && pane.body !== pane.base;
  const activeMarks = pane ? agentMarks[pane.path] : undefined;
  /* Explorer dots: which files carry an unreviewed agent change. */
  const changedPaths = useMemo(() => new Set(Object.keys(agentMarks)), [agentMarks]);
  const lines = useMemo(() => body.split('\n'), [body]);

  /* Buffer vs disk, as a line diff. `pane.base` is the on-disk original and
     `body` the live buffer: diffing `body` against `pane.body` compared the
     buffer with itself, so a real edit reviewed as "0 added, 0 removed". */
  const diffRows = useMemo(
    () => (pane ? lineDiff(pane.base.split('\n'), pane.body.split('\n')) : []),
    [pane, body]);

  /* The quick-open index: every servable file, not just open tabs. Ctrl+P
     originally listed only files already open, which made it a tab switcher
     wearing a file finder's clothes. */
  useEffect(() => {
    let live = true;
    const ac = new AbortController();
    api.codeFiles(ac.signal)
      .then((r) => {
        if (!live || !r?.files) return;
        setKnown(r.files);
        setFilesTruncated(!!r.truncated);
      })
      .catch(() => undefined);
    return () => { live = false; ac.abort(); };
  }, [wsEpoch]);

  /* Newly opened files stay in the list even if the index missed them. */
  useEffect(() => {
    setKnown((k) => (panes.length ? [...new Set([...k, ...panes.map((p) => p.path)])] : k));
  }, [panes]);

  const flash = useCallback((m: string) => {
    setNote(m);
    window.setTimeout(() => setNote((n) => (n === m ? null : n)), 2200);
  }, []);

  /* Persist drafts so a reload keeps unsaved work — DEBOUNCED:
     every keystroke used to serialise the entire draft map (every
     open buffer, not just the one being typed in) to localStorage
     synchronously on the main thread. */
  const draftsRef = useRef(drafts);
  draftsRef.current = drafts;
  const draftsTimer = useRef<number | undefined>(undefined);
  const draftsWarned = useRef(false);
  useEffect(() => {
    window.clearTimeout(draftsTimer.current);
    draftsTimer.current = window.setTimeout(() => {
      if (saveDrafts(draftsRef.current) || draftsWarned.current) return;
      draftsWarned.current = true;
      flash('localStorage is full — drafts may not survive a reload');
    }, 800);
    return () => window.clearTimeout(draftsTimer.current);
  }, [drafts, flash]);
  useEffect(() => () => {
    window.clearTimeout(draftsTimer.current);
    /* Flush the final burst on the way out. */
    saveDrafts(draftsRef.current);
  }, []);

  /* `reload` is what "Reload from server" needs. The staleness banner only
     renders when the pane is clean, so the file is always already open - and
     the `panes.some(...)` early return below made the only offered action a
     guaranteed no-op: it set the active tab and returned without fetching.
     Reload must therefore skip that guard and overwrite body AND base. */
  const openReq = useRef(0);
  const open = useCallback(async (path: string, opts?: { reload?: boolean }) => {
    if (!opts?.reload && panes.some((p) => p.path === path)) {
      setActivePath(path);
      return;
    }
      const req = ++openReq.current;
    try {
      const f = await api.codeFile(path).catch((e: unknown) => {
        // The server's message is the useful one ("too large", "not text",
        // "outside the roots"); do not flatten it to a generic failure.
        const msg = (e as { message?: string })?.message || String(e);
        throw new Error(msg.replace(/^HTTP \d+:\s*/, ''));
      });
      /* The user may have opened another file while this fetch
         was in flight; the slower response must not steal the
         active tab (or land its pane on top of a newer one). */
      if (req !== openReq.current) return;
      if (!f) { flash(`cannot open ${path} - the server returned nothing for it`); return; }
      if (opts?.reload) {
        /* Reload discards the local draft, and the draft is the ONLY copy of
           that work - the server never received it. Confirm, and keep a
           backup so "undo" is not the only way out. */
        const hadDraft = path in drafts && drafts[path] !== panes.find((p) => p.path === path)?.base;
        if (hadDraft) {
          const keep = window.confirm(
            `${path} has unsaved changes in this browser.\n\n` +
            `Reloading replaces them with the server's copy and discards your edits.\n\n` +
            `Save a copy first? OK saves your version as ${path}.recovered.txt and then reloads.`);
          if (keep) downloadText(`${path}.recovered.txt`, drafts[path]);
          else if (!window.confirm(`Discard your changes to ${path} and reload anyway?`)) return;
        }
        setPanes((ps) => ps.map((p) => (
          p.path === path ? { ...p, file: f, body: f.text, base: f.text } : p
        )));
        setDrafts((d) => {
          if (!(path in d)) return d;
          const next = { ...d };
          delete next[path];
          return next;
        });
        setActivePath(path);
        flash(`reloaded ${path}`);
        return;
      }
      const body = drafts[path] ?? f.text;
      setPanes((ps) => (ps.some((p) => p.path === path) ? ps : [...ps, { path, file: f, body, base: f.text }]));
      setActivePath(path);
      /* Remember when this copy was read: the save sends it back so the server
         can refuse to clobber a file that moved on in the meantime. */
      setCheckedIn(f.mtime ?? null);
    } catch (e) {
      flash(`cannot open ${path}: ${e instanceof Error ? e.message : 'error'}`);
    }
  }, [panes, drafts, flash]);

  const close = useCallback((idx: number) => {
    setPanes((ps) => {
      if (!ps[idx]) return ps;
      const victim = ps[idx];
      const next = ps.filter((_, i) => i !== idx);
      /* Select a neighbour by path: the tab to the left if there was one,
         otherwise the new last tab. */
      setActivePath((cur) => {
        if (cur !== victim.path) return cur;
        return next[Math.min(idx, next.length - 1)]?.path ?? null;
      });
      return next;
    });
  }, []);

  const edit = useCallback((v: string) => {
    const path = pane?.path;
    if (!path) return;
    setPanes((ps) => ps.map((p) => (p.path === path ? { ...p, body: v } : p)));
    setDrafts((d) => ({ ...d, [path]: v }));
  }, [pane]);

  /* The buffer as it was before the change currently being applied.

     This has to be a ref rather than `el.value`: for real typing the browser
     has ALREADY updated `el.value` by the time React's onChange runs, so
     snapshotting `el.value` there records the new text and undo removes
     nothing. Programmatic edits are the opposite case - `el.value` is still
     the old text - so both paths read this ref, which `edit` keeps in step. */
  const lastBody = useRef('');
  useEffect(() => { if (pane) lastBody.current = pane.body; }, [pane]);

  /* The editor surface is Monaco, so there is no DOM node to poke: the caret
     and the buffer are read back through the handle instead. */
  const syncCursor = useCallback((info?: CaretInfo) => {
    if (!info) {
      const c = ed.current?.caret();
      if (!c) return;
      setCursor(posOf(body, c.offset));
      setSelLen(c.selLen);
      return;
    }
    setCursor(posOf(body, info.offset));
    setSelLen(info.selLen);
  }, [body]);

  /* Undo history. `restore` puts a snapshot back into the editor AND the
     caret with it, which is the whole point: Monaco's own undo cannot see
     the programmatic edits this section makes, so it would step through a
     history that does not match what the user just did. */
  /* Restoring from history writes the buffer, which would fire Monaco's
     change event and land back in `onType` - and `onType` records a
     snapshot. Without this guard, undoing pushed the pre-undo state onto the
     undo stack and cleared the redo stack, so redo could never work and
     repeated undo walked forwards. */
  const restoring = useRef(false);

  const applyText = useCallback((text: string, start: number, end: number) => {
    if (!ed.current) return;
    restoring.current = true;
    ed.current.apply(text, Math.min(start, text.length), Math.min(end, text.length));
    restoring.current = false;
  }, []);

  /* `applyText` is defined below `useHistory` in source order but is needed by
     it, so the indirection goes through a ref rather than reordering hooks. */
  const applyTextRef = useRef<((t: string, s: number, e: number) => void) | null>(null);
  const history = useHistory(useCallback((s: Snapshot) => {
    applyTextRef.current?.(s.text, s.start, s.end);
    // The editor model is not the source of truth for Save/drafts. Without
    // this the next Ctrl+S wrote the pre-undo text back to disk (H8).
    lastBody.current = s.text;
    edit(s.text);
  }, [edit]));
  applyTextRef.current = applyText;

  /* Undo history is PER-BUFFER. The stack holds bare snapshots
     with no path, so switching tabs used to leave file A's
     snapshots on it — Ctrl+Z in file B then wrote file A's text
     into file B's textarea (and file A's selection with it).
     VS Code keeps one undo stack per editor; the tab switch is
     the boundary here. Folds are line numbers into the old
     buffer, so they die with it too. */
  const lastPath = useRef<string | null>(null);
  useEffect(() => {
    if (activePath === lastPath.current) return;
    lastPath.current = activePath;
    history.reset('');
    lastBody.current = pane?.body ?? '';
    setFolded(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePath]);

  const replaceRange = useCallback((from: number, to: number, text: string,
                                   selStart = from + text.length) => {
    if (!ed.current) return;
    const cur = ed.current.text();
    const sel = ed.current.selection();
    const next = cur.slice(0, from) + text + cur.slice(to);
    /* Record the state BEFORE the change, with the selection that produced
       it, so undo returns to where the edit started. */
    history.commit(cur, sel.start, sel.end);
    lastBody.current = next;
    ed.current.apply(next, Math.min(selStart, next.length), Math.min(selStart, next.length));
  }, [history]);

  /* Typed input goes through the same door so Ctrl+Z undoes typing too. */
  const onType = useCallback((v: string, info?: CaretInfo) => {
    if (!restoring.current) {
      const before = lastBody.current;
      lastBody.current = v;
      const at = info?.offset ?? ed.current?.caret().offset ?? 0;
      history.commit(before, at, at, true);
    } else {
      lastBody.current = v;
    }
    edit(v);
  }, [edit, history]);

  /* Derived structure, recomputed only when the buffer or language changes.
     Folding is Monaco's, so this only feeds the outline. */
  const outline = useMemo<Symbol[]>(
    () => (pane ? symbolsOf(pane.body, lang) : []),
    [pane, lang],
  );

  const loadGit = useCallback(async (which: 'status' | 'diff' | 'log') => {
  setGitView(which);
  setGitBusy(true);
  try {
    const r = await api.codeGit(which);
    if (r) {
      setGitOut({ text: r.output || '', ok: r.ok,
        error: r.error || (r.ok ? undefined : 'git reported a problem') });
    }
  } catch (e) {
    setGitOut({ text: '', ok: false,
      error: `could not read git: ${e instanceof Error ? e.message : 'error'}` });
  } finally {
    setGitBusy(false);
  }
}, []);


/* Run the project's real lint/typecheck/tests.
     * Deliberately NOT auto-run on edit: a test suite takes what it takes, and
     * firing one per keystroke would hammer the machine. It is a deliberate
     * action, and the result is kept until the next run so a red state is
     * never silently replaced by an empty list. */
const runProjectChecks = useCallback(async () => {
  if (checksBusy) return;
  setChecksBusy(true);
  try {
    const r = await api.codeChecks();
    if (r) setChecks(r);
  } catch (e) {
    setChecks({
      ok: false, checks_run: 0, results: [],
      error: `could not run checks: ${e instanceof Error ? e.message : 'error'}`,
    });
  } finally {
    setChecksBusy(false);
  }
}, [checksBusy]);

/* Debounced real syntax check. Sends the draft text; the server parses it
     with `ast.parse` for Python and never writes or executes anything. */
  useEffect(() => {
    if (!pane) { setProblems([]); setCheckNote(null); return; }
    if (lang !== 'python'
        && !['typescript', 'typescriptreact', 'javascript', 'json'].includes(lang)) {
      setProblems([]);
      setCheckNote('no parser available for this language');
      return;
    }
    const ac = new AbortController();
    setChecking(true);
    const t = window.setTimeout(() => {
      api.codeCheck(pane.body, lang, ac.signal)
        .then((r) => {
          if (ac.signal.aborted || !r) return;
          setProblems(r.problems ?? []);
          setCheckNote(r.note ?? (r.checked ? null : 'check could not run'));
        })
        .catch((e) => {
          /* A failed check used to be swallowed, and the empty
             problems list then rendered "“ No syntax errors" —
             the exact opposite of what happened. */
          if (ac.signal.aborted) return;
          setProblems([]);
          setCheckNote(`check failed: ${e instanceof Error ? e.message : 'error'}`);
        })
        .finally(() => { if (!ac.signal.aborted) setChecking(false); });
    }, 450);
    return () => {
      ac.abort();
      window.clearTimeout(t);
      /* The debounce was cancelled before the check ever ran —
         nothing is in flight, so the "checking" pill must not
         spin forever. */
      setChecking(false);
    };
  }, [pane, lang]);

  /* The source is read-only from the agent, so it can change underneath a
     draft. Notice that, rather than silently showing text that no longer
     matches what the server would serve. (The 20s poll is the backstop; an
     agent write that lands while the section is open is pushed in through
     `syncPath` below, immediately.) */
  useEffect(() => {
    if (!sectionActive || !pane || dirty) { setStale(null); return; }
    let live = true;
    const ac = new AbortController();
    const t = window.setInterval(() => {
      api.codeFile(pane.path, ac.signal)
        .then((f) => {
          if (!live || !f) return;
          setStale(f.version !== pane.file.version ? pane.path : null);
        })
        .catch(() => undefined);
    }, 20000);
    return () => { live = false; ac.abort(); window.clearInterval(t); };
  }, [sectionActive, pane, dirty]);

  /* A write just landed on disk. If the file is open in a clean pane, pull
     the server's copy in - the marks then show exactly what changed. If the
     pane has unsaved edits, do NOT clobber them: raise the stale banner and
     let the user decide. If the file is not open at all, open it, the way
     Cursor puts you in front of what the agent just did. */
  useEffect(() => {
    if (!syncPath) return;
    const p = panes.find((x) => x.path === syncPath);
    if (p && p.body !== p.base) { setStale(syncPath); return; }
    void open(syncPath, { reload: true });
    setSyncPath(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [syncPath]);

  const reveal = useCallback((needle: string, cs: boolean, backwards: boolean,
                              moveFocus = false) => {
    if (!ed.current || !needle) return;
    const hay = cs ? body : body.toLowerCase();
    const n = cs ? needle : needle.toLowerCase();
    const { start, end } = ed.current.selection();
    let at: number;
    if (backwards) {
      at = hay.lastIndexOf(n, Math.max(0, start - 1));
      if (at === -1) at = hay.lastIndexOf(n);
    } else {
      at = hay.indexOf(n, end);
      if (at === -1) at = hay.indexOf(n);
    }
    if (at === -1) return;
    /* Do NOT steal focus here. The Find box owns focus while it is open, so
       Enter keeps advancing matches; callers that really want the caret in
       the editor pass moveFocus. */
    if (moveFocus) ed.current?.focus();
    ed.current?.selectAndReveal(at, at + n.length);
    syncCursor();
  }, [syncCursor]);

  /* Apply a pending jump-to-line once its file is the active pane. */
  useEffect(() => {
    if (!jump || !pane || pane.path !== jump.path) return;
    const off = offsetOf(pane.body, { line: jump.line, col: jump.col });
    setJump(null);
    requestAnimationFrame(() => {
      ed.current?.revealOffset(off);
      syncCursor();
    });
  }, [jump, pane, syncCursor]);

  /* Undo/redo read the live editor rather than React state, because the
     editor is the only thing that always holds the current buffer. */
  const doUndo = useCallback(() => {
    if (!ed.current) return;
    const { start, end } = ed.current.selection();
    if (!history.undo({ text: ed.current.text(), start, end })) {
      flash('nothing to undo');
    }
  }, [history, flash]);

  const doRedo = useCallback(() => {
    if (!ed.current) return;
    const { start, end } = ed.current.selection();
    if (!history.redo({ text: ed.current.text(), start, end })) {
      flash('nothing to redo');
    }
  }, [history, flash]);

  /* Line operations run Monaco's own (i.e. VS Code's) actions. They used to
     be hand-written offset arithmetic over the buffer, which meant two
     implementations of the same edit — and the comment map here drifted from
     the language's real comment token. The editor knows how to do all of
     these, including with a multi-cursor selection. */
  const lineOps = useCallback((kind: EditorOp) => {
    if (!ed.current) return;
    ed.current.runOp(kind);
    syncCursor();
  }, [syncCursor]);

  /*  commands  */
  const CMDS: { id: string; label: string; run: () => void }[] = useMemo(() => [
    { id: 'file.quickOpen', label: 'Go to File', run: () => setPalette('file') },
    { id: 'file.save', label: 'Save (browser draft only)', run: () => flash('nothing is written to disk — this is a browser draft') },
    { id: 'file.revert', label: 'Revert File to Disk Contents', run: () => {
      if (!pane) return;
      if (pane.body === pane.base) { flash('no local changes to revert'); return; }
      edit(pane.base);
      flash(`reverted ${pane.path} to what the agent serves`);
    } },
    { id: 'view.find', label: 'Find in File', run: () => setFind((f) => ({ ...f, open: true })) },
    { id: 'view.goto', label: 'Go to Line/Column', run: () => setGoto('') },
    { id: 'view.explorer', label: 'Toggle Explorer', run: () => setExplorerOn((v) => !v) },
    { id: 'view.minimap', label: 'Toggle Minimap', run: () => setMinimapOn((v) => {
      ed.current?.setMinimap(!v);
      return !v;
    }) },
    { id: 'view.wrap', label: 'Toggle Word Wrap', run: () => setWrap((v) => !v) },
    { id: 'view.zen', label: 'Toggle Zen Mode', run: () => setZen((v) => !v) },
    { id: 'edit.comment', label: 'Toggle Line Comment', run: () => lineOps('comment') },
    { id: 'edit.indent', label: 'Indent Lines', run: () => lineOps('indent') },
    { id: 'edit.outdent', label: 'Outdent Lines', run: () => lineOps('outdent') },
    { id: 'edit.moveUp', label: 'Move Line Up', run: () => lineOps('moveUp') },
    { id: 'edit.moveDown', label: 'Move Line Down', run: () => lineOps('moveDown') },
    { id: 'edit.deleteLine', label: 'Delete Line', run: () => lineOps('deleteLine') },
    { id: 'edit.dupLine', label: 'Duplicate Line Down', run: () => lineOps('dupLine') },
    { id: 'edit.selectNext', label: 'Select Next Occurrence', run: () => lineOps('selectNext') },
    { id: 'edit.copyLineUp', label: 'Copy Line Up', run: () => lineOps('copyUp') },
    { id: 'edit.copyLineDown', label: 'Copy Line Down', run: () => lineOps('copyDown') },
    { id: 'edit.trim', label: 'Trim Trailing Whitespace', run: () => {
      if (!ed.current) return;
      const before = ed.current.text();
      const caret0 = ed.current.caret().offset;
      lineOps('trimTrailing');
      const after = ed.current.text();
      if (before === after) flash('no trailing whitespace');
      else ed.current.revealOffset(Math.min(caret0, after.length));
      flash('trimmed trailing whitespace');
    } },
    { id: 'edit.undo', label: 'Undo', run: () => doUndo() },
    { id: 'edit.redo', label: 'Redo', run: () => doRedo() },
    { id: 'view.foldAll', label: 'Fold All', run: () => ed.current?.fold('all') },
    { id: 'view.unfoldAll', label: 'Unfold All', run: () => ed.current?.fold('none') },
    { id: 'view.outline', label: 'Toggle Symbol Outline', run: () => setOutlineOn((v) => !v) },
    { id: 'search.workspace', label: 'Search Across Workspace', run: () => setSearch((s) => ({ ...s, open: true })) },
    { id: 'view.shortcuts', label: 'Keyboard Shortcuts', run: () => setShortcuts(true) },
    { id: 'view.copyPath', label: 'Copy File Path', run: () => {
      if (!pane) return;
      void navigator.clipboard?.writeText(pane.path).then(
        () => flash(`copied ${pane.path}`),
        () => flash('clipboard blocked by the browser'),
      );
    } },
  ], [doRedo, doUndo, edit, flash, lineOps, pane, replaceRange]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      const inField = e.target instanceof HTMLElement
        && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA');

      if (e.key === 'Escape') {
        /* Exactly ONE overlay closes per Escape, and the event
           stops here: without stopPropagation the same keypress
           also reached the overlay's own focus-trap handler, so
           two stacked overlays (a go-to box over an open find
           panel, say) both closed on one press. */
        /* The save review is the last thing rendered, so it is on top and
           takes Escape before anything under it. DiffView is a plain modal
           with no key handling of its own. */
        if (saveDiff.open) {
          e.preventDefault(); e.stopPropagation();
          setSaveDiff((s) => ({ ...s, open: false })); return;
        }
        if (shortcuts) {
          e.preventDefault(); e.stopPropagation();
          setShortcuts(false); return;
        }
        if (palette) {
          e.preventDefault(); e.stopPropagation();
          setPalette(null); return;
        }
        if (goto !== null) {
          e.preventDefault(); e.stopPropagation();
          setGoto(null); return;
        }
        if (search.open) {
          e.preventDefault(); e.stopPropagation();
          setSearch((s) => ({ ...s, open: false })); return;
        }
        if (find.open) {
          e.preventDefault(); e.stopPropagation();
          /* Escape from Find is the "take me to the match" gesture: close
             the panel AND put the caret on the match. It has to happen HERE,
             in the window capture handler: this runs before React's
             onKeyDown on the input, so a handler inside FindWidget never got
             the event — the panel was already unmounting, and focus fell to
             <body> every time. Two frames, because the editor's restore
             effect also runs on this state change and the last writer wins. */
          const wasCs = find.cs;
          const q = find.query;
          setFind((f) => ({ ...f, open: false }));
          requestAnimationFrame(() => requestAnimationFrame(() => {
            reveal(q, wasCs, false, true);
          }));
          return;
        }
        /* Last resort, and the documented one: Escape leaves the editor. The
           textarea is the only focusable thing in the view, so without this a
           keyboard user who never discovers Shift+Tab has no way out. */
        if (inField && edInFocus(e.target)) {
          e.preventDefault();
          ed.current?.blur();
          return;
        }
      }
      /* While any overlay or panel owns the keyboard, the background
         editor's shortcuts must not fire: Ctrl+Z inside the command
         palette undid the editor behind it, and Ctrl+G could stack a
         second modal under the first. Find/search are chrome, not
         modals — their shortcuts stay live while the EDITOR itself
         holds the focus. */
      if (palette || shortcuts || goto !== null) return;
      if ((search.open || find.open) && !edInFocus(e.target)) return;
      if (e.key === ',' && mod && !e.shiftKey) {
        e.preventDefault(); setShortcuts(true); return;
      }
      if (mod && e.shiftKey && e.key.toLowerCase() === 'f') {
        e.preventDefault();
        setSearch((s) => ({ ...s, open: true }));
        return;
      }
      if (mod && e.shiftKey && e.key.toLowerCase() === 'o') {
        e.preventDefault();
        const first = outline[0];
        if (first) replaceRange(offsetOf(body, { line: first.line, col: 1 }),
          offsetOf(body, { line: first.line, col: 1 }), '');
        else flash('no symbols found in this file');
        return;
      }
      /* The three IDE gestures: F12 go-to-definition, Shift+F12 find
         references, F2 rename, Ctrl+S save. Without them the caret can edit
         text but cannot ask the project anything. */
      if (e.key === 'F12') {
        e.preventDefault();
        if (e.shiftKey) {
          if (caretWord) { setIntelWord(caretWord); setIntelOpen(true); }
          else flash('put the caret on a symbol first');
        } else {
          void goToDefinition();
        }
        return;
      }
      if (e.key === 'F2') {
        e.preventDefault();
        if (caretWord) { setIntelWord(caretWord); setIntelOpen(true); }
        else flash('put the caret on a symbol first');
        return;
      }
      /* Ctrl+S opens the save review rather than writing: the diff has to be
         looked at before anything reaches the disk. */
      if (mod && e.key.toLowerCase() === 's' && !e.shiftKey) {
        e.preventDefault();
        setSaveDiff({ open: true, confirm: false, note: null, error: null });
        return;
      }
      if (mod && e.key.toLowerCase() === 'z' && !e.shiftKey) {
        e.preventDefault(); doUndo(); return;
      }
      if ((mod && e.key.toLowerCase() === 'y')
          || (mod && e.shiftKey && e.key.toLowerCase() === 'z')) {
        e.preventDefault(); doRedo(); return;
      }
      if (mod && e.shiftKey && e.key.toLowerCase() === 'p') {
        e.preventDefault(); setPalette('cmd'); return;
      }
      if (mod && !e.shiftKey && e.key.toLowerCase() === 'p') {
        e.preventDefault(); setPalette('file'); return;
      }
      if (mod && e.key.toLowerCase() === 'f') {
        e.preventDefault(); setFind((f) => ({ ...f, open: true })); return;
      }
      if (mod && e.key.toLowerCase() === 'g') { e.preventDefault(); setGoto(''); return; }
      if (mod && e.key.toLowerCase() === 's') {
        e.preventDefault();
        flash('nothing is written to disk — this is a browser draft');
        return;
      }
      if (mod && e.key.toLowerCase() === 'w' && panes.length) {
        e.preventDefault(); close(active); return;
      }
      if (mod && e.key === 'Tab' && panes.length > 1) {
        e.preventDefault();
        const i = (active + (e.shiftKey ? panes.length - 1 : 1)) % panes.length;
        setActivePath(panes[i].path);
        return;
      }
      if (!inField || edInFocus(e.target)) {
        /* Fold/unfold the region at the caret. Monaco keys this Ctrl+Shift+[ / ]
           itself, but this window listener runs in the capture phase and would
           otherwise race it, so the gesture is claimed here and run through the
           handle - the same stock actions, one owner. */
        if (mod && e.shiftKey && (e.key === '[' || e.key === ']')) {
          e.preventDefault();
          ed.current?.fold(e.key === '[' ? 'cursor' : 'expand');
          return;
        }
        if (mod && e.key === '/') { e.preventDefault(); lineOps('comment'); return; }
        if (mod && e.key.toLowerCase() === 'd') { e.preventDefault(); lineOps('selectNext'); return; }
        if (e.shiftKey && e.altKey && e.key === 'ArrowUp') { e.preventDefault(); lineOps('copyUp'); return; }
        if (e.shiftKey && e.altKey && e.key === 'ArrowDown') { e.preventDefault(); lineOps('copyDown'); return; }
        /* Move the caret a line up/down keeping the column. Monaco does this
           natively too, but ONLY as part of its own Alt+Up/Down "move line",
           which SHIFTS the line rather than moving the caret. */
        if (mod && e.shiftKey && e.key.toLowerCase() === 'k') { e.preventDefault(); lineOps('deleteLine'); return; }
      }
      /* Everything the editor now owns natively — Tab indent, Enter
         auto-indent and colon-block opening, bracket/quote auto-close and
         wrap-around-selection, Backspace pair deletion, Alt+Up/Down line
         moves — is GONE from here, deliberately. These used to be ~100 lines
         of offset arithmetic over the textarea's value. They are gone because
         Monaco implements every one of them, keyed the way VS Code keys
         them, and works with multi-cursor selection as well. Two
         implementations of the same edit is exactly how they drift, and the
         bug report here was "backspace left a stray )".

         The Tab handler also had a side effect worth remembering: by
         swallowing Tab it was a keyboard trap (WCAG 2.1.2) — focus could
         never leave the editor except through the Escape hatch below. */
  }
  window.addEventListener('keydown', onKey, true);
  return () => window.removeEventListener('keydown', onKey, true);
}, [active, body, close, cursor.line, doRedo, doUndo,
    find.open, flash, goto, lang, lineOps, offsetOf, outline, palette, panes.length, saveDiff.open,
    replaceRange, search.open, shortcuts, syncCursor]);

  /* The caret's scroll-into-view is Monaco's job. The hand-written version
     below measured a scroll container that no longer exists. */

  const gotoLine = (raw: string) => {
    const m = raw.match(/^(\d+)(?::(\d+))?$/);
    if (!m) return;
    const line = Math.max(1, Math.min(lines.length, Number(m[1])));
    const col = Math.max(1, Number(m[2] ?? 1));
    const off = offsetOf(body, { line, col });
    replaceRange(off, off, '');
    setGoto(null);
  };

  /* The identifier under the caret, so F12/F2 have something to ask about. */
  const caretWord = useWordAtCursor(body, cursor.line, cursor.col, !!pane);

  /** Open a file (if it is not already) and put the caret on `line`. */
  const jumpTo = useCallback(async (file: string, line: number) => {
    if (!file) return;
    if (pane?.path !== file) await open(file);
    setJump({ path: file, line, col: 1 });
  }, [open, pane?.path]);

  const goToDefinition = useCallback(async () => {
    if (!pane) return;
    if (!caretWord) { flash('put the caret on a symbol first'); return; }
    try {
      const r = await api.codeIntel('definition', {
        path: pane.path, name: caretWord,
        line: cursor.line, col: cursor.col,
      });
      if (r.found && r.file && r.line) {
        await jumpTo(r.file, r.line);
      } else {
        flash(`no definition of ${caretWord} found`);
      }
    } catch {
      flash('definition lookup failed');
    }
  }, [pane, caretWord, cursor.line, cursor.col, jumpTo, flash]);

  /* Accept or refuse the agent's pending write. The pane/tree/marks sync
     happens when the write LANDS on disk (its tool_result frame), not here:
     this POST returns as soon as the decision is recorded, and reloading
     immediately could read the pre-write bytes. */
  const decideEdit = useCallback(async (id: string, approve: boolean) => {
    setEditBusy(true);
    try {
      await api.codePermission(id, approve);
      setPendingEdit(null);
    } catch { /* a 404 means the run already moved on; close either way */ }
    finally { setEditBusy(false); }
  }, []);

  /* Ctrl+S writes the buffer to disk. Gated on the client twice over: the
     diff has to be looked at, and the server re-checks the mtime it handed
     us so a change made elsewhere in between is not silently overwritten.
     Writes are atomic, with a timestamped backup of what was there. */
  const saveToDisk = useCallback(async (confirm = false) => {
    if (!pane) return;
    if (!dirty) {
      flash(`${pane.path} already matches disk - nothing written`);
      return;
    }
    if (saving) return;
    setSaving(true);
    try {
      const res = await api.codeWrite(pane.path, body, {
        confirm,
        mtime: checkedIn ?? undefined,
      });
      if (res?.ok) {
        setPanes((ps) => ps.map((p) => (p.path === pane.path
          ? { ...p, base: body, file: { ...p.file, text: body, version: String(Date.now()) } }
          : p)));
        setSaveDiff({ open: false, confirm: false, note: 'written', error: null });
        setCheckedIn(null);
        /* The file now matches disk with the agent's change folded in, so
           the green/red marks have served their purpose. */
        setAgentMarks((m) => {
          if (!(pane.path in m)) return m;
          const next = { ...m };
          delete next[pane.path];
          return next;
        });
        flash(`wrote ${pane.path} to disk`);
      } else {
        setSaveDiff((s) => ({
          ...s,
          confirm: false,
          error: res?.error || 'write refused by the agent',
        }));
      }
    } catch {
      setSaveDiff((s) => ({ ...s, confirm: false, error: 'write failed - is the agent up?' }));
    } finally {
      setSaving(false);
    }
  }, [pane, body, dirty, saving, checkedIn, flash]);

  /* Keep one file's agent change: the bytes stay exactly as they are on disk,
     only the review marks go. */
  const keepChange = useCallback(async (path: string) => {
    try {
      const r = await api.codeKeep(path);
      if (!r?.ok) throw new Error(r?.error || 'the server did not keep it');
      setAgentMarks((m) => {
        if (!(path in m)) return m;
        const next = { ...m };
        delete next[path];
        return next;
      });
      flash(`kept ${path} as the agent left it`);
    } catch (e) {
      /* Keep only clears the marks once the snapshot is actually gone. The
         old order - clear first, request second, swallow the error - made a
         failed Keep look successful: the marks vanished while the server
         still listed the file as unreviewed. */
      flash(`could not keep ${path}: ${e instanceof Error ? e.message : 'request failed'}`);
    }
  }, [flash]);

  /* Revert one file to the snapshot taken before the agent first wrote it.
     A file the agent CREATED is removed and its pane closed - keeping a tab
     open on a file that no longer exists was how the old flow lied. */
  const [reviewBusy, setReviewBusy] = useState(false);
  const revertChange = useCallback(async (path: string) => {
    setReviewBusy(true);
    try {
      const r = await api.codeRevert(path);
      if (r?.ok) {
        setAgentMarks((m) => {
          const next = { ...m };
          delete next[path];
          return next;
        });
        setStale(null);
        refreshFromDisk();
        if (r.deleted) {
          const idx = panes.findIndex((p) => p.path === path);
          if (idx >= 0) close(idx);
          flash(`reverted: removed ${path} (the agent had created it)`);
        } else {
          void open(path, { reload: true });
          flash(`reverted ${path} to before the agent touched it`);
        }
      } else {
        flash(r?.error || 'nothing to revert for this file');
      }
    } catch {
      flash('revert failed - is the agent up?');
    } finally {
      setReviewBusy(false);
    }
  }, [flash, panes, close, open, refreshFromDisk]);

  /* An unreviewed change survives a page reload: the server keeps the
     snapshots, so re-mark the files it lists. Session-local marks (a change
     made while this page is open) win over the fetch. */
  useEffect(() => {
    let live = true;
    const ac = new AbortController();
    api.codeChanges(ac.signal).then((r) => {
      if (!live || !r?.changes?.length) return;
      setAgentMarks((m) => {
        const next = { ...m };
        for (const c of r.changes!) {
          if (c.path && !(c.path in next)) {
            next[c.path] = parseUnifiedMarks(String(c.diff || ''));
          }
        }
        return next;
      });
    }).catch(() => undefined);
    return () => { live = false; ac.abort(); };
  }, []);

  return (
    /* `.ide` scopes the editor palette (index.css): graphite chrome, a denser
       type scale and square rows. The rest of the console keeps its own look,
       so the editor reads as an editor without a second design system. */
    <div className="ide flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <main id={mainId} tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        {!zen && (
          <TopBar crumb="Code">
            {dirty && <Pill tone="info">browser draft</Pill>}
            <button type="button" onClick={() => setAgentOn((v) => !v)}
              aria-pressed={agentOn}
              title={agentOn ? 'Hide the agent panel' : 'Show the agent panel'}
              className={`flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] transition-colors ${
                agentOn
                  ? 'border-zinc-600 bg-white/[0.07] text-zinc-200'
                  : 'border-glass-border text-zinc-400 hover:bg-glass-hover'}`}>
              <Sparkles size={11} /> Agent
            </button>
            <button type="button" title="Command palette (Ctrl+Shift+P)"
              onClick={() => setPalette('cmd')}
              className="rounded border border-glass-border px-2 py-0.5 text-[11px] text-zinc-400 hover:bg-glass-hover">
              <Play size={11} />
            </button>
          </TopBar>
        )}

        {/* `flex-col lg:flex-row`, for the same reason as the root: below lg
            the explorer is `w-full`, so a row here hands it the whole viewport
            and squeezes the editor column to zero, which then overflows the
            document sideways. */}
        <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
          {/* Explorer */}
          {!zen && explorerOn && (
            <aside aria-label="Explorer"
              className="flex max-h-[34vh] w-full flex-none flex-col border-b border-glass-border bg-surface-0 lg:max-h-none lg:w-[232px] lg:border-b-0 lg:border-r xl:w-[260px]">
              <Explorer
                onOpen={open}
                activePath={pane?.path ?? null}
                onWorkspaceChange={refreshFromDisk}
                reloadAt={wsEpoch}
                changed={changedPaths}
              />
            </aside>
          )}

          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            {/* Tabs */}
            {!zen && (
              <div role="tablist" aria-label="Open editors"
                className="flex flex-none items-stretch overflow-x-auto border-b border-glass-border bg-surface-0">
                {panes.map((p, i) => {
                  const isDirty = p.body !== p.base;
                  return (
                    <div key={p.path} role="tab" aria-selected={i === active}
                      tabIndex={0}
                      onClick={() => setActivePath(p.path)}
                      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setActivePath(p.path); } }}
                      onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); close(i); } }}
                      title={p.path}
                      className={`group flex flex-none cursor-pointer items-center gap-1.5 border-t border-transparent px-3 py-1.5 text-[11.5px] ${
                        i === active ? 'border-accent bg-surface-1 text-zinc-100' : 'text-zinc-400 hover:bg-glass-hover'}`}>
                      {isDirty
                        ? <Circle size={9} className="flex-none fill-accent text-accent" />
                        : <span className="w-[9px] flex-none" />}
                      <span className="max-w-[150px] truncate">{p.path.split('/').pop()}</span>
                      <button type="button" aria-label={`Close ${p.path}`}
                        onClick={(e) => { e.stopPropagation(); close(i); }}
                        className="rounded p-0.5 text-zinc-muted opacity-0 hover:bg-white/10 hover:text-zinc-200 focus:opacity-100 group-hover:opacity-100">
                        <X size={11} />
                      </button>
                    </div>
                  );
                })}
                <button type="button" title="Go to File (Ctrl+P)"
                  onClick={() => setPalette('file')}
                  className="flex flex-none items-center px-2 text-zinc-muted hover:bg-glass-hover hover:text-zinc-200">
                  <PlusIcon />
                </button>
              </div>
            )}

            {/* Zen mode hides the top bar and the tab strip —
                which also hid any trace of WHICH file is being
                edited. A one-line crumb keeps the user oriented
                without giving zen mode back its chrome. */}
            {zen && pane && (
              <div className="flex-none truncate border-b border-glass-border bg-surface-0 px-3 py-1 text-[11px] text-zinc-muted">
                {pane.path}
              </div>
            )}

            {/* Breadcrumbs */}
            {pane && !zen && (
              <nav aria-label="Breadcrumbs"
                className="flex flex-none items-center gap-1 overflow-x-auto border-b border-glass-border bg-surface-1 px-3 py-1 text-[11px] text-zinc-muted">
                {pane.path.split('/').map((seg, i, arr) => (
                  <span key={i} className="flex items-center gap-1 whitespace-nowrap">
                    {i > 0 && <ChevronRight size={10} className="text-zinc-muted/60" />}
                    <span className={i === arr.length - 1 ? 'text-zinc-300' : ''}>{seg}</span>
                  </span>
                ))}
                <span className="ml-auto flex items-center gap-1">
                  {/* Editor actions live on the editor's own toolbar. They used
                      to sit in the agent panel header, which read as if the AI
                      owned go-to-definition and saving. */}
                  <button type="button" onClick={() => void goToDefinition()}
                    title="Go to definition (F12)"
                    disabled={!caretWord}
                    className="flex flex-none items-center gap-1 rounded border border-glass-border px-1.5 py-0.5 text-[10.5px] text-zinc-400 hover:text-zinc-200 disabled:opacity-40">
                    <FileCode2 size={11} /> <span className="hidden xl:inline">Definition</span>
                  </button>
                  <button type="button"
                    onClick={() => { if (caretWord) { setIntelWord(caretWord); setIntelOpen(true); } else flash('put the caret on a symbol first'); }}
                    title="Find references (Shift+F12)"
                    disabled={!caretWord}
                    className="flex flex-none items-center gap-1 rounded border border-glass-border px-1.5 py-0.5 text-[10.5px] text-zinc-400 hover:text-zinc-200 disabled:opacity-40">
                    <Globe size={11} /> <span className="hidden xl:inline">References</span>
                  </button>
                  <button type="button" onClick={() => setSaveDiff((s) => ({ ...s, open: true }))}
                    title="Save to disk (Ctrl+S)"
                    disabled={!pane || !dirty}
                    className="flex flex-none items-center gap-1 rounded border border-glass-border px-1.5 py-0.5 text-[10.5px] text-zinc-400 hover:text-zinc-200 disabled:opacity-40">
                    <Save size={11} /> <span className="hidden xl:inline">Save</span>
                  </button>
                  {lang === 'markdown' && (
                    <button type="button" onClick={() => setPreviewMd((v) => !v)}
                      aria-pressed={previewMd}
                      title={previewMd
                        ? 'Back to the source'
                        : 'Render this draft as markdown (the file on disk is untouched)'}
                      className={`flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10.5px] ${
                        previewMd
                          ? 'border-sky-400/50 text-sky-300'
                          : 'border-glass-border text-zinc-400 hover:text-zinc-200'}`}>
                      <Eye size={11} /> {previewMd ? 'Edit' : 'Preview'}
                    </button>
                  )}
                  {/* The language moved to the status bar; showing it here as
                      well was the second place saying the same thing. */}
                </span>
              </nav>
            )}

            {/* What the agent changed in this file, and the two ways out.
                The lines themselves carry the marks (green added, yellow
                edited, red deleted); this bar recounts them and waits for the
                user's verdict - the write is ALREADY on disk, so Keep only
                dismisses the review and Revert restores the snapshot. */}
            {pane && activeMarks && (
              <div className="flex flex-none items-center gap-2 border-b border-glass-border px-3 py-1 text-[11px] text-zinc-300"
                   style={{ background: 'rgba(63, 185, 80, 0.07)' }}>
                <span className="h-2 w-2 flex-none rounded-[2px]"
                      style={{ background: '#3fb950' }} />
                <span className="whitespace-nowrap">agent changed this file</span>
                {activeMarks.added.length > 0 && (
                  <span className="whitespace-nowrap" style={{ color: '#3fb950' }}>
                    +{activeMarks.added.length} added
                  </span>
                )}
                {activeMarks.modified.length > 0 && (
                  <span className="whitespace-nowrap" style={{ color: '#d29922' }}>
                    ~{activeMarks.modified.length} edited
                  </span>
                )}
                {activeMarks.deleted > 0 && (
                  <span className="whitespace-nowrap" style={{ color: '#f85149' }}>
                    −{activeMarks.deleted} deleted
                  </span>
                )}
                <span className="flex-1" />
                {reviewBusy && <Loader2 size={11} className="animate-spin text-zinc-400" />}
                <button type="button"
                  onClick={() => void revertChange(pane.path)}
                  disabled={reviewBusy}
                  title="Put the file back exactly as it was before the agent touched it"
                  className="flex-none whitespace-nowrap rounded border border-glass-border px-2 py-[2px] text-[11px] text-zinc-300 hover:border-rose-400/60 hover:text-rose-300 disabled:opacity-50">
                  Revert
                </button>
                <button type="button"
                  onClick={() => keepChange(pane.path)}
                  disabled={reviewBusy}
                  title="Keep the agent's change and clear the marks"
                  className="flex-none whitespace-nowrap rounded border border-emerald-500/40 px-2 py-[2px] text-[11px] text-emerald-300 hover:bg-emerald-500/10 disabled:opacity-50">
                  Keep
                </button>
              </div>
            )}

            <FindWidget state={find} setState={setFind} body={body}
              editor={ed.current} onReveal={reveal} />

            {/* Editor */}
            {!pane ? (
              /* bg-surface-1, not the app void: with no file open the column is
                 transparent, so the empty state sat on #090909 - a near-black
                 slab flanked by #181818 chrome, the loudest "unfinished" signal
                 in the section. It now paints the same surface the editor will. */
              <div className="flex min-h-0 flex-1 items-center justify-center bg-surface-1">
                <Empty icon={<Code2 size={22} />}
                  title="Pick a file to start editing"
                  /* Was "nothing is written to disk", which stopped being true
                     when the agent gained gated write tools. The honest split is
                     about WHO writes: your buffer stays here until you press
                     save, and the agent's writes wait for your approval. */
                  sub="Ctrl+P opens any file in the workspace. Your edits stay in this browser until you save; the agent writes straight to the workspace, marks every change, and you Keep or Revert each file afterwards." />
              </div>
            ) : (
              <div className="relative flex min-h-0 flex-1">
                {outlineOn && (
                  <aside aria-label="Symbol outline"
                    className="hidden max-h-[30vh] w-[210px] flex-none flex-col overflow-y-auto border-r border-glass-border bg-surface-0 lg:block">
                    <div className="flex flex-none items-center gap-1.5 px-3 py-2 text-[10.5px] font-bold uppercase tracking-[0.14em] text-zinc-muted">
                      <ListTree size={12} /> Outline
                    </div>
                    {outline.length ? outline.map((s, i) => (
                      <button key={`${s.line}-${s.name}-${i}`} type="button"
                        onClick={() => ed.current?.revealLine(s.line + 1)}
                        className={`flex w-full items-baseline gap-1.5 px-3 py-0.5 text-left text-[11.5px] hover:bg-glass-hover ${
                          cursor.line === s.line + 1 ? 'bg-accent/10 text-zinc-100' : 'text-zinc-400'}`}>
                        <span className={s.kind === 'class' ? 'text-amber-200' : 'text-emerald-300'}>
                          {s.kind === 'class' ? 'C' : s.kind === 'section' ? '§' : '†'}
                        </span>
                        <span className="truncate">{s.name}</span>
                      </button>
                    )) : (
                      <p className="px-3 py-2 text-[11px] text-zinc-muted">
                        no symbols found in this file
                      </p>
                    )}
                  </aside>
                )}
                {/* Monaco owns folding, the minimap, find/replace and every
                    keyboard editing behaviour, so none of that is drawn here
                    any more. Markdown files can swap it for the rendered
                    draft - READMEs and notes are written to be read. */}
                {previewMd && lang === 'markdown' ? (
                  <div
                    className="prose-chat absolute inset-0 overflow-y-auto bg-surface-0 px-5 py-4 text-[13px] leading-relaxed text-zinc-200"
                    /* renderMarkdown is the console's own safe renderer (the
                       Chat section renders every answer with it): it escapes
                       the source first, so a draft can contain anything. */
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(body) }}
                  />
                ) : (
                  <MonacoEditor
                    body={body}
                    lang={lang}
                    path={pane.path}
                    problems={problemLines}
                    marks={activeMarks ?? null}
                    ariaLabel={`Editor for ${pane.path}`}
                    wrap={wrap}
                    onChange={(v, info) => onType(v, info)}
                    onCursor={(info) => syncCursor(info)}
                    onHandle={(h) => { ed.current = h; }}
                    onFoldChange={setFolded}
                  />
                )}
              </div>
            )}

            {/* Bottom panel — one strip, VS Code style. Collapsed until it has
                something to show, so the editor keeps the height. */}
            <div className="flex flex-none flex-col border-t border-glass-border bg-surface-0">
              <div role="tablist" aria-label="Editor panel"
                className="flex h-[28px] flex-none items-center gap-0.5 px-1.5">
                <button type="button" role="tab"
                  aria-selected={panel === 'problems'}
                  onClick={() => setPanel(panel === 'problems' ? null : 'problems')}
                  className={`flex h-full items-center gap-1.5 border-t-2 px-2.5 text-[11.5px] font-medium transition-colors ${
                    panel === 'problems'
                      ? 'border-accent text-zinc-100'
                      : 'border-transparent text-zinc-500 hover:bg-surface-2 hover:text-zinc-300'}`}>
                  <CircleDot size={11} />
                  Problems
                  <span className={`rounded-sm px-1 text-[10px] tabular-nums ${
                    problems.length ? 'bg-danger-dim text-danger' : 'bg-white/5 text-zinc-500'}`}>
                    {problems.length}
                  </span>
                </button>
                <button type="button" role="tab"
                  aria-selected={panel === 'terminal'}
                  onClick={() => setPanel(panel === 'terminal' ? null : 'terminal')}
                  className={`flex h-full items-center gap-1.5 border-t-2 px-2.5 text-[11.5px] font-medium transition-colors ${
                    panel === 'terminal'
                      ? 'border-accent text-zinc-100'
                      : 'border-transparent text-zinc-500 hover:bg-surface-2 hover:text-zinc-300'}`}>
                  <Terminal size={11} />
                  Terminal
                  {termSeen && (
                    <span className={`h-1.5 w-1.5 rounded-full ${
                      termStatus === 'open' ? 'bg-emerald-400'
                        : termStatus === 'connecting' ? 'bg-amber-400'
                        : termStatus === 'error' ? 'bg-rose-400' : 'bg-zinc-500'}`} />
                  )}
                </button>
                <span className="flex-1" />
                {/* The project's REAL checks, not the buffer parser. `/api/code/check`
                    judges this draft with ast.parse; this runs pytest/tsc/npm test/
                    ruff/cargo/go vet. Slow by nature — that is the point, and it is
                    the only signal that catches a change which parses but is wrong. */}
                <button type="button"
                  onClick={() => { setPanel('problems'); void runProjectChecks(); }}
                  disabled={checksBusy}
                  aria-label="Run the project's lint, typecheck and tests"
                  className="flex h-[21px] flex-none items-center gap-1 rounded border border-white/10 px-1.5 text-[10.5px] font-semibold text-zinc-400 hover:border-zinc-600 hover:text-zinc-200 disabled:opacity-60">
                  {checksBusy ? <Loader2 size={10} className="animate-spin" />
                    : <Play size={10} />}
                  {checksBusy ? 'running…' : 'Run checks'}
                </button>
                {panel && (
                  <button type="button" onClick={() => setPanel(null)}
                    title="Hide panel"
                    aria-label="Hide panel"
                    className="flex h-[21px] w-[21px] flex-none items-center justify-center rounded text-zinc-500 hover:text-zinc-200">
                    <ChevronDown size={12} />
                  </button>
                )}
              </div>

              {panel === 'problems' && (
                <div className="max-h-[30vh] overflow-y-auto border-t border-glass-border">
                  {checks && (
                    <div className="border-b border-glass-border">
                      <div className="flex items-center gap-2 px-3 py-1.5">
                        <Pill tone={checks.ok ? 'ok' : 'err'}>
                          {checks.ok ? 'all checks passed' : 'checks failed'}
                        </Pill>
                        <span className="text-[11px] text-zinc-muted">
                          {checks.checks_run} checker{checks.checks_run === 1 ? '' : 's'}
                          {checks.cwd ? ` · ${checks.cwd}` : ''}
                        </span>
                      </div>
                      {checks.error && (
                        <p className="px-3 pb-2 text-[11.5px] text-amber-300">
                          {checks.error}
                        </p>
                      )}
                      {checks.results.map((r) => (
                        <details key={r.tool} className="border-t border-glass-border/60">
                          <summary className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-[11.5px] hover:bg-glass-hover">
                            {r.ok ? <CircleCheck size={11} className="flex-none text-emerald-400" />
                              : <CircleDot size={11} className="flex-none text-rose-400" />}
                            <span className="font-semibold">{r.tool}</span>
                            <Pill tone={r.ok ? 'ok' : 'err'}>
                              {r.exit_code == null ? 'error' : `exit ${r.exit_code}`}
                            </Pill>
                            <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-muted">
                              {(r.stdout || r.error || r.stderr || '').trim().split('\n').slice(-1)[0]}
                            </span>
                          </summary>
                          <pre className="max-h-56 overflow-auto whitespace-pre-wrap px-3 pb-2 text-[11px] leading-relaxed text-zinc-300">
                            {[r.stdout, r.stderr, r.error].filter(Boolean).join('\n')}
                          </pre>
                        </details>
                      ))}
                    </div>
                  )}

                  {problems.length > 0 ? (
                    <ul className="divide-y divide-glass-border">
                      {problems.map((p, i) => (
                        <li key={`${p.line}-${p.col}-${i}`}>
                          <button type="button"
                            onClick={() => replaceRange(
                              offsetOf(body, { line: p.line, col: p.col }),
                              offsetOf(body, { line: p.line, col: p.col }), '')}
                            className="flex w-full items-start gap-2 px-3 py-1.5 text-left text-[11.5px] hover:bg-glass-hover">
                            <CircleDot size={11} className="mt-[3px] flex-none text-rose-400" />
                            <span className="flex-none tabular-nums text-zinc-muted">
                              {p.line}:{p.col}
                            </span>
                            <span className="min-w-0 flex-1 text-zinc-200">{p.message}</span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <ul className="space-y-1 px-3 py-2 text-[11.5px] text-zinc-400">
                      <li className="flex items-start gap-2">
                        <span className="mt-[2px] flex-none text-emerald-400">✓</span>
                        <span>{checking ? 'checking the draft…' : (checkNote
                          ?? (lang === 'python'
                            ? 'No syntax errors — parsed with Python’s own ast module.'
                            : 'No syntax errors in the delimiters of this draft.'))}</span>
                      </li>
                      <li className="pl-5 text-[11px] text-zinc-500">
                        Syntax only — type errors and go-to-definition need a language
                        server. Run checks runs the project’s own lint, typecheck and tests.
                      </li>
                    </ul>
                  )}
                </div>
              )}

              <div className={panel === 'terminal' ? 'h-[220px]' : 'h-0 overflow-hidden'}>
                <TerminalDock bare open={panel === 'terminal'} onStatus={onTermStatus} />
              </div>
            </div>
          </div>

          {/* The agent. Tool-using, so it reads and searches the project on its
              own instead of being handed a context slice; the trace of what it
              did is part of the answer, not a debug log. It owns its own
              conversation, abort handle and model pick. Hidden with CSS rather
              than unmounted, so collapsing the pane cannot eat a typed draft. */}
          <div className={agentOn
            ? 'flex min-h-0 min-w-0 w-full flex-none lg:w-[340px] xl:w-[400px] 2xl:w-[440px]'
            : 'hidden'}>
          <div className="flex min-h-0 min-w-0 flex-1 flex-col border-t border-glass-border bg-surface-0 lg:border-t-0">
            {/* No second "AGENT" bar above the panel's own header: the header
                already carries the sparkle, the conversation title and the
                file chip, and a title row over a title row is just chrome. */}
            <AgentPanel
              path={pane?.path ?? ''}
              body={body}
              model={aiModel}
              onModelChange={setAiModel}
              onAsk={async (q, hist, onStep, onMeta, signal, onText, extra) => {
                let answer = '';
                /* A run that ends without `done` is a broken stream, not a
                   finished answer. The server always sends done on a clean
                   stop; the old loop just fell out and returned whatever
                   partial text it had accumulated, which read as success. */
                let sawDone = false;
                /* Reasoning arrives as a stream of small deltas. Turning each
                   one into its own step is what produced a wall of identical
                   "thinking" rows - one per word. They accumulate into ONE row
                   instead, and a tool call or answer token closes it, so the
                   activity still reads think -> call -> result -> think. */
                let thinkRow: string | null = null;
                let thinkBuf = '';
                let thinkN = 0;
                const closeThink = () => { thinkRow = null; thinkBuf = ''; };
                for await (const f of api.codeAgentStream(
                  q, pane?.path ?? '', body, hist, signal,
                  aiModel || undefined, undefined, extra, true)) {
                  if (f.type === 'delta' && f.text) {
                    closeThink();
                    answer += f.text;
                    onText?.(f.text);
                  } else if (f.type === 'tool_call') {
                    closeThink();
                    onStep({ id: f.id, label: f.name ?? 'tool',
                             detail: briefArgs(f), kind: 'call' });
                  } else if (f.type === 'tool_result') {
                    closeThink();
                    /* Pair the result with its parked write (permission and
                       tool_result carry different ids). Consume the slot on
                       failure too: a refused write must not leave marks
                       behind for a later call of the same tool. */
                    const wi = pendingWrites.current.findIndex(
                      (w) => w.tool === f.name);
                    const w = wi >= 0 ? pendingWrites.current[wi] : null;
                    if (wi >= 0) pendingWrites.current.splice(wi, 1);
                    if (f.ok !== false && w && f.name && WRITE_TOOLS.has(f.name)) {
                      /* A write tool succeeded, so the workspace changed on
                         disk behind the explorer's back. Re-read the tree now
                         - not at the end of the turn - so the new file shows
                         up as soon as it exists. (A file the agent had just
                         created used to stay invisible, and a folder already
                         showing "empty" kept saying "empty", until the user
                         collapsed and re-expanded it by hand.) */
                      refreshFromDisk();
                      setAgentMarks((m) => ({ ...m, [w.path]: w.marks }));
                      setSyncPath(w.path);
                    }
                    onStep({ id: f.id, label: f.name ?? 'tool',
                             detail: outcomeBrief(f), kind: f.ok === false ? 'err' : 'ok' });
                  } else if (f.type === 'write') {
                    /* Auto mode: the write is already dispatched - no
                       decision is expected. Park the payload; the matching
                       tool_result applies the marks and syncs the pane. No
                       activity row here: the tool_call frame right after
                       this one already shows the write in the trace. */
                    closeThink();
                    if (WRITE_TOOLS.has(String(f.tool || ''))) {
                      pendingWrites.current.push({
                        path: String(f.path || ''),
                        tool: String(f.tool || ''),
                        marks: parseUnifiedMarks(String(f.diff || '')),
                      });
                    }
                  } else if (f.type === 'permission') {
                    /* The run is now parked in the server, waiting on this
                       decision. Showing the dialog is what releases it - the
                       loop does not continue until the operator chooses. */
                    closeThink();
                    /* Agent-relative args.path is NOT a client path: the
                       server sends the client-visible one alongside. */
                    if (WRITE_TOOLS.has(String(f.tool || ''))) {
                      pendingWrites.current.push({
                        path: String(f.path || ''),
                        tool: String(f.tool || ''),
                        marks: parseUnifiedMarks(String(f.diff || '')),
                      });
                    }
                    setPendingEdit({
                      id: String(f.id || ''), tool: String(f.tool || 'write'),
                      summary: String(f.summary || ''), diff: String(f.diff || ''),
                    });
                    onStep({ id: f.id, label: f.tool ?? 'write',
                             detail: f.summary ?? 'waiting for your approval',
                             kind: 'ask' });
                  } else if (f.type === 'thinking' && f.text) {
                    if (thinkRow === null) { thinkRow = `think-${thinkN++}`; thinkBuf = f.text; }
                    else thinkBuf += f.text;
                    onStep({ id: thinkRow, label: 'thinking', text: thinkBuf, kind: 'think' });
                  } else if (f.type === 'done') {
                    sawDone = true;
                    if (typeof f.answer === 'string') answer = f.answer;
                    /* The model that actually answered, not the one asked
                       for: with Auto the gateway picks, and echoing the
                       request would be a lie the user cannot trace. */
                    if (f.model || f.provider) {
                      onStep({ label: 'model',
                               detail: [f.model, f.provider].filter(Boolean).join(' · '),
                               kind: 'model' });
                    }
                    /* A stopped run is not an error - it is a run that hit a
                       stated limit. Surface the reason instead of pretending
                       the (possibly empty) answer is complete. */
                    if (f.stopped) {
                      onStep({ label: 'stopped', detail: f.stopped, kind: 'err' });
                    }
                    onMeta({
                      model: f.model, thinking: f.thinking,
                      usage: f.input_tokens != null && f.output_tokens != null
                        ? { input: f.input_tokens, output: f.output_tokens }
                        : undefined,
                    });
                  } else if (f.type === 'error') {
                    throw new Error(f.text || 'the agent stopped');
                  }
                }
                if (!sawDone) {
                  throw new Error('the agent stream ended before the run finished');
                }
                return answer;
              }}
              files={known}
              gitView={gitView}
              gitBusy={gitBusy}
              gitOut={gitOut ? { text: gitOut.text, ok: !!gitOut.ok } : null}
              onGit={(t) => loadGit(t)}
            />
          </div>
          </div>
        </div>

        {/* Workspace search */}
        {search.open && (
          <div className="flex flex-none flex-col border-b border-glass-border bg-surface-1">
            <div className="flex items-center gap-2 px-3 py-2">
              <Search size={12} className="flex-none text-zinc-muted" />
              <input
                value={search.q}
                onChange={(e) => {
                  const q = e.target.value;
                  setSearch((s) => ({ ...s, q }));
                  if (q.trim().length < 3) { setSearch((s) => ({ ...s, hits: [] })); return; }
                  setSearch((s) => ({ ...s, busy: true }));
                  /* Debounce, cancel, and drop out-of-order replies.
                     This used to fire on every keystroke with no debounce and
                     no AbortSignal, so typing "DOCUMENT_EMBED" queued ~10 full
                     workspace scans; the server walked ~2100 files per request,
                     and because those handlers are `async def` doing blocking
                     I/O, every keystroke stalled every other /api route too.
                     Responses also landed out of order, so an older query's
                     results overwrote a newer one's. */
                  searchReq.current += 1;
                  const seq = searchReq.current;
                  window.clearTimeout(searchTimer.current);
                  searchAbort.current?.abort();
                  const ac = new AbortController();
                  searchAbort.current = ac;
                  searchTimer.current = window.setTimeout(() => {
                    api.codeSearch(q, 200, ac.signal)
                      .then((r) => {
                        if (seq !== searchReq.current) return;
                        setSearch((s) => ({ ...s, hits: r?.matches ?? [], busy: false }));
                      })
                      .catch(() => {
                        if (seq !== searchReq.current) return;
                        if (ac.signal.aborted) return;
                        setSearch((s) => ({ ...s, hits: [], busy: false }));
                      });
                  }, 250);
                }}
                onKeyDown={(e) => { if (e.key === 'Escape') setSearch((s) => ({ ...s, open: false })); }}
                placeholder="Search across the workspace (Ctrl+Shift+F)"
                aria-label="Search across the workspace"
                className="min-w-0 flex-1 rounded border border-glass-border bg-surface-0 px-2 py-1 text-[12px] text-zinc-100 outline-none focus:border-accent/50"
              />
              <span className="text-[10.5px] tabular-nums text-zinc-muted">
                {search.busy ? 'searching…'
                  : search.q.trim().length < 3 ? 'type 3+ characters'
                  : `${search.hits.length} match${search.hits.length === 1 ? '' : 'es'}`}
              </span>
              <button type="button" onClick={() => setSearch((s) => ({ ...s, open: false }))}
                aria-label="Close search"
                className="rounded p-0.5 text-zinc-muted hover:bg-glass-hover hover:text-zinc-200">
                <X size={13} />
              </button>
            </div>
            {search.hits.length > 0 && (
              <ul className="max-h-[24vh] overflow-y-auto border-t border-glass-border">
                {search.hits.map((h, i) => (
                  <li key={`${h.path}-${h.line}-${i}`}>
                    <button type="button"
                      onClick={async () => {
                        setSearch((s) => ({ ...s, open: false }));
                        await open(h.path);
                        /* The buffer for a newly opened file is not in this
                           closure, so the jump is applied by an effect once the
                           pane is actually active rather than guessed here. */
                        setJump({ path: h.path, line: h.line, col: h.col });
                      }}
                      className="flex w-full items-baseline gap-2 px-3 py-1 text-left text-[11.5px] hover:bg-glass-hover">
                      <span className="flex-none truncate text-zinc-muted">{h.path}</span>
                      <span className="flex-none tabular-nums text-zinc-muted">{h.line}</span>
                      <span className="min-w-0 flex-1 truncate text-zinc-300">{h.text}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* Stale-source notice */}
        {stale && (
          <div className="flex flex-none items-center gap-2 border-b border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-[11.5px] text-amber-200">
            <TriangleAlert size={12} className="flex-none" />
            <span className="min-w-0 flex-1 truncate">
              {stale} changed on the server since it was opened here.
            </span>
            <button type="button"
              onClick={() => open(stale, { reload: true })}
              className="flex-none rounded border border-amber-500/40 px-1.5 py-0.5 hover:bg-amber-500/20">
              Reload from server
            </button>
          </div>
        )}

        {/* Status bar. The agent-writes notice lives HERE, not in the top bar:
            it is standing context about how this workspace behaves, and the
            status bar is where an editor keeps that (branch, remote,
            encoding). In the top bar it competed with the breadcrumb for
            attention every second. */}
        <div className="ide-status flex flex-none items-center gap-x-3 overflow-hidden whitespace-nowrap border-t border-glass-border bg-surface-0 px-3 py-1 text-[10.5px] text-zinc-muted">
          <span className="hidden flex-none items-center gap-1 text-zinc-500 xl:flex" title="The coding agent writes straight to the workspace; every change is marked in the editor and can be kept or reverted per file afterwards.">
            <ShieldQuestion size={10} /> agent auto-applies · review after
          </span>
          {problems.length > 0 && (
            <span className="flex-none text-rose-400">{problems.length} error{problems.length === 1 ? '' : 's'}</span>
          )}
          {/* Right cluster in VS Code's order: cursor, then text settings, then
              the toggles. The language used to sit on the far left wearing an
              icon, in front of Ln/Col, which read like a stray filter chip. */}
          <span className="ml-auto flex min-w-0 flex-none items-center gap-x-3">
            <span className="flex-none">Ln {cursor.line}, Col {cursor.col}</span>
            {selLen > 0 && <span className="hidden flex-none xl:inline">({selLen} selected)</span>}
            {folded > 0 && <span className="hidden flex-none xl:inline">{folded} folded</span>}
            <span className="hidden flex-none xl:inline">Spaces: 4</span>
            <span className="hidden flex-none xl:inline">UTF-8</span>
            <span className="hidden flex-none 2xl:inline">{body.length} chars</span>
            <span className="flex-none">{pane ? LANG_LABEL[lang] ?? lang : 'no file'}</span>
            <span className="flex flex-none items-center gap-0.5">
              <button type="button" onClick={() => setOutlineOn((v) => !v)}
                aria-pressed={outlineOn} title="Toggle symbol outline"
                className={`rounded px-1 ${outlineOn ? 'text-accent' : 'hover:text-zinc-300'}`}>
                <ListTree size={10} />
              </button>
              <button type="button" onClick={() => setShortcuts(true)}
                title="Keyboard shortcuts (Ctrl+,)"
                className="rounded px-1 hover:text-zinc-300">
                <Keyboard size={10} />
              </button>
              <button type="button" onClick={() => setWrap((v) => !v)}
                aria-pressed={wrap} title="Toggle word wrap"
                className={`rounded px-1 ${wrap ? 'text-accent' : 'hover:text-zinc-300'}`}>
                <WrapText size={10} />
              </button>
              <button type="button" onClick={() => setMinimapOn((v) => !v)}
                aria-pressed={minimapOn} title="Toggle minimap"
                className={`rounded px-1 ${minimapOn ? 'text-accent' : 'hover:text-zinc-300'}`}>
                <Maximize2 size={10} />
              </button>
              {!zen && (
                <button type="button" onClick={() => setExplorerOn((v) => !v)}
                  aria-pressed={explorerOn} title="Toggle explorer"
                  className={`rounded px-1 ${explorerOn ? 'text-accent' : 'hover:text-zinc-300'}`}>
                  {explorerOn ? <PanelLeftClose size={10} /> : <PanelLeftOpen size={10} />}
                </button>
              )}
              <button type="button" onClick={() => setZen((v) => !v)}
                aria-pressed={zen} title="Zen mode"
                className={`rounded px-1 ${zen ? 'text-accent' : 'hover:text-zinc-300'}`}>
                <Square size={9} />
              </button>
            </span>
          </span>
          <span className="flex min-w-0 items-center" role="status">
            {note
              ? <span className="truncate text-accent/90">{note}</span>
              : <span className="hidden truncate 2xl:inline">draft stays in this browser until saved</span>}
          </span>
        </div>

        {/* Overlays */}
        {palette === 'file' && (
          <Palette mode="file" files={known} truncated={filesTruncated}
            onClose={() => setPalette(null)}
            onPick={(f) => { setPalette(null); void open(f); }} />
        )}
        {palette === 'cmd' && (
          <Palette mode="cmd" files={CMDS.map((c) => c.label)} onClose={() => setPalette(null)}
            onPick={(label) => {
              setPalette(null);
              CMDS.find((c) => c.label === label)?.run();
            }} />
        )}
        {shortcuts && <ShortcutSheet onClose={() => setShortcuts(false)} />}
        {goto !== null && (
          <GotoBox initial={goto} max={lines.length}
            onCancel={() => setGoto(null)} onGo={gotoLine} />
        )}
        {/* The save review: the buffer against what is on disk, as a real
            diff, before anything is written. */}
        {saveDiff.open && pane && (
          <DiffView
            title="Save to disk"
            subtitle={`${pane.path} — ${diffRows.filter((r) => r.kind === 'add').length} added, ${diffRows.filter((r) => r.kind === 'del').length} removed`}
            rows={diffRows}
            applyLabel={saving ? 'writing…' : 'Save to disk'}
            onApply={() => void saveToDisk(true)}
            onClose={() => setSaveDiff((s) => ({ ...s, open: false }))}
            busy={saving}
            note={saveDiff.note}
            error={saveDiff.error}
          />
        )}
        {/* References / rename / hover, on the word at the caret. */}
        {intelOpen && pane && (
          <IntelModal path={pane.path} word={intelWord}
            onJump={(f, l) => { setIntelOpen(false); void jumpTo(f, l); }}
            onClose={() => setIntelOpen(false)} />
        )}
        {/* The agent's write gate. Mounted outside <main> because it is modal
            over the whole panel, and it has no backdrop-dismiss: a write that
            is neither accepted nor refused must not proceed. */}
        <EditPermissionDialog pending={pendingEdit} busy={editBusy}
          onDecide={decideEdit} />
      </main>
    </div>
  );
}

function PlusIcon() {
  return <span className="text-[13px] leading-none">+</span>;
}

const SHORTCUTS: string[][] = [
  ['Ctrl+P', 'Go to file (fuzzy)'],
  ['Ctrl+Shift+P', 'Command palette'],
  ['Ctrl+Shift+F', 'Search across the workspace'],
  ['Ctrl+F', 'Find in file'],
  ['Ctrl+G', 'Go to line / column'],
  ['Ctrl+,', 'This list'],
  ['Ctrl+S', 'Nothing — drafts are browser-only'],
  ['Ctrl+W', 'Close tab'],
  ['Ctrl+Tab', 'Next tab'],
  ['Ctrl+Z / Ctrl+Y', 'Undo / redo'],
  ['Ctrl+/', 'Toggle line comment'],
  ['Ctrl+D', 'Select next occurrence'],
  ['Ctrl+Shift+K', 'Delete line'],
  ['Alt+Shift+Up/Down', 'Copy line up / down'],
  ['Alt+Up/Down', 'Move the caret to the line above / below'],
  ['Escape', 'Close a panel, or leave the editor'],
  ['Ctrl+Shift+[ / ]', 'Fold / unfold the region at the caret'],
  ['Tab / Shift+Tab', 'Indent / outdent'],
  ['Enter', 'Keep indentation, and open a block after ":" or "{"'],
  ['(  [  {  "  \'  `', 'Auto-close, and wrap a selection'],
  ['Backspace', 'Delete a matching bracket pair'],
];
function ShortcutSheet({ onClose }: { onClose: () => void }) {
  const trapRef = useRef<HTMLDivElement | null>(null);
  useFocusTrap(trapRef, { onEscape: onClose });
  return (
    <div
      ref={trapRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
         onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div role="dialog" aria-modal="true" aria-label="Keyboard shortcuts"
           className="max-h-[80vh] w-[min(560px,94vw)] overflow-hidden rounded-lg border border-glass-border bg-surface-1 shadow-2xl">
        <div className="flex items-center gap-2 border-b border-glass-border px-3.5 py-2.5">
          <Keyboard size={13} className="text-accent" />
          <span className="text-[12.5px] font-semibold text-zinc-100">Keyboard shortcuts</span>
          <button type="button" onClick={onClose} aria-label="Close shortcuts"
            className="ml-auto rounded p-0.5 text-zinc-muted hover:bg-glass-hover hover:text-zinc-200">
            <X size={13} />
          </button>
        </div>
        <ul className="max-h-[66vh] overflow-y-auto py-1">
          {SHORTCUTS.map(([k, d]) => (
            <li key={k} className="flex items-baseline gap-3 px-3.5 py-1 hover:bg-glass-hover">
              <kbd className="w-[168px] flex-none rounded border border-glass-border bg-surface-0 px-1.5 py-0.5 text-left font-mono text-[10.5px] text-zinc-300">
                {k}
              </kbd>
              <span className="text-[12px] text-zinc-400">{d}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function GotoBox({ initial, max, onGo, onCancel }: {
  initial: string; max: number; onGo: (v: string) => void; onCancel: () => void;
}) {
  const [v, setV] = useState(initial);
  const ref = useRef<HTMLInputElement>(null);
  const trapRef = useRef<HTMLDivElement | null>(null);
  useFocusTrap(trapRef, { onEscape: onCancel });
  useEffect(() => { ref.current?.focus(); ref.current?.select(); }, []);
  return (
    <div
      ref={trapRef}
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 pt-[12vh]"
         onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
      <div role="dialog" aria-modal="true" aria-label="Go to line"
           className="w-[min(420px,92vw)] overflow-hidden rounded-lg border border-glass-border bg-surface-1 shadow-2xl">
        <div className="flex items-center gap-2 border-b border-glass-border px-3 py-2">
          <span className="text-[11px] text-zinc-muted">Go to line (1-{max})</span>
          <input ref={ref} value={v} inputMode="numeric"
            onChange={(e) => setV(e.target.value)}
            onKeyDown={(e) => {
              // preventDefault on every branch. Without it, `onGo`
              // unmounts this box, `useFocusTrap`'s cleanup returns
              // focus to the textarea, and Chromium then applies the
              // Enter default to that newly focused textarea —
              // INSERTING A NEWLINE into the file. The corruption was
              // then persisted to the localStorage draft. The palette's
              // handler already did this; GotoBox did not.
              if (e.key === 'Enter') { e.preventDefault(); onGo(v); }
              if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
            }}
            aria-label="Line number"
            className="ml-auto w-28 rounded border border-glass-border bg-surface-0 px-2 py-1 font-mono text-[12px] text-zinc-100 outline-none focus:border-accent/50" />
        </div>
      </div>
    </div>
  );
}
