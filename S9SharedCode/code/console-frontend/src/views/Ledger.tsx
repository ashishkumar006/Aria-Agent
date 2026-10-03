import { useCallback, useEffect, useRef, useState } from 'react';
import { Receipt } from 'lucide-react';
import { Rail, TopBar, Empty, Skel, Stat } from '../components/ui';
import { api, CONF, type SkillRow, type TurnRow } from '../api';

export default function Ledger() {
  const [scope, setScope] = useState<'all' | 'one'>('all');
  const [cid, setCid] = useState('');
  const [rows, setRows] = useState<SkillRow[]>([]);
  const [turns, setTurns] = useState<TurnRow[]>([]);
  const [totals, setTotals] = useState<{ dollars?: number; calls?: number; skills?: number }>({});
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState('');
  // Set when the chosen scope cannot be satisfied (e.g. "one conversation"
  // with no id picked). Distinct from a load failure: nothing went wrong,
  // the view is just incomplete, and it must not read as "no spend".
  const [scopeError, setScopeError] = useState('');
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  // Submitted scope only: typing a conversation id must not refetch (the
  // old version reloaded + reset the 15s interval on every keystroke).
  const [appliedCid, setAppliedCid] = useState('');
  // Recent conversation ids for the picker (labelled by query); manual
  // paste still works for ids outside this list.
  const [recent, setRecent] = useState<{ id: string; label: string }[]>([]);

  useEffect(() => {
    let live = true;
    (async () => {
      // Both conversation kinds are billable now: research runs (s8-) and
      // lightweight chat threads (ct-). The picker must offer both, or chat
      // spend is unreachable without a manual paste.
      const [sess, th] = await Promise.allSettled([api.sessions(50), api.chatThreads(50)]);
      if (!live) return;
      const seen = new Set<string>();
      const opts: { id: string; label: string }[] = [];
      if (sess.status === 'fulfilled') {
        for (const s of sess.value.sessions || []) {
          const id = s.conversation_id || '';
          if (!id || seen.has(id)) continue;
          seen.add(id);
          const q = (s.query || '(untitled)').slice(0, 42);
          opts.push({ id, label: `${q} · ${id.slice(0, 8)}` });
        }
      }
      if (th.status === 'fulfilled') {
        for (const t of th.value.threads || []) {
          const id = t.conversation_id || '';
          if (!id || seen.has(id)) continue;
          seen.add(id);
          const q = (t.title || '(chat)').slice(0, 42);
          opts.push({ id, label: `chat: ${q} · ${id.slice(0, 8)}` });
        }
      }
      setRecent(opts.slice(0, 30));
    })().catch(() => {});
    return () => { live = false; };
  }, []);

  const load = useCallback(async () => {
    try {
      // Scope "one" with an EMPTY id used to fall back to all-time, so the
      // button said "One conversation" while the table showed the whole
      // lifetime. A bogus id correctly showed zero, which made the empty case
      // the only unguarded path. Ask for the id instead.
      if (scope === 'one' && !appliedCid.trim()) {
        if (!aliveRef.current) return;
        setRows([]);
        setTurns([]);
        setTotals({});
        setLoaded(true);
        setLoadError('');
        setScopeError('Pick a conversation to see its spend.');
        return;
      }
      const d = await api.bySkill(scope === 'one' ? appliedCid.trim() : undefined);
      if (!aliveRef.current) return;
      setRows(d.rows || []);
      setTurns(d.turns || []);
      setTotals(d.totals || {});
      setLoaded(true);
      setLoadError('');
    } catch (e) {
      /* Swallowed before, so an unreachable API showed "No spend yet" —
         indistinguishable from genuinely zero spend. */
      if (!aliveRef.current) return;
      setLoaded(true);
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, [scope, appliedCid]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const t = setInterval(load, CONF.pollSlowMs);
    return () => clearInterval(t);
  }, [load]);

  const applyScope = (s: 'all' | 'one') => {
    setScope(s);
    if (s === 'all') { setAppliedCid(''); setScopeError(''); }
    else setAppliedCid(cid.trim());
  };

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
        <div className="px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">Scope</div>
        <div className="space-y-2 px-2.5">
          {(['all', 'one'] as const).map((s) => (
            <button
              key={s}
              onClick={() => applyScope(s)}
              className={`block w-full rounded-[10px] border p-2.5 text-left text-[12.5px] font-semibold ${scope === s ? 'border-violet-400/60 bg-violet-400/5' : 'border-white/10 bg-[#0e0e12] hover:border-white/25'}`}
            >
              {s === 'all' ? 'All time' : 'One conversation'}
            </button>
          ))}
          {scope === 'one' && (
            <>
              {!!recent.length && (
                <select
                  value=""
                  onChange={(e) => {
                    if (!e.target.value) return;
                    setCid(e.target.value);
                    setAppliedCid(e.target.value);
                  }}
                  aria-label="Pick a recent conversation"
                  className="w-full rounded-md border border-white/10 bg-black/40 px-2 py-1.5 text-xs text-zinc-300 outline-none focus:border-violet-400"
                >
                  <option value="">pick recent…</option>
                  {recent.map((r) => (
                    <option key={r.id} value={r.id}>{r.label}</option>
                  ))}
                </select>
              )}
              <input
                value={cid} onChange={(e) => setCid(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') setAppliedCid(cid.trim()); }}
                onBlur={() => setAppliedCid(cid.trim())}
                placeholder="…or paste conversation id"
                aria-label="Conversation id"
                className="w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 font-mono text-xs outline-none focus:border-violet-400"
              />
            </>
          )}
        </div>
        <div className="mt-auto hidden border-t border-white/10 px-3.5 py-2.5 text-[11px] text-zinc-muted lg:block">refresh {CONF.pollSlowMs / 1000}s</div>
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <TopBar crumb="Ledger" />
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex flex-wrap gap-2.5 p-3.5 pb-0">
            <Stat k="SPEND" v={`$${Number(totals.dollars || 0).toFixed(4)}`} s="lifetime USD" />
            <Stat k="CALLS" v={String(totals.calls || 0)} s="llm calls" />
            <Stat k="SKILLS" v={String(rows.length)} s="with spend" />
            <Stat k="TURNS" v={String(turns.length)} s="recorded" />
          </div>
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">SPEND BY SKILL</div>
          <section className="mx-3.5 overflow-x-auto rounded-[10px] border border-white/10 bg-[#0e0e12]">
            {!loaded ? <Skel n={4} /> : (
              <table className="w-full min-w-[560px] border-collapse text-[12.5px]">
                <thead>
                  <tr className="border-b border-white/10 text-left text-[10px] tracking-[0.1em] text-zinc-muted">
                    <th className="px-2.5 py-2 font-semibold">Skill</th>
                    <th className="px-2.5 py-2 text-right font-semibold">Calls</th>
                    <th className="px-2.5 py-2 text-right font-semibold">In tokens</th>
                    <th className="px-2.5 py-2 text-right font-semibold">Out tokens</th>
                    <th className="px-2.5 py-2 text-right font-semibold">USD</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={`${r.skill || 'unknown'}-${i}`} className="border-b border-white/5 tabular-nums hover:bg-white/[0.02]">
                      <td className="px-2.5 py-2">{r.skill}</td>
                      <td className="px-2.5 py-2 text-right">{r.calls}</td>
                      <td className="px-2.5 py-2 text-right">{r.in_tokens}</td>
                      <td className="px-2.5 py-2 text-right">{r.out_tokens}</td>
                      <td className="px-2.5 py-2 text-right">${Number(r.dollars || 0).toFixed(4)}</td>
                    </tr>
                  ))}
                  {!rows.length && <tr><td colSpan={5}>{loadError ? <div className="px-3 py-4 text-xs text-red-200">couldn't load spend ({loadError}). <button onClick={load} className="underline">retry</button></div> : scopeError ? <div className="px-3 py-4 text-xs text-amber-200">{scopeError}</div> : <Empty icon={<Receipt size={30} />} title="No spend yet" sub="Chat or research something and costs land here." />}</td></tr>}
                </tbody>
              </table>
            )}
          </section>
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">TURN HISTORY</div>
          <section className="mx-3.5 mb-3.5 overflow-x-auto rounded-[10px] border border-white/10 bg-[#0e0e12]">
            <table className="w-full min-w-[560px] border-collapse text-[12.5px]">
              <thead>
                <tr className="border-b border-white/10 text-left text-[10px] tracking-[0.1em] text-zinc-muted">
                  <th className="px-2.5 py-2 font-semibold">Time</th>
                  <th className="px-2.5 py-2 font-semibold">Query</th>
                  <th className="px-2.5 py-2 text-right font-semibold">Calls</th>
                  <th className="px-2.5 py-2 text-right font-semibold">USD</th>
                </tr>
              </thead>
              <tbody>
                {turns.map((x, i) => (
                  <tr key={`${x.ts}-${i}`} className="border-b border-white/5 tabular-nums hover:bg-white/[0.02]">
                    <td className="whitespace-nowrap px-2.5 py-2">{x.ts ? new Date(x.ts * 1000).toLocaleString() : '—'}</td>
                    <td className="px-2.5 py-2">{x.query || '(no query)'}</td>
                    <td className="px-2.5 py-2 text-right">{x.calls}</td>
                    <td className="px-2.5 py-2 text-right">${Number(x.usd || 0).toFixed(4)}</td>
                  </tr>
                ))}
                {!turns.length && loaded && <tr><td colSpan={4}><div className="px-3 py-4 text-xs text-zinc-muted">no turns recorded</div></td></tr>}
              </tbody>
            </table>
          </section>
        </div>
      </div>
    </div>
  );
}
