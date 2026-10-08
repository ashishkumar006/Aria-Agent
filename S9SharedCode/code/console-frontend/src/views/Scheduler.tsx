import { useCallback, useEffect, useRef, useState } from 'react';
import { CalendarClock, Trash2, X } from 'lucide-react';
import { Rail, TopBar, Empty, Skel, Pill, Stat, SkipLink } from '../components/ui';
import { api, ago, fmtFire, CONF, type ScheduleJob } from '../api';

function asList(s: ScheduleJob[] | Record<string, ScheduleJob> | undefined): (ScheduleJob & { key: string })[] {
  if (!s) return [];
  if (Array.isArray(s)) return s.map((j, i) => ({ key: j.id || String(i), ...j }));
  return Object.entries(s).map(([id, j]) => ({ ...j, key: id, id: j.id || id }));
}

/* `ago()` is the console's one relative-time formatter. Scheduler had its
   own `agoText` with a different day boundary (48h vs 24h), so a 30h-old
   run read "1d ago" in Runs and "ran 30h ago" here. */
function lastFire(ts?: number): string {
  if (!ts) return '';
  return `ran ${ago(Date.now() / 1000 - ts)}`;
}

export default function Scheduler() {
  const [jobs, setJobs] = useState<(ScheduleJob & { key: string })[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [q, setQ] = useState('');
  const [when, setWhen] = useState('');
  const [out, setOut] = useState('');
  const [loadError, setLoadError] = useState('');
  const [busy, setBusy] = useState(false);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const load = useCallback(async () => {
    try {
      const d = await api.scheduleList();
      if (!aliveRef.current) return;
      setJobs(asList(d.schedules));
      setLoaded(true);
      setLoadError('');
    } catch (e) {
      /* Previously swallowed, so an unreachable API rendered "No jobs" —
         indistinguishable from a genuinely empty board. */
      if (!aliveRef.current) return;
      setLoaded(true);
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // The list only mutates when a job fires (minutes/hours away), so a flat
  // 2s poll while armed is 7.5× the documented list cadence for static
  // data. Go fast only when a firing is actually imminent (<60s out).
  const nowS = Date.now() / 1000;
  const imminent = jobs.some((j) => j.enabled && (j.next_fire || 0) > 0 && (j.next_fire || 0) - nowS < 60);
  useEffect(() => {
    const t = setInterval(load, imminent ? CONF.pollFastMs : CONF.pollSlowMs);
    return () => clearInterval(t);
  }, [load, imminent]);

  const create = async () => {
    /* POST /api/schedule has no idempotency key, so a double-click created
       two jobs that each ran a full agent turn and each billed. */
    if (busy) return;
    if (!q.trim() || !when.trim()) { setOut('query and when are required.'); return; }
    setBusy(true);
    setOut('scheduling…');
    try {
      const d = await api.scheduleCreate(q.trim(), when.trim());
      if (d.status === 'ok') {
        setOut(`scheduled ${d.id || ''}`);
        setQ('');
        setWhen('');
        load();
      } else setOut(d.message || 'failed');
    } catch (e) { setOut(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  const cancel = async (id: string | undefined) => {
    /* Same guard as create/remove: without it a double-click double-fired,
       and each firing schedules real agent work. */
    if (!id || busy) return;
    setBusy(true);
    try {
      const d = await api.scheduleCancel(id);
      setOut(d.status === 'ok' ? `cancelled ${id}` : 'failed');
      load();
    } catch (e) { setOut(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  const remove = async (id: string | undefined) => {
    if (!id || busy) return;
    if (!window.confirm(`Delete this scheduled job permanently?\n\n${id}`)) return;
    setBusy(true);
    try {
      const d = await api.scheduleDelete(id);
      setOut(d.status === 'ok' ? `deleted ${id}` : 'failed');
      load();
    } catch (e) { setOut(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  const active = jobs.filter((j) => j.enabled).sort((a, b) => (a.next_fire || 0) - (b.next_fire || 0));

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">
          Jobs
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400">{jobs.length}</span>
        </div>
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2.5 pb-3">
          {!loaded && <Skel />}
          {loaded && loadError && !jobs.length && (
            <div className="rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-center text-xs text-red-200">
              couldn't load jobs ({loadError}). <button onClick={load} className="underline">retry</button>
            </div>
          )}
          {loaded && !loadError && !jobs.length && <Empty icon={<CalendarClock size={30} />} title="No jobs" sub="Schedule a reminder or a recurring agent run." />}
          {jobs.map((j) => (
            <div key={j.key} className="rounded-[10px] border border-white/10 bg-[#0e0e12] p-2.5">
              <div className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold">{(j.query || '(no query)').slice(0, 90)}</span>
                <Pill tone={j.enabled ? 'info' : 'muted'}>{j.enabled ? 'scheduled' : 'off'}</Pill>
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-zinc-muted">
                <span className="rounded border border-violet-400/30 bg-violet-400/10 px-1.5 py-0.5 font-mono text-[10.5px] text-violet-200">{j.when || j.recurring || '?'}</span>
                <span>{j.enabled ? fmtFire(j.next_fire) : lastFire(j.last_fire) || 'cancelled'}</span>
              </div>
              {j.last_error && (
                <div className="mt-1 break-words text-[10.5px] text-red-300" title={j.last_error}>last run failed: {j.last_error.slice(0, 120)}</div>
              )}
            </div>
          ))}
        </div>
      </div>
      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Scheduler" />
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex flex-wrap gap-2.5 p-3.5 pb-0">
            <Stat k="JOBS" v={String(jobs.length)} s="total scheduled" />
            <Stat k="ACTIVE" v={String(active.length)} s="armed & waiting" tone={active.length ? 'ok' : undefined} />
            <Stat k="NEXT FIRE" v={active.length ? fmtFire(active[0].next_fire).split(' (')[0] : '—'} s={active.length ? (active[0].query || '').slice(0, 40) : 'no active jobs'} />
          </div>
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">NEW JOB</div>
          <section className="mx-3.5 rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5">
            <div className="flex flex-wrap items-end gap-2.5">
              <label className="min-w-[220px] flex-[2] text-[11px] text-zinc-muted">query<br />
                <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.nativeEvent.isComposing) return; if (e.key === 'Enter') create(); }} placeholder="remind me to stretch" aria-label="Job query" className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400" />
              </label>
              <label className="text-[11px] text-zinc-muted">when<br />
                <input value={when} onChange={(e) => setWhen(e.target.value)} onKeyDown={(e) => { if (e.nativeEvent.isComposing) return; if (e.key === 'Enter') create(); }} placeholder="in 30m · daily@09:00 · every 2h · tomorrow 9am" aria-label="Job schedule" className="mt-1 w-52 rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400" />
              </label>
              <button onClick={create} disabled={busy} aria-label="Schedule job" className="rounded-md bg-violet-400 px-4 py-2 text-xs font-bold text-[#0b0b0e] disabled:opacity-50 hover:brightness-110">{busy ? 'Scheduling…' : 'Schedule'}</button>
            </div>
            {out && <div className="mt-2 text-[11px] text-zinc-muted" role="status">{out}</div>}
          </section>
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">ALL JOBS · {jobs.length} total · {active.length} active</div>
          <section className="mx-3.5 mb-3.5 overflow-x-auto rounded-[10px] border border-white/10 bg-[#0e0e12]">
            <table className="w-full min-w-[560px] border-collapse text-[12.5px]">
              <thead>
                <tr className="border-b border-white/10 text-left text-[10px] tracking-[0.1em] text-zinc-muted">
                  <th className="px-2.5 py-2 font-semibold">Query</th>
                  <th className="px-2.5 py-2 font-semibold">When</th>
                  <th className="px-2.5 py-2 font-semibold">Next fire</th>
                  <th className="px-2.5 py-2 font-semibold">State</th>
                  <th className="px-2.5 py-2" />
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.key} className="border-b border-white/5 hover:bg-white/[0.02]">
                    <td className="px-2.5 py-2">{(j.query || '').slice(0, 80)}</td>
                    <td className="px-2.5 py-2"><span className="rounded border border-violet-400/30 bg-violet-400/10 px-1.5 py-0.5 font-mono text-[10.5px] text-violet-200">{j.when || j.recurring || ''}</span></td>
                    <td className="px-2.5 py-2 tabular-nums">{j.enabled ? fmtFire(j.next_fire) : lastFire(j.last_fire) || '—'}</td>
                    <td className="px-2.5 py-2">
                      <div className="flex items-center gap-1.5">
                        {/* One word for the enabled state: the rail above
                            calls it "scheduled" while this table said
                            "active" for the same job. */}
                        <Pill tone={j.enabled ? 'info' : 'muted'}>{j.enabled ? 'scheduled' : 'off'}</Pill>
                        {j.last_error && <span className="max-w-[160px] truncate text-[10.5px] text-red-300" title={j.last_error}>failed</span>}
                      </div>
                    </td>
                    <td className="px-2.5 py-2 text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        {j.enabled && j.id && <button onClick={() => cancel(j.id)} title="Cancel" aria-label={`Cancel job ${j.query || j.key}`} className="rounded-md border border-white/15 px-2 py-1 hover:border-red-400 hover:text-red-300"><X size={13} /></button>}
                        {!j.enabled && j.id && <button onClick={() => remove(j.id)} title="Delete" aria-label={`Delete job ${j.query || j.key}`} className="rounded-md border border-white/15 px-2 py-1 hover:border-red-400 hover:text-red-300"><Trash2 size={13} /></button>}
                      </div>
                    </td>
                  </tr>
                ))}
                {!jobs.length && loaded && <tr><td colSpan={5}><Empty icon={<CalendarClock size={30} />} title={loadError ? "Couldn't load" : "No jobs"} sub={loadError || undefined} /></td></tr>}
              </tbody>
            </table>
          </section>
        </div>
      </main>
    </div>
  );
}
