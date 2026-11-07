import { useCallback, useEffect, useRef, useState } from 'react';
import { useSectionActive } from './KeepAlive';
import {
  Box, ExternalLink, Eye, EyeOff, Gauge, Play, RefreshCw, Square,
  Terminal, Wand2,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  api, type BlenderBounds, type BlenderCatalog, type BlenderOperator,
  type BlenderRender, type BlenderStatus, type PartDetail,
} from '../api';

type Props = {
  part: PartDetail;
  version: string;
  onChanged: (part: PartDetail) => void;
};

function screenBounds(el: HTMLElement | null): BlenderBounds | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  return {
    x: Math.round((window.screenX + r.left) * dpr),
    y: Math.round((window.screenY + r.top) * dpr),
    width: Math.round(r.width * dpr),
    height: Math.round(r.height * dpr),
  };
}

function defaultParams(op?: BlenderOperator): string {
  if (!op) return '{}';
  const out: Record<string, unknown> = {};
  for (const p of op.params || []) {
    if (!p.name || p.name === 'rna_type') continue;
    if (p.type === 'ENUM') {
      out[p.name] = p.default ?? p.enum?.[0] ?? '';
    } else if (p.type === 'BOOLEAN') {
      out[p.name] = p.default === true;
    } else if (p.type === 'FLOAT' || p.type === 'INT') {
      out[p.name] = p.default ?? 0;
    } else if (p.type === 'STRING') {
      out[p.name] = p.default ?? '';
    }
  }
  return JSON.stringify(out, null, 2);
}

function Badge({ tone, children }: { tone: 'good' | 'muted' | 'warn'; children: React.ReactNode }) {
  const cls = tone === 'good' ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200'
    : tone === 'warn' ? 'border-amber-400/30 bg-amber-400/10 text-amber-200'
      : 'border-white/10 bg-white/[0.03] text-zinc-400';
  return <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${cls}`}>{children}</span>;
}

export default function BlenderWorkspace({ part, version, onChanged }: Props) {
  const host = useRef<HTMLDivElement | null>(null);
  const sectionActive = useSectionActive();
  const [status, setStatus] = useState<BlenderStatus | null>(null);
  const [live, setLive] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [hidden, setHidden] = useState(false);
  const [catalog, setCatalog] = useState<BlenderCatalog | null>(null);
  const [query, setQuery] = useState('modifier');
  const [op, setOp] = useState('object.modifier_add');
  const [params, setParams] = useState('{\n  "type": "BEVEL"\n}');
  const [render, setRender] = useState<BlenderRender | null>(null);
  const [engine, setEngine] = useState('BLENDER_EEVEE');
  const [width, setWidth] = useState(1024);
  const [height, setHeight] = useState(1024);
  const [samples, setSamples] = useState(32);

  const refreshStatus = useCallback(async () => {
    try {
      const d = await api.blenderStatus();
      setStatus(d);
      const isLive = !!d.live?.live && d.live?.part_id === part.id;
      setLive(isLive);
      if (isLive) setHidden(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [part.id]);

  useEffect(() => {
    void refreshStatus();
    const t = window.setInterval(() => void refreshStatus(), 5000);
    return () => {
      window.clearInterval(t);
      // Leaving the workspace must not leave a native window floating over
      // another console page. The Blender session itself survives; the
      // window is hidden and shown again on return.
      void api.blenderShow(part.id, false).catch(() => {});
    };
  }, [part.id, refreshStatus]);

  const pushBounds = useCallback(() => {
    if (!live) return;
    const bounds = screenBounds(host.current);
    if (!bounds) return;
    void api.blenderBounds(part.id, bounds).catch(() => {});
  }, [live, part.id]);

  useEffect(() => {
    if (!live) return;
    // A remount (switching back from another Parts tab) must restore the
    // native window, not just compute its bounds.
    void api.blenderShow(part.id, true).catch(() => {});
    const t = window.setTimeout(() => pushBounds(), 120);
    return () => window.clearTimeout(t);
  }, [live, part.id, pushBounds]);

  useEffect(() => {
    if (!live) return;
    const el = host.current;
    if (!el) return;
    const ro = new ResizeObserver(() => pushBounds());
    ro.observe(el);
    window.addEventListener('resize', pushBounds);
    window.addEventListener('scroll', pushBounds, true);
    // Moving the Aria window fires no resize event; a slow tick keeps the
    // docked Blender window on the same part of the screen as its host.
    const follow = window.setInterval(() => pushBounds(), 1200);
    pushBounds();
    return () => {
      ro.disconnect();
      window.clearInterval(follow);
      window.removeEventListener('resize', pushBounds);
      window.removeEventListener('scroll', pushBounds, true);
    };
  }, [live, pushBounds]);

  useEffect(() => {
    if (!live) return;
    void api.blenderShow(part.id, !!sectionActive).catch(() => {});
  }, [live, part.id, sectionActive]);

  const open = useCallback(async () => {
    setBusy('open');
    setError('');
    try {
      const bounds = screenBounds(host.current) || undefined;
      const r = await api.blenderOpen(part.id, version, bounds as BlenderBounds);
      if (r.error) throw new Error(r.error);
      setLive(true);
      setHidden(false);
      toast.success('Blender opened in the Parts workspace');
      await refreshStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [part.id, refreshStatus, version]);

  const sync = useCallback(async () => {
    setBusy('sync');
    setError('');
    try {
      const r = await api.blenderSync(part.id);
      if (r.error || !r.part) throw new Error(r.error || 'sync failed');
      onChanged(r.part);
      toast.success(`Saved from Blender as ${r.part.current_version}`);
      await refreshStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [onChanged, part.id, refreshStatus]);

  const toggleHidden = useCallback(async () => {
    const next = !hidden;
    setHidden(next);
    await api.blenderShow(part.id, !next).catch(() => {});
  }, [hidden, part.id]);

  const close = useCallback(async () => {
    setBusy('close');
    try {
      await api.blenderClose(part.id);
      setLive(false);
      toast.success('Blender session closed');
      await refreshStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [part.id, refreshStatus]);

  const search = useCallback(async () => {
    setBusy('catalog');
    setError('');
    try {
      const d = await api.blenderCatalog(query, 40);
      if (d.error) throw new Error(d.error);
      setCatalog(d);
      if (d.operators?.length) {
        const first = d.operators[0];
        setOp(first.name);
        setParams(defaultParams(first));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [query]);

  const chooseOp = useCallback((name: string) => {
    setOp(name);
    const found = catalog?.operators?.find((o) => o.name === name);
    setParams(defaultParams(found));
  }, [catalog]);

  const runOp = useCallback(async () => {
    let parsed: Record<string, unknown> = {};
    try {
      parsed = params.trim() ? JSON.parse(params) : {};
    } catch {
      setError('Params must be valid JSON');
      return;
    }
    setBusy('op');
    setError('');
    try {
      const r = await api.blenderOp(part.id, {
        version, op, params: parsed, mode: '',
      });
      if (r.error || !r.part) throw new Error(r.error || 'operator failed');
      onChanged(r.part);
      toast.success(`${op} saved as ${r.part.current_version}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [onChanged, op, params, part.id, version]);

  const doRender = useCallback(async () => {
    setBusy('render');
    setError('');
    try {
      const r = await api.blenderRender(part.id, {
        version, engine, width, height, samples,
      });
      if (r.error || !r.image_base64) throw new Error(r.error || 'render failed');
      setRender(r);
      toast.success('Render complete');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [engine, height, part.id, samples, version, width]);

  const chosen = catalog?.operators?.find((o) => o.name === op);
  const liveInfo = status?.live?.info || {};

  return (
    <div className="grid min-h-0 gap-3 xl:grid-cols-[minmax(0,1fr)_350px]">
      <div className="flex min-w-0 flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-white/10 bg-black/25 px-2 py-1.5">
          <Badge tone={status?.available ? 'good' : 'warn'}>
            {status?.available ? 'Blender available' : 'Blender not found'}
          </Badge>
          {live ? <Badge tone="good">live · {String(liveInfo.blender || 'Blender')}</Badge>
            : <Badge tone="muted">not running</Badge>}
          <span className="text-[10px] text-zinc-500">
            {status?.executable ? status.executable.split('\\').slice(-2).join('\\') : ''}
          </span>
          <span className="ml-auto text-[10px] text-zinc-600">
            hosted as a native Blender window over this viewport
          </span>
        </div>

        <div ref={host}
          className="relative min-h-[420px] flex-1 overflow-hidden rounded-lg border border-white/10 bg-[#08080b]">
          {!live ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center">
              <Box size={30} className="text-zinc-600" />
              <p className="m-0 text-[13px] font-semibold text-zinc-300">
                Open {part.name} in real Blender
              </p>
              <p className="m-0 max-w-md text-[11px] leading-relaxed text-zinc-500">
                The full Blender UI opens over this panel. Its operators, add-ons,
                sculpting, UV tools, materials and renders are driven by the live
                bridge; changes come back as a new immutable version.
              </p>
              <button type="button" onClick={() => void open()} disabled={!!busy || !status?.available}
                className="inline-flex items-center gap-2 rounded-md bg-violet-500 px-3 py-1.5 text-[11.5px] font-semibold text-white disabled:opacity-40">
                <ExternalLink size={13} />
                {busy === 'open' ? 'Opening Blender…' : 'Open in Blender'}
              </button>
            </div>
          ) : (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center">
              <p className="m-0 text-[11.5px] text-zinc-500">
                {hidden ? 'Blender is hidden.' : 'Blender is docked over this panel.'}
              </p>
              <div className="flex gap-1.5">
                <button type="button" onClick={() => void toggleHidden()}
                  className="inline-flex items-center gap-1.5 rounded border border-white/15 px-2 py-1 text-[11px] text-zinc-300">
                  {hidden ? <Eye size={12} /> : <EyeOff size={12} />}
                  {hidden ? 'Show' : 'Hide'}
                </button>
                <button type="button" onClick={() => void sync()} disabled={!!busy}
                  className="inline-flex items-center gap-1.5 rounded border border-emerald-400/30 bg-emerald-400/10 px-2 py-1 text-[11px] text-emerald-200 disabled:opacity-40">
                  <RefreshCw size={12} />{busy === 'sync' ? 'Saving…' : 'Sync to Aria'}
                </button>
                <button type="button" onClick={() => void close()} disabled={!!busy}
                  className="inline-flex items-center gap-1.5 rounded border border-white/15 px-2 py-1 text-[11px] text-zinc-400 disabled:opacity-40">
                  <Square size={11} />Close
                </button>
              </div>
            </div>
          )}
        </div>

        {render?.image_base64 && (
          <div className="rounded-lg border border-white/10 bg-[#0a0a0d] p-2">
            <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-wide text-zinc-500">
              <span>Blender render</span>
              <span>{render.engine} · {width}×{height}</span>
            </div>
            <img src={`data:image/png;base64,${render.image_base64}`}
              alt="Blender render of the current part"
              className="max-h-72 w-full rounded object-contain" />
          </div>
        )}
      </div>

      <aside className="flex min-h-0 flex-col gap-2">
        <div className="rounded-lg border border-white/10 bg-surface-1/60 p-2">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10.5px] uppercase tracking-wide text-zinc-500">Blender session</span>
            <button type="button" onClick={() => void refreshStatus()}
              className="rounded p-0.5 text-zinc-600 hover:text-zinc-300" aria-label="Refresh Blender status">
              <RefreshCw size={11} />
            </button>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {!live ? (
              <button type="button" onClick={() => void open()} disabled={!!busy || !status?.available}
                className="inline-flex items-center gap-1.5 rounded border border-violet-400/40 bg-violet-400/15 px-2 py-1 text-[11px] text-violet-100 disabled:opacity-40">
                <ExternalLink size={12} />Open
              </button>
            ) : (
              <>
                <button type="button" onClick={() => void toggleHidden()}
                  className="inline-flex items-center gap-1.5 rounded border border-white/15 px-2 py-1 text-[11px] text-zinc-300">
                  {hidden ? <Eye size={12} /> : <EyeOff size={12} />}{hidden ? 'Show' : 'Hide'}
                </button>
                <button type="button" onClick={() => void sync()} disabled={!!busy}
                  className="inline-flex items-center gap-1.5 rounded border border-emerald-400/30 bg-emerald-400/10 px-2 py-1 text-[11px] text-emerald-200 disabled:opacity-40">
                  <RefreshCw size={12} />Sync
                </button>
                <button type="button" onClick={() => void close()} disabled={!!busy}
                  className="inline-flex items-center gap-1.5 rounded border border-white/15 px-2 py-1 text-[11px] text-zinc-400 disabled:opacity-40">
                  <Square size={11} />Close
                </button>
              </>
            )}
          </div>
          {status?.live?.part_id && status.live.part_id !== part.id && (
            <p className="mt-1.5 text-[10.5px] text-amber-300">
              Another part is open in Blender. Close it before opening this one.
            </p>
          )}
        </div>

        <div className="rounded-lg border border-white/10 bg-surface-1/60 p-2">
          <div className="mb-1 flex items-center gap-1.5 text-[10.5px] uppercase tracking-wide text-zinc-500">
            <Terminal size={11} />Operator catalog
          </div>
          <div className="flex gap-1">
            <input value={query} onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void search(); }}
              placeholder="modifier, uv, sculpt…" aria-label="Search Blender operators"
              className="min-w-0 flex-1 rounded border border-white/12 bg-black/30 px-2 py-1 text-[11px] text-zinc-200 outline-none focus:border-violet-400" />
            <button type="button" onClick={() => void search()} disabled={busy === 'catalog'}
              className="rounded border border-white/15 px-2 py-1 text-[11px] text-zinc-300 disabled:opacity-40">
              {busy === 'catalog' ? '…' : 'Search'}
            </button>
          </div>
          {catalog?.operators?.length ? (
            <>
              <select value={op} onChange={(e) => chooseOp(e.target.value)}
                aria-label="Blender operator"
                className="mt-1 w-full rounded border border-white/12 bg-black/30 px-1.5 py-1 text-[11px] text-zinc-200">
                {catalog.operators.map((o) => (
                  <option key={o.name} value={o.name}>{o.name}</option>
                ))}
              </select>
              {chosen?.description && (
                <p className="mt-1 text-[10px] leading-snug text-zinc-500">{chosen.description}</p>
              )}
              <textarea value={params} onChange={(e) => setParams(e.target.value)}
                rows={5} aria-label="Operator parameters JSON"
                className="mt-1 w-full resize-y rounded border border-white/12 bg-black/30 px-2 py-1 font-mono text-[10.5px] text-zinc-200 outline-none focus:border-violet-400" />
              <button type="button" onClick={() => void runOp()} disabled={!!busy}
                className="mt-1 inline-flex w-full items-center justify-center gap-1.5 rounded bg-violet-500 px-2 py-1.5 text-[11px] font-semibold text-white disabled:opacity-40">
                <Play size={12} />{busy === 'op' ? 'Running…' : 'Run operator → new version'}
              </button>
            </>
          ) : (
            <p className="mt-1 text-[10px] leading-snug text-zinc-600">
              Search Blender's own operator registry. Results include the
              parameters discovered from `bpy.ops`; the call runs inside the
              live GUI and is saved as a new version.
            </p>
          )}
        </div>

        <div className="rounded-lg border border-white/10 bg-surface-1/60 p-2">
          <div className="mb-1 flex items-center gap-1.5 text-[10.5px] uppercase tracking-wide text-zinc-500">
            <Gauge size={11} />Render
          </div>
          <div className="grid grid-cols-2 gap-1.5">
            <label className="text-[10px] text-zinc-500">Engine
              <select value={engine} onChange={(e) => setEngine(e.target.value)}
                className="mt-0.5 w-full rounded border border-white/12 bg-black/30 px-1 py-1 text-[10.5px] text-zinc-200">
                <option value="BLENDER_EEVEE">EEVEE</option>
                <option value="CYCLES">Cycles</option>
              </select>
            </label>
            <label className="text-[10px] text-zinc-500">Samples
              <input type="number" min={1} max={256} value={samples}
                onChange={(e) => setSamples(Math.max(1, Math.min(256, Number(e.target.value) || 32)))}
                className="mt-0.5 w-full rounded border border-white/12 bg-black/30 px-1 py-1 text-[10.5px] text-zinc-200" />
            </label>
            <label className="text-[10px] text-zinc-500">Width
              <input type="number" min={64} max={4096} value={width}
                onChange={(e) => setWidth(Math.max(64, Math.min(4096, Number(e.target.value) || 1024)))}
                className="mt-0.5 w-full rounded border border-white/12 bg-black/30 px-1 py-1 text-[10.5px] text-zinc-200" />
            </label>
            <label className="text-[10px] text-zinc-500">Height
              <input type="number" min={64} max={4096} value={height}
                onChange={(e) => setHeight(Math.max(64, Math.min(4096, Number(e.target.value) || 1024)))}
                className="mt-0.5 w-full rounded border border-white/12 bg-black/30 px-1 py-1 text-[10.5px] text-zinc-200" />
            </label>
          </div>
          <button type="button" onClick={() => void doRender()} disabled={!!busy}
            className="mt-1 inline-flex w-full items-center justify-center gap-1.5 rounded border border-white/20 px-2 py-1.5 text-[11px] font-semibold text-zinc-200 disabled:opacity-40">
            <Wand2 size={12} />{busy === 'render' ? 'Rendering…' : 'Render current version'}
          </button>
        </div>

        {error && <p role="alert" className="rounded border border-rose-400/30 bg-rose-400/10 p-2 text-[11px] text-rose-200">{error}</p>}
        <p className="text-[10px] leading-snug text-zinc-600">
          The operator catalog is Blender's own `bpy.ops` registry. Every
          successful mutation is checkpointed in Blender and saved here as an
          immutable version. Use Sync to capture manual edits made in Blender.
        </p>
      </aside>
    </div>
  );
}
