import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  LayoutGrid, Loader2, Plus, RefreshCw, Sparkles, Trash2, X, Pencil, Check,
} from 'lucide-react';
import { toast } from 'sonner';
import { Rail, TopBar, Empty, Pill, Skel, SkipLink } from '../components/ui';
import { A2ui } from '../components/A2ui';
import { useMainId, useActivePoll } from '../components/KeepAlive';
import {
  api, type A2uiSurface, type TrackerApp, type TrackerAppDetail,
} from '../api';
import { Pane, PaneStrip, usePaneMode } from '../lib/panes';

const fmtTs = (ts: number | null | undefined) =>
  !ts ? '—' : new Date(ts * 1000).toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });

function columnsOf(spec: Record<string, unknown>, items: Record<string, unknown>[]) {
  const ui = (spec.ui || {}) as { columns?: { field: string; label: string }[] };
  if (ui.columns?.length) return ui.columns;
  const first = Object.keys(items[0] || {});
  return (first.length ? first : ['id']).map((f) => ({ field: f, label: f }));
}

function cell(v: unknown): string {
  if (v == null) return '—';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

export default function Apps() {
  const mainId = useMainId();
  const railMode = usePaneMode('apps.rail');
  const [apps, setApps] = useState<TrackerApp[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TrackerAppDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busy, setBusy] = useState(false);
  const [itemFilter, setItemFilter] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState('');
  const [createDescription, setCreateDescription] = useState('');
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState('');
  /* A build runs on the server after the dialog closes. Keeping this outside
     the modal is what lets the user switch sections while it works; the
     keep-alive Apps view stays mounted and opens the app when it lands. */
  const [building, setBuilding] = useState<{ name: string } | null>(null);
  const [buildError, setBuildError] = useState('');
  const [editOpen, setEditOpen] = useState(false);
  const [editName, setEditName] = useState('');
  const [editSchedule, setEditSchedule] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [editBusy, setEditBusy] = useState(false);
  const [editError, setEditError] = useState('');
  const detailReq = useRef(0);
  const descriptionRef = useRef<HTMLTextAreaElement | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await api.trackerApps();
      setApps(d.apps || []);
      setLoadError('');
    } catch (e) {
      setLoadError(String((e as Error)?.message || e));
    } finally {
      setLoaded(true);
    }
  }, []);

  useActivePoll(load, 15000);

  const openApp = useCallback(async (id: string) => {
    const req = ++detailReq.current;
    setOpenId(id);
    setDetail(null);
    setDetailError('');
    setActionError('');
    setItemFilter('');
    setEditOpen(false);
    setEditError('');
    setDetailLoading(true);
    try {
      const d = await api.trackerApp(id);
      if (detailReq.current !== req) return;
      setDetail(d);
    } catch (e) {
      if (detailReq.current === req) {
        setDetailError(String((e as Error)?.message || e));
      }
    } finally {
      if (detailReq.current === req) setDetailLoading(false);
    }
  }, []);

  const selected = apps.find((a) => a.id === openId) || null;
  const isA2ui = (detail?.kind || selected?.kind) === 'a2ui';
  const surface = detail?.surface
    || (detail?.spec?.surface as A2uiSurface | undefined)
    || null;

  const refresh = useCallback(async () => {
    if (!openId || busy) return;
    setBusy(true);
    setActionError('');
    try {
      await api.trackerRefresh(openId);
      await load();
      await openApp(openId);
    } catch (e) {
      setActionError(String((e as Error)?.message || e));
    } finally { setBusy(false); }
  }, [busy, load, openApp, openId]);

  const remove = useCallback(async () => {
    if (!openId) return;
    const name = selected?.name || openId;
    if (!confirm(`Delete the app "${name}"? Its saved snapshots are removed too.`)) return;
    try {
      await api.trackerDelete(openId);
      setOpenId(null);
      setDetail(null);
      await load();
    } catch (e) {
      setActionError(String((e as Error)?.message || e));
    }
  }, [load, openId, selected?.name]);

  const runAction = useCallback(async (action: string, args: Record<string, unknown>) => {
    if (!openId || busy) return;
    setBusy(true);
    setActionError('');
    try {
      const raw = args.input;
      const input: Record<string, string> = {};
      if (raw && typeof raw === 'object') {
        for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
          input[k] = String(v ?? '');
        }
      }
      const res = await api.appAction(openId, action, input);
      if (res.error) throw new Error(res.error);
      if (res.app) setDetail(res.app);
      else await openApp(openId);
      await load();
    } catch (e) {
      setActionError(String((e as Error)?.message || e));
    } finally { setBusy(false); }
  }, [busy, load, openApp, openId]);

  const startEdit = useCallback(() => {
    if (!detail) return;
    setEditName(detail.name || '');
    setEditSchedule(detail.schedule || 'manual');
    setEditDescription(detail.description || '');
    setEditError('');
    setEditOpen(true);
  }, [detail]);

  const saveEdit = useCallback(async () => {
    if (!openId || editBusy) return;
    if (!editName.trim()) { setEditError('Name cannot be empty'); return; }
    setEditBusy(true);
    setEditError('');
    try {
      const res = await api.appPatch(openId, {
        name: editName.trim(),
        schedule: editSchedule.trim() || 'manual',
        description: editDescription,
      });
      if (res.error) throw new Error(res.error);
      if (res.app) setDetail(res.app);
      else await openApp(openId);
      await load();
      setEditOpen(false);
    } catch (e) {
      setEditError(String((e as Error)?.message || e));
    } finally { setEditBusy(false); }
  }, [openId, editBusy, editName, editSchedule, editDescription, load, openApp]);

  const closeCreate = useCallback(() => {
    setCreateOpen(false);
    setCreateError('');
  }, []);

  useEffect(() => {
    if (!createOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeCreate();
    };
    document.addEventListener('keydown', onKey);
    const t = window.setTimeout(() => descriptionRef.current?.focus(), 30);
    return () => {
      document.removeEventListener('keydown', onKey);
      window.clearTimeout(t);
    };
  }, [closeCreate, createOpen]);

  const buildApp = useCallback(async () => {
    const description = createDescription.trim();
    if (!description || createBusy || building) return;
    const requestedName = createName.trim() || 'New app';
    // Dismiss the dialog BEFORE the request. The builder can take tens of
    // seconds and used to hold the whole console behind a modal with no way
    // to leave; the job is server-side and does not need the modal alive.
    setCreateBusy(true);
    setCreateError('');
    setBuildError('');
    setCreateOpen(false);
    setCreateName('');
    setCreateDescription('');
    setBuilding({ name: requestedName });
    try {
      const res = await api.appBuild(description, requestedName === 'New app' ? undefined : requestedName);
      if (res.error) throw new Error(res.error);
      const id = res.app?.id;
      await load();
      if (id) await openApp(id);
      toast.success(`App built${res.app?.name ? `: ${res.app.name}` : ''}`);
    } catch (e) {
      const msg = String((e as Error)?.message || e);
      setBuildError(msg);
      toast.error(`App build failed: ${msg}`);
    } finally {
      setCreateBusy(false);
      setBuilding(null);
    }
  }, [building, createBusy, createDescription, createName, load, openApp]);

  const items = detail?.items || [];
  const spec = detail?.spec || {};
  const columns = useMemo(() => columnsOf(spec, items), [spec, items]);
  const filteredItems = useMemo(() => {
    const q = itemFilter.trim().toLowerCase();
    if (!q) return items;
    return items.filter((it) => JSON.stringify(it).toLowerCase().includes(q));
  }, [itemFilter, items]);

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      {railMode === 'collapsed'
        ? <PaneStrip id="apps.rail" label="Apps list" />
        : (
      <aside className="flex w-full max-h-[34vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:h-full lg:max-h-none lg:w-[272px] lg:border-b-0 lg:border-r"
             aria-label="Apps">
        <div className="flex items-center justify-between px-3.5 pb-2 pt-3.5">
          <h2 className="text-xs font-bold tracking-wide">Apps</h2>
          <button
            type="button"
            onClick={() => { setCreateOpen(true); setCreateError(''); }}
            disabled={!!building}
            title={building ? `Building ${building.name}…` : undefined}
            className="flex items-center gap-1 rounded-md bg-violet-400 px-2 py-1 text-[11px] font-bold text-[#0b0b0e] hover:brightness-110 disabled:opacity-50">
            {building ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />}
            {building ? 'Building…' : 'New app'}
          </button>
        </div>
        {building && (
          <div role="status" aria-live="polite"
               className="mx-2.5 mb-2 flex items-center gap-2 rounded-lg border border-violet-400/30 bg-violet-400/5 px-2.5 py-2 text-[11px] leading-snug text-violet-200">
            <Loader2 size={12} className="flex-none animate-spin" />
            <span className="min-w-0">Building <b className="font-semibold">{building.name}</b>… you can leave this section.</span>
          </div>
        )}
        <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto px-2.5 pb-3">
          {!loaded && <Skel />}
          {loaded && loadError && (
            <div className="rounded-lg border border-red-400/30 bg-red-400/5 p-2.5 text-[11px] text-red-200">
              couldn't load apps ({loadError}).
              <button onClick={() => void load()} className="ml-1 underline">retry</button>
            </div>
          )}
          {loaded && !loadError && !apps.length && (
            <Empty icon={<LayoutGrid size={26} />} title="No apps yet"
              sub="Describe an app and Aria builds it." />
          )}
          {apps.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => void openApp(a.id)}
              className={`block w-full rounded-lg border p-2.5 text-left ${
                openId === a.id
                  ? 'border-violet-400/60 bg-violet-400/5'
                  : 'border-white/10 bg-[#0e0e12] hover:border-white/25'
              }`}>
              <div className="flex items-center gap-1.5">
                <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold">
                  {a.name}
                </span>
                <Pill tone={a.kind === 'a2ui' ? 'info' : 'muted'}>
                  {a.kind === 'a2ui' ? 'agent' : a.kind === 'json_feed' ? 'feed' : 'tracker'}
                </Pill>
                {a.error ? <Pill tone="err">error</Pill> : null}
              </div>
              <div className="mt-1 truncate text-[10.5px] text-zinc-muted">
                {a.schedule} · {a.next_refresh ? `next ${fmtTs(a.next_refresh)}` : 'manual'}
              </div>
            </button>
          ))}
        </div>
      </aside>
        )}

      <main id={mainId} tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb={selected ? `Apps / ${selected.name}` : 'Apps'} panes="apps">
          {openId && (
            <>
              <button type="button" onClick={() => void refresh()} disabled={busy}
                aria-label="Refresh app now"
                className="flex items-center gap-1.5 rounded-md border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] hover:border-violet-400 disabled:opacity-50">
                {busy ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                Refresh
              </button>
              <button type="button" onClick={() => void remove()}
                aria-label="Delete app"
                className="flex items-center gap-1.5 rounded-md border border-red-400/40 bg-red-400/5 px-2.5 py-1 text-[11px] text-red-200 hover:bg-red-400/15">
                <Trash2 size={12} />
              </button>
            </>
          )}
        </TopBar>

        <div className="min-h-0 flex-1 overflow-y-auto p-3.5">
          {buildError && (
            <div role="alert" className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-rose-400/30 bg-rose-400/5 px-3 py-2 text-[11.5px] text-rose-200">
              <span className="min-w-0 flex-1">App build failed: {buildError}</span>
              <button type="button" onClick={() => setBuildError('')}
                className="rounded border border-rose-400/40 px-2 py-0.5 text-[10.5px] font-bold">
                dismiss
              </button>
            </div>
          )}
          {!openId && (
            <div className="flex h-full items-center justify-center">
              <div className="max-w-[440px] text-center">
                <LayoutGrid size={34} className="mx-auto mb-3 text-violet-300/80" />
                <h2 className="text-[15px] font-semibold text-zinc-100">Build an app by describing it</h2>
                <p className="mt-1.5 text-[12px] leading-relaxed text-zinc-500">
                  Aria designs the interface and, when it needs live data or a
                  recurring update, wires the refresh into the backend. Pick an
                  app on the left or create a new one.
                </p>
                <button type="button" onClick={() => setCreateOpen(true)}
                  className="mt-4 inline-flex items-center gap-1.5 rounded-md bg-violet-400 px-3.5 py-2 text-xs font-bold text-[#0b0b0e] hover:brightness-110">
                  <Sparkles size={13} /> New app
                </button>
              </div>
            </div>
          )}

          {openId && detailLoading && <Skel n={5} />}
          {openId && detailError && (
            <div role="alert" className="rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-xs text-red-200">
              couldn't open this app — {detailError}
              <button onClick={() => void openApp(openId)} className="ml-1 underline">retry</button>
            </div>
          )}
          {openId && detail && (
            <>
              <Pane id="apps.header">
              <div className="mb-3 rounded-[10px] border border-white/10 bg-[#0e0e12] px-3.5 py-3">
                {editOpen ? (
                  <div className="flex flex-col gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <input value={editName} onChange={(e) => setEditName(e.target.value)}
                        aria-label="App name" placeholder="App name"
                        className="h-8 min-w-0 flex-1 rounded border border-white/10 bg-black/40 px-2.5 text-xs outline-none focus:border-violet-400" />
                      <input value={editSchedule} onChange={(e) => setEditSchedule(e.target.value)}
                        aria-label="Refresh schedule" placeholder="manual · daily@HH:MM · every Nm"
                        className="h-8 w-52 rounded border border-white/10 bg-black/40 px-2.5 text-[11px] outline-none focus:border-violet-400" />
                    </div>
                    <textarea value={editDescription} onChange={(e) => setEditDescription(e.target.value)}
                      rows={2} aria-label="App description" placeholder="Description"
                      className="w-full resize-y rounded border border-white/10 bg-black/40 p-2.5 text-[12px] leading-relaxed outline-none focus:border-violet-400" />
                    {editError && (
                      <p role="alert" className="rounded border border-rose-400/30 bg-rose-400/5 px-2.5 py-1.5 text-[11px] text-rose-200">{editError}</p>
                    )}
                    <div className="flex items-center justify-end gap-2">
                      <button type="button" onClick={() => { setEditOpen(false); setEditError(''); }} disabled={editBusy}
                        className="rounded border border-white/15 px-3 py-1.5 text-[11px] text-zinc-300 hover:border-white/30 disabled:opacity-40">Cancel</button>
                      <button type="button" onClick={() => void saveEdit()} disabled={editBusy || !editName.trim()}
                        className="flex items-center gap-1.5 rounded bg-violet-400 px-3.5 py-1.5 text-[11.5px] font-bold text-[#0b0b0e] hover:brightness-110 disabled:opacity-40">
                        {editBusy ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                        {editBusy ? 'Saving…' : 'Save'}
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-wrap items-start gap-2">
                    <div className="min-w-0 flex-1">
                      <h1 className="truncate text-[15px] font-semibold text-zinc-100">{detail.name}</h1>
                      <p className="mt-0.5 text-[11.5px] leading-snug text-zinc-500">
                        {detail.description
                          || (detail.kind === 'a2ui'
                            ? 'Agent-built interactive app.'
                            : `${detail.kind} tracker`)}
                      </p>
                    </div>
                    <Pill tone={detail.kind === 'a2ui' ? 'info' : 'muted'}>
                      {detail.kind === 'a2ui' ? 'agent-built' : detail.kind}
                    </Pill>
                    <span className="text-[10.5px] text-zinc-500">
                      {detail.schedule} · {detail.next_refresh ? `next ${fmtTs(detail.next_refresh)}` : 'manual'}
                    </span>
                    <button type="button" onClick={startEdit} aria-label="Edit app details"
                      className="rounded p-1 text-zinc-500 hover:text-zinc-200"><Pencil size={13} /></button>
                  </div>
                )}
              </div>
              </Pane>

              {actionError && (
                <p role="alert" className="mb-3 rounded-lg border border-rose-400/30 bg-rose-400/5 px-3 py-2 text-[11.5px] text-rose-200">
                  {actionError}
                </p>
              )}
              {detail.error && (
                <p role="status" className="mb-3 rounded-lg border border-amber-400/30 bg-amber-400/5 px-3 py-2 text-[11.5px] text-amber-200">
                  last refresh: {detail.error}
                </p>
              )}

              {isA2ui ? (
                <div className="rounded-[10px] border border-white/10 bg-[#08080a] p-3.5">
                  {surface ? (
                    <A2ui surface={surface} busy={busy} onAction={(a, args) => void runAction(a, args)} />
                  ) : (
                    <Empty icon={<Sparkles size={28} />} title="No surface yet"
                      sub="Refresh this app to rebuild its interface." />
                  )}
                </div>
              ) : (
                <div className="rounded-[10px] border border-white/10 bg-[#0e0e12]">
                  <div className="flex flex-wrap items-center gap-2 border-b border-white/10 px-3 py-2">
                    <span className="text-[10.5px] font-bold tracking-[0.12em] text-zinc-500">
                      {detail.count} ITEMS
                    </span>
                    <Pane id="apps.filter">
                    <input
                      value={itemFilter}
                      onChange={(e) => setItemFilter(e.target.value)}
                      placeholder="Filter items…"
                      aria-label="Filter items"
                      className="ml-auto h-7 w-40 rounded border border-white/10 bg-black/40 px-2 text-[11px] outline-none focus:border-violet-400"
                    />
                    </Pane>
                  </div>
                  {!items.length ? (
                    <Empty icon={<LayoutGrid size={28} />} title="No items yet"
                      sub="Hit Refresh to fetch the source." />
                  ) : (
                    <div className="max-h-[60vh] overflow-auto">
                      <table className="w-full text-left text-[11.5px]">
                        <thead className="sticky top-0 bg-[#0e0e12] text-[10px] uppercase tracking-wide text-zinc-500">
                          <tr>{columns.map((c) => <th key={c.field} className="px-2.5 py-2">{c.label}</th>)}</tr>
                        </thead>
                        <tbody>
                          {filteredItems.map((it, i) => (
                            <tr key={String(it.id ?? it[columns[0]?.field] ?? i)}
                                className="border-t border-white/5 hover:bg-white/[0.02]">
                              {columns.map((c) => (
                                <td key={c.field} className="max-w-[320px] truncate px-2.5 py-1.5 text-zinc-300"
                                    title={cell(it[c.field])}>{cell(it[c.field])}</td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </main>

      {createOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/65 p-4"
             role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) closeCreate(); }}>
          <div role="dialog" aria-modal="true" aria-label="New app"
               className="w-full max-w-[560px] rounded-xl border border-white/15 bg-[#0e0e12] shadow-2xl">
            <div className="flex items-center gap-2 border-b border-white/10 px-4 py-3">
              <Sparkles size={14} className="text-violet-300" />
              <span className="flex-1 text-[13px] font-semibold">New app</span>
              <button type="button" onClick={closeCreate} aria-label="Close new app"
                className="rounded p-1 text-zinc-500 hover:text-zinc-200"><X size={14} /></button>
            </div>
            <div className="p-4">
              <label className="block text-[11px] text-zinc-400">Name <span className="text-zinc-600">(optional)</span>
                <input value={createName} onChange={(e) => setCreateName(e.target.value)}
                  placeholder="Aria can name it"
                  className="mt-1 h-8 w-full rounded border border-white/10 bg-black/40 px-2.5 text-xs outline-none focus:border-violet-400" />
              </label>
              <label className="mt-3 block text-[11px] text-zinc-400">What should it do?
                <textarea ref={descriptionRef} value={createDescription}
                  onChange={(e) => setCreateDescription(e.target.value)}
                  rows={5}
                  placeholder="Describe what the app should show, what the user can do in it, and whether it should update itself."
                  className="mt-1 w-full resize-y rounded border border-white/10 bg-black/40 p-2.5 text-[12.5px] leading-relaxed outline-none focus:border-violet-400" />
              </label>
              {createError && (
                <p role="alert" className="mt-2 rounded border border-rose-400/30 bg-rose-400/5 px-2.5 py-2 text-[11.5px] text-rose-200">
                  {createError}
                </p>
              )}
              <div className="mt-3 flex items-center justify-end gap-2">
                <button type="button" onClick={closeCreate} disabled={createBusy}
                  className="rounded border border-white/15 px-3 py-1.5 text-[11px] text-zinc-300 hover:border-white/30 disabled:opacity-40">
                  Cancel
                </button>
                <button type="button" onClick={() => void buildApp()}
                  disabled={createBusy || !createDescription.trim()}
                  className="flex items-center gap-1.5 rounded bg-violet-400 px-3.5 py-1.5 text-[11.5px] font-bold text-[#0b0b0e] hover:brightness-110 disabled:opacity-40">
                  {createBusy ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
                  {createBusy ? 'Building…' : 'Build app'}
                </button>
              </div>
              {createBusy && (
                <p className="mt-2 text-[10.5px] text-zinc-500">
                  Aria is designing the surface and, if needed, checking live data.
                </p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
