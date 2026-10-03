import { useEffect, useState } from 'react';
import { ThumbsUp, ThumbsDown, RotateCcw, Clock, DollarSign } from 'lucide-react';
import { renderMarkdown } from './markdown';
import { api, type NodeDetail } from '../api';

type Tab = 'overview' | 'output' | 'preview' | 'stats';

function fmtDur(d: NodeDetail): string {
  const a = d.started_at, b = d.completed_at;
  if (typeof a === 'number' && typeof b === 'number' && b >= a) return `${(b - a).toFixed(2)}s`;
  if (typeof d.result?.elapsed_s === 'number') return `${d.result.elapsed_s.toFixed(2)}s`;
  return '—';
}

function outText(d: NodeDetail): string {
  const o = d.result?.output;
  const t = typeof o === 'string' ? o : JSON.stringify(o, null, 1);
  return String(t == null ? '(no output yet)' : t);
}

export default function Inspector({
  sid,
  nid,
  topic,
  onRerun,
}: {
  sid: string;
  nid: string;
  topic: string;
  onRerun: () => void;
}) {
  const [tab, setTab] = useState<Tab>('overview');
  const [d, setD] = useState<NodeDetail | null>(null);
  const [err, setErr] = useState('');
  const [vote, setVote] = useState<0 | 1 | -1>(0);

  useEffect(() => {
    let live = true;
    setD(null);
    setErr('');
    setVote(0);
    api.node(sid, nid).then(
      (v) => { if (live) setD(v); },
      (e) => { if (live) setErr(String(e.message || e)); },
    );
    api.feedbackGet(nid).then(
      (v) => { if (live && (v.vote === 1 || v.vote === -1)) setVote(v.vote); },
      () => {},
    );
    return () => { live = false; };
  }, [sid, nid]);

  const cast = async (v: 1 | -1) => {
    try {
      // The skill travels with the vote so /api/feedback/rollup can answer
      // "which skill produces output people mark bad?" — the raw skill id,
      // which the server stores and groups by.
      await api.feedbackPost(sid, nid, v, String(d?.skill || ''));
      setVote(v);
    } catch {
      /* best-effort */
    }
  };

  const skill = String(d?.skill || nid || '');
  /* `skill` is the raw registry id ("plannerAgent"). Re-appending "Agent"
     and upper-casing produced the header "PLANNERAGENT" — two words fused
     into one. Label with the human part, and keep the raw id on the
     sub-line so nothing is hidden. */
  const label = skill.replace(/agent$/i, '').replace(/([a-z0-9])([A-Z])/g, '$1 $2');
  const title = label.toUpperCase().slice(0, 24) || nid.toUpperCase();
  const r = d?.result || {};
  const cost = r.cost != null ? String(r.cost) : '—';
  /* NodeState names this field `prompt_sent`. Reading `prompt` (which never
     exists on the wire) left the AGENT GOAL panel permanently hidden — the
     most useful panel when asking why a node did the wrong thing. */
  const goal = d?.prompt_sent || '';

  /* Planner successors are OBJECTS (`{skill, inputs, metadata}`), not
     strings. Rendering one with `{x}` threw React error #31, which unmounted
     the entire app — clicking the planner node (node 1 of every run) blanked
     the whole console. Normalise to text before rendering. */
  const successorLabels = (r.successors || []).map((x) =>
    typeof x === 'string'
      ? x
      : `${x.skill || '?'}${x.metadata?.label ? ` · ${x.metadata.label}` : ''}`);
  const inputLabels = d?.inputs || [];

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-start gap-2 px-4 pb-2 pt-3">
        <div className="min-w-0">
          <div className="truncate text-xs font-bold tracking-wider">{d ? title : 'LOADING…'}</div>
          <div className="mt-0.5 truncate font-mono text-[11px] text-zinc-muted" title={skill}>{d && skill ? `${nid} · ${skill}` : nid}</div>
        </div>
        <div className="flex-1" />
        <button
          onClick={onRerun}
          className="flex items-center gap-1.5 rounded-md bg-violet-400 px-3 py-1.5 text-xs font-bold text-[#0b0b0e] shadow-[0_0_16px_rgba(139,124,246,0.35)] hover:brightness-110"
        >
          <RotateCcw size={13} /> Run again
        </button>
      </div>
      <div className="flex gap-0.5 border-b border-white/10 px-3">
        {(['overview', 'output', 'preview', 'stats'] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-2.5 py-2 text-[10.5px] font-bold tracking-wider ${
              tab === t ? 'text-violet-300 shadow-[inset_0_-2px_0_#8b7cf6]' : 'text-zinc-muted hover:text-zinc-300'
            }`}
          >
            {t.toUpperCase()}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 text-[12.5px]">
        {err && <div className="text-xs text-red-300">node unavailable: {err}</div>}
        {!d && !err && <div className="text-xs text-zinc-muted">loading node…</div>}
        {d && tab === 'overview' && (
          <div className="space-y-3">
            {/* Failed nodes carry the reason in result.error, which was
                never rendered — a failed node showed its output with no
                error text and no agent attribution. */}
            {typeof r.error === 'string' && r.error && (
              <div className="rounded-lg border border-red-400/30 bg-red-400/5 p-2.5 leading-relaxed text-red-200">
                <div className="mb-1 text-[10px] font-bold tracking-[0.12em] text-red-300">ERROR</div>
                {r.error.slice(0, 1200)}
              </div>
            )}
            <div>
              <div className="mb-1 text-[10px] font-bold tracking-[0.12em] text-zinc-muted">USER QUERY</div>
              <div className="rounded-lg border border-white/10 bg-[#101016] p-2.5 leading-relaxed text-zinc-300">{topic || '—'}</div>
            </div>
            {goal && (
              <div>
                <div className="mb-1 text-[10px] font-bold tracking-[0.12em] text-zinc-muted">AGENT GOAL</div>
                <div className="rounded-lg border border-white/10 bg-[#101016] p-2.5 leading-relaxed text-zinc-300">
                  {String(goal).slice(0, 900)}
                </div>
              </div>
            )}
            <div>
              <div className="mb-1 text-[10px] font-bold tracking-[0.12em] text-zinc-muted">INPUTS (READS)</div>
              <div className="flex flex-wrap gap-1.5">
                {inputLabels.length
                  ? inputLabels.map((x, i) => (
                    <span key={`${x}-${i}`} className="rounded border border-emerald-400/30 bg-emerald-400/10 px-2 py-0.5 font-mono text-[10.5px] text-emerald-200">{x}</span>
                  ))
                  : <span className="text-xs text-zinc-muted">—</span>}
              </div>
            </div>
            <div>
              <div className="mb-1 text-[10px] font-bold tracking-[0.12em] text-zinc-muted">OUTPUTS (WRITES)</div>
              <div className="flex flex-wrap gap-1.5">
                {successorLabels.length
                  ? successorLabels.map((x, i) => (
                    <span key={`${x}-${i}`} className="rounded border border-violet-400/35 bg-violet-400/10 px-2 py-0.5 font-mono text-[10.5px] text-violet-200">{x}</span>
                  ))
                  : <span className="text-xs text-zinc-muted">—</span>}
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <span className="flex items-center gap-1.5 rounded-full border border-violet-400/45 bg-violet-400/10 px-2.5 py-1 text-[10.5px] font-bold text-violet-200">
                <Clock size={12} /> {fmtDur(d)}
              </span>
              <span className="flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-[10.5px] font-bold text-zinc-300">
                <DollarSign size={12} /> {cost}
              </span>
            </div>
            <div>
              <div className="mb-1 text-[10px] font-bold tracking-[0.12em] text-zinc-muted">EXECUTION OUTPUT</div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-lg border border-white/10 bg-[#0a0a0d] p-2.5 font-mono text-[11.5px] leading-relaxed">{outText(d).slice(0, 2500)}</pre>
            </div>
          </div>
        )}
        {d && tab === 'output' && (
          <pre className="whitespace-pre-wrap rounded-lg border border-white/10 bg-[#0a0a0d] p-2.5 font-mono text-[11.5px] leading-relaxed">{outText(d).slice(0, 6000)}</pre>
        )}
        {d && tab === 'preview' && <ReportPreview text={asText(r.output)} />}
        {d && tab === 'stats' && (
          <dl className="space-y-1.5">
            {/* `AgentResult` carries provider/cost/elapsed_s only — no model
                name and no token counts. Those rows read permanent "—", so
                the tab now shows only fields the server actually sends. */}
            {[
              ['agent', r.agent_name || d?.skill || '—'],
              ['provider', r.provider || '—'],
              ['duration', fmtDur(d)],
              ['cost', cost],
              ['retries', String(d.retries ?? 0)],
              ['status', d.status || '—'],
            ].map(([k, v]) => (
              <div key={k} className="flex justify-between gap-3 border-b border-white/5 py-1.5">
                <dt className="text-zinc-muted">{k}</dt>
                <dd className="truncate font-mono text-[11.5px] text-zinc-200">{v}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>
      <div className="flex items-center gap-1.5 border-t border-white/10 px-4 py-2.5">
        <button
          onClick={() => cast(1)}
          title="Good output"
          aria-label="Mark output good"
          aria-pressed={vote === 1}
          className={`rounded-md border border-white/10 bg-white/5 p-2 hover:border-violet-400 ${vote === 1 ? 'border-violet-400 bg-violet-400/15' : ''}`}
        >
          <ThumbsUp size={15} className={vote === 1 ? 'text-violet-300' : 'text-zinc-400'} />
        </button>
        <button
          onClick={() => cast(-1)}
          title="Bad output"
          aria-label="Mark output bad"
          aria-pressed={vote === -1}
          className={`rounded-md border border-white/10 bg-white/5 p-2 hover:border-violet-400 ${vote === -1 ? 'border-violet-400 bg-violet-400/15' : ''}`}
        >
          <ThumbsDown size={15} className={vote === -1 ? 'text-violet-300' : 'text-zinc-400'} />
        </button>
      </div>
    </div>
  );
}

/** `AgentResult.output` is a dict in practice (a report lives under
    `output.final_answer`/`report`, free text under `output.text`), so the
    old `typeof === 'string'` guard made the Preview tab show "nothing
    rendered yet" for every node in the app. Walk the common shapes before
    giving up. */
function asText(v: unknown): string {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  if (typeof v !== 'object') return String(v);
  const o = v as Record<string, unknown>;
  for (const k of ['final_answer', 'report', 'answer', 'text', 'output', 'summary']) {
    const got = asText(o[k]);
    if (got.trim()) return got;
  }
  try { return JSON.stringify(v, null, 2); } catch { return ''; }
}

/** Minimal rich preview — shares the hardened renderer with Chat so
    headings/bold/lists/links/code fences behave (and escape) identically. */
export function ReportPreview({ text }: { text: string }) {
  if (!text) return <div className="text-xs text-zinc-muted">nothing rendered yet</div>;
  return (
    <div
      className="prose-chat text-[13px] leading-relaxed text-zinc-300"
      dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
    />
  );
}
