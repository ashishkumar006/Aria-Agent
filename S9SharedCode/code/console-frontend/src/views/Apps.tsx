import { useCallback, useEffect, useRef, useState } from 'react';
import { LayoutGrid, Flag, Plus, ArrowLeft, RefreshCw, Trash2 } from 'lucide-react';
import { Rail, TopBar, Empty, Stat, Pill } from '../components/ui';
import { api, CONF, type FeedEvent, type FlagRow, type TrackerApp, type TrackerAppDetail, type FeedbackRollup, type McpStats } from '../api';

const fmtTs = (ts: number | null | undefined) =>
  !ts ? '—' : new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

type ItemView = 'table' | 'cards';

/** Per-app mini-UI config from the spec (with sane fallbacks for specs
 *  created before the ui block existed). */
function uiConf(detail: TrackerAppDetail | null) {
  const spec = (detail?.spec || {}) as Record<string, unknown>;
  const ui = (spec.ui || {}) as { view?: string; columns?: { field: string; label: string }[]; sort?: string; highlight_new?: boolean };
  const idField = (spec.id_field as string) || 'id';
  const firstKeys = Object.keys(detail?.items?.[0] || {});
  const columns = ui.columns?.length
    ? ui.columns
    : (firstKeys.length ? firstKeys : [idField]).map((f) => ({ field: f, label: f }));
  return {
    defaultView: (ui.view === 'cards' ? 'cards' : 'table') as ItemView,
    columns,
    sort: ui.sort || idField,
    highlight: ui.highlight_new !== false,
    idField,
  };
}

export default function Apps() {
  const [tiles, setTiles] = useState({ gw: false, sess: 0, jobs: 0, tools: '—', spend: '—' });
  const [spend, setSpend] = useState<{ skill: string; dollars: number }[]>([]);
  const [feed, setFeed] = useState<FeedEvent[]>([]);
  const [flags, setFlags] = useState<FlagRow[]>([]);
  const [prefab, setPrefab] = useState({ configured: false, live: false });
  const [trackers, setTrackers] = useState<TrackerApp[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TrackerAppDetail | null>(null);
  const [detailErr, setDetailErr] = useState('');
  const [itemFilter, setItemFilter] = useState('');
  const [viewSel, setViewSel] = useState<'auto' | ItemView>('auto');
  const [showPrefab, setShowPrefab] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [createErr, setCreateErr] = useState('');
  const [form, setForm] = useState({ name: '', kind: 'openrouter_free', schedule: 'daily@09:00', url: '', items_path: 'data', id_field: 'id', match_field: '', match_equals: '', fields: 'id,name' });
  const [busy, setBusy] = useState(false);
  // Monotonic guard for the detail panel; flag names currently mid-write.
  const detailReq = useRef(0);
  const flagBusy = useRef(new Set<string>());
  // Distinguishes "the API is down" from "there is nothing to show". Nine
  // parallel reads resolve independently, so track the board-level ones.
  const [boardError, setBoardError] = useState('');
  // A toggle that reverted because the write failed must say so; silently
  // snapping back reads as the user's own mis-click.
  const [toggleError, setToggleError] = useState('');
  /* The two signals that were being collected and thrown away: thumbs
     up/down per skill, and the fetch-cache hit rate behind research
     latency. Both are here because they change what an operator does next. */
  const [feedback, setFeedback] = useState<FeedbackRollup | null>(null);
  const [mcp, setMcp] = useState<McpStats | null>(null);

  const load = useCallback(async () => {
    const [h, sess, sch, tools, guard, sp, ev, fl, tr, fb, mst] = await Promise.all([
      api.safe(api.health()),
      api.safe(api.sessions(200)),
      api.safe(api.scheduleList()),
      api.safe(api.tools()),
      api.safe(api.toolsGuard()),
      api.safe(api.bySkill()),
      api.safe(api.events(12)),
      api.safe(api.flags()),
      api.safe(api.trackerApps()),
      api.safe(api.feedbackRollup(30, 3)),
      api.safe(api.mcpStats()),
    ]);
    // If the three primary reads all failed, the board is down — not empty.
    // (Individual tiles tolerate partial failure; the banner does not.)
    if (!h && !sess && !tr) setBoardError('could not reach the agent (is it running on :8500?)');
    else setBoardError('');
    const gwUp = !!h && h.gateway_up !== false;
    const nSess = sess ? (sess.sessions || []).length : 0;
    const jl = sch?.schedules;
    const jobs = jl ? (Array.isArray(jl) ? jl : Object.values(jl)) : [];
    const nJobs = jobs.filter((j) => j.enabled).length;
    const cat = tools ? (tools.tools || []).length : 0;
    const nOff = guard ? (guard.disabled || []).length : 0;
    const rows = sp ? (sp.rows || []) : [];
    const dollars = rows.reduce((a, r) => a + Number(r.dollars || 0), 0);
    setTiles({
      gw: gwUp,
      sess: nSess,
      jobs: nJobs,
      tools: `${cat - nOff}/${cat}`,
      spend: `$${dollars.toFixed(4)}`,
    });
    setSpend(rows.slice(0, 8).map((r) => ({ skill: r.skill, dollars: Number(r.dollars || 0) })));
    setFeed((ev && ev.events) || []);
    if (fl) {
      /* Never clobber a flag the user is mid-toggle on: a poll that started
         before the write must not resolve over it. */
      if (!flagBusy.current.size) {
        setFlags(fl.flags || []);
        setPrefab(fl.prefab || { configured: false, live: false });
      }
    }
    if (tr) setTrackers(tr.apps || []);
    setFeedback(fb);
    setMcp(mst);
  }, []);

  const openApp = useCallback(async (id: string) => {
    /* Sequence guard. With two or more boards, a slow source (a cold JSON
       feed) meant board A's response could land after board B's and render
       A's items under B's breadcrumb — after which "Delete app" and
       "Refresh now" acted on the wrong id. */
    const req = ++detailReq.current;
    setOpenId(id);
    setDetail(null);
    setDetailErr('');
    setItemFilter('');
    setViewSel('auto');
    setShowPrefab(false);
    try {
      const d = await api.trackerApp(id);
      if (detailReq.current !== req) return;
      setDetail(d);
    } catch (e) {
      if (detailReq.current !== req) return;
      setDetailErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const refreshDetail = useCallback(async () => {
    if (!openId) return;
    const req = ++detailReq.current;
    const id = openId;
    setBusy(true);
    try {
      await api.trackerRefresh(id);
      const d = await api.trackerApp(id);
      if (detailReq.current !== req) return;
      setDetail(d);
      const t = await api.safe(api.trackerApps());
      if (t) setTrackers(t.apps || []);
    } catch (e) {
      if (detailReq.current !== req) return;
      setDetailErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [openId]);

  const deleteApp = useCallback(async () => {
    if (!openId || busy) return;
    const name = detail?.name || openId;
    /* Unguarded, a double-click on the TopBar delete fired two DELETEs and
       destroyed the spec and every snapshot with no way back. */
    if (!window.confirm(`Delete the board "${name}" and all its snapshots? This cannot be undone.`)) return;
    setBusy(true);
    try {
      const r = await api.safe(api.trackerDelete(openId));
      if (r) setTrackers(r.apps || []);
      setOpenId(null);
      setDetail(null);
    } finally {
      setBusy(false);
    }
  }, [openId, detail, busy]);

  const createApp = useCallback(async () => {
    setCreateErr('');
    const spec: Record<string, unknown> = {
      name: form.name.trim(),
      kind: form.kind,
      schedule: form.schedule.trim() || 'manual',
    };
    if (form.kind === 'json_feed') {
      spec.url = form.url.trim();
      spec.items_path = form.items_path.trim() || 'data';
      spec.id_field = form.id_field.trim() || 'id';
      if (form.match_field.trim()) {
        let eq: unknown = form.match_equals;
        try { eq = JSON.parse(form.match_equals); } catch { /* keep string */ }
        spec.match = { field: form.match_field.trim(), equals: eq };
      }
      spec.fields = form.fields.split(',').map((s) => s.trim()).filter(Boolean);
    }
    setBusy(true);
    try {
      const r = await api.trackerCreate(spec);
      if (r.error) { setCreateErr(r.error); return; }
      if (r.apps) setTrackers(r.apps);
      const nid = (r.spec as { id?: string } | undefined)?.id;
      setShowCreate(false);
      setForm({ name: '', kind: 'openrouter_free', schedule: 'daily@09:00', url: '', items_path: 'data', id_field: 'id', match_field: '', match_equals: '', fields: 'id,name' });
      if (nid) await openApp(nid);
    } catch (e) {
      setCreateErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [form, openApp]);

  /* Optimistic with rollback. Previously the toggle only adopted the POST
     response, so a 15s poll that started before the write resolved
     afterwards and snapped the switch back — the user then clicked again
     and reversed their own change. The server was right; only the UI lied. */
  const flip = async (f: FlagRow) => {
    if (f.type !== 'bool' || flagBusy.current.has(f.name)) return;
    const next = !(f.value === true);
    const before = flags;
    flagBusy.current.add(f.name);
    setFlags((p) => p.map((x) => (x.name === f.name ? { ...x, value: next } : x)));
    try {
      const r = await api.setFlag(f.name, next);
      setFlags(r.flags || before);
      setToggleError('');
    } catch (e) {
      setFlags(before); // roll back to exactly what was on screen
      // The switch snapping back with no message is indistinguishable from
      // the user mis-clicking. Say what happened.
      setToggleError(`${f.name}: ${(e as Error)?.message || e}`);
      await load();
    } finally {
      flagBusy.current.delete(f.name);
    }
  };

  const revert = async (f: FlagRow) => {
    if (flagBusy.current.has(f.name)) return;
    const before = flags;
    flagBusy.current.add(f.name);
    try {
      const r = await api.setFlag(f.name, null);
      if (r.flags) setFlags(r.flags);
    } catch {
      setFlags(before);
    } finally {
      flagBusy.current.delete(f.name);
    }
  };

  useEffect(() => {
    load();
    const t = setInterval(load, CONF.pollSlowMs);
    return () => clearInterval(t);
  }, [load]);

  const max = Math.max(1e-9, ...spend.map((r) => r.dollars));

  // Mini-UI: per-app view config + Prefab-gated view availability.
  const tableOn = (flags.find((f) => f.name === 'apps.views.table')?.value ?? true) !== false;
  const cardsOn = (flags.find((f) => f.name === 'apps.views.cards')?.value ?? true) !== false;
  const prefabOn = (flags.find((f) => f.name === 'apps.views.prefab')?.value ?? true) !== false;
  const ui = uiConf(detail);
  const wantView: ItemView = viewSel === 'auto' ? ui.defaultView : viewSel;
  const effView: ItemView = (wantView === 'cards' ? cardsOn : tableOn)
    ? wantView
    : (tableOn || !cardsOn ? 'table' : 'cards');
  const visibleItems = (detail?.items || [])
    .filter((it) => !itemFilter || JSON.stringify(it).toLowerCase().includes(itemFilter.toLowerCase()))
    .sort((a, b) => String(a[ui.sort] ?? '').localeCompare(String(b[ui.sort] ?? '')))
    .slice(0, 200);
  const isNewId = (id: string) =>
    ui.highlight && (detail?.added_items || []).some(
      (a) => String(a[ui.idField] ?? a.id) === id,
    );
  const idOf = (it: Record<string, unknown>, i: number) =>
    String(it[ui.idField] ?? it.id ?? `#${i}`);

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <Rail />
      <div className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5 text-xs font-bold tracking-wide">
          Boards
          <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[10.5px] font-bold text-zinc-400">{1 + trackers.length}</span>
        </div>
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-2.5 pb-3">
          <button
            onClick={() => { setOpenId(null); setDetail(null); }}
            className={`block w-full rounded-[10px] border p-2.5 text-left ${openId === null ? 'border-violet-400/60 bg-violet-400/5' : 'border-white/10 bg-[#0e0e12] hover:border-white/25'}`}
          >
            <div className="flex items-center gap-2">
              <span className="flex-1 text-[12.5px] font-semibold">System overview</span>
              {/* "live" is info-blue everywhere else (Research, Runs); the
                  green here was the same word in a different color. */}
              <Pill tone="info">live</Pill>
            </div>
            <div className="mt-1 text-[11px] text-zinc-muted">agent + gateway</div>
          </button>
          {trackers.map((t) => (
            <button
              key={t.id}
              onClick={() => openApp(t.id)}
              className={`block w-full rounded-[10px] border p-2.5 text-left ${openId === t.id ? 'border-violet-400/60 bg-violet-400/5' : 'border-white/10 bg-[#0e0e12] hover:border-white/25'}`}
            >
              <div className="flex items-center gap-2">
                <span className="flex-1 truncate text-[12.5px] font-semibold">{t.name}</span>
                {t.error
                  ? <Pill tone="err">error</Pill>
                  : (t.added > 0 || t.removed > 0)
                    ? <Pill tone="warn">changed</Pill>
                    : <Pill tone="ok">{t.count}</Pill>}
              </div>
              <div className="mt-1 text-[11px] text-zinc-muted">{t.count} items · {t.schedule}</div>
            </button>
          ))}
          <button
            onClick={() => setShowCreate((v) => !v)}
            className="flex w-full items-center justify-center gap-1.5 rounded-[10px] border border-dashed border-white/15 p-2.5 text-[12px] text-zinc-400 hover:border-violet-400/60 hover:text-zinc-200"
          >
            <Plus size={13} /> New app
          </button>
        </div>
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <TopBar crumb={openId && detail ? `Apps / ${detail.name}` : 'Apps'}>
          {openId ? (
            <>
              <button onClick={() => { setOpenId(null); setDetail(null); }} className="flex items-center gap-1 rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs hover:border-violet-400">
                <ArrowLeft size={13} /> Boards
              </button>
              <button onClick={refreshDetail} disabled={busy} aria-label="Refresh app now" className="flex items-center gap-1.5 rounded-md border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs hover:border-violet-400 disabled:opacity-45">
                <RefreshCw size={13} className={busy ? 'animate-spin' : ''} /> Refresh now
              </button>
              <button onClick={deleteApp} disabled={busy} aria-label="Delete app" className="flex items-center gap-1.5 rounded-md border border-red-400/40 bg-red-400/5 px-3 py-1.5 text-xs text-red-200 hover:bg-red-400/15 disabled:opacity-45">
                <Trash2 size={13} />
              </button>
            </>
          ) : (
            <Pill tone={tiles.gw ? 'ok' : 'warn'}>{tiles.gw ? '● all systems live' : '● agent only'}</Pill>
          )}
        </TopBar>
        {boardError && (
          <div className="mx-3.5 mt-3 rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-center text-xs text-red-200">
            {boardError} <button onClick={load} className="underline">retry</button>
          </div>
        )}
        {toggleError && (
          <p role="alert" className="mx-3.5 mt-3 rounded-lg border border-amber-400/40 bg-amber-400/5 p-3 text-center text-xs text-amber-200">
            toggle reverted: {toggleError}
          </p>
        )}
        {showCreate && !openId && (
          <div className="mx-3.5 mt-3 rounded-[10px] border border-violet-400/40 bg-[#0e0e12] p-3.5">
            <div className="mb-2.5 text-xs font-bold tracking-wide">NEW TRACKER APP</div>
            <div className="grid grid-cols-2 gap-2.5">
              <label className="col-span-2 block text-[11px] text-zinc-400">Name
                <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="e.g. Free OpenRouter models" className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs text-zinc-100 outline-none focus:border-violet-400" />
              </label>
              <label className="block text-[11px] text-zinc-400">Kind
                <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })} className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs text-zinc-100 outline-none focus:border-violet-400">
                  <option value="openrouter_free">OpenRouter free models (preset)</option>
                  <option value="json_feed">Generic JSON feed</option>
                </select>
              </label>
              <label className="block text-[11px] text-zinc-400">Schedule
                <input value={form.schedule} onChange={(e) => setForm({ ...form, schedule: e.target.value })} placeholder="daily@09:00 · every 12h · manual" title="Also accepts: every 45s, in 30m, tomorrow 9am, ISO datetime, epoch" className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 font-mono text-xs text-zinc-100 outline-none focus:border-violet-400" />
              </label>
              {form.kind === 'json_feed' && (
                <>
                  <label className="col-span-2 block text-[11px] text-zinc-400">JSON URL
                    <input value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} placeholder="https://…" className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 font-mono text-xs text-zinc-100 outline-none focus:border-violet-400" />
                  </label>
                  <label className="block text-[11px] text-zinc-400">Items path (dot)
                    <input value={form.items_path} onChange={(e) => setForm({ ...form, items_path: e.target.value })} placeholder="data" className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 font-mono text-xs text-zinc-100 outline-none focus:border-violet-400" />
                  </label>
                  <label className="block text-[11px] text-zinc-400">ID field
                    <input value={form.id_field} onChange={(e) => setForm({ ...form, id_field: e.target.value })} placeholder="id" className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 font-mono text-xs text-zinc-100 outline-none focus:border-violet-400" />
                  </label>
                  <label className="block text-[11px] text-zinc-400">Match field (optional)
                    <input value={form.match_field} onChange={(e) => setForm({ ...form, match_field: e.target.value })} placeholder="pricing.prompt" className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 font-mono text-xs text-zinc-100 outline-none focus:border-violet-400" />
                  </label>
                  <label className="block text-[11px] text-zinc-400">Match equals (JSON)
                    <input value={form.match_equals} onChange={(e) => setForm({ ...form, match_equals: e.target.value })} placeholder='"0"' className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 font-mono text-xs text-zinc-100 outline-none focus:border-violet-400" />
                  </label>
                  <label className="col-span-2 block text-[11px] text-zinc-400">Fields (comma-separated)
                    <input value={form.fields} onChange={(e) => setForm({ ...form, fields: e.target.value })} placeholder="id,name" className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 font-mono text-xs text-zinc-100 outline-none focus:border-violet-400" />
                  </label>
                </>
              )}
            </div>
                  {createErr && <div role="status" className="mt-2 text-xs text-red-300">{createErr}</div>}
            <div className="mt-2.5 flex gap-2">
              <button onClick={createApp} disabled={busy || !form.name.trim()} className="rounded-md bg-violet-400 px-4 py-2 text-xs font-bold text-[#0b0b0e] hover:brightness-110 disabled:opacity-50">Create app</button>
              <button onClick={() => setShowCreate(false)} className="rounded-md border border-white/15 bg-white/5 px-4 py-2 text-xs hover:border-violet-400">Cancel</button>
            </div>
          </div>
        )}
        {openId ? (
          <div className="min-h-0 flex-1 overflow-y-auto p-3.5">
                {detailErr && <div role="status" className="rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-xs text-red-200">{detailErr}</div>}
            {!detail && !detailErr && <div className="py-10 text-center text-sm text-zinc-muted">loading…</div>}
            {detail && (
              <>
                <div className="flex flex-wrap gap-2.5">
                  <Stat k="ITEMS" v={String(detail.count)} s={detail.kind} />
                  <Stat k="ADDED" v={`+${detail.added}`} s="since last refresh" tone={detail.added ? 'warn' : 'ok'} />
                  <Stat k="REMOVED" v={`−${detail.removed}`} s="since last refresh" tone={detail.removed ? 'bad' : 'ok'} />
                  <Stat k="REFRESHED" v={fmtTs(detail.refreshed_at)} s={`next: ${fmtTs(detail.next_refresh)}`} />
                </div>
                {detail.error && <div className="mt-2.5 rounded-lg border border-red-400/30 bg-red-400/5 p-2.5 text-xs text-red-200">last refresh failed: {detail.error}</div>}
                <div className="flex items-center justify-between px-0.5 pb-1 pt-4">
                  <div className="text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">ITEMS</div>
                  <div className="flex items-center gap-2">
                    {/* The item list was filterable in code (`itemFilter`)
                        but no input ever wrote to it, so a 200-row board
                        could not be narrowed. */}
                    <input
                      value={itemFilter}
                      onChange={(e) => setItemFilter(e.target.value)}
                      placeholder="Filter items…"
                      aria-label="Filter items"
                      className="w-40 rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400"
                    />
                    {prefabOn && detail && (
                      <button
                        onClick={() => setShowPrefab((v) => !v)}
                        aria-pressed={showPrefab}
                        title="PrefectHQ/prefab bundled render (spike)"
                        className={`rounded-lg border px-3 py-1 text-[11px] ${showPrefab ? 'border-violet-400/60 bg-violet-400/20 font-bold text-zinc-100' : 'border-white/10 bg-white/5 text-zinc-400 hover:text-zinc-200'}`}
                      >
                        Prefab
                      </button>
                    )}
                    {tableOn && cardsOn && !showPrefab && (
                      <div className="flex gap-1 rounded-lg border border-white/10 bg-white/5 p-[3px]" role="group" aria-label="Item view">
                      {(['table', 'cards'] as const).map((v) => (
                        <button
                          key={v}
                          onClick={() => setViewSel(v)}
                          aria-pressed={effView === v}
                          className={`rounded px-3 py-1 text-[11px] capitalize ${effView === v ? 'bg-violet-400/20 font-bold text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'}`}
                        >
                          {v}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                </div>
                {showPrefab && detail ? (
                  <PrefabFrame id={detail.id} name={detail.name} />
                ) : (
                <section className="rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5">
                  {/* A filter with no matches used to render a bare empty box
                      with no message, while the stat card still read
                      "ITEMS 20" -- so "no matches" and "data lost" were
                      indistinguishable. */}
                  {!!detail.items.length && !visibleItems.length && (
                    <div className="py-6 text-center text-xs text-amber-200">
                      No items match {itemFilter ? <>&ldquo;{itemFilter}&rdquo;</> : 'this view'}
                      {' '}({detail.items.length} item{detail.items.length === 1 ? '' : 's'} loaded).{' '}
                      <button onClick={() => setItemFilter('')} className="underline">clear filter</button>
                    </div>
                  )}
                  {!detail.items.length && <Empty icon={<LayoutGrid size={30} />} title="No items yet — hit Refresh now" />}
                  {effView === 'cards' ? (
                    <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
                      {visibleItems.map((it, i) => {
                        const id = idOf(it, i);
                        return (
                          <div key={`${id}-${i}`} className={`rounded-lg border p-3 ${isNewId(id) ? 'border-amber-300/40 bg-amber-300/[0.04]' : 'border-white/10 bg-black/30'}`}>
                            <div className="flex items-center gap-2">
                              <span className="min-w-0 flex-1 truncate font-mono text-[12.5px] font-semibold text-zinc-100">{id}</span>
                              {isNewId(id) && <Pill tone="warn">new</Pill>}
                            </div>
                            <div className="mt-1.5 space-y-1">
                              {ui.columns.filter((c) => c.field !== ui.idField && c.field !== 'id').slice(0, 4).map((c) => (
                                <div key={c.field} className="flex justify-between gap-2 text-[11.5px]">
                                  <span className="flex-none text-zinc-muted">{c.label}</span>
                                  <span className="truncate text-right text-zinc-200">{String(it[c.field] ?? '—').slice(0, 60)}</span>
                                </div>
                              ))}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                  visibleItems.map((it, i) => {
                    const id = idOf(it, i);
                    return (
                      <div key={`${id}-${i}`} className="flex items-center gap-2.5 border-b border-white/5 py-2 text-xs last:border-0">
                        <span className="min-w-0 flex-1 truncate font-mono text-zinc-100">{id}</span>
                        {ui.columns.filter((c) => c.field !== ui.idField && c.field !== 'id').slice(0, 3).map((c) => (
                          <span key={c.field} className="hidden flex-none text-zinc-muted md:inline">{c.label}: <span className="text-zinc-300">{String(it[c.field] ?? '—').slice(0, 40)}</span></span>
                        ))}
                        {isNewId(id) && <Pill tone="warn">new</Pill>}
                      </div>
                    );
                  }))}
                </section>
                )}
                {!!detail.history.length && (
                  <>
                    <div className="px-0.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">HISTORY</div>
                    <section className="mb-3.5 rounded-[10px] border border-white/10 bg-[#0e0e12] py-2 font-mono text-xs leading-relaxed">
                      {detail.history.slice().reverse().slice(0, 10).map((h, i) => (
                        <div key={`${h.ts}-${i}`} className="flex gap-2.5 px-3 py-[3px]">
                          <span className="flex-none tabular-nums text-zinc-muted">{fmtTs(h.ts)}</span>
                          <span className="text-zinc-400">{h.count} items{(h.added?.length || h.removed?.length) ? ` · +${(h.added || []).length} −${(h.removed || []).length}` : ''}</span>
                        </div>
                      ))}
                    </section>
                  </>
                )}
              </>
            )}
          </div>
        ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex flex-wrap gap-2.5 p-3.5 pb-0">
            <Stat k="AGENT" v="up" s={`:${CONF.agentPort}`} tone="ok" />
            <Stat k="GATEWAY" v={tiles.gw ? 'up' : 'down'} s={`:${CONF.gatewayPort}`} tone={tiles.gw ? 'ok' : 'bad'} />
            <Stat k="SESSIONS" v={String(tiles.sess)} s="first 200 listed" />
            <Stat k="JOBS ARMED" v={String(tiles.jobs)} s="scheduler" />
            <Stat k="TOOLS LIVE" v={tiles.tools} s="of catalog" />
            <Stat k="SPEND" v={tiles.spend} s="lifetime USD" />
          </div>
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">
            FLAGS & CONFIG
            <span className="ml-2 font-mono normal-case tracking-normal text-zinc-muted">
              {prefab.live ? 'prefab cloud live' : prefab.configured ? 'prefab cloud configured (offline)' : 'local only'}
            </span>
          </div>
          <section className="mx-3.5 rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5">
            {!flags.length && <Empty icon={<Flag size={30} />} title="No flags defined" />}
            {flags.map((f) => (
              <div key={f.name} className="flex items-center gap-3 border-b border-white/5 py-2.5 text-xs last:border-0">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate font-mono text-zinc-100">{f.name}</span>
                    <Pill tone={f.source === 'prefab' ? 'info' : f.source === 'override' ? 'warn' : 'muted'}>
                      {f.source}
                    </Pill>
                  </div>
                  {!!f.description && <div className="mt-0.5 break-words text-[11px] leading-relaxed text-zinc-muted">{f.description}</div>}
                </div>
                {f.type === 'bool' ? (
                  <button
                    onClick={() => flip(f)}
                    role="switch"
                    aria-checked={f.value === true}
                    aria-label={`Toggle ${f.name}`}
                    /* Writes are tracked in flagBusy; a clickable-looking
                       switch during the write invited the double-toggle
                       that reversed the user's own change. */
                    aria-disabled={flagBusy.current.has(f.name)}
                    className={`relative h-6 w-12 flex-none rounded-full transition-colors lg:h-5.5 lg:w-10 ${f.value === true ? 'bg-violet-400' : 'bg-white/15'} ${flagBusy.current.has(f.name) ? 'opacity-50' : ''}`}
                  >
                    <span className={`absolute top-0.5 h-4.5 w-4.5 rounded-full bg-white transition-all ${f.value === true ? 'left-[18px]' : 'left-0.5'}`} />
                  </button>
                ) : (
                  <span className="flex-none font-mono text-zinc-300">{String(f.value)}</span>
                )}
                {f.source === 'override' && (
                  <button onClick={() => revert(f)} className="flex-none text-[11px] text-zinc-muted underline hover:text-zinc-300">
                    revert
                  </button>
                )}
              </div>
            ))}
          </section>
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">SPEND BY SKILL</div>
          <section className="mx-3.5 rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5">
            {!spend.length && <Empty icon={<LayoutGrid size={30} />} title="No spend yet" />}
            {spend.map((r, i) => (
              <div key={`${r.skill || 'unknown'}-${i}`} className="my-1.5 flex items-center gap-2.5 text-xs">
                <span className="w-[110px] flex-none truncate sm:w-[150px]">{r.skill}</span>
                <span className="h-2 flex-1 overflow-hidden rounded bg-white/10">
                  {/* `0/0` is NaN, and an invalid `width: NaN%` makes the
                      browser fall back to width:auto — a block-level span
                      then fills the track, so every $0.0000 row drew a
                      *full* bar. Floor the denominator instead. */}
                  <span
                    className={`block h-full rounded ${r.dollars > 0 ? 'bg-violet-400' : 'bg-transparent'}`}
                    style={{ width: max > 0 ? `${Math.min(100, (r.dollars / max) * 100)}%` : '0%' }}
                  />
                </span>
                <span className="w-[74px] flex-none text-right tabular-nums sm:w-[90px]">${r.dollars.toFixed(4)}</span>
              </div>
            ))}
          </section>
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">OUTPUT RATINGS · last 30 days</div>
          <section className="mx-3.5 rounded-[10px] border border-white/10 bg-[#0e0e12] p-3.5">
            {!feedback || !feedback.totals.total ? (
              <div className="text-xs text-zinc-muted">
                No ratings yet. Use the thumbs on a node in Runs — they are the
                only signal that says whether a prompt edit helped.
              </div>
            ) : (
              <>
                <div className="mb-2 text-xs text-zinc-400">
                  <span className="font-bold text-ok">{feedback.totals.up} good</span>
                  {' · '}
                  <span className="font-bold text-danger">{feedback.totals.down} bad</span>
                  {feedback.unlabelled ? (
                    <span className="ml-1.5 text-zinc-muted">({feedback.unlabelled} from an older console)</span>
                  ) : null}
                </div>
                {feedback.by_skill.filter((r) => r.total > 0).map((r) => {
                  // Worst first: the thing to fix is at the top. The bar is
                  // the share of DOWN votes — a bar that is full should mean
                  // "bad", not the smoothed prior the first version drew.
                  const pct = r.total > 0 ? Math.round((r.down / r.total) * 100) : 0;
                  return (
                    <div key={r.name} className="my-1.5 flex items-center gap-2.5 text-xs">
                      <span className="w-[110px] flex-none truncate sm:w-[150px]" title={r.name}>
                        {r.name.startsWith('(unlabelled') ? 'older runs' : r.name}
                      </span>
                      <span className="h-2 flex-1 overflow-hidden rounded bg-white/10">
                        <span
                          className={`block h-full rounded ${pct >= 50 ? 'bg-red-400/70' : pct > 0 ? 'bg-amber-300/70' : 'bg-emerald-400/60'}`}
                          style={{ width: `${Math.max(pct, r.total > 0 ? 4 : 0)}%` }}
                        />
                      </span>
                      <span className="w-[74px] flex-none text-right tabular-nums sm:w-[110px]">
                        {r.up}↑ {r.down}↓
                        {r.confidence === 'low' ? <span className="ml-1 text-zinc-muted" title="too few votes to read as a rate">*</span> : null}
                      </span>
                    </div>
                  );
                })}
                <div className="mt-1 text-[10.5px] text-zinc-muted">
                  bar = share of bad votes · * fewer than 3 votes — not a rate
                </div>
              </>
            )}
          </section>
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">PIPELINE HEALTH</div>
          <section className="mx-3.5 mb-3.5 grid grid-cols-2 gap-2.5 sm:grid-cols-3">
            <Stat
              k="FETCH CACHE"
              v={mcp ? `${Math.round((mcp.cache?.hit_rate ?? 0) * 100)}%` : '—'}
              s={mcp ? `${mcp.cache?.hits ?? 0} hits / ${mcp.cache?.entries ?? 0} cached` : 'stats unavailable'}
              tone={(mcp?.cache?.hit_rate ?? 0) >= 0.3 ? 'ok' : 'warn'}
            />
            <Stat
              k="OUTCOMES LOGGED"
              v={mcp?.tool_outcomes ? String(mcp.tool_outcomes.written) : '—'}
              s={mcp?.tool_outcomes ? `${mcp.tool_outcomes.queued} queued · ${mcp.tool_outcomes.errors} failed` : 'tool outcomes'}
            />
            <Stat
              k="MCP BREAKER"
              v={!mcp?.mcp_breaker ? '—' : mcp.mcp_breaker.open ? 'open' : 'closed'}
              s={mcp?.mcp_breaker ? `${mcp.mcp_breaker.trips} trips` : 'tool subprocess'}
              tone={mcp?.mcp_breaker?.open ? 'bad' : 'ok'}
            />
          </section>
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">LATEST ACTIVITY · refreshes every 15s</div>
          <section className="mx-3.5 mb-3.5 rounded-[10px] border border-white/10 bg-[#0e0e12] py-2 font-mono text-xs leading-relaxed">
            {!feed.length && <div className="px-3 py-2 text-zinc-muted">no recent activity</div>}
            {feed.map((e, i) => (
              <div key={`${e.t}-${e.src}-${i}`} className="flex gap-2.5 px-3 py-[3px] hover:bg-white/[0.03]">
                <span className="flex-none tabular-nums text-zinc-muted">{e.iso}</span>
                <span className="min-w-[86px] flex-none text-violet-300">{e.src}</span>
                <span className="break-words text-zinc-400">{e.msg}</span>
              </div>
            ))}
          </section>
        </div>
        )}
      </div>
    </div>
  );
}

/** The Prefab view, isolated.
    The endpoint returns a server-rendered HTML document, so mounting it
    unsandboxed gave that document the console's own origin and full access
    to every `/api/*` route — while its content comes from an arbitrary
    user-supplied JSON feed URL. `sandbox="allow-scripts"` WITHOUT
    `allow-same-origin` puts it in an opaque origin, so even if the bundled
    renderer ever emitted a cell value unescaped, the blast radius is the
    frame. The bundled renderer is offline-capable, so it needs no
    same-origin access. Failures are also surfaced instead of appearing as a
    blank white box. */
function PrefabFrame({ id, name }: { id: string; name: string }) {
  const [err, setErr] = useState('');
  useEffect(() => {
    let live = true;
    setErr('');
    api.prefabCheck(id)
      .then((r) => {
        if (!live) return;
        if (!r.ok) setErr(`${r.status} ${r.text || 'render failed'}`);
      })
      .catch((e) => { if (live) setErr(String(e?.message || e)); });
    return () => { live = false; };
  }, [id]);

  if (err) {
    return (
      <div className="rounded-[10px] border border-red-400/30 bg-red-400/5 p-3.5 text-xs text-red-200">
        <div className="mb-1 font-bold">prefab render failed</div>
        <div className="break-words font-mono text-[11px] text-red-200/80">{err}</div>
      </div>
    );
  }
  return (
    <iframe
      title={`Prefab view of ${name}`}
      src={`/api/apps/${encodeURIComponent(id)}/prefab`}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      loading="lazy"
      className="h-[640px] w-full rounded-[10px] border border-white/10 bg-white"
    />
  );
}
