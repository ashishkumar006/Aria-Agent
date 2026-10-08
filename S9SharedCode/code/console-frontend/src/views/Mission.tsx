import { useCallback, useEffect, useRef, useState } from 'react';
import { Pause, Play, Terminal } from 'lucide-react';
import { Rail, TopBar, Empty, Stat, SkipLink } from '../components/ui';
import { api, CONF, type FeedEvent } from '../api';

const LEVELS = ['all', 'run', 'sched', 'tool', 'info', 'err'] as const;

export default function Mission() {
  const [all, setAll] = useState<FeedEvent[]>([]);
  const [level, setLevel] = useState<(typeof LEVELS)[number]>('all');
  const [q, setQ] = useState('');
  const [paused, setPaused] = useState(false);
  const [loadErr, setLoadErr] = useState('');
  const sigRef = useRef('');
  const pausedRef = useRef(false);
  pausedRef.current = paused;
  // Sequence guard: a slow poll can outlive its interval, and an
  // older response landing after a newer one would step the feed
  // (and its signature) backwards — the next identical fetch would
  // then look "new" and re-render stale lines.
  const reqRef = useRef(0);

  const tick = useCallback(async () => {
    const req = ++reqRef.current;
    try {
      const d = await api.events(200);
      if (req !== reqRef.current) return;
      const evs = d.events || [];
      // Signature over the FULL id list: first|last-only missed insertions
      // in the middle and skipped re-renders. It covers content as well as
      // timestamps, so a rewritten line in the same timestamp slot is not
      // silently swallowed.
      const sig = evs.map((e) => `${e.t}:${e.src}:${e.msg}`).join(',');
      setLoadErr('');
      if (sig === sigRef.current) return;
      sigRef.current = sig;
      setAll(evs);
    } catch (e) {
      // Do NOT swallow this. The feed previously caught every one of 14
      // failure classes and kept rendering the "live" pill with ERRORS 0 in
      // the OK tone -- so the one page whose job is surfacing server and tool
      // failures asserted the system had none while it could read nothing.
      setLoadErr(String((e as Error)?.message || e));
    }
  }, []);

  useEffect(() => {
    tick();
    const t = setInterval(() => { if (!pausedRef.current) tick(); }, CONF.pollLiveMs);
    return () => clearInterval(t);
  }, [tick]);

  const counts = { run: 0, sched: 0, tool: 0, info: 0, err: 0 };
  for (const e of all) {
    if (e.level in counts) counts[e.level as keyof typeof counts]++;
  }
  const ql = q.toLowerCase();
  const vis = all.filter((e) =>
    (level === 'all' || e.level === level) &&
    (!ql || `${e.src} ${e.msg}`.toLowerCase().includes(ql)));

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">
          Sources
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400">{vis.length} shown</span>
        </div>
        <div className="space-y-1.5 px-2.5">
          {LEVELS.map((l) => (
            <button
              key={l}
              onClick={() => setLevel(l)}
              className={`block w-full rounded-md px-2.5 py-1.5 text-left text-xs ${level === l ? 'bg-violet-400/15 font-bold text-zinc-100' : 'text-zinc-400 hover:bg-white/5'}`}
            >
              {l === 'all' ? 'all' : l === 'run' ? 'runs' : l === 'sched' ? 'scheduler' : l === 'tool' ? 'tools' : l === 'info' ? 'server' : 'errors'}
            </button>
          ))}
        </div>
        <div className="mt-auto hidden border-t border-white/10 px-3.5 py-2.5 text-[11px] text-zinc-muted lg:block">poll {CONF.pollLiveMs / 1000}s · guarded</div>
      </div>
      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Console">
          <span
            role="status"
            className={`rounded-full border px-2.5 py-1 text-[10.5px] font-bold ${
              loadErr
                ? 'border-red-400/50 bg-red-400/10 text-red-200'
                : paused
                  ? 'border-white/10 bg-white/5 text-zinc-400'
                  : 'border-violet-400/45 bg-violet-400/10 text-violet-200'}`}
          >
            {loadErr ? '● feed error' : paused ? '○ paused' : '● live'}
          </span>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter…" aria-label="Filter events" className="w-44 rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400" />
          <button onClick={() => setPaused((p) => !p)} className="flex items-center gap-1.5 rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs hover:border-violet-400">
            {paused ? <><Play size={13} /> resume</> : <><Pause size={13} /> pause</>}
          </button>
        </TopBar>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex flex-wrap gap-2.5 p-3.5 pb-0">
            {/* These count feed LINES currently held, not system totals —
                labelling them "recent sessions / jobs armed" read as
                authoritative counts. */}
            <Stat k="RUN LINES" v={String(counts.run)} s="in feed" />
            <Stat k="SCHED LINES" v={String(counts.sched)} s="in feed" />
            <Stat k="SERVER LINES" v={String(counts.info + counts.tool)} s="server + tool log lines" />
            <Stat
              k="ERRORS"
              v={loadErr ? '?' : String(counts.err)}
              s={loadErr ? 'feed unreachable' : 'need attention'}
              tone={loadErr || counts.err ? 'bad' : 'ok'}
            />
          </div>
          {loadErr && (
            <p role="alert" className="mx-3.5 mb-3.5 rounded-[10px] border border-red-400/40 bg-red-400/5 px-3.5 py-2.5 text-[12.5px] text-red-200">
              Cannot read the event feed: {loadErr.slice(0, 180)} — the counts
              below are from the last successful read, not live. Retrying every{' '}
              {CONF.pollLiveMs / 1000}s.
            </p>
          )}
          <section className="mx-3.5 mb-3.5 mt-3 overflow-hidden rounded-[10px] border border-white/10 bg-[#0e0e12]">
            <div className="border-b border-white/10 px-3.5 py-2.5 text-xs font-bold tracking-wider">EVENT FEED</div>
            <div className="max-h-[calc(100vh-320px)] min-h-[200px] overflow-y-auto py-2 font-mono text-xs leading-relaxed">
              {!vis.length && !loadErr && <Empty icon={<Terminal size={30} />} title="No events for this filter yet" />}
              {!vis.length && !!loadErr && (
                <p className="px-3 py-2 text-zinc-500">Last successful read shown above; the feed is currently unreachable.</p>
              )}
              {vis.map((e, i) => (
                <div key={`${e.t}-${e.src}-${i}`} className={`flex gap-2.5 border-l-2 px-3 py-[3px] hover:bg-white/[0.03] ${e.level === 'err' ? 'border-red-400' : 'border-transparent'}`}>
                  <span className="flex-none tabular-nums text-zinc-muted">{e.iso}</span>
                  <span className={`min-w-[86px] flex-none ${e.level === 'err' ? 'text-red-300' : e.level === 'tool' ? 'text-emerald-300' : e.level === 'run' ? 'text-violet-300' : e.level === 'sched' ? 'text-amber-200' : 'text-violet-300'}`}>{e.src}</span>
                  <span className={`break-words ${e.level === 'err' ? 'text-red-200' : 'text-zinc-400'}`}>{e.msg}</span>
                </div>
              ))}
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}
