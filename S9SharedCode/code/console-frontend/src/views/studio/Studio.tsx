import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ArrowLeft, ArrowUpRight, ChevronRight, Code2, Eye, FileDiff, FileText, FolderOpen, FolderPlus, FolderTree, GitBranch, Globe, PanelLeftOpen, PanelRightClose, PanelRightOpen, Play, Plus, RefreshCw, ScrollText, Search, ShieldAlert, Terminal, TriangleAlert, X } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import MonacoEditor, { type EditorHandle } from '../../components/MonacoEditor';
import { renderMarkdown } from '../../components/markdown';
import { api } from '../../api';
import type { StudioChange, StudioCheckpoint, StudioHunk, StudioSession } from '../../api';
import { groupChanges, pickRedo, pickUndo, restoreLabel, restoreParts } from './timeline';
import { Splitter, useSplit, useSplitPx } from '../../lib/useSplit';
import { baseName as BASENAME, isUnderRoot, langFor, newStudioSid, sessionName, setStoredStudioSid, studioSid, useCodeIdeFlag, useStudioRuns, useStudioSessions, useStudioWorkspace } from '../../lib/studioSession';
import { Explorer } from './Explorer';
import { FolderPicker } from './FolderPicker';
import { AgentPanel } from './AgentPanel';
import { Sessions } from './Sessions';
import ReviewEditor, { type ReviewHandle } from './ReviewEditor';
import { Dock } from './Dock';
import { SearchReplace } from './SearchReplace';
import { GitPanel } from './GitPanel';
import { BrowserPane } from './BrowserPane';
import { RunsPanel } from './RunsPanel';
import { Label } from './chrome';
import { Empty, Pill, Skel } from '../../components/primitives';

/* The name each section panel wears in its header — the activity bar's
   tooltips and this label must not disagree about what a section is called. */
type PanelId = 'explorer' | 'search' | 'changes' | 'git' | 'browser' | 'runs'
  | 'terminal' | 'context';

const DRAWER_LABEL: Record<PanelId, string> = {
  explorer: 'Explorer', search: 'Search', changes: 'Changes', git: 'Git',
  browser: 'Browser', runs: 'Runs', terminal: 'Terminal', context: 'Context',
};

/* The drawer wears the same identity as the activity bar that opens it:
   one icon, one tile, the console's own card-head language. */
/* The console's own section symbols (the `RAIL` in components/ui.tsx) wherever
   a studio panel is the same idea — Runs is the console's Play, Session is its
   Chat bubble — so the studio reads as one product rather than a second icon
   language. The panels the console has no section for keep their own lucide
   glyph, drawn at the same size and stroke. */
const DRAWER_ICON: Record<PanelId, ReactNode> = {
  explorer: <FolderTree size={16} />,
  search: <Search size={16} />,
  changes: <FileDiff size={16} />,
  git: <GitBranch size={16} />,
  browser: <Globe size={16} />,
  runs: <Play size={16} />,
  terminal: <Terminal size={16} />,
  context: <ScrollText size={16} />,
};

/* The studio's left column (sessions) and workbench (editor + dock) are
   unmounted for now: their features are being re-homed, and the code stays here
   so the move is a re-mount rather than a rewrite. Flip one to true to bring
   that piece back exactly as it was. The section panels are NOT behind a flag:
   they live in the right-hand slide-over, which is always available. */
const SHOW_SESSIONS = false;
const SHOW_WORKBENCH = true;

const CodePage = lazy(() => import('../Code'));

/* Aria Code Studio — a coding workspace with its own chrome.

   WHY THIS IS A ROUTE AND NOT A MODE OF /code: a "focus mode" that re-skins the
   same tab keeps the sidebar it was supposed to drop, keeps the state of the
   page it replaced, and leaks a second conversation into what looked like the
   first. `/authoring/focus` already established the pattern this follows: it is
   a real path, it resolves to the same keep-alive entry as its parent section,
   and the page itself decides what chrome is absent. Here the answer is
   "all of it": `Rail` is never rendered inside Studio, so nothing on screen
   belongs to the rest of Aria while you are coding.

   WHY THE OLD PAGE SURVIVES: with `ui.code_ide` off this route renders `Code`
   instead, and `/code` renders what it always rendered. The two differ in
   WHERE A FILE IS WRITTEN — a studio session owns its folders, the Code page
   shares one process-wide workspace — which is not a difference to discover by
   accident, so the switch is one flag, off by default, and the redirect is the
   only change when it is on. */

interface Buffer {
  path: string;
  name: string;
  text: string;
  lang: string;
  /** Exact mtime from the last read or write; a save without it cannot detect
   *  drift, so it is carried rather than re-fetched. */
  mtime?: number;
  base: string;
  error: string;
  /** The read FAILED — there is no file, no permission, nothing to show.
   *  Distinct from `error`, which also carries "truncated for editing" on a
   *  buffer that loaded fine. Without the distinction the studio opened a
   *  missing file as an empty editable document with a source/review switch
   *  above it: a banner said "cannot stat" while the pane invited you to type
   *  into a file that does not exist. */
  failed?: boolean;
  loading: boolean;
  /** Opened by a single click and reusable by the next one; pinning (double
   *  click, or typing in it) makes it permanent, the way every editor does. */
  preview: boolean;
  /** Which of the two views this buffer is showing. `review` is the server's
   *  baseline beside the current text; `source` is the ordinary editor. They
   *  are the same file — the mode only decides what you are looking AT. */
  mode: 'source' | 'review';
  /** Pending hunks for this path, from the review route. */
  hunks: StudioHunk[];
  /** The server's baseline text: what a Revert of this file returns to. Not
   *  `base` (the copy the editor was opened with) because after a keep the
   *  two differ, and diffing against the wrong one re-offers decided hunks. */
  reviewBase: string;
  /** Why a decision was refused, in the server's words. */
  conflict: string;
}

function emptyBuffer(path: string): Buffer {
  return { path, name: BASENAME(path), text: '', lang: langFor(path), base: '',
           error: '', loading: true, preview: false, mode: 'source',
           hunks: [], reviewBase: '', conflict: '' };
}

function StudioShell({ sid, onSwitch }: { sid: string; onSwitch: (s: string) => void }) {
  /* Every other route titles itself through `TopBar`, and the studio
     deliberately renders no TopBar — so the browser tab kept showing whatever
     the last visible page had set it to ("Aria · Research"), which makes the
     tab, the history entry and a screen reader all lie about where you are.
     Claimed here and released to nothing on unmount: leaving the studio
     navigates to a view that sets its own title. */
  useEffect(() => {
    document.title = 'Aria · Studio';
  }, [sid]);
  const ws = useStudioWorkspace(sid);
  const sessions = useStudioSessions(sid);
  const runs = useStudioRuns(sid);
  const [buffers, setBuffers] = useState<Record<string, Buffer>>({});
  const [tabs, setTabs] = useState<string[]>([]);
  const [active, setActive] = useState('');
  /* Which drawer the workbench shows beside the editor: the file tree, the
     workspace search, or the unreviewed changes. "" hides the drawer and leaves
     the editor the whole width, which is the state you want while actually
     reading a file. The default is "" — the session column lives there instead
     (context, project, runs), and nothing is open until the user asks for a
     section. */
  const [drawer, setDrawer] = useState<PanelId | ''>('');
  /* The right-hand slide-over. Explorer is the default tab; the "+" opens a
     second one, and each tab can be closed on its own. */
  const [openPanels, setOpenPanels] = useState<{ key: string; id: PanelId }[]>([{ key: 'explorer-0', id: 'explorer' }]);
  const [drawerKey, setDrawerKey] = useState('explorer-0');
  const panelSeq = useRef(0);
  const sessionStripRef = useRef<HTMLDivElement>(null);
  /* Tabs the user closed. Closing hides the tab; it does NOT archive or delete
     the session — it stays reachable from its project's session list. */
  const [closedTabs, setClosedTabs] = useState<Set<string>>(() => {
    try { return new Set(JSON.parse(localStorage.getItem('aria.studio.closed_tabs') || '[]')); }
    catch { return new Set(); }
  });
  useEffect(() => {
    try { localStorage.setItem('aria.studio.closed_tabs', JSON.stringify([...closedTabs])); }
    catch { /* private mode */ }
  }, [closedTabs]);
  /* The session you are looking at always has a tab. */
  useEffect(() => {
    setClosedTabs((prev) => {
      if (!prev.has(sid)) return prev;
      const n = new Set(prev);
      n.delete(sid);
      return n;
    });
  }, [sid]);
  /* Reported by the conversation so the top bar's ring can show it, and kept
     per session: the ring used to sit at 0% after a reload or a session switch
     because only a LIVE run reports context, so the last real measurement was
     thrown away. */
  const [ctx, setCtx] = useState({ chars: 0, cap: 0 });
  const ctxKey = `aria.studio.ctx.${sid}`;
  useEffect(() => {
    try {
      const raw = localStorage.getItem(ctxKey);
      if (raw) {
        const got = JSON.parse(raw) as { chars?: number; cap?: number };
        setCtx({ chars: Number(got.chars) || 0, cap: Number(got.cap) || 0 });
      } else {
        setCtx({ chars: 0, cap: 0 });
      }
    } catch {
      setCtx({ chars: 0, cap: 0 });
    }
  }, [ctxKey]);
  const onContext = useCallback((chars: number, cap: number) => {
    /* A mount or an idle reconnect reports (0, 0) — "nothing measured", not
       "the window is empty". Storing that would wipe the last real value the
       ring was showing, which is exactly the "isn't getting updated" bug. */
    if (!chars && !cap) return;
    setCtx((c) => (c.chars === chars && c.cap === cap ? c : { chars, cap }));
    try {
      localStorage.setItem(ctxKey, JSON.stringify({ chars, cap }));
    } catch { /* private mode: the ring just stays session-local */ }
  }, [ctxKey]);
  /* The right-hand slide-over. Chrome-style tabs inside it; Explorer is the
     default because "where are my files" is the first question a coding
     session asks. */
  const [panelOpen, setPanelOpen] = useState(false);
  /* The panel list is a menu, not a second row of tabs: the strip shows the
     one panel you are in, and this is how you leave it. */
  const [panelMenu, setPanelMenu] = useState(false);
  const [ctxMenu, setCtxMenu] = useState(false);
  /* The file open in the Explorer's second pane. Clicking a row previews it
     here instead of opening the (hidden) workbench. */
  const [previewPath, setPreviewPath] = useState('');
  const [previewText, setPreviewText] = useState('');
  const [previewMd, setPreviewMd] = useState(false);
  /* A file switch must not inherit the previous file's render mode: opening an
     .html after a rendered .md showed the HTML source as prose. */
  useEffect(() => { setPreviewMd(false); }, [previewPath]);
  /** Open a panel as a tab: add it if it is not open, then focus it. Declared
   *  here because the composer's own verbs (changes, files) open panels. */
  const openPanel = useCallback((id: PanelId) => {
    const key = `${id}-${++panelSeq.current}`;
    setOpenPanels((list) => [...list, { key, id }]);
    setDrawerKey(key);
    setDrawer(id);
    setPanelOpen(true);
    setPanelMenu(false);
  }, []);
  /* The dock keeps its own open/tab/height state rather than borrowing the
     drawer's: collapsing a terminal must not lose its scrollback, and the
     height is a person's choice that outlives any one pane. */
  const [dockTab, setDockTab] = useState<'terminal' | 'problems' | 'output' | 'checks'>('terminal');
  /* Whether this session holds a Chromium. In the status bar, not hidden in
     the pane: a browser is a process with a network stack and a cookie jar,
     and "is something of mine still running" should be answerable at a
     glance. */
  const [, setBrowserLive] = useState(false);
  const [dockOpen, setDockOpen] = useState(false);
  /* Dock on the right (OpenCode-style) or along the bottom. A person reading a
     build log beside a file wants the vertical space; a person watching a
     terminal wants the width. One toggle, remembered per window. */
  const [dockSide, setDockSide] = useState(() => {
    try { return localStorage.getItem('aria.studio.dock_side') === '1'; }
    catch { return false; }
  });
  const toggleDockSide = useCallback(() => {
    setDockSide((v) => {
      const next = !v;
      try { localStorage.setItem('aria.studio.dock_side', next ? '1' : '0'); } catch { /* private mode */ }
      return next;
    });
  }, []);
  /* Pixels, not a percentage, and remembered: a dock is sized to the log you
     are reading, and that choice belongs to the window, not the project. */
  const [dockHeight, setDockHeightRaw] = useState(() => {
    const n = Number(localStorage.getItem('aria.studio.dock_h') || 220);
    return Number.isFinite(n) ? Math.max(110, Math.min(900, n)) : 220;
  });
  const setDockHeight = useCallback((n: number) => {
    setDockHeightRaw(n);
    try { localStorage.setItem('aria.studio.dock_h', String(n)); } catch { /* private mode */ }
  }, []);
  /* The workbench — tabs, editor, diff — is a panel you bring out, not the
     surface the studio opens on. The reference product's answer to "where do
     my files live" is: beside the conversation, when there is something to
     look at. Nothing is lost by hiding it; the buffers stay loaded, so
     reopening is instant rather than a refetch. */
  const [showWorkbench, setShowWorkbench] = useState(false);
  /* OpenCode's left edge is a narrow icon rail, not a 248px session list: the
     session TABS at the top are the switcher. The full Sessions panel is still
     one click away (the rail's top icon). */
  const [railCollapsed, setRailCollapsed] = useState(true);
  const [status, setStatus] = useState('');
  const [adding, setAdding] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [folderPath, setFolderPath] = useState('');
  const [, setCursor] = useState({ line: 1, col: 1 });
  const [confirmDiscard, setConfirmDiscard] = useState('');
  /* The session's unreviewed changes by path: ONE fetch, shared by the
     Changes panel and by the decision of whether opening a file should land in
     review. Two components asking the same question separately is how a
     sidebar and an editor disagree about whether work is pending. */
  const [pending, setPending] = useState<Record<string, StudioChange>>({});
  const [busyHunk, setBusyHunk] = useState('');
  const [checkpoints, setCheckpoints] = useState<StudioCheckpoint[]>([]);
  /* Checkpoints are never deleted, so the walk needs a cursor. The server owns
     it (`acted`) because it has to survive a reload, a new session in the same
     folder, and a client that was closed between two undos. */
  const [acted, setActed] = useState<string[]>([]);
  const edRef = useRef<EditorHandle | null>(null);
  const reviewRef = useRef<ReviewHandle | null>(null);

  const loadPending = useCallback(async () => {
    try {
      const got = await api.studioChanges(sid);
      const map: Record<string, StudioChange> = {};
      for (const c of got?.changes || []) map[c.path] = c;
      setPending(map);
    } catch {
      /* A failed refresh leaves the previous list on screen rather than
         silently emptying it — an empty Changes panel reads as "the agent
         changed nothing", which is a lie about someone else's work. */
    }
  }, [sid]);

  const loadCheckpoints = useCallback(async () => {
    try {
      const got = await api.studioCheckpoints(sid);
      setCheckpoints(got?.checkpoints || []);
      setActed(got?.acted || []);
    } catch { /* same reasoning as loadPending */ }
  }, [sid]);

  useEffect(() => { void loadPending(); void loadCheckpoints(); },
              [loadPending, loadCheckpoints]);

  /* Load the Explorer's preview file. One read per path, cancelled on switch. */
  useEffect(() => {
    if (!previewPath) { setPreviewText(''); return; }
    let live = true;
    void api.codeFile(previewPath, undefined, sid)
      .then((f) => {
        if (!live) return;
        const rec = f as { text?: string; error?: string } | null;
        setPreviewText(rec?.error ? `// ${rec.error}` : (rec?.text || ''));
      })
      .catch(() => { if (live) setPreviewText('// could not read this file'); });
    return () => { live = false; };
  }, [previewPath, sid]);

  /* The workbench's share of the window. It starts near half because the
     conversation and the code are read side by side; the conversation keeps
     the other half, which is what its composer needs to stay readable. */
  const right = useSplit({ storageKey: 'aria.studio.right', default: 48, min: 25, max: 70,
                           liveQuery: '(min-width: 1100px)' });
  /* The slide-over's width, in pixels, persisted per window. It is a flex
     child now (not an overlay), so this is what the conversation yields. */
  const drawerPx = useSplitPx({ storageKey: 'aria.studio.drawer', initial: 440,
                                min: 320, max: 1400 });
  /* The Explorer's own split: file tree ↔ preview, as a share of the panel. */
  const explorerSplit = useSplit({ storageKey: 'aria.studio.explorer_split',
                                   default: 46, min: 25, max: 70 });

  const roots = ws.ws?.roots || [];

  /* The model choice is global (a session records which model answered, it
     does not configure one) and lives in Studio rather than in AgentPanel so
     the session column can show the same truth. */
  const MODEL_KEY = 'aria.studio.model';
  const [model, setModel] = useState<string>(() => {
    try { return localStorage.getItem(MODEL_KEY) || ''; } catch { return ''; }
  });
  const chooseModel = useCallback((v: string) => {
    setModel(v);
    try { localStorage.setItem(MODEL_KEY, v); } catch { /* private mode */ }
  }, []);

  /* Lifted out of AgentPanel: the session column shows the same branch the
     composer chip does, so one fetch, one truth, no second `git status`
     on every mount. */
  const [branch, setBranch] = useState<string | null>(null);
  const refreshBranch = useCallback(async () => {
    if (!roots.length) { setBranch(null); return; }
    try {
      const r = await api.studioGit(sid, 'status', {});
      if (r?.ok === false || !r?.repo) { setBranch('no repository here'); return; }
      const out = String(r.output || '');
      const m = /On branch ([^\s]+)/.exec(out) || /^##\s+([^\s.]+)/m.exec(out);
      setBranch(m ? m[1] : 'detached HEAD');
    } catch { setBranch(null); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid, roots.length]);
  useEffect(() => { void refreshBranch(); }, [refreshBranch]);

  const openPath = useCallback(async (path: string, pin = true, review?: boolean) => {
    if (!path) return;
    if (!isUnderRoot(path, roots)) {
      setStatus(`"${path}" is not inside a folder this session holds`);
      return;
    }
    setActive(path);
    setShowWorkbench(true);
    setBuffers((b) => {
      const have = b[path];
      if (have && !have.error) {
        // A preview tab is replaced by the next single-clicked file; a pinned
        // one stays. Typing into a preview pins it, same as every editor.
        return pin && have.preview ? { ...b, [path]: { ...have, preview: false } } : b;
      }
      return { ...b, [path]: { ...emptyBuffer(path), preview: !pin } };
    });
    setTabs((t) => (t.includes(path) ? t : [...t, path]));
    if (buffers[path] && !buffers[path].error) return;
    try {
      /* `codeFile` is deliberately NOT wrapped in `safe()`, so an error
         response arrives as a body with a reason rather than as null — the
         type does not carry that field, and the reason is the useful part. */
      const f = await api.codeFile(path, undefined, sid) as
        (Awaited<ReturnType<typeof api.codeFile>> & { error?: string }) | null;
      if (!f || f.error) throw new Error(f?.error || 'could not be opened');
      /* A file the agent has left unreviewed opens IN REVIEW: the whole point
         of the surface is that the change is what you are shown, not something
         you have to go looking for in a sidebar. */
      const reviewMode = review ?? Boolean(pendingOf(path)?.hunks.length);
      setBuffers((b) => ({ ...b, [path]: {
        ...b[path], text: f.text || '', base: f.text || '',
        /* The extension decides, always — including when the server answered
           with a `language` of its own. The two used to be reconciled with a
           ternary that looked up the same extension on both sides, which read
           like a rule and was a no-op. */
        lang: langFor(path),
        mtime: f.mtime, loading: false, failed: false,
        mode: reviewMode ? 'review' : 'source',
        error: f.truncated ? 'file truncated for editing' : '',
      } }));
      setStatus('');
      if (reviewMode) void refreshReview(path);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setBuffers((b) => ({ ...b, [path]: {
        ...b[path], loading: false, failed: true, error: msg,
        // Nothing was read, so there is nothing to edit and nothing to
        // compare: an empty `text` here is not a blank file, it is absence.
        text: '', base: '', hunks: [], reviewBase: '', mode: 'source',
      } }));
      setStatus(msg);
    }
    // `pending` is a dependency on purpose: whether a file opens in review is
    // a question about the CURRENT unreviewed set, and a stale copy of it
    // silently decides for the user with last refresh's facts.
  }, [roots, sid, buffers, pending]);

  /** The pending change record for a path, if this session has one. */
  function pendingOf(path: string): StudioChange | undefined {
    return pending[path];
  }

  /** Re-read this path's pending hunks and the baseline they hang off.
   *
   *  Called on open, after a save, and after any decision — the three moments
   *  when the set of things worth reviewing actually changes. It never
   *  re-reads the file: the buffer is the user's, and replacing it under them
   *  is how an editor loses work. */
  const refreshReview = useCallback(async (path: string) => {
    try {
      const got = await api.studioChanges(sid, path);
      const row = (got?.changes || []).find((c) => c.path === path) as
        StudioChange | undefined;
      setBuffers((b) => {
        const cur = b[path];
        if (!cur) return b;
        const hunks = row?.hunks || [];
        return { ...b, [path]: {
          ...cur,
          hunks,
          reviewBase: typeof row?.baseline === 'string' ? row.baseline : '',
          mode: cur.mode === 'review' && !hunks.length ? 'source' : cur.mode,
          conflict: row?.baseline_missing
            ? 'the stored baseline for this file is gone; the hunks below can '
              + 'no longer be reversed safely'
            : cur.conflict,
        } };
      });
    } catch (e) {
      setBuffers((b) => (b[path] ? { ...b, [path]: { ...b[path],
        conflict: e instanceof Error ? e.message : String(e) } } : b));
    }
  }, [sid]);

  const type = useCallback((text: string) => {
    setBuffers((b) => {
      const cur = b[active];
      if (!cur) return b;
      const next = { ...b, [active]: { ...cur, text, preview: false } };
      return next;
    });
  }, [active]);

  const save = useCallback(async () => {
    const buf = buffers[active];
    if (!buf || buf.text === buf.base) { setStatus('nothing to save'); return; }
    try {
      const r = await api.codeWrite(buf.path, buf.text,
                                    { confirm: true, mtime: buf.mtime, sid });
      if (r?.error) { setStatus(r.error); return; }
      if (!r?.ok) { setStatus(r?.backup ? 'written with a backup' : 'saved'); }
      setBuffers((b) => ({ ...b, [buf.path]: {
        ...b[buf.path], base: buf.text,
        mtime: typeof (r as { mtime?: number })?.mtime === 'number'
          ? (r as { mtime: number }).mtime : Date.now(),
      } }));
      setStatus(r?.unchanged ? 'already on disk' : `saved ${buf.path}`);
      /* A save moves the file relative to the baseline the pending hunks were
         measured against, so the review has to be re-read: showing hunks that
         can no longer be found is how a click turns into a refusal. */
      if (buf.mode === 'review') void refreshReview(buf.path);
      void loadPending();
    } catch (e) {
      setStatus(e instanceof Error ? e.message : String(e));
    }
  }, [active, buffers, sid, refreshReview, loadPending]);

  /* Ctrl+S belongs to the editor, not the browser. Bound on the window and
     scoped to this route by the component's lifetime: leaving Studio must hand
     the chord back, or a Code page or Authoring tab opened later would find
     Save hijacked by a view that is not on screen.

     `Esc` leaves review and `F8` walks its hunks for the same reason: they are
     the two gestures that make a diff reviewable without touching the mouse,
     and a keyboard shortcut that only works while the editor has focus is a
     shortcut nobody finds. */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        void save();
        return;
      }
      if (e.key === 'Escape') {
        const buf = buffers[active];
        if (buf?.mode === 'review') {
          e.preventDefault();
          setBuffers((b) => ({ ...b, [active]: { ...b[active], mode: 'source' } }));
          setStatus(`source view · ${buf.hunks.length} hunk(s) still unreviewed`);
        }
        return;
      }
      if (e.key === 'F8') {
        const buf = buffers[active];
        if (buf?.mode !== 'review' || !buf.hunks.length) return;
        e.preventDefault();
        if (e.shiftKey) reviewRef.current?.prev();
        else reviewRef.current?.next();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [save, active, buffers]);

  const closeTab = useCallback((path: string) => {
    const buf = buffers[path];
    if (buf && buf.text !== buf.base) { setConfirmDiscard(path); return; }
    setTabs((t) => {
      const next = t.filter((p) => p !== path);
      setActive((a) => (a === path ? (next[next.length - 1] || '') : a));
      return next;
    });
    setBuffers((b) => { const n = { ...b }; delete n[path]; return n; });
  }, [buffers]);

  /** Decide one hunk. `keep` never touches the file; `revert` does, so the
   *  buffer is re-read from disk afterwards rather than assumed — the pane
   *  must show what the server just wrote, not what it wrote on top of. */
  const applyHunk = useCallback(async (path: string, hunkId: string,
                                       action: 'keep' | 'revert') => {
    setBusyHunk(hunkId);
    try {
      const r = await api.studioHunkApply(sid, path, hunkId, action);
      if (r && r.ok === false) {
        const msg = String(r.error || 'the decision was refused');
        setBuffers((b) => (b[path]
          ? { ...b, [path]: { ...b[path], conflict: msg } } : b));
        setStatus(msg);
        return;
      }
      setStatus(action === 'keep'
        ? `kept one hunk of ${path} · ${r?.hunks_remaining ?? 0} left to review`
        : `reverted one hunk of ${path} · ${r?.hunks_remaining ?? 0} left`);
      if (action === 'revert') {
        const f = await api.codeFile(path, undefined, sid) as
          (Awaited<ReturnType<typeof api.codeFile>> & { error?: string }) | null;
        if (f && !f.error) {
          setBuffers((b) => (b[path] ? { ...b, [path]: {
            ...b[path], text: f.text || '', base: f.text || '',
            mtime: typeof f.mtime === 'number' ? f.mtime : b[path].mtime,
          } } : b));
        } else if (r?.deleted) {
          // The last hunk of a file the agent created went back, so the file is
          // gone; a tab that keeps showing it is worse than a closed one.
          closeTab(path);
          void loadPending();
          void loadCheckpoints();
          return;
        }
      }
      await refreshReview(path);
      void loadPending();
      void loadCheckpoints();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setBuffers((b) => (b[path]
        ? { ...b, [path]: { ...b[path], conflict: msg } } : b));
      setStatus(msg);
    } finally {
      setBusyHunk('');
    }
  }, [sid, refreshReview, closeTab, loadPending, loadCheckpoints]);

  /** Whole-file verbs, from the review toolbar. Revert here is the file's
   *  baseline, which after a keep is NOT what the agent found — so the label
   *  says what it returns to rather than promising an "undo the change". */
  const revertFile = useCallback(async (path: string) => {
    const r = await api.codeRevert(path, sid);
    setStatus(r?.error || (r?.deleted ? `reverted ${path} (file removed)`
                                      : `reverted ${path}`));
    if (r?.deleted) { closeTab(path); void loadPending(); return; }
    const f = await api.codeFile(path, undefined, sid) as
      (Awaited<ReturnType<typeof api.codeFile>> & { error?: string }) | null;
    if (f && !f.error) {
      setBuffers((b) => (b[path] ? { ...b, [path]: {
        ...b[path], text: f.text || '', base: f.text || '',
        mtime: typeof f.mtime === 'number' ? f.mtime : b[path].mtime,
        mode: 'source', hunks: [], conflict: '',
      } } : b));
    }
    void loadPending();
    void loadCheckpoints();
  }, [sid, closeTab, loadPending, loadCheckpoints]);

  const keepFile = useCallback(async (path: string) => {
    const r = await api.codeKeep(path, sid);
    setStatus(r?.error || `kept ${path}`);
    setBuffers((b) => (b[path] ? { ...b, [path]: {
      ...b[path], mode: 'source', hunks: [], reviewBase: '', conflict: '',
    } } : b));
    void loadPending();
  }, [sid, loadPending]);

  /* Every buffer whose file was written under it has to come back from disk.
     Keeping a stale copy would let a later Ctrl+S undo the restore. Shared by
     the checkpoint panel and `/undo` so the two cannot restore to different
     states of the same file. */
  const pullFromDisk = useCallback(async (paths: string[]) => {
    for (const p of paths) {
      if (!buffers[p]) continue;
      const f = await api.codeFile(p, undefined, sid) as
        (Awaited<ReturnType<typeof api.codeFile>> & { error?: string }) | null;
      if (f && !f.error) {
        setBuffers((b) => (b[p] ? { ...b, [p]: {
          ...b[p], text: f.text || '', base: f.text || '',
          mtime: typeof f.mtime === 'number' ? f.mtime : b[p].mtime,
          mode: 'source', hunks: [], conflict: '',
        } } : b));
      } else {
        /* The path is gone (a restore can take a renamed-away file with it).
           Keeping the buffer would leave a tab showing bytes that are no longer
           on disk, and saving it would recreate the file we just removed. */
        setBuffers((b) => {
          if (!b[p]) return b;
          const next = { ...b };
          delete next[p];
          return next;
        });
        setTabs((t) => t.filter((x) => x !== p));
      }
    }
  }, [sid, buffers]);

  /* A change set: every file one task left pending, and the two verbs that only
     make sense over the whole set. Reviewing a four-file rename one hunk at a
     time is how half of it ends up kept by accident. Each path is a whole-file
     verb (its baseline), then the buffers come back from disk so an open tab
     cannot save a stale copy over the decision. */
  const keepSet = useCallback(async (paths: string[]) => {
    for (const p of paths) await api.codeKeep(p, sid);
    setStatus(`kept ${paths.length} file(s) as one change`);
    await pullFromDisk(paths);
    void loadPending();
    void loadCheckpoints();
  }, [sid, pullFromDisk, loadPending, loadCheckpoints]);

  const revertSet = useCallback(async (paths: string[]) => {
    for (const p of paths) {
      const r = await api.codeRevert(p, sid);
      if (r?.deleted) closeTab(p);
    }
    setStatus(`reverted ${paths.length} file(s) as one change`);
    await pullFromDisk(paths);
    void loadPending();
    void loadCheckpoints();
  }, [sid, pullFromDisk, closeTab, loadPending, loadCheckpoints]);

  const restoreCheckpoint = useCallback(async (id: string, name: string) => {
    const r = await api.studioCheckpointRestore(sid, id);
    if (r && r.ok === false) { setStatus(String(r.error || 'restore refused')); return; }
    const parts = restoreParts(r);
    setStatus(`restored ${name} · ${parts.join(' · ') || 'nothing to do'}`
      + ` · ${r?.undo?.name || 'no undo captured'}`);
    await pullFromDisk(r?.restored || []);
    void loadPending();
    void loadCheckpoints();
  }, [sid, pullFromDisk, loadPending, loadCheckpoints]);

  /** `/undo` in the composer: the newest checkpoint, by the same call the
   *  Changes list uses. Answers with a sentence because a typed command that
   *  silently did nothing is indistinguishable from one that worked. */
  const undoLatest = useCallback(async (): Promise<string> => {
    /* Undo and redo walk one checkpoint list in opposite directions; the
       shared rule for which entry each takes lives in `timeline.ts`, where it
       is tested. Using `checkpoints[0]` blindly would make a second `/undo`
       cancel the first. */
    const cp = pickUndo(checkpoints, acted);
    if (!cp) return 'no checkpoint in this session yet — nothing to undo';
    const r = await api.studioCheckpointRestore(sid, cp.id);
    if (r && r.ok === false) return String(r.error || 'the restore was refused');
    const parts = restoreParts(r);
    await pullFromDisk(r?.restored || []);
    void loadPending();
    if (r?.acted) setActed(r.acted);
    void loadCheckpoints();
    return `undo “${cp.name}” · ${parts.join(' · ') || 'nothing to do'}`
      + (r?.undo?.id ? ' · /redo puts it back' : '');
  }, [sid, checkpoints, acted, pullFromDisk, loadPending, loadCheckpoints]);

  /** `/redo`: put back what the last `/undo` replaced. The restore that ran
   *  captured the pre-undo tree as a "before restoring …" checkpoint, so redo
   *  is that snapshot restored — no separate stack to keep in sync. */
  const redoLatest = useCallback(async (): Promise<string> => {
    const cp = pickRedo(checkpoints, acted);
    if (!cp) return 'nothing to redo — no undo has run in this session';
    const r = await api.studioCheckpointRestore(sid, cp.id);
    if (r && r.ok === false) return String(r.error || 'the restore was refused');
    const parts = restoreParts(r);
    await pullFromDisk(r?.restored || []);
    void loadPending();
    if (r?.acted) setActed(r.acted);
    void loadCheckpoints();
    return `redo “${restoreLabel(cp.name)}”`
      + ` · ${parts.join(' · ') || 'nothing to do'}`;
  }, [sid, checkpoints, acted, pullFromDisk, loadPending, loadCheckpoints]);

  /** `/new` in the composer and the "+ New session" button are the same act,
   *  so they call the same stable function. A fresh arrow here would be an
   *  unstable prop, and an unstable prop into the panel is the render loop
   *  that froze this page on 2026-11-06. */
  const newSession = useCallback(() => {
    const next = newStudioSid();
    if (setStoredStudioSid(next)) onSwitch(next);
  }, [onSwitch]);

  /** A folder group's "+". The sessions index only carries folder NAMES, so
      the absolute path comes from the roots of any session in the group —
      the roots file is the registry of where `S24 Car Game` actually lives. */
  const sessionsReload = sessions.reload;
  const looksAbsolute = (p: string) => /^([A-Za-z]:[\\/]|\/|~)/.test(p || '');
  const newSessionInFolder = useCallback((groupName: string,
                                          fromSid: string, seedPath?: string) => {
    const next = newStudioSid();
    const bind = (target: string) => {
      void api.studioRootAdd(next, target)
        .catch(() => { setStatus(`could not open ${target} in the new session`); })
        .then(() => { void sessionsReload(); })
        .finally(() => { if (setStoredStudioSid(next)) onSwitch(next); });
    };
    if (seedPath && looksAbsolute(seedPath)) { bind(seedPath); return; }
    void api.studioWorkspace(fromSid)
      .then((r) => {
        const hit = (r?.roots || []).find((x) => x.name === groupName);
        if (hit?.path) { bind(hit.path); return; }
        setStatus(`could not resolve where ${groupName} lives — open the folder from the empty state instead`);
      })
      .catch(() => {
        setStatus(`could not resolve where ${groupName} lives — open the folder from the empty state instead`);
      });
  }, [onSwitch, sessionsReload]);

  /* Stable so the panel can depend on it without being re-driven by it. */
  const refreshStudio = useCallback(() => {
    void loadPending();
    void loadCheckpoints();
  }, [loadPending, loadCheckpoints]);

  /* One project (folder) per session. Adding a new one replaces the old. */
  const setProject = useCallback(async (path: string): Promise<string> => {
    let err = '';
    for (const r of roots) {
      if (r.path === path) continue;
      err = await ws.removeFolder(r.path);
      if (err) return err;
    }
    if (!roots.some((r) => r.path === path)) err = await ws.addFolder(path);
    return err;
  }, [roots, ws]);

  const addFolder = useCallback(async () => {
    const p = folderPath.trim();
    setAdding(false); setFolderPath('');
    if (!p) return;
    const err = await setProject(p);
    // The server's reason is the useful part: "not a folder", "does not
    // exist", or the state-dir rule that keeps sessions/ out of reach.
    setStatus(err || `opened ${p}`);
    // A folder adds the session to the registry; the rail must not wait for
    // a remount to show it.
    void sessions.reload();
  }, [folderPath, setProject, sessions]);

  /* Unsaved buffers were shown in the footer, which is gone; the tab strip
     already marks a modified buffer, so the list itself is not needed. */

  /* Stable identities for everything handed down to the conversation. A fresh
     arrow per render is normally harmless, but a child that keeps one in a
     hook dependency list re-runs its effect on every parent render — and if
     that effect fetches and sets state here, the two components drive each
     other forever. That is how this page froze: three requests per round,
     unbounded. */
  const openForReview = useCallback((p: string) => {
    void openPath(p, true, true);
    setShowWorkbench(true);
  }, [openPath]);

  const notePending = useCallback(() => {
    void loadPending();
    void loadCheckpoints();
    /* Every finished or errored turn lands here, so it is the one place that
       refreshes the session's copy of the runs list — without it the context
       meter, the USAGE block and the "N agents" footer all kept the pre-turn
       values forever. */
    void runs.reload();
  }, [loadPending, loadCheckpoints, runs.reload]);

  const showChanges = useCallback(() => { openPanel('changes'); }, [openPanel]);

  /* The right-hand slide-over. Opening it with no tab chosen lands on the file
     explorer — the panel a coding session reaches for first. The same button
     closes it, so this is a toggle rather than an open-only verb. */
  const togglePanel = useCallback(() => {
    if (panelOpen) { setPanelOpen(false); return; }
    setOpenPanels((list) => (list.length ? list : [{ key: 'explorer-0', id: 'explorer' }]));
    setDrawerKey((k) => (k || 'explorer-0'));
    setDrawer((d) => (d || 'explorer'));
    setPanelOpen(true);
  }, [panelOpen]);

  /** Close one panel tab. Closing the last one closes the slide-over. */
  const closePanelTab = useCallback((key: string) => {
    const next = openPanels.filter((t) => t.key !== key);
    setOpenPanels(next);
    if (drawerKey === key) {
      if (!next.length) { setDrawerKey(''); setDrawer(''); setPanelOpen(false); }
      else { setDrawerKey(next[next.length - 1].key); setDrawer(next[next.length - 1].id); }
    }
  }, [openPanels, drawerKey]);

  /* Open a project: always start a NEW session bound to it, so two sessions
     can work the same folder and the blank one stays blank. */
  const openProject = useCallback(async (path: string) => {
    let target = path;
    if (!/^([A-Za-z]:[\\/]|\/)/.test(target)) {
      const hit = roots.find((r) => r.name === target || r.path === target);
      if (hit) target = hit.path;
    }
    const next = newStudioSid();
    const res = await api.studioRootAdd(next, target) as { error?: string } | null;
    if (res?.error) { setStatus(res.error); return; }
    if (setStoredStudioSid(next)) onSwitch(next);
    void sessions.reload();
  }, [roots, sessions, onSwitch]);

  const switchSession = useCallback((target: string) => {
    setClosedTabs((prev) => {
      if (!prev.has(target)) return prev;
      const n = new Set(prev);
      n.delete(target);
      return n;
    });
    onSwitch(target);
  }, [onSwitch]);

  /* Every project the studio knows about, grouped from the session index.
     Sessions keep their whole row: the flyout needs `current` (where you are),
     `updated` (recency) and `pending_review` (work waiting) to be a switcher
     rather than a list of names. */
  const projects = useMemo(() => {
    const map = new Map<string, StudioSession[]>([]);
    const bySid = new Map<string, StudioSession>();
    for (const s of sessions.sessions) bySid.set(s.sid, s);
    const add = (p: string, s: StudioSession) => {
      const name = p.split(/[\\/]/).filter(Boolean).pop() || p;
      const entry = map.get(name) || [];
      if (!entry.some((x) => x.sid === s.sid)) entry.push(s);
      map.set(name, entry);
    };
    for (const s of sessions.sessions) for (const p of s.paths || []) add(p, s);
    const cur = bySid.get(sid);
    const self: StudioSession = cur || {
      sid, label: '', roots: roots.length, paths: roots.map((r) => r.name),
      archived: false, created: 0, updated: 0, current: true,
    };
    for (const r of roots) add(r.path, self);
    return [...map.entries()].map(([name, list]) => ({
      path: list[0]?.paths?.[0] || name,
      name,
      sessions: [...list].sort((a, b) => (b.updated || 0) - (a.updated || 0)),
    }));
  }, [sessions.sessions, roots, sid]);

  const visibleSessions = useMemo(() => {
    const list = sessions.sessions.filter((s) => !closedTabs.has(s.sid));
    // A brand-new session has no server row yet; show it anyway.
    if (!list.some((s) => s.sid === sid)) {
      const cur = sessions.sessions.find((s) => s.sid === sid);
      list.unshift({
        sid, label: cur?.label || '', roots: roots.length,
        paths: roots.map((r) => r.name), archived: false,
        created: cur?.created || 0, updated: cur?.updated || 0, current: true,
      });
    }
    return list;
  }, [sessions.sessions, closedTabs, sid, roots]);

  /* Two sessions can point at the same folder, so their fallback names collide
     ("gmail-agent-mcp - Copy" twice) and the strip shows two identical tabs.
     A duplicated label is disambiguated with a short suffix of its id, so every
     tab is tellable apart at a glance. */
  const sessionLabels = useMemo(() => {
    const counts = new Map<string, number>();
    for (const s of visibleSessions) {
      const n = sessionName(s);
      counts.set(n, (counts.get(n) || 0) + 1);
    }
    const out = new Map<string, string>();
    for (const s of visibleSessions) {
      const n = sessionName(s);
      out.set(s.sid, (counts.get(n) || 0) > 1 ? `${n} · ${s.sid.slice(-4)}` : n);
    }
    return out;
  }, [visibleSessions]);

  /* Escape leaves the panel picker. It is a content view, not a popover, so an
     outside click must not dismiss it. */
  useEffect(() => {
    if (!panelMenu) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setPanelMenu(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [panelMenu]);

  /* The palette shortcuts from the OpenCode reference. */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!e.ctrlKey || !e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === 'b') { e.preventDefault(); openPanel('browser'); }
      else if (k === 't') { e.preventDefault(); openPanel('terminal'); }
      else if (k === 'p') { e.preventDefault(); openPanel('explorer'); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [openPanel]);

  /* Open the OS "Open Folder…" dialog on the machine the agent runs on.
     Someone who wants this app to open their project folder does not want
     a rebuilt tree view — they want the dialog from every other app. The
     call parks until the dialog is closed (or five minutes pass), shows a
     status line while it is open, and falls back to the in-app picker when
     the platform has no dialog or the route fails. */
  const [pickingFolder, setPickingFolder] = useState(false);
  const openFolder = useCallback(async () => {
    if (pickingFolder) return;
    setPickingFolder(true);
    setStatus('waiting for you to pick a folder…');
    try {
      const r = await api.studioPickFolder();
      if (r?.path) {
        const err = await setProject(r.path);
        setStatus(err || `opened ${r.path}`);
        void sessions.reload();
      } else if (r?.cancelled) {
        setStatus('folder pick cancelled');
      } else {
        setStatus(r?.error ? `OS picker unavailable (${r.error}) — browsing in-app`
                           : 'OS picker unavailable — browsing in-app');
        setPickerOpen(true);
      }
    } catch (e) {
      setStatus(`OS picker unavailable (${e instanceof Error ? e.message : String(e)}) — browsing in-app`);
      setPickerOpen(true);
    } finally {
      setPickingFolder(false);
    }
  }, [pickingFolder, setProject, sessions]);

  const addFolderHere = useCallback(() => {
    openPanel('explorer');
    void openFolder();
  }, [openFolder, openPanel]);

  /* The picker's answer. The server still enforces `validate_root` on add;
     the picker only greyed the refusal ahead of time. */
  const pickFolder = useCallback(async (p: string) => {
    setPickerOpen(false);
    const err = await setProject(p);
    setStatus(err || `opened ${p}`);
    void sessions.reload();
    // Picking a folder from disk makes it the project: reveal its tree.
    if (!err) openPanel('explorer');
  }, [setProject, sessions, openPanel]);



  const nameFromFirstTask = useCallback((text: string) => {
    void sessions.rename(text.slice(0, 80));
  }, [sessions]);

  const activeBuf = buffers[active];
  const openPaths = useMemo(() => tabs.slice(), [tabs]);
  const navigate = useNavigate();
  const ctxPct = ctx.cap > 0 ? Math.min(100, Math.round((ctx.chars / ctx.cap) * 100)) : 0;
  /* The ring measures characters (the agent's own context meter is char-based),
     but a person reads context in TOKENS. ~4 chars/token is the usual English
     estimate; label it as an estimate rather than pretending it is exact. */
  const toTokens = (chars: number) => Math.round((chars || 0) / 4);
  const ctxTokens = toTokens(ctx.chars);
  const ctxCapTokens = toTokens(ctx.cap);
  const fmtK = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));
  const ctxTitle = ctx.cap > 0
    ? `${model || 'Auto model'} · ~${ctxTokens.toLocaleString()} / ~${ctxCapTokens.toLocaleString()} tokens`
    : `${model || 'Auto model'} · no context measurement yet`;

  return (
    <div className="studio-oc relative flex h-full min-h-0 w-full flex-col overflow-hidden bg-void">
      {/* One fixed bar above the row: console exit, session tabs, the open
          panel tabs, and the context / toggle controls. It never shrinks, so
          the toggle cannot move when the slide-over opens, and the panel tabs
          share its line. */}
      <div className="flex flex-none items-center gap-1 border-b border-glass-border bg-surface-0 px-2 py-1.5">
        <button type="button" onClick={() => navigate('/console')}
                aria-label="Back to the console" title="Back to the console"
                className="flex h-7 flex-none items-center gap-1.5 rounded-md px-2 text-[12px] text-zinc-400 transition-colors hover:bg-white/5 hover:text-zinc-100">
          <ArrowLeft size={13} />
          <span className="hidden sm:inline">Console</span>
        </button>
        <span aria-hidden className="h-4 w-px flex-none bg-white/10" />
        <div className="relative flex min-w-0 flex-1 items-center">
          <div ref={sessionStripRef}
               className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
            {visibleSessions.slice(0, 10).map((s, i) => {
            const on = s.sid === sid;
            return (
              <div key={s.sid} className="flex flex-none items-center">
                {/* Small vertical rule between tab outlines. */}
                {i > 0 && <span aria-hidden className="mx-1 h-4 w-px flex-none bg-white/10" />}
                <div title={sessionLabels.get(s.sid) || sessionName(s)}
                     className={'group flex w-[180px] flex-none items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12px] transition-colors '
                       + (on ? 'bg-accent/20 font-medium text-zinc-50'
                             : 'text-zinc-muted hover:bg-white/5 hover:text-zinc-200')}>
                  {/* The active session gets a violet edge, not just a lighter
                      grey: with ten tabs of similar weight the current one has
                      to be findable without reading every label. */}
                  {on && <span aria-hidden className="h-4 w-1 flex-none rounded-full bg-accent" />}
                  <span aria-hidden className="flex h-4 w-4 flex-none items-center justify-center rounded bg-accent/20 font-bold text-[10px] text-accent">
                    {(sessionLabels.get(s.sid) || sessionName(s)).slice(0, 1).toUpperCase()}
                  </span>
                  <button type="button" onClick={() => onSwitch(s.sid)}
                          className="min-w-0 flex-1 truncate text-left">
                    {sessionLabels.get(s.sid) || sessionName(s)}
                  </button>
                  {!!s.live && (
                    <span className="h-1.5 w-1.5 flex-none animate-pulse rounded-full bg-emerald-400" title="running" />
                  )}
                  <button type="button" aria-label={`Close ${s.label || 'session'}`}
                          onClick={() => {
                            setClosedTabs((prev) => new Set(prev).add(s.sid));
                            if (s.sid === sid) {
                              const next = visibleSessions.find((x) => x.sid !== s.sid);
                              if (next) onSwitch(next.sid);
                            }
                          }}
                          className="flex h-4 w-4 flex-none items-center justify-center rounded text-zinc-500 opacity-0 transition hover:bg-white/10 hover:text-danger group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100">
                    <X size={11} />
                  </button>
                </div>
              </div>
            );
          })}
          <button type="button" onClick={newSession} aria-label="New session" title="New session"
                  className="flex h-7 w-7 flex-none items-center justify-center rounded-lg text-zinc-muted transition-colors hover:bg-white/5 hover:text-zinc-100">
            <Plus size={13} />
          </button>
          </div>
          {/* The strip hides its scrollbar, so with enough sessions the tabs
              simply vanish off the right edge. A faded edge and a step button
              make the overflow visible without giving up the compact bar. */}
          {visibleSessions.length > 6 && (
            <button type="button" aria-label="Scroll sessions" title="More sessions"
                    onClick={() => sessionStripRef.current?.scrollBy({ left: 380, behavior: 'smooth' })}
                    className="absolute right-0 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md border border-glass-border bg-surface-1 text-zinc-400 shadow-md transition-colors hover:text-zinc-100">
              <ChevronRight size={12} />
            </button>
          )}
        </div>
      </div>
      {/* Second row: the context ring and the panel toggle, moved out of the
          top bar so the session tabs get the full width. Right-aligned, same
          height as the controls they carried. */}
      <div className="flex flex-none items-center justify-between gap-2 border-b border-glass-border bg-surface-0 px-2 py-1">
        {/* Breadcrumb: the second row now names what you are looking at. */}
        <div className="flex min-w-0 items-center gap-1.5 text-[11px]">
          <span className="flex-none rounded bg-accent/20 px-1.5 py-0.5 font-bold text-accent">
            {(roots[0]?.name || 'P').slice(0, 1).toUpperCase()}
          </span>
          <span className="flex-none text-zinc-400">{roots[0]?.name || 'project'}</span>
          <span className="text-zinc-600">/</span>
          <span className="min-w-0 truncate text-zinc-200">{sessionLabels.get(sid) || 'session'}</span>
        </div>
        <div className="flex flex-none items-center gap-2">
          <span className="font-mono text-[11px] text-zinc-400">
            {ctxTokens ? fmtK(ctxTokens) : '—'}
          </span>
        <div className="relative flex-none">
          <button type="button" onClick={() => setCtxMenu((v) => !v)} disabled={!roots.length}
                  aria-label={`Model context: ${ctxTitle}`} title={ctxTitle}
                  className="relative flex h-7 w-7 items-center justify-center rounded-full disabled:opacity-40"
                  style={{ background: `conic-gradient(#ffffff ${ctxPct}%, rgba(255,255,255,0.14) 0)` }}>
            <span aria-hidden className="absolute inset-[2.5px] rounded-full bg-surface-1" />
          </button>
          {ctxMenu && (
            <div role="menu" aria-label="Model context"
                 className="absolute right-0 top-full z-50 mt-1 w-[240px] overflow-hidden rounded-md border border-glass-border bg-surface-2 shadow-depth">
              <div className="flex items-center justify-between px-3 py-1.5 text-[12px] text-zinc-300">
                <span>Tokens</span><span className="font-mono">~{ctxTokens.toLocaleString()}</span>
              </div>
              <div className="flex items-center justify-between px-3 py-1.5 text-[12px] text-zinc-300">
                <span>Context</span><span className="font-mono">{ctx.cap ? `${ctxPct}%` : '—'}</span>
              </div>
              {/* The ring is the control people actually click, so the popover
                  is where "0%" has to stop looking like a measurement. */}
              <div className="px-3 pb-2">
                <div className="h-1 w-full overflow-hidden rounded-full bg-white/[0.06]">
                  <div className={'h-full rounded-full '
                    + (ctxPct >= 85 ? 'bg-danger' : ctxPct >= 60 ? 'bg-warn' : 'bg-accent')}
                    style={{ width: `${Math.min(100, Math.max(ctx.cap ? 1 : 0, ctxPct))}%` }} />
                </div>
                <p className="mt-1 text-[10.5px] leading-snug text-zinc-muted">
                  {ctx.cap
                    ? `~${ctxTokens.toLocaleString()} of ~${ctxCapTokens.toLocaleString()} tokens`
                    : 'not measured yet — send the agent a message'}
                </p>
              </div>
              <button type="button" role="menuitem"
                      onClick={() => { setCtxMenu(false); openPanel('context'); }}
                      className="w-full border-t border-glass-border px-3 py-2 text-left text-[12px] text-zinc-200 hover:bg-white/5">
                View context usage
              </button>
            </div>
          )}
        </div>
        <button type="button" onClick={togglePanel} disabled={!roots.length}
                aria-label={panelOpen ? 'Close panel' : 'Open panel'}
                title={panelOpen ? 'Close panel' : 'Open panel'}
                aria-expanded={panelOpen}
                className="flex h-7 w-7 flex-none items-center justify-center rounded-full border border-glass-border bg-surface-1 text-zinc-400 transition-colors hover:border-white/20 hover:text-zinc-200 disabled:opacity-40">
          {panelOpen ? <PanelRightClose size={14} /> : <PanelRightOpen size={14} />}
        </button>
        </div>
      </div>
      <div className="flex min-h-0 flex-1">
        {/* Sessions own the left column, not files.
            The app Rail is never rendered here, which is what "isolated from
            the sidebar" has to mean to be worth anything — and inside the
            studio the same argument applies one level down: the object you
            manage is the agent working on your code, so that is what the list
            shows. Files live in the workbench, beside the conversation. */}
        {SHOW_SESSIONS && (railCollapsed ? (
          <div className="flex w-12 flex-none flex-col items-center gap-1 border-r border-glass-border bg-surface-0 py-2">
            <button type="button" onClick={() => setRailCollapsed(false)}
                    aria-label="Show sessions" title="Show sessions"
                    className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-white/5 hover:text-zinc-100">
              <PanelLeftOpen size={16} />
            </button>
            <button type="button" onClick={newSession}
                    aria-label="New session" title="New session"
                    className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-white/5 hover:text-zinc-100">
              <Plus size={16} />
            </button>
            <span className="my-1 h-px w-6 bg-white/10" />
            {([
              ['explorer', <FolderTree size={16} />, 'Files'],
              ['search', <Search size={16} />, 'Search'],
              ['changes', <ShieldAlert size={16} />, 'Changes'],
              ['git', <GitBranch size={16} />, 'Git'],
              ['browser', <Globe size={16} />, 'Browser'],
              ['runs', <ScrollText size={16} />, 'Runs'],
            ] as const).map(([id, icon, label]) => (
              <button key={id} type="button"
                      onClick={() => { setShowWorkbench(true); setDrawer(drawer === id ? '' : id); }}
                      aria-label={label} title={label} aria-pressed={drawer === id}
                      className={'flex h-8 w-8 items-center justify-center rounded-md transition-colors '
                        + (drawer === id
                          ? 'bg-white/10 text-zinc-50'
                          : 'text-zinc-400 hover:bg-white/5 hover:text-zinc-100')}>
                {icon}
              </button>
            ))}
            <span className="min-h-0 flex-1" />
            <button type="button" onClick={toggleDockSide}
                    aria-label="Toggle dock side" title="Dock side / bottom"
                    className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-white/5 hover:text-zinc-100">
              <PanelRightOpen size={16} />
            </button>
            <a href="/console" aria-label="Console" title="Back to the console"
               className="flex h-8 w-8 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-white/5 hover:text-zinc-100">
              <ArrowUpRight size={16} />
            </a>
          </div>
        ) : (
          /* The same act as the hero's button: the path input lives in the
             workbench aside, so `adding` alone renders nothing and the menu item
             looks dead whenever the workbench is hidden. */
          <div className="w-[248px] flex-none">
            <Sessions sid={sid} sessions={sessions.sessions}
                      loading={sessions.loading} error={sessions.error}
                      onSwitch={onSwitch}
                      onNew={newSession}
                      onNewInFolder={newSessionInFolder}
                      onArchive={sessions.archive}
                      onAddFolder={addFolderHere}
                      onReload={() => { void sessions.reload(); void ws.refresh(); }} />
          </div>
        ))}

        {/* The conversation is the surface the studio opens on. */}
        <main className="flex min-h-0 min-w-0 flex-1 flex-col bg-void">
          <AgentPanel sid={sid} activePath={active}
                      activeText={activeBuf?.text || ''} openFiles={openPaths}
                      onOpenPath={openForReview}
                      onPending={notePending}

                      onChanges={showChanges}
                      roots={roots}
                      onAddFolder={addFolderHere}
                      onContext={onContext}
                      onGit={() => openPanel('git')}
                      onBrowseDisk={() => setPickerOpen(true)}
                      projects={projects}
                      onOpenProject={(p) => void openProject(p)}
                      onSwitchSession={switchSession}
                      onFirstTask={nameFromFirstTask}
                      runs={runs.runs}
                      onWatch={runs.watch}
                      onRunStarted={() => { void runs.reload(); }}
                      onNewSession={newSession}
                      onUndo={undoLatest}
                      onRedo={redoLatest}
                      onRefresh={refreshStudio}
                      onTerminal={() => { setShowWorkbench(true); setDockOpen(true); setDockTab('terminal'); }}
                      branch={branch}
                      refreshBranch={refreshBranch}
                      model={model}
                      setModel={chooseModel}
                      runsReady={!runs.loading}
                      disabled={!roots.length} />
        </main>

        {/* The workbench: tabs, editor, diff, file tree, changes. It appears
            beside the conversation when there is something to look at and is
            dismissed when there is not — hiding it keeps every buffer loaded,
            so bringing it back is instant rather than a refetch. Its sections
            sit behind the vertical bar on the far right. */}
        {SHOW_WORKBENCH && showWorkbench && (
          <>
            <Splitter split={right} label="Conversation and workbench" />
            <section className={'flex min-h-0 border-l border-glass-border bg-void '
              + (dockSide ? 'flex-row' : 'flex-col')}
                     style={{ width: right.live ? `${right.value}%` : '52%',
                              maxWidth: 'none' }}>
              <div className="flex min-h-0 flex-1">

                <div className="flex min-h-0 min-w-0 flex-1 flex-col">
                  <div role="tablist" aria-label="Open files"
                       className="flex flex-none items-stretch overflow-x-auto border-b border-glass-border bg-surface-0">
            {tabs.map((p) => {
              const b = buffers[p];
              const isDirty = !!b && b.text !== b.base;
              return (
                <div key={p}
                     role="tab" aria-selected={active === p}
                     className={
                       'group flex max-w-[220px] flex-none cursor-pointer items-center gap-1 border-t-2 border-t-transparent border-r border-glass-border px-2 py-1.5 text-[12px] '
                       + (active === p
                         ? 'border-t-2 border-t-accent bg-void text-zinc-100'
                         : 'text-zinc-muted hover:bg-white/5')
                     }>
                  <button type="button" onClick={() => setActive(p)}
                          onDoubleClick={() => setBuffers((bb) => ({ ...bb, [p]: { ...bb[p], preview: false } }))}
                          className="min-w-0 truncate"
                          title={p}
                          aria-label={`Focus ${p}`}>
                    <span className={b?.preview ? 'italic' : ''}>{b?.name || BASENAME(p)}</span>
                    {isDirty && <span className="ml-1 text-zinc-400" title="unsaved">●</span>}
                  </button>
                  <button type="button"
                          onClick={() => closeTab(p)}
                          aria-label={`Close ${p}`}
                          className="flex h-5 w-5 flex-none items-center justify-center rounded text-zinc-muted opacity-50 hover:bg-white/10 hover:opacity-100"><X size={11} /></button>
                </div>
              );
            })}
            {!tabs.length && (
              /* The workbench's own empty state below is the action; a second
                 clickable "open one" up here was the same invitation twice,
                 60px apart. */
              <span className="px-3 py-1.5 text-[11px] text-zinc-muted">no file open</span>
            )}
          </div>

          <div className="flex min-h-0 flex-1 flex-col">
            {confirmDiscard && (
              <div className="flex flex-none items-center gap-2 border-b border-warn/30 bg-warn-dim px-3 py-1.5 text-[12px] text-warn">
                <span className="min-w-0 flex-1 truncate">
                  {BASENAME(confirmDiscard)} has unsaved edits.
                </span>
                <button type="button"
                        onClick={() => {
                          const p = confirmDiscard;
                          setConfirmDiscard('');
                          setTabs((t) => {
                            const next = t.filter((x) => x !== p);
                            setActive((a) => (a === p ? (next[next.length - 1] || '') : a));
                            return next;
                          });
                          setBuffers((b) => { const n = { ...b }; delete n[p]; return n; });
                        }}
                        className="rounded-md border border-glass-border px-1.5 py-0.5 hover:bg-white/5">
                  discard anyway
                </button>
                <button type="button"
                        onClick={() => { setActive(confirmDiscard); setConfirmDiscard(''); void save(); }}
                        className="rounded-md border border-glass-border px-1.5 py-0.5 hover:bg-white/5">
                  save it
                </button>
                <button type="button" onClick={() => setConfirmDiscard('')}
                        className="rounded-md px-1.5 py-0.5 text-warn/70 hover:bg-white/5"><X size={11} /></button>
              </div>
            )}
            {activeBuf?.loading && (
              <p className="flex-none px-3 py-2 text-[12px] text-zinc-muted">loading…</p>
            )}
            {activeBuf?.error && !activeBuf.loading && (
              <p className="flex-none border-b border-warn/30 bg-warn-dim px-3 py-1.5 text-[12px] text-warn">
                {activeBuf.error}
              </p>
            )}
            {activeBuf && !activeBuf.loading && !activeBuf.failed && (
              /* The two modes of the same buffer. `review` is the server's
                 baseline beside the current text with a Keep/Revert under each
                 hunk; `source` is the ordinary editor. Both are the file you
                 have open — the mode decides what you are looking AT, not what
                 is loaded. */
              <div className="flex flex-none items-center gap-2 border-b border-glass-border bg-surface-0 px-2 py-1 text-[11px]">
                <div role="group" aria-label="Editor view"
                     className="flex items-center gap-0.5 rounded-md border border-glass-border p-0.5">
                  <button type="button" aria-pressed={activeBuf.mode === 'source'}
                          onClick={() => setBuffers((b) => ({ ...b, [active]:
                            { ...b[active], mode: 'source' } }))}
                          className={'rounded-md px-1.5 py-0.5 '
                            + (activeBuf.mode === 'source'
                              ? 'bg-white/10 text-zinc-100' : 'text-zinc-muted hover:bg-white/5')}>
                    source
                  </button>
                  <button type="button" aria-pressed={activeBuf.mode === 'review'}
                          disabled={!activeBuf.hunks.length}
                          title={activeBuf.hunks.length
                            ? `${activeBuf.hunks.length} unreviewed hunk(s) — Esc leaves review`
                            : 'nothing pending on this file'}
                          onClick={() => {
                            setBuffers((b) => ({ ...b, [active]:
                              { ...b[active], mode: 'review' } }));
                            void refreshReview(active);
                          }}
                          className={'rounded-md px-1.5 py-0.5 '
                            + (activeBuf.mode === 'review'
                              ? 'bg-white/10 text-zinc-100' : 'text-zinc-muted hover:bg-white/5')
                            + ' disabled:opacity-40'}>
                    review{activeBuf.hunks.length ? ` ·${activeBuf.hunks.length}` : ''}
                  </button>
                </div>
                {activeBuf.mode === 'review' && !!activeBuf.hunks.length && (
                  <>
                    <button type="button" onClick={() => void keepFile(activeBuf.path)}
                            className="whitespace-nowrap rounded-md border border-ok/40 bg-ok-dim px-2 py-0.5 text-ok hover:bg-ok/20">
                      Keep file
                    </button>
                    <button type="button" onClick={() => void revertFile(activeBuf.path)}
                            className="whitespace-nowrap rounded-md border border-danger/40 bg-danger-dim px-2 py-0.5 text-danger hover:bg-danger/20">
                      Revert file
                    </button>
                  </>
                )}
                {/* The two muted strings this row used to carry ("F8 next
                    hunk · Shift+F8 previous · Esc source" and "vs the stored
                    baseline") were half its width, permanently. Same facts,
                    now on hover. */}
                <span className="ml-auto min-w-0 truncate text-[11px] text-zinc-muted"
                      title={activeBuf.mode === 'review'
                        ? `${activeBuf.path}\nCompares against the stored baseline.\nF8 next hunk · Shift+F8 previous · Esc source`
                        : activeBuf.path}>
                  {activeBuf.mode === 'review' && activeBuf.hunks.length
                    ? `${activeBuf.hunks.length} hunk${activeBuf.hunks.length === 1 ? '' : 's'} in view`
                    : ''}
                </span>
              </div>
            )}
            {activeBuf?.conflict && activeBuf.mode === 'review' && (
              <p role="alert"
                 className="flex-none border-b border-warn/30 bg-warn-dim px-3 py-1.5 text-[11px] text-warn">
                {activeBuf.conflict}
              </p>
            )}
            {activeBuf?.failed ? (
              /* A buffer that never loaded is not an empty document. Before
                 this it fell through to the ordinary editor: line 1 of
                 nothing, a source/review switch, and a one-line banner above
                 it saying the file could not be read — the pane and the
                 banner describing different situations. */
              <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 px-6">
                <span aria-hidden className="text-warn/80"><TriangleAlert size={20} /></span>
                <p className="max-w-[60ch] text-center text-[13px] leading-relaxed text-warn">
                  {activeBuf.error || 'the file could not be read'}
                </p>
                <p className="max-w-[60ch] text-center text-[12px] leading-relaxed text-zinc-muted">
                  Nothing is open in this tab — there are no bytes to edit and
                  no baseline to review. If the agent wrote this file, its
                  change is still unreviewed and can be put back; otherwise
                  close the tab and open the file again.
                </p>
                <div className="flex items-center gap-2">
                  <button type="button"
                          onClick={() => { void openPath(activeBuf.path, true, true); }}
                          className="rounded-md border border-glass-border px-2.5 py-1 text-[12px] text-zinc-200 hover:bg-white/5">
                    Try again
                  </button>
                  <button type="button"
                          onClick={() => void revertFile(activeBuf.path)}
                          className="rounded-md border border-danger/40 bg-danger-dim px-2.5 py-1 text-[12px] text-danger hover:bg-danger/20">
                    Revert to the checkpoint
                  </button>
                  <button type="button" onClick={() => closeTab(activeBuf.path)}
                          className="rounded-md px-2.5 py-1 text-[12px] text-zinc-muted hover:bg-white/5 hover:text-zinc-200">
                    Close tab
                  </button>
                </div>
              </div>
            ) : activeBuf && !activeBuf.loading ? (
              activeBuf.mode === 'review' ? (
                <ReviewEditor
                  path={activeBuf.path}
                  lang={activeBuf.lang}
                  base={activeBuf.reviewBase}
                  body={activeBuf.text}
                  hunks={activeBuf.hunks}
                  busy={busyHunk}
                  dirty={activeBuf.text !== activeBuf.base}
                  conflict=""
                  ariaLabel={`Review of ${activeBuf.path}`}
                  onApply={(id, action) => void applyHunk(activeBuf.path, id, action)}
                  onChange={(v) => type(v)}
                  onHandle={(h) => { reviewRef.current = h; }}
                />
              ) : (
                <div className="relative min-h-0 flex-1">
                  <MonacoEditor
                    body={activeBuf.text}
                    lang={activeBuf.lang}
                    path={activeBuf.path}
                    ariaLabel={`Editor for ${activeBuf.path}`}
                    onChange={(v) => type(v)}
                    onCursor={(info) => setCursor({ line: info.line, col: info.col })}
                    onHandle={(h) => { edRef.current = h; }}
                  />
                </div>
              )
            ) : (!activeBuf && (
              <div className="flex min-h-0 flex-1 flex-col items-center justify-center px-6">
                <Empty icon={roots.length ? <FileText size={28} /> : <FolderOpen size={28} />}
                       title={roots.length ? 'No file is open' : 'No folders yet'}
                       sub={roots.length
                         ? 'Open one from the Files drawer, or ask the agent to read a file.'
                         : 'Add a folder to give this session somewhere to work.'} />
                {roots.length ? (
                  <button type="button"
                          onClick={() => { setShowWorkbench(true); setDrawer('explorer'); }}
                          className="flex items-center gap-1.5 rounded-md border border-glass-border px-2.5 py-1 text-[12px] text-zinc-300 hover:bg-white/5 hover:text-zinc-100">
                    <FolderOpen size={12} /> Open a file
                  </button>
                ) : (
                  <button type="button"
                          onClick={() => { setShowWorkbench(true); setDrawer('explorer'); void openFolder(); }}
                          className="flex items-center gap-1.5 rounded-md bg-zinc-100 px-3.5 py-1.5 text-xs font-bold text-[#161616] transition-colors hover:brightness-110">
                    <FolderPlus size={13} /> Add a folder
                  </button>
                )}
              </div>
            ))}
          </div>
                </div>


              </div>
              {/* The dock spans the workbench, not the editor column: a shell,
                  a build log and a problem list are read against the file you
                  are looking at, and a terminal squeezed into the leftover
                  width beside an open file tree is 46 pixels of unusable
                  prompt — measured, not guessed, when the drawer was open. */}
              <div className={dockSide
                ? 'flex min-h-0 flex-none'
                : 'flex min-h-0 flex-none flex-col'}
                   style={dockSide ? { width: 380 } : undefined}>
                <Dock sid={sid} roots={roots} tab={dockTab} setTab={setDockTab}
                      open={dockOpen} setOpen={setDockOpen} buffers={buffers}
                      runs={runs.runs} height={dockHeight}
                      setHeight={setDockHeight}
                      side={dockSide}
                      onOpenPath={(p) => void openPath(p, true, true)} />
              </div>
            </section>
          </>
        )}
        <div className="flex min-h-0 flex-none overflow-hidden transition-[width] duration-200 ease-out"
             style={{ width: panelOpen ? drawerPx.value + 3 : 0 }}>
          {panelOpen && <Splitter split={drawerPx} label="Section panel width" />}
          <aside
            className="flex h-full min-h-0 min-w-0 flex-1 flex-col border-l border-glass-border shadow-depth"
            style={{ width: drawerPx.value, background: 'var(--color-surface-1)' }}>
            {/* The console's card accent: a hairline along the top edge. */}
            <span aria-hidden
                  className="pointer-events-none absolute inset-x-0 top-0 h-[2px] bg-gradient-to-r from-accent/70 to-transparent" />
            {/* The panel tabs, in the panel (not the session bar). */}
            <div className="flex flex-none items-center gap-1 overflow-x-auto border-b border-glass-border px-2 py-1.5">
              {openPanels.map((t) => {
                const id = t.id;
                const on = drawerKey === t.key;
                return (
                  <div key={t.key}
                       className={'group flex flex-none items-center gap-1.5 rounded-lg px-2.5 py-[6px] text-[12px] transition-colors '
                         + (on ? 'bg-white/[0.06] font-semibold text-zinc-100'
                               : 'text-zinc-400 hover:bg-white/5 hover:text-zinc-200')}>
                    <button type="button" onClick={() => { setDrawerKey(t.key); setDrawer(id); }} className="flex items-center gap-1.5">
                      <span className={on ? 'text-accent' : 'text-zinc-muted'}>{DRAWER_ICON[id]}</span>
                      <span className="whitespace-nowrap">{DRAWER_LABEL[id]}</span>
                    </button>
                    <button type="button" aria-label={`Close ${DRAWER_LABEL[id]} tab`}
                            onClick={() => closePanelTab(t.key)}
                            className="flex h-4 w-4 items-center justify-center rounded text-zinc-500 opacity-0 transition hover:bg-white/10 hover:text-danger group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100">
                      <X size={10} />
                    </button>
                  </div>
                );
              })}
              <button type="button" onClick={() => setPanelMenu((v) => !v)}
                      aria-label="Open a panel tab" title="Open a panel tab"
                      className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-white/5 hover:text-zinc-100">
                <Plus size={13} />
              </button>
            </div>
            {adding && (
              <div className="flex flex-none items-center gap-1.5 border-b border-glass-border px-2.5 py-2">
                <input
                  autoFocus
                  value={folderPath}
                  onChange={(e) => setFolderPath(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void addFolder();
                    if (e.key === 'Escape') { setAdding(false); setFolderPath(''); }
                  }}
                  placeholder="C:\\dev\\my-repo"
                  aria-label="Folder to open in this session"
                  className="min-w-0 flex-1 rounded-md border border-glass-border bg-black/40 px-2 py-1.5 text-xs text-zinc-100 outline-none transition-colors focus:border-white/20"
                />
                <button type="button" onClick={() => void addFolder()}
                        className="rounded-md bg-zinc-100 px-3.5 py-1.5 text-xs font-bold text-[#161616] transition-colors hover:brightness-110">
                  Open
                </button>
              </div>
            )}
            <>
            {drawer === 'explorer' && (
              <div className="flex min-h-0 flex-1">
                <div className="flex min-h-0 flex-col"
                     style={{ width: previewPath ? `${explorerSplit.value}%` : '100%' }}>
                  <Explorer sid={sid} roots={roots}
                            onOpen={(p) => setPreviewPath(p)}
                            activePath={previewPath || active}
                            pending={pending}
                            onRenamed={(m) => {
                              setStatus(m);
                              void loadPending();
                              void loadCheckpoints();
                              if (active) void refreshReview(active);
                            }} />
                </div>
                {previewPath && (
                  <>
                    <Splitter split={explorerSplit} label="Explorer and preview" />
                    <div className="flex min-h-0 flex-1 flex-col">
                    <div className="flex flex-none items-center gap-2 border-b border-glass-border px-2 py-1">
                      <span className="min-w-0 flex-1 truncate font-mono text-[12px] not-italic text-zinc-400"
                            title={previewPath}>{previewPath}</span>
                      {previewPath.toLowerCase().endsWith('.md') && (
                        <button type="button" onClick={() => setPreviewMd((v) => !v)}
                                title={previewMd ? 'Show source' : 'Render Markdown'}
                                aria-label={previewMd ? 'Show source' : 'Render Markdown'}
                                className="flex h-5 w-5 flex-none items-center justify-center rounded text-zinc-400 hover:bg-white/5 hover:text-zinc-100">
                          {previewMd ? <Code2 size={12} /> : <Eye size={12} />}
                        </button>
                      )}
                      <button type="button" aria-label="Close preview" title="Close preview"
                              onClick={() => setPreviewPath('')}
                              className="flex h-5 w-5 flex-none items-center justify-center rounded text-zinc-500 hover:bg-white/5 hover:text-zinc-200">
                        <X size={12} />
                      </button>
                    </div>
                    {previewText ? (
                      previewMd && previewPath.toLowerCase().endsWith('.md') ? (
                        <div className="prose-chat min-h-0 flex-1 overflow-auto px-4 py-3 text-[13px] leading-relaxed text-zinc-200"
                             dangerouslySetInnerHTML={{ __html: renderMarkdown(previewText, 0, false, true) }} />
                      ) : (
                        <div className="relative min-h-0 flex-1">
                          <MonacoEditor body={previewText} lang={langFor(previewPath)}
                                        path={previewPath}
                                        ariaLabel={`Preview of ${previewPath}`}
                                        dense
                                        onChange={() => {}} onCursor={() => {}}
                                        onHandle={() => {}} />
                        </div>
                      )
                    ) : (
                      <p className="p-3 text-[12px] text-zinc-500">loading…</p>
                    )}
                    </div>
                  </>
                )}
              </div>
            )}
            {drawer === 'search' && (
              <SearchReplace sid={sid} roots={roots}
                             onChanged={() => { void loadPending(); }}
                             onOpenPath={(p, line) => {
                void openPath(p, true, true);
                if (line) setTimeout(() => edRef.current?.revealLine(line), 250);
              }} />
            )}
            {drawer === 'git' && (
              <GitPanel sid={sid} roots={roots}
                        onChanged={() => { void loadPending(); void refreshReview(active); }} />
            )}
            {drawer === 'browser' && (
              <BrowserPane sid={sid}
                           onFocusChange={(running) => setBrowserLive(running)} />
            )}
            {drawer === 'runs' && (
              <RunsPanel sid={sid} runs={runs.runs} live={runs.live} />
            )}
            {drawer === 'changes' && (
              <ChangesList pending={pending} busy={busyHunk}
                           onReload={() => { void loadPending(); void loadCheckpoints(); }}
                           onOpen={(p) => void openPath(p, true, true)}
                           onApply={applyHunk}
                           onKeepSet={(ps) => void keepSet(ps)}
                           onRevertSet={(ps) => void revertSet(ps)}
                           checkpoints={checkpoints}
                           onRestore={(id, name) => void restoreCheckpoint(id, name)} />
            )}
            {drawer === 'terminal' && (
              <div className="flex min-h-0 flex-1 flex-col">
                <Dock sid={sid} roots={roots} tab={dockTab} setTab={setDockTab}
                      open={true}
                      setOpen={(v) => { if (!v) closePanelTab('terminal'); }}
                      buffers={buffers} runs={runs.runs}
                      height={dockHeight} setHeight={setDockHeight}
                      side={false}
                      onOpenPath={(p) => void openPath(p, true, true)} />
              </div>
            )}
            {drawer === 'context' && (
              <div className="flex min-h-0 flex-1 flex-col">
                <div className="flex-none border-b border-glass-border px-4 pb-3 pt-3">
                  {/* The grid below is a wall of numbers of equal weight, and
                      the one a person came for — how full is the window — was
                      a bare "0%". One bar says it at a glance. */}
                  <div className="flex items-baseline gap-2">
                    <span className="text-[10px] font-medium uppercase tracking-[0.14em] text-zinc-muted">
                      Context
                    </span>
                    <span className="text-[12px] font-medium tabular-nums text-zinc-200">
                      {ctx.cap ? `${ctxPct}%` : 'not measured yet'}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-muted">
                      {ctx.cap
                        ? `~${ctxTokens.toLocaleString()} of ~${ctxCapTokens.toLocaleString()} tokens`
                        : 'send the agent a message and the window fills in'}
                    </span>
                  </div>
                  <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-white/[0.06]">
                    <div className={'h-full rounded-full transition-[width] duration-300 '
                      + (ctxPct >= 85 ? 'bg-danger' : ctxPct >= 60 ? 'bg-warn' : 'bg-accent')}
                      style={{ width: `${Math.max(ctx.cap ? 1 : 0, Math.min(100, ctxPct))}%` }} />
                  </div>
                  <p className="mt-1.5 text-[10.5px] text-zinc-muted">
                    {runs.runs[0]?.input_tokens
                      ? `${Number(runs.runs[0].input_tokens).toLocaleString()} in · `
                        + `${Number(runs.runs[0].output_tokens || 0).toLocaleString()} out on the newest run`
                      : 'no completed run yet'}
                  </p>
                </div>
                <div className="grid flex-none grid-cols-2 gap-x-6 gap-y-3 border-b border-glass-border px-4 py-3">
                  {([
                    ['Session', runs.runs[0]?.question || '—'],
                    ['Messages', String(runs.runs[0]?.frames || 0)],
                    ['Provider', runs.runs[0]?.provider || '—'],
                    ['Model', runs.runs[0]?.model || model || '—'],
                    ['Context Limit', ctx.cap ? `~${ctxCapTokens.toLocaleString()}` : '—'],
                    ['Total Tokens', ctxTokens ? `~${ctxTokens.toLocaleString()}` : '—'],
                    ['Usage', ctx.cap ? `${ctxPct}%` : '—'],
                    ['Input Tokens', String(runs.runs[0]?.input_tokens || 0)],
                    ['Output Tokens', String(runs.runs[0]?.output_tokens || 0)],
                    ['Tool Calls', String(runs.runs[0]?.tool_calls || 0)],
                  ] as [string, string][]).map(([k, v]) => (
                    <div key={k} className="min-w-0">
                      <p className="text-[10px] font-medium uppercase tracking-[0.14em] text-zinc-muted">{k}</p>
                      <p className="truncate text-[12px] text-zinc-200" title={v}>{v}</p>
                    </div>
                  ))}
                </div>
                <RunsPanel sid={sid} runs={runs.runs} live={runs.live} />
              </div>
            )}
            </>
          </aside>
        </div>
        {/* Session facts live in the Context tab now (runs, frames, tokens,
            cost); the old always-open session panel is gone. */}


      </div>

      {/* The panel palette: every panel the workbench can show. It used to list
          three entries (Browser / Terminal / Open file) while the only way to
          reach Search, Changes, Git or Runs was the icon rail — which renders
          ONLY when the session column is collapsed. So most of the studio was
          unreachable at normal width, and `drawer === 'search'` had no caller
          at all. */}
      {panelMenu && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/55 backdrop-blur-[3px]"
             onPointerDown={(e) => { if (e.target === e.currentTarget) setPanelMenu(false); }}>
          <div role="dialog" aria-modal="true" aria-label="Open a panel"
               className="w-[460px] max-w-[92vw] rounded-xl border border-glass-border bg-surface-1 p-2 shadow-2xl">
            {([
              { id: 'explorer' as PanelId, label: 'Files', key: 'CTRL ALT P', icon: <FolderTree size={15} /> },
              { id: 'search' as PanelId, label: 'Search', key: '', icon: <Search size={15} /> },
              { id: 'changes' as PanelId, label: 'Changes', key: '', icon: <ShieldAlert size={15} /> },
              { id: 'git' as PanelId, label: 'Git', key: '', icon: <GitBranch size={15} /> },
              { id: 'runs' as PanelId, label: 'Runs', key: '', icon: <ScrollText size={15} /> },
              { id: 'browser' as PanelId, label: 'Browser', key: 'CTRL ALT B', icon: <Globe size={15} /> },
              { id: 'terminal' as PanelId, label: 'Terminal', key: 'CTRL ALT T', icon: <Terminal size={15} /> },
              { id: 'context' as PanelId, label: 'Context', key: '', icon: <PanelRightOpen size={15} /> },
            ]).map((a) => (
              <button key={a.id} type="button"
                      onClick={() => { openPanel(a.id); setPanelMenu(false); }}
                      aria-pressed={drawer === a.id}
                      className={'flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-left text-[13px] transition-colors hover:bg-white/5 '
                        + (drawer === a.id ? 'text-zinc-100' : 'text-zinc-300')}>
                <span className="text-zinc-500">{a.icon}</span>
                <span className="flex-1">{a.label}</span>
                {a.key && (
                  <kbd className="rounded border border-glass-border px-1.5 py-0.5 font-mono text-[10px] text-zinc-500">{a.key}</kbd>
                )}
              </button>
            ))}
          </div>
        </div>
      )}

      <FolderPicker open={pickerOpen}
                    onClose={() => setPickerOpen(false)}
                    onPick={(p) => void pickFolder(p)} />

      {/* The footer is gone, but its status line was the only announcement of
          what the shell is doing. Screen readers still get it. */}
      <p role="status" aria-live="polite" className="sr-only">{status}</p>
    </div>
  );
}

/** "just now / 4m / 2h / 3d" for the checkpoint rows. A list of undated
 *  "before task N" entries cannot be read oldest-to-newest by eye, which is
 *  the only thing that makes an undo history usable. */
function agoShort(ts?: number): string {
  if (!ts) return '';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

/** The unreviewed-change list, and the undo history beside it.
 *
 *  Deliberately a component fed by ONE fetch rather than a second caller of
 *  the same route: the studio and the old page must show the same truth from
 *  the same model, or the sidebar and the editor disagree about whether work
 *  is pending — which is the kind of thing a user reads as the agent having
 *  done nothing.
 *
 *  The rows are hunks, not files, because a hunk is what can be decided. A
 *  file-level list offers "keep this file", which forces a yes on work the
 *  user has only half read. */
function ChangesList({ pending, busy, checkpoints, onReload, onOpen, onApply,
                       onKeepSet, onRevertSet, onRestore }: {
  pending: Record<string, StudioChange>;
  busy: string;
  checkpoints: StudioCheckpoint[];
  onReload: () => void;
  onOpen: (p: string) => void;
  onApply: (p: string, id: string, action: 'keep' | 'revert') => void;
  /** Change-set verbs: whole tasks, not whole files. */
  onKeepSet: (paths: string[]) => void;
  onRevertSet: (paths: string[]) => void;
  onRestore: (id: string, name: string) => void;
}) {
  const rows = Object.values(pending).sort((a, b) => a.path.localeCompare(b.path));
  const [confirm, setConfirm] = useState('');
  const [showAllCp, setShowAllCp] = useState(false);
  const totalHunks = rows.reduce((n, r) => n + r.hunks.length, 0);

  /* One change set = every file a single task left pending. The grouping rule
     and its ordering live in `timeline.ts`, where it is tested; this component
     only draws them. */
  const groups = useMemo(() => groupChanges(rows, checkpoints), [rows, checkpoints]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-none items-center gap-1 px-2.5 py-2">
        <Label className="min-w-0 flex-1 truncate">
          {rows.length
            ? `${groups.length} change${groups.length === 1 ? '' : 's'} · `
              + `${rows.length} file${rows.length === 1 ? '' : 's'} · ${totalHunks} hunk`
            : 'Unreviewed changes'}
        </Label>
        <button type="button" onClick={onReload}
                aria-label="Re-read the unreviewed changes"
                title="Re-read the unreviewed changes"
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-100">
          <RefreshCw size={12} />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-1.5 pb-2">
        {!rows.length && (
          <p className="px-2 py-1.5 text-[11px] leading-relaxed text-zinc-muted">
            Nothing pending. Agent writes appear here until you keep or revert
            each hunk.
          </p>
        )}
        {groups.map((g) => (
          <div key={g.key} className="mb-2.5">
            <div className="flex items-center gap-2 px-1 pb-1">
              <span className="min-w-0 flex-1 truncate text-[11px] font-medium text-zinc-300"
                    title={g.name}>{g.name}</span>
              {(g.added + g.removed) > 0 && (
                <span className="flex-none font-mono text-[10.5px] tabular-nums">
                  {!!g.added && <span className="text-ok">+{g.added}</span>}
                  {!!g.removed && <span className="text-danger"> −{g.removed}</span>}
                </span>
              )}
              <button type="button" disabled={!!busy}
                      onClick={() => onKeepSet(g.files.map((f) => f.path))}
                      title={`keep all ${g.files.length} file(s) in this change`}
                      className="flex-none rounded-md border border-ok/25 px-2 py-[3px] text-[10.5px] font-semibold text-ok/80 transition-colors hover:border-ok/50 hover:bg-ok/15 hover:text-ok disabled:opacity-40">
                keep all
              </button>
              <button type="button" disabled={!!busy}
                      onClick={() => onRevertSet(g.files.map((f) => f.path))}
                      title={`undo all ${g.files.length} file(s) in this change`}
                      className="flex-none rounded-md border border-glass-border px-2 py-[3px] text-[10.5px] font-semibold text-zinc-400 transition-colors hover:bg-white/5 hover:text-zinc-100 disabled:opacity-40">
                undo all
              </button>
            </div>
            <ChangeCard r={g.files[0]} busy={busy}
                        onOpen={onOpen} onApply={onApply} />
            {g.files.slice(1).map((r) => (
              <div key={r.path} className="mt-1.5">
                <ChangeCard r={r} busy={busy} onOpen={onOpen} onApply={onApply} />
              </div>
            ))}
          </div>
        ))}

        {!!checkpoints.length && (
          <div className="mt-3 border-t border-glass-border pt-2">
            <div className="flex items-center gap-1.5 px-2.5 pb-1.5">
              <Label className="min-w-0 flex-1">Checkpoints</Label>
              <span className="flex-none rounded-full bg-white/[0.06] px-1.5 py-px text-[10px] font-semibold tabular-nums text-zinc-400">
                {checkpoints.length}
              </span>
            </div>
            {/* The list used to render every checkpoint the session had ever
                made — twenty-odd undated "before task N" rows, all identical,
                with no verb on them. A person scrolling that cannot tell the
                recent from the ancient or see that clicking one restores
                anything, so it caps at eight and says which is which. */}
            {(showAllCp ? checkpoints : checkpoints.slice(0, 8)).map((c) => (
              <div key={c.id} className="px-2 py-0.5">
                {confirm === c.id ? (
                  <div className="flex flex-wrap items-center gap-1 rounded-md border border-warn/30 bg-warn-dim px-1.5 py-1 text-[11px] text-warn">
                    <span className="min-w-0 flex-1">
                      restore {c.files} file(s) to “{c.name}”? This overwrites
                      what is there now — and captures it as a checkpoint first.
                    </span>
                    <button type="button"
                            onClick={() => { setConfirm(''); onRestore(c.id, c.name); }}
                            className="rounded-md border border-glass-border px-1.5 py-0.5 hover:bg-white/5">
                      restore
                    </button>
                    <button type="button" onClick={() => setConfirm('')}
                            className="rounded-md px-1.5 py-0.5 hover:bg-white/5">
                      cancel
                    </button>
                  </div>
                ) : (
                  <button type="button"
                          onClick={() => setConfirm(c.id)}
                          title={`${c.bytes} bytes stored · turn ${c.turn}`}
                          className="group/cp flex w-full items-center gap-1.5 rounded px-1 py-1 text-left hover:bg-white/5">
                    <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-300">
                      {c.name}
                    </span>
                    <span className="flex-none tabular-nums text-[11px] text-zinc-muted">
                      {c.files} file{c.files === 1 ? '' : 's'} · {agoShort(c.when)}
                    </span>
                    <span className="flex-none rounded border border-glass-border px-1 text-[11px] text-zinc-muted opacity-0 group-hover/cp:opacity-100 group-focus-visible:opacity-100">
                      restore
                    </span>
                  </button>
                )}
              </div>
            ))}
            {checkpoints.length > 8 && (
              <button type="button" onClick={() => setShowAllCp((v) => !v)}
                      className="mt-1 w-full rounded px-2 py-1 text-left text-[11px] text-zinc-muted hover:bg-white/5 hover:text-zinc-200">
                {showAllCp ? 'show fewer'
                  : `show ${checkpoints.length - 8} older`}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** One pending file inside a change set. Its own hunks keep their own
 *  keep/undo; the set around it owns the verdict on the whole task. */
function ChangeCard({ r, busy, onOpen, onApply }: {
  r: StudioChange;
  busy: string;
  onOpen: (p: string) => void;
  onApply: (p: string, id: string, action: 'keep' | 'revert') => void;
}) {
  return (
    <div className="overflow-hidden rounded-[10px] border border-glass-border bg-surface-1">
      <button type="button" onClick={() => onOpen(r.path)}
              className="flex w-full items-center gap-1.5 px-2.5 py-2 text-left text-[12.5px] font-medium text-zinc-100 transition-colors hover:bg-white/[0.04]"
              title={`${r.summary || ''}\n${r.path}`}>
        <span className="min-w-0 flex-1 truncate">{r.path}</span>
        {r.created && (
          <Pill tone="ok">new</Pill>
        )}
        {/* "+0 −0" is not a fact about a change, it is a fact about the
            counter: an entry whose two numbers are both zero says the
            bytes never moved, and showing it as a clean diff makes the
            panel look broken rather than telling the truth. */}
        {(r.added ?? 0) + (r.removed ?? 0) > 0 ? (
          <span className="flex flex-none items-center gap-1.5 font-mono text-[11px] tabular-nums">
            {!!r.added && <span className="text-ok">+{r.added}</span>}
            {!!r.removed && <span className="text-danger">−{r.removed}</span>}
          </span>
        ) : (
          <span className="flex-none text-[11px] text-zinc-muted">no line change</span>
        )}
      </button>
      <ul className="divide-y divide-edge/70 px-1.5 pb-1">
        {r.hunks.map((h) => (
          <li key={h.id}
              className="group/hunk flex items-center gap-2 rounded-md px-1 py-1.5 text-[11px] text-zinc-muted">
            <span className="min-w-0 flex-1 truncate"
                  title={`${h.header}\n${h.id}`}>
              {/* A pure deletion has no position in the NEW file, so its
                  `new_start` is 0 and the row read "L0" — a line that
                  does not exist. Fall back to where it was. */}
              {h.new_start ? `L${h.new_start}` : `was L${h.old_start}`}
              <span className="text-ok"> +{h.added}</span>
              <span className="text-danger"> −{h.removed}</span>
            </span>
            {/* The verdict sits at the end of the row, quiet until you reach
                for it. Two saturated buttons per hunk made a list of eight
                hunks read as sixteen controls and no content. */}
            <button type="button" disabled={!!busy}
                    onClick={() => onApply(r.path, h.id, 'keep')}
                    title="keep this hunk"
                    className="shrink-0 rounded-md border border-ok/25 px-2 py-[3px] text-[10.5px] font-semibold text-ok/80 transition-colors hover:border-ok/50 hover:bg-ok/15 hover:text-ok disabled:opacity-40">
              keep
            </button>
            <button type="button" disabled={!!busy}
                    onClick={() => onApply(r.path, h.id, 'revert')}
                    title="undo this hunk"
                    className="shrink-0 rounded-md border border-glass-border px-2 py-[3px] text-[10.5px] font-semibold text-zinc-400 transition-colors hover:bg-white/5 hover:text-zinc-100 disabled:opacity-40">
              undo
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** `/studio` when the flag is off is the ordinary Code page, not a shell with
 *  nothing in it, and not a blank screen.
 *
 *  The alternative — refuse the route — gives a flag that cannot be turned on
 *  to look at, because turning it off in the Apps board would 404 a tab people
 *  already have bookmarked. Falling through to the page it replaced keeps the
 *  switch one edit in either direction, which is what a kill switch is for. */
export default function Studio() {
  const { on, loading } = useCodeIdeFlag();
  /* The session id lives here, one level above the shell, because switching
     sessions is a navigation the shell itself cannot perform honestly: a
     studio session owns buffers, an agent transcript and a set of folders, and
     re-using the same component for a different one carries all three across —
     which is the class of silent cross-project write this surface exists to
     stop. Keying the shell on the id makes the switch a remount. */
  const [sid, setSid] = useState(() => studioSid());
  if (loading) {
    return (
      <div className="h-full flex-1 overflow-auto p-4">
        <Skel n={3} />
      </div>
    );
  }
  if (!on) {
    return (
      <Suspense fallback={<div className="p-4 text-[12px] text-zinc-muted">loading…</div>}>
        <CodePage />
      </Suspense>
    );
  }
  return (
    <StudioShell key={sid} sid={sid}
                 onSwitch={(next) => {
                   if (!setStoredStudioSid(next)) return;
                   setSid(next);
                 }} />
  );
}
