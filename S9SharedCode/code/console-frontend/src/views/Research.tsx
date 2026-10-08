import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Search, FlaskConical } from 'lucide-react';
import DagCanvas from '../components/DagCanvas';
import Inspector from '../components/Inspector';
import { isTerminal, renderMarkdown } from '../components/markdown';
import { Rail, TopBar, Pill, useHealth, SkipLink } from '../components/ui';
import { DocumentViewer } from './DocSetup';
import { api, headers, briefFor, displayTopic, ago, CONF, type GraphPayload, type GraphNode, type SessionSummary } from '../api';

type Depth = 'quick' | 'standard' | 'deep';
type CanvasView = 'graph' | 'report' | 'log';

/** Pull readable markdown out of a formatter node's stored output
 *  ({final_answer} today; tolerant of older shapes). */
function extractReport(out: unknown): string {
  if (typeof out === 'string') return unwrapAnswer(out);
  if (out && typeof out === 'object') {
    const o = out as Record<string, unknown>;
    for (const k of ['final_answer', 'report', 'markdown', 'content', 'text', 'answer']) {
      if (typeof o[k] === 'string' && (o[k] as string).trim()) return unwrapAnswer(o[k] as string);
    }
  }
  return '';
}

/** Recover plain markdown from a model reply that still wears its
 *  transport wrapper. Three shapes occur in practice, all observed live:
 *    1. fenced:        ```json\n{"final_answer": "…"}\n```
 *    2. closed object:  {"final_answer": "…"}
 *    3. UNTERMINATED:   {"final_answer": "…      (no closing brace)
 *  Left alone, shape 1 made renderMarkdown treat the ENTIRE report as one
 *  code block — headings, lists and links all vanished — and shape 3
 *  printed raw JSON as the report body. The server strips this on the
 *  sectioned path (`skills._strip_answer_envelope`) but a single-call
 *  formatter or an older stored run still ships the wrapper, so the
 *  client normalises too. Belt and braces: the user must never see JSON. */
function unwrapAnswer(s: string): string {
  let t = (s || '').trim();
  if (!t) return '';
  // Strip the opening and closing fences INDEPENDENTLY. The old regex
  // required both, so an envelope whose closing fence was missing (a stored
  // 4,597-char `final_answer` starting "```json" and ending mid-string) fell
  // through every branch: the fence regex missed, `startsWith('{')` was false
  // because the text began with backticks, and the bare-fence regex missed
  // too - so the whole report rendered as one code block of raw JSON, which
  // is precisely what this function exists to prevent.
  t = t.replace(/^```(?:json|markdown|md)?[ \t]*\r?\n?/i, '');
  t = t.replace(/```\s*$/, '');
  t = t.trim();
  if (t.startsWith('{')) {
    try {
      const obj = JSON.parse(t) as Record<string, unknown>;
      for (const k of ['final_answer', 'answer', 'report', 'text', 'content', 'markdown']) {
        const v = obj?.[k];
        if (typeof v === 'string' && v.trim()) return v.trim();
      }
    } catch {
      // Unterminated object: take everything after the first colon.
      const m = /^\{\s*"(?:final_answer|answer|report|text|content|markdown)"\s*:\s*"([\s\S]*)$/.exec(t);
      if (m) {
        let body = m[1].replace(/["'}\s,]+$/, '');
        // The value still carries escaped newlines from the JSON string.
        body = body.replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\"/g, '"');
        if (body.trim()) return body.trim();
      }
      return '';
    }
  }
  // A bare fence (no envelope) around real markdown.
  const bare = /^```(?:markdown|md)?[ \t]*\r?\n([\s\S]*?)\r?\n?```\s*$/i.exec(t);
  if (bare) return bare[1].trim();
  return t;
}

export default function Research() {
  const gwUp = useHealth();
  const [topics, setTopics] = useState<SessionSummary[]>([]);
  const [topicsError, setTopicsError] = useState('');
  const [filter, setFilter] = useState('');
  const [brief, setBrief] = useState('');
  const [depth, setDepth] = useState<Depth>('standard');
  const [topic, setTopic] = useState('');
  const [sid, setSid] = useState('');
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState<'idle' | 'running' | 'done' | 'error'>('idle');
  const [note, setNote] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [answer, setAnswer] = useState('');
const [files, setFiles] = useState<{ artifact: string; filename: string; format: string }[]>([]);
const [viewFile, setViewFile] = useState<string | null>(null);
  const [view, setView] = useState<CanvasView>('graph');
  /* Live reasoning log. The server streams a `log` frame per orchestrator
     stdout line, and this view used to DISCARD them all — so a five-minute
     run showed a static canvas and a heartbeat, with no sense of what the
     agent was actually doing. Kept capped; auto-followed while live. */
  const [logs, setLogs] = useState<string[]>([]);
  const [logFollow, setLogFollow] = useState(true);
  const logBoxRef = useRef<HTMLDivElement | null>(null);
  const pollRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const reqRef = useRef(0);
  const t0 = useRef(0);
  // Set by the Stop button so the abort path keeps "stopped." instead of
  // overwriting it with the generic "cancelled." a moment later.
  const stoppedRef = useRef(false);
  // Focus handoff for the Start/Stop pair: both buttons stay
  // mounted and the inactive one is disabled, so clicking one
  // disables it — focus moves to the button that just became the
  // live action instead of being thrown back to <body>.
  const stopBtnRef = useRef<HTMLButtonElement | null>(null);
  const startBtnRef = useRef<HTMLButtonElement | null>(null);
  // Post-stop catch-up poll (see stop()): the server skips the remaining
  // nodes a moment AFTER the cancel lands, so the canvas needs a few extra
  // fetches to show the partial graph instead of the pre-cancel frame.
  const catchUpRef = useRef<number | null>(null);
  // Conversation continuity across runs on this page (see the `started`
  // branch in start()).
  const cidRef = useRef('');
  const loc = useLocation();
  // Runs' Inspector "Run again" hands the original query over as state, so
  // re-running a run from its own graph no longer lands on an empty box.
  useEffect(() => {
    const b = (loc.state as { brief?: string } | null)?.brief;
    if (typeof b === 'string' && b.trim()) setBrief(b.trim());
  }, [loc.state]);
  // Monotonic counter so a slow graph response can never overwrite a newer
  // one, and so post-unmount writes are dropped.
  const pollSeq = useRef(0);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; pollSeq.current++; }, []);

  const refreshTopics = useCallback(async () => {
    try {
        const d = await api.sessions(50);
        if (!aliveRef.current) return;
        /* Research runs only. An authoring run is in the same session store,
           so this list used to show documents being built on the Authoring
           page as if they were research topics - and the research sidebar
           offered no way to open one. */
        setTopics((d.sessions || []).filter(
          (s) => s.run_kind !== 'authoring'
            && (/research agent/i.test(s.query || '') || (s.skills || []).length > 1),
        ));
      setTopicsError('');
    } catch (e) {
      if (!aliveRef.current) return;
      setTopicsError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refreshTopics();
    const t = setInterval(refreshTopics, CONF.pollSlowMs);
    return () => clearInterval(t);
  }, [refreshTopics]);

  const stopPoll = () => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  };

  const stopCatchUp = () => {
    if (catchUpRef.current) { clearInterval(catchUpRef.current); catchUpRef.current = null; }
  };

  const abortRun = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    stopPoll();
    // The post-stop catch-up must die here too: openTopic() routes
    // through abortRun, and a catch-up left running keeps polling
    // the OLD session and painting its partial graph over the topic
    // the user just opened.
    stopCatchUp();
  };

  // Unmount (or navigation away) mid-run: kill the SSE stream, the graph
  // poll AND the post-stop catch-up — previously the first two leaked forever.
  useEffect(() => () => { abortRun(); stopCatchUp(); }, []);

  /* Log auto-follow: while live (or re-enabled), each new batch of lines
     pins the scrollback to the bottom — but only if the user hasn't
     scrolled up to read, same discipline as the Chat transcript. */
  useEffect(() => {
    const el = logBoxRef.current;
    if (el && logFollow) el.scrollTop = el.scrollHeight;
  }, [logs, logFollow]);

  const poll = useCallback(async (id: string) => {
    /* Sequence guard: the fast poll (2s) can be slower than its own
       interval on a large graph, so without this an older frame can land
       after a newer one and the canvas visibly steps backwards. */
    const seq = ++pollSeq.current;
    try {
      const g = await api.graph(id);
      if (seq !== pollSeq.current) return null;
      setGraph(g);
      return g;
    } catch {
      return null; // keep last frame
    }
  }, []);

  const [dlErr, setDlErr] = useState('');

  /* A top-level navigation CANNOT send X-Aria-Token, so the Get control used to
     be a bare <a href> that returned 403 on every click: the button existed
     and did nothing. Measured on the same session and URL — 403 via
     navigation, 200 application/pdf via fetch with the header. Fetch the bytes
     and save from a blob, the way Authoring does. */
  const downloadArtifact = useCallback(
    async (artifact: string, filename: string) => {
      setDlErr('');
      try {
        const r = await fetch(
          `/api/artifact/${encodeURIComponent(artifact)}?download=1`,
          { headers: headers() },
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename || 'document';
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 10_000);
      } catch (e) {
        setDlErr(
          `could not download ${filename || 'the file'}: ${(e as Error)?.message || e}`,
        );
      }
    },
    [],
  );

  const openTopic = async (s: SessionSummary) => {
    if (running) {
      setNote('stop or wait for the current run first.');
      return;
    }
    const req = ++reqRef.current;
    abortRun();
    setSid(s.session_id);
    setTopic(displayTopic(s));
    // Only seed the brief when the box is empty. Overwriting whatever the
    // user had typed meant that peeking at a past report silently replaced
    // their brief with a one-line title, and "Research this topic" would
    // then launch a run built from that title.
    setBrief((prev) => prev || displayTopic(s));
    setAnswer('');
    setLogs([]);
    setFiles([]);
    setView('graph');
    setSelected(null);
    setStatus('idle');
    setNote('');
    /* The Files row was only ever populated from the live `done` frame, so a
       run you opened again from the sidebar lost its files even though the
       server still had them. Read the produced record. */
    void api.runProduced(s.session_id).then((snap) => {
      if (reqRef.current !== req) return;
      if (snap?.files?.length) setFiles(snap.files);
    }).catch(() => { /* no files, or unreadable: the row simply stays empty */ });
    try {
      const g = await api.graph(s.session_id);
      if (reqRef.current !== req) return;
      setGraph(g);
      // An empty node list is the same "no graph" as a null payload
      // (the server returns 200 + empty nodes for a session whose
      // graph was never written) — the note must cover both.
      if (!g || !g.nodes.length) {
        setNote('that run has no graph — it may not have started.');
        return;
      }
      // History topics never streamed a done-frame, so load the stored
      // formatter output — otherwise past reports are invisible.
      const fmts = (g?.nodes || []).filter(
        (n) => /format/i.test(n.skill || '') && isTerminal(n.status),
      );
      const last = fmts[fmts.length - 1];
      if (last) {
        try {
          const nd = await api.node(s.session_id, last.id);
          if (reqRef.current === req) setAnswer(extractReport(nd?.result?.output));
        } catch { /* report stays empty */ }
      }
    } catch {
      if (reqRef.current === req) setGraph(null);
    }
  };

  const start = async () => {
    const t = brief.trim();
    if (!t || running) {
      if (!t) setNote('type a topic first.');
      return;
    }
    abortRun();
    stopCatchUp();
    const ctl = new AbortController();
    abortRef.current = ctl;
    setRunning(true);
    // The focus handoff has to wait for React to COMMIT the disabled
    // flip. `setRunning(true)` and this `.focus()` share one batched
    // tick, so Stop is still `disabled` when focus() runs — the call
    // silently does nothing and focus falls to <body> (measured: 53 Tabs
    // from there to reach Stop). A rAF lands after the commit.
    requestAnimationFrame(() => stopBtnRef.current?.focus());
    setStatus('running');
    stoppedRef.current = false;
    setTopic(t);
    setAnswer('');
    setLogs([]);
    setLogFollow(true);
    setView('graph');
    setGraph(null);
    setSelected(null);
    setSid('');
    setNote('researching…');
    t0.current = Date.now();
    try {
      /* `research: true` makes the server dedupe this submit: a double-click or
       a retry after a slow response joins the run already in flight instead of
       starting (and paying for) a second identical one. The key is the topic
       itself, so two clicks on "quick" and then "deep" are still distinct. */
    for await (const ev of api.chat(briefFor(t, depth), cidRef.current || undefined, ctl.signal,
                                   { research: true, idempotencyKey: `research:${depth}:${t}` })) {
        if (ctl.signal.aborted) break;
        if (ev.type === 'started') {
          const esid = (ev as { session_id?: string }).session_id;
          /* The server also returns a conversation_id. It was dropped here,
             so every run minted a fresh conversation: no memory continuity,
             no per-thread cost ledger, and no way to reattach to a live run
             after a reload — which is what resolve_session exists for. */
          const ecid = (ev as { conversation_id?: string }).conversation_id;
          if (ecid) cidRef.current = ecid;
          if (!esid) continue;
          setSid(esid);
          pollRef.current = window.setInterval(() => poll(esid), CONF.pollFastMs);
          poll(esid);
        } else if (ev.type === 'log') {
          const text = String((ev as { text?: string }).text ?? '').slice(0, 500);
          if (text) {
            setLogs((prev) => {
              const next = prev.length >= 300 ? prev.slice(prev.length - 299) : prev;
              return [...next, text];
            });
          }
        } else if (ev.type === 'done') {
          stopPoll();
          const id = ((ev as { session_id?: string }).session_id || sid) as string;
          if (id) {
            try {
              const g = await api.graph(id);
              // A Stop during that fetch must not resurrect "done"
              // over the "stopped." state stop() already set.
              if (!ctl.signal.aborted) setGraph(g);
            } catch { /* keep */ }
          }
          if (ctl.signal.aborted) return; // stop() owns the final state now
          /* Files the run ACTUALLY produced. The `done` frame carries them and
             this view used to ignore that field, so a research run that really
             did render a PDF showed only a "Download .md" button built from the
             answer text: the user was told a file existed and had no way to
             open it. Prefer what the server says exists over what the prose
             claims. */
          const evFiles = ((ev as { files?: { artifact: string; filename: string; format: string }[] }).files) || [];
          setFiles(evFiles);
          if (id) {
            const snap = await api.runProduced(id).catch(() => null);
            if (snap && snap.files?.length) setFiles(snap.files);
          }
          // Normalise here too, not just on the history path: the live
          // `done` frame is the same model text and can arrive fenced or
          // wrapped, which used to render the whole report as one code
          // block or print raw JSON.
          const raw = (ev as { answer?: string }).answer;
          setAnswer(typeof raw === 'string' ? unwrapAnswer(raw) : '');
          setStatus('done');
          setView('report'); // the finished report is the readable thing
          setNote(`done in ${Math.round((Date.now() - t0.current) / 1000)}s.`);
          setRunning(false);
          requestAnimationFrame(() => startBtnRef.current?.focus());
          refreshTopics();
        } else if (ev.type === 'error') {
          stopPoll();
          setStatus('error');
          setNote(String((ev as { text?: string }).text || 'error'));
          setRunning(false);
          requestAnimationFrame(() => startBtnRef.current?.focus());
        }
      }
      if (ctl.signal.aborted) {
        setStatus('idle');
        setNote(stoppedRef.current ? 'stopped.' : 'cancelled.');
        setRunning(false);
        requestAnimationFrame(() => startBtnRef.current?.focus());
      }
    } catch (e) {
      if (ctl.signal.aborted) {
        setStatus('idle');
        setNote(stoppedRef.current ? 'stopped.' : 'cancelled.');
      } else {
        setStatus('error');
        setNote(String((e as Error).message || e));
      }
        stopPoll();
        setRunning(false);
        requestAnimationFrame(() => startBtnRef.current?.focus());
      } finally {
      if (abortRef.current === ctl) abortRef.current = null;
    }
  };

  const stop = () => {
    // Aborting the client stream alone only stops LISTENING — the server run
    // kept going (and kept billing). Ask the agent to stop too; fire-and-
    // forget, the graph poll below keeps showing the partial result.
    const liveSid = sid;
    if (liveSid) api.cancelRun({ session_id: liveSid }).catch(() => { /* offline */ });
    stoppedRef.current = true;
    abortRun();
    setRunning(false);
    requestAnimationFrame(() => startBtnRef.current?.focus());
    setStatus('idle');
    setNote('stopped.');
    // The graph poll just died with the stream, but the executor may not
    // have reached its cancel check yet (gateway warmup + memory read run
    // before the first node), so graph.json can appear SECONDS after the
    // cancel lands. Keep fetching until the partial graph is on disk and
    // every node is terminal - or until we give up after ~20s.
    if (liveSid) {
      stopCatchUp();
      let n = 0;
      const tick = async () => {
        const g = await poll(liveSid);
        const nodes = g?.nodes || [];
        const settled = nodes.length > 0 && nodes.every((nd) => isTerminal(nd.status));
        // Refresh the topic list once, at settle (or give-up) — not
        // every tick: 20 ticks of full session-list reloads for one
        // stop, most of which show nothing new.
        if (settled || ++n >= 20) {
          stopCatchUp();
          refreshTopics();
        }
      };
      catchUpRef.current = window.setInterval(() => { void tick(); }, CONF.pollCatchUpMs);
      void tick();
    }
  };

  const download = () => {
    if (!answer) return;
    const blob = new Blob([`# ${topic}\n\n${answer}`], { type: 'text/markdown' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `research-${Date.now()}.md`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  };

  const copy = () => {
    if (!answer) return;
    navigator.clipboard.writeText(`# ${topic}\n\n${answer}`).then(
      () => setNote('copied to clipboard.'),
      () => setNote('copy failed — select the report manually.'),
    );
  };

  const vis = topics.filter((s) =>
    !filter || displayTopic(s).toLowerCase().includes(filter.toLowerCase()),
  );

  const statusPill =
    status === 'running' ? <Pill tone="info">running</Pill> :
    status === 'done' ? <Pill tone="ok">done</Pill> :
    status === 'error' ? <Pill tone="err">error</Pill> :
    <Pill tone="muted">idle</Pill>;

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">
          Topics
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400">{topics.length}</span>
        </div>
        <div className="px-2.5 pb-2">
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter…"
            aria-label="Filter topics"
            className="w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400"
          />
        </div>
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2.5 pb-3">
          {topicsError && !!topics.length && (
            <div className="rounded-lg border border-amber-300/30 bg-amber-300/5 p-2.5 text-[11px] text-amber-200">
              refresh failed — showing last data.
              <button onClick={refreshTopics} className="ml-1.5 underline">retry</button>
            </div>
          )}
          {topicsError && !topics.length && (
            <div className="rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-center text-xs text-red-200">
              couldn't load topics ({topicsError}). <button onClick={refreshTopics} className="underline">retry</button>
            </div>
          )}
          {!topicsError && !vis.length && (
            <div className="py-8 text-center">
              <FlaskConical size={26} className="mx-auto mb-2 text-zinc-muted" />
              <div className="text-sm font-bold">No research yet</div>
              <div className="mx-auto mt-1 max-w-[180px] text-xs leading-relaxed text-zinc-muted">Your briefs land here.</div>
            </div>
          )}
          {vis.map((s) => (
            <button
              key={s.session_id}
              onClick={() => openTopic(s)}
              className={`block w-full rounded-[10px] border p-2.5 text-left ${
                s.session_id === sid ? 'border-violet-400/60 bg-violet-400/5' : 'border-white/10 bg-[#0e0e12] hover:border-white/25'
              }`}
            >
              <div className="truncate text-[12.5px] font-semibold">{displayTopic(s).slice(0, 90)}</div>
              <div className="mt-1 text-[11px] text-zinc-muted">{s.nodes || 0} nodes · {ago(s.updated_ago)}</div>
            </button>
          ))}
        </div>
        <div className="hidden border-t border-white/10 px-3.5 py-2.5 text-[11px] text-zinc-muted lg:block">
          <div className="text-[10px] font-bold tracking-[0.12em] text-zinc-muted">SESSION</div>
          <div className="mt-0.5 truncate font-mono">{sid ? sid.slice(0, 12) + '…' : '—'}</div>
        </div>
      </div>

      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Research">
          {statusPill}
          <div className="flex gap-1 rounded-lg border border-white/10 bg-white/5 p-[3px]" role="group" aria-label="Canvas view">
            {(['graph', 'report', 'log'] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                disabled={(v === 'report' && !answer) || (v === 'log' && !logs.length && !running)}
                aria-pressed={view === v}
                className={`rounded px-3 py-1.5 text-[11.5px] capitalize disabled:opacity-40 ${view === v ? 'bg-violet-400/20 font-bold text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'}`}
              >
                {v}
              </button>
            ))}
          </div>
          <button onClick={download} disabled={!answer} aria-label="Download report as markdown" className="rounded-md border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs hover:border-violet-400 disabled:opacity-45">Download .md</button>
          <button onClick={copy} disabled={!answer} aria-label="Copy report to clipboard" className="rounded-md border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs hover:border-violet-400 disabled:opacity-45">Copy</button>
        </TopBar>

                {/* Real files first. This row exists because research would say "I
            created report.pdf" and offer only a markdown download - the file
            was never reachable. Anything listed here came from the server's
            produced-files record, not from the answer's prose. */}
        {files.length > 0 && (
          <div className="mx-3.5 mt-3 flex flex-wrap items-center gap-2 rounded-[10px] border border-white/10 bg-[#0e0e12] p-2.5">
            <span className="text-[11px] font-bold uppercase tracking-[0.12em] text-zinc-muted">
              Files
            </span>
            {files.map((f) => (
              <span key={f.artifact} className="flex items-center gap-1.5 rounded-md border border-white/10 bg-white/5 px-2 py-1 text-[11.5px]">
                <Pill tone="muted">{f.format || 'file'}</Pill>
                <span className="max-w-[220px] truncate text-zinc-200" title={f.filename}>
                  {f.filename}
                </span>
                <button
                  onClick={() => setViewFile(f.artifact)}
                  className="rounded border border-violet-400/50 px-1.5 py-0.5 text-[11px] text-violet-200 hover:bg-violet-400/20"
                >
                  View
                </button>
                <button
                  type="button"
                  onClick={() => downloadArtifact(f.artifact, f.filename)}
                  className="rounded border border-white/15 px-1.5 py-0.5 text-[11px] text-zinc-300 hover:border-violet-400"
                >
                  Get
                </button>
              </span>
            ))}
            {dlErr && (
              <span role="alert" className="text-[11px] text-red-300">{dlErr}</span>
            )}
          </div>
        )}
        <div className="mx-3.5 mt-3 rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5 shadow-[0_8px_24px_rgba(0,0,0,0.45)]">
          <textarea
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing || (e.nativeEvent as unknown as { keyCode?: number }).keyCode === 229) return;
              if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) start();
            }}
            placeholder="Research topic — e.g. solid-state battery supply chain: who leads, where are the bottlenecks"
            rows={3}
            aria-label="Research topic"
            className="w-full resize-y rounded-lg border border-white/10 bg-black/40 p-2.5 text-[13px] leading-relaxed outline-none focus:border-violet-400"
          />
          <div className="mt-2.5 flex flex-wrap items-center gap-2.5">
            <div className="flex gap-1 rounded-lg border border-white/10 bg-white/5 p-[3px]" role="group" aria-label="Research depth">
              {(['quick', 'standard', 'deep'] as const).map((d) => (
                <button
                  key={d}
                  onClick={() => setDepth(d)}
                  aria-pressed={depth === d}
                  className={`rounded px-3 py-1.5 text-[11.5px] ${depth === d ? 'bg-violet-400/20 font-bold text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'}`}
                >
                  {d}
                </button>
              ))}
            </div>
            <span className="text-[11px] text-zinc-muted" role="status">{note}</span>
            <span className="flex-1" />
            {/* Both buttons stay mounted; the inactive one is
                disabled — never swapped out, because removing a
                focused button from the DOM throws focus back to
                <body>. start()/stop() (and the done/error paths)
                hand focus to whichever button just became live. */}
            <button
              ref={stopBtnRef}
              onClick={stop}
              disabled={!running}
              aria-label="Stop the run"
              className="rounded-md border border-red-400/50 bg-red-400/10 px-4 py-2 text-xs font-bold text-red-200 hover:bg-red-400/20 disabled:opacity-40"
            >
              Stop
            </button>
            <button
              ref={startBtnRef}
              onClick={start}
              disabled={running}
              className="rounded-md bg-violet-400 px-4 py-2 text-xs font-bold text-[#0b0b0e] shadow-[0_0_16px_rgba(139,124,246,0.35)] hover:brightness-110 disabled:opacity-50"
            >
              Research this topic
            </button>
          </div>
        </div>

        <div className="flex min-h-0 flex-1 gap-3 p-3.5">
          <div className="relative min-w-0 flex-1 overflow-hidden rounded-[10px] border border-white/10 bg-[#08080a] shadow-[0_8px_24px_rgba(0,0,0,0.45)]">
            {/* Run summary: status, span, and per-node durations. The span
                is wall-clock across the graph's own timestamps, so a slow
                run explains itself instead of just looking stuck. */}
            {graph && graph.nodes.length > 0 && view !== 'report' && (
              <RunSummary graph={graph} running={running} sid={sid} />
            )}
            {viewFile ? (
          <div className="docview-frame m-3.5">
            <div className="docview-bar">
              <span className="font-semibold">
                {files.find((f) => f.artifact === viewFile)?.filename || viewFile}
              </span>
              <button onClick={() => setViewFile(null)} className="rounded-md border border-white/15 px-2 py-1 text-[11px] hover:border-violet-400">
                Back to report
              </button>
            </div>
            <DocumentViewer
              source={{
                kind: 'artifact',
                artifact: viewFile,
                format: files.find((f) => f.artifact === viewFile)?.format || '',
                label: files.find((f) => f.artifact === viewFile)?.filename || viewFile,
              }}
            />
          </div>
        ) : view === 'log' ? (
              <LogView logs={logs} running={running} follow={logFollow} setFollow={setLogFollow} boxRef={logBoxRef} />
            ) : view === 'report' && answer ? (
              <div className="h-full overflow-y-auto">
                {/* `renderMarkdown(answer, 1, true)` demotes model headings so a report's
                    "# Executive Summary" can never become the page's only h1
                    mid-document, AND anchors the shallowest heading the model
                    actually wrote to h2 - the formatter emits `##` sections,
                    which without the fit produced a report with no top-level
                    heading at all. The `prose-report` class replaces a
                    30-utility `[&_...]` chain that styled elements the
                    renderer previously didn't emit. */}
                <article
                  tabIndex={-1}
                  aria-live="polite"
                  className="prose-chat prose-report mx-auto px-8 py-7 text-[14px] leading-[1.8] text-zinc-200"
                >
                  <div className="mb-5 border-b border-white/10 pb-4">
                    <div className="text-[11px] font-bold uppercase tracking-[0.14em] text-violet-300/80">Report</div>
                    <div className="mt-1 text-[19px] font-bold leading-snug text-zinc-50">{topic || 'Research report'}</div>
                    <div className="mt-1.5 font-mono text-[11px] text-zinc-muted">
                      {(graph?.nodes.length || 0)} nodes{note ? ` · ${note}` : ''}{sid ? ` · ${sid.slice(0, 12)}…` : ''}
                    </div>
                  </div>
                  <div dangerouslySetInnerHTML={{ __html: renderMarkdown(answer, 1, true) }} />
                </article>
              </div>
            ) : !graph || !graph.nodes.length ? (
              <div className="flex h-full flex-col items-center justify-center p-8 text-center">
                <Search size={30} className="mb-2.5 text-zinc-muted" />
                <div className="text-sm font-bold">
                  {running ? 'Starting the pipeline…' : 'The pipeline appears here'}
                </div>
                <div className="mt-1 max-w-[340px] text-xs leading-relaxed text-zinc-muted">
                  {running
                    ? 'The agent is warming up the gateway and reading memory — the first node lands in a few seconds.'
                    : 'Ask a topic above — the agent graph builds live on this canvas while it works. Drag to pan, scroll to zoom.'}
                </div>
              </div>
            ) : (
              <DagCanvas key={sid} graph={graph} running={running} selectedId={selected} onSelect={setSelected} />
            )}
            {(view === 'graph' && graph && graph.nodes.length > 0) && (
              <div className="absolute bottom-2.5 left-2.5 rounded-full border border-white/10 bg-black/70 px-3 py-1 font-mono text-[10.5px] text-zinc-400 backdrop-blur">
                {graph.nodes.length} nodes{running ? ' · live' : ''}{running ? <RunClock t0={t0.current} /> : null}
              </div>
            )}
          </div>
          {selected && sid && (
            <div className="w-[400px] max-w-[45vw] flex-none overflow-y-auto rounded-[10px] border border-white/10 bg-[#0b0b0e] shadow-[0_8px_24px_rgba(0,0,0,0.45)]">
              <Inspector sid={sid} nid={selected} topic={topic} onRerun={start} />
            </div>
          )}
        </div>
        <div className="px-4 pb-2 text-[11px] text-zinc-muted">
          pipeline runs on the agent DAG · gateway {gwUp ? 'up' : 'down'}
        </div>
      </main>
    </div>
  );
}

/** Live "Ns elapsed" badge that ticks itself. The elapsed time used to be
    parent state updated from the 2s graph poll, which re-rendered the
    topics list, the brief form, DagCanvas and the report on every tick for
    the whole run. Scoped here, only the badge re-renders. */
function RunClock({ t0 }: { t0: number }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), CONF.clockTickMs);
    return () => clearInterval(t);
  }, []);
  if (!t0) return null;
  return <span> · {Math.max(0, Math.round((Date.now() - t0) / 1000))}s</span>;
}

function fmtDur(s?: number | null): string {
  if (s == null || !isFinite(s)) return '—';
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

/** One-line run summary + duration bars, docked above the canvas. Span is
    min(started) â†’ max(completed, now if live), straight from the graph
    rollup's own timestamps — no extra requests. The pill reflects the RUN
    (done/failed/live), not the page: showing "idle" for a finished run
    buried the outcome. */
function RunSummary({ graph, running, sid }: {
  graph: GraphPayload;
  running: boolean;
  sid: string;
}) {
  const starts = graph.nodes.map((n) => n.started_at).filter((t): t is number => typeof t === 'number');
  const ends = graph.nodes.map((n) => n.completed_at).filter((t): t is number => typeof t === 'number');
  const span = starts.length ? fmtDur((ends.length && !running ? Math.max(...ends) : Date.now() / 1000) - Math.min(...starts)) : null;
  const doneCount = graph.nodes.filter((n) => isTerminal(n.status)).length;
  const failed = graph.nodes.some((n) => /fail|error/i.test(n.status || ''));
  const pill = running
    ? <Pill tone="info">running</Pill>
    : failed
      ? <Pill tone="err">failed</Pill>
      : doneCount === graph.nodes.length && graph.nodes.length > 0
        ? <Pill tone="ok">done</Pill>
        : <Pill tone="muted">idle</Pill>;
  return (
    <div className="border-b border-white/10 bg-[#0b0b0e]/80 px-4 py-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {pill}
        <span className="font-mono text-[11px] text-zinc-300">
          {doneCount}/{graph.nodes.length} nodes
        </span>
        {span && (
          <span className="font-mono text-[11px] tabular-nums text-zinc-300" title="Wall-clock span across node timestamps">
            â± {span}{running ? ' so far' : ' total'}
          </span>
        )}
        {sid && (
          <span className="truncate font-mono text-[10.5px] text-zinc-muted" title={sid}>
            {sid.slice(0, 12)}…
          </span>
        )}
      </div>
      <div className="mt-2 max-w-[560px]">
        <Timeline nodes={graph.nodes} running={running} />
      </div>
    </div>
  );
}

/** Per-node duration bars, from the timing the executor stamps into the
    graph rollup. This is what makes a 310s run legible: instead of "3
    nodes, took forever", you see planner 6s, researcher 275s, formatter
    8s — and you know exactly which hop stalled. Bars scale to the longest
    node; the running node pulses. */
function Timeline({ nodes, running }: { nodes: GraphNode[]; running: boolean }) {
  const nowS = Date.now() / 1000;
  const rows = nodes.map((n) => {
    const start = typeof n.started_at === 'number' ? n.started_at : null;
    const end = typeof n.completed_at === 'number' ? n.completed_at : null;
    const live = running && start != null && end == null;
    const dur = typeof n.elapsed_s === 'number' ? n.elapsed_s
      : start != null ? Math.max(0, (end ?? nowS) - start) : null;
    return { n, dur, live };
  });
  const max = Math.max(0.001, ...rows.map((r) => r.dur ?? 0));
  return (
    <div className="space-y-1" role="list" aria-label="Node durations">
      {rows.map(({ n, dur, live }) => (
        <div key={n.id} role="listitem" className="flex items-center gap-2">
          <span className="w-24 flex-none truncate font-mono text-[10.5px] text-zinc-400" title={n.id}>
            {(n.skill || n.id).replace(/agent$/i, '').slice(0, 14) || n.id}
          </span>
          <div className="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-white/5">
            <div
              className={`h-full rounded-full ${live ? 'animate-pulse bg-violet-400' : n.status === 'failed' || n.status === 'error' ? 'bg-red-400/70' : isTerminal(n.status) ? 'bg-emerald-400/60' : 'bg-zinc-600'}`}
              style={{ width: `${Math.max(2, ((dur ?? 0) / max) * 100)}%` }}
            />
          </div>
          <span className="w-14 flex-none text-right font-mono text-[10.5px] tabular-nums text-zinc-400">
            {dur == null ? '—' : fmtDur(dur)}
          </span>
        </div>
      ))}
    </div>
  );
}

/** The run's live reasoning log, streamed as SSE `log` frames. Previously
    discarded entirely; now a first-class view so a long run shows its work
    instead of a static canvas. Capped server-side (queue) and client-side
    (300 lines); follow can be paused to read. */
function LogView({ logs, running, follow, setFollow, boxRef }: {
  logs: string[];
  running: boolean;
  follow: boolean;
  setFollow: (v: boolean) => void;
  boxRef: React.RefObject<HTMLDivElement | null>;
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-none items-center gap-2 border-b border-white/10 px-4 py-2">
        <span className="text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">
          LIVE LOG{running ? ' · streaming' : ''}
        </span>
        <span className="flex-1" />
        <span className="font-mono text-[10.5px] text-zinc-muted">{logs.length} lines</span>
        <button
          onClick={() => setFollow(!follow)}
          aria-pressed={follow}
          className="rounded-md border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] text-zinc-300 hover:border-violet-400"
        >
          {follow ? 'pause follow' : 'follow'}
        </button>
      </div>
      <div
        ref={boxRef}
        onScroll={() => {
          const el = boxRef.current;
          if (el) setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 120);
        }}
        className="min-h-0 flex-1 overflow-y-auto p-4 font-mono text-[11.5px] leading-relaxed"
        aria-live="off"
      >
        {!logs.length && (
          <div className="text-zinc-muted">
            {running ? 'waiting for the first log line…' : 'no log lines captured for this run.'}
          </div>
        )}
        {logs.map((l, i) => (
          <div key={i} className="whitespace-pre-wrap break-words text-zinc-400">
            <span className="mr-2 select-none text-zinc-600">{String(i + 1).padStart(3, ' ')}</span>{l}
          </div>
        ))}
      </div>
    </div>
  );
}
