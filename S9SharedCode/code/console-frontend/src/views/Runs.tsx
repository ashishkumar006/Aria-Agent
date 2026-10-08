import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { GitBranch, Square } from 'lucide-react';
import { Rail, TopBar, Empty, Skel, Pill, SkipLink } from '../components/ui';
import { classifyStatus, isRunLive, pillForStatus, type RunStatus } from '../components/markdown';
import DagCanvas from '../components/DagCanvas';
import Inspector from '../components/Inspector';
import { api, ago, displayTopic, CONF, type RunRow, type GraphPayload } from '../api';

function statusOf(r: RunRow): RunStatus {
  return classifyStatus(Object.keys(r.status_counts || {}).join(' '));
}

/** A stopped run is neither "done" nor "queued": surface it explicitly.
    Base label+tone come from the shared pillForStatus so "live" is the
    same blue and "queued" the same grey as everywhere else (this page
    previously used two different tones for the identical word "queued"). */
function pillFor(r: RunRow): { label: string; tone: RunStatus } {
  const c = r.status_counts || {};
  const total = Object.values(c).reduce<number>((a, b) => a + (Number(b) || 0), 0);
  const stopped = Object.entries(c)
    .filter(([k]) => /skip|cancel/i.test(k))
    .reduce((a, [, v]) => a + (Number(v) || 0), 0);
  if (stopped > 0 && stopped >= total) return { label: 'stopped', tone: 'warn' };
  if (stopped > 0) return { label: 'partial', tone: 'warn' };
  const p = pillForStatus(Object.keys(c).join(' '));
  return { label: p.label === 'unknown' ? 'queued' : p.label, tone: p.tone };
}

export default function Runs() {
  const loc = useLocation();
const nav = useNavigate();
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [totals, setTotals] = useState<{ runs?: number; nodes?: number; dollars?: number }>({});
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [q, setQ] = useState('');
  const [flt, setFlt] = useState<'' | 'ok' | 'err'>('');
  const [sid, setSid] = useState('');
  const [query, setQuery] = useState('');
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [liveSid, setLiveSid] = useState('');
  const [note, setNote] = useState('');
  const reqRef = useRef(0);
  // Bumped on every graph fetch and on unmount, so a slow response can never
  // land after a newer one and step the canvas backwards.
  const gSeq = useRef(0);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; gSeq.current++; }, []);

  const refresh = useCallback(async () => {
    try {
      const d = await api.runsSummary(200);
      if (!aliveRef.current) return;
      setRuns(d.runs || []);
      setTotals(d.totals || {});
      setLoaded(true);
      setLoadError('');
    } catch (e) {
      if (!aliveRef.current) return;
      setLoaded(true);
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, CONF.pollSlowMs);
    return () => clearInterval(t);
  }, [refresh]);

  const open = useCallback(async (id: string) => {
    const req = ++reqRef.current;
    const seq = ++gSeq.current;
    setSid(id);
    setSelected(null);
    const r = runs.find((x) => x.session_id === id);
    /* The topic, not query.txt: `query` is the skill prompt (research runs
       store the researcher system prompt there), and this value seeds both
       the Inspector header and "Rerun in Research". */
    setQuery(r ? displayTopic(r) : '');
    setNote('');
    /* Only poll when the run looks live. Previously liveSid was set
       unconditionally, so the poll effect immediately re-fetched the graph
       open() had just loaded — two identical requests for every run,
       including years-old terminal ones. */
    /* Same rule as the poll below, for the same reason: `status_counts` from
       the rollup contains a stranded `pending` on many finished runs, so
       "some status is not terminal" is not evidence that a run is going. A
       pending alongside anything settled means it is not live. */
    const counts = Object.entries(r?.status_counts || {})
      .map(([k, v]) => ({ status: k, n: (v as number) || 0 }));
    const looksLive = !r || !r.status_counts
      ? true
      : isRunLive(counts);
    setLiveSid(looksLive ? id : '');
    try {
      const g = await api.graph(id);
      if (reqRef.current === req && seq === gSeq.current) setGraph(g);
    } catch (e) {
      if (reqRef.current !== req || seq !== gSeq.current) return;
      setGraph(null);
      setNote(`could not load this run's graph: ${e instanceof Error ? e.message : String(e)}`);
    }
  }, [runs]);

  // deep link from Research: /runs#<session_id>
  useEffect(() => {
    const h = loc.hash.slice(1);
    if (h && runs.some((r) => r.session_id === h) && h !== sid) open(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs]);

  // Poll the selected graph ONLY while the run is still live: stop as soon
  // as every node reaches a terminal status (or the selection changes).
  useEffect(() => {
    if (!liveSid) return;
    let dead = false;
    /* The server answers 200 with an EMPTY node list for an unknown
       or deleted session, so "every node terminal" (which needs at
       least one node) never fires and this poll spun forever. Count
       consecutive empty frames instead: a couple also cover the
       warmup window before the first node is written, three means
       nothing is ever going to appear. */
    let emptyTicks = 0;
    const tick = async () => {
      const seq = ++gSeq.current;
      try {
        const g = await api.graph(liveSid);
        if (dead || seq !== gSeq.current) return;
        setGraph(g);
        const nodes = g.nodes || [];
        /* `every(isTerminal)` could never be true for a run whose recovery
           replan left a stranded `pending` node, so the poll never stopped and
           the Stop button stayed offered on a run that finished hours ago
           (measured: 9 requests in 18s, still going). `isRunLive` mirrors the
           server's `_run_is_live`: a pending node alone is not work in flight. */
        const live = nodes.length === 0 ? emptyTicks < 3 : isRunLive(nodes);
        emptyTicks = nodes.length === 0 ? emptyTicks + 1 : 0;
        if (!live) setLiveSid('');
      } catch { /* keep last frame */ }
    };
    tick();
    const t = setInterval(tick, CONF.pollFastMs);
    return () => { dead = true; clearInterval(t); };
  }, [liveSid]);

  const vis = runs.filter((s) => {
    /* Search the topic first, then fall back to the raw prompt so a run is
       still findable by text that only exists in the skill instruction. */
    if (q && !(`${s.topic || ''} ${s.query || ''}`.toLowerCase().includes(q.toLowerCase()))) return false;
    const st = statusOf(s);
    if (flt === 'ok' && st !== 'ok') return false;
    if (flt === 'err' && st !== 'err') return false;
    return true;
  });
  /* Counts the FILTERED rows, not the whole board: the badge sits
     under the list, so "3 runs with failures" must describe what is
     actually on screen after search/status filters. */
  const nErr = vis.filter((s) => statusOf(s) === 'err').length;

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[264px] lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">
          Runs
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400">{totals.runs || 0}</span>
        </div>
        <div className="flex gap-1.5 px-2.5 pb-2">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter…" aria-label="Filter runs" className="min-w-0 flex-1 rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400" />
          <select value={flt} onChange={(e) => setFlt(e.target.value as '' | 'ok' | 'err')} aria-label="Status filter" className="rounded-md border border-white/10 bg-black/40 px-1.5 py-1.5 text-xs outline-none">
            <option value="">all</option><option value="ok">ok</option><option value="err">errors</option>
          </select>
        </div>
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2.5 pb-3">
          {!loaded && <Skel />}
          {loaded && loadError && !!runs.length && (
            <div className="rounded-lg border border-amber-300/30 bg-amber-300/5 p-2.5 text-[11px] text-amber-200">
              refresh failed ({loadError}) — showing last data.
              <button onClick={refresh} className="ml-1.5 underline">retry</button>
            </div>
          )}
          {loaded && !loadError && !vis.length && (
            runs.length
              /* Runs exist but the filter excludes them all: "No runs yet"
                 told the user they had never run anything. */
              ? <Empty icon={<GitBranch size={30} />} title="No runs match" sub="Clear the filter or status to see the rest of your history." />
              : <Empty icon={<GitBranch size={30} />} title="No runs yet" sub="Run something in Research (or a scheduled job) and every pipeline execution lands here. Chat is lightweight and leaves no trace." />
          )}
          {loaded && loadError && !runs.length && (
            <div className="rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-center text-xs text-red-200">
              couldn't load runs ({loadError}). <button onClick={refresh} className="underline">retry</button>
            </div>
          )}
          {vis.map((s) => {
            const pill = pillFor(s);
            return (
              <button
                key={s.session_id}
                onClick={() => open(s.session_id)}
                className={`block w-full rounded-[10px] border p-2.5 text-left ${s.session_id === sid ? 'border-violet-400/60 bg-violet-400/5' : 'border-white/10 bg-[#0e0e12] hover:border-white/25'}`}
              >
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold" title={displayTopic(s)}>{displayTopic(s).slice(0, 90)}</span>
                  <Pill tone={pill.tone}>{pill.label}</Pill>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-2 text-[11px] tabular-nums text-zinc-muted">
                  <span>{s.nodes || 0} nodes</span>
                  <span>${Number(s.usd || 0).toFixed(4)}</span>
                  <span>{ago(s.updated ? Date.now() / 1000 - s.updated : 0)}</span>
                </div>
              </button>
            );
          })}
        </div>
      </div>
      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Runs">
          {/* Kept mounted and hidden rather than conditionally rendered:
              swapping the button out on run start/end destroyed keyboard
              focus mid-interaction and threw focus back to <body>. */}
          <button
            hidden={!liveSid}
            onClick={async () => {
              try {
                const d = await api.cancelRun({ session_id: liveSid });
                setNote(d.found ? 'stopping run — remaining nodes will be skipped.' : 'nothing is running for this run.');
              } catch (e) {
                setNote(`cancel failed: ${e instanceof Error ? e.message : String(e)}`);
              }
            }}
            aria-label="Stop this run"
            tabIndex={liveSid ? 0 : -1}
            className="flex items-center gap-1.5 rounded-md border border-red-400/40 bg-red-400/10 px-2.5 py-1 text-[10.5px] font-bold text-red-200 hover:bg-red-400/20"
          >
            <Square size={11} /> Stop run
          </button>
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-[10.5px] font-bold text-zinc-400">
            {totals.runs || 0} runs · {totals.nodes || 0} nodes · ${Number(totals.dollars || 0).toFixed(4)}
          </span>
        </TopBar>
        {note && (
          <div className="px-4 pt-2 text-[11px] text-zinc-400">{note}</div>
        )}
        <div className="flex min-h-0 flex-1 gap-3 p-3.5">
          <div className="relative min-w-0 flex-1 overflow-hidden rounded-[10px] border border-white/10 bg-[#08080a]">
            {!graph || !graph.nodes.length ? (
              <Empty icon={<GitBranch size={30} />} title="Select a run" sub="Its execution graph renders here — click any node to inspect it." />
            ) : (
              /* DagCanvas is keyed by sid: it keeps per-instance
                 state (dragged node positions, the fitted-once
                 structure signature) in refs, and without a remount
                 opening a different run inherited the previous
                 run's positions and never re-fit. */
              <DagCanvas key={sid} graph={graph} running={!!liveSid} selectedId={selected} onSelect={setSelected} />
            )}
          </div>
          {selected && sid && (
            <div className="w-[400px] max-w-[45vw] flex-none overflow-y-auto rounded-[10px] border border-white/10 bg-[#0b0b0e]">
              {/* "Run again" used to be an empty arrow function, so the
                  button did nothing at all. It now hands the original query
                  to Research, which seeds its brief from location.state. */}
              <Inspector
                sid={sid}
                nid={selected}
                topic={query}
                onRerun={() => nav('/research', { state: { brief: query } })}
              />
            </div>
          )}
        </div>
        {!!nErr && <div className="px-4 pb-2 text-[11px] text-red-300">{nErr} run{nErr === 1 ? '' : 's'} with failures</div>}
      </main>
    </div>
  );
}
