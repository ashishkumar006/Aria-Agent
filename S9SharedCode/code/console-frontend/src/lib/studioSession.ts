import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type StudioRoot, type StudioRun, type StudioSession, type StudioWorkspace } from '../api';

/** The studio's own session identity, and the paths that go with it.
 *
 *  WHY A SESSION ID EXISTS AT ALL: the code workspace used to be two module
 *  globals on the server, so opening a folder in one tab changed what another
 *  tab's editor saved into — and doing it while an agent was running re-rooted
 *  that agent's next tool call into a different project. Both failures are
 *  silent: nothing errors, the file simply lands somewhere else. A studio
 *  session is the thing that owns folders now, so the id has to be minted
 *  before the first request and stay stable across reloads.
 *
 *  It is kept in `localStorage` rather than in the URL because it is a
 *  workspace, not a document: a shared or bookmarked link must not hand someone
 *  else an id that resolves to folders on this machine.
 */
const SID_KEY = 'aria.studio.sid';

/** The shape `studio_workspace.SID_RE` accepts, kept in step with it on purpose:
 *  a client that could mint an id the server rejects would show an empty
 *  workspace with no reason attached. */
export function newStudioSid(): string {
  const rand = (() => {
    try {
      const b = new Uint8Array(4);
      crypto.getRandomValues(b);
      return Array.from(b).map((x) => x.toString(16).padStart(2, '0')).join('');
    } catch {
      return Math.floor(Math.random() * 0xffffffff).toString(16).padStart(8, '0');
    }
  })();
  return `st-${rand}`;
}

export function studioSid(): string {
  let sid = '';
  try { sid = localStorage.getItem(SID_KEY) || ''; } catch { /* private mode */ }
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(sid)) {
    sid = newStudioSid();
    try { localStorage.setItem(SID_KEY, sid); } catch { /* keep it in memory */ }
  }
  return sid;
}

/** Point this browser at a different session.
 *
 *  The caller remounts the shell with the new id rather than re-using it: a
 *  studio session owns buffers, an agent transcript and a set of folders, and
 *  carrying any of those across the switch is the cross-project bug this whole
 *  surface was built to remove. */
export function setStoredStudioSid(sid: string): boolean {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(sid)) return false;
  try { localStorage.setItem(SID_KEY, sid); } catch { /* private mode */ }
  return true;
}

/** Paths in this surface are BASE-relative with the folder name baked in —
 *  "proj-a/src/app.ts" — because that is what `/api/code/*` resolves and what
 *  the server's own `_code_client_path` produces. Two helpers keep the
 *  split/join on one rule instead of in six call sites. */
export const rootNameOf = (path: string): string => {
  const p = (path || '').replace(/\\/g, '/').replace(/^\/+/, '');
  const i = p.indexOf('/');
  return i < 0 ? p : p.slice(0, i);
};

export const relWithinRoot = (path: string, name: string): string => {
  const p = (path || '').replace(/\\/g, '/').replace(/^\/+/, '');
  return p === name ? '' : (p.startsWith(name + '/') ? p.slice(name.length + 1) : p);
};

export const joinPath = (root: string, rel: string): string =>
  [root.replace(/\/+$/, ''), rel.replace(/^\/+/, '')].filter(Boolean).join('/');

export const baseName = (p: string): string => (p || '').split('/').pop() || p;

/** Monaco's language id for a path. Driven by the extension and nothing else:
 *  the server sends a `language` field that is derived from the same thing, and
 *  a buffer opened before that response lands (or after it fails) still has to
 *  highlight correctly rather than fall back to plain text at random. */
const LANGS: Record<string, string> = {
  py: 'python', ts: 'typescript', tsx: 'typescript', js: 'javascript',
  jsx: 'javascript', mjs: 'javascript', cjs: 'javascript', json: 'json',
  md: 'markdown', html: 'html', css: 'css', sh: 'shell', yml: 'yaml',
  yaml: 'yaml', toml: 'toml', txt: 'plaintext', sql: 'sql', rs: 'rust',
  go: 'go', java: 'java', c: 'c', h: 'c', cpp: 'cpp',
};

export const langFor = (path: string): string => {
  const ext = baseName(path).toLowerCase().split('.').pop() || '';
  return LANGS[ext] || 'plaintext';
};

export interface StudioState {
  ws: StudioWorkspace | null;
  /** null while loading; false when the session holds nothing. */
  ready: boolean;
  busy: boolean;
  error: string;
  refresh: () => Promise<void>;
  addFolder: (path: string) => Promise<string>;
  removeFolder: (path: string) => Promise<string>;
  makePlayground: () => Promise<string>;
  resetFolders: () => Promise<string>;
}

export function useStudioWorkspace(sid: string): StudioState {
  const [ws, setWs] = useState<StudioWorkspace | null>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    try {
      const next = await api.studioWorkspace(sid);
      setWs(next || null);
      setError(next?.error || '');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setReady(true);
    }
  }, [sid]);

  useEffect(() => { setReady(false); void refresh(); }, [refresh]);

  const mutate = useCallback(async (
    fn: () => Promise<StudioWorkspace | undefined>,
  ): Promise<string> => {
    setBusy(true);
    try {
      const next = await fn();
      if (next?.error) { setError(next.error); return next.error; }
      if (next) setWs(next);
      setError('');
      return '';
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      return msg;
    } finally {
      setBusy(false);
    }
  }, []);

  return {
    ws, ready, busy, error, refresh,
    addFolder: (path) => mutate(() => api.studioRootAdd(sid, path)),
    removeFolder: (path) => mutate(() => api.studioRootRemove(sid, path)),
    makePlayground: () => mutate(() => api.studioPlayground(sid)),
    resetFolders: () => mutate(() => api.studioReset(sid)),
  };
}

/** Whether a path belongs to a declared root. The explorer refuses to descend
 *  into anything else, and the editor refuses to open it — but the check is
 *  deliberately NOT the security boundary. `_code_abs` on the server is, and a
 *  client-side filter that was also the boundary would be a bug waiting for a
 *  hand-typed URL. */
export function isUnderRoot(path: string, roots: StudioRoot[]): boolean {
  const name = rootNameOf(path);
  return roots.some((r) => r.name === name);
}

/** The session list the studio's left column shows.
 *
 *  Read once per mount and re-read after anything that names a session (a new
 *  folder, a first task, an archive). A list that only updates on reload is
 *  how a session you just started becomes invisible in the very panel whose
 *  job is to show it. */
export function useStudioSessions(sid: string): {
  sessions: StudioSession[];
  loading: boolean;
  error: string;
  reload: () => Promise<void>;
  rename: (label: string) => Promise<void>;
  archive: (target: string, archived: boolean) => Promise<void>;
} {
  const [sessions, setSessions] = useState<StudioSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const reload = useCallback(async () => {
    try {
      const got = await api.studioSessions(sid);
      setSessions(got?.sessions || []);
      setError(got?.error || '');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [sid]);

  useEffect(() => { void reload(); }, [reload]);

  const rename = useCallback(async (label: string) => {
    try { await api.studioSessionRename(sid, label); } catch { /* the list
      simply keeps the name it had; the next reload picks up the truth */ }
    void reload();
  }, [sid, reload]);

  const archive = useCallback(async (target: string, archived: boolean) => {
    try { await api.studioSessionArchive(target, archived); } catch { /* as above */ }
    void reload();
  }, [reload]);

  return { sessions, loading, error, reload, rename, archive };
}

/** The session's runs, and the watcher that keeps them current.
 *
 *  A run is an object on the server now (slice 5), so the panel can list
 *  agents, stop one by id, and replay one after a reload. `watch` holds an
 *  open stream while anything is live and stops itself when nothing is — the
 *  alternative is a timer per tab polling a list that is usually empty. */
export function useStudioRuns(sid: string): {
  runs: StudioRun[];
  live: number;
  waiting: string[];
  reload: () => Promise<void>;
  watch: () => void;
  stop: (id: string) => Promise<string>;
} {
  const [runs, setRuns] = useState<StudioRun[]>([]);
  const [live, setLive] = useState(0);
  const [waiting, setWaiting] = useState<string[]>([]);
  const ctrl = useRef<AbortController | null>(null);

  const reload = useCallback(async () => {
    try {
      const got = await api.studioRuns(sid);
      setRuns(got?.runs || []);
      setLive(got?.live ?? 0);
      setWaiting(got?.waiting || []);
    } catch { /* the list keeps what it had; a failed read is not an empty one */ }
  }, [sid]);

  const watch = useCallback(() => {
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    (async () => {
      /* The server ends the window the moment nothing is live, and the panel
         opens it at the same instant it POSTs the turn — so the first poll can
         legitimately see zero and close. That is how `⚑ 2 agents` never
         appeared: tab A's stream died before tab B's run existed, and nothing
         reopened it. Re-open a few times while the count is still zero, then
         stop; a tab that is not running anything costs a short request. */
      for (let attempt = 0; attempt < 4; attempt++) {
        let sawLive = false;
        try {
          for await (const s of api.studioLive(sid, c.signal)) {
            const n = Number(s.live || 0);
            if (n > 0) sawLive = true;
            setLive(n);
            setRuns((s.runs || []) as unknown as StudioRun[]);
          }
        } catch { /* closed, or the server ended the window */ }
        if (c.signal.aborted || sawLive) return;
        await new Promise((r) => setTimeout(r, 1200));
      }
    })();
  }, [sid]);

  useEffect(() => {
    void reload();
    return () => { ctrl.current?.abort(); };
  }, [reload]);

  const stop = useCallback(async (id: string) => {
    const r = await api.studioRunStop(sid, id);
    void reload();
    return r?.error || '';
  }, [sid, reload]);

  return { runs, live, waiting, reload, watch, stop };
}

/** `ui.code_ide`, read once per mount. One flag decides both halves of the
 *  surface — `/studio` renders the studio, `/code` redirects into it — so the
 *  two views cannot disagree about which mode the app is in.
 *
 *  Off is the default and off is the OLD page, not a stripped one: the studio
 *  changes where a file is written, and a half-rendered version of that is
 *  worse than the page that exists today. */
export function useCodeIdeFlag(): { on: boolean; loading: boolean } {
  const [on, setOn] = useState(false);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const r = await api.flags();
        const hit = (r?.flags || []).find((f) => f.name === 'ui.code_ide');
        if (live) setOn(!!hit?.value);
      } catch {
        /* A flag read that fails stays OFF. Defaulting to the new surface on a
           failed read would put people in a workspace they did not choose. */
        if (live) setOn(false);
      } finally {
        if (live) setLoading(false);
      }
    })();
    return () => { live = false; };
  }, []);
  return { on, loading };
}
