import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowUpRight, Bot, Code2, FileText, FolderOpen, FolderPlus, FolderTree, GitBranch, Globe, RefreshCw, Search, ShieldAlert, Sparkles, TriangleAlert, X } from 'lucide-react';
import MonacoEditor, { type EditorHandle } from '../../components/MonacoEditor';
import { api } from '../../api';
import type { StudioChange, StudioCheckpoint, StudioHunk } from '../../api';
import { Splitter, useSplit } from '../../lib/useSplit';
import { baseName as BASENAME, isUnderRoot, langFor, newStudioSid, setStoredStudioSid, studioSid, useCodeIdeFlag, useStudioRuns, useStudioSessions, useStudioWorkspace } from '../../lib/studioSession';
import { Explorer } from './Explorer';
import { AgentPanel } from './AgentPanel';
import { Sessions } from './Sessions';
import ReviewEditor, { type ReviewHandle } from './ReviewEditor';
import { Dock } from './Dock';
import { SearchReplace } from './SearchReplace';
import { GitPanel } from './GitPanel';
import { BrowserPane } from './BrowserPane';
import { Label } from './chrome';
import { Empty, Pill, Skel } from '../../components/ui';

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
     reading a file. */
  const [drawer, setDrawer] = useState<'explorer' | 'search' | 'changes' | 'git' | 'browser' | ''>('explorer');
  /* The dock keeps its own open/tab/height state rather than borrowing the
     drawer's: collapsing a terminal must not lose its scrollback, and the
     height is a person's choice that outlives any one pane. */
  const [dockTab, setDockTab] = useState<'terminal' | 'problems' | 'output' | 'checks'>('terminal');
  /* Whether this session holds a Chromium. In the status bar, not hidden in
     the pane: a browser is a process with a network stack and a cookie jar,
     and "is something of mine still running" should be answerable at a
     glance. */
  const [browserLive, setBrowserLive] = useState(false);
  const [dockOpen, setDockOpen] = useState(false);
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
  const [status, setStatus] = useState('');
  const [adding, setAdding] = useState(false);
  const [folderPath, setFolderPath] = useState('');
  const [cursor, setCursor] = useState({ line: 1, col: 1 });
  const [confirmDiscard, setConfirmDiscard] = useState('');
  /* The session's unreviewed changes by path: ONE fetch, shared by the
     Changes panel and by the decision of whether opening a file should land in
     review. Two components asking the same question separately is how a
     sidebar and an editor disagree about whether work is pending. */
  const [pending, setPending] = useState<Record<string, StudioChange>>({});
  const [busyHunk, setBusyHunk] = useState('');
  const [checkpoints, setCheckpoints] = useState<StudioCheckpoint[]>([]);
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
    } catch { /* same reasoning as loadPending */ }
  }, [sid]);

  useEffect(() => { void loadPending(); void loadCheckpoints(); },
              [loadPending, loadCheckpoints]);

  /* The workbench's share of the window. It starts near half because the
     conversation and the code are read side by side; the conversation keeps
     the other half, which is what its composer needs to stay readable. */
  const right = useSplit({ storageKey: 'aria.studio.right', default: 48, min: 25, max: 70,
                           liveQuery: '(min-width: 1100px)' });

  const roots = ws.ws?.roots || [];

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
      }
    }
  }, [sid, buffers]);

  const restoreCheckpoint = useCallback(async (id: string, name: string) => {
    const r = await api.studioCheckpointRestore(sid, id);
    if (r && r.ok === false) { setStatus(String(r.error || 'restore refused')); return; }
    const parts = [
      r?.restored?.length ? `restored ${r.restored.length} file(s)` : '',
      r?.skipped?.length ? `${r.skipped.length} outside this session` : '',
      r?.failed?.length ? `${r.failed.length} failed` : '',
    ].filter(Boolean);
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
    const cp = checkpoints[0];
    if (!cp) return 'no checkpoint in this session yet — nothing to undo';
    const r = await api.studioCheckpointRestore(sid, cp.id);
    if (r && r.ok === false) return String(r.error || 'the restore was refused');
    const parts = [
      r?.restored?.length ? `restored ${r.restored.length} file(s)` : '',
      r?.skipped?.length ? `${r.skipped.length} outside this session` : '',
      r?.failed?.length ? `${r.failed.length} failed` : '',
    ].filter(Boolean);
    await pullFromDisk(r?.restored || []);
    void loadPending();
    void loadCheckpoints();
    return `undo “${cp.name}” · ${parts.join(' · ') || 'nothing to do'}`
      + ` · ${r?.undo?.name || 'no undo checkpoint captured'}`;
  }, [sid, checkpoints, pullFromDisk, loadPending, loadCheckpoints]);

  /** `/new` in the composer and the "+ New session" button are the same act,
   *  so they call the same stable function. A fresh arrow here would be an
   *  unstable prop, and an unstable prop into the panel is the render loop
   *  that froze this page on 2026-11-06. */
  const newSession = useCallback(() => {
    const next = newStudioSid();
    if (setStoredStudioSid(next)) onSwitch(next);
  }, [onSwitch]);

  /* Stable so the panel can depend on it without being re-driven by it. */
  const refreshStudio = useCallback(() => {
    void loadPending();
    void loadCheckpoints();
  }, [loadPending, loadCheckpoints]);

  const addFolder = useCallback(async () => {
    const p = folderPath.trim();
    setAdding(false); setFolderPath('');
    if (!p) return;
    const err = await ws.addFolder(p);
    // The server's reason is the useful part: "not a folder", "does not
    // exist", or the state-dir rule that keeps sessions/ out of reach.
    setStatus(err || `opened ${p}`);
  }, [folderPath, ws]);

  const dirty = useMemo(() => tabs.filter((p) => {
    const b = buffers[p];
    return !!b && b.text !== b.base;
  }), [tabs, buffers]);

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
  }, [loadPending, loadCheckpoints]);

  const showChanges = useCallback(() => {
    setShowWorkbench(true);
    setDrawer('changes');
  }, []);

  const addFolderHere = useCallback(() => {
    setShowWorkbench(true);
    setDrawer('explorer');
    setAdding(true);
  }, []);

  const nameFromFirstTask = useCallback((text: string) => {
    void sessions.rename(text.slice(0, 80));
  }, [sessions]);

  const activeBuf = buffers[active];
  const openPaths = useMemo(() => tabs.slice(), [tabs]);
  const pendingCount = useMemo(() => Object.keys(pending).length, [pending]);
  const sessionLabel = useMemo(() => {
    const cur = sessions.sessions.find((s) => s.sid === sid);
    return (cur?.label || '').trim() || 'New session';
  }, [sessions.sessions, sid]);

  return (
    <div className="flex h-full min-h-0 w-full flex-col overflow-hidden bg-surface-0">
      <div className="flex min-h-0 flex-1">
        {/* Sessions own the left column, not files.
            The app Rail is never rendered here, which is what "isolated from
            the sidebar" has to mean to be worth anything — and inside the
            studio the same argument applies one level down: the object you
            manage is the agent working on your code, so that is what the list
            shows. Files live in the workbench, beside the conversation. */}
        <div className="w-[262px] flex-none">
          <Sessions sid={sid} sessions={sessions.sessions}
                    loading={sessions.loading} error={sessions.error}
                    onSwitch={onSwitch}
                    onNew={newSession}
                    onArchive={sessions.archive}
                    /* The same act as the hero's button: the path input lives in
                       the workbench aside, so `adding` alone renders nothing and
                       the menu item looks dead whenever the workbench is hidden. */
                    onAddFolder={addFolderHere}
                    onReload={() => { void sessions.reload(); void ws.refresh(); }} />
        </div>

        {/* The conversation is the surface the studio opens on. */}
        <main className="flex min-h-0 min-w-0 flex-1 flex-col bg-surface-0">
          {/* Every console view opens on a glass bar with its own name in it.
              The studio had no title anywhere in the main column — its
              identity was a 12px line inside the sessions rail — so the first
              thing on screen was a void. The crumb names the session too, and
              the one action that matters after an agent turn (review the
              changes) sits on the right where Layout/search/refresh sit on the
              other pages. */}
          <header className="glass sticky top-0 z-20 flex flex-none items-center gap-2 px-4 py-2.5">
            <span aria-hidden
                  className="flex h-6 w-6 flex-none items-center justify-center rounded-md bg-accent/15 text-accent">
              <Code2 size={13} />
            </span>
            <span className="flex min-w-0 items-center gap-1.5 text-[13px] font-semibold">
              <span className="flex-none text-zinc-200">Studio</span>
              <span className="flex-none text-zinc-600">/</span>
              <span className="max-w-[38vw] truncate text-[12px] font-normal text-zinc-muted sm:max-w-[340px]"
                    title={sessionLabel}>
                {sessionLabel}
              </span>
            </span>
            <span className="ml-auto flex min-w-0 items-center gap-2">
              <a href="/console"
                 className="flex items-center gap-1.5 rounded-lg px-2 py-1 text-[12px] text-zinc-muted transition-colors hover:bg-white/5 hover:text-zinc-100"
                 title="Leave the studio and go back to the rest of Aria">
                Console <ArrowUpRight size={12} aria-hidden />
              </a>
            </span>
          </header>
          <AgentPanel sid={sid} activePath={active}
                      activeText={activeBuf?.text || ''} openFiles={openPaths}
                      onOpenPath={openForReview}
                      onPending={notePending}
                      heroTitle={roots.length
                        ? `What should we work on in ${roots[0].name}?`
                        : 'No folder is open in this session yet'}
                      heroHint={roots.length ? ''
                        : 'The agent reads and edits inside the folders this session holds, and nothing outside them. Open one to give it somewhere to work.'}
                      onChanges={showChanges}
                      roots={roots}
                      onAddFolder={addFolderHere}
                      onFirstTask={nameFromFirstTask}
                      runs={runs.runs}
                      onWatch={runs.watch}
                      onRunStarted={() => { void runs.reload(); }}
                      onNewSession={newSession}
                      onUndo={undoLatest}
                      onRefresh={refreshStudio}
                      onTerminal={() => { setShowWorkbench(true); setDockOpen(true); setDockTab('terminal'); }}
                      disabled={!roots.length} />
        </main>

        {/* The workbench: tabs, editor, diff, file tree, changes. It appears
            beside the conversation when there is something to look at and is
            dismissed when there is not — hiding it keeps every buffer loaded,
            so bringing it back is instant rather than a refetch. */}
        {showWorkbench && (
          <>
            <Splitter split={right} label="Conversation and workbench" />
            <section className="flex min-h-0 flex-col border-l border-glass-border bg-surface-0"
                     style={{ width: right.live ? `${right.value}%` : '52%',
                              maxWidth: 'none' }}>
              <div className="flex flex-none items-center gap-1 border-b border-glass-border bg-surface-1/40 px-2 py-1.5 text-[12px]">
                {/* The label is its own field, not the first word of the
                    tooltip. Deriving it from the description is how this strip
                    rendered "Repository:" and "Browser:" with dangling colons
                    and nothing after them — seen in the screenshot, not in a
                    test. */}
                {([
                  ['explorer', <FolderTree size={12} />, 'Files', 'The folders this session holds'],
                  ['search', <Search size={12} />, 'Search', 'Find and replace across every folder here'],
                  ['changes', <ShieldAlert size={12} />, 'Changes', 'What the agent wrote that you have not reviewed'],
                  ['git', <GitBranch size={12} />, 'Git', 'Stage, commit and branch — nothing leaves this machine'],
                  ['browser', <Globe size={12} />, 'Browser', 'The page the agent is looking at, its log, and take-over'],
                ] as const).map(([id, icon, label, description]) => (
                  <button key={id} type="button" title={description}
                          aria-label={`${label} — ${description}`}
                          aria-pressed={drawer === id}
                          onClick={() => setDrawer(drawer === id ? '' : id)}
                          className={
                            'flex flex-none items-center gap-1.5 rounded-lg px-2 py-1 transition-colors '
                            + (drawer === id
                              ? 'bg-accent/15 text-zinc-50'
                              : 'text-zinc-muted hover:bg-white/5 hover:text-zinc-200')
                          }>
                    <span aria-hidden className={drawer === id ? 'text-accent' : 'opacity-80'}>{icon}</span>
                    <span className="hidden xl:inline">{label}</span>
                    {id === 'changes' && !!pendingCount && (
                      <span className="rounded-full bg-accent/25 px-1.5 py-px text-[10px] font-semibold leading-none text-accent">
                        {pendingCount}
                      </span>
                    )}
                  </button>
                ))}
                <span className="min-w-0 flex-1" />
                <button type="button" onClick={() => setShowWorkbench(false)}
                        aria-label="Hide the workbench"
                        title="Hide — buffers stay open"
                        className="rounded px-1.5 py-1 text-zinc-muted hover:bg-white/5 hover:text-zinc-100">
                  <X size={12} />
                </button>
              </div>

              <div className="flex min-h-0 flex-1">
                {drawer && (
                  <aside className="flex min-h-0 w-[248px] flex-none flex-col border-r border-edge bg-surface-1/40">
                    {/* The console's card head: label on the left, actions on
                        the right, each one an icon with a title. Text buttons
                        here were three words competing for 248px, which is
                        what pushed the label itself to "WORKSP...". */}
                    <div className="flex flex-none items-center gap-1 border-b border-edge px-2.5 py-2">
                      <Label className="flex-1">Workspace</Label>
                      <button type="button" onClick={() => void ws.refresh()}
                              disabled={ws.busy}
                              aria-label="Re-read this session's folders"
                              title="Re-read this session's folders"
                              className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-100 disabled:opacity-40">
                        <RefreshCw size={12} />
                      </button>
                      <button type="button" onClick={() => setAdding((v) => !v)}
                              aria-label="Open a folder in this session"
                              title="Open a folder in this session"
                              className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-100">
                        <FolderPlus size={12} />
                      </button>
                      <button type="button"
                              onClick={async () => { setStatus(await ws.makePlayground() || 'playground created'); }}
                              disabled={ws.busy}
                              aria-label="Create an empty scratch folder for this session"
                              title="Create an empty scratch folder for this session"
                              className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-100 disabled:opacity-40">
                        <Sparkles size={12} />
                      </button>
                    </div>
                    {adding && (
                      <div className="flex flex-none items-center gap-1.5 border-b border-edge px-2.5 py-2">
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
                          className="min-w-0 flex-1 rounded-lg border border-glass-border bg-surface-2 px-2 py-1.5 text-[12px] text-zinc-100 outline-none transition-colors focus:border-accent/60"
                        />
                        <button type="button" onClick={() => void addFolder()}
                                className="rounded-lg bg-accent px-2.5 py-1.5 text-[11px] font-semibold text-black transition-colors hover:bg-accent/90">
                          Open
                        </button>
                      </div>
                    )}
                    {roots.length > 0 && (
                      /* One row for every declared folder, not only when there
                         is more than one: the tree's own "N folders" header
                         said less, and closing the single folder you have open
                         needs the same × as closing the second. */
                      <div className="flex flex-wrap gap-1 border-b border-edge px-2.5 py-2">
                        {roots.map((r) => (
                          <span key={r.path}
                                className="flex items-center gap-1 rounded-md border border-glass-border bg-surface-2 py-0.5 pl-1.5 pr-0.5 text-[11px] text-zinc-300"
                                title={r.path}>
                            {r.name}
                            <button type="button"
                                    aria-label={`Remove ${r.name} from this session`}
                                    onClick={async () => {
                                      setStatus(await ws.removeFolder(r.path) || `closed ${r.name}`);
                                    }}
                                    className="flex h-5 w-5 flex-none items-center justify-center rounded text-zinc-500 transition-colors hover:bg-white/10 hover:text-danger">
                              <X size={11} />
                            </button>
                          </span>
                        ))}
                      </div>
                    )}

                    {drawer === 'explorer' && (
                      <Explorer sid={sid} roots={roots}
                                onOpen={(p) => void openPath(p)}
                                activePath={active}
                                onRenamed={(m) => {
                                  setStatus(m);
                                  void loadPending();
                                  void loadCheckpoints();
                                  if (active) void refreshReview(active);
                                }} />
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
                    {drawer === 'changes' && (
                      <ChangesList pending={pending} busy={busyHunk}
                                   onReload={() => { void loadPending(); void loadCheckpoints(); }}
                                   onOpen={(p) => void openPath(p, true, true)}
                                   onApply={applyHunk}
                                   checkpoints={checkpoints}
                                   onRestore={(id, name) => void restoreCheckpoint(id, name)} />
                    )}
                  </aside>
                )}

                <div className="flex min-h-0 min-w-0 flex-1 flex-col">
                  <div role="tablist" aria-label="Open files"
                       className="flex flex-none items-stretch overflow-x-auto border-b border-glass-border bg-surface-1/40">
            {tabs.map((p) => {
              const b = buffers[p];
              const isDirty = !!b && b.text !== b.base;
              return (
                <div key={p}
                     role="tab" aria-selected={active === p}
                     className={
                       'group flex max-w-[220px] flex-none cursor-pointer items-center gap-1 border-r border-glass-border px-2 py-1.5 text-[12px] '
                       + (active === p
                         ? 'bg-surface-0 text-zinc-100'
                         : 'text-zinc-muted hover:bg-white/5')
                     }>
                  <button type="button" onClick={() => setActive(p)}
                          onDoubleClick={() => setBuffers((bb) => ({ ...bb, [p]: { ...bb[p], preview: false } }))}
                          className="min-w-0 truncate"
                          title={p}
                          aria-label={`Focus ${p}`}>
                    <span className={b?.preview ? 'italic' : ''}>{b?.name || BASENAME(p)}</span>
                    {isDirty && <span className="ml-1 text-accent" title="unsaved">●</span>}
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
                        className="rounded-lg border border-glass-border px-1.5 py-0.5 hover:bg-white/5">
                  discard anyway
                </button>
                <button type="button"
                        onClick={() => { setActive(confirmDiscard); setConfirmDiscard(''); void save(); }}
                        className="rounded-lg border border-glass-border px-1.5 py-0.5 hover:bg-white/5">
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
              <div className="flex flex-none items-center gap-2 border-b border-glass-border bg-surface-1/40 px-2 py-1 text-[11px]">
                <div role="group" aria-label="Editor view"
                     className="flex items-center gap-0.5 rounded-lg border border-glass-border p-0.5">
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
                            className="whitespace-nowrap rounded-lg border border-ok/40 bg-ok-dim px-2 py-0.5 text-ok hover:bg-ok/20">
                      Keep file
                    </button>
                    <button type="button" onClick={() => void revertFile(activeBuf.path)}
                            className="whitespace-nowrap rounded-lg border border-danger/40 bg-danger-dim px-2 py-0.5 text-danger hover:bg-danger/20">
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
                          className="rounded-lg border border-glass-border px-2.5 py-1 text-[12px] text-zinc-200 hover:bg-white/5">
                    Try again
                  </button>
                  <button type="button"
                          onClick={() => void revertFile(activeBuf.path)}
                          className="rounded-lg border border-danger/40 bg-danger-dim px-2.5 py-1 text-[12px] text-danger hover:bg-danger/20">
                    Revert to the checkpoint
                  </button>
                  <button type="button" onClick={() => closeTab(activeBuf.path)}
                          className="rounded-lg px-2.5 py-1 text-[12px] text-zinc-muted hover:bg-white/5 hover:text-zinc-200">
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
                          className="flex items-center gap-1.5 rounded-lg border border-glass-border px-2.5 py-1 text-[12px] text-zinc-300 hover:bg-white/5 hover:text-zinc-100">
                    <FolderOpen size={12} /> Open a file
                  </button>
                ) : (
                  <button type="button"
                          onClick={() => { setShowWorkbench(true); setDrawer('explorer'); }}
                          className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-[12px] font-semibold text-black transition-colors hover:bg-accent/90">
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
              <Dock sid={sid} roots={roots} tab={dockTab} setTab={setDockTab}
                    open={dockOpen} setOpen={setDockOpen} buffers={buffers}
                    runs={runs.runs} height={dockHeight}
                    setHeight={setDockHeight}
                    onOpenPath={(p) => void openPath(p, true, true)} />
            </section>
          </>
        )}
      </div>

      <footer className="glass flex h-[28px] flex-none items-center gap-3 px-3 text-[11px] text-zinc-muted">
        {/* The folder NAMES, not "2 folders · s-s12". The session id is a
            debugging fact and it was the second-most-prominent thing on every
            screen; what a person needs here is "which project am I in". */}
        <span className="flex min-w-0 items-center gap-1.5"
              title={roots.length
                ? roots.map((r) => r.path).join('\n') + `\n\nsession ${sid}`
                : `no folders yet · session ${sid}`}>
          <FolderTree size={11} aria-hidden className="flex-none opacity-70" />
          <span className="min-w-0 truncate">
            {roots.length
              ? roots.slice(0, 3).map((r) => r.name).join(', ')
                + (roots.length > 3 ? ` +${roots.length - 3}` : '')
              : 'no folder'}
          </span>
        </span>
        {/* Agents, not keystrokes. "2 agents" is the count of runs this
            session has alive right now, and the waiting ones are called out
            because a run parked on an approval is not working — a status bar
            that says "2 running" for two blocked agents is a lie. */}
        {runs.live > 0 && (
          <span className="flex flex-none items-center gap-1 text-accent"
                title={runs.waiting.length
                  ? `${runs.waiting.length} waiting for approval`
                  : 'working'}>
            <Bot size={11} aria-hidden /> {runs.live} agent{runs.live === 1 ? '' : 's'}
            {runs.waiting.length ? ` · ${runs.waiting.length} waiting` : ''}
          </span>
        )}
        {browserLive && (
          <button type="button"
                  onClick={() => { setShowWorkbench(true); setDrawer('browser'); }}
                  title="This session holds a Chromium. Stop does not close it."
                  className="flex flex-none items-center gap-1 text-ok hover:text-ok/80">
            <Globe size={11} aria-hidden /> browser open
          </button>
        )}
        {!!pendingCount && (
          <button type="button"
                  onClick={() => { setShowWorkbench(true); setDrawer('changes'); }}
                  className="flex min-w-0 items-center gap-1 truncate text-warn hover:text-warn/80"
                  title="Open the workbench on the unreviewed changes">
            <ShieldAlert size={11} className="flex-none" />
            {pendingCount} unreviewed
          </button>
        )}
        {active && (
          <span className="min-w-0 truncate" title={active}>
            {BASENAME(active)} · Ln {cursor.line}, Col {cursor.col} · {activeBuf?.lang || ''}
            {dirty.includes(active) ? ' · modified' : ''}
            {activeBuf?.mode === 'review'
              ? ` · review ${activeBuf.hunks.length} hunk`
              : ''}
          </span>
        )}
        {dirty.length > 0 && (
          <span className="text-warn">{dirty.length} unsaved</span>
        )}
        <span className="flex min-w-0 flex-1 truncate text-right" title={status}>{status}</span>
        <span className="flex flex-none items-center gap-1">
          {!showWorkbench && (
            <>
              <button type="button"
                      onClick={() => { setShowWorkbench(true); setDrawer('explorer'); }}
                      className="flex items-center gap-1.5 rounded-md px-1.5 py-0.5 transition-colors hover:bg-white/5 hover:text-zinc-100">
                <FolderTree size={12} /> Files
              </button>
              <button type="button"
                      onClick={() => { setShowWorkbench(true); setDrawer('git'); }}
                      className="flex items-center gap-1.5 rounded-md px-1.5 py-0.5 transition-colors hover:bg-white/5 hover:text-zinc-100">
                <GitBranch size={12} /> Git
              </button>
            </>
          )}
        </span>
      </footer>
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
function ChangesList({ pending, busy, checkpoints, onReload, onOpen, onApply, onRestore }: {
  pending: Record<string, StudioChange>;
  busy: string;
  checkpoints: StudioCheckpoint[];
  onReload: () => void;
  onOpen: (p: string) => void;
  onApply: (p: string, id: string, action: 'keep' | 'revert') => void;
  onRestore: (id: string, name: string) => void;
}) {
  const rows = Object.values(pending).sort((a, b) => a.path.localeCompare(b.path));
  const [confirm, setConfirm] = useState('');
  const [showAllCp, setShowAllCp] = useState(false);
  const totalHunks = rows.reduce((n, r) => n + r.hunks.length, 0);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-none items-center gap-1 px-2.5 py-2">
        <Label className="min-w-0 flex-1 truncate">
          {rows.length ? `${rows.length} file · ${totalHunks} hunk` : 'Unreviewed changes'}
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
        {rows.map((r) => (
          <div key={r.path} className="mb-1.5 overflow-hidden rounded-lg border border-edge bg-surface-1">
            <button type="button" onClick={() => onOpen(r.path)}
                    className="flex w-full items-center gap-1.5 px-2.5 py-2 text-left text-[12px] text-zinc-200 transition-colors hover:bg-white/[0.04]"
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
                <span className="flex flex-none items-center gap-1.5 text-[11px] tabular-nums">
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
                    className="flex items-center gap-1.5 rounded-md py-1.5 text-[11px] text-zinc-muted">
                  <span className="min-w-0 flex-1 truncate"
                        title={`${h.header}\n${h.id}`}>
                    {/* A pure deletion has no position in the NEW file, so its
                        `new_start` is 0 and the row read "L0" — a line that
                        does not exist. Fall back to where it was. */}
                    {h.new_start ? `L${h.new_start}` : `was L${h.old_start}`}
                    <span className="text-ok"> +{h.added}</span>
                    <span className="text-danger"> −{h.removed}</span>
                  </span>
                  <button type="button" disabled={!!busy}
                          onClick={() => onApply(r.path, h.id, 'keep')}
                          className="rounded-md border border-ok/30 bg-ok-dim px-1.5 py-0.5 text-[10px] font-semibold text-ok transition-colors hover:bg-ok/20 disabled:opacity-40">
                    keep
                  </button>
                  <button type="button" disabled={!!busy}
                          onClick={() => onApply(r.path, h.id, 'revert')}
                          className="rounded-md border border-glass-border px-1.5 py-0.5 text-[10px] font-semibold text-zinc-300 transition-colors hover:bg-white/5 disabled:opacity-40">
                    undo
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}

        {!!checkpoints.length && (
          <div className="mt-3 border-t border-edge pt-2">
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
                  <div className="flex flex-wrap items-center gap-1 rounded-lg border border-warn/30 bg-warn-dim px-1.5 py-1 text-[11px] text-warn">
                    <span className="min-w-0 flex-1">
                      restore {c.files} file(s) to “{c.name}”? This overwrites
                      what is there now — and captures it as a checkpoint first.
                    </span>
                    <button type="button"
                            onClick={() => { setConfirm(''); onRestore(c.id, c.name); }}
                            className="rounded-lg border border-glass-border px-1.5 py-0.5 hover:bg-white/5">
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
