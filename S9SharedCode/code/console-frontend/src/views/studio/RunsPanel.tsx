import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, type StudioRun, type StudioRunCost, type StudioRunFrame } from '../../api';
import {
  ArrowLeft, CheckCircle2, ChevronRight, Circle, Loader2, MinusCircle,
  OctagonX, Square, XCircle,
} from 'lucide-react';
import { Label, Panel } from './chrome';

/* The RUNS panel: the log of every agent run this session holds.
 *
 * A run is an object on the server (id, transcript, cost) but the console only
 * ever showed it as a spinner and a line of prose. This panel is the other
 * half of that: the list on the left of nothing — one screen — and one run
 * drilled in at a time, with EVERY frame the loop emitted: the calls it made,
 * the arguments it sent, the results it got back, the approvals it parked
 * on, the auto-writes it landed, and the money it spent.
 *
 * Two views, both honest: "timeline" folds the transcript into the same
 * blocks the live transcript uses (thought, action, result), and "raw" is the
 * file verbatim — one line per frame, index and offset included, for the
 * day the timeline and the disk disagree and the disk has to win. */

const STATUS_META: Record<string, { label: string; cls: string; glyph: () => ReactNode }> = {
  running: { label: 'running', cls: 'border-info/40 bg-info/10 text-info', glyph: () => <Loader2 size={11} className="animate-spin" /> },
  waiting: { label: 'waiting for you', cls: 'border-warn/40 bg-warn-dim text-warn', glyph: () => <Square size={10} /> },
  stopping: { label: 'stopping', cls: 'border-warn/40 bg-warn-dim text-warn', glyph: () => <Loader2 size={11} className="animate-spin" /> },
  finished: { label: 'finished', cls: 'border-ok/30 bg-ok-dim text-ok', glyph: () => <CheckCircle2 size={11} /> },
  stopped: { label: 'stopped early', cls: 'border-warn/40 bg-warn-dim text-warn', glyph: () => <MinusCircle size={11} /> },
  error: { label: 'errored', cls: 'border-danger/40 bg-danger-dim text-danger', glyph: () => <XCircle size={11} /> },
};
const statusMeta = (s?: string) =>
  STATUS_META[s || ''] ?? { label: s || 'unknown', cls: 'border-glass-border bg-white/5 text-zinc-400', glyph: () => <Circle size={10} /> };

const ago = (ts?: number): string => {
  if (!ts) return '';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};
const dur = (a?: number, b?: number): string => {
  if (!a) return '';
  const s = Math.max(0, Math.round((b || Date.now() / 1000) - a));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
};
const clock = (ts?: number): string =>
  ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
const num = (n?: number): string => Math.round(n || 0).toLocaleString();

/** The frames the loop emits, coalesced into readable blocks. Consecutive
 *  thought deltas become one THOUGHT block and consecutive answer deltas one
 *  ANSWER block; nothing is dropped, nothing is summarized away — the fold is
 *  in the reading, not in the record. */
interface Ev {
  kind: 'started' | 'status' | 'think' | 'answer' | 'tool' | 'permission' | 'write' | 'error' | 'done' | 'unknown';
  at: number;
  text?: string;
  /* tool rows */
  id?: string; name?: string; args?: unknown; result?: string; ok?: boolean | null; latency?: number;
  /* permission / write rows */
  tool?: string; summary?: string; diff?: string; path?: string; auto?: boolean;
  /* done row */
  done?: StudioRunFrame;
  /* unknown rows */
  k?: string; raw?: StudioRunFrame;
}

export function buildEvents(frames: StudioRunFrame[]): Ev[] {
  const out: Ev[] = [];
  let textBuf = '', thinkBuf = '', textAt = 0, thinkAt = 0;
  const t0 = frames.length && Number(frames[0].at) ? Number(frames[0].at) : 0;
  const off = (f: StudioRunFrame) => Math.max(0, (Number(f.at) || t0) - t0);
  const flushText = () => { if (textBuf.trim()) out.push({ kind: 'answer', at: textAt, text: textBuf }); textBuf = ''; };
  const flushThink = () => { if (thinkBuf) out.push({ kind: 'think', at: thinkAt, text: thinkBuf }); thinkBuf = ''; };
  for (const f of frames) {
    const k = String(f.t || f.type || '');
    const at = off(f);
    switch (k) {
      case 'started':
        flushThink(); flushText();
        out.push({ kind: 'started', at, raw: f });
        break;
      case 'status':
        flushThink(); flushText();
        out.push({ kind: 'status', at, text: String(f.text || '') });
        break;
      case 'thinking':
        if (!f.thinking_dropped) { if (!thinkBuf) thinkAt = at; thinkBuf += String(f.text || f.delta || ''); }
        break;
      case 'delta':
      case 'text':
        if (!textBuf) textAt = at; textBuf += String(f.text || f.delta || '');
        break;
      case 'tool_call':
        flushThink(); flushText();
        out.push({ kind: 'tool', at, id: String(f.id || ''), name: String(f.name || 'tool'),
                   args: f.arguments, result: '', ok: null });
        break;
      case 'tool_result': {
        const id = String(f.id || '');
        const row = [...out].reverse().find((e) => e.kind === 'tool'
          && (e.id === id || e.ok === null));
        const res = String(f.result || f.error || '');
        const ok = f.ok !== false && !f.error;
        if (row) { row.result = res; row.ok = ok; row.latency = Number(f.latency) || undefined; }
        else out.push({ kind: 'tool', at, id, name: String(f.name || 'tool'),
                        args: null, result: res, ok });
        break;
      }
      case 'permission':
        flushThink(); flushText();
        out.push({ kind: 'permission', at, tool: String(f.tool || ''),
                   summary: String(f.summary || ''), diff: String(f.diff || '') });
        break;
      case 'write':
        flushThink(); flushText();
        out.push({ kind: 'write', at, path: String(f.path || ''),
                   summary: String(f.summary || ''), diff: String(f.diff || ''),
                   tool: String(f.tool || ''), auto: !!f.auto });
        break;
      case 'error':
        flushThink(); flushText();
        out.push({ kind: 'error', at, text: String(f.text || f.error || 'the run failed') });
        break;
      case 'done':
        flushThink(); flushText();
        out.push({ kind: 'done', at, done: f });
        break;
      default:
        flushThink(); flushText();
        out.push({ kind: 'unknown', at, k, raw: f });
    }
  }
  flushThink(); flushText();
  return out;
}

const fmtArgs = (args: unknown): string => {
  if (args === null || args === undefined) return '';
  if (typeof args === 'string') return args;
  try { return JSON.stringify(args, null, 2); } catch { return String(args); }
};

export function RunsPanel({ sid, runs, live }: {
  sid: string;
  runs: StudioRun[];
  live: number;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  /* Frames, held per selected run. Live runs follow themselves: the poll
     appends `after=<known>` and the timeline grows without a reload. */
  const [frames, setFrames] = useState<StudioRunFrame[]>([]);
  const [framesFor, setFramesFor] = useState('');
  const [framesErr, setFramesErr] = useState('');
  const [framesLive, setFramesLive] = useState(false);
  const [view, setView] = useState<'timeline' | 'raw'>('timeline');
  const [cost, setCost] = useState<StudioRunCost | null>(null);

  const sel = useMemo(() => runs.find((r) => r.id === selected) || null, [runs, selected]);
  const selLive = sel ? (sel.status === 'running' || sel.status === 'waiting' || sel.status === 'stopping') : false;

  useEffect(() => {
    if (!selected) { setFrames([]); setFramesFor(''); setFramesErr(''); setFramesLive(false); return; }
    if (framesFor === selected) return;
    let cancelled = false;
    setFrames([]); setFramesErr('');
    api.studioRunFrames(sid, selected)
      .then((r) => {
        if (cancelled) return;
        setFrames(r?.frames || []);
        setFramesFor(selected);
        setFramesLive(!!r?.live || selLive);
        if (r?.error) setFramesErr(String(r.error));
      })
      .catch((e) => { if (!cancelled) setFramesErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
    // selLive is deliberately not a dependency: the open stream decides liveness.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid, selected]);

  /* Follow a live run. 1.5s is well under the human's reading speed for a
     diff or an argument list, and the append carries an offset so nothing
     already on screen is re-parsed. */
  useEffect(() => {
    if (!selected || !framesLive) return;
    const t = window.setInterval(() => {
      api.studioRunFrames(sid, selected, frames.length)
        .then((r) => {
          const batch = r?.frames || [];
          if (batch.length) setFrames((prev) => [...prev, ...batch]);
          setFramesLive(!!r?.live);
        })
        .catch(() => { /* a missed beat; the next one lands */ });
    }, 1500);
    return () => window.clearInterval(t);
  }, [sid, selected, framesLive, frames.length]);

  useEffect(() => {
    if (!selected) { setCost(null); return; }
    let liveFlag = true;
    const read = () => {
      api.studioRunCost(sid, selected)
        .then((c) => { if (liveFlag) setCost(c); })
        .catch(() => { if (liveFlag) setCost({ unknown: true }); });
    };
    read();
    const t = selLive ? window.setInterval(read, 3000) : null;
    return () => { liveFlag = false; if (t !== null) window.clearInterval(t); };
  }, [sid, selected, selLive]);

  const stopSelected = useCallback(() => {
    const id = selected;
    if (!id) return;
    void api.studioRunStop(sid, id).catch(() => { /* the next poll shows the truth */ });
  }, [sid, selected]);

  if (selected && sel) {
    const meta = statusMeta(sel.status);
    const events = buildEvents(frames);
    const t0 = frames.length && Number(frames[0].at) ? Number(frames[0].at) : 0;
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex flex-none items-center gap-1.5 border-b border-glass-border px-2 py-1.5">
          <button type="button" onClick={() => setSelected(null)}
                  aria-label="Back to the runs list" title="Back to the runs list"
                  className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-100">
            <ArrowLeft size={13} />
          </button>
          <Label>Run</Label>
          <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-zinc-muted" title={sel.id}>{sel.id}</span>
          {(sel.status === 'running' || sel.status === 'waiting') && (
            <button type="button" onClick={stopSelected}
                    title="Stop this run at its next hop — the loop stops buying model calls"
                    className="flex h-6 items-center gap-1 rounded-md border border-danger/40 bg-danger-dim px-2 text-[10.5px] font-semibold text-danger transition-colors hover:bg-danger/20">
              <Square size={9} fill="currentColor" /> stop
            </button>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-auto px-3 pb-4 pt-2">
          {/* The question this run answered, verbatim — the thing the list was about. */}
          <p className="whitespace-pre-wrap text-[12px] leading-relaxed text-zinc-200">{sel.question}</p>

          {/* Summary facts, one grid, every number real. Cost is the gateway's
              ledger or an explicit unknown — never $0.00 dressed up as free. */}
          <Panel className="mt-2 px-3 py-2">
            <div className="flex items-center gap-2">
              <span className={'flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9.5px] font-bold uppercase tracking-[0.12em] ' + meta.cls}>
                {meta.glyph()} {meta.label}
              </span>
              {sel.model && <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-zinc-muted" title={sel.model}>{sel.model}</span>}
              {sel.provider && <span className="flex-none font-mono text-[9.5px] text-zinc-600">{sel.provider}</span>}
            </div>
            <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1.5">
              {[
                ['mode', sel.mode || 'agent'],
                ['started', clock(sel.started) + (sel.ended ? ' → ' + clock(sel.ended) : '')],
                ['duration', dur(sel.started, sel.ended)],
                ['frames', num(frames.length || sel.frames)],
                ['tokens', `${num(sel.input_tokens)} in · ${num(sel.output_tokens)} out`],
                ['tool calls', num(sel.tool_calls)],
              ].map(([k, v]) => (
                <div key={k} className="flex min-w-0 items-baseline justify-between gap-2">
                  <dt className="flex-none text-[10px] text-zinc-muted">{k}</dt>
                  <dd className="min-w-0 truncate font-mono text-[10.5px] text-zinc-200" title={String(v)}>{v}</dd>
                </div>
              ))}
            </dl>
            <div className="mt-2 border-t border-white/5 pt-2">
              {cost === null
                ? <p className="font-mono text-[10.5px] text-zinc-muted">cost — reading the ledger…</p>
                : cost.unknown
                  ? <p className="font-mono text-[10.5px] text-warn">cost unknown — the gateway ledger was not reachable</p>
                  : (
                    <p className="font-mono text-[10.5px] text-zinc-200"
                       title={`in ${num(cost.in_tok)} · out ${num(cost.out_tok)} · ${cost.calls ?? 0} billable call(s)`}>
                      ${(cost.usd ?? 0).toFixed(4)}
                      <span className="text-zinc-500"> · {cost.calls ?? 0} calls · {num(cost.cache_reads)} cached · {num(cost.cache_creates)} fresh</span>
                    </p>
                  )}
            </div>
            {sel.error && (
              <p className="mt-2 whitespace-pre-wrap rounded-md border border-danger/30 bg-danger-dim px-2 py-1 font-mono text-[10.5px] text-danger">{sel.error}</p>
            )}
            {sel.stopped && (
              <p className="mt-2 whitespace-pre-wrap rounded-md border border-warn/30 bg-warn-dim px-2 py-1 font-mono text-[10.5px] text-warn">stopped: {sel.stopped}</p>
            )}
          </Panel>

          {/* The view toggle. Timeline is the readable fold; raw is the record. */}
          <div className="mt-2 flex items-center gap-1 rounded-md border border-glass-border bg-black/40 p-0.5">
            {(['timeline', 'raw'] as const).map((v) => (
              <button key={v} type="button" onClick={() => setView(v)}
                      aria-pressed={view === v}
                      className={'min-w-0 flex-1 rounded px-2 py-1 text-[10px] font-bold uppercase tracking-[0.14em] transition-colors '
                        + (view === v ? 'bg-white/10 text-zinc-400' : 'text-zinc-500 hover:text-zinc-200')}>
                {v === 'timeline' ? 'Timeline' : 'Raw log'}
              </button>
            ))}
          </div>
          {framesErr && <p className="mt-1.5 font-mono text-[10.5px] text-warn">{framesErr}</p>}

          {view === 'raw' ? (
            <div className="mt-2 overflow-hidden rounded-[10px] border border-glass-border bg-surface-1">
              <p className="border-b border-white/5 px-3 py-1 font-mono text-[9.5px] uppercase tracking-[0.12em] text-zinc-600">
                {frames.length} frame{frames.length === 1 ? '' : 's'} · as written to the transcript file
              </p>
              <pre className="max-h-none whitespace-pre-wrap break-all px-3 py-2 font-mono text-[10px] leading-[1.5] text-zinc-400">
                {frames.map((f, i) => (
                  <span key={i} className="block">
                    <span className="text-zinc-700">{String(i).padStart(4, ' ')}  {(Number(f.at) ? (Number(f.at) - t0).toFixed(1) : '   0.0')}s  </span>
                    <span className="text-zinc-500">{String(f.t || f.type || '?')}  </span>
                    {JSON.stringify(f, null, 0)}
                  </span>
                ))}
              </pre>
            </div>
          ) : (
            <ol className="mt-2 space-y-1.5">
              {events.map((ev, i) => <EventRow key={i} ev={ev} />)}
              {events.length === 0 && (
                <li className="px-2 py-3 text-center text-[12px] text-zinc-muted">
                  {framesLive ? 'waiting for the first frame…' : 'no frames recorded for this run.'}
                </li>
              )}
              {framesLive && (
                <li className="flex items-center gap-2 px-2 py-1 text-[10.5px] text-zinc-500">
                  <Loader2 size={11} className="animate-spin text-info" /> following the live run…
                </li>
              )}
            </ol>
          )}
        </div>
      </div>
    );
  }

  /* The list. One row per run: the question, the verdict, the cost of it. */
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-none items-center gap-2 border-b border-glass-border px-3 py-1.5">
        <Label>Agent runs</Label>
        <span className="ml-auto flex items-center gap-2 font-mono text-[10.5px] tabular-nums text-zinc-muted">
          {live > 0 && <span className="flex items-center gap-1 text-info"><Loader2 size={10} className="animate-spin" />{live} live</span>}
          {runs.length} run{runs.length === 1 ? '' : 's'}
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-2 pb-4 pt-2">
        {runs.length === 0 && (
          <div className="mt-6 flex flex-col items-center gap-2 px-4 text-center">
            <OctagonX size={22} className="text-zinc-700" aria-hidden />
            <p className="text-[12px] leading-relaxed text-zinc-muted">
              No agent runs yet. The first task handed to the agent shows up here —
              every frame it emits, and what it cost.
            </p>
          </div>
        )}
        {runs.map((r) => {
          const meta = statusMeta(r.status);
          return (
            <button key={r.id} type="button" onClick={() => setSelected(r.id)}
                    className="group mb-1 block w-full rounded-[10px] border border-glass-border bg-surface-1 px-2.5 py-2 text-left transition-colors hover:border-white/20">
              <span className="flex items-center gap-2">
                <span className={'flex-none ' + (r.status === 'finished' ? 'text-ok'
                  : r.status === 'error' ? 'text-danger'
                  : r.status === 'running' || r.status === 'stopping' ? 'text-info'
                  : 'text-warn')}>
                  {meta.glyph()}
                </span>
                <span className="min-w-0 flex-1 truncate text-[12px] leading-snug text-zinc-200"
                      title={r.question}>{r.question}</span>
                <ChevronRight size={12} aria-hidden className="flex-none text-zinc-600 transition-transform group-hover:translate-x-0.5" />
              </span>
              <span className="mt-1 flex items-center gap-2 pl-6 font-mono text-[9.5px] tabular-nums text-zinc-600">
                <span>{ago(r.started)}</span>
                <span aria-hidden>·</span>
                <span>{dur(r.started, r.ended)}</span>
                <span aria-hidden>·</span>
                <span>{num(r.tool_calls)} calls</span>
                <span aria-hidden>·</span>
                <span>{num(r.output_tokens)} out</span>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* One coalesced event row. The offset on the left is seconds since the first
   frame landed, so a timeline reads as a log, not as a chat transcript. */
function EventRow({ ev }: { ev: Ev }) {
  const at = (
    <span className="block w-9 flex-none pt-px text-right font-mono text-[9.5px] tabular-nums leading-snug text-zinc-600">
      +{ev.at.toFixed(ev.at >= 100 ? 0 : 1)}s
    </span>
  );
  const tag = (text: string, cls: string) => (
    <span className={'mr-1.5 inline-block rounded border px-1 py-px font-mono text-[9px] font-bold uppercase tracking-[0.1em] ' + cls}>{text}</span>
  );
  switch (ev.kind) {
    case 'started': {
      const f = (ev.raw || {}) as Record<string, unknown>;
      return (
        <li className="flex items-start gap-2">{at}
          <span className="min-w-0 text-[11px] leading-snug text-zinc-400">
            {tag('start', 'border-glass-border bg-white/5 text-zinc-400')}
            run began{f.mode ? <> · mode <span className="font-mono text-zinc-300">{String(f.mode)}</span></> : null}
            {f.max_hops !== undefined && <> · cap <span className="font-mono text-zinc-300">{String(f.max_hops)}</span> hops</>}
          </span>
        </li>
      );
    }
    case 'status':
      return (
        <li className="flex items-start gap-2">{at}
          <span className="min-w-0 text-[11px] italic leading-snug text-zinc-500">
            {tag('status', 'border-glass-border bg-white/5 text-zinc-500')}
            {ev.text}
          </span>
        </li>
      );
    case 'think':
      return (
        <li className="flex items-start gap-2">{at}
          <div className="min-w-0 flex-1">
            {tag('thought', 'border-glass-border bg-white/5 text-zinc-500')}
            <p className="whitespace-pre-wrap pt-0.5 text-[11.5px] italic leading-relaxed text-zinc-500">{ev.text}</p>
          </div>
        </li>
      );
    case 'answer':
      return (
        <li className="flex items-start gap-2">{at}
          <div className="min-w-0 flex-1">
            {tag('answer', 'border-white/15 bg-white/[0.06] text-zinc-300')}
            <p className="whitespace-pre-wrap pt-0.5 text-[11.5px] leading-relaxed text-zinc-200">{ev.text}</p>
          </div>
        </li>
      );
    case 'tool':
      return (
        <li className="flex items-start gap-2">{at}
          <div className="min-w-0 flex-1 overflow-hidden rounded-md border border-glass-border bg-surface-1">
            <div className="flex items-center gap-2 border-b border-white/5 px-2 py-1">
              {tag('action', 'border-white/15 bg-white/[0.06] text-zinc-300')}
              <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] font-semibold text-zinc-200" title={ev.name}>{ev.name}</span>
              {ev.id && <span className="flex-none font-mono text-[9px] text-zinc-600" title={ev.id}>{ev.id.slice(0, 8)}</span>}
              {ev.latency !== undefined && <span className="flex-none font-mono text-[9px] text-zinc-600">{ev.latency.toFixed(2)}s</span>}
              {ev.ok === null
                ? <Loader2 size={11} className="animate-spin text-zinc-400" />
                : ev.ok
                  ? <CheckCircle2 size={11} className="text-ok/80" />
                  : <XCircle size={11} className="text-danger" />}
            </div>
            {ev.args !== null && ev.args !== undefined && (
              <pre className="max-h-44 overflow-auto whitespace-pre-wrap break-all px-2 py-1 font-mono text-[10px] leading-snug text-zinc-400">{fmtArgs(ev.args)}</pre>
            )}
            {ev.result && (
              <pre className={'max-h-44 overflow-auto whitespace-pre-wrap break-all border-t border-white/5 px-2 py-1 font-mono text-[10px] leading-snug '
                + (ev.ok === false ? 'text-danger/90' : 'text-zinc-500')}>{ev.result}</pre>
            )}
          </div>
        </li>
      );
    case 'permission':
      return (
        <li className="flex items-start gap-2">{at}
          <div className="min-w-0 flex-1 overflow-hidden rounded-md border border-warn/30 bg-warn-dim">
            <div className="flex items-center gap-2 px-2 py-1">
              {tag('approval', 'border-warn/40 bg-warn/20 text-warn')}
              <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-200">waiting for you — {ev.tool}</span>
            </div>
            {ev.summary && <p className="px-2 pb-1 text-[10.5px] text-zinc-400">{ev.summary}</p>}
            {ev.diff && <pre className="max-h-44 overflow-auto whitespace-pre-wrap border-t border-warn/20 px-2 py-1 font-mono text-[9.5px] leading-snug text-zinc-500">{ev.diff}</pre>}
          </div>
        </li>
      );
    case 'write':
      return (
        <li className="flex items-start gap-2">{at}
          <div className="min-w-0 flex-1 overflow-hidden rounded-md border border-ok/25 bg-ok-dim">
            <div className="flex items-center gap-2 px-2 py-1">
              {tag('write', 'border-ok/40 bg-ok/15 text-ok')}
              <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-zinc-200" title={ev.path}>{ev.path}</span>
              <span className="flex-none font-mono text-[9px] uppercase text-zinc-500">{ev.auto ? 'auto' : 'gated'}</span>
            </div>
            {ev.summary && <p className="px-2 pb-1 text-[10.5px] text-zinc-400">{ev.summary}</p>}
            {ev.diff && <pre className="max-h-44 overflow-auto whitespace-pre-wrap border-t border-ok/20 px-2 py-1 font-mono text-[9.5px] leading-snug text-zinc-500">{ev.diff}</pre>}
          </div>
        </li>
      );
    case 'error':
      return (
        <li className="flex items-start gap-2">{at}
          <span className="min-w-0 flex-1 whitespace-pre-wrap rounded-md border border-danger/30 bg-danger-dim px-2 py-1 font-mono text-[10.5px] leading-snug text-danger">
            {tag('error', 'border-danger/40 bg-danger/15 text-danger')}
            {ev.text}
          </span>
        </li>
      );
    case 'done': {
      const f = (ev.done || {}) as Record<string, unknown>;
      return (
        <li className="flex items-start gap-2">{at}
          <span className="min-w-0 text-[11px] leading-snug text-ok">
            {tag('done', 'border-ok/30 bg-ok-dim text-ok')}
            {num(Number(f.input_tokens || 0))} in · {num(Number(f.output_tokens || 0))} out · {Number(f.tool_calls || 0)} calls
            {typeof f.provider === 'string' && f.provider ? <> · {f.provider}</> : null}
          </span>
        </li>
      );
    }
    case 'unknown':
      return (
        <li className="flex items-start gap-2">{at}
          <span className="min-w-0 flex-1 whitespace-pre-wrap break-all font-mono text-[9.5px] leading-snug text-zinc-600">
            {tag(String(ev.k || 'frame'), 'border-glass-border bg-white/5 text-zinc-500')}
            {JSON.stringify(ev.raw)}
          </span>
        </li>
      );
  }
}
