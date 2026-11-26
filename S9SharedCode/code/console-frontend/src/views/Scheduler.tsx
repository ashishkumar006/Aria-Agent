import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { CalendarClock, Play, Trash2, X } from 'lucide-react';
import { Rail, TopBar, Empty, Skel, Pill, Stat, SkipLink } from '../components/ui';
import { useMainId, useActivePoll } from '../components/KeepAlive';
import { api, ago, fmtFire, CONF, type ScheduleJob } from '../api';
import { Pane, PaneStrip, usePaneMode } from '../lib/panes';

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

type Job = ScheduleJob & { key: string };

const isWake = (j: Job) => j.kind === 'wake';

/* A wake's `when` is stored as `every 3600s` because that is what the parser
   round-trips. Nobody reads that. The cadence is said the way the user said it
   in the first place, and anything that does not map to a word keeps the
   seconds rather than lying about it. */
function cadence(stepS?: number): string {
  const s = Number(stepS || 0);
  if (!s) return '';
  if (s % 86400 === 0) return s === 86400 ? 'daily' : `every ${s / 86400}d`;
  if (s % 3600 === 0) return s === 3600 ? 'hourly' : `every ${s / 3600}h`;
  if (s % 60 === 0) return `every ${s / 60}m`;
  return `every ${s}s`;
}

/* Raw ISO and bare epochs came straight from the model's own `when` string, so
   a one-shot reminder read "2027-01-01T09:00:00" in a column beside "every 2h".
   Anything that parses as a date is shown the way every other date in the
   console is shown; a phrase is shown as the phrase. */
function humanWhen(j: Job): string {
  if (isWake(j)) return cadence(j.step_s) || cadence(0) || j.when || 'on change';
  const w = String(j.when || j.recurring || '').trim();
  if (!w) return '—';
  if (/^\d{4}-\d{2}-\d{2}[T ]/.test(w) || /^\d{9,10}$/.test(w)) {
    const ms = /^\d+$/.test(w) ? Number(w) * 1000 : Date.parse(w);
    if (!Number.isNaN(ms)) {
      return new Date(ms).toLocaleString(undefined,
        { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
    }
  }
  return w;
}

/* One line, not three. The surface and the terms it must match are the whole
   story of a watch, and they fit in a phrase; the exact JSON stays in the
   tooltip for whoever wants to check it. */
function watchSummary(j: Job): string {
  const w = j.watch;
  if (!w || (!w.surface && !w.match)) return 'any change';
  const terms = Object.entries(w.match || {}).map(([k, v]) => `${k}: ${v}`).join(', ');
  return [w.surface || 'anything', terms].filter(Boolean).join(' · ');
}

/* `last_error` on a wake is usually not an error: a watch that ran out its 24
   hours, or one the switch is holding, is an ending or a pause. Saying "failed"
   in red about both is the kind of lie that trains people to ignore the column. */
function notice(j: Job): { text: string; bad: boolean } | null {
  if (!j.last_error) return null;
  if (!isWake(j)) return { text: 'failed', bad: true };
  if (!j.enabled) return { text: 'watch ended', bad: false };
  return { text: 'paused', bad: false };
}

/* A row whose query is an instruction to the agent ("remind me to X") is
   re-executed at fire time and schedules itself again, so the user receives a
   confirmation instead of the reminder. The scheduler now converts those to
   nudges, but a row armed before that fix is still in the store and must be
   called out rather than listed as healthy. */
const REMINDER_IMPERATIVE = /^\s*(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(?:remind\s+(?:me|us)|reminder\s+(?:to|for)|nudge\s+me|ping\s+me|tell\s+me\s+to)\b/i;
function selfScheduling(j: Job): boolean {
  if (isWake(j)) return false;
  const q = String(j.query || '');
  if (!REMINDER_IMPERATIVE.test(q)) return false;
  // If the row also carries the words to send AND they differ from the query,
  // the nudge path is doing its job; only a row with no message loops.
  const msg = String(j.notify_policy?.message || '').trim();
  return !msg;
}

/* Order the list the way the user reads it: what is about to happen first,
   and what already happened last.

   The backend returns rows in JSON-key order, so before this the table was
   effectively unordered - a reminder created last week could sit above one
   due in ten minutes.

   There is no `status` field on a schedule, so "finished" is derived:
   `enabled: false` means either cancelled or an already-fired one-shot, and
   either way it is not going to fire again. A non-recurring job that is
   still enabled but whose `next_fire` is behind the clock is overdue - it
   is still armed, so it stays up with the live rows rather than being filed
   away with the dead ones.

   Recurring rows never "finish": they re-arm, so they stay in the live
   group forever. */
export function orderJobs(jobs: Job[]): Job[] {
  const isLive = (j: Job) => !!j.enabled;
  const dueOf = (j: Job) => (j.next_fire || j.created || 0);
  // Within the dead group, most recently active first: the one you just ran
  // should be the first finished thing you see, not the oldest.
  const doneOf = (j: Job) => (j.last_fire || j.next_fire || j.created || 0);

  return [...jobs].sort((a, b) => {
    const la = isLive(a);
    const lb = isLive(b);
    if (la !== lb) return la ? -1 : 1;
    if (la) return dueOf(a) - dueOf(b);
    return doneOf(b) - doneOf(a);
  });
}

export default function Scheduler() {
  const mainId = useMainId();
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

  // The list only mutates when a job fires (minutes/hours away), so a flat
  // 2s poll while armed is 7.5A- the documented list cadence for static
  // data. Go fast only when a firing is actually imminent (<60s out).
  const nowS = Date.now() / 1000;
  const imminent = jobs.some((j) => j.enabled && (j.next_fire || 0) > 0 && (j.next_fire || 0) - nowS < 60);
  useActivePoll(load, imminent ? CONF.pollFastMs : CONF.pollSlowMs);

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

  const toggle = async (id: string, enabled: boolean) => {
    setBusy(true);
    try {
      const d = await api.scheduleToggle(id, enabled);
      setOut(d.ok ? (d.error ? `${d.schedule_id}: ${d.error}` : `${enabled ? 'enabled' : 'disabled'} ${id}`)
                  : 'failed');
      load();
    } catch (e) { setOut(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  const ordered = orderJobs(jobs);
  const active = ordered.filter((j) => j.enabled);

  /* Two different promises were being read as one list. A reminder is "run this
     task at that time"; a watch is "look, cheaply and repeatedly, until
     something specific has happened" — and the wake rows are taller and denser
     than the job rows, so a flat table of both looked like noise. Grouped by
     kind first, then by whether it can still fire, each group labelled and each
     group omitted when empty: an all-reminders board renders exactly as it did
     with one heading instead of four. */
  const groupsOf = (rows: Job[]) => ({
    live: orderJobs(rows.filter((j) => j.enabled)),
    done: orderJobs(rows.filter((j) => !j.enabled)),
  });
  const wakeG = groupsOf(ordered.filter(isWake));
  const jobG = groupsOf(ordered.filter((j) => !isWake(j)));
  const groups: { label: string; rows: Job[] }[] = [
    { label: 'WATCHES · ARMED', rows: wakeG.live },
    { label: 'WATCHES · ENDED', rows: wakeG.done },
    { label: 'JOBS · ARMED', rows: jobG.live },
    { label: 'JOBS · FINISHED', rows: jobG.done },
  ].filter((g) => g.rows.length);
  const wakeCount = wakeG.live.length + wakeG.done.length;

  /* The Jobs column collapses rather than hides: its load-failure banner holds
     this view's only retry, so it has to leave a strip behind to come back by.
     The mode is read here — this is a hook — and applied at the block's two
     boundaries, leaving the column's own JSX byte for byte. */
  const railMode = usePaneMode('scheduler.rail');

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      {railMode === 'collapsed'
        ? <PaneStrip id="scheduler.rail" label="Jobs" />
        : (
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
          {groups.map((g) => (
            <div key={g.label} className="space-y-2">
              <div className="px-0.5 pt-1 text-[10px] font-bold tracking-[0.14em] text-zinc-muted">
                {g.label} · {g.rows.length}
              </div>
              {g.rows.map((j) => {
                const n = notice(j);
                return (
                  <div key={j.key} className="rounded-[10px] border border-white/10 bg-[#0e0e12] p-2.5">
                    {/* Title and state on one line, cadence and timing on the
                        next. The wake's badges used to sit on their own third
                        line, which made the rail read as three different kinds
                        of row instead of two kinds of job. */}
                    <div className="flex items-center gap-2">
                      <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold"
                            title={isWake(j) ? j.reason : j.query}>
                        {isWake(j) ? j.reason : j.query || '(no query)'}
                      </span>
                      <Pill tone={j.enabled ? 'info' : 'muted'}>{j.enabled ? 'scheduled' : 'off'}</Pill>
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-zinc-muted">
                      <span className="rounded border border-violet-400/30 bg-violet-400/10 px-1.5 py-0.5 font-mono text-[10.5px] text-violet-200"
                            title={j.when || j.recurring || ''}>{humanWhen(j) || '—'}</span>
                      <span className="truncate">{j.enabled ? fmtFire(j.next_fire) : lastFire(j.last_fire) || 'cancelled'}</span>
                    </div>
                    {isWake(j) && (
                      <div className="mt-1 flex items-baseline gap-1.5 text-[10.5px] text-zinc-muted">
                        <span className={`flex-none ${j.armed_by === 'agent' ? 'text-amber-200' : ''}`}>
                          {j.armed_by === 'agent' ? "Aria's watch" : 'your watch'}
                        </span>
                        <span className="flex-none"
                              title="turns this watch may still take. Ticks that find nothing cost nothing and are not counted; this counts the turns it spends.">
                          {j.wakes_left ?? 0} left
                        </span>
                        <span className="min-w-0 flex-1 truncate" title={`${watchSummary(j)} · ${JSON.stringify(j.watch || {})}`}>
                          · {watchSummary(j)}
                        </span>
                      </div>
                    )}
                    {j.notify_policy?.message && (
                      <div className="mt-1 truncate text-[10.5px] text-zinc-300"
                           title={j.notify_policy.message}>
                        <span className="text-zinc-500">says: </span>
                        {j.notify_policy.message}
                      </div>
                    )}
                    {selfScheduling(j) && (
                      <div className="mt-1 text-[10.5px] text-red-300"
                           title="This row's query is an instruction to Aria, so it re-schedules itself at every fire instead of reminding you. Cancel it and ask again - new reminders are stored as text to say.">
                        re-schedules itself - cancel and re-create
                      </div>
                    )}
                    {n && (
                      <div className={`mt-1 truncate text-[10.5px] ${n.bad ? 'text-red-300' : 'text-zinc-muted'}`}
                           title={j.last_error}>
                        {n.text}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      </div>
        )}
      <main id={mainId} tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Scheduler" panes="scheduler" />
        <div className="min-h-0 flex-1 overflow-y-auto">
          <Pane id="scheduler.stats">
          <div className="flex flex-wrap gap-2.5 p-3.5 pb-0">
            <Stat k="JOBS" v={String(jobs.length)} s="total scheduled" loading={!loaded} />
            <Stat k="ACTIVE" v={String(active.length)} s="armed & waiting" loading={!loaded}
              tone={active.length ? 'ok' : undefined} />
            <Stat k="NEXT FIRE"
              v={active.length ? fmtFire(active[0].next_fire).split(' (')[0] : '—'}
              s={!loaded ? 'reading schedule…' : active.length
                ? (isWake(active[0])
                    ? `${watchSummary(active[0])} · ${humanWhen(active[0])}`
                    : (active[0].query || '').slice(0, 40))
                : 'no active jobs'}
              loading={!loaded} />
          </div>
          </Pane>
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
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">
            ALL JOBS · {loaded
              ? `${jobs.length} total · ${active.length} active${wakeCount ? ` · ${wakeCount} watch${wakeCount === 1 ? '' : 'es'}` : ''}`
              : 'reading jobs…'}
          </div>
          <section className="mx-3.5 mb-3.5 overflow-x-auto rounded-[10px] border border-white/10 bg-[#0e0e12]">
            <table className="w-full min-w-[560px] border-collapse text-[12.5px]">
              <thead>
                <tr className="border-b border-white/10 text-left text-[10px] tracking-[0.1em] text-zinc-muted">
                  <th className="w-[34%] px-2.5 py-2 font-semibold">What</th>
                  <th className="w-[132px] px-2.5 py-2 font-semibold">Kind</th>
                  <th className="w-[128px] px-2.5 py-2 font-semibold">When</th>
                  <th className="px-2.5 py-2 font-semibold">Next fire</th>
                  <th className="px-2.5 py-2 font-semibold">State</th>
                  <th className="px-2.5 py-2" />
                </tr>
              </thead>
              <tbody>
                {/* Every row is one line tall. A wake used to stack a badge
                    row, a two-line wrapped reason and a third "watching ..."
                    line inside one cell, so its rows measured 96px beside a
                    reminder's 43px and the table read as clutter. The facts did
                    not go away: they moved into columns every row can use, and
                    anything that does not fit truncates with the full text in
                    the tooltip. Grouped by kind, then by whether it can still
                    fire, so a watch is never mistaken for a reminder. */}
                {groups.map((g) => (
                  <Fragment key={g.label}>
                    <tr>
                      <td colSpan={6}
                          className="border-b border-white/10 bg-white/[0.02] px-2.5 py-1.5 text-[10px] font-bold tracking-[0.14em] text-zinc-muted">
                        {g.label} · {g.rows.length}
                      </td>
                    </tr>
                    {g.rows.map((j) => {
                      const n = notice(j);
                      return (
                        <tr key={j.key} className="border-b border-white/5 hover:bg-white/[0.02]">
                          <td className="max-w-0 px-2.5 py-2">
                            {/* The reason and what triggers it are one sentence
                                to a reader: "watch for X, on documents ·
                                filename: Y". Splitting them across columns made
                                the trigger truncate to "documents · filename:
                                wi…" in a column whose header says Kind. */}
                            <span className="flex items-baseline gap-1.5">
                              <span className="min-w-0 max-w-[56%] shrink truncate"
                                    title={isWake(j) ? j.reason : j.query}>
                                {isWake(j) ? j.reason : j.query || '(no query)'}
                              </span>
                              {isWake(j) && (
                                <span className="min-w-0 flex-1 truncate text-[10.5px] text-zinc-muted"
                                      title={`${watchSummary(j)} · ${JSON.stringify(j.watch || {})}`}>
                                  · {watchSummary(j)}
                                </span>
                              )}
                            </span>
                          </td>
                          <td className="px-2.5 py-2">
                            {isWake(j) ? (
                              <Pill tone={j.armed_by === 'agent' ? 'warn' : 'info'}>
                                {j.armed_by === 'agent' ? "Aria's watch" : 'your watch'}
                              </Pill>
                            ) : (
                              <span className="whitespace-nowrap text-[10.5px] text-zinc-muted">
                                {j.recurring ? 'recurring' : 'one-off'}
                              </span>
                            )}
                          </td>
                          <td className="px-2.5 py-2">
                            <span className="whitespace-nowrap rounded border border-violet-400/30 bg-violet-400/10 px-1.5 py-0.5 font-mono text-[10.5px] text-violet-200"
                                  title={j.when || j.recurring || ''}>{humanWhen(j) || '—'}</span>
                          </td>
                          <td className="whitespace-nowrap px-2.5 py-2 tabular-nums text-[11.5px]">
                            {j.enabled ? fmtFire(j.next_fire) : lastFire(j.last_fire) || '—'}
                          </td>
                          <td className="px-2.5 py-2">
                            <span className="flex items-center gap-1.5">
                              <Pill tone={j.enabled ? 'info' : 'muted'}>{j.enabled ? 'scheduled' : 'off'}</Pill>
                              {isWake(j) && (
                                <span className="whitespace-nowrap text-[10.5px] text-zinc-muted"
                                      title="turns this watch may still take. Ticks that find nothing cost nothing and are not counted; this counts the turns it spends.">
                                  {j.wakes_left ?? 0} left
                                </span>
                              )}
                              {n && (
                                <span className={`truncate text-[10.5px] ${n.bad ? 'text-red-300' : 'text-zinc-muted'}`}
                                      title={j.last_error}>{n.text}</span>
                              )}
                            </span>
                          </td>
                          <td className="px-2.5 py-2 text-right">
                            <span className="flex items-center justify-end gap-1.5">
                              {!j.enabled && j.id && <button onClick={() => toggle(j.id, true)} title="Enable" aria-label={`Enable job ${j.reason || j.query || j.key}`} className="flex min-h-6 min-w-6 items-center justify-center rounded-md border border-white/15 px-2 py-1 hover:border-ok hover:text-ok"><Play size={13} /></button>}
                              {j.enabled && j.id && <button onClick={() => cancel(j.id)} title="Cancel" aria-label={`Cancel job ${j.reason || j.query || j.key}`} className="flex min-h-6 min-w-6 items-center justify-center rounded-md border border-white/15 px-2 py-1 hover:border-red-400 hover:text-red-300"><X size={13} /></button>}
                              {!j.enabled && j.id && <button onClick={() => remove(j.id)} title="Delete" aria-label={`Delete job ${j.reason || j.query || j.key}`} className="flex min-h-6 min-w-6 items-center justify-center rounded-md border border-white/15 px-2 py-1 hover:border-red-400 hover:text-red-300"><Trash2 size={13} /></button>}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </Fragment>
                ))}
                {!jobs.length && loaded && <tr><td colSpan={6}><Empty icon={<CalendarClock size={30} />} title={loadError ? "Couldn't load" : "No jobs"} sub={loadError || undefined} /></td></tr>}
              </tbody>
            </table>
          </section>
        </div>
      </main>
    </div>
  );
}
