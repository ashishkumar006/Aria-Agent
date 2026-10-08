import { useCallback, useEffect, useRef, useState } from 'react';
import { Receipt } from 'lucide-react';
import { Rail, TopBar, Empty, Skel, Stat, SkipLink } from '../components/ui';
import { api, CONF, displayTopic, type SkillRow, type TurnRow } from '../api';

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
  // Recent conversation ids for the picker (labelled by the
  // topic, not the raw skill prompt); manual paste still works
  // for ids outside this list.
  const [recent, setRecent] = useState<{ id: string; label: string }[]>([]);

  /* Node reliability. Separate state from the spend load on purpose: spend and
     reliability answer different questions, and one failing must not blank
     the other. */
  const [nodeHealth, setNodeHealth] = useState<{
    sessions: number;
    rows: import('../api').NodeHealthRow[];
    totals: { nodes: number; failed: number; fail_pct: number };
  } | null>(null);
  const [nodeLoaded, setNodeLoaded] = useState(false);
  const [nodeErr, setNodeErr] = useState('');

  const loadNodes = useCallback(async () => {
    setNodeErr('');
    try {
      const d = await api.nodeHealth();
      setNodeHealth(d);
    } catch (e) {
      setNodeErr((e as Error)?.message || String(e));
    } finally {
      setNodeLoaded(true);
    }
  }, []);

  useEffect(() => { void loadNodes(); }, [loadNodes]);

  const refreshRecent = useCallback(() => {
    // Both conversation kinds are billable: research runs (s8-)
    // and lightweight chat threads (ct-). The picker must offer
    // both, or chat spend is unreachable without a manual paste.
    Promise.allSettled([api.sessions(50), api.chatThreads(50)]).then(
      ([sess, th]) => {
        const seen = new Set<string>();
        /* An id containing a path separator is a stale stored key; the
           cost endpoint answers 400 for it, so offering it only produces
           an error the user cannot act on. */
        const usable = (id: string) =>
          !!id && !seen.has(id) && !/[/\\]/.test(id) && !id.includes('..');
        const chatOpts: { id: string; label: string }[] = [];
        const runOpts: { id: string; label: string }[] = [];
        if (th.status === 'fulfilled') {
          for (const t of th.value.threads || []) {
            const id = t.conversation_id || '';
            if (!usable(id)) continue;
            seen.add(id);
            const q = (t.title || '(chat)').slice(0, 42);
            chatOpts.push({ id, label: `chat: ${q} · ${id.slice(0, 8)}` });
          }
        }
        if (sess.status === 'fulfilled') {
          for (const s of sess.value.sessions || []) {
            const id = s.conversation_id || '';
            if (!usable(id)) continue;
            seen.add(id);
            // The topic, not `query`: for research runs `query`
            // is the skill instruction, which would label every
            // entry with the same unreadable paragraph.
            const q = (displayTopic(s) || '(untitled)').slice(0, 42);
            runOpts.push({ id, label: `${q} · ${id.slice(0, 8)}` });
          }
        }
        /* Chat threads FIRST. The old order pushed up to 50 research
           conversations and then `slice(0, 30)`, so with 29 research runs
           present EVERY chat thread was pushed out — the opposite of the
           comment's intent, and chat spend needed a manual paste. */
        setRecent([...chatOpts.slice(0, 15), ...runOpts].slice(0, 30));
      },
      () => { /* the picker falls back to manual paste */ },
    );
  }, []);

  useEffect(() => {
    refreshRecent();
    // Keep the picker fresh while it is the active scope: a
    // conversation created after mount was unreachable in the
    // picker until a full reload.
    if (scope !== 'one') return;
    const t = setInterval(refreshRecent, CONF.pollSlowMs);
    return () => clearInterval(t);
  }, [refreshRecent, scope]);

  // Bumped per load: the 15s poll can be slower than its own
  // interval on a slow API, and an older response landing after
  // a newer one would present a stale table as current.
  const loadSeq = useRef(0);

  const scopedCid = scope === 'one' && !!appliedCid.trim();
  /* The gateway reports `dollars: 0.0` on every provider row and every
     turn_cost on disk is `usd: 0.0`, so this page rendered a confident
     "$0.0000 lifetime USD" over millions of billed tokens. That zero means
     "no pricing data", not "no spend" — one is a gap, the other a wrong
     number. */
  const hasSpend = rows.length > 0 && rows.some((r) => Number(r.dollars || 0) !== 0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
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
      if (!aliveRef.current || seq !== loadSeq.current) return;
/* An id nothing was ever billed to is not "zero spend" — it is no
         * such conversation. BUT the server also sets this flag on ids that
         * DO have a ledger on disk: `unknown_conv` is only computed for
         * non-`ct-` ids, so a thread whose directory exists under
         * state/threads/ but is missing from chat_threads.json comes back
         * with real `rows` AND `unknown_conversation: true`. Checking the
         * flag first deleted those rows and told the user "nothing was
         * ever billed to it" while the response body held the spend —
         * 94 calls invisible in the all-time view. Rows win. */
      if (d.unknown_conversation && !(d.rows || []).length) {
        setRows([]);
        setTurns([]);
        setTotals({});
        setLoaded(true);
        setLoadError('');
        setScopeError(`no conversation with id "${appliedCid.trim().slice(0, 24)}" — nothing was ever billed to it.`);
        return;
      }
      setRows(d.rows || []);
      setTurns(d.turns || []);
      setTotals(d.totals || {});
      setLoaded(true);
      setLoadError('');
      // A successful load replaces whatever the empty-scope
      // prompt said — it must not linger over real data.
      setScopeError('');
    } catch (e) {
      /* Swallowed before, so an unreachable API showed "No spend yet" —
         indistinguishable from genuinely zero spend. */
      if (!aliveRef.current || seq !== loadSeq.current) return;
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
      <SkipLink />
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
      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Ledger" />
        <div className="min-h-0 flex-1 overflow-y-auto">
          {/* A failed refresh with rows still on screen used to
              look like success — the table read as current while
              the API was down. Say which it is. */}
          {loaded && loadError && !!rows.length && (
            <div className="mx-3.5 mt-3 rounded-lg border border-amber-300/30 bg-amber-300/5 p-2.5 text-[11px] text-amber-200">
              refresh failed ({loadError}) — showing last data.
              <button onClick={load} className="ml-1.5 underline">retry</button>
            </div>
          )}
          {/* Three of these four tiles asserted something they cannot know. The
              gateway reports `dollars: 0.0` for every provider row and
              every turn_cost on disk is `usd: 0.0`, so the page rendered a
              confident "$0.0000 lifetime USD" over 5.7M billed tokens — a
              zero that means "no pricing data", not "no spend". Saying so is
              the difference between a missing number and a wrong one.
              Likewise "lifetime" was hardcoded under a single-conversation
              scope, and "with spend" counted rows that all read $0.0000. */}
          <div className="flex flex-wrap gap-2.5 p-3.5 pb-0">
            <Stat
              k="SPEND"
              v={hasSpend ? `$${Number(totals.dollars || 0).toFixed(4)}` : 'n/a'}
              s={hasSpend ? (scopedCid ? 'this conversation' : 'lifetime USD')
                          : 'cost data unavailable'}
            />
            <Stat k="CALLS" v={String(totals.calls || 0)} s="llm calls" />
            <Stat k="SKILLS" v={String(rows.length)} s={hasSpend ? 'with spend' : 'ran'} />
            <Stat k="TURNS" v={String(turns.length)}
              s={scopedCid ? 'in this conversation' : 'recorded (most recent 200)'} />
          </div>
          {/* ── NODE RELIABILITY ──────────────────────────────────────────
              Spend alone cannot tell you whether the work happened. This
              section answers the question a spend table hides: of the nodes a
              skill ran, how many failed, and WHY.

              The percentage is against THAT SKILL's own node count, not the
              grand total - "author failed 38% of its nodes" is actionable,
              "4 nodes failed overall" is not. Reasons are bucketed into a
              fixed vocabulary because free-text errors do not aggregate: 65
              author failures looked like 65 problems until they were grouped,
              and they were three. */}
          <div className="px-3.5 pb-1 pt-5 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">
            NODE RELIABILITY
            {nodeHealth && (
              <span className="ml-2 font-normal normal-case tracking-normal text-zinc-600">
                last {nodeHealth.sessions} run{nodeHealth.sessions === 1 ? '' : 's'}
                {' · '}
                {nodeHealth.totals.failed} of {nodeHealth.totals.nodes} nodes failed
                {' '}({nodeHealth.totals.fail_pct}%)
              </span>
            )}
          </div>
          <section className="mx-3.5 overflow-x-auto rounded-[10px] border border-white/10 bg-[#0e0e12]">
            {!nodeLoaded ? <Skel n={3} /> : nodeHealth?.rows?.length ? (
              <table className="w-full min-w-[640px] border-collapse text-[12.5px]">
                <thead>
                  <tr className="border-b border-white/10 text-left text-[10px] tracking-[0.1em] text-zinc-muted">
                    <th className="px-2.5 py-2 font-semibold">Skill</th>
                    <th className="px-2.5 py-2 text-right font-semibold">Nodes</th>
                    <th className="px-2.5 py-2 text-right font-semibold">Failed</th>
                    <th className="px-2.5 py-2 text-right font-semibold">Fail rate</th>
                    <th className="px-2.5 py-2 font-semibold">Why</th>
                  </tr>
                </thead>
                <tbody>
                  {nodeHealth.rows.map((r) => (
                    <tr key={r.skill} className="border-b border-white/5 hover:bg-white/[0.02]">
                      <td className="px-2.5 py-2">{r.skill}</td>
                      <td className="px-2.5 py-2 text-right tabular-nums">{r.nodes}</td>
                      <td className="px-2.5 py-2 text-right tabular-nums">
                        {r.failed}
                        {r.skipped ? (
                          <span className="ml-1 text-zinc-600" title={`${r.skipped} skipped by recovery`}>
                            (+{r.skipped} skipped)
                          </span>
                        ) : null}
                      </td>
                      <td className="px-2.5 py-2 text-right tabular-nums">
                        <span className={
                          r.fail_pct >= 20 ? 'text-red-300 font-semibold'
                            : r.fail_pct >= 5 ? 'text-amber-200' : 'text-emerald-300'
                        }>
                          {r.fail_pct}%
                        </span>
                      </td>
                      <td className="px-2.5 py-2">
                        {r.reasons.length ? (
                          <ul className="space-y-0.5">
                            {r.reasons.map((x) => (
                              <li key={x.reason} className="text-[11.5px] text-zinc-400">
                                <span className="tabular-nums text-zinc-500">{x.count}×</span>
                                {' '}{x.reason}
                              </li>
                            ))}
                          </ul>
                        ) : (
                          <span className="text-[11.5px] text-zinc-600">no failures</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="px-3 py-4 text-[12px] text-zinc-600">
                {nodeErr
                  ? <>couldn't load node reliability ({nodeErr}). <button onClick={loadNodes} className="underline">retry</button></>
                  : 'No completed nodes in the recent runs.'}
              </div>
            )}
          </section>

          <div className="px-3.5 pb-1 pt-5 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">SPEND BY SKILL</div>
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
      </main>
    </div>
  );
}
