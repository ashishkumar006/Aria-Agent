import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  FileDown, Loader2, AlertTriangle, Sparkles, LayoutGrid, ScrollText,
  Terminal, Download, History, FileText, Table2, Presentation,
} from 'lucide-react';
import { SkipLink, Rail, TopBar, Pill } from '../components/ui';
import { renderMarkdown } from '../components/markdown';
import { api, headers, ago, displayTopic, type DocPreview, type SessionSummary } from '../api';
import { DocumentViewer } from './DocSetup';

type Kind = 'auto' | 'pdf' | 'pptx' | 'docx' | 'xlsx';
type View = 'preview' | 'receipt' | 'log';

/* The output tabs, in the order the arrows walk them. */
const TABS: [View, string, typeof FileText][] = [
  ['preview', 'Preview', LayoutGrid],
  ['receipt', 'Result', ScrollText],
  ['log', 'Run log', Terminal],
];
const TAB_ORDER = TABS.map(([id]) => id);

const SUGGESTIONS = [
  'A one-page briefing on the placement rules in my uploaded Bluebook',
  'A 10-slide deck introducing our vector-search stack to the team',
  'A spreadsheet comparing IVF, HNSW and flat search on recall, build time and memory',
  "A detailed PDF report on what we learned from this week's research runs",
];

interface MadeFile {
  filename: string;
  format: string;
  artifact: string;
  bytes?: number;
}

/** An authoring run, as the sidebar shows it.
 *
 *  The list is server-backed; localStorage only has to carry an in-flight run
 *  across a reload, so it is not the record of what a run produced. */
interface Run extends SessionSummary {
  /** null until /produced has been read for this run. An unread run is
   *  UNKNOWN, and rendering it as [] claimed "0 files" for a run that was still
   *  executing - which is exactly what a run still executing looks like. */
  files: MadeFile[] | null;
  answer?: string;
  at?: number;
  /** Set while the run is still executing server-side. */
  live?: boolean;
  /** The node rollup from /produced, so a reopened run has a Run log. */
  logs?: string[];
  kind?: Kind;
}

/** What /produced says about one run. */
type Produced = {
  files?: { artifact: string; filename: string; format: string }[];
  answer?: string;
  live?: boolean;
  nodes?: { id: string; skill: string; status: string }[];
};

/** The log of a run that is no longer streaming: the nodes it ran. */
const logLines = (nodes?: Produced['nodes']) =>
  (nodes || []).map((n) => `[${n.id}] ${n.skill} ${n.status}`);

/** Fold a /produced snapshot into its row. One place, so the sidebar count, the
 *  receipt and the file list can never disagree about what a run did. */
const withProduced = (r: Run, snap: Produced): Run => {
  const fs = (snap.files || []).map((f) => ({
    artifact: f.artifact,
    filename: f.filename,
    format: f.format,
  }));
  return {
    ...r,
    files: fs,
    answer: snap.answer || '',
    live: !!snap.live,
    logs: logLines(snap.nodes),
    // What was ACTUALLY produced, not what the run was asked for.
    kind: (fs[0]?.format || r.kind || 'auto') as Kind,
  };
};

const LS_KEY = 'aria.authoring.runs.v1';
const MAX_KEPT = 40;
/* /api/sessions clamps to 500 and every row parses a whole graph.json, so ask
 * for the whole store in one request. A window of 60 returned only the 40
 * newest authoring runs out of 131, which left 91 paid documents unreachable
 * while the badge still claimed a complete list. */
const SESSION_WINDOW = 500;
/* /produced is one graph parse per call, so files are read lazily: the newest
 * rows up front, the rest as the list is scrolled. Fanning all 131 out at once
 * made the sidebar hang and delayed the rows that had already loaded. */
const READ_AHEAD = 12;

/** The locally remembered runs. localStorage is not the source of truth for
 *  the list any more, but it is what carries an in-flight run across a reload,
 *  so it is read before the server answers and written on every change. */
function readLocal(): Run[] {
  try {
    const raw = localStorage.getItem(LS_KEY);
    const runs = raw ? JSON.parse(raw) as Run[] : [];
    /* a row saved by an older build may carry no files array at all: normalise
     * it, so it reads as neither a real empty list nor a crash. */
    return Array.isArray(runs)
      ? runs.map((r) => ({ ...r, files: Array.isArray(r.files) ? r.files : null }))
      : [];
  } catch { return []; }  // private mode: the list simply will not persist
}

const KIND_ICON: Record<string, typeof FileText> = {
  pdf: FileText, pptx: Presentation, docx: FileText, xlsx: Table2,
};

export default function Authoring() {
  const [brief, setBrief] = useState('');
  const [running, setRunning] = useState(false);
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const [logs, setLogs] = useState<string[]>([]);
  const [answer, setAnswer] = useState('');
  const [files, setFiles] = useState<MadeFile[]>([]);
  const [follow, setFollow] = useState(true);
  const [view, setView] = useState<View>('preview');
  const [history, setHistory] = useState<Run[]>([]);
  const [activeId, setActiveId] = useState('');
  /** the session window came back full, so older runs are still off-list. */
  const [windowFull, setWindowFull] = useState(false);
  const [preview, setPreview] = useState<Record<string, DocPreview>>({});
  const [previewErr, setPreviewErr] = useState('');
  /* PDFs get the browser's own renderer, not extracted text. pypdf hands
   * back a flat run of words - the table columns come back as one paragraph
   * ("Metric HNSW IVF Recall / Accuracy Very High...") - which reads far
   * worse than the real page. The Documents view already previews PDFs this
   * way; blob URL + iframe, revoked when it goes away. */
  const [pdfUrls, setPdfUrls] = useState<Record<string, string>>({});

  const ctlRef = useRef<AbortController | null>(null);
  const reqRef = useRef(0);
  const sidRef = useRef('');
  const startRef = useRef<HTMLButtonElement>(null);
  const stopRef = useRef<HTMLButtonElement>(null);
  const logBox = useRef<HTMLDivElement>(null);
  const resultRef = useRef<HTMLDivElement>(null);
  const [busyFile, setBusyFile] = useState('');

  /* session ids whose /produced has been queued, and the reads in flight. Both
   * exist so the mount prefetch, the scroll prefetch and a click cannot fire
   * three requests for one run and race three merges into the same list. */
  const askedRef = useRef(new Set<string>());
  const inflightRef = useRef(new Set<string>());
  /** runs the server last said were still going: the only ones worth a poll. */
  const runningRef = useRef(new Set<string>());
  /* the output tablist is a roving tabindex, so the arrows must be able to put
   * focus on whichever tab they select. */
  const tabRefs = useRef<Partial<Record<View, HTMLButtonElement | null>>>({});

  /* ── history ───────────────────────────────────────────────────────────
   * The list itself is server-backed (see the history effect below).
   * localStorage is read first so an in-flight run is on screen before the
   * network answers, and so its rows are not lost if the read fails. */
  useEffect(() => {
    const local = readLocal();
    if (local.length) setHistory(local);
  }, []);

  /* The cap is on what is WRITTEN, never on what is shown. Capping the
   * in-memory list meant the next finished run sliced every reachable document
   * back down to MAX_KEPT - the truncation the server-backed rebuild exists to
   * remove, back again after a single build. */
  const saveRun = useCallback((r: Run) => {
    setHistory((prev) => {
      const next = [r, ...prev.filter((p) => p.session_id !== r.session_id)];
      try {
        localStorage.setItem(LS_KEY, JSON.stringify(next.slice(0, MAX_KEPT)));
      } catch { /* quota: keep the in-memory list */ }
      return next;
    });
  }, []);

  useEffect(() => {
    if (follow && logBox.current) logBox.current.scrollTop = logBox.current.scrollHeight;
  }, [logs, follow]);

  /** Read /produced for one run and fold it into its row. Concurrent reads are
   *  deduped: the mount prefetch, the scroll prefetch and a click would
   *  otherwise fetch the same session three times and race three merges. */
  const loadProduced = useCallback((sid: string) => {
    if (!sid || inflightRef.current.has(sid)) return Promise.resolve();
    inflightRef.current.add(sid);
    return api.runProduced(sid)
      .then((snap) => {
        if (snap.live) runningRef.current.add(sid);
        else runningRef.current.delete(sid);
        setHistory((prev) => prev.map(
          (r) => (r.session_id === sid ? withProduced(r, snap) : r),
        ));
      })
      .catch(() => { /* unreadable: the row keeps its metadata, and a click retries */ })
      .finally(() => { inflightRef.current.delete(sid); });
  }, []);

  /** Read a batch of runs, one READ_AHEAD wave at a time. */
  const prefetchProduced = useCallback((sids: string[]) => {
    const todo = sids.filter((s) => s && !askedRef.current.has(s));
    if (!todo.length) return;
    todo.forEach((s) => askedRef.current.add(s));
    let i = 0;
    const wave = () => {
      if (i >= todo.length) return;
      const batch = todo.slice(i, i + READ_AHEAD);
      i += batch.length;
      void Promise.all(batch.map((s) => loadProduced(s))).then(wave);
    };
    wave();
  }, [loadProduced]);

  /** The rows the reader is about to scroll into. */
  const unread = useCallback(() => history
    .map((r) => r.session_id)
    .filter((s) => !askedRef.current.has(s))
    .slice(0, READ_AHEAD), [history]);

  /* A run the server still calls live is re-read every few seconds, so the
   * spinner is the truth and the file appears the moment it is written. A
   * settled run is never polled again: nothing here costs a request while no
   * run is going. */
  useEffect(() => {
    const iv = window.setInterval(() => {
      for (const sid of Array.from(runningRef.current)) void loadProduced(sid);
    }, 4000);
    return () => window.clearInterval(iv);
  }, [loadProduced]);

  /* Load the previews of whatever files are on screen. Reading the artifact
   * is the only honest source - see the DocPreview doc comment. One effect
   * serves both entry points (a fresh run and a run reopened from history),
   * tracked by id so it cannot loop. */
  const loadedRef = useRef<string>('');
  useEffect(() => {
    const ids = files.map((f) => f.artifact).sort().join(',');
    if (!ids || ids === loadedRef.current) return;
    loadedRef.current = ids;
    let alive = true;
    setPreviewErr('');
    (async () => {
      const got: Record<string, DocPreview> = {};
      const urls: Record<string, string> = {};
      for (const f of files) {
        if (!alive) return;
        try {
          got[f.artifact] = await api.artifactPreview(f.artifact);
        } catch (e) {
          if (alive) {
            setPreviewErr(`could not read ${f.filename}: ${(e as Error)?.message || e}`);
          }
          continue;
        }
        if ((got[f.artifact]?.kind || f.format) === 'pdf') {
          try {
            const r = await fetch(`/api/artifact/${f.artifact}?download=0`, {
              headers: headers(),
            });
            if (r.ok && alive) urls[f.artifact] = URL.createObjectURL(await r.blob());
          } catch { /* fall back to the extracted text */ }
        }
      }
      if (!alive) return;
      setPreview(got);
      setPdfUrls((old) => {
        Object.values(old).forEach((u) => URL.revokeObjectURL(u));
        return urls;
      });
    })();
    return () => { alive = false; };
  }, [files]);

  const loadPreviews = useCallback(async (fs: MadeFile[]) => {
    loadedRef.current = '';
    setFiles(fs);
  }, []);

  useEffect(() => () => {
    Object.values(pdfUrls).forEach((u) => URL.revokeObjectURL(u));
  }, [pdfUrls]);

  /** Put a run's row on screen: its answer, its files and its log lines. The
   *  row is the single source, so a snapshot that lands AFTER the click (the
   *  files had never been read) still reaches the pane. */
  const showRun = useCallback((r: Run) => {
    setAnswer(r.answer || '');
    /* the streamed log is newer than the row's node rollup: never blank it */
    setLogs((cur) => (r.logs?.length ? r.logs : cur));
    const fs = r.files || [];
    setFiles(fs);
    setView(fs.length ? 'preview' : 'receipt');
    if (fs.length) void loadPreviews(fs);
  }, [loadPreviews]);

  /** Reopen a run from the sidebar: restores its files, answer and receipt. */
  const openRun = useCallback((r: Run) => {
    setRunning(false);
    setErr('');
    setNote('');
    setActiveId(r.session_id);
    showRun(r);
    /* its files may never have been read - an older document, or one the
     * prefetch has not reached. Read it rather than claim it has no summary. */
    void loadProduced(r.session_id);
    requestAnimationFrame(() => resultRef.current?.focus());
  }, [showRun, loadProduced]);

  /* The open row drives the pane. Only a SETTLED row is re-shown: a live one is
   * re-read every few seconds, and re-applying it would drag the reader off the
   * tab they chose and re-fetch every preview. */
  const activeRun = useMemo(
    () => history.find((r) => r.session_id === activeId),
    [history, activeId],
  );
  useEffect(() => {
    if (activeRun && !activeRun.live) showRun(activeRun);
  }, [activeRun, showRun]);

  /* ── leave it running ──────────────────────────────────────────────────
   * A run keeps executing on the server after the browser closes the SSE
   * stream — Research relies on exactly that, and its topic list still shows
   * the run when you come back. Authoring did not: every bit of state lived
   * in component memory and was only persisted on `done`, so navigating away
   * mid-run (or reloading) forgot a run that was still going, and the file it
   * was about to produce was unreachable from this view. The in-flight run is
   * now written to storage as soon as the server names the session, and
   * reattached by polling /produced on mount. */
  const [live, setLive] = useState<{ sid: string; brief: string } | null>(null);
  const liveRef = useRef(live);
  liveRef.current = live;

  /** Merge a polled /produced snapshot into the view + history. */
  const adopt = useCallback((base: Run, snap: Produced) => {
    const r = withProduced(base, snap);
    saveRun(r);
    setActiveId(r.session_id);
    setFiles(r.files || []);
    setAnswer(r.answer || '');
    setView((r.files || []).length ? 'preview' : 'receipt');
  }, [saveRun]);

  /* Reattach to a run that was left in flight. */
  const reattach = useCallback(async (sid: string, brief: string) => {
    setRunning(true);
    setLive({ sid, brief });
    setNote('reconnected · working…');
    setErr('');
    let settled = false;
    for (let i = 0; i < 900; i++) {
      // eslint-disable-next-line no-await-in-loop
      const snap = await api.runProduced(sid).catch(() => null);
      if (!snap) break;
      if (snap.nodes?.length) setLogs(logLines(snap.nodes));
      if (!snap.live) {
        settled = true;
        setRunning(false);
        setLive(null);
        adopt({ session_id: sid, topic: brief.slice(0, 120),
                files: null, at: Date.now() }, snap);
        setNote(snap.files.length
          ? `finished while you were away · ${snap.files.length} file${snap.files.length === 1 ? '' : 's'}`
          : 'finished while you were away · no file was produced');
        if (!snap.files.length) {
          setErr(`That run produced no document. It answered: "${(snap.answer || '').slice(0, 120)}"`);
        }
        break;
      }
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => { setTimeout(r, 2000); });
    }
    if (!settled) {
      setRunning(false);
      setLive(null);
      setNote('lost track of that run — reopen it from Recent documents.');
    }
  }, [adopt]);

  /* ── the document history is SERVER-backed ──────────────────────────────
   * It used to live only in localStorage, which meant a document you built was
   * reachable from exactly one browser profile and vanished from the UI the
   * moment that storage was cleared - even though the file was sitting in the
   * artifact store, valid and downloadable. Reported live as "the document is
   * not showing up and I cannot access it".
   *
   * The server already knows: every authoring run wrote produced_files.json.
   * So the list is rebuilt from the session store on mount, and localStorage is
   * demoted to what it is actually good for - remembering an in-flight run
   * across a reload.
   *
   * Two things this must not do again. It must not read a fixed window of
   * sessions: /api/sessions pages by recency across BOTH kinds of run, so a
   * window of 60 surfaced only the 40 newest authoring runs out of 131 and the
   * badge claimed completeness over 91 documents it could not reach. And it must
   * not cap the rows: a paid document you cannot open is money you cannot get
   * back. */
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const d = await api.sessions(SESSION_WINDOW);
        if (!alive) return;
        const rows = (d.sessions || []).filter((s) => s.run_kind === 'authoring');
        /* metadata only. /produced is one graph parse per call, and reading all
         * 131 up front made the sidebar unusable for seconds - so the files are
         * read lazily, for the rows that are shown. */
        const loaded: Run[] = rows.map((s) => ({
          session_id: s.session_id,
          topic: s.topic || s.query || '',
          // null, not []: nothing has been read for this run yet, and [] reads
          // as "0 files" - the exact look of a run still going.
          files: null,
          at: (s.updated || 0) * 1000,
          live: false,
          kind: 'auto',
        }));
        if (!alive) return;
        /* a full window means the store holds more runs than one request
         * returns, so the list says so rather than claiming to be all of it. */
        setWindowFull((d.sessions || []).length >= SESSION_WINDOW);
        setHistory((prev) => {
          /* Keep anything local the server has not caught up with yet - a run
           * still in flight has no produced_files.json. Nothing is sliced off:
           * every run the server knows about stays reachable. */
          const seen = new Set(loaded.map((r) => r.session_id));
          return [...prev.filter((r) => !seen.has(r.session_id)), ...loaded]
            .sort((a, b) => (b.at || 0) - (a.at || 0));
        });
        prefetchProduced(loaded.slice(0, READ_AHEAD).map((r) => r.session_id));
      } catch { /* the sidebar simply stays as it was */ }
    })();
    return () => { alive = false; };
  }, []);

  /* On mount: is there a run still going? */
  useEffect(() => {
    let alive = true;
    try {
      const pending = readLocal().find((r) => r.live && !r.files?.length);
      if (pending && alive) void reattach(pending.session_id, pending.topic || '');
    } catch { /* private mode */ }
    return () => { alive = false; };
  }, [reattach]);

  // Stop turns invisible when the run ends, which drops focus to <body>. Put it
  // back on Author so a keyboard user lands somewhere real.
  useEffect(() => {
    if (!running) startRef.current?.focus();
  }, [running]);

  const start = useCallback(async () => {
    const t = brief.trim();
    if (!t || running) {
      if (!t) setErr('Describe what you want the document to be first.');
      return;
    }
    ctlRef.current?.abort();
    const ctl = new AbortController();
    ctlRef.current = ctl;
    const req = ++reqRef.current;
    sidRef.current = '';

    setRunning(true);
    requestAnimationFrame(() => stopRef.current?.focus());
    setErr(''); setNote('drafting…'); setLogs([]); setAnswer(''); setFiles([]);
    setPreview({}); setPreviewErr(''); setActiveId('');
    setView('receipt');

    /* No `doc_setup` and no directive line. With the Setup card gone there is
       nothing to enforce, and a payload of eleven defaults would be a lie: it
       would claim the user chose A4/portrait/Report when they chose nothing.
       The brief is the whole instruction - "as a PDF" is how a person asks
       for a PDF - and `_clean_doc_setup` still honours a forced setup from any
       other caller. */
    const query = `Create a document.\n\n${t}`;

    try {
      // An idempotency key, or three synchronous clicks are three PAID runs.
      // The `running` guard cannot help here: within one tick React has not
      // re-rendered, so `running` is still false. Research already dedupes
      // server-side on this key; authoring has to opt in too.
      const idempotencyKey =
        `auth-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      for await (const ev of api.chat(query, undefined, ctl.signal, {
        idempotencyKey,
      })) {
        if (reqRef.current !== req) return;
        if (ev.type === 'started') {
          /* Persist the in-flight run the moment the server names it, so it
             survives navigation. Before this the run existed only inside the
             stream and was forgotten the moment the page went away. */
          const s = String((ev as { session_id?: string }).session_id || '');
          if (s) {
            sidRef.current = s;
            setLive({ sid: s, brief: t });
            saveRun({ session_id: s, topic: t.slice(0, 120), query,
                      // Nothing was requested yet; the format is whatever the run produces.
  files: [], at: Date.now(), live: true, kind: 'auto' } as Run);
            setActiveId(s);
          }
        } else if (ev.type === 'log') {
          const txt = String((ev as { text?: string }).text || '');
          if (txt.trim()) setLogs((l) => [...l.slice(-500), txt]);
        } else if (ev.type === 'status') {
          // Heartbeats ("working… 40s elapsed") are progress, not log output.
          // They used to be appended to the same list, so the run log filled
          // with timer spam after the run had already finished.
          const txt = String((ev as { text?: string }).text || '');
          if (/elapsed/.test(txt)) setNote(txt.replace(/\s*·\s*$/, ''));
          else if (txt.trim()) setLogs((l) => [...l.slice(-500), txt]);
        } else if (ev.type === 'error') {
          if (reqRef.current === req) {
            setErr(String((ev as { text?: string }).text || 'the run failed'));
            setNote('');
            setRunning(false);
          }
        } else if (ev.type === 'done') {
          if (ctl.signal.aborted) return;
          const raw = String((ev as { answer?: string }).answer || '');
          const sid = String((ev as { session_id?: string }).session_id || '');
          sidRef.current = sid;
          setAnswer(raw);
          setRunning(false);
          let made: MadeFile[] = [];
          const reported = (ev as { files?: MadeFile[] }).files;
          if (Array.isArray(reported)) {
            made = reported.filter(
              (f) => f && typeof f.artifact === 'string'
                && f.artifact.startsWith('art:'),
            );
          }
          setFiles(made);
          setActiveId(sid);
          setView(made.length ? 'preview' : 'receipt');

          /* A document request that produced no file is NOT a success. The
           * planner can short-circuit (a stray "reply with only OK" memory
           * once did exactly that), and the pane then showed "done." with a
           * two-character answer and no explanation. */
          if (!made.length) {
            setErr(
              'The run finished without producing a file. The agent answered '
              + `instead: "${raw.slice(0, 120)}"`,
            );
            setNote('');
          } else {
            setNote(`done · ${made.length} file${made.length === 1 ? '' : 's'}`);
          }
          if (sid) {
            saveRun({
              session_id: sid,
              topic: t.slice(0, 120),
              query: query,
              files: made,
              answer: raw,
              at: Date.now(),
              live: false,
              // What was ACTUALLY produced, not what the run was asked for.
              // With no format control the model chooses, so the honest kind
              // is only knowable once a file exists.
              kind: (made[0]?.format || 'auto') as Kind,
            } as Run);
          }
          setLive(null);
          requestAnimationFrame(() => startRef.current?.focus());
        }
      }
    } catch (e) {
      if (reqRef.current === req && !ctl.signal.aborted) {
        setErr(String((e as Error)?.message || e));
        setNote('');
        setRunning(false);
      }
    } finally {
      if (ctlRef.current === ctl) ctlRef.current = null;
    }
    // The whole `setup` object, not `setup.format`. Listing only the format
  // re-memoised the callback on a format change, so it kept the closure's
  // STALE setup: changing Style, Margins, Length, Citations, Orientation or
  // Columns updated the panel and the live preview, and then sent the
  // defaults to the renderer. Three of the four always-visible controls were
  // inert.
  }, [brief, running, saveRun]);

  /* Aborting the SSE stream only stops the CLIENT: the worker keeps running
   * and its spend keeps mounting (agent_server documents this). Cancel on
   * the server too. */
  const stop = () => {
    const sid = sidRef.current;
    ctlRef.current?.abort();
    setRunning(false);
    setNote('stopped.');
    if (sid) {
      api.cancelRun({ session_id: sid }).catch(() => { /* best effort */ });
      setErr('Stopped. The server may still finish this run — check the Runs page.');
    }
    requestAnimationFrame(() => startRef.current?.focus());
  };

  /* An <a href> navigation cannot send X-Aria-Token, so a direct link to
   * /api/artifact/... 403s and the download silently cancels. Fetch with the
   * header, then hand the browser a blob URL. */
  const downloadFile = async (f: MadeFile) => {
    if (busyFile) return;
    setBusyFile(f.artifact);
    setErr('');
    try {
      const r = await fetch(`/api/artifact/${f.artifact}?download=1`, {
        headers: headers(),
      });
      if (!r.ok) {
        let msg = `${r.status}`;
        try { msg = String((await r.json())?.error || msg); } catch { /* not JSON */ }
        throw new Error(msg);
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = f.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      setNote(`downloaded ${f.filename} (${Math.max(1, Math.round(blob.size / 1024))} KB)`);
    } catch (e) {
      setErr(`download failed: ${String((e as Error)?.message || e)}`);
    } finally {
      setBusyFile('');
    }
  };
  const totalWarnings = useMemo(
    () => files.reduce((n, f) => n + (preview[f.artifact]?.warnings?.length || 0), 0),
    [files, preview],
  );

  const hasOutput = !!(files.length || answer || logs.length);
  /* the open run has not been read yet. Saying so is the honest state; the
   * pane used to fall through to "produced 0 files and no written summary". */
  const reading = !!(activeRun && activeRun.files === null);

  /* ── the output tablist ──────────────────────────────
   * A disabled tab cannot take focus, so the arrows skip it: landing on one
   * would swallow the key press and read as broken. */
  const tabEnabled = (t: View) => t !== 'preview' || files.length > 0;
  const stepTab = (from: View, dir: 1 | -1) => {
    const n = TAB_ORDER.length;
    const i = Math.max(0, TAB_ORDER.indexOf(from));
    for (let k = 1; k <= n; k += 1) {
      const t = TAB_ORDER[(i + dir * k + n * n) % n];
      if (tabEnabled(t)) return t;
    }
    return from;
  };
  /** Home/End: the first or last tab that can actually be selected. */
  const edgeTab = (dir: 1 | -1) => {
    const n = TAB_ORDER.length;
    for (let k = 0; k < n; k += 1) {
      const t = TAB_ORDER[dir === 1 ? k : n - 1 - k];
      if (tabEnabled(t)) return t;
    }
    return view;
  };

  /* ── preview bodies ─────────────────────────────────────────────────── */

  const PreviewPane = ({ f }: { f: MadeFile }) => {
    const p = preview[f.artifact];
    if (!p) {
      return (
        <div className="flex h-full items-center justify-center p-6 text-[12.5px] text-zinc-muted">
          <Loader2 size={14} className="mr-2 animate-spin" /> reading {f.filename}…
        </div>
      );
    }
    const warns = p.warnings || [];

    return (
      <div className="space-y-3 p-3.5">
        {warns.length > 0 && (
          <div role="status" className="flex items-start gap-2 rounded-lg border border-amber-400/30 bg-amber-400/10 p-2.5 text-[12px] text-amber-100">
            <AlertTriangle size={14} className="mt-px flex-none" />
            <div>
              <div className="font-semibold">Check this before you use it</div>
              {warns.map((w) => <div key={w} className="mt-0.5">{w}</div>)}
            </div>
          </div>
        )}

        {p.kind === 'pptx' && (
          <div>
            {/* The slides themselves. A list of slide titles cannot show
                whether the chart fits or the type is legible; the drawn slide
                can. */}
            <div className="docview-frame">
              <DocumentViewer source={{
                kind: 'artifact', artifact: f.artifact,
                format: 'pptx', label: f.filename,
              }} />
            </div>
            <details className="mt-2.5">
              <summary className="cursor-pointer text-[11px] font-semibold text-zinc-muted">
                Speaker notes and bullet text
              </summary>
              <h3 className="mb-2 mt-2 text-[11px] font-bold uppercase tracking-[0.12em] text-zinc-muted">
                {p.slides?.length || 0} slides
              </h3>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {(p.slides || []).map((s) => (
                <div key={s.n}
                  className={`rounded-lg border p-2.5 ${
                    s.empty
                      ? 'border-red-400/40 bg-red-400/5'
                      : 'border-white/10 bg-[#0e0e12]'}`}>
                  <div className="flex items-center gap-1.5">
                    <span className="text-[10px] font-bold text-zinc-600">{s.n}</span>
                    <span className={`truncate text-[12px] font-semibold ${
                      s.empty ? 'text-red-300' : ''}`}>
                      {s.title || (s.empty ? '(blank slide)' : 'Untitled')}
                    </span>
                  </div>
                  {s.bullets.length > 0 && (
                    <ul className="mt-1.5 space-y-1">
                      {s.bullets.slice(0, 6).map((b, i) => (
                        <li key={i} className="flex gap-1.5 text-[11.5px] leading-snug text-zinc-400">
                          <span className="text-zinc-600">·</span>
                          <span className="min-w-0 break-words">{b}</span>
                        </li>
                      ))}
                      {s.bullets.length > 6 && (
                        <li className="text-[11px] text-zinc-600">
                          +{s.bullets.length - 6} more
                        </li>
                      )}
                    </ul>
                  )}
                  {s.notes && (
                    <p className="mt-1.5 line-clamp-2 text-[10.5px] italic text-zinc-600">
                      {s.notes}
                    </p>
                  )}
                </div>
              ))}
            </div>
            </details>
          </div>
        )}

        {p.kind === 'xlsx' && (
          <div className="space-y-3">
            {/* The sheets themselves, read back out of the saved .xlsx, with
                numbers right-aligned the way a spreadsheet shows them. */}
            <div className="docview-frame">
              <DocumentViewer source={{
                kind: 'artifact', artifact: f.artifact,
                format: 'xlsx', label: f.filename,
              }} />
            </div>
            <details>
              <summary className="cursor-pointer text-[11px] font-semibold text-zinc-muted">
                Raw cell values
              </summary>
            {(p.sheets || []).map((s) => (
              <div key={s.name}>
                <h3 className="mb-1 text-[11px] font-bold uppercase tracking-[0.12em] text-zinc-muted">
                  {s.name} {s.empty && <span className="text-red-400">(empty)</span>}
                </h3>
                {s.rows.length > 0 ? (
                  <div className="overflow-x-auto rounded-lg border border-white/10">
                    <table className="w-full border-collapse text-[11.5px]">
                      <tbody>
                        {s.rows.slice(0, 30).map((row, ri) => (
                          <tr key={ri} className={ri === 0
                            ? 'bg-white/[0.06] font-semibold'
                            : ri % 2 ? 'bg-white/[0.015]' : ''}>
                            {row.map((c, ci) => (
                              <td key={ci}
                                className="border-b border-white/5 px-2 py-1.5 align-top text-zinc-300">
                                {c}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {s.rows.length > 30 && (
                      <div className="p-1.5 text-[11px] text-zinc-600">
                        +{s.rows.length - 30} more rows
                      </div>
                    )}
                  </div>
                ) : (
                  <p className="text-[11.5px] text-zinc-600">This sheet has no cells.</p>
                )}
              </div>
            ))}
            </details>
          </div>
        )}

        {p.kind === 'pdf' && (
          <div className="space-y-2.5">
            {/* The DOCUMENT, not an outline of it. This used to be extracted
                text, with a comment explaining that an inline PDF viewer is
                impossible in headless Chromium so it rendered as a white
                panel. The fix was to rasterise the delivered pages server-side
                and show those images - which needs no browser PDF plugin at
                all, and is the only way to actually SEE whether the margins,
                the measure and the chart are right. */}
            <div className="docview-frame">
              <DocumentViewer source={{
                kind: 'artifact', artifact: f.artifact,
                format: f.format || 'pdf', label: f.filename,
              }} />
            </div>
            <div className="flex items-center gap-2">
              {pdfUrls[f.artifact] && (
                <button
                  type="button"
                  onClick={() => window.open(pdfUrls[f.artifact], '_blank', 'noopener')}
                  className="flex items-center gap-1.5 rounded-md border border-white/10 px-2 py-1 text-[11px] font-semibold hover:border-violet-400"
                >
                  Open in a new tab
                </button>
              )}
              <span className="text-[11px] text-zinc-600">
                {p.pages?.length || 0} page(s)
              </span>
            </div>
            {/* The extracted text of every page used to be dumped here, one
                block per page. It was noise, not information: the rasterised
                page above IS the document, the words are already on screen,
                and each block repeated the running header and the title. A
                reader scrolling past three full pages of duplicated prose to
                reach nothing was the opposite of a preview. The page images
                and the thumbnail strip are the whole story. */}
          </div>
        )}

        {p.kind === 'docx' && (
          <div className="space-y-2">
            {/* The document's own content, read back out of the saved .docx. */}
            <div className="docview-frame">
              <DocumentViewer source={{
                kind: 'artifact', artifact: f.artifact,
                format: 'docx', label: f.filename,
              }} />
            </div>
            <details>
              <summary className="cursor-pointer text-[11px] font-semibold text-zinc-muted">
                Plain text
              </summary>
            {(p.paragraphs || []).map((para, i) => (
              <p key={i} className="text-[12.5px] leading-relaxed text-zinc-300">{para}</p>
            ))}
            {(p.tables || []).map((t, ti) => (
              <div key={ti} className="overflow-x-auto rounded-lg border border-white/10">
                <table className="w-full border-collapse text-[11.5px]">
                  <tbody>
                    {t.map((row, ri) => (
                      <tr key={ri} className={ri === 0 ? 'bg-white/[0.06] font-semibold' : ''}>
                        {row.map((c, ci) => (
                          <td key={ci} className="border-b border-white/5 px-2 py-1.5 text-zinc-300">{c}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
            </details>
          </div>
        )}

        {p.kind === 'unknown' && (
          <p className="text-[12.5px] text-zinc-400">
            This file type has no preview. Download it to open it.
          </p>
        )}
      </div>
    );
  };

  /* ── render ─────────────────────────────────────────────────────────── */

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Authoring">
          <span className="text-[11.5px] text-zinc-muted">
            {running ? 'building…' : note || 'Ctrl+Enter to build'}
          </span>
        </TopBar>

        {/* The Setup card is gone. It asked for format, paper, typeface,
            length, orientation, columns, margins, citations and three
            toggles - eleven decisions before you had even described the
            document, and most of them were things the brief already implied.
            Removing it means the run reads the brief and picks the format,
            page and structure itself, which is what the user was asking for
            when they said the document should be right without them having to
            specify it. The `doc_setup` plumbing is untouched on the server, so
            a forced setup still works for callers that send one. */}
        <div className="flex min-h-0 flex-1 flex-col gap-3 p-3.5 xl:flex-row">
          {/* ── left column: composer + history ─────────────────────── */}
          <div className="flex min-h-0 w-full shrink-0 flex-col gap-3 max-md:max-h-[52vh] max-md:overflow-y-auto xl:w-[400px] xl:max-w-[44%]">
            <section
              aria-labelledby="brief-h"
              className="shrink-0 rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5 shadow-[0_8px_24px_rgba(0,0,0,0.45)]"
            >
              <h2 id="brief-h" className="sr-only">Document brief</h2>
              <label htmlFor="brief-box" className="mb-1.5 block text-[11px] font-bold uppercase tracking-[0.12em] text-zinc-muted">
                Brief
              </label>
              <textarea
                id="brief-box"
                value={brief}
                onChange={(e) => setBrief(e.target.value)}
                onKeyDown={(e) => {
                  if (e.nativeEvent.isComposing) return;
                  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) start();
                }}
                placeholder="Describe the document you want — e.g. a board briefing on the clawback rules in my uploaded Bluebook, as a PDF"
                rows={4}
                className="w-full resize-y rounded-lg border border-white/10 bg-black/40 p-2.5 text-[13px] leading-relaxed outline-none focus:border-violet-400"
              />
              {/* The format is now part of the ask rather than a separate
                * control: "as a PDF", "as a deck" or "as a spreadsheet" in the
                * brief is how a person actually specifies it, and it travels
                * in the same sentence as everything else that shapes the
                * document. */}
              {/* justify-between, not a flex-1 spacer inside a wrapping row:
               * at 390px the bases summed to 353px in 332px of space and the
                * Submit button wrapped to a second line, so the card jumped
                * 42px between states. */}
              <div className="mt-2.5 flex items-center justify-between gap-2">
                <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-muted" role="status">
                  {running
                    ? (note || 'working…')
                    : note || 'Ctrl+Enter to build'}
                </span>
                {/* Say it, because it is the thing that was missing. */}
                {running && (
                  <span className="hidden flex-none text-[10.5px] text-zinc-600 lg:inline">
                    safe to navigate away
                  </span>
                )}
{/* Both stay mounted: unmounting a focused button throws focus
                  * to <body>. Stop is only *visible* while a run is going, so
                  * an idle page does not offer to cancel nothing - and when the
                  * run ends focus moves to Author, because the button the user
                  * was on is now hidden. */}
                <button
                  ref={stopRef}
                  type="button"
                  onClick={stop}
                  disabled={!running}
                  aria-label="Stop the run"
                  aria-hidden={!running}
                  tabIndex={running ? 0 : -1}
                  className={`${running ? '' : 'invisible'} flex-none rounded-md border border-red-400/50 bg-red-400/10 px-3.5 py-2 text-xs font-bold text-red-200 hover:bg-red-400/20 disabled:opacity-40`}
                >
                  Stop
                </button>
                <button
                  ref={startRef}
                  type="button"
                  onClick={start}
                  disabled={running}
                  className="flex-none items-center gap-1.5 rounded-md bg-violet-400 px-3.5 py-2 text-xs font-bold text-[#0b0b0e] shadow-[0_0_16px_rgba(139,124,246,0.35)] hover:brightness-110 disabled:opacity-50"
                >
                  {running
                    ? <Loader2 size={13} className="animate-spin" />
                    : <Sparkles size={13} />}
                  {running ? 'Building…' : 'Author'}
                </button>
              </div>

              {/* four example briefs were a third card of vertical pressure,
                  stacked between the brief and the documents the user came
                  back for. Folded under the brief they cost one closed row
                  and stay a keystroke away. */}
              {!hasOutput && !running && (
                <details className="mt-2.5">
                  <summary className="cursor-pointer text-[11px] font-semibold text-zinc-muted">
                    Try one of these
                  </summary>
                  <ul className="mt-2 space-y-1.5">
                    {SUGGESTIONS.map((s) => (
                      <li key={s}>
                        <button
                          type="button"
                          onClick={() => { setBrief(s); startRef.current?.focus(); }}
                          className="w-full rounded-lg border border-white/10 px-2.5 py-1.5 text-left text-[11.5px] text-zinc-300 hover:border-violet-400"
                        >
                          {s}
                        </button>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </section>

            {/* ── history sidebar ──────────────────────────────────── */}
            <section
              aria-labelledby="hist-h"
              className="flex min-h-0 flex-1 flex-col rounded-[10px] border border-white/10 bg-[#0e0e12]"
            >
              <h2 id="hist-h"
                className="flex flex-none items-center gap-1.5 border-b border-white/10 p-2.5 text-[11px] font-bold uppercase tracking-[0.12em] text-zinc-muted">
                <History size={12} /> Recent documents
                {history.length > 0 && (
                  <span className="ml-auto font-normal normal-case tracking-normal text-zinc-600">
                                        {windowFull ? `${history.length}+` : history.length}
                  </span>
                )}
              </h2>
              <div className="min-h-0 flex-1 overflow-y-auto p-1.5"
                onScroll={(e) => {
                  /* read the rows the reader is about to scroll into, so an
                   * older paid document opens with its answer rather than a
                   * spinner - without paying for every row up front. */
                  const el = e.currentTarget;
                  if (el.scrollHeight - el.scrollTop - el.clientHeight < 600) {
                    prefetchProduced(unread());
                  }
                }}>
                {history.length === 0 ? (
                  <p className="p-2 text-[11.5px] text-zinc-600">
                    Nothing yet. Documents you build are listed here and stay
                    downloadable after a reload.
                  </p>
                ) : (
                  <ul className="space-y-1">
                    {windowFull && (
                      <li className="px-2 pb-1 text-[10.5px] text-zinc-600">
                        the store holds more runs than one request returns; this
                        is the newest {SESSION_WINDOW} of them
                      </li>
                    )}
                    {history.map((r) => (
                      <li key={r.session_id}>
                        <button
                          type="button"
                          onClick={() => openRun(r)}
                          aria-current={r.session_id === activeId ? 'true' : undefined}
                          className={`w-full rounded-lg px-2 py-1.5 text-left hover:bg-white/5 ${
                            r.session_id === activeId ? 'bg-violet-400/10' : ''}`}
                        >
                          <span className="flex items-center gap-1.5">
                            {(r.files || []).map((f, i) => {
                              const Icon = KIND_ICON[f.format] || FileText;
                              return <Icon key={i} size={11} className="flex-none text-violet-300" />;
                            })}
                            <span className="min-w-0 flex-1 truncate text-[12px] font-semibold">
                              {r.topic || displayTopic(r)}
                            </span>
                          </span>
                          <span className="mt-0.5 flex items-center gap-1.5 text-[10.5px] text-zinc-600">
                            {/* three states, never two: a run the server says is still going, a run
                            whose files have not been read yet, and a run that is finished. The
                            middle one used to be reported as "0 files", which is what an unfinished
                            run looks like. */}
                            {r.live ? (
                            <>
                            <Loader2 size={9} className="animate-spin text-amber-300" />
                            running
                            </>
                            ) : r.files === null ? (
                            <>
                            <Loader2 size={9} className="animate-spin" />
                            reading
                            {r.at ? ` · ${ago((Date.now() - r.at) / 1000)}` : ''}
                            </>
                            ) : (
                            <>
                            {r.files.length} file{r.files.length === 1 ? '' : 's'}
                            {r.at ? ` · ${ago((Date.now() - r.at) / 1000)}` : ''}
                            </>
                            )}
                          </span>
                          </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </section>
          </div>

          {/* ── right column: the output ───────────────────────────── */}
          <section
            aria-labelledby="out-h"
            className="flex min-h-0 min-w-0 flex-1 flex-col rounded-[10px] border border-white/10 bg-[#08080a] shadow-[0_8px_24px_rgba(0,0,0,0.45)]"
          >
            <h2 id="out-h" className="sr-only">Output</h2>
            {/* Hidden until there is something to switch between: with no
                files the tabs are all disabled, and a dead tab bar sitting
                under a full-bleed preview just reads as broken. */}
            {(hasOutput || running) && (
            <div className="flex flex-none flex-wrap items-center gap-2 border-b border-white/10 p-2">
              <div role="tablist" aria-label="Output view" className="flex gap-1">
                {TABS.map(([id, label, Icon]) => (
                  <button
                    key={id}
                    ref={(el) => { tabRefs.current[id] = el; }}
                    type="button"
                    role="tab"
                    id={`tab-${id}`}
                    aria-selected={view === id}
                    aria-controls={`panel-${id}`}
                    tabIndex={view === id ? 0 : -1}
                    disabled={id === 'preview' && !files.length}
                    onClick={() => setView(id)}
                    onKeyDown={(e) => {
                    /* The arrows move the selection AND the focus. Selecting alone left focus
                    on a tab that was no longer the selected one, and Tab jumped out of the
                    tablist instead of moving to the next control - the roving tabindex had
                    already moved the only tab stop elsewhere. */
                    const to = e.key === 'ArrowRight' ? stepTab(view, 1)
                    : e.key === 'ArrowLeft' ? stepTab(view, -1)
                    : e.key === 'Home' ? edgeTab(1)
                    : e.key === 'End' ? edgeTab(-1)
                    : null;
                    if (!to) return;
                    e.preventDefault();
                    setView(to);
                    tabRefs.current[to]?.focus();
                    }}
                    className={`flex items-center gap-1.5 rounded px-2.5 py-1 text-[11.5px] disabled:opacity-40 ${
                    view === id
                    ? 'bg-violet-400/20 font-bold text-zinc-100'
                    : 'text-zinc-400 hover:text-zinc-200'}`}
                    >
                    <Icon size={12} />
                    {label}
                    {id === 'log' && logs.length ? ` (${logs.length})` : ''}
                  </button>
                ))}
                    </div>
              <span className="flex-1" />
              {totalWarnings > 0 && <Pill tone="warn">{totalWarnings} warning</Pill>}
              {files.length > 0 && (
                <Pill tone="ok">{files.length} file{files.length === 1 ? '' : 's'}</Pill>
              )}
            </div>
            )}

            {/* The pre-run layout preview is gone with the Setup card. It
                * rendered a SAMPLE_SPEC through whatever the controls were
                * set to, so without controls it would have shown a
                * decorative A4 page that predicted nothing about the document
                * the brief would actually produce - a preview of a document
                * that does not exist yet. The empty state says what to do
                * instead. */}
            {!hasOutput && !running && !reading && (
                <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 p-6 text-center text-[12.5px] text-zinc-muted">
                <p className="max-w-[42ch] leading-relaxed">
                  Describe the document on the left and Aria researches it,
                  then renders a real file you can read here and download.
                </p>
                <p className="max-w-[46ch] text-[11.5px] text-zinc-600">
                  Say what you want it to be &mdash; &ldquo;a PDF briefing&rdquo;,
                  &ldquo;a 10-slide deck&rdquo;, &ldquo;a spreadsheet&rdquo; &mdash;
                  and it will pick the format, page and structure.
                </p>
              </div>
            )}

            {/* Files: the download IS the deliverable, so it stays visible
             * above whichever tab is open. */}
            {files.length > 0 && (
              <div className="flex-none space-y-1.5 border-b border-white/10 p-2.5">
                {files.map((f) => {
                  const Icon = KIND_ICON[f.format] || FileDown;
                  const p = preview[f.artifact];
                  return (
                    <div key={f.artifact}
                      className="flex items-center gap-2 rounded-lg border border-white/10 bg-[#0e0e12] px-2.5 py-2">
                      <Icon size={14} className="flex-none text-violet-300" />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[12.5px] font-semibold">
                          {f.filename}
                        </span>
                        {p?.subtitle && (
                          <span className="block truncate text-[10.5px] text-zinc-600">
                            {p.subtitle}
                          </span>
                        )}
                      </span>
                      {f.format && <Pill tone="info">{f.format}</Pill>}
                      <button
                        type="button"
                        onClick={() => downloadFile(f)}
                        disabled={!!busyFile}
                        aria-label={`Download ${f.filename}`}
                        className="flex flex-none items-center gap-1 rounded-md border border-white/10 px-2 py-1 text-[11px] font-semibold hover:border-violet-400 disabled:opacity-60"
                      >
                        {busyFile === f.artifact
                          ? <Loader2 size={12} className="animate-spin" />
                          : <Download size={12} />}
                        Get
                      </button>
                    </div>
                  );
                })}
                {previewErr && (
                  <p className="text-[11px] text-amber-300">{previewErr}</p>
                )}
              </div>
            )}

            {/* The pane grows to its content instead of `h-full`: it was a
             * fixed-height box that stayed 596px tall while holding 246px of
             * output, leaving ~350px of dead gutter under every result. */}
            <div className="min-h-0 flex-1 overflow-y-auto">
              {err ? (
                <div role="alert" className="flex items-start gap-2 p-4 text-[12.5px] text-red-200">
                  <AlertTriangle size={15} className="mt-px flex-none" />
                  <span className="break-words">{err}</span>
                </div>
              ) : view === 'log' ? (
                <div
                  ref={logBox}
                  role="tabpanel"
                  id="panel-log"
                  aria-labelledby="tab-log"
                  tabIndex={0}
                  className="p-3 font-mono text-[11.5px] leading-relaxed text-zinc-400"
                >
                  {logs.map((l, i) => (
                    <div key={i} className="whitespace-pre-wrap break-words">{l}</div>
                  ))}
                  {!logs.length && <div className="text-zinc-muted">no output yet</div>}
                  <label className="sticky bottom-0 mt-2 block bg-[#08080a] pt-1 text-[11px] text-zinc-500">
                    <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow
                  </label>
                </div>
              ) : view === 'preview' ? (
                <div role="tabpanel" id="panel-preview" aria-labelledby="tab-preview" tabIndex={-1}>
                  {files.map((f) => <PreviewPane key={f.artifact} f={f} />)}
                </div>
              ) : (
                <div role="tabpanel" id="panel-receipt" aria-labelledby="tab-receipt" tabIndex={-1}>
                {reading ? (
                <p className="flex items-center gap-2 p-4 text-[12.5px] text-zinc-muted">
                <Loader2 size={13} className="animate-spin" /> reading that run…
                </p>
                ) : answer ? (
                    <article
                      ref={resultRef}
                      aria-live="polite"
                      className="prose-chat prose-report prose-wide px-4 py-3.5"
                      dangerouslySetInnerHTML={{ __html: renderMarkdown(answer, 1, true) }}
                    />
                  ) : files.length ? (
                    <p className="p-4 text-[12.5px] text-zinc-muted">
                      This run produced {files.length} file{files.length === 1 ? '' : 's'} and no
                      written summary.
                    </p>
                  ) : (
                    <div className="p-4">
                      <h3 className="mb-2 text-[12.5px] font-semibold">
                        Describe a document and the agent will research what it
                        needs, write it, and render a real file you can preview
                        and download.
                      </h3>
                      <p className="text-[12px] text-zinc-muted">
                        Pick a format above, or leave it on Auto and let the
                        agent choose.
                      </p>
                    </div>
                  )}
                </div>
              )}
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}
