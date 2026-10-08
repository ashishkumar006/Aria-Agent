import { useCallback, useEffect, useState } from 'react';
import { Boxes } from 'lucide-react';
import { Rail, TopBar, Empty, Skel, Pill, Stat, SkipLink } from '../components/ui';
import { api, CONF, type ToolSpec } from '../api';

const PACKS = [
  { id: 'research', name: 'Web Research', ico: '◉',
    desc: 'Search the web, fetch pages and PDFs, query memory and index documents for retrieval.',
    tools: ['web_search', 'fetch_url', 'fetch_pdf', 'wayback_fetch',
      'news_search', 'wikipedia_search', 'openalex_search', 'arxiv_search',
      'extract_tables', 'search_knowledge', 'index_document',
      'verify_citations', 'recall_preferences', 'remember_preference',
      'search_files'] },
  { id: 'comms', name: 'Messaging', ico: '✉',
    desc: 'Email, Telegram, Slack and Discord — read, send and stay in the loop.',
    tools: ['send_email', 'gmail_query', 'gmail_refresh_token', 'send_telegram',
      'slack_history', 'slack_message', 'slack_refresh_token', 'discord_message'] },
  { id: 'plan', name: 'Calendar & Scheduling', ico: '◷',
    desc: 'Calendar events plus the built-in job scheduler the Scheduler page drives.',
    tools: ['create_calendar_event', 'calendar_query', 'calendar_refresh_token',
      'schedule_task', 'list_scheduled', 'cancel_scheduled'] },
  { id: 'files', name: 'Workspace Files', ico: '▤',
    desc: 'List, read, create, edit and delete files in the agent workspace.',
    tools: ['read_file', 'list_dir', 'create_file', 'update_file', 'edit_file', 'delete_file'] },
  { id: 'integrations', name: 'Integrations', ico: '❖',
    desc: 'GitHub, Notion and computer use.',
    tools: ['github_query', 'notion_query', 'computer_action'] },
];

function packOf(tool: string): string {
  return PACKS.find((p) => p.tools.includes(tool))?.name || 'Other';
}

export default function Skills() {
  const [catalog, setCatalog] = useState<ToolSpec[]>([]);
  const [disabled, setDisabled] = useState<Set<string>>(new Set());
  // Non-empty when the guard read failed: the withheld set is then UNKNOWN,
  // which must not be rendered as an all-clear.
  const [guardError, setGuardError] = useState('');
  // A tool/pack toggle that failed writes nothing, so the control must say so.
  const [toggleError, setToggleError] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [q, setQ] = useState('');
  // Which pack write is in flight, so the button can disable itself.
  const [packBusy, setPackBusy] = useState<string | null>(null);
  // Same for single-tool toggles.
  const [toolBusy, setToolBusy] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    try {
      const d = await api.tools();
      setCatalog(d.tools || []);
      try {
        const g = await api.toolsGuard();
        if (g.disabled === null || g.error) {
          // The guard read failed SERVER-SIDE: it answers
          // 200 with disabled:null, and an empty list here
          // would render as an authoritative all-clear —
          // exactly the failure mode this state exists for.
          setDisabled(new Set());
          setGuardError(g.error || 'tool guard state unknown');
        } else {
          setDisabled(new Set(g.disabled || []));
          setGuardError('');
        }
      } catch (e) {
        // Do NOT assume all live. This is the permission surface: a failed
        // guard read used to render "40/40 tools live / WITHHELD none", an
        // authoritative all-clear derived from a read that never happened.
        setGuardError(String((e as Error)?.message || e));
      }
      setLoaded(true);
      setLoadError('');
    } catch (e) {
      setLoaded(true);
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  /* The same /api/tools data is polled every 15s on Apps. Without this, a
     toggle made in Apps (or another tab) never appears here until reload. */
  useEffect(() => {
    const t = setInterval(load, CONF.pollSlowMs);
    return () => clearInterval(t);
  }, [load]);

  /* Single-tool toggle with the same pending-state discipline as flipPack:
     without it a double-click fired two POSTs that resolved in either
     order against a stale `disabled` set. */
  const flip = async (tool: string, on: boolean) => {
    if (toolBusy.has(tool)) return;
    setToolBusy(new Set(toolBusy).add(tool));
    try {
      const r = await api.setTool(tool, on);
      if (r.error) throw new Error(r.error);
      setDisabled(new Set(r.disabled || []));
      setToggleError('');
    } catch (e) {
      // The button reverting in silence read as the user's own mis-click.
      setToggleError(`${tool}: ${(e as Error)?.message || e}`);
      load();
    }
    finally {
      setToolBusy((prev) => {
        const next = new Set(prev);
        next.delete(tool);
        return next;
      });
    }
  };

  /* Sequential POSTs with no pending state meant a second click recomputed
     `anyLive` from the stale `disabled` set, so the two chains fought and
     the final load() decided the outcome by arrival order. Compute the
     target once, block re-entry, and show the pending state. */
  const flipPack = async (id: string) => {
    const p = PACKS.find((k) => k.id === id);
    if (!p || packBusy === id) return;
    // "Disable pack" must win when anything in the pack is still live.
    // `some(t => !disabled.has(t))` asked the opposite question: on an
    // all-live pack it was TRUE, so the Disable button POSTed
    // enabled:true for every tool and changed nothing (two of the three
    // label states were dead). Enable only when every tool is withheld.
    const off = p.tools.filter((t) => disabled.has(t)).length;
    const enable = off === p.tools.length;
    setPackBusy(id);
    try {
      // Do not swallow per-tool failures: `.catch(() => null)` made a pack
      // toggle that 500'd on all 5 tools look like it had worked.
      const results = await Promise.all(
        p.tools.map(async (t) => {
          try {
            const r = await api.setTool(t, enable);
            if (r.error) throw new Error(r.error);
            return null;
          } catch (e) {
            return `${t}: ${(e as Error)?.message || e}`;
          }
        }));
      const failed = results.filter(Boolean);
      setToggleError(failed.length
        ? `pack ${id}: ${failed.length} of ${p.tools.length} failed — ${failed[0]}`
        : '');
      await load();
    } finally {
      setPackBusy(null);
    }
  };

  const known = new Set(catalog.map((t) => t.name));
  const nLive = catalog.filter((t) => !disabled.has(t.name)).length;
  const ql = q.toLowerCase();
  const rows = catalog.filter((t) =>
    !ql || t.name.toLowerCase().includes(ql) ||
    (t.description || '').toLowerCase().includes(ql) ||
    packOf(t.name).toLowerCase().includes(ql));

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">
          Capabilities
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400">{PACKS.length} packs</span>
        </div>
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2.5 pb-3">
          {PACKS.map((p) => {
            // With the guard unreadable the withheld set is UNKNOWN, so
            // "live" is not knowable either. The pack header and the
            // rail footer used to compute a definite count anyway and
            // printed "live 15/15" next to the amber guard-error banner
            // — a false all-clear in the same viewport.
            const live = guardError
              ? 0
              : p.tools.filter((t) => known.has(t) && !disabled.has(t)).length;
            const off = p.tools.length - live;
            return (
              <div key={p.id} className="rounded-[10px] border border-white/10 bg-[#0e0e12] p-2.5">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold">{p.ico} {p.name}</span>
                  {/* "live" is info-blue and off/withheld is muted grey
                      everywhere else; this pack header used green + yellow
                      for the same two states the table below calls
                      muted "withheld". */}
                  <Pill tone={guardError ? 'err' : off ? 'muted' : 'info'}>
                    {guardError ? 'unknown' : off ? `${off} off` : 'live'}
                  </Pill>
                </div>
                <div className="mt-1 text-[11px] text-zinc-muted">
                  {guardError ? 'counts unknown' : `${live}/${p.tools.length} tools live`}
                </div>
              </div>
            );
          })}
        </div>
        <div className="hidden space-y-1.5 border-t border-white/10 px-3.5 py-2.5 text-[11px] lg:block">
          <div className="text-[10px] font-bold tracking-[0.12em] text-zinc-muted">LIVE TOOLS</div>
          <div>{guardError ? '?' : `${nLive} / ${catalog.length}`}</div>
          <div className="text-[10px] font-bold tracking-[0.12em] text-zinc-muted">WITHHELD</div>
          <div className="break-words">
            {guardError ? 'unknown — guard read failed' : disabled.size ? [...disabled].join(', ') : 'none'}
          </div>
        </div>
      </div>
      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Skills">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter…" aria-label="Filter tools" className="w-44 rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400" />
        </TopBar>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex flex-wrap gap-2.5 p-3.5 pb-0">
            <Stat k="PACKS" v={String(PACKS.length)} s="capability bundles" />
            {guardError ? (
              <>
                <Stat k="TOOLS LIVE" v="?" s="guard state unknown" tone="bad" />
                <Stat k="WITHHELD" v="?" s="guard read failed" tone="bad" />
              </>
            ) : loadError ? (
              // A failed catalogue read is not "zero tools": the counts
              // below were computed from an empty list, so the page used
              // to assert TOOLS LIVE 0 / WITHHELD 0 beside a red error
              // panel. Say what is actually true.
              <>
                <Stat k="TOOLS LIVE" v="?" s="catalogue unavailable" tone="bad" />
                <Stat k="WITHHELD" v="?" s="catalogue unavailable" tone="bad" />
              </>
            ) : (
              <>
                <Stat k="TOOLS LIVE" v={String(nLive)} s="offered to the planner" tone={nLive ? 'ok' : 'bad'} />
                <Stat k="WITHHELD" v={String(disabled.size)} s="hidden from the planner" tone={disabled.size ? 'bad' : undefined} />
              </>
            )}
          </div>
          {toggleError && (
            <p role="alert" className="mx-3.5 mb-3 mt-3 rounded-[10px] border border-amber-400/40 bg-amber-400/5 px-3.5 py-2.5 text-[12.5px] text-amber-200">
              toggle did not apply: {toggleError.slice(0, 200)}
            </p>
          )}
          {guardError && (
            <p role="alert" className="mx-3.5 mb-3 mt-3 rounded-[10px] border border-amber-400/40 bg-amber-400/5 px-3.5 py-2.5 text-[12.5px] text-amber-200">
              Could not read which tools are withheld ({guardError.slice(0, 160)}).
              The counts are unknown, not zero — check the agent before assuming
              every tool is live.
            </p>
          )}
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">CAPABILITY PACKS</div>
          {!loaded && <div className="px-3.5"><Skel n={2} /></div>}
          {loaded && loadError && !catalog.length && (
            <div className="mx-3.5 mb-3 rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-center text-xs text-red-200">
              couldn't load tool catalog ({loadError}). <button onClick={load} className="underline">retry</button>
            </div>
          )}
          <div className="grid grid-cols-[repeat(auto-fill,minmax(250px,1fr))] gap-3 px-3.5">
            {PACKS.filter((p) => !ql || p.name.toLowerCase().includes(ql) || p.tools.some((t) => t.includes(ql))).map((p) => {
              const live = p.tools.filter((t) => known.has(t) && !disabled.has(t)).length;
              const off = p.tools.length - live;
              return (
                <div key={p.id} className={`rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5 ${off === p.tools.length ? 'opacity-60' : ''}`}>
                  <div className="mb-2 flex items-center gap-2.5">
                    <span className="flex h-[34px] w-[34px] flex-none items-center justify-center rounded-lg border border-violet-400/30 bg-violet-400/10 text-[17px]">{p.ico}</span>
                    <span className="text-[13.5px] font-bold">{p.name}</span>
                  </div>
                  <div className="mb-2.5 min-h-[38px] text-xs leading-relaxed text-zinc-400">{p.desc}</div>
                  <div className="mb-3 flex flex-wrap gap-1.5">
                    {p.tools.map((t) => (
                      <span key={t} className={`rounded border border-white/10 bg-white/5 px-1.5 py-0.5 font-mono text-[10.5px] text-zinc-400 ${disabled.has(t) ? 'line-through opacity-50' : ''}`}>{t}</span>
                    ))}
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-zinc-muted">{live}/{p.tools.length} live</span>
                    <button
                      onClick={() => flipPack(p.id)}
                      disabled={packBusy === p.id}
                      aria-label={`${off ? 'Enable' : 'Disable'} pack ${p.name || p.id}`}
                      className="rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs hover:border-violet-400 disabled:opacity-50"
                    >
                      {packBusy === p.id ? 'Saving…' : off ? 'Enable pack' : 'Disable pack'}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">ALL TOOLS</div>
          {/* `overflow-hidden` clipped the table instead of scrolling it. At
              390px the table is 549px in a 362px section, so the State pill
              AND the Withhold/Enable button (right edge 554px) sat off-screen
              and could not be operated on at all — the permission control was
              unreachable on a phone. Note `documentElement.scrollWidth ==
              clientWidth` throughout, so a page-level overflow check reports
              "fine" while the control is unusable. */}
          <section className="mx-3.5 mb-3.5 overflow-x-auto rounded-[10px] border border-white/10 bg-[#0e0e12]">
            <table className="w-full border-collapse text-[12.5px]">
              <thead>
                <tr className="border-b border-white/10 text-left text-[10px] tracking-[0.1em] text-zinc-muted">
                  <th className="px-2.5 py-2 font-semibold">Tool</th>
                  <th className="px-2.5 py-2 font-semibold">Pack</th>
                  <th className="px-2.5 py-2 font-semibold">Description</th>
                  <th className="px-2.5 py-2 font-semibold">State</th>
                  <th className="px-2.5 py-2" />
                </tr>
              </thead>
              <tbody>
                {rows.map((t) => {
                  const off = disabled.has(t.name);
                  // Guard unreadable: "live" is not knowable, so the row
                  // must not claim it. The button still works — the write
                  // is authoritative — but the state cell says unknown.
                  const state = guardError ? 'unknown' : off ? 'withheld' : 'live';
                  return (
                    <tr key={t.name} className="border-b border-white/5 hover:bg-white/[0.02]">
                      <td className="px-2.5 py-2 font-mono text-[11.5px]">{t.name}</td>
                      <td className="px-2.5 py-2 text-zinc-400">{packOf(t.name)}</td>
                      <td className="px-2.5 py-2 text-zinc-400">{(t.description || '').slice(0, 110)}</td>
                      <td className="px-2.5 py-2">
                        <Pill tone={guardError ? 'err' : off ? 'muted' : 'ok'}>{state}</Pill>
                      </td>
                      <td className="px-2.5 py-2 text-right">
                        <button
                          onClick={() => flip(t.name, off)}
                          disabled={toolBusy.has(t.name)}
                          aria-label={`${off ? 'Enable' : 'Withhold'} tool ${t.name}`}
                          className="rounded-md border border-white/15 px-2.5 py-1 text-xs hover:border-violet-400 disabled:opacity-50"
                        >
                          {toolBusy.has(t.name) ? 'Saving…' : off ? 'Enable' : 'Withhold'}
                        </button>
                      </td>
                    </tr>
                  );
                })}
                {!rows.length && loaded && <tr><td colSpan={5}><Empty icon={<Boxes size={30} />} title="No tools match" /></td></tr>}
              </tbody>
            </table>
          </section>
        </div>
      </main>
    </div>
  );
}
