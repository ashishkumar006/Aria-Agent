/* Code — a browser editor over the agent's two source trees.

   Scope, stated plainly because it matters:

   * The agent serves these files READ ONLY (`/api/code/*`, path-confined to
     `S9SharedCode/code` and `llm_gatewayV9`). There is no write, move, delete
     or execute route, so nothing typed here can reach the disk or run.
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
  Braces, ChevronDown, ChevronRight, Circle, Code2, Columns2,
  FileCode2, FileText, Folder, FolderOpen, Maximize2, PanelLeftClose,
  PanelLeftOpen, Play, Search, Square, Terminal, TriangleAlert, WrapText, X,
  Keyboard, ListTree, CircleDot,
} from 'lucide-react';
import { Rail, TopBar, Empty, Pill, SkipLink } from '../components/ui';
import { useFocusTrap } from '../lib/a11y';
import Editor from '../components/CodeEditor';

/* A draft only ever exists in this browser - the server never receives it - so
   before anything destructive touches it, the user gets a copy they can keep.
   A revoke-style data URL rather than a blob URL: nothing to revoke, and it
   cannot outlive the click. */
function downloadText(name: string, text: string) {
  const url = `data:text/plain;charset=utf-8,${encodeURIComponent(text)}`;
  const a = document.createElement('a');
  a.href = url;
  a.download = name.replace(/[\\/]/g, '_');
  a.click();
}
import { foldRanges, symbols as symbolsOf, type Symbol } from '../lib/lang';
import { useHistory, type Snapshot } from '../lib/useHistory';
import {
  api, type CodeEntry, type CodeFile, type CodeProblem, type CodeMatch,
} from '../api';

const STORE_KEY = 'aria.code.drafts.v1';
const COMMENT: Record<string, string> = {
  python: '# ', sh: '# ', shell: '# ', yaml: '# ', yml: '# ', toml: '# ',
  ini: '; ', sql: '-- ', css: '/* ', html: '<!-- ', javascript: '// ',
  typescript: '// ', typescriptreact: '// ', json: '// ', markdown: '<!-- ',
};
const LANG_LABEL: Record<string, string> = {
  python: 'Python', typescript: 'TypeScript', typescriptreact: 'TypeScript React',
  javascript: 'JavaScript', json: 'JSON', yaml: 'YAML', toml: 'TOML',
  markdown: 'Markdown', css: 'CSS', html: 'HTML', shell: 'Shell', sql: 'SQL',
  text: 'Plain Text', ini: 'Config',
};

/* ── small helpers ─────────────────────────────────────────────────────── */

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

/* ── editor state ───────────────────────────────────────────────────────── */

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

function lineSlice(text: string, line: number): string {
  const starts = offsets(text);
  const i = line - 1;
  if (i < 0 || i >= starts.length) return '';
  const end = i + 1 < starts.length ? starts[i + 1] - 1 : text.length;
  return text.slice(starts[i], end);
}

/* ── explorer ───────────────────────────────────────────────────────────── */

function Explorer({
  onOpen, activePath,
}: { onOpen: (p: string) => void; activePath: string | null }) {
  const [tree, setTree] = useState<Record<string, CodeEntry[]>>({});
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [roots, setRoots] = useState<string[]>([]);

  useEffect(() => {
    let live = true;
    api.codeRoots()
      .then((r) => { if (live && r?.roots) setRoots(r.roots.map((x) => x.name)); })
      .catch((e) => {
        /* Swallowed before, so a failed root fetch left the
           explorer on "reading workspace…" forever with no way
           to tell broken from slow. */
        if (live) setErr(e instanceof Error ? e.message : 'could not read the workspace');
      });
    return () => { live = false; };
  }, []);

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
    /* Only auto-open paths that are actually reachable. The roots are
       `S9SharedCode/code` and `llm_gatewayV9`, so a file at
       `S9SharedCode/code/auth.py` yields the candidate `S9SharedCode` —
       which is inside neither root. Requesting it returned "path outside
       the code workspace", the error stuck forever (the dir never enters
       `tree`, so `!(d in tree)` stayed true and every tree change re-fired
       it), and it MASKED genuine failures: a 503 from /api/code/roots was
       correctly shown until opening a file replaced it with this. */
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
    />
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-none items-center gap-1.5 px-3 py-2 text-[10.5px] font-bold uppercase tracking-[0.14em] text-zinc-muted">
        Explorer
        {busy && <span className="ml-auto text-[10px] normal-case text-accent/70">reading…</span>}
      </div>
      {err && (
        <p className="px-3 py-1.5 text-[11px] text-amber-400/90">{err}</p>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto pb-2">
        <ul>{roots.map((r) => renderDir(r, 0))}</ul>
        {!roots.length && !err && (
          <p className="px-3 py-2 text-[11px] text-zinc-muted">reading workspace…</p>
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
  name, path, depth, open, toggle, tree, onOpen, activePath, busy,
}: {
  name: string; path: string; depth: number; open: Set<string>;
  toggle: (d: string) => void; tree: Record<string, CodeEntry[]>;
  onOpen: (p: string) => void; activePath: string | null; busy: boolean;
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
          ? <FolderOpen size={12} className="flex-none text-accent/80" />
          : <Folder size={12} className="flex-none text-accent/60" />}
        <span className="truncate">{name}</span>
      </button>
      {isOpen && (
        <ul>
          {(tree[path] ?? []).map((e) => {
            const child = `${path}/${e.name}`;
            return e.dir ? (
              <DirNode key={child} name={e.name} path={child} depth={depth + 1}
                open={open} toggle={toggle} tree={tree} onOpen={onOpen}
                activePath={activePath} busy={busy} />
            ) : (
              <li key={child}>
                <button
                  type="button"
                  onClick={() => onOpen(child)}
                  title={child}
                  aria-current={activePath === child ? 'true' : undefined}
                  className={`flex w-full items-center gap-1.5 rounded py-[3px] pr-2 text-left text-[12px] ${
                    activePath === child
                      ? 'bg-accent/10 text-zinc-100'
                      : 'text-zinc-400 hover:bg-glass-hover'
                  }`}
                  style={{ paddingLeft: 20 + depth * 12 }}
                >
                  {e.name.endsWith('.py')
                    ? <FileCode2 size={12} className="flex-none text-accent/80" />
                    : /\.tsx?$/.test(e.name)
                      ? <FileCode2 size={12} className="flex-none text-sky-400/80" />
                      : <FileText size={12} className="flex-none text-zinc-muted" />}
                  <span className="truncate">{e.name}</span>
                </button>
              </li>
            );
          })}
          {!(tree[path] ?? []).length && (
            <li className="py-1 text-[11px] text-zinc-muted"
                style={{ paddingLeft: 20 + depth * 12 }}>
              {busy ? 'loading…' : 'empty'}
            </li>
          )}
        </ul>
      )}
    </li>
  );
}

/* ── minimap ────────────────────────────────────────────────────────────── */

function Minimap({ lines, cursorLine, height }: {
  lines: string[]; cursorLine: number; height: number;
}) {
  const rows = lines.slice(0, 4000);
  return (
    <div aria-hidden className="relative flex-none select-none overflow-hidden border-l border-glass-border bg-surface-0"
         style={{ width: 74, height }}>
      <div className="absolute inset-y-0 right-0 w-1.5">
        {rows.map((l, i) => (
          <div key={i} className="flex items-end gap-px px-0.5" style={{ height: 3 }}>
            <span className="h-[1px] flex-none bg-zinc-600/70"
                  style={{ width: `${Math.min(100, l.trim().length * 2.2)}%` }} />
          </div>
        ))}
      </div>
      <div className="absolute inset-x-0 bg-accent/10"
           style={{
             top: `${(Math.max(0, cursorLine - 1) / Math.max(1, rows.length)) * height}px`,
             height: Math.max(12, (height / Math.max(1, rows.length)) * 3),
           }} />
    </div>
  );
}

/* ── find widget ────────────────────────────────────────────────────────── */

interface FindState { open: boolean; query: string; replace: string; cs: boolean }

function FindWidget({ state, setState, body, taRef, onReveal }: {
  state: FindState; setState: (s: FindState) => void;
  body: string; taRef: React.RefObject<HTMLTextAreaElement | null>;
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
            const el = taRef.current;
            if (!el || !state.query) return;
            const hay = state.cs ? body : body.toLowerCase();
            const n = state.cs ? state.query : state.query.toLowerCase();
            const at = hay.indexOf(n, el.selectionStart);
            if (at === -1) return;
            const next = body.slice(0, at) + state.replace + body.slice(at + n.length);
            const setter = (Object.getOwnPropertyDescriptor(
              window.HTMLTextAreaElement.prototype, 'value')?.set);
            setter?.call(el, next);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            /* Advance the caret past the replacement so the next
               Replace walks on to the following match. The
               selection used to stay put, so every click
               re-found and re-replaced the SAME match and the
               walk never moved. */
            const end = Math.min(at + state.replace.length, next.length);
            el.setSelectionRange(end, end);
          }}
          className="rounded border border-glass-border px-1.5 py-0.5 text-[10.5px] text-zinc-300 disabled:opacity-40 hover:bg-glass-hover">
          Replace
        </button>
        <button type="button" disabled={!total}
          onClick={() => {
            const el = taRef.current;
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
            const setter = (Object.getOwnPropertyDescriptor(
              window.HTMLTextAreaElement.prototype, 'value')?.set);
            setter?.call(el, out);
            el.dispatchEvent(new Event('input', { bubbles: true }));
          }}
          className="rounded border border-glass-border px-1.5 py-0.5 text-[10.5px] text-zinc-300 disabled:opacity-40 hover:bg-glass-hover">
          All
        </button>
      </div>
    </div>
  );
}

/* ── quick open / command palette ───────────────────────────────────────── */

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

/* ── the editor ─────────────────────────────────────────────────────────── */

const BRACKETS: Record<string, [string, string]> = {
  python: ['(', ')'], typescript: ['(', ')'], typescriptreact: ['(', ')'],
  javascript: ['(', ')'], json: ['(', ')'], css: ['(', ')'],
  yaml: ['(', ')'], toml: ['(', ')'], sql: ['(', ')'], html: ['(', ')'],
};

export default function Code() {
  const [panes, setPanes] = useState<Pane[]>([]);
  /* The active tab is tracked by PATH, not by index. It was an index, and
     `open()` appended the new pane then set the active index to `active + 1`
     - which is only the new pane's index when the previous active tab was the
     last one. Opening a file while any other tab was selected left the new
     pane invisible: no editor, no textarea, just a tab that looked broken. */
  const [activePath, setActivePath] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>(() => loadDrafts());
  const [explorerOn, setExplorerOn] = useState(true);
  const [minimapOn, setMinimapOn] = useState(true);
  const [wrap, setWrap] = useState(false);
  const [zen, setZen] = useState(false);
  const [palette, setPalette] = useState<'file' | 'cmd' | null>(null);
  const [goto, setGoto] = useState<string | null>(null);
  const [find, setFind] = useState<FindState>({ open: false, query: '', replace: '', cs: false });
  const [cursor, setCursor] = useState<Cursor>({ line: 1, col: 1 });
  const [selLen, setSelLen] = useState(0);
  const [note, setNote] = useState<string | null>(null);
  const [known, setKnown] = useState<string[]>([]);
  /* The file index is capped server-side; when it was, quick
     open must say so instead of silently missing files. */
  const [filesTruncated, setFilesTruncated] = useState(false);
  const [folded, setFolded] = useState<Set<number>>(new Set());
  const [outlineOn, setOutlineOn] = useState(false);
  const [problems, setProblems] = useState<CodeProblem[]>([]);
  /* A fresh array literal in the JSX (`problems.map(...)`) gave <Editor> a new
     prop identity on every parent render, which defeats its memo entirely - so
     the editor re-rendered per keystroke no matter what. Derive it once. */
  const problemLines = useMemo(
    () => problems.map((p) => ({ line: p.line - 1 })),
    [problems],
  );
  const [checkNote, setCheckNote] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
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
  const ta = useRef<HTMLTextAreaElement>(null);
  const scroller = useRef<HTMLDivElement>(null);

  const active = panes.findIndex((p) => p.path === activePath);
  const pane = active >= 0 ? panes[active] : null;
  const body = pane?.body ?? '';
  const lang = pane?.file.language ?? 'text';
  const dirty = !!pane && pane.body !== pane.base;
  const lines = useMemo(() => body.split('\n'), [body]);

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
  }, []);

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

  /* Wrap the native setter so React sees the change (a controlled textarea
     ignores a bare `el.value = x`). */
  const setTextarea = useCallback((el: HTMLTextAreaElement, v: string) => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value')?.set;
    setter?.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }, []);

  const syncCursor = useCallback(() => {
    const el = ta.current;
    if (!el) return;
    setCursor(posOf(el.value, el.selectionStart));
    setSelLen(el.selectionEnd - el.selectionStart);
  }, []);

  /* Undo history. `restore` puts a snapshot back into the textarea AND the
     caret with it, which is the whole point: native Ctrl+Z cannot see the
     programmatic edits this section makes, so it would step through a history
     that does not match what the user just did. */
  /* Restoring from history writes the textarea, which fires `input`, which
     lands back in `onType` - and `onType` records a snapshot. Without this
     guard, undoing pushed the pre-undo state onto the undo stack and cleared
     the redo stack, so redo could never work and repeated undo walked
     forwards. */
  const restoring = useRef(false);

  const applyText = useCallback((text: string, start: number, end: number) => {
    const el = ta.current;
    if (!el) return;
    restoring.current = true;
    setTextarea(el, text);
    requestAnimationFrame(() => {
      el.selectionStart = Math.min(start, text.length);
      el.selectionEnd = Math.min(end, text.length);
      syncCursor();
      el.focus();
      restoring.current = false;
    });
  }, [setTextarea, syncCursor]);

  /* `applyText` is defined below `useHistory` in source order but is needed by
     it, so the indirection goes through a ref rather than reordering hooks. */
  const applyTextRef = useRef<((t: string, s: number, e: number) => void) | null>(null);
  const history = useHistory(useCallback((s: Snapshot) => {
    applyTextRef.current?.(s.text, s.start, s.end);
  }, []));
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
    setFolded(new Set());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePath]);

  const replaceRange = useCallback((from: number, to: number, text: string,
                                     selStart = from + text.length) => {
    const el = ta.current;
    if (!el) return;
    const next = el.value.slice(0, from) + text + el.value.slice(to);
    /* Record the state BEFORE the change, with the selection that produced
       it, so undo returns to where the edit started. */
    history.commit(el.value, el.selectionStart, el.selectionEnd);
    lastBody.current = next;
    setTextarea(el, next);
    requestAnimationFrame(() => {
      el.selectionStart = selStart;
      el.selectionEnd = selStart;
      syncCursor();
      el.focus();
    });
  }, [history, setTextarea, syncCursor]);

  /* Typed input goes through the same door so Ctrl+Z undoes typing too. */
  const onType = useCallback((v: string) => {
    const el = ta.current;
    if (el && !restoring.current) {
      const before = lastBody.current;
      lastBody.current = v;
      const at = el.selectionStart;
      history.commit(before, at, at, true);
    } else {
      lastBody.current = v;
    }
    edit(v);
  }, [edit, history]);

  /* Derived structure, recomputed only when the buffer or language changes. */
  const outline = useMemo<Symbol[]>(
    () => (pane ? symbolsOf(pane.body, lang) : []),
    [pane, lang],
  );
  const allFolds = useMemo(
    () => (pane ? foldRanges(pane.body, lang) : []),
    [pane, lang],
  );

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
             problems list then rendered "✓ No syntax errors" —
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
         nothing is in flight, so the "checking…" pill must not
         spin forever. */
      setChecking(false);
    };
  }, [pane, lang]);

  /* The source is read-only from the agent, so it can change underneath a
     draft. Notice that, rather than silently showing text that no longer
     matches what the server would serve. */
  useEffect(() => {
    if (!pane || dirty) { setStale(null); return; }
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
  }, [pane, dirty]);

  const reveal = useCallback((needle: string, cs: boolean, backwards: boolean,
                            moveFocus = false) => {
    const el = ta.current;
    if (!el || !needle) return;
    const hay = cs ? el.value : el.value.toLowerCase();
    const n = cs ? needle : needle.toLowerCase();
    let at: number;
    if (backwards) {
      at = hay.lastIndexOf(n, Math.max(0, el.selectionStart - 1));
      if (at === -1) at = hay.lastIndexOf(n);
    } else {
      at = hay.indexOf(n, el.selectionEnd);
      if (at === -1) at = hay.indexOf(n);
    }
    if (at === -1) return;
    /* Do NOT steal focus here. `el.focus()` ran inside the Find box's Enter
       handler, so focus moved DURING the keydown and the NEXT Enter was
       consumed by the editor's own handler — whose Enter branch runs
       auto-indent. Repeating "find next" therefore silently inserted a
       newline each time: `import hmac` became `import \n\n\n\n\nhmac`,
       shredding the statement across six lines with no warning. Focus stays
       in the Find box so Enter keeps advancing matches; callers that really
       want the caret in the editor pass moveFocus. */
    if (moveFocus) el.focus();
    el.setSelectionRange(at, at + n.length);
    syncCursor();
    /* Scroll the match into view: measure against the line height the editor
       uses (leading-[1.55rem] on a 12.5px font). */
    const line = posOf(el.value, at).line;
    const lh = 24.8;
    const top = (line - 1) * lh;
    const box = scroller.current;
    if (box && (top < box.scrollTop || top > box.scrollTop + box.clientHeight - lh * 3)) {
      box.scrollTop = Math.max(0, top - box.clientHeight / 2);
    }
  }, [syncCursor]);

  /* Apply a pending jump-to-line once its file is the active pane. */
  useEffect(() => {
    if (!jump || !pane || pane.path !== jump.path) return;
    const el = ta.current;
    const off = offsetOf(pane.body, { line: jump.line, col: jump.col });
    setJump(null);
    requestAnimationFrame(() => {
      if (!el) return;
      el.focus();
      el.setSelectionRange(off, off);
      syncCursor();
      const box = scroller.current;
      const lh = 24.8;
      if (box) box.scrollTop = Math.max(0, (jump.line - 1) * lh - box.clientHeight / 2);
    });
  }, [jump, pane, syncCursor]);

  /* Undo/redo read the live textarea rather than React state, because the
     textarea is the only thing that always holds the current buffer. */
  const doUndo = useCallback(() => {
    const el = ta.current;
    if (!el) return;
    if (!history.undo({ text: el.value, start: el.selectionStart, end: el.selectionEnd })) {
      flash('nothing to undo');
    }
  }, [history, flash]);

  const doRedo = useCallback(() => {
    const el = ta.current;
    if (!el) return;
    if (!history.redo({ text: el.value, start: el.selectionStart, end: el.selectionEnd })) {
      flash('nothing to redo');
    }
  }, [history, flash]);

  const lineOps = useCallback((kind: 'indent' | 'outdent' | 'comment'
                                | 'moveUp' | 'moveDown' | 'deleteLine'
                                | 'dupLine' | 'selectNext' | 'copyUp' | 'copyDown') => {
    const el = ta.current;
    if (!el) return;
    const v = el.value;
    const startLn = posOf(v, el.selectionStart).line;
    const endLn = posOf(v, el.selectionEnd).line;
    const from = offsetOf(v, { line: startLn, col: 1 });
    const to = offsetOf(v, { line: endLn, col: lineSlice(v, endLn).length + 1 });
    const block = v.slice(from, to);
    const blockLines = block.split('\n');

    if (kind === 'indent' || kind === 'outdent') {
      const shifted = blockLines.map((l) =>
        kind === 'indent' ? '    ' + l : l.replace(/^[ \t]{1,4}/, ''));
      const joined = shifted.join('\n');
      replaceRange(from, to, joined, from);
      requestAnimationFrame(() => {
        el.selectionStart = from;
        el.selectionEnd = from + joined.length;
        syncCursor();
      });
      return;
    }
    if (kind === 'comment') {
      const mark = COMMENT[lang] ?? '// ';
      const allCommented = blockLines.every((l) => !l.trim() || l.trimStart().startsWith(mark.trim()));
      const shifted = blockLines.map((l) => {
        if (!l.trim()) return l;
        if (allCommented) return l.replace(new RegExp(`^(\\s*)${mark.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s?`), '$1');
        const ind = l.match(/^\s*/)?.[0] ?? '';
        return `${ind}${mark}${l.slice(ind.length)}`;
      });
      const joined = shifted.join('\n');
      replaceRange(from, to, joined, from);
      requestAnimationFrame(() => {
        el.selectionStart = from;
        el.selectionEnd = from + joined.length;
        syncCursor();
      });
      return;
    }
    if (kind === 'deleteLine') {
      const lineEnd = endLn < offsets(v).length
        ? offsetOf(v, { line: endLn + 1, col: 1 }) : v.length;
      const nextFrom = lineEnd < v.length ? lineEnd : from;
      replaceRange(from, Math.min(lineEnd, v.length), '', nextFrom);
      return;
    }
    if (kind === 'dupLine') {
      const ls = lineSlice(v, startLn);
      replaceRange(to, to, `\n${ls}`);
      return;
    }
    if (kind === 'copyUp' || kind === 'copyDown') {
      const ls = lineSlice(v, startLn);
      if (kind === 'copyUp') {
        if (startLn === 1) return;
        replaceRange(from, from, `${ls}\n`, from);
      } else {
        replaceRange(to, to, `\n${ls}`);
      }
      return;
    }
    if (kind === 'moveUp' || kind === 'moveDown') {
      if (kind === 'moveUp' && startLn === 1) return;
      if (kind === 'moveDown' && endLn === lines.length) return;
      const swap = kind === 'moveUp' ? startLn - 1 : endLn + 1;
      const other = lineSlice(v, swap);
      if (kind === 'moveUp') {
        replaceRange(from, to, `${blockLines[blockLines.length - 1] ? '' : ''}${other}\n${blockLines.join('\n')}`, from);
      } else {
        replaceRange(from, to, `${block}\n${other}`, from);
      }
      return;
    }
    if (kind === 'selectNext') {
      const word = v.slice(el.selectionStart, el.selectionEnd)
        || v.slice(from, offsetOf(v, { line: startLn, col: lineSlice(v, startLn).length + 1 }))
          .match(/[A-Za-z0-9_]+$/)?.[0]
        || '';
      if (!word) return;
      const at = v.indexOf(word, el.selectionEnd);
      if (at === -1) return;
      el.focus();
      el.setSelectionRange(at, at + word.length);
      syncCursor();
    }
  }, [lang, lines.length, replaceRange, syncCursor]);

  /* ── commands ─────────────────────────────────────────────────────────── */
  const CMDS: { id: string; label: string; run: () => void }[] = useMemo(() => [
    { id: 'file.quickOpen', label: 'Go to File…', run: () => setPalette('file') },
    { id: 'file.save', label: 'Save (browser draft only)', run: () => flash('nothing is written to disk — this is a browser draft') },
    { id: 'file.revert', label: 'Revert File to Disk Contents', run: () => {
      if (!pane) return;
      if (pane.body === pane.base) { flash('no local changes to revert'); return; }
      edit(pane.base);
      flash(`reverted ${pane.path} to what the agent serves`);
    } },
    { id: 'view.find', label: 'Find in File', run: () => setFind((f) => ({ ...f, open: true })) },
    { id: 'view.goto', label: 'Go to Line/Column…', run: () => setGoto('') },
    { id: 'view.explorer', label: 'Toggle Explorer', run: () => setExplorerOn((v) => !v) },
    { id: 'view.minimap', label: 'Toggle Minimap', run: () => setMinimapOn((v) => !v) },
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
    { id: 'edit.trim', label: 'Trim Trailing Whitespace (draft)', run: () => {
      const el = ta.current;
      if (!el) return;
      const cleaned = el.value.split('\n').map((l) => l.replace(/[ \t]+$/, '')).join('\n');
      if (cleaned === el.value) { flash('no trailing whitespace'); return; }
      replaceRange(0, el.value.length, cleaned, el.selectionStart);
      flash('trimmed trailing whitespace in the draft only');
    } },
    { id: 'edit.undo', label: 'Undo', run: () => doUndo() },
    { id: 'edit.redo', label: 'Redo', run: () => doRedo() },
    { id: 'view.foldAll', label: 'Fold All', run: () => setFolded(new Set(allFolds.map((f) => f.start))) },
    { id: 'view.unfoldAll', label: 'Unfold All', run: () => setFolded(new Set()) },
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
  ], [allFolds, doRedo, doUndo, edit, flash, lineOps, pane, replaceRange]);

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
        if (inField && ta.current && e.target === ta.current) {
          e.preventDefault();
          ta.current.blur();
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
      if ((search.open || find.open) && e.target !== ta.current) return;
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
      if (mod && e.key.toLowerCase() === 'z' && !e.shiftKey) {
        e.preventDefault(); doUndo(); return;
      }
      if ((mod && e.key.toLowerCase() === 'y')
          || (mod && e.shiftKey && e.key.toLowerCase() === 'z')) {
        e.preventDefault(); doRedo(); return;
      }
      if (mod && e.shiftKey && (e.key === '[' || e.key === ']')) {
        e.preventDefault();
        setFolded((s) => {
          const n = new Set(s);
          const here = cursor.line - 1;
          const f = allFolds.find((x) => x.start === here);
          if (!f) return n;
          /* Fold the region containing the caret, or the innermost open one. */
          const target = e.key === ']'
            ? f
            : [...allFolds].reverse().find((x) => x.start <= here && x.end >= here) ?? f;
          if (n.has(target.start)) n.delete(target.start);
          else n.add(target.start);
          return n;
        });
        return;
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
      if (!inField || e.target === ta.current) {
        if (mod && e.key === '/') { e.preventDefault(); lineOps('comment'); return; }
        if (mod && e.key.toLowerCase() === 'd') { e.preventDefault(); lineOps('selectNext'); return; }
        if (e.shiftKey && e.altKey && e.key === 'ArrowUp') { e.preventDefault(); lineOps('copyUp'); return; }
        if (e.shiftKey && e.altKey && e.key === 'ArrowDown') { e.preventDefault(); lineOps('copyDown'); return; }
        /* Move the caret to the line above/below, keeping the
           column. The editor has no real multi-cursor support,
           so these keys do exactly this — they no longer also
           push a marker into a cursor count that nothing
           rendered. */
        if (e.altKey && e.key === 'ArrowUp' && ta.current) {
          e.preventDefault();
          const el = ta.current;
          const ln = posOf(el.value, el.selectionStart).line;
          if (ln > 1) {
            const col = el.selectionStart - offsetOf(el.value, { line: ln, col: 1 }) + 1;
            const off = offsetOf(el.value, { line: ln - 1, col });
            el.focus();
            el.setSelectionRange(off, off);
          }
          syncCursor();
          return;
        }
        if (e.altKey && e.key === 'ArrowDown' && ta.current) {
          e.preventDefault();
          const el = ta.current;
          const ln = posOf(el.value, el.selectionStart).line;
          if (ln < lines.length) {
            const col = el.selectionStart - offsetOf(el.value, { line: ln, col: 1 }) + 1;
            const off = offsetOf(el.value, { line: ln + 1, col });
            el.focus();
            el.setSelectionRange(off, off);
          }
          syncCursor();
          return;
        }
        if (mod && e.shiftKey && e.key.toLowerCase() === 'k') { e.preventDefault(); lineOps('deleteLine'); return; }
      }
      /* Tab inserts an indent, but ONLY the unmodified one. Both Tab and Shift+Tab
         used to be swallowed and turned into four spaces, so keyboard focus could
         never leave the textarea at all - a hard keyboard trap (WCAG 2.1.2 No
         Keyboard Trap). Shift+Tab now falls through to the browser, which is the
         standard way out; Escape is a second escape hatch below. */
      if (e.key === 'Tab' && !e.shiftKey && !mod && e.target === ta.current) {
        e.preventDefault();
        const el = ta.current!;
        replaceRange(el.selectionStart, el.selectionEnd, '    ');
        return;
      }
      if (e.key === 'Enter' && e.target === ta.current) {
        /* Keep the previous line's indentation, and open a block if the line
           ends in a colon — the two cases that matter when editing Python or
           YAML by hand. */
        const el = ta.current!;
        const line = lineSlice(el.value, posOf(el.value, el.selectionStart).line);
        const ind = line.match(/^\s*/)?.[0] ?? '';
        const opens = /[:{]\s*$/.test(line.trimEnd());
        const next = posOf(el.value, el.selectionEnd);
        const brace = BRACKETS[lang];
        const before = el.value[el.selectionStart - 1];
        const after = el.value[el.selectionStart];
        if (brace && before === brace[0] && after === brace[1]) {
          e.preventDefault();
          const at = el.selectionStart;
          replaceRange(at, at + 1, `\n${ind}    \n${ind}`, at + ind.length + 5);
          return;
        }
        e.preventDefault();
        const at = next.line === next.col ? el.selectionStart : el.selectionStart;
        replaceRange(at, at, `\n${ind}${opens ? '    ' : ''}`);
        return;
      }
      if (['(', '[', '{', '"', "'", '`'].includes(e.key) && e.target === ta.current) {
        const el = ta.current!;
        const pairs: Record<string, string> = { '(': ')', '[': ']', '{': '}', '"': '"', "'": "'", '`': '`' };
        const close = pairs[e.key];
        if (el.selectionStart !== el.selectionEnd) {
          e.preventDefault();
          const a = el.selectionStart;
          const b = el.selectionEnd;
          replaceRange(a, b, e.key + el.value.slice(a, b) + close, a + 1);
          return;
        }
        /* Only auto-close when it is unambiguous: not before a word character,
           not when the very next character already is the closing one. */
        const nextCh = el.value[el.selectionStart] ?? '';
        if (!/[A-Za-z0-9_]/.test(nextCh)) {
          e.preventDefault();
          /* This handler runs BEFORE the browser inserts the character, so the
             buffer still ends at `selectionStart`. The pair is inserted there
             and the caret goes one past the opener, i.e. between the two. */
          replaceRange(el.selectionStart, el.selectionStart,
            e.key + close, el.selectionStart + 1);
        }
        return;
      }
      if (e.key === 'Backspace' && e.target === ta.current) {
        const el = ta.current!;
        if (el.selectionStart !== el.selectionEnd) return;
        /* Opener -> closer. This used to be keyed by the closing delimiter
           while being looked up with the character BEFORE the caret, which is
           the opening one - so the lookup always missed and Backspace deleted
           only half of an auto-inserted pair, leaving a stray `)`. */
        const closer: Record<string, string> = { '(': ')', '[': ']', '{': '}', '"': '"', "'": "'", '`': '`' };
        const before = el.value[el.selectionStart - 1] ?? '';
        const after = el.value[el.selectionStart] ?? '';
        if (closer[before] && closer[before] === after) {
          e.preventDefault();
          replaceRange(el.selectionStart - 1, el.selectionStart + 1, '');
        }
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [active, allFolds, body, close, cursor.line, doRedo, doUndo,
      find.open, flash, goto, lang, lineOps, offsetOf, outline, palette, panes.length,
      replaceRange, search.open, shortcuts, syncCursor]);

  /* Scroll the active line into view when the cursor moves by keyboard. */
  useEffect(() => {
    const box = scroller.current;
    if (!box) return;
    const lh = 24.8;
    const top = (cursor.line - 1) * lh;
    if (top < box.scrollTop || top > box.scrollTop + box.clientHeight - lh * 3) {
      box.scrollTop = Math.max(0, top - box.clientHeight / 2);
    }
  }, [cursor.line]);

  const gotoLine = (raw: string) => {
    const m = raw.match(/^(\d+)(?::(\d+))?$/);
    if (!m) return;
    const line = Math.max(1, Math.min(lines.length, Number(m[1])));
    const col = Math.max(1, Number(m[2] ?? 1));
    const off = offsetOf(body, { line, col });
    replaceRange(off, off, '');
    setGoto(null);
  };

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        {!zen && (
          <TopBar crumb="Code">
            {dirty && <Pill tone="info">browser draft</Pill>}
            <Pill tone="warn">read-only source</Pill>
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
              className="flex max-h-[34vh] w-full flex-none flex-col border-b border-glass-border bg-surface-0 lg:max-h-none lg:w-[268px] lg:border-b-0 lg:border-r">
              <Explorer onOpen={open} activePath={pane?.path ?? null} />
              <div className="flex-none border-t border-glass-border px-3 py-1.5 text-[10px] text-zinc-muted">
                read-only · {panes.length} open
              </div>
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
                      className={`group flex flex-none cursor-pointer items-center gap-1.5 border-r border-glass-border px-2.5 py-1.5 text-[11.5px] ${
                        i === active ? 'bg-surface-1 text-zinc-100' : 'text-zinc-400 hover:bg-glass-hover'}`}>
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
                className="flex flex-none items-center gap-1 overflow-x-auto border-b border-glass-border px-3 py-1 text-[11px] text-zinc-muted">
                {pane.path.split('/').map((seg, i, arr) => (
                  <span key={i} className="flex items-center gap-1 whitespace-nowrap">
                    {i > 0 && <ChevronRight size={10} className="text-zinc-muted/60" />}
                    <span className={i === arr.length - 1 ? 'text-zinc-300' : ''}>{seg}</span>
                  </span>
                ))}
                <span className="ml-auto flex items-center gap-1">
                  <span className="whitespace-nowrap">{LANG_LABEL[lang] ?? lang}</span>
                </span>
              </nav>
            )}

            <FindWidget state={find} setState={setFind} body={body} taRef={ta}
              onReveal={reveal} />

            {/* Editor */}
            {!pane ? (
              <div className="flex min-h-0 flex-1 items-center justify-center">
                <Empty icon={<Code2 size={22} />}
                  title="Pick a file to start editing"
                  sub="Ctrl+P opens any file in the two service trees. Edits stay in this browser; nothing is written to disk." />
              </div>
            ) : (
              <div className="flex min-h-0 flex-1">
                {outlineOn && (
                  <aside aria-label="Symbol outline"
                    className="hidden max-h-[30vh] w-[210px] flex-none flex-col overflow-y-auto border-r border-glass-border bg-surface-0 lg:block">
                    <div className="flex flex-none items-center gap-1.5 px-3 py-2 text-[10.5px] font-bold uppercase tracking-[0.14em] text-zinc-muted">
                      <ListTree size={12} /> Outline
                    </div>
                    {outline.length ? outline.map((s, i) => (
                      <button key={`${s.line}-${s.name}-${i}`} type="button"
                        onClick={() => replaceRange(
                          offsetOf(body, { line: s.line, col: 1 }),
                          offsetOf(body, { line: s.line, col: 1 }), '')}
                        className={`flex w-full items-baseline gap-1.5 px-3 py-0.5 text-left text-[11.5px] hover:bg-glass-hover ${
                          cursor.line === s.line + 1 ? 'bg-accent/10 text-zinc-100' : 'text-zinc-400'}`}>
                        <span className={s.kind === 'class' ? 'text-amber-200' : 'text-emerald-300'}>
                          {s.kind === 'class' ? 'C' : s.kind === 'section' ? '§' : 'ƒ'}
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
                <Editor
                  body={body}
                  lang={lang}
                  path={pane.path}
                  folded={folded}
                  cursorLine={cursor.line - 1}
                  caretOffset={ta.current?.selectionStart ?? -1}
                  selectionStart={ta.current?.selectionStart ?? 0}
                  selectionEnd={ta.current?.selectionEnd ?? 0}
                  problems={problemLines}
                  ariaLabel={`Editor for ${pane.path}`}
                  wrap={wrap}
                  onChange={onType}
                  onCursor={syncCursor}
                  onFoldToggle={(line) => setFolded((s) => {
                    const n = new Set(s);
                    if (n.has(line)) n.delete(line); else n.add(line);
                    return n;
                  })}
                  registerRef={(el) => { ta.current = el; }}
                  scrollerRef={scroller}
                />
                {minimapOn && (
                  <Minimap lines={lines} cursorLine={cursor.line}
                    height={scroller.current?.clientHeight ?? 400} />
                )}
              </div>
            )}

            {/* Problems — real diagnostics, not an apology. */}
            <div className="max-h-[26vh] flex-none overflow-y-auto border-t border-glass-border bg-surface-0">
              <div className="sticky top-0 flex items-center gap-2 border-b border-glass-border bg-surface-0 px-3 py-1.5">
                <Terminal size={12} className="text-zinc-muted" />
                <span className="text-[11px] font-bold uppercase tracking-[0.14em] text-zinc-muted">
                  Problems
                </span>
                {checking
                  ? <Pill tone="muted">checking…</Pill>
                  : <Pill tone={problems.length ? 'err' : 'ok'}>
                      {problems.length} problem{problems.length === 1 ? '' : 's'}
                    </Pill>}
                {checkNote && <Pill tone="muted">{checkNote}</Pill>}
              </div>
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
                <ul className="space-y-1.5 px-3 py-2.5 text-[11.5px] text-zinc-400">
                  <li className="flex items-start gap-2">
                    {problems.length === 0 && !checkNote && (
                      <>
                        <span className="mt-[3px] flex-none text-emerald-400">✓</span>
                        <span>No syntax errors{lang === 'python'
                          ? ' — the draft was parsed with Python’s own ast module.'
                          : ' in the delimiters of this draft.'}</span>
                      </>
                    )}
                  </li>
                  <li className="flex items-start gap-2">
                    <TriangleAlert size={12} className="mt-0.5 flex-none text-amber-400/80" />
                    <span>Type errors, completion and go-to-definition still need a
                      language server, which this section does not run. These are
                      syntax checks only.</span>
                  </li>
                  <li className="flex items-start gap-2">
                    <Play size={12} className="mt-0.5 flex-none text-zinc-muted" />
                    <span>Run and agent actions stay disabled until execution is
                      sandboxed and approval-gated.</span>
                  </li>
                </ul>
              )}
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

        {/* Status bar */}
        <div className="flex flex-none flex-wrap items-center gap-x-3 gap-y-0.5 border-t border-glass-border bg-surface-0 px-3 py-1 text-[10.5px] text-zinc-muted">
          <span className="flex items-center gap-1">
            <Columns2 size={10} /> {pane ? LANG_LABEL[lang] ?? lang : 'no file'}
          </span>
          <span>Ln {cursor.line}, Col {cursor.col}</span>
          {selLen > 0 && <span>({selLen} selected)</span>}
          {folded.size > 0 && <span>{folded.size} folded</span>}
          {problems.length > 0 && (
            <span className="text-rose-400">{problems.length} error{problems.length === 1 ? '' : 's'}</span>
          )}
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
          <span>UTF-8</span>
          <span>Spaces: 4</span>
          <span>{body.length} chars</span>
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
          <span className="ml-auto flex items-center gap-1.5" role="status">
            {note && <span className="text-accent/90">{note}</span>}
            {!note && <span>draft stays in this browser — nothing is written to disk</span>}
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
