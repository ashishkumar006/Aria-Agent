import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Gauge, RotateCcw, Upload, Wand2 } from 'lucide-react';
import { Rail, SkipLink, TopBar } from '../components/ui';
import { useMainId } from '../components/KeepAlive';
import { headers } from '../api';

/**
 * Parts: upload a mesh, see it, let an agent optimise it in a measured loop.
 *
 * The loop the UI drives is exactly the one the gateway is built for:
 *
 *   measure -> propose a pass -> compare the objective -> keep or revert
 *
 * The important part is `objective`. Every iteration returns one lower-is-
 * better number, so "did that help?" is a comparison and the loop stops on its
 * own when the objective stops falling. An earlier version of this loop fed
 * the ORIGINAL mesh back each time, so a later iteration that was actually
 * worse still reported "improved" - the chain is what makes the comparison
 * meaningful.
 *
 * Nothing here decides what to optimise. The passes are described by
 * `/v1/parts/optimise/passes` and the caller picks; the geometry is drawn by
 * `/v1/parts/geometry` as interleaved positions plus a flat index buffer,
 * which is the shape a WebGL/three.js attribute wants.
 */

type PassInfo = {
  name: string;
  description: string;
  params: Record<string, unknown>;
  modifies: boolean;
};

type Metrics = {
  vertices: number;
  faces: number;
  volume_mm3: number;
  surface_area_mm2: number;
  dimensions_mm: number[];
  min_edge_mm: number;
  median_edge_mm: number;
  overhang_faces: number;
  overhang_area_mm2: number;
  thin_regions: number;
};

type Geometry = {
  positions: number[];
  indices: number[];
  position_count: number;
  face_count: number;
};

type Step = {
  pass: string;
  faces: number;
  objective: number;
  kept: boolean;
  note: string;
};

async function post(url: string, blob: Blob, query: string) {
  const fd = new FormData();
  fd.append('file', new File([blob], 'part.stl', { type: 'application/octet-stream' }));
  // Same-origin agent proxy: the browser cannot call the gateway directly
  // (no CORS, no gateway token in the page), and the per-launch X-Aria-Token
  // has to ride along, so every call goes through the shared `headers()`.
  const r = await fetch(`/api/parts${url}${query ? `?${query}` : ''}`, {
    method: 'POST',
    headers: headers(),
    body: fd,
  });
  const parsed = await r.json().catch(() => null);
  if (!r.ok || parsed?.error) {
    throw new Error(parsed?.error ?? `HTTP ${r.status}`);
  }
  return parsed as Promise<{
    metrics: Metrics;
    objective: number;
    geometry: Geometry;
    note: string;
    changed?: boolean;
    faces_before?: number;
    faces_after?: number;
    objective_before?: number;
    objective_after?: number;
    improved?: boolean;
    result_stl_base64?: string | null;
    note_detail?: string;
  }>;
}

/** Base64 -> Blob in the browser, without pulling in a dependency. */
function b64ToBlob(b64: string): Blob {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: 'application/octet-stream' });
}

function fmt(n: number, digits = 1): string {
  return Number.isFinite(n) ? n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }) : '—';
}

export default function Parts() {
  const mainId = useMainId();
  const [file, setFile] = useState<Blob | null>(null);
  const [fileName, setFileName] = useState('');
  const [passes, setPasses] = useState<PassInfo[]>([]);
  const [passErr, setPassErr] = useState('');
  const [pass, setPass] = useState('decimate');
  const [budget, setBudget] = useState(20_000);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [geom, setGeom] = useState<Geometry | null>(null);
  const [objective, setObjective] = useState<number | null>(null);
  const [history, setHistory] = useState<Step[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [running, setRunning] = useState(false);
  const stop = useRef(false);

  // Pass registry, so the UI never hardcodes a name or its parameters.
  useEffect(() => {
    let live = true;
    fetch('/api/parts/passes', { headers: headers() })
      .then((r) => r.json())
      .then((d: { passes?: PassInfo[]; error?: string }) => {
        if (d?.error) throw new Error(d.error);
        if (live) { setPasses(d.passes ?? []); setPassErr(''); }
      })
      .catch((e) => {
        /* Was a useMemo with a swallowed catch: a render-phase side effect that
           fired twice under StrictMode and, when the gateway was unreachable,
           left an empty pass dropdown with no explanation. */
        if (live) { setPasses([]); setPassErr(String((e as Error)?.message || e)); }
      });
    return () => { live = false; };
  }, []);

  const measure = useCallback(async (blob: Blob) => {
    setError('');
    const g = await post('/geometry', blob, '');
    setGeom(g.geometry);
    setMetrics(g.metrics);
    setObjective(g.objective);
    return g;
  }, []);

  const onPick = useCallback(async (f: File | null) => {
    if (!f) return;
    setBusy(true);
    setHistory([]);
    setFileName(f.name);
    try {
      const blob = f.slice();
      setFile(blob);
      await measure(blob);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [measure]);

  /**
   * One chain step: run the pass on `current`, and KEEP the result only when
   * the objective actually fell. Chaining is the whole point - comparing each
   * pass against the original mesh made a later, worse iteration look better.
   */
  const stepOnce = useCallback(async (current: Blob): Promise<Blob | null> => {
    const q = pass === 'decimate'
      ? `pass_name=decimate&target_faces=${budget}`
      : `pass_name=${encodeURIComponent(pass)}`;
    const r = await post('/optimise', current, q);

    const objBefore = r.objective_before ?? r.objective ?? 0;
    const objAfter = r.objective_after ?? objBefore;
    const improved = r.objective_after !== undefined
      ? objAfter < objBefore
      : (r.improved ?? false);
    const faces = r.faces_after ?? r.metrics?.faces ?? 0;

    if (!r.changed || !r.result_stl_base64) {
      setHistory((h) => [...h, {
        pass, faces, objective: objAfter, kept: false,
        note: r.note_detail ?? r.note ?? 'no change',
      }]);
      return null;
    }

    const next = b64ToBlob(r.result_stl_base64);
    setHistory((h) => [...h, {
      pass, faces, objective: objAfter, kept: improved,
      note: improved ? 'kept' : 'reverted: objective got worse',
    }]);

    if (!improved) return null;
    await measure(next);
    return next;
  }, [budget, measure, pass]);

  const runLoop = useCallback(async () => {
    if (!file) return;
    setBusy(true);
    setRunning(true);
    setError('');
    stop.current = false;
    let current: Blob | null = file;
    try {
      // Bounded: the loop exits on its own when the objective stops falling,
      // and this ceiling is only a backstop.
      for (let i = 0; i < 8 && current && !stop.current; i++) {
        current = await stepOnce(current);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setRunning(false);
    }
  }, [file, stepOnce]);

  const chosen = passes.find((p) => p.name === pass);

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <main id={mainId} tabIndex={-1}
            className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Parts" />
        <div className="min-h-0 flex-1 overflow-auto p-4">
          <p className="mb-4 text-xs text-zinc-500">
            Upload a mesh, then optimise it in a measured loop. Every step
            reports one objective — lower is better — so the loop stops when
            further changes stop helping.
          </p>

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="flex min-h-0 flex-col gap-3">
          <label className="flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed border-white/15 bg-white/[0.02] px-4 py-6 text-xs text-zinc-400 hover:border-white/30">
            <Upload size={14} />
            {fileName || 'Choose an STL or OBJ part'}
            <input
              type="file"
              accept=".stl,.obj,.ply,.3mf"
              className="hidden"
              onChange={(e) => void onPick(e.target.files?.[0] ?? null)}
            />
          </label>

          <MeshPreview geom={geom} />

          {metrics && (
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 rounded-lg border border-white/10 bg-[#0a0a0d] p-3 text-[11.5px] sm:grid-cols-4">
              <Stat label="faces" value={fmt(metrics.faces, 0)} />
              <Stat label="vertices" value={fmt(metrics.vertices, 0)} />
              <Stat label="volume" value={`${fmt(metrics.volume_mm3)} mm³`} />
              <Stat label="area" value={`${fmt(metrics.surface_area_mm2)} mm²`} />
              <Stat label="dimensions"
                    value={metrics.dimensions_mm.map((d) => fmt(d, 0)).join(' × ')} />
              <Stat label="thinnest edge" value={`${fmt(metrics.min_edge_mm, 2)} mm`} />
              <Stat label="overhang faces" value={fmt(metrics.overhang_faces, 0)} />
              <Stat label="thin regions" value={fmt(metrics.thin_regions, 0)} />
            </div>
          )}
        </div>

        <aside className="flex flex-col gap-3">
          <div className="rounded-lg border border-white/10 bg-[#0a0a0d] p-3">
            <label htmlFor="parts-pass" className="mb-2 flex items-center gap-1.5 text-[11.5px] font-semibold uppercase tracking-wide text-zinc-500">
              <Wand2 size={12} /> Pass
            </label>
            {/* `htmlFor`/`id` rather than a wrapping label: the heading used to
                be a plain <div>, so this select - the one control on the page
                that chooses what the optimiser does - had no accessible name
                and announced only as "combo box". */}
            <select
              id="parts-pass"
              value={pass}
              onChange={(e) => setPass(e.target.value)}
              className="w-full rounded-md border border-white/10 bg-[#121216] px-2 py-1.5 text-xs text-zinc-200"
            >
              {passes.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}{p.modifies ? '' : ' (measure only)'}
                </option>
              ))}
            </select>            {passErr && !passes.length && (
              <p className="mt-1.5 text-[11px] text-amber-300/90">
                couldn&apos;t load the pass list from the gateway ({passErr.slice(0, 120)}).
              </p>
            )}
            {chosen && (
              <p className="mt-1.5 text-[11px] leading-snug text-zinc-500">
                {chosen.description}
              </p>
            )}
            {pass === 'decimate' && (
              <label className="mt-2 block text-[11px] text-zinc-500">
                triangle budget
                <input
                  type="number"
                  min={4}
                  value={budget}
                  onChange={(e) => setBudget(Math.max(4, Number(e.target.value) || 4))}
                  className="mt-1 w-full rounded-md border border-white/10 bg-[#121216] px-2 py-1 font-mono text-[11.5px] text-zinc-200"
                />
              </label>
            )}
            <div className="mt-3 flex gap-1.5">
              <button
                type="button"
                disabled={!file || busy}
                onClick={() => void runLoop()}
                className="flex-1 rounded-md bg-indigo-500/90 px-2 py-1.5 text-[11.5px] font-semibold text-white disabled:opacity-40"
              >
                {running ? 'optimising…' : 'Optimise until it stops helping'}
              </button>
              {running && (
                <button
                  type="button"
                  onClick={() => { stop.current = true; }}
                  title="Stop after the current step"
                  className="rounded-md border border-white/15 px-2 py-1.5 text-[11.5px] text-zinc-300"
                >
                  <RotateCcw size={12} />
                </button>
              )}
            </div>
          </div>

          {objective !== null && (
            <div className="rounded-lg border border-white/10 bg-[#0a0a0d] p-3">
              <div className="mb-1 flex items-center gap-1.5 text-[11.5px] font-semibold uppercase tracking-wide text-zinc-500">
                <Gauge size={12} /> Objective
              </div>
              <div className="font-mono text-lg text-zinc-100">
                {fmt(objective, 3)}
              </div>
              <p className="mt-1 text-[11px] leading-snug text-zinc-500">
                Lower is better. Triangle count, inverted solids, thin
                features and overhangs all feed it.
              </p>
            </div>
          )}

          {history.length > 0 && (
            <div className="rounded-lg border border-white/10 bg-[#0a0a0d] p-3">
              <div className="mb-1.5 text-[11.5px] font-semibold uppercase tracking-wide text-zinc-500">
                Loop
              </div>
              <ol className="space-y-1 text-[11.5px]">
                {history.map((h, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <span className={h.kept ? 'text-emerald-400' : 'text-zinc-500'}>
                      {h.kept ? '✓' : '·'}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-zinc-300">
                      {h.pass} → {fmt(h.faces, 0)} faces
                    </span>
                    <span className="font-mono text-zinc-500">
                      {fmt(h.objective, 2)}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          )}

          {error && (
            <div role="alert"
                 className="rounded-lg border border-rose-500/40 bg-rose-500/10 p-2.5 text-[11.5px] text-rose-200">
              {error}
            </div>
          )}
        </aside>
          </div>
        </div>
      </main>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-zinc-500">{label}</div>
      <div className="font-mono text-zinc-200">{value}</div>
    </div>
  );
}

/**
 * Wireframe preview from the returned buffers.
 *
 * Deliberately NOT three.js: that is a ~600kB dependency for one view, and the
 * first thing anyone wants to see is whether the mesh is the right shape. The
 * buffers are already an index array, so projecting the edges is enough.
 */
function MeshPreview({ geom }: { geom: Geometry | null }) {
  const ref = useRef<HTMLCanvasElement | null>(null);

  // Draw on a rAF rather than in an effect body, so the canvas is sized after
  // layout and is not repainted on every unrelated state change.
  useMemo(() => {
    let raf = 0;
    const draw = () => {
      const cv = ref.current;
      if (!cv || !geom || geom.indices.length === 0) return;
      const dpr = Math.min(2, globalThis.devicePixelRatio || 1);
      const w = cv.clientWidth, h = cv.clientHeight;
      if (!w || !h) { raf = requestAnimationFrame(draw); return; }
      cv.width = w * dpr; cv.height = h * dpr;
      const ctx = cv.getContext('2d');
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      const p = geom.positions;
      let minX = Infinity, minY = Infinity, minZ = Infinity;
      let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
      for (let i = 0; i < p.length; i += 3) {
        if (p[i] < minX) minX = p[i]; if (p[i] > maxX) maxX = p[i];
        if (p[i + 1] < minY) minY = p[i + 1]; if (p[i + 1] > maxY) maxY = p[i + 1];
        if (p[i + 2] < minZ) minZ = p[i + 2]; if (p[i + 2] > maxZ) maxZ = p[i + 2];
      }
      const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2, cz = (minZ + maxZ) / 2;
      const span = Math.max(maxX - minX, maxY - minY, maxZ - minZ) || 1;
      const s = (Math.min(w, h) * 0.42) / span;
      const yaw = 0.7, pit = 0.45;
      const cy_ = Math.cos(yaw), sy = Math.sin(yaw);
      const cp = Math.cos(pit), sp = Math.sin(pit);
      const proj = (x: number, y: number, z: number): [number, number] => {
        const dx = x - cx, dy = y - cy, dz = z - cz;
        const x1 = dx * cy_ - dz * sy;
        const z1 = dx * sy + dz * cy_;
        const y2 = dy * cp - z1 * sp;
        return [w / 2 + x1 * s, h / 2 - y2 * s];
      };

      ctx.strokeStyle = 'rgba(129,140,248,0.55)';
      ctx.lineWidth = 0.6;
      ctx.beginPath();
      const idx = geom.indices;
      const step = Math.max(3, Math.floor(idx.length / 3 / 4000) * 3);
      for (let i = 0; i < idx.length; i += step) {
        for (let k = 0; k < 3; k++) {
          const a = idx[i + k] * 3, b = idx[i + ((k + 1) % 3)] * 3;
          if (a >= p.length || b >= p.length) continue;
          const [ax, ay] = proj(p[a], p[a + 1], p[a + 2]);
          const [bx, by] = proj(p[b], p[b + 1], p[b + 2]);
          ctx.moveTo(ax, ay); ctx.lineTo(bx, by);
        }
      }
      ctx.stroke();
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [geom]);

  return (
    <div className="min-h-[280px] flex-1 rounded-lg border border-white/10 bg-[#0a0a0d]">
      <canvas ref={ref} className="h-full min-h-[280px] w-full" />
      {!geom && (
        <p className="p-4 text-center text-xs text-zinc-500">
          Upload a part to render it.
        </p>
      )}
    </div>
  );
}