import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  FileDown, Loader2, AlertTriangle, Sparkles, LayoutGrid, ScrollText,
  Terminal, Download, History, FileText, Table2, Presentation,
  Maximize2, MessageSquareText, Send, ArrowLeft,
  Check, X, Clock, SkipForward,
} from 'lucide-react';
import { SkipLink, Rail, TopBar, Pill } from '../components/ui';
import { useMainId, useSectionActive } from '../components/KeepAlive';
import { toast } from 'sonner';
import { renderMarkdown } from '../components/markdown';
import { api, headers, ago, displayTopic, type DocPreview, type SessionSummary } from '../api';
import { DocumentViewer } from './DocSetup';
import { DocumentModal, type DocumentTarget } from '../components/DocumentModal';
import { ModelPicker } from '../components/ModelPicker';
import { FilePreview } from '../components/FilePreview';
import { type PdfQuote, type QuoteHit } from '../components/PdfViewer';
import { answerCitations } from '../lib/citations';
import { Pane } from '../lib/panes';

type Kind = 'auto' | 'pdf' | 'pptx' | 'docx' | 'xlsx';
type View = 'preview' | 'receipt' | 'log' | 'discuss';

/* The output tabs, in the order the arrows walk them.

   Focus is NOT in this list. It used to be a tab that re-skinned the Discuss
   workspace, which left the tab strip, the top bar, the pills and the run's
   chrome on screen — the opposite of what a reading mode is for. It is now its
   own page at `/authoring/focus`, resolved to this same section by
   `SUB_SECTIONS` in App.tsx so entering it never starts a new conversation,
   and rendering nothing but the document and Aria. */
const TABS: [View, string, typeof FileText][] = [
  ['preview', 'Preview', LayoutGrid],
  ['receipt', 'Result', ScrollText],
  ['log', 'Run log', Terminal],
  ['discuss', 'Discuss', MessageSquareText],
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
  /** The Documents-library entry these bytes were indexed into, with their
   *  chunk embeddings. It is what makes the file searchable and what a
   *  discussion about it binds to; "" or absent means it never got filed. */
  document_id?: string;
  /** What render_document reported about the delivered document. */
  blocks?: number;
  stats?: { words?: number; sections?: number; format?: string };
  /** Tokens + spend. `scope` is "run": this is the whole authoring run's
   *  cost, not this file's alone, because the planner, researchers and author
   *  all draw on one budget and cannot be attributed per document. */
  cost?: {
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    turns: number;
    calls?: number;
    cost: number;
    currency: string;
    scope: string;
    by_agent?: Record<string, {
      input_tokens: number;
      output_tokens: number;
      cost: number;
      calls: number;
    }>;
  };
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
  /** Per-node status + duration, for the receipt's "what this run did".
   *  Named `nodeList` because `SessionSummary` already has a numeric `nodes`
   *  count and this interface extends it. */
  nodeList?: RunNode[];
  /** The sources the researchers actually opened, for the receipt. */
  sources?: SourceRef[];
  kind?: Kind;
};

/** One node in a produced run: what it was, how it ended, how long it took. */
type RunNode = { id: string; skill: string; status: string; duration?: number | null };
/** One source a researcher opened. */
type SourceRef = { url: string; title?: string };

/** What /produced says about one run. */
type Produced = {
  files?: MadeFile[];
  answer?: string;
  live?: boolean;
  nodes?: RunNode[];
  sources?: SourceRef[];
};

/** A parsed run-log line: a node status or an info line. */
type NodeStatus = 'running' | 'complete' | 'failed' | 'skipped';
type LogLine = {
  kind: 'node' | 'info' | 'error';
  id?: string;
  skill?: string;
  status?: NodeStatus;
  duration?: string;
  detail?: string;
  text: string;
};

const NODE_LINE_RE =
  /^\[(n:\d+)\]\s+(\S+)\s+(running|complete|failed|skipped)\b\s*(?:\(([^)]+)\))?\s*(.*)$/;
const SKIP_LINE_RE =
  /^->\s+(n:\d+)\s+(\S+)\s+skipped\b\s*(?:\(([^)]+)\))?\s*(.*)$/;

/** Parse one log line into a node status or an info line. */
const parseLogLine = (text: string): LogLine => {
  const t = text.trim();
  let m = NODE_LINE_RE.exec(t);
  if (m) {
    return {
      kind: 'node', id: m[1], skill: m[2], status: m[3] as NodeStatus,
      duration: m[4] || undefined, detail: (m[5] || '').trim(), text,
    };
  }
  m = SKIP_LINE_RE.exec(t);
  if (m) {
    return {
      kind: 'node', id: m[1], skill: m[2], status: 'skipped',
      duration: m[3] || undefined, detail: (m[4] || '').trim(), text,
    };
  }
  if (/^\[error\]/i.test(t)) return { kind: 'error', text };
  return { kind: 'info', text };
};

/** 1.2s / 1m 05s — compact, for the per-node stopwatch. */
const fmtElapsed = (ms: number): string => {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(s % 60).padStart(2, '0')}s`;
};

/** The receipt's structured header: what the run did, and what it checked. */
function RunSummary({ nodes, sources }: {
  nodes: RunNode[]; sources: SourceRef[];
}) {
  if (!nodes.length && !sources.length) return null;
  return (
    <div className="mb-4 border-b border-white/10 pb-3">
      <div className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-wide text-zinc-500">
        What this run did
      </div>
      {nodes.length > 0 && (
        <ul className="mb-3 space-y-0.5 text-[12px]">
          {nodes.map((n) => {
            const tone = n.status === 'complete' ? 'text-emerald-300'
              : n.status === 'failed' ? 'text-red-300'
              : n.status === 'skipped' ? 'text-zinc-500' : 'text-amber-300';
            return (
              <li key={n.id} className="flex items-baseline gap-2">
                <span className="w-9 flex-none font-mono text-[10px] text-zinc-600">{n.id}</span>
                <span className={tone}>{n.skill}</span>
                <span className="text-zinc-600">{n.status}</span>
                {n.duration != null && (
                  <span className="ml-auto flex-none tabular-nums text-zinc-600">
                    {n.duration}s
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {sources.length > 0 && (
        <>
          <div className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-wide text-zinc-500">
            Sources checked ({sources.length})
          </div>
          <ul className="space-y-1 text-[12px]">
            {sources.map((s) => (
              <li key={s.url} className="min-w-0">
                <a
                  href={s.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  title={s.url}
                  className="block truncate text-sky-300/90 hover:text-sky-200 hover:underline"
                >
                  {s.title || s.url}
                </a>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/** The log of a run that is no longer streaming: the nodes it ran. */
const logLines = (nodes?: Produced['nodes']) =>
  (nodes || []).map((n) => {
    const d = n.duration != null ? ` (${n.duration}s)` : '';
    return `[${n.id}] ${n.skill} ${n.status}${d}`;
  });

const kb = (n?: number) =>
  n == null ? '' : n >= 1024 * 1024
    ? `${(n / 1024 / 1024).toFixed(1)} MB`
    : `${Math.max(1, Math.round(n / 1024))} KB`;

/** One line under a filename: what was delivered, and what the run cost.
 *
 *  Nothing is invented - each part is omitted when the server did not report
 *  it, so a missing figure reads as absent rather than as zero. */
const fileMeta = (f: MadeFile): string[] => {
  const out: string[] = [];
  const w = f.stats?.words;
  const s = f.stats?.sections;
  if (w != null) {
    out.push(s != null
      ? `${w.toLocaleString()} words · ${s} section${s === 1 ? '' : 's'}`
      : `${w.toLocaleString()} words`);
  }
  if (f.bytes != null) out.push(kb(f.bytes));
  const c = f.cost;
  if (c && c.total_tokens) {
    // `scope` is honest about what the number is: "run" is this authoring run's
    // spend; "session" is the whole conversation's, used only as a fallback
    // when the per-turn ledger could not be written. Presenting the second as
    // the first would over- or under-state a document's cost silently.
    const label = c.scope === 'session' ? 'tokens (whole session)'
      : c.scope === 'run' ? 'tokens' : 'tokens';
    out.push(`${c.total_tokens.toLocaleString()} ${label}`);
    if (c.cost > 0) {
      out.push(`$${c.cost.toFixed(3)}${c.scope === 'session' ? ' (session)' : ''}`);
    }
  }
  return out;
};

/** Fold a /produced snapshot into its row. One place, so the sidebar count, the
 *  receipt and the file list can never disagree about what a run did. */
const withProduced = (r: Run, snap: Produced): Run => {
  const fs: MadeFile[] = (snap.files || []).map((f) => ({
    artifact: f.artifact,
    filename: f.filename,
    format: f.format,
    // Carried through so the file list can show what was actually delivered
    // and what the run cost. Dropping them here is why every file rendered
    // as a bare name with no size.
    bytes: f.bytes,
    blocks: f.blocks,
    stats: f.stats,
    cost: f.cost,
    document_id: f.document_id,
  }));
  return {
    ...r,
    files: fs,
    answer: snap.answer || '',
    live: !!snap.live,
    logs: logLines(snap.nodes),
    nodeList: snap.nodes || r.nodeList,
    sources: snap.sources || r.sources,
    // What was ACTUALLY produced, not what the run was asked for.
    kind: (fs[0]?.format || r.kind || 'auto') as Kind,
  };
};

/** Stable, short key for one authoring brief. The server dedupes only while a
 *  run is in flight, so this collapses a double-click and a retry of the SAME
 *  brief - but a deliberate re-run after the first finishes still starts a new
 *  one, because the claim is released on `done`. A per-call random key (the
 *  old behaviour) made every retry a fresh paid run. */
function briefKey(text: string): string {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return `auth:${(h >>> 0).toString(36)}:${text.length}`;
}

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

/** A delivered file, drawn in place.
 *
 *  This used to be the server-rasterised page strip, which asked the gateway
 *  for a fixed sample of pages — twelve of a forty-page document, with the
 *  rest unreachable — and offered one zoom step and no text to select. pdf.js
 *  draws every page instead, lazily, and puts the words in the DOM. The local
 *  wrapper is gone too: `FilePreview` is the one that decides pdf.js / the
 *  browser's own text view / the raster pager, so Authoring, Research and the
 *  document window cannot disagree about what a format needs.
 */
const PDF_BOX = 'flex h-[70vh] min-h-[420px] flex-col overflow-hidden ' +
                'rounded-lg border border-white/10 bg-[#0b0b0f]';

export default function Authoring() {
  const mainId = useMainId();
  const active = useSectionActive();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  /* Focus is a page, not a view state, so a cold link, browser-back and the
     address bar all reach it. `ui.authoring_focus` is its switch: off means the
     route renders the ordinary Authoring screen and no Focus control appears —
     the same thread, the same data, nothing left half-built.
     Fail open on a flag read that throws: a feature silently disappearing
     because the console could not reach /api/flags is worse than the feature
     staying on one turn too long. */
  const [focusAllowed, setFocusAllowed] = useState(true);
  useEffect(() => {
    let live = true;
    api.flags().then((r: any) => {
      if (!live) return;
      const row = (r?.flags || []).find((f: any) => f.name === 'ui.authoring_focus');
      if (row) setFocusAllowed(row.value !== false);
    }).catch(() => { /* keep it on */ });
    return () => { live = false; };
  }, []);
  const focus = pathname === '/authoring/focus' && focusAllowed;
  const [brief, setBrief] = useState('');
  const [docStyle, setDocStyle] = useState('auto');
  const [running, setRunning] = useState(false);
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const [logs, setLogs] = useState<string[]>([]);
  /** Ticks while a run is live, for the per-node stopwatch. */
  const [now, setNow] = useState(() => Date.now());
  /** The displayed run's node list and sources, for the receipt. */
  const [runNodes, setRunNodes] = useState<RunNode[]>([]);
  const [runSources, setRunSources] = useState<SourceRef[]>([]);
  const [answer, setAnswer] = useState('');
  const [files, setFiles] = useState<MadeFile[]>([]);
  const [follow, setFollow] = useState(true);
  const [view, setView] = useState<View>('preview');
  const [history, setHistory] = useState<Run[]>([]);
  const [activeId, setActiveId] = useState('');
  /** the session window came back full, so older runs are still off-list. */
  const [windowFull, setWindowFull] = useState(false);
  /** the history read from the store failed; distinct from "no documents". */
  const [historyErr, setHistoryErr] = useState('');
  const [preview, setPreview] = useState<Record<string, DocPreview>>({});
  const [previewErr, setPreviewErr] = useState('');

  const ctlRef = useRef<AbortController | null>(null);
  /* False once the view is torn down, so the reattach poll and the SSE loop
     stop touching state after unmount. Reset to true on mount: React's
     StrictMode dev double-invoke runs the teardown once before the real mount. */
  const mountedRef = useRef(true);
  const reqRef = useRef(0);
  const sidRef = useRef('');
  const startRef = useRef<HTMLButtonElement>(null);
  const stopRef = useRef<HTMLButtonElement>(null);
  const logBox = useRef<HTMLDivElement>(null);
  /** When each node first appeared as running, for the live stopwatch. */
  const nodeStarts = useRef(new Map<string, number>());
  const resultRef = useRef<HTMLDivElement>(null);
  const [busyFile, setBusyFile] = useState('');
  /* The document on screen, opened as a window over the page. */
  const [viewer, setViewer] = useState<DocumentTarget | null>(null);
  /* The discussion thread about one produced file. */
  const [discDoc, setDiscDoc] = useState<MadeFile | null>(null);
  const [discMsgs, setDiscMsgs] = useState<{ role: 'user' | 'assistant'; text: string }[]>([]);
  const [discCid, setDiscCid] = useState('');
  /* The thread's identity is derived from the document, not minted by the
     server on the first turn. A random id meant a Discuss conversation lived
     only as long as the component: reload the page, reopen the same run, and
     the questions and answers were gone even though the server had stored
     every one of them. One thread per document is also what a reader expects
     when they come back to a paper they were discussing. */
  const discCidFor = (fileId?: string) => (fileId ? `disc-${fileId}` : '');
  /* Which gateway model answers here. Empty means "the gateway decides", which
     is the existing behaviour; a pick is remembered per document so a long
     discussion does not change model between visits. */
  const [discModel, setDiscModel] = useState('');
  const [discInput, setDiscInput] = useState('');
  const [discBusy, setDiscBusy] = useState(false);
  const [discNote, setDiscNote] = useState('');
  const [discErr, setDiscErr] = useState('');
  const discCtl = useRef<AbortController | null>(null);
  const discBox = useRef<HTMLDivElement>(null);
  /* ── what the open page is lighting up ────────────────────────────────
   * A discussion answers about a document the reader can see, so the words it
   * quotes are worth pointing at. `discFocus` is the answer whose citations are
   * lit — the newest by default, and whichever chip row was clicked otherwise.
   * Lit-then-quiet beats every turn of a long thread highlighted at once. */
  const [discFocus, setDiscFocus] = useState<number | null>(null);
  const [discActive, setDiscActive] = useState<{ id: string; nonce: number } | null>(null);
  const [discHits, setDiscHits] = useState<QuoteHit[]>([]);
  /* The Focus split: how much of the row the document keeps, as a percentage.
   * Dragged because reading a page and asking a one-line question want
   * different room, and a fixed 32% is a guess about which is happening now.
   * Persisted, so a layout once chosen survives the next run and a reload.
   * Only meaningful at xl and above, where the two panes sit side by side; the
   * stacked layout is the divider's own hiding place. */
  const FOCUS_SPLIT_KEY = 'aria.focus.split';
  const SPLIT_MIN = 25;
  const SPLIT_MAX = 85;
  const [focusSplit, setFocusSplit] = useState(() => {
    try {
      const v = Number(localStorage.getItem(FOCUS_SPLIT_KEY));
      return v >= SPLIT_MIN && v <= SPLIT_MAX ? v : 68;
    } catch {
      return 68;
    }
  });
  const focusRow = useRef<HTMLDivElement | null>(null);
  /* Tailwind's `xl`, where the two panes sit side by side. Below it the row
   * stacks, and a percentage width would squeeze the conversation into a strip
   * across a tall page — so the split is applied only while it means something.
   * Read from a matchMedia listener rather than the container width, because
   * the container is already at 100% either way and cannot tell them apart. */
  const [splitLive, setSplitLive] = useState(
    () => typeof window !== 'undefined'
      && window.matchMedia('(min-width: 1280px)').matches);
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1280px)');
    const on = (e: MediaQueryListEvent) => setSplitLive(e.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(FOCUS_SPLIT_KEY, String(focusSplit));
    } catch { /* a blocked storage must not block the layout */ }
  }, [focusSplit]);

  const startSplitDrag = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startPct = focusSplit;
    const move = (ev: PointerEvent) => {
      const rect = focusRow.current?.getBoundingClientRect();
      if (!rect || rect.width <= 0) return;
      const pct = startPct + ((ev.clientX - startX) / rect.width) * 100;
      setFocusSplit(Math.round(Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, pct))));
    };
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
  }, [focusSplit]);

  const discQuotes = useMemo<PdfQuote[]>(() => {
    if (discFocus == null) return [];
    const m = discMsgs[discFocus];
    if (!m || m.role !== 'assistant') return [];
    return answerCitations(m.text)
      .map((c, i) => ({ id: `q${discFocus}-${i}`, page: c.page, quote: c.quote }));
  }, [discFocus, discMsgs]);

  /** Where each lit quote was actually found, for the chip labels. */
  const hitByQuote = useMemo(() => {
    const m = new Map<string, QuoteHit>();
    for (const h of discHits) if (!m.has(h.id)) m.set(h.id, h);
    return m;
  }, [discHits]);

  const focusQuote = useCallback((id: string) => {
    setDiscActive({ id, nonce: Date.now() });
  }, []);

  const onQuoteHits = useCallback((hits: QuoteHit[]) => {
    setDiscHits((prev) => (prev.length === hits.length ? hits : hits.slice()));
  }, []);

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
    if (!active) return;
    const iv = window.setInterval(() => {
      for (const sid of Array.from(runningRef.current)) void loadProduced(sid);
    }, 4000);
    return () => window.clearInterval(iv);
  }, [active, loadProduced]);

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
      /* Read the files in parallel. A run that produced a PDF, a deck and a
       * sheet used to pay three sequential round-trips before the first
       * preview could appear. The full PDF bytes are deliberately NOT fetched
       * here: DocumentViewer rasterises them server-side, and the old eager
       * blob download doubled the transfer just to power an "open in a new
       * tab" button that is now lazy. */
      const results = await Promise.all(files.map(async (f) => {
        try {
          return [f.artifact, await api.artifactPreview(f.artifact), ''] as const;
        } catch (e) {
          return [f.artifact, null, `could not read ${f.filename}: ${(e as Error)?.message || e}`] as const;
        }
      }));
      if (!alive) return;
      const got: Record<string, DocPreview> = {};
      let firstError = '';
      for (const [id, preview, error] of results) {
        if (preview) got[id] = preview;
        if (error && !firstError) firstError = error;
      }
      setPreview(got);
      if (firstError) setPreviewErr(firstError);
    })();
    return () => { alive = false; };
  }, [files]);

  const loadPreviews = useCallback(async (fs: MadeFile[]) => {
    loadedRef.current = '';
    setFiles(fs);
  }, []);

  /* Teardown for the whole view: mark it unmounted (so the reattach poll below
     stops calling setState) and abort any open authoring stream - without this
     an SSE response outlived the view after a section switch. */
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      ctlRef.current?.abort();
    };
  }, []);

  /** Put a run's row on screen: its answer, its files and its log lines. The
   *  row is the single source, so a snapshot that lands AFTER the click (the
   *  files had never been read) still reaches the pane. */
  const showRun = useCallback((r: Run) => {
    setAnswer(r.answer || '');
    /* the streamed log is newer than the row's node rollup: never blank it */
    setLogs((cur) => (r.logs?.length ? r.logs : cur));
    setRunNodes(r.nodeList || []);
    setRunSources(r.sources || []);
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
      /* Bail if the view went away: this loop runs up to 900× at 2s (30 min),
         and without the guard it kept polling and calling setState long after
         unmount. */
      if (!mountedRef.current) return;
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
  const loadHistory = useCallback(async () => {
    try {
      const d = await api.sessions(SESSION_WINDOW);
      if (!mountedRef.current) return;
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
      setHistoryErr('');
    } catch (e) {
      /* Not swallowed. A failed read used to leave `history` empty and the
         sidebar then said "Nothing yet", so a paid document looked lost. */
      if (mountedRef.current) {
        setHistoryErr(String((e as Error)?.message || e));
      }
    }
  }, [prefetchProduced]);

  useEffect(() => { void loadHistory(); }, [loadHistory]);

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

  /* Live per-node stopwatch: tick only while a run is going and the log is
     visible, so the elapsed beside a running node moves without re-rendering
     the whole page every second otherwise. */
  useEffect(() => {
    if (!running || view !== 'log') return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running, view]);

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
    nodeStarts.current.clear(); setNow(Date.now());
    setRunNodes([]); setRunSources([]);
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
      // re-rendered, so `running` is still false. Stable per brief, not per
      // call: a retry after a dropped response must join the run it already
      // paid for instead of starting a second one.
      const idempotencyKey = briefKey(t);
      for await (const ev of api.chat(query, undefined, ctl.signal, {
        idempotencyKey,
        /* Declared, not inferred: this run is an authoring run, so it must be
           listed on the Authoring page from its first second and never in the
           Research sidebar. */
        runKind: 'authoring',
        docSetup: docStyle === 'auto' ? undefined : { style: docStyle },
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
          if (txt.trim()) {
            const parsed = parseLogLine(txt);
            if (parsed.kind === 'node' && parsed.id) {
              if (parsed.status === 'running'
                  && !nodeStarts.current.has(parsed.id)) {
                nodeStarts.current.set(parsed.id, Date.now());
              }
              // Upsert by node id: the running line is REPLACED by its
              // complete/failed line, so the log shows one row per node with
              // its final status and duration instead of a stale "running".
              setLogs((l) => {
                const idx = l.findIndex((x) => {
                  const p = parseLogLine(x);
                  return p.kind === 'node' && p.id === parsed.id;
                });
                if (idx < 0) return [...l.slice(-500), txt];
                if (l[idx] === txt) return l;
                const copy = l.slice();
                copy[idx] = txt;
                return copy;
              });
            } else {
              setLogs((l) => [...l.slice(-500), txt]);
            }
          }
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
          /* The done frame carries files but not the node rollup or the
           * sources; read /produced once so the receipt can show what the run
           * did and what it checked. */
          setRunNodes([]);
          setRunSources([]);
          if (sid) {
            void api.runProduced(sid).then((snap) => {
              setRunNodes(snap.nodes || []);
              setRunSources(snap.sources || []);
            }).catch(() => { /* the receipt still has the answer */ });
          }

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
  }, [brief, docStyle, running, saveRun]);

  /* Aborting the SSE stream only stops the CLIENT: the worker keeps running
   * and its spend keeps mounting (agent_server documents this). Cancel on
   * the server too. */
  const stop = () => {
    const sid = sidRef.current;
    ctlRef.current?.abort();
    setRunning(false);
    setErr('');
    if (!sid) {
      setNote('stopped.');
      requestAnimationFrame(() => startRef.current?.focus());
      return;
    }
    /* Cancelling is a server-side flag honoured at the next node boundary:
     * the stream can close before that, so keep the run marked live and keep
     * /produced polling. Without this the row stayed `live:true` forever and
     * reopening Authoring reattached to a run that had already stopped. */
    runningRef.current.add(sid);
    setNote('stopping after the current step...');
    void api.cancelRun({ session_id: sid })
      .then((res) => {
        if (!mountedRef.current) return;
        setNote(res.found
          ? 'stop requested - waiting for the current step to end'
          : 'stopped.');
        void loadProduced(sid);
      })
      .catch(() => {
        if (!mountedRef.current) return;
        setNote('stopped locally; the server did not acknowledge the cancel.');
        void loadProduced(sid);
      });
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
      const size = Math.max(1, Math.round(blob.size / 1024));
      setNote(`downloaded ${f.filename} (${size} KB)`);
      // The inline note is transient and the pane may have scrolled; a toast
      // is what confirms the download to a person who just clicked.
      toast.success('Downloaded', { description: `${f.filename} · ${size} KB` });
    } catch (e) {
      const msg = String((e as Error)?.message || e);
      setErr(`download failed: ${msg}`);
      toast.error('Download failed', { description: msg });
    } finally {
      setBusyFile('');
    }
  };

  /* ── discussing a document Aria produced ──────────────────────────────
   * A rendered file is not the end of the conversation. Every produced
   * artifact is indexed in the Documents library with its own chunks, so a
   * question about it can be answered from what is actually on disk — and the
   * thread is bound to exactly that document, so nothing else answers for it.
   *
   * `ensureDocument` is the self-healing half: a run that finished before the
   * filing existed, or one whose upload failed while the gateway was away,
   * still has its bytes. Indexing them now is the difference between "ask about
   * this" and a file the agent cannot read. */
  const ensureDocument = useCallback(async (f: MadeFile): Promise<string> => {
    if (f.document_id) return f.document_id;
    const r = await api.indexArtifact(f.artifact);
    const id = String(r?.document_id || '');
    if (!id) {
      throw new Error(r?.error
        || 'this file is not in Documents yet and could not be added');
    }
    setFiles((prev) => prev.map((p) => (
      p.artifact === f.artifact ? { ...p, document_id: id } : p)));
    setDiscDoc((d) => (d && d.artifact === f.artifact ? { ...d, document_id: id } : d));
    return id;
  }, []);

  const bindDiscussion = useCallback(async (f: MadeFile) => {
    setDiscErr('');
    setDiscNote(`reading ${f.filename}…`);
    try {
      const id = await ensureDocument(f);
      const d = await api.documentDetail(id);
      const doc = d?.document;
      if (!doc) {
        setDiscNote(`${f.filename} is bound to this thread. Ask about it.`);
        return;
      }
      if (doc.status !== 'ready') {
        setDiscNote(`${doc.filename} is still ${doc.status}. Ask now and Aria `
          + 'answers from the parts already read.');
      } else if (!doc.enabled) {
        await api.setDocumentEnabled(id, true);
        setDiscNote(`${doc.filename} was switched off for agent search. It is `
          + 'on now and bound to this thread.');
      } else {
        setDiscNote(`${doc.filename} is bound to this thread. Ask about it.`);
      }
    } catch (e) {
      setDiscErr(`could not read ${f.filename}: ${String((e as Error)?.message || e)}`);
      setDiscNote('');
    }
  }, [ensureDocument]);

  /** Read a stored discussion thread back into the panel. */
  const hydrateDiscussion = useCallback(async (cid: string) => {
    if (!cid) return;
    try {
      const d: any = await api.chatThread(cid);
      const msgs = Array.isArray(d?.messages) ? d.messages : [];
      const mapped = msgs
        .filter((m: any) => m && (m.role === 'user' || m.role === 'assistant'))
        .map((m: any) => ({
          role: m.role as 'user' | 'assistant',
          // A stored report/answer rides on `content`; a citation marker the
          // model emitted is part of the text and the highlighter reads it
          // from there, so it is kept rather than stripped.
          text: String(m.content ?? m.text ?? ''),
        }))
        .filter((m: any) => m.text.trim());
      // Only fill an empty panel: this runs on open, and a turn already in
      // flight must not be overwritten by the snapshot taken before it.
      if (mapped.length) {
        setDiscMsgs((cur) => (cur.length ? cur : mapped));
      }
    } catch {
      /* A thread that does not exist yet is the normal first case, and the
         panel is already empty. Nothing to say about it. */
    }
  }, []);

  const openDiscussion = useCallback((f: MadeFile) => {
    setView('discuss');
    setDiscFocus(null);
    setDiscActive(null);
    setDiscHits([]);
    if (discDoc?.artifact === f.artifact) return;
    discCtl.current?.abort();
    setDiscBusy(false);
    setDiscDoc(f);
    setDiscMsgs([]);
    // The thread's id is the document's, so the questions asked about a paper
    // are the same thread tomorrow. `''` here used to mean "let the server
    // mint one", which is why the panel opened empty every time.
    const cid = discCidFor(f.document_id || f.artifact || f.filename);
    setDiscCid(cid);
    setDiscModel(localStorage.getItem(`aria.disc.model.${cid}`) || '');
    void hydrateDiscussion(cid);
    void bindDiscussion(f);
  }, [bindDiscussion, discDoc]);

  const sendDiscussion = useCallback(async () => {
    const text = discInput.trim();
    if (!text || discBusy || !discDoc) return;
    setDiscInput('');
    setDiscErr('');
    let docId = discDoc.document_id || '';
    if (!docId) {
      try {
        docId = await ensureDocument(discDoc);
      } catch (e) {
        setDiscInput(text);
        setDiscErr(String((e as Error)?.message || e));
        return;
      }
    }
    setDiscMsgs((m) => [...m, { role: 'user', text }, { role: 'assistant', text: '' }]);
    /* The index this turn's answer will land at, so its citations can be lit
       the moment it finishes. */
    const assistantAt = discMsgs.length + 1;
    setDiscBusy(true);
    setDiscFocus(assistantAt);
    setDiscActive(null);
    setDiscHits([]);
    const ac = new AbortController();
    discCtl.current = ac;
    const setLast = (value: string) => setDiscMsgs((m) => {
      const out = [...m];
      const last = out[out.length - 1];
      if (last && last.role === 'assistant') {
        out[out.length - 1] = { ...last, text: value };
      }
      return out;
    });
    let answer = '';
    let cid = discCid;
    try {
      for await (const ev of api.chatSimpleStream(
        text, cid || undefined, ac.signal, discModel || undefined,
        { docIds: [docId], cite: true },
      )) {
        if (ev.type === 'started' && ev.conversation_id) {
          cid = String(ev.conversation_id);
          setDiscCid(cid);
          // Persist the binding on the thread, not just this turn. A body value
          // is authoritative for the turn it belongs to, so without this the
          // conversation forgets what it is about the moment it is opened in
          // Chat tomorrow. Best-effort: the turn is already streaming.
          void api.setChatThreadPrefs(cid, true, undefined, [docId]);
        } else if (ev.type === 'delta' && ev.text) {
          answer += String(ev.text);
          setLast(answer);
        } else if (ev.type === 'status' && ev.text) {
          setDiscNote(String(ev.text));
        } else if (ev.type === 'done') {
          if (ev.conversation_id) {
            cid = String(ev.conversation_id);
            setDiscCid(cid);
          }
          // The streamed deltas and the final answer are the same text; trust
          // `answer` when it is there, because a delta can be dropped by a
          // back-pressured queue while the persisted answer cannot.
          const final = String(ev.answer || '');
          if (final) setLast(final);
          setDiscNote('');
          // Walk the reader to the first thing this answer actually quoted.
          // Waiting for the whole text matters: a marker is only a citation
          // once its closing bracket has arrived.
          const cited = answerCitations(final || answer);
          if (cited.length) {
            setDiscActive({ id: `q${assistantAt}-0`, nonce: Date.now() });
          }
        } else if (ev.type === 'error') {
          setDiscErr(String(ev.text || 'Aria could not answer this turn'));
        }
      }
    } catch (e) {
      if (!ac.signal.aborted) {
        setDiscErr(String((e as Error)?.message || e));
      }
    } finally {
      setDiscBusy(false);
      discCtl.current = null;
    }
  }, [discBusy, discCid, discDoc, discInput, discMsgs.length, ensureDocument]);

  const stopDiscussion = useCallback(() => {
    discCtl.current?.abort();
    discCtl.current = null;
    setDiscBusy(false);
  }, []);

  /** Open a produced file in the document window. */
  const openDocument = useCallback((f: MadeFile) => {
    setViewer({
      kind: 'artifact', artifact: f.artifact,
      format: f.format || 'pdf', label: f.filename,
    });
  }, []);
  /* Which of this run's files the window is showing, when it came from the
     list. That is what lets Download and Discuss work inside the window. */
  const viewerFile = viewer && viewer.kind === 'artifact'
    ? files.find((f) => f.artifact === viewer.artifact)
    : undefined;

  /* A different run is a different set of documents; do not leave the previous
     one's thread bound to a file that is no longer on screen. */
  useEffect(() => {
    discCtl.current?.abort();
    setDiscDoc(null);
    setDiscMsgs([]);
    setDiscCid('');
    setDiscNote('');
    setDiscErr('');
    setDiscBusy(false);
    setDiscFocus(null);
    setDiscActive(null);
    setDiscHits([]);
    setDiscModel('');
  }, [activeId]);

  /* The Discuss workspace is only useful with a document on the page. Opening
     it from the tab bar, or arriving at /authoring/focus directly, picks the
     run's first file, so it never arrives as an empty chat next to an empty
     pane. */
  useEffect(() => {
    /* A cold /authoring/focus deep link has never seen the tab strip, so
       `view` is still 'preview'. Set it to 'discuss' on the way in and leaving
       Focus lands back on the workspace rather than on a tab the user was never
       on. Setting the value it already holds is a no-op in React. */
    if (focus) setView('discuss');
    if ((view === 'discuss' || focus) && !discDoc && files.length) openDiscussion(files[0]);
  }, [view, focus, discDoc, files, openDiscussion]);

  useEffect(() => {
    if ((view === 'discuss' || focus) && discBox.current) {
      discBox.current.scrollTop = discBox.current.scrollHeight;
    }
  }, [discMsgs, view, focus]);
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
  const tabEnabled = (t: View) => (t === 'preview' || t === 'discuss')
    ? files.length > 0 : true;
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

  /* Rendered as a plain call, not a component: this body closes over the run's
   * state, so defining it as a component would give it a new identity on every
   * Authoring render. React then unmounts the whole preview, and the viewer
   * re-downloads the document and snaps back to fit-width mid-zoom. */
  const renderPreview = (f: MadeFile) => {
    /* A PDF does not need `artifactPreview` to be drawable: DocumentViewer
     * rasterises the artifact itself and reports its own page count. Waiting
     * for pypdf first put an avoidable round-trip in front of the first page,
     * so the viewer opens immediately and the metadata (warnings, subtitle)
     * fills in when it arrives. Other formats still need their extracted
     * slides/sheets/paragraphs before they can render. */
    const p = preview[f.artifact]
      ?? (f.format === 'pdf' ? { kind: 'pdf' } as DocPreview : null);
    if (!p) {
      return (
        <div key={f.artifact} className="flex h-full items-center justify-center p-6 text-[12.5px] text-zinc-muted">
          <Loader2 size={14} className="mr-2 animate-spin" /> reading {f.filename}…
        </div>
      );
    }
    const warns = p.warnings || [];

    return (
      <div key={f.artifact} className="space-y-3 p-3.5">
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
          <FilePreview target={{
            kind: 'artifact', artifact: f.artifact,
            format: f.format || 'pdf', label: f.filename,
          }} className={PDF_BOX} />
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

  /* One tab strip, drawn by the normal page and by the Discuss workspace alike:
   * a discussion is a view of the same run, not a different place, and leaving
   * it must be the same single click as leaving any other tab.
   *
   * A function returning JSX, not a component: a nested component type is
   * re-created on every parent render, which would remount the PDF and redraw
   * every page each time a message arrived. */
  const tabStrip = () => (!hasOutput && !running ? null : (
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
            disabled={!tabEnabled(id)}
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
      <Pane id="authoring.pills">
        {totalWarnings > 0 && <Pill tone="warn">{totalWarnings} warning</Pill>}
        {files.length > 0 && (
          <Pill tone="ok">{files.length} file{files.length === 1 ? '' : 's'}</Pill>
        )}
      </Pane>
      {/* The way out of Discuss lives here, not in the Documents rail. That
          rail is a pane the user may fold away, and an exit that disappears
          along with the thing it exits from is how a view becomes a cage.
          Focus has its own exit in the document header, because on that page
          there is no strip to put one in. */}
      {view === 'discuss' && (
        <button type="button" onClick={() => setView('receipt')}
                className="flex flex-none items-center gap-1.5 rounded border border-white/10
                           px-2 py-1 text-[11px] text-zinc-400 hover:border-violet-400
                           hover:text-zinc-100">
          <ArrowLeft size={12} /> Back to the run
        </button>
      )}
    </div>
  ));

  /* Memoised: a fresh object identity on every render made FilePreview reload
     the document on every stream delta — the visible blink in focus mode. */
  const discTarget = useMemo<DocumentTarget | null>(() => discDoc ? {
    kind: 'artifact', artifact: discDoc.artifact,
    format: discDoc.format || 'pdf', label: discDoc.filename,
  } : null, [discDoc?.artifact, discDoc?.format, discDoc?.filename]);
  const discussing = (view === 'discuss' || focus) && !!discDoc && !!discTarget;

  /** The words Aria pointed at, as chips under her answer.
   *
   *  A chip says where the quote was *found*, which is not always where she
   *  said it was: the page a chunk carried can disagree with the page the model
   *  remembered. Saying "on p.5" instead of quietly highlighting page 5 under a
   *  "p.3" label is the difference between a citation you can check and one you
   *  have to take on trust. */
  const chipsFor = (idx: number, text: string) => {
    const cs = answerCitations(text);
    if (!cs.length) return null;
    return (
      <div className="mt-1 flex flex-wrap items-center gap-1">
        {cs.map((c, i) => {
          const id = `q${idx}-${i}`;
          const hit = hitByQuote.get(id);
          const label = c.page ? `p.${c.page}` : 'in this document';
          const where = !hit ? ''
            : hit.page !== c.page ? ` · found on p.${hit.page}` : '';
          return (
            <button key={id} type="button"
                    title={c.quote}
                    onClick={() => { setDiscFocus(idx); focusQuote(id); }}
                    className="flex max-w-[240px] items-center gap-1 rounded-full border border-white/10 px-2 py-0.5 text-[10.5px] text-zinc-400 hover:border-violet-400 hover:text-zinc-100">
              <span className="flex-none font-semibold text-amber-200/90">{label}{where}</span>
              <span className="min-w-0 truncate">“{c.quote}”</span>
            </button>
          );
        })}
      </div>
    );
  };

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      {/* Focus strips the page down to the document and Aria: no section rail,
          no top bar, no tab strip, no pills, no run list. The way out is one
          button in the document header, and it goes back to /authoring with
          everything else as it was — the same thread, the same scroll, the same
          document. */}
      {!focus && <Rail />}
      <main id={mainId} tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        {!focus && (
        <TopBar crumb="Authoring" panes="authoring">
          <span className="text-[11.5px] text-zinc-muted">
            {running ? 'building…' : note || 'Ctrl+Enter to build'}
          </span>
        </TopBar>
        )}

        {/* The Setup card is gone. It asked for format, paper, typeface,
            length, orientation, columns, margins, citations and three
            toggles - eleven decisions before you had even described the
            document, and most of them were things the brief already implied.
            Removing it means the run reads the brief and picks the format,
            page and structure itself, which is what the user was asking for
            when they said the document should be right without them having to
            specify it. The `doc_setup` plumbing is untouched on the server, so
            a forced setup still works for callers that send one. */}
        {focus && !discussing ? (
          /* `/authoring/focus` reached with nothing to read — the run has not
             produced a document yet, or the URL was opened before the session
             came back. The alternative is the ordinary chrome-less page, which
             looks like a broken layout rather than an empty one. */
          <div className="flex min-h-0 flex-1 items-center justify-center p-6">
            <div className="max-w-[440px] text-center">
              <Maximize2 size={18} className="mx-auto mb-2 text-zinc-600" />
              <p className="text-[12.5px] text-zinc-400">
                {activeId
                  ? 'This run has not produced a document to read yet.'
                  : 'Focus reads one document beside the conversation about it — open a finished run first.'}
              </p>
              <button type="button" onClick={() => navigate('/authoring')}
                      className="mt-3 inline-flex items-center gap-1.5 rounded border border-white/15
                                 px-2.5 py-1 text-[11.5px] text-zinc-300 hover:border-violet-400">
                <ArrowLeft size={12} /> Back to Authoring
              </button>
            </div>
          </div>
        ) : discussing && discDoc && discTarget ? (
          /* ── the Discuss workspace, and the Focus page ───────────────
           * The document, the words about it, and nothing else. A discussion
           * is read with the page open beside it, so the brief, the run list
           * and the file rows' buttons all step out of the way.
           *
           * Discuss keeps the run's document rail (a multi-file run needs a way
           * to switch) and the tab strip. Focus is the same thread on its own
           * page with both gone, because one document on screen does not need a
           * list of one document and a reader working through a forty-page
           * report wants the width. Same messages, same binding, so moving
           * between the two never starts a new conversation. */
          <div className="flex min-h-0 flex-1 flex-col gap-2 p-2.5">
            {!focus && tabStrip()}
            <div ref={focusRow}
                 className="flex min-h-0 flex-1 flex-col gap-2.5 xl:flex-row">
              {view === 'discuss' && !focus && (
              <Pane id="authoring.discussRail">
                <nav aria-label="Documents from this run"
                     className="flex w-full min-h-0 shrink-0 flex-col gap-1.5 rounded-[10px] border border-white/10 bg-[#0e0e12] p-2 max-md:max-h-[30vh] xl:w-[218px]">
                  <h2 className="flex items-center gap-1.5 px-1 text-[11px] font-bold uppercase tracking-[0.12em] text-zinc-muted">
                    <FileText size={12} /> Documents
                  </h2>
                  <ul className="min-h-0 flex-1 space-y-1 overflow-y-auto">
                    {files.map((f) => {
                      const Icon = KIND_ICON[f.format] || FileDown;
                      const on = f.artifact === discDoc.artifact;
                      return (
                        <li key={f.artifact}>
                          <button type="button"
                                  onClick={() => openDiscussion(f)}
                                  aria-current={on ? 'true' : undefined}
                                  title={f.filename}
                                  className={`w-full rounded-lg px-2 py-1.5 text-left hover:bg-white/5 ${
                                    on ? 'bg-violet-400/10 ring-1 ring-violet-400/40' : ''}`}>
                            <span className="flex items-center gap-1.5">
                              <Icon size={12} className="flex-none text-violet-300" />
                              <span className="min-w-0 flex-1 truncate text-[12px] font-semibold">
                                {f.filename}
                              </span>
                            </span>
                            <span className="mt-0.5 block truncate text-[10.5px] text-zinc-500">
                              {fileMeta(f).join(' · ') || f.format}
                            </span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </nav>
              </Pane>
              )}

              <section aria-label="The document"
                       className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[10px] border border-white/10 bg-[#08080a]">
                <header className="flex flex-none items-center gap-2 border-b border-white/10 px-2.5 py-1.5">
                  <FileText size={13} className="flex-none text-zinc-muted" />
                  <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold"
                        title={discDoc.filename}>
                    {discDoc.filename}
                  </span>
                  {discDoc.format && <Pill tone="info">{discDoc.format}</Pill>}
                  {discDoc.document_id && <Pill tone="ok">in Documents</Pill>}
                  {/* Which gateway model answers this document. The roster comes
                      from the gateway itself, so a model added there is pickable
                      here without a console change. The pick is remembered per
                      document, because a discussion that changes model halfway
                      through reads like two different people answering.
                      Shown in Focus too: the mode hides the rail, the run list
                      and the tab strip — furniture around the reading — but the
                      model answering the question is not furniture, and a reader
                      who cannot see or set it has no idea which model a passage
                      was reasoned with. */}
                  <ModelPicker model={discModel} busy={discBusy} applyDefault={false}
                               label="Model for this discussion"
                               onChange={(id) => {
                                 setDiscModel(id);
                                 try {
                                   if (id) localStorage.setItem(`aria.disc.model.${discCid}`, id);
                                   else localStorage.removeItem(`aria.disc.model.${discCid}`);
                                 } catch { /* a blocked storage never blocks a reply */ }
                               }} />
                  {/* Focus's entry and exit live on the document itself, not in
                      the strip it hides: on the Focus page there is no tab strip
                      to put a button in, and an exit that only exists in the
                      thing being exited is how a mode becomes a cage. */}
                  {focusAllowed && (
                    <button type="button"
                            onClick={() => navigate(focus ? '/authoring' : '/authoring/focus')}
                            title={focus ? 'Leave Focus and go back to the run'
                                         : 'Focus — only this document and Aria'}
                            className="flex flex-none items-center gap-1 rounded border border-white/10
                                       px-1.5 py-0.5 text-[11px] text-zinc-400
                                       hover:border-violet-400 hover:text-zinc-100">
                      {focus ? <ArrowLeft size={11} /> : <Maximize2 size={11} />}
                      {focus ? 'Exit focus' : 'Focus'}
                    </button>
                  )}
                </header>
                <FilePreview target={discTarget}
                             quotes={discQuotes} active={discActive}
                             onHits={onQuoteHits}
                             className="flex min-h-0 flex-1 flex-col"
                             rasterClassName="min-h-0 flex-1 overflow-y-auto" />
              </section>

              {/* The split between the page and the conversation, Focus only.
                  A separator role with arrow keys, because a drag a keyboard
                  user cannot reach is a control that does not exist for them. */}
              {focus && (
                <div role="separator" aria-orientation="vertical"
                     aria-label="Resize the document and the conversation"
                     aria-valuenow={focusSplit} aria-valuemin={SPLIT_MIN}
                     aria-valuemax={SPLIT_MAX}
                     tabIndex={0}
                     onPointerDown={startSplitDrag}
                     onKeyDown={(e) => {
                       const step = e.shiftKey ? 10 : 2;
                       if (e.key === 'ArrowLeft') {
                         e.preventDefault();
                         setFocusSplit((v) => Math.max(SPLIT_MIN, v - step));
                       } else if (e.key === 'ArrowRight') {
                         e.preventDefault();
                         setFocusSplit((v) => Math.min(SPLIT_MAX, v + step));
                       }
                     }}
                     className="hidden h-1.5 flex-none cursor-col-resize self-stretch
                                rounded-full border border-white/10 bg-white/5
                                transition-colors hover:border-violet-400/70 hover:bg-violet-400/20
                                focus-visible:border-violet-400 focus-visible:outline-none
                                xl:block xl:h-auto xl:w-1.5 xl:self-stretch"
                     title={`${focusSplit}% document · ${100 - focusSplit}% conversation`}
                />
              )}

              {/* On the Focus page there is no tablist, so this must not claim
                  to be a tabpanel — an orphan `aria-labelledby` points at a tab
                  that is not on the page. */}
              <section aria-label="Discussion with Aria"
                       {...(focus ? {} : { role: 'tabpanel', id: `panel-${view}`,
                                           'aria-labelledby': `tab-${view}` })}
                       tabIndex={-1}
                       className={`flex w-full min-h-0 shrink-0 flex-col rounded-[10px] border border-white/10 bg-[#0e0e12] max-md:max-h-[60vh] ${
                         /* Focus gives the extra width to the page, which is
                            the whole point of the mode — and the reader decides
                            how much, so this is a percentage off the slider
                            rather than a fixed 32%. `shrink-0` above already
                            keeps it from collapsing; the inline width is the
                            only place the split is expressed, so the class list
                            and the style can never disagree. */
                         focus ? '' : 'xl:w-[430px] xl:max-w-[46%]'}`}
                       style={focus && splitLive
                         ? { width: `${100 - focusSplit}%`, maxWidth: 'none' }
                         : undefined}>
                <div className="flex flex-none items-center gap-2 border-b border-white/10 px-3 py-2">
                  <MessageSquareText size={13} className="flex-none text-violet-300" />
                  <span className="min-w-0 flex-1 truncate text-[12px] font-semibold">
                    About {discDoc.filename}
                  </span>
                  {discBusy && (
                    <Loader2 size={12} className="flex-none animate-spin text-zinc-muted" />
                  )}
                </div>
                {discNote && (
                  <p role="status"
                     className="flex-none border-b border-white/10 bg-white/5 px-3 py-1.5 text-[10.5px] text-zinc-400">
                    {discNote}
                  </p>
                )}
                <div ref={discBox} className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
                  {!discMsgs.length && !discBusy && (
                    <div className="space-y-2">
                      <p className="text-[12.5px] text-zinc-400">
                        Ask about anything in this document. Aria reads the copy
                        the run filed in your Documents library — its own chunks,
                        not a re-search of the web — and when she quotes it, the
                        words light up on the page beside her.
                      </p>
                      <div className="flex flex-wrap gap-1.5 pt-1">
                        {[
                          'Summarise what this document concludes',
                          'Which claims here are weakest?',
                          'Explain the numbers in the comparison',
                          'What is missing that belongs in it?',
                        ].map((s) => (
                          <button key={s} type="button"
                                  disabled={discBusy}
                                  onClick={() => setDiscInput(s)}
                                  className="rounded-full border border-white/10 px-2.5 py-1 text-[11px] text-zinc-400 hover:border-violet-400 hover:text-zinc-200 disabled:opacity-40">
                            {s}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                  {discMsgs.map((m, i) => (
                    <div key={i} className={m.role === 'user'
                      ? 'ml-auto max-w-[85%] rounded-lg bg-violet-400/15 px-2.5 py-1.5 text-[12px] text-zinc-100'
                      : 'mr-auto max-w-[96%] text-[12.5px] text-zinc-200'}>
                      {m.role === 'assistant' ? (
                        m.text ? (
                          <>
                            <div className="prose-chat"
                              dangerouslySetInnerHTML={{ __html: renderMarkdown(m.text, 1, true) }} />
                            {chipsFor(i, m.text)}
                          </>
                        ) : (
                          <p className="flex items-center gap-2 text-[12px] text-zinc-muted">
                            <Loader2 size={12} className="animate-spin" /> reading the document…
                          </p>
                        )
                      ) : m.text}
                    </div>
                  ))}
                  {discErr && (
                    <p role="alert" className="text-[11.5px] text-red-200">{discErr}</p>
                  )}
                </div>
                <div className="flex flex-none items-end gap-2 border-t border-white/10 p-2.5">
                  <textarea
                    value={discInput}
                    onChange={(e) => setDiscInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        void sendDiscussion();
                      }
                    }}
                    rows={2}
                    aria-label="Ask about this document"
                    placeholder="Ask about this document… (Enter sends, Shift+Enter adds a line)"
                    className="min-h-[42px] flex-1 resize-none rounded-md border border-white/10 bg-[#08080a] px-2.5 py-2 text-[12.5px] outline-none focus:border-violet-400"
                  />
                  {discBusy ? (
                    <button type="button" onClick={stopDiscussion}
                            className="flex flex-none items-center gap-1 rounded-md border border-red-400/40 px-2.5 py-2 text-[11.5px] font-semibold text-red-200">
                      Stop
                    </button>
                  ) : (
                    <button type="button" onClick={() => void sendDiscussion()}
                            disabled={!discInput.trim()}
                            className="flex flex-none items-center gap-1 rounded-md bg-violet-400/20 px-2.5 py-2 text-[11.5px] font-bold text-zinc-100 disabled:opacity-40">
                      <Send size={12} /> Send
                    </button>
                  )}
                </div>
              </section>
            </div>
          </div>
        ) : (
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
              <div className="mt-2 flex flex-wrap items-center gap-2 text-[10.5px] text-zinc-500">
                <label htmlFor="doc-style" className="font-semibold uppercase tracking-wide">
                  Style
                </label>
                <select id="doc-style" value={docStyle}
                  onChange={(e) => setDocStyle(e.target.value)}
                  className="rounded-md border border-white/10 bg-black/40 px-2 py-1 text-[11px] text-zinc-200 outline-none focus:border-violet-400">
                  <option value="auto">Auto — Aria picks from the brief</option>
                  <optgroup label="Study & course material">
                    <option value="lecture">Lecture — centred masthead, colour heads</option>
                    <option value="exambook">Exam book — ruled masthead, red heads</option>
                    <option value="coursework">Coursework — navy cover band</option>
                  </optgroup>
                  <optgroup label="Business & reference">
                    <option value="report">Report</option>
                    <option value="brief">Brief</option>
                    <option value="memo">Memo</option>
                    <option value="whitepaper">Whitepaper</option>
                    <option value="manual">Manual</option>
                    <option value="technical">Technical</option>
                    <option value="newsletter">Newsletter (2-col)</option>
                  </optgroup>
                  <optgroup label="Book-like">
                    <option value="academic">Academic</option>
                    <option value="book">Book</option>
                    <option value="plain">Plain</option>
                  </optgroup>
                </select>
                <span className="text-zinc-600">
                  Layout and visual identity, not just type; Auto reads the brief.
                </span>
              </div>
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
                <Pane id="authoring.suggestions">
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
                </Pane>
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
                {historyErr && (
                  <p className="p-2 text-[11.5px] text-amber-300/90">
                    couldn&apos;t load documents from the store ({historyErr}).{' '}
                    <button
                      onClick={() => void loadHistory()}
                      className="underline"
                    >
                      retry
                    </button>
                  </p>
                )}
                {history.length === 0 && !historyErr ? (
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
            {tabStrip()}

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
                        {/* What was actually delivered, and what the run cost.
                            *  A 9-word "document" and a 1,900-word one look
                            *  identical in a file list otherwise. */}
                        <span className="mt-0.5 block text-[10.5px] text-zinc-500">
                          {fileMeta(f).map((bit, i) => (
                            <span key={bit}>
                              {i > 0 && <span className="text-zinc-700"> · </span>}
                              {bit}
                            </span>
                          ))}
                        </span>
                      </span>
                      {f.format && <Pill tone="info">{f.format}</Pill>}
                      {/* Open is the deliverable: the browser's own document
                          viewer, in a window over the page. */}
                      <button
                        type="button"
                        onClick={() => openDocument(f)}
                        aria-label={`Open ${f.filename}`}
                        title="Open the document"
                        className="flex flex-none items-center gap-1 rounded-md border border-white/10 px-2 py-1 text-[11px] font-semibold hover:border-violet-400"
                      >
                        <Maximize2 size={12} /> Open
                      </button>
                      <button
                        type="button"
                        onClick={() => openDiscussion(f)}
                        aria-label={`Discuss ${f.filename} with Aria`}
                        title="Talk to Aria about this document"
                        className="flex flex-none items-center gap-1 rounded-md border border-white/10 px-2 py-1 text-[11px] font-semibold hover:border-violet-400"
                      >
                        <MessageSquareText size={12} /> Discuss
                      </button>
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
                  className="p-3 font-mono text-[12.5px] leading-relaxed"
                >
                  {logs.map((l, i) => {
                    const p = parseLogLine(l);
                    if (p.kind === 'node') {
                      const isRunning = p.status === 'running';
                      const elapsed = isRunning
                        ? fmtElapsed(now - (nodeStarts.current.get(p.id || '') ?? now))
                        : p.duration;
                      const Icon = p.status === 'complete' ? Check
                        : p.status === 'failed' ? X
                        : p.status === 'skipped' ? SkipForward : Clock;
                      const tone = p.status === 'complete' ? 'text-emerald-300'
                        : p.status === 'failed' ? 'text-red-300'
                        : p.status === 'skipped' ? 'text-zinc-500'
                        : 'text-amber-300';
                      return (
                        <div key={i} className="flex items-start gap-2">
                          <Icon size={13} className={tone + ' mt-0.5 flex-none'} />
                          <span className={tone + ' min-w-0 flex-1 whitespace-pre-wrap break-words'}>
                            <span className="font-semibold">{p.skill}</span>
                            {isRunning && p.detail ? (
                              <span className="text-zinc-500"> · {p.detail.slice(0, 96)}</span>
                            ) : null}
                            {p.status === 'failed' && p.detail ? (
                              <span className="text-red-300/80"> · {p.detail.slice(0, 120)}</span>
                            ) : null}
                          </span>
                          {elapsed ? (
                            <span className="flex-none tabular-nums text-zinc-500">{elapsed}</span>
                          ) : null}
                        </div>
                      );
                    }
                    return (
                      <div
                        key={i}
                        className={
                          'whitespace-pre-wrap break-words '
                          + (p.kind === 'error' ? 'text-red-300/90' : 'text-zinc-400')
                        }
                      >
                        {l}
                      </div>
                    );
                  })}
                  {!logs.length && <div className="text-zinc-muted">no output yet</div>}
                  <label className="sticky bottom-0 mt-2 block bg-[#08080a] pt-1 text-[11px] text-zinc-500">
                    <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow
                  </label>
                </div>
              ) : view === 'discuss' ? (
                <div role="tabpanel" id={`panel-${view}`} aria-labelledby={`tab-${view}`}
                     tabIndex={-1} className="p-4 text-[12.5px] text-zinc-muted">
                  This run has no document to discuss yet.
                </div>
              ) : view === 'preview' ? (
                <div role="tabpanel" id="panel-preview" aria-labelledby="tab-preview" tabIndex={-1}>
                  {files.map((f) => renderPreview(f))}
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
                    >
                      <RunSummary nodes={runNodes} sources={runSources} />
                      <div dangerouslySetInnerHTML={{ __html: renderMarkdown(answer, 1, true) }} />
                    </article>
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
        )}
      </main>

      {/* The document window: centred, with the console blurred behind it —
          the same shape Chat's "read full report" uses, because a document
          deserves to arrive that way. A PDF opens in the browser's own viewer,
          so page scroll, selection, search and zoom are the real ones. */}
      <DocumentModal
        open={!!viewer}
        target={viewer}
        onClose={() => setViewer(null)}
        onDownload={viewerFile ? () => void downloadFile(viewerFile) : undefined}
        onDiscuss={viewerFile ? () => {
          const f = viewerFile;
          setViewer(null);
          openDiscussion(f);
        } : undefined}
      />
    </div>
  );
}
