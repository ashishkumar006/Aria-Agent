import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle, ArrowDownToLine, Box, Check, Circle, Copy, Download, Gauge,
  History, Layers, Maximize2, Pencil, Play, Plus, Search, Sparkles, Square,
  Trash2, Upload, Wand2, X,
} from 'lucide-react';
import { toast } from 'sonner';
import { Rail, SkipLink, TopBar } from '../components/ui';
import { useMainId } from '../components/KeepAlive';
import { Pane, PaneStrip, usePaneMode } from '../lib/panes';
import PartViewer from '../components/PartViewer';
import BlenderWorkspace from '../components/BlenderWorkspace';
import {
  api, type PartCapabilities, type PartDetail, type PartGeometry, type PartIssue,
  type PartJob, type PartPassInfo, type PartSummary, type PartVersion, type PartView,
} from '../api';

type Tab = 'analyze' | 'optimize' | 'history' | 'aria';

const UNIT_SCALE = { mm: 1, cm: 10, in: 25.4 } as const;
const TERMINAL_JOB = new Set(['done', 'cancelled', 'error']);

function fmt(n: number | null | undefined, digits = 1): string {
  return typeof n === 'number' && Number.isFinite(n)
    ? n.toLocaleString(undefined, {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    })
    : '—';
}

function when(seconds?: number): string {
  if (!seconds) return '';
  return new Date(seconds * 1000).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function VerdictPill({ verdict }: { verdict?: PartDetail['verdict'] }) {
  const status = verdict?.status || 'unknown';
  const cls = status === 'ok' ? 'border-emerald-400/40 bg-emerald-400/10 text-emerald-200'
    : status === 'warn' ? 'border-amber-400/40 bg-amber-400/10 text-amber-200'
      : status === 'unusable' ? 'border-rose-400/40 bg-rose-400/10 text-rose-200'
        : 'border-white/15 bg-white/5 text-zinc-400';
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] font-semibold capitalize ${cls}`}>
      {status === 'ok' ? <Check size={10} /> : status === 'unusable'
        ? <AlertTriangle size={10} /> : <Circle size={8} />}
      {status}
      {typeof verdict?.printable === 'boolean' && (
        <span className="font-normal opacity-70">
          · {verdict.printable ? 'printable' : 'not printable'}
        </span>
      )}
    </span>
  );
}

function Stat({ label, value, mono = true }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`truncate text-[12px] text-zinc-200 ${mono ? 'font-mono' : ''}`}
        title={value}>{value}</div>
    </div>
  );
}

function Tab({ active, onClick, icon, children }: {
  active: boolean; onClick: () => void; icon: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <button type="button" onClick={onClick} aria-pressed={active}
      className={`flex items-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] font-semibold transition ${
        active ? 'bg-violet-400/15 text-violet-100' : 'text-zinc-500 hover:bg-white/5 hover:text-zinc-200'
      }`}>
      {icon}{children}
    </button>
  );
}

function IssueRow({ issue }: { issue: PartIssue }) {
  const tone = issue.severity === 'error' ? 'text-rose-300'
    : issue.severity === 'warn' ? 'text-amber-300' : 'text-zinc-400';
  return (
    <li className="rounded-md border border-white/8 bg-black/20 p-2">
      <div className={`flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-wide ${tone}`}>
        {issue.severity === 'error' ? <AlertTriangle size={11} /> : <Circle size={8} />}
        {issue.code.replaceAll('_', ' ')}
      </div>
      <p className="mt-1 text-[11.5px] leading-snug text-zinc-300">{issue.message}</p>
    </li>
  );
}

function SmallButton({ children, onClick, disabled, tone = 'ghost', title }: {
  children: React.ReactNode; onClick?: () => void; disabled?: boolean;
  tone?: 'ghost' | 'primary' | 'danger'; title?: string;
}) {
  const cls = tone === 'primary'
    ? 'border-violet-400/40 bg-violet-400/15 text-violet-100 hover:bg-violet-400/25'
    : tone === 'danger'
      ? 'border-rose-400/35 bg-rose-400/10 text-rose-200 hover:bg-rose-400/20'
      : 'border-white/12 bg-white/[0.03] text-zinc-300 hover:border-white/25 hover:text-zinc-100';
  return (
    <button type="button" title={title} disabled={disabled} onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-40 ${cls}`}>
      {children}
    </button>
  );
}

export default function Parts() {
  const mainId = useMainId();
  const sidebarMode = usePaneMode('parts.sidebar');
  const [parts, setParts] = useState<PartSummary[]>([]);
  const [stats, setStats] = useState<Record<string, number>>({});
  const [selectedId, setSelectedId] = useState('');
  const [detail, setDetail] = useState<PartDetail | null>(null);
  const [geometry, setGeometry] = useState<PartGeometry | null>(null);
  const [compareGeom, setCompareGeom] = useState<PartGeometry | null>(null);
  const [compareOn, setCompareOn] = useState(false);
  const [previewVersion, setPreviewVersion] = useState('');
  const [view, setView] = useState<PartView>({ preset: 'iso', mode: 'shaded', overlay: 'none' });
  const [resetToken, setResetToken] = useState(0);
  const [search, setSearch] = useState('');
  const [tab, setTab] = useState<Tab>('analyze');
  const [workspaceMode, setWorkspaceMode] = useState<'viewer' | 'blender'>('viewer');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [geomBusy, setGeomBusy] = useState(false);
  const [passes, setPasses] = useState<PartPassInfo[]>([]);
  const [caps, setCaps] = useState<PartCapabilities | null>(null);
  const [pass, setPass] = useState('decimate');
  const [targetFaces, setTargetFaces] = useState(20_000);
  const [iterations, setIterations] = useState(1);
  const [strength, setStrength] = useState(0.4);
  const [overhangAngle, setOverhangAngle] = useState(45);
  const [maxSteps, setMaxSteps] = useState(6);
  const [job, setJob] = useState<PartJob | null>(null);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importName, setImportName] = useState('');
  const [importUnits, setImportUnits] = useState<keyof typeof UNIT_SCALE>('mm');
  const [autoRepair, setAutoRepair] = useState(true);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [runPrompt, setRunPrompt] = useState('Inspect this part and recommend the next step.');
  const [running, setRunning] = useState(false);
  const [runLog, setRunLog] = useState<string[]>([]);
  const [runAnswer, setRunAnswer] = useState('');
  const [runError, setRunError] = useState('');
  const [runSession, setRunSession] = useState('');
  const runAbort = useRef<AbortController | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const importFocus = useRef<HTMLInputElement | null>(null);

  const loadList = useCallback(async () => {
    const d = await api.partsList();
    setParts(d.parts || []);
    setStats(d.stats || {});
    return d.parts || [];
  }, []);

  const applyView = useCallback((patch: Partial<PartView>, persist = true) => {
    setView((prev) => {
      const next = { ...prev, ...patch };
      if (persist && detail) {
        void api.partSetView(detail.id, next).catch(() => { /* view is a preference */ });
      }
      return next;
    });
  }, [detail]);

  const loadDetail = useCallback(async (id: string, version?: string) => {
    const d = await api.partDetail(id);
    const part = d.part;
    setDetail(part);
    setSelectedId(id);
    setPreviewVersion(version || part.current_version || '');
    const nextView = part.view || {};
    setView({
      preset: nextView.preset || 'iso',
      mode: nextView.mode === 'wireframe' ? 'wireframe' : 'shaded',
      overlay: nextView.overlay || 'none',
    });
    setTargetFaces(Math.max(4, Math.min(20_000, (part.faces || 20_000) - 1)));
    window.history.replaceState(null, '', `/parts${id ? `?id=${encodeURIComponent(id)}` : ''}`);
    return part;
  }, []);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const [list, p, c] = await Promise.all([
          loadList(), api.partPasses().catch(() => ({ passes: [] })),
          api.partCapabilities().catch(() => null),
        ]);
        if (!live) return;
        setPasses(p.passes || []);
        setCaps(c);
        const wanted = new URLSearchParams(window.location.search).get('id') || '';
        const id = list.some((x) => x.id === wanted) ? wanted : (list[0]?.id || '');
        if (id) await loadDetail(id);
      } catch (e) {
        if (live) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { live = false; };
  }, [loadDetail, loadList]);

  // The viewer follows the selected version. Geometry is fetched from the
  // gateway, so the browser never holds the source file.
  useEffect(() => {
    if (!detail) return;
    const version = previewVersion || detail.current_version || '';
    if (!version) return;
    let live = true;
    setGeomBusy(true);
    setGeometry(null);
    api.partGeometry(detail.id, version, 140_000)
      .then((d) => { if (live) setGeometry(d.geometry); })
      .catch((e) => { if (live) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (live) setGeomBusy(false); });
    return () => { live = false; };
    // Depend on the RESOLVED version, not the raw preview slot: on first
    // open previewVersion goes '' -> current in two renders, which used to
    // fire two full decimations of the same large mesh.
  }, [detail?.id, previewVersion || detail?.current_version]);

  useEffect(() => {
    if (!compareOn || !detail || !previewVersion
      || previewVersion === detail.current_version) {
      setCompareGeom(null);
      return;
    }
    let live = true;
    api.partGeometry(detail.id, detail.current_version || '', 90_000)
      .then((d) => { if (live) setCompareGeom(d.geometry); })
      .catch(() => { if (live) setCompareGeom(null); });
    return () => { live = false; };
  }, [compareOn, detail?.id, detail?.current_version, previewVersion]);

  // Recover a loop that is already running (or last ran) for this part. The
  // job lives server-side, so a refresh or a section switch must not make it
  // look like nothing is happening.
  useEffect(() => {
    if (!detail) return;
    let live = true;
    api.partJobs(detail.id)
      .then((d) => { if (live) setJob(d.jobs?.[0] || null); })
      .catch(() => { /* the job list is a convenience */ });
    return () => { live = false; };
  }, [detail?.id]);

  // Poll a running loop. The job survives navigation; this view only mirrors it.
  useEffect(() => {
    if (!job || TERMINAL_JOB.has(job.status)) return;
    const selected = detail?.id;
    let live = true;
    const tick = async () => {
      try {
        const d = await api.partJob(job.id);
        if (!live || !d.job) return;
        setJob(d.job);
        if (TERMINAL_JOB.has(d.job.status)) {
          await loadList();
          // Only reload the part the user is actually looking at. A finished
          // background job must not switch the pane back to its own part.
          if (selected === job.part_id) await loadDetail(job.part_id);
          if (d.job.status === 'error') setError(d.job.error || 'the loop failed');
          else toast.success(d.job.note || 'optimisation finished');
        }
      } catch { /* the next tick retries */ }
    };
    const t = window.setInterval(() => void tick(), 1800);
    return () => { live = false; window.clearInterval(t); };
  }, [job?.id, job?.status, job?.part_id, detail?.id, loadDetail, loadList]);

  useEffect(() => {
    if (importFile) importFocus.current?.focus();
  }, [importFile]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return parts;
    return parts.filter((p) => p.name.toLowerCase().includes(q)
      || (p.tags || []).some((t) => t.toLowerCase().includes(q))
      || (p.source?.filename || '').toLowerCase().includes(q));
  }, [parts, search]);

  const activeVersionId = previewVersion || detail?.current_version || '';
  const activeVersion: PartVersion | undefined = useMemo(() => {
    if (!detail) return undefined;
    // The current version is the only one that carries `report` (slicer
    // result, repair steps); prefer it over the stripped history entry.
    if (detail.current?.id === activeVersionId) return detail.current;
    return detail.versions?.find((v) => v.id === activeVersionId)
      || detail.current || undefined;
  }, [detail, activeVersionId]);
  const activeVerdict = activeVersion?.verdict || detail?.verdict;
  const activeMetrics = activeVersion?.metrics || {};
  const activeIssues = activeVersion?.issues || detail?.issues || [];
  const isCurrent = !!detail && activeVersionId === detail.current_version;
  const chosenPass = passes.find((p) => p.name === pass);
  const accept = (caps?.backends?.trimesh?.available ?? true)
    ? '.stl,.obj,.ply,.3mf,.off,.glb,.gltf,.dae,.amf'
    : '.stl,.obj';

  const refresh = useCallback(async (id: string, version?: string) => {
    await loadDetail(id, version);
    await loadList();
  }, [loadDetail, loadList]);

  const onPickFile = (f: File | null) => {
    if (!f) return;
    setError('');
    setImportFile(f);
    setImportName(f.name.replace(/\.[^.]+$/, ''));
    setImportUnits('mm');
    setAutoRepair(true);
  };

  const doImport = useCallback(async () => {
    if (!importFile) return;
    setBusy('import');
    setError('');
    try {
      const d = await api.partImport(importFile, {
        name: importName || importFile.name.replace(/\.[^.]+$/, ''),
        scale: UNIT_SCALE[importUnits],
        autoRepair,
      });
      if (!d.part) throw new Error(d.error || 'import failed');
      setImportFile(null);
      await loadList();
      await loadDetail(d.part.id);
      toast.success(`Imported ${d.part.name}`);
      setTab('analyze');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [autoRepair, importFile, importName, importUnits, loadDetail, loadList]);

  const selectPart = useCallback(async (id: string) => {
    setCompareOn(false);
    setConfirmDelete(false);
    setRenaming(false);
    setWorkspaceMode('viewer');
    setError('');
    try {
      await loadDetail(id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [loadDetail]);

  const runAnalyse = useCallback(async (repair = false, slicer = false) => {
    if (!detail || !activeVersionId) return;
    setBusy(repair ? 'repair' : slicer ? 'slicer' : 'analyse');
    setError('');
    try {
      if (repair) {
        const r = await api.partRepair(detail.id, { version: activeVersionId });
        if (r.error) throw new Error(r.error);
        await refresh(detail.id, r.part?.current_version);
        toast.success(r.changed ? 'Repaired and saved as a new version'
          : 'No geometry change was needed');
        setTab('history');
      } else {
        const r = await api.partAnalyse(detail.id, {
          version: activeVersionId, slicer,
        });
        if (r.error) throw new Error(r.error);
        await refresh(detail.id, activeVersionId);
        toast.success(slicer ? 'Slicer probe finished' : 'Analysis updated');
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [activeVersionId, detail, refresh]);

  const passParams = useCallback((): Record<string, unknown> => {
    if (pass === 'decimate') return { target_faces: Math.max(4, targetFaces) };
    if (pass === 'smooth') return { iterations: Math.max(1, iterations), strength };
    if (pass === 'detect_overhang') return { overhang_angle_deg: overhangAngle };
    return {};
  }, [iterations, overhangAngle, pass, strength, targetFaces]);

  const runOnePass = useCallback(async () => {
    if (!detail || !activeVersionId) return;
    setBusy('optimise');
    setError('');
    try {
      const r = await api.partOptimise(detail.id, {
        version: activeVersionId, pass, params: passParams(),
      });
      if (r.error) throw new Error(r.error);
      await refresh(detail.id, r.part?.current_version);
      if (!r.changed) toast.info('The pass made no change');
      else if (r.improved) toast.success('Kept: the objective improved');
      else toast.info('Reverted: the result was worse, current version unchanged');
      setTab('history');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [activeVersionId, detail, pass, passParams, refresh]);

  const startLoop = useCallback(async () => {
    if (!detail || !activeVersionId) return;
    setError('');
    try {
      const r = await api.partOptimiseLoop(detail.id, {
        version: activeVersionId, pass, params: passParams(), max_steps: maxSteps,
      });
      if (!r.job) throw new Error(r.error || 'loop could not start');
      setJob(r.job);
      toast.success('Optimisation loop started');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [activeVersionId, detail, maxSteps, pass, passParams]);

  const cancelJob = useCallback(async () => {
    if (!job) return;
    try {
      const d = await api.partCancelJob(job.id);
      if (d.job) setJob(d.job);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [job]);

  const restore = useCallback(async (version: string) => {
    if (!detail) return;
    setBusy('restore');
    try {
      const d = await api.partRestore(detail.id, version);
      if (d.error) throw new Error(d.error);
      await refresh(detail.id, version);
      toast.success(`Restored ${version}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [detail, refresh]);

  const download = useCallback(async (version?: string) => {
    if (!detail) return;
    try {
      const blob = await api.partDownload(detail.id, version);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = activeVersion?.filename || `${detail.name.replace(/\s+/g, '-')}.stl`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [activeVersion?.filename, detail]);

  const saveRename = useCallback(async () => {
    if (!detail) return;
    const name = renameValue.trim();
    if (!name || name === detail.name) { setRenaming(false); return; }
    try {
      const d = await api.partUpdate(detail.id, { name });
      if (d.error) throw new Error(d.error);
      await refresh(detail.id, activeVersionId);
      setRenaming(false);
      toast.success('Renamed');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [activeVersionId, detail, refresh, renameValue]);

  const duplicate = useCallback(async () => {
    if (!detail) return;
    try {
      const d = await api.partDuplicate(detail.id);
      if (d.error || !d.part) throw new Error(d.error || 'duplicate failed');
      await loadList();
      await selectPart(d.part.id);
      toast.success('Duplicated');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [detail, loadList, selectPart]);

  const removePart = useCallback(async () => {
    if (!detail) return;
    setBusy('delete');
    try {
      const d = await api.partDelete(detail.id);
      if (d.error) throw new Error(d.error);
      setConfirmDelete(false);
      setWorkspaceMode('viewer');
      setDetail(null);
      setGeometry(null);
      setJob(null);
      setSelectedId('');
      const list = await loadList();
      setPreviewVersion('');
      window.history.replaceState(null, '', '/parts');
      if (list[0]) await selectPart(list[0].id);
      toast.success('Part deleted');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [detail, loadList, selectPart]);

  const startRun = useCallback(async () => {
    if (!detail || !activeVersionId) return;
    if (!runPrompt.trim()) return;
    runAbort.current?.abort();
    const ac = new AbortController();
    runAbort.current = ac;
    setRunning(true);
    setRunLog([]);
    setRunAnswer('');
    setRunError('');
    setRunSession('');
    setTab('aria');
    try {
      for await (const f of api.chat(runPrompt.trim(), undefined, ac.signal, {
        runKind: 'parts', partId: detail.id, versionId: activeVersionId,
      })) {
        if (f.type === 'started' && f.session_id) setRunSession(String(f.session_id));
        else if (f.type === 'log' && f.text) {
          // The DAG's log frames also carry the prompt and the FINAL block;
          // repeating those next to the answer reads like a duplicated reply.
          const lines = String(f.text).split('\n')
            .map((line) => line.trim())
            .filter((line) => line && line.length < 300
              && !line.startsWith('FINAL:')
              && !line.includes('[PART CONTEXT')
              && !line.startsWith('part id:') && !line.startsWith('name:')
              && !line.startsWith('faces:') && !line.startsWith('dimensions mm:')
              && !line.startsWith('volume mm3:') && !line.startsWith('verdict:')
              && !line.startsWith('versions:') && !line.startsWith('current version:')
              && !line.startsWith('source:'));
          if (lines.length) setRunLog((prev) => [...prev, ...lines].slice(-40));
        } else if (f.type === 'done') {
          setRunAnswer(String(f.answer || ''));
          if (f.session_id) setRunSession(String(f.session_id));
        } else if (f.type === 'error') {
          setRunError(String(f.text || 'the run failed'));
        }
      }
    } catch (e) {
      if (!ac.signal.aborted) setRunError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
      await loadDetail(detail.id);
      await loadList();
    }
  }, [activeVersionId, detail, loadDetail, loadList, runPrompt]);

  const stopRun = useCallback(async () => {
    runAbort.current?.abort();
    setRunning(false);
    if (runSession) {
      try { await api.cancelRun({ session_id: runSession }); } catch { /* best effort */ }
    }
  }, [runSession]);

  const importPanel = importFile ? (
    <div className="mx-auto max-w-2xl rounded-xl border border-white/12 bg-surface-1 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="m-0 text-[15px] font-semibold text-zinc-100">Import part</h2>
          <p className="mt-1 text-[11.5px] text-zinc-500">
            {importFile.name} · {(importFile.size / 1024 / 1024).toFixed(2)} MB
          </p>
        </div>
        <button type="button" aria-label="Cancel import" onClick={() => setImportFile(null)}
          className="rounded p-1 text-zinc-500 hover:bg-white/5 hover:text-zinc-200">
          <X size={14} />
        </button>
      </div>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-[10.5px] uppercase tracking-wide text-zinc-500">
          Name
          <input ref={importFocus} value={importName}
            onChange={(e) => setImportName(e.target.value)}
            className="rounded-md border border-white/12 bg-black/30 px-2 py-1.5 text-[12px] normal-case tracking-normal text-zinc-200 outline-none focus:border-violet-400" />
        </label>
        <label className="flex flex-col gap-1 text-[10.5px] uppercase tracking-wide text-zinc-500">
          Model units
          <select value={importUnits}
            onChange={(e) => setImportUnits(e.target.value as keyof typeof UNIT_SCALE)}
            className="rounded-md border border-white/12 bg-black/30 px-2 py-1.5 text-[12px] normal-case tracking-normal text-zinc-200 outline-none focus:border-violet-400">
            <option value="mm">millimetres (mm)</option>
            <option value="cm">centimetres (cm)</option>
            <option value="in">inches (in)</option>
          </select>
        </label>
      </div>
      <label className="mt-3 flex items-center gap-2 text-[11.5px] text-zinc-300">
        <input type="checkbox" checked={autoRepair}
          onChange={(e) => setAutoRepair(e.target.checked)}
          className="h-3.5 w-3.5 accent-violet-400" />
        Auto-repair the upload into a usable second version
      </label>
      <p className="mt-2 text-[10.5px] leading-snug text-zinc-500">
        Binary STL has no shared vertices, so a clean export often looks
        watertight-broken until welded. The original stays as v1; repair adds a
        version, never overwrites.
      </p>
      {error && <p role="alert" className="mt-2 rounded border border-rose-400/30 bg-rose-400/10 p-2 text-[11.5px] text-rose-200">{error}</p>}
      <div className="mt-4 flex items-center justify-end gap-2">
        <SmallButton onClick={() => setImportFile(null)}>Cancel</SmallButton>
        <SmallButton tone="primary" disabled={busy === 'import'} onClick={() => void doImport()}>
          <Upload size={12} />{busy === 'import' ? 'Importing…' : 'Import & measure'}
        </SmallButton>
      </div>
    </div>
  ) : null;

  const sidebar = sidebarMode === 'collapsed'
    ? <PaneStrip id="parts.sidebar" label="Saved parts list" />
    : (
    <aside aria-label="Saved parts"
      className="flex max-h-56 w-full shrink-0 flex-col border-b border-white/10 bg-[#0b0b0f] lg:max-h-none lg:w-60 lg:border-b-0 lg:border-r">
      <div className="flex items-center justify-between gap-2 px-3 pt-3">
        <div>
          <div className="text-[11.5px] font-semibold uppercase tracking-wide text-zinc-500">Parts</div>
          <Pane id="parts.stats">
          <div className="text-[10.5px] text-zinc-600">
            {stats.parts ?? parts.length} saved · {stats.versions ?? 0} versions
          </div>
          </Pane>
        </div>
        <button type="button" aria-label="Import part" onClick={() => fileInput.current?.click()}
          className="rounded-md border border-violet-400/40 bg-violet-400/15 p-1.5 text-violet-100 hover:bg-violet-400/25">
          <Plus size={14} />
        </button>
      </div>
      <Pane id="parts.search">
      <div className="relative px-3 py-2">
        <Search size={12} className="absolute left-5 top-1/2 -translate-y-1/2 text-zinc-600" />
        <input value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder="Search parts" aria-label="Search parts"
          className="w-full rounded-md border border-white/10 bg-black/30 py-1 pl-6 pr-2 text-[11.5px] text-zinc-200 outline-none focus:border-violet-400" />
      </div>
      </Pane>
      <div className="min-h-0 flex-1 overflow-auto px-2 pb-3">
        {filtered.length === 0 ? (
          <p className="px-2 py-4 text-[11px] text-zinc-600">
            {parts.length ? 'No part matches that search.' : 'No saved parts yet. Import a mesh to start.'}
          </p>
        ) : filtered.map((p) => {
          const selected = p.id === selectedId;
          return (
            <button type="button" key={p.id} onClick={() => void selectPart(p.id)}
              className={`mb-1 flex w-full items-start gap-2 rounded-md border px-2 py-1.5 text-left transition ${
                selected ? 'border-violet-400/40 bg-violet-400/10' : 'border-transparent hover:border-white/10 hover:bg-white/[0.03]'
              }`}>
              <span className={`mt-0.5 flex h-6 w-6 flex-none items-center justify-center rounded border ${
                p.verdict?.status === 'unusable' ? 'border-rose-400/30 text-rose-300'
                  : p.verdict?.status === 'warn' ? 'border-amber-400/30 text-amber-300'
                    : 'border-emerald-400/30 text-emerald-300'
              }`}><Box size={13} /></span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[11.5px] font-semibold text-zinc-200">{p.name}</span>
                <span className="block truncate text-[10px] text-zinc-500">
                  {p.format?.toUpperCase()} · {fmt(p.faces, 0)} faces · {p.current_version}
                </span>
              </span>
              {p.kept === false && <span className="text-[9px] text-zinc-600">rev</span>}
            </button>
          );
        })}
      </div>
    </aside>
  );

  const versionSelect = detail && (
    <label className="flex items-center gap-1 text-[10.5px] text-zinc-500">
      <History size={11} />
      <select value={activeVersionId}
        onChange={(e) => { setPreviewVersion(e.target.value); setCompareOn(false); }}
        aria-label="View version"
        className="rounded border border-white/12 bg-black/30 px-1.5 py-0.5 text-[11px] text-zinc-200 outline-none focus:border-violet-400">
        {(detail.versions || []).slice().reverse().map((v) => (
          <option key={v.id} value={v.id}>
            {v.id} · {v.op}{v.kept === false ? ' (reverted)' : ''}
          </option>
        ))}
      </select>
    </label>
  );

  const header = detail && (
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          {renaming ? (
            <input autoFocus value={renameValue} onChange={(e) => setRenameValue(e.target.value)}
              onBlur={() => void saveRename()} onKeyDown={(e) => {
                if (e.key === 'Enter') void saveRename();
                if (e.key === 'Escape') setRenaming(false);
              }}
              aria-label="Part name"
              className="w-64 rounded border border-violet-400/40 bg-black/30 px-2 py-1 text-[15px] font-semibold text-zinc-100 outline-none" />
          ) : (
            <h1 className="m-0 truncate text-[16px] font-semibold tracking-tight text-zinc-100">{detail.name}</h1>
          )}
          <button type="button" aria-label="Rename part"
            onClick={() => { setRenameValue(detail.name); setRenaming(true); }}
            className="rounded p-1 text-zinc-600 hover:bg-white/5 hover:text-zinc-300">
            <Pencil size={11} />
          </button>
          <VerdictPill verdict={activeVerdict} />
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-[10.5px] text-zinc-500">
          <span>{detail.source?.filename}</span>
          <span>·</span>
          {versionSelect}
          {!isCurrent && <span className="text-amber-300">viewing a non-current version</span>}
        </div>
        {(detail.tags || []).length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {(detail.tags || []).map((t) => (
              <span key={t} className="rounded-full border border-white/10 px-1.5 py-0.5 text-[10px] text-zinc-400">{t}</span>
            ))}
          </div>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="inline-flex overflow-hidden rounded-md border border-white/12">
          <button type="button" onClick={() => setWorkspaceMode('viewer')}
            aria-pressed={workspaceMode === 'viewer'}
            className={`px-2 py-1 text-[10.5px] font-semibold ${workspaceMode === 'viewer' ? 'bg-violet-400/20 text-violet-100' : 'text-zinc-500 hover:text-zinc-200'}`}>
            Viewport
          </button>
          <button type="button" onClick={() => setWorkspaceMode('blender')}
            aria-pressed={workspaceMode === 'blender'}
            className={`px-2 py-1 text-[10.5px] font-semibold ${workspaceMode === 'blender' ? 'bg-violet-400/20 text-violet-100' : 'text-zinc-500 hover:text-zinc-200'}`}>
            Blender
          </button>
        </span>
        <SmallButton onClick={() => void download()} disabled={!activeVersionId} title="Download this version">
          <Download size={12} />Download
        </SmallButton>
        <SmallButton onClick={() => void duplicate()} title="Duplicate this part">
          <Copy size={12} />
        </SmallButton>
        {confirmDelete ? (
          <span className="inline-flex items-center gap-1 rounded-md border border-rose-400/35 bg-rose-400/10 px-1.5 py-1 text-[10.5px] text-rose-200">
            delete all versions?
            <button type="button" onClick={() => void removePart()} className="font-semibold underline">yes</button>
            <button type="button" onClick={() => setConfirmDelete(false)} className="text-zinc-400">no</button>
          </span>
        ) : (
          <SmallButton tone="danger" onClick={() => setConfirmDelete(true)} title="Delete part">
            <Trash2 size={12} />
          </SmallButton>
        )}
      </div>
    </div>
  );

  const viewer = detail && (
    <div className="flex min-h-0 flex-col gap-2">
      <Pane id="parts.viewbar">
      <div className="flex flex-wrap items-center gap-1.5 rounded-lg border border-white/10 bg-black/20 px-2 py-1.5">
        <span className="text-[10px] uppercase tracking-wide text-zinc-600">View</span>
        {(['iso', 'front', 'right', 'top'] as const).map((preset) => (
          <button type="button" key={preset}
            onClick={() => applyView({ preset })}
            aria-pressed={view.preset === preset}
            className={`rounded px-1.5 py-0.5 text-[10.5px] ${view.preset === preset ? 'bg-violet-400/20 text-violet-100' : 'text-zinc-500 hover:text-zinc-200'}`}>
            {preset}
          </button>
        ))}
        <span className="mx-1 h-3 w-px bg-white/10" />
        <button type="button" onClick={() => applyView({ mode: view.mode === 'wireframe' ? 'shaded' : 'wireframe' })}
          className={`rounded px-1.5 py-0.5 text-[10.5px] ${view.mode === 'wireframe' ? 'bg-violet-400/20 text-violet-100' : 'text-zinc-500 hover:text-zinc-200'}`}>
          wireframe
        </button>
        <select aria-label="Overlay" value={view.overlay || 'none'}
          onChange={(e) => applyView({ overlay: e.target.value })}
          className="rounded border border-white/10 bg-black/30 px-1 py-0.5 text-[10.5px] text-zinc-300 outline-none">
          <option value="none">no overlay</option>
          <option value="overhang">overhangs</option>
          <option value="thin_walls">thin edges</option>
          <option value="issues">all issues</option>
        </select>
        <span className="ml-auto flex items-center gap-1">
          <label className={`inline-flex items-center gap-1 text-[10.5px] ${isCurrent ? 'text-zinc-600' : 'text-zinc-400'}`}>
            <input type="checkbox" disabled={isCurrent} checked={compareOn}
              onChange={(e) => setCompareOn(e.target.checked)}
              className="h-3 w-3 accent-violet-400" />
            compare current
          </label>
          <button type="button" title="Reset camera" aria-label="Reset camera"
            onClick={() => setResetToken((n) => n + 1)}
            className="rounded p-1 text-zinc-500 hover:bg-white/5 hover:text-zinc-200">
            <Maximize2 size={12} />
          </button>
        </span>
      </div>
      </Pane>
      <div className="relative min-h-[260px] flex-1 overflow-hidden rounded-lg border border-white/10 bg-[#08080b] lg:min-h-[420px]">
        <PartViewer geometry={geometry} view={view} compare={compareOn ? compareGeom : null}
          resetToken={resetToken} className="h-[42vh] w-full lg:h-[520px]"
          onUserCamera={() => { /* the user's orbit is local until they pick a preset */ }} />
        {geomBusy && (
          <span className="absolute left-2 top-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] text-zinc-400">loading geometry…</span>
        )}
        {!geometry && !geomBusy && (
          <p className="absolute inset-0 flex items-center justify-center text-[11.5px] text-zinc-600">
            No geometry for this version.
          </p>
        )}
        <span className="absolute bottom-2 left-2 rounded bg-black/50 px-1.5 py-0.5 text-[10px] text-zinc-500">
          drag to orbit · wheel to zoom · right-drag to pan
        </span>
      </div>
      <Pane id="parts.metrics">
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 rounded-lg border border-white/10 bg-[#0a0a0d] p-3 sm:grid-cols-4">
        <Stat label="faces" value={fmt(activeMetrics.faces, 0)} />
        <Stat label="vertices" value={fmt(activeMetrics.vertices, 0)} />
        <Stat label="dimensions" value={(activeMetrics.dimensions_mm || []).map((d) => fmt(d, 1)).join(' × ') || '—'} />
        <Stat label="volume" value={activeMetrics.volume_mm3 != null ? `${fmt(activeMetrics.volume_mm3)} mm³` : '—'} />
        <Stat label="surface area" value={activeMetrics.surface_area_mm2 != null ? `${fmt(activeMetrics.surface_area_mm2)} mm²` : '—'} />
        <Stat label="objective" value={fmt(activeVersion?.objective ?? detail.objective, 3)} />
        <Stat label="thinnest edge" value={activeMetrics.min_edge_mm != null ? `${fmt(activeMetrics.min_edge_mm, 2)} mm` : '—'} />
        <Stat label="version" value={`${activeVersionId}${activeVersion?.kept === false ? ' · reverted' : ''}`} />
      </div>
      </Pane>
    </div>
  );

  const analysisTab = detail && (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10.5px] uppercase tracking-wide text-zinc-500">Analysis</span>
        <div className="flex gap-1">
          <SmallButton disabled={busy === 'analyse' || busy === 'slicer'}
            onClick={() => void runAnalyse(false)}>
            <Gauge size={12} />{busy === 'analyse' ? 'Measuring…' : 'Analyse'}
          </SmallButton>
          <SmallButton disabled={busy === 'slicer' || busy === 'analyse'}
            onClick={() => void runAnalyse(false, true)}
            title="Ask PrusaSlicer whether this mesh slices (reports unavailable if it is not installed)">
            <Layers size={12} />{busy === 'slicer' ? 'Slicing…' : 'Slicer'}
          </SmallButton>
          <SmallButton tone="primary" disabled={busy === 'repair'} onClick={() => void runAnalyse(true)}>
            <Wand2 size={12} />{busy === 'repair' ? 'Repairing…' : 'Repair'}
          </SmallButton>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <VerdictPill verdict={activeVerdict} />
        <span className="text-[10.5px] text-zinc-500">
          {activeVerdict?.errors ?? 0} errors · {activeVerdict?.warnings ?? 0} warnings
        </span>
      </div>
      {activeIssues.length === 0 ? (
        <p className="rounded-md border border-emerald-400/20 bg-emerald-400/5 p-2 text-[11.5px] text-emerald-200">
          No issues reported for this version.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {activeIssues.map((issue, i) => <IssueRow key={`${issue.code}-${i}`} issue={issue} />)}
        </ul>
      )}
      <div className="rounded-md border border-white/8 bg-black/20 p-2 text-[10.5px] text-zinc-500">
        <div className="font-semibold uppercase tracking-wide text-zinc-600">Backends</div>
        <div className="mt-1 flex flex-wrap gap-1">
          {Object.entries(caps?.backends || {}).map(([name, b]) => (
            <span key={name} className={`rounded-full border px-1.5 py-0.5 ${
              b.available ? 'border-emerald-400/25 text-emerald-300' : 'border-white/10 text-zinc-600'
            }`}>{name}{b.version ? ` ${b.version}` : ''}{b.available ? '' : ' missing'}</span>
          ))}
        </div>
        <p className="mt-1">
          Formats: {(caps?.natively_readable || ['stl', 'obj']).join(', ')}
          {caps?.backends?.trimesh?.available ? ' + trimesh formats' : ''}.
          Limit {caps?.limits?.max_faces ? `${fmt(caps.limits.max_faces, 0)} faces` : '—'}.
        </p>
      </div>
      {(() => {
        const slicer = (activeVersion as unknown as {
          report?: { slicer?: { status?: string; detail?: string;
                                gcode_bytes?: number } };
        })?.report?.slicer;
        if (!slicer) return null;
        const ok = slicer.status === 'ok';
        return (
          <p className={`rounded-md border p-2 text-[10.5px] ${
            ok ? 'border-emerald-400/25 bg-emerald-400/5 text-emerald-200'
              : 'border-white/10 bg-black/20 text-zinc-500'
          }`}>
            <span className="font-semibold uppercase tracking-wide">Slicer</span>{' '}
            {slicer.status}{slicer.gcode_bytes ? ` · ${fmt(slicer.gcode_bytes, 0)} gcode bytes` : ''}
            {slicer.detail ? ` — ${slicer.detail}` : ''}
          </p>
        );
      })()}
    </div>
  );

  const optimizeTab = detail && (
    <div className="space-y-2">
      <div className="text-[10.5px] uppercase tracking-wide text-zinc-500">Optimise</div>
      <label className="block text-[10.5px] text-zinc-500">
        Pass
        <select value={pass} onChange={(e) => setPass(e.target.value)}
          className="mt-0.5 w-full rounded-md border border-white/12 bg-black/30 px-2 py-1.5 text-[11.5px] text-zinc-200 outline-none focus:border-violet-400">
          {passes.map((p) => <option key={p.name} value={p.name}>{p.name}{p.modifies === false ? ' (measure only)' : ''}</option>)}
        </select>
      </label>
      {chosenPass && <p className="text-[10.5px] leading-snug text-zinc-500">{chosenPass.description}</p>}
      {pass === 'decimate' && (
        <label className="block text-[10.5px] text-zinc-500">
          Target triangles
          <input type="number" min={4} value={targetFaces}
            onChange={(e) => setTargetFaces(Math.max(4, Number(e.target.value) || 4))}
            className="mt-0.5 w-full rounded-md border border-white/12 bg-black/30 px-2 py-1 font-mono text-[11.5px] text-zinc-200 outline-none focus:border-violet-400" />
        </label>
      )}
      {pass === 'smooth' && (
        <div className="grid grid-cols-2 gap-2">
          <label className="block text-[10.5px] text-zinc-500">
            Iterations
            <input type="number" min={1} max={20} value={iterations}
              onChange={(e) => setIterations(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
              className="mt-0.5 w-full rounded-md border border-white/12 bg-black/30 px-2 py-1 font-mono text-[11.5px] text-zinc-200" />
          </label>
          <label className="block text-[10.5px] text-zinc-500">
            Strength
            <input type="number" min={0.01} max={1} step={0.05} value={strength}
              onChange={(e) => setStrength(Math.max(0.01, Math.min(1, Number(e.target.value) || 0.4)))}
              className="mt-0.5 w-full rounded-md border border-white/12 bg-black/30 px-2 py-1 font-mono text-[11.5px] text-zinc-200" />
          </label>
        </div>
      )}
      {pass === 'detect_overhang' && (
        <label className="block text-[10.5px] text-zinc-500">
          Build angle (degrees)
          <input type="number" min={0} max={89} value={overhangAngle}
            onChange={(e) => setOverhangAngle(Math.max(0, Math.min(89, Number(e.target.value) || 45)))}
            className="mt-0.5 w-full rounded-md border border-white/12 bg-black/30 px-2 py-1 font-mono text-[11.5px] text-zinc-200" />
        </label>
      )}
      <div className="flex flex-wrap gap-1.5">
        <SmallButton tone="primary" disabled={busy === 'optimise'} onClick={() => void runOnePass()}>
          <Play size={12} />{busy === 'optimise' ? 'Running…' : (chosenPass?.modifies === false ? 'Run measure' : 'Run one pass')}
        </SmallButton>
        {chosenPass?.modifies !== false && (
          <SmallButton onClick={() => void startLoop()} disabled={!!job && !TERMINAL_JOB.has(job.status)}>
            <Sparkles size={12} />Loop
          </SmallButton>
        )}
      </div>
      {chosenPass?.modifies !== false && (
        <label className="block text-[10.5px] text-zinc-500">
          Max loop steps
          <input type="number" min={1} max={12} value={maxSteps}
            onChange={(e) => setMaxSteps(Math.max(1, Math.min(12, Number(e.target.value) || 6)))}
            className="mt-0.5 w-full rounded-md border border-white/12 bg-black/30 px-2 py-1 font-mono text-[11.5px] text-zinc-200" />
        </label>
      )}
      {job && (
        <div className="rounded-md border border-white/10 bg-black/25 p-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[10.5px] font-semibold uppercase tracking-wide text-zinc-500">
              Job {job.status}
            </span>
            {!TERMINAL_JOB.has(job.status) && (
              <button type="button" onClick={() => void cancelJob()}
                className="rounded p-0.5 text-zinc-500 hover:text-rose-300" aria-label="Stop job">
                <Square size={11} />
              </button>
            )}
          </div>
          <div className="mt-1 text-[10.5px] text-zinc-400">
            step {job.step}/{job.max_steps} · {job.pass}
            {job.note ? ` · ${job.note}` : ''}
          </div>
          <ol className="mt-1 max-h-40 space-y-0.5 overflow-auto font-mono text-[10px] text-zinc-500">
            {(job.log || []).slice(-12).map((line, i) => <li key={i}>{line}</li>)}
          </ol>
          {job.error && <p className="mt-1 text-[10.5px] text-rose-300">{job.error}</p>}
        </div>
      )}
    </div>
  );

  const historyTab = detail && (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[10.5px] uppercase tracking-wide text-zinc-500">Versions</span>
        <span className="text-[10px] text-zinc-600">{detail.versions?.length || 0}</span>
      </div>
      {(detail.versions || []).slice().reverse().map((v) => {
        const active = v.id === activeVersionId;
        return (
          <div key={v.id}
            className={`rounded-md border p-2 ${active ? 'border-violet-400/40 bg-violet-400/5' : 'border-white/8 bg-black/20'}`}>
            <button type="button" onClick={() => { setPreviewVersion(v.id); setCompareOn(false); }}
              className="flex w-full items-center gap-2 text-left">
              <span className={`font-mono text-[11px] ${v.kept === false ? 'text-zinc-500' : 'text-zinc-200'}`}>{v.id}</span>
              <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-300">{v.op}</span>
              {v.id === detail.current_version && <span className="rounded bg-emerald-400/10 px-1 text-[9px] text-emerald-300">current</span>}
              {v.kept === false && <span className="rounded bg-white/5 px-1 text-[9px] text-zinc-500">reverted</span>}
            </button>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-zinc-500">
              <span>{fmt(v.metrics?.faces, 0)} faces</span>
              <span>obj {fmt(v.objective, 3)}</span>
              <span>{when(v.created)}</span>
              {v.note && <span className="truncate">{v.note}</span>}
            </div>
            <div className="mt-1 flex flex-wrap gap-1">
              {active && !isCurrent && <SmallButton onClick={() => void restore(v.id)}>Restore</SmallButton>}
              <SmallButton onClick={() => void download(v.id)}><ArrowDownToLine size={11} />STL</SmallButton>
              {v.origin === 'dag' && <span className="rounded-full border border-violet-400/25 px-1.5 py-0.5 text-[9px] text-violet-300">aria</span>}
            </div>
          </div>
        );
      })}
    </div>
  );

  const ariaTab = detail && (
    <div className="flex min-h-0 flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="inline-flex items-center gap-1 text-[10.5px] uppercase tracking-wide text-zinc-500">
          <Sparkles size={11} /> DAG agent
        </span>
        <span className="rounded-full border border-violet-400/25 px-1.5 py-0.5 text-[9px] text-violet-300">parts run</span>
      </div>
      <textarea value={runPrompt} rows={3}
        onChange={(e) => setRunPrompt(e.target.value)}
        placeholder="Ask the DAG agent to plan and make a change…"
        aria-label="Ask Aria about this part"
        className="w-full resize-none rounded-md border border-white/12 bg-black/30 px-2 py-1.5 text-[11.5px] text-zinc-200 outline-none focus:border-violet-400" />
      <div className="flex flex-wrap gap-1.5">
        <SmallButton tone="primary" disabled={running || !runPrompt.trim()} onClick={() => void startRun()}>
          <Sparkles size={12} />{running ? 'Planning…' : 'Plan with Aria'}
        </SmallButton>
        {running && <SmallButton onClick={() => void stopRun()}><Square size={11} />Stop</SmallButton>}
      </div>
      <p className="text-[10px] leading-snug text-zinc-600">
        Runs on the DAG engine with the part tools. Quick actions above stay
        deterministic and free of planner latency.
      </p>
      {runLog.length > 0 && (
        <details className="rounded-md border border-white/8 bg-black/30 p-2">
          <summary className="cursor-pointer text-[10px] uppercase tracking-wide text-zinc-500">
            Run log ({runLog.length})
          </summary>
          <ol className="mt-1 max-h-40 space-y-0.5 overflow-auto font-mono text-[10px] text-zinc-500">
            {runLog.map((line, i) => <li key={i}>{line}</li>)}
          </ol>
        </details>
      )}
      {runError && <p role="alert" className="rounded border border-rose-400/30 bg-rose-400/10 p-2 text-[11px] text-rose-200">{runError}</p>}
      {runAnswer && (
        <div className="max-h-56 overflow-auto whitespace-pre-wrap rounded-md border border-emerald-400/20 bg-emerald-400/5 p-2 text-[11px] leading-relaxed text-zinc-200">
          {runAnswer}
        </div>
      )}
    </div>
  );

  const inspector = detail && (
    <aside className="flex min-h-0 flex-col gap-2 rounded-lg border border-white/10 bg-surface-1/60 p-2">
      <div role="tablist" aria-label="Part inspector" className="flex flex-wrap gap-0.5 border-b border-white/8 pb-1">
        <Tab active={tab === 'analyze'} onClick={() => setTab('analyze')} icon={<Gauge size={11} />}>Analyze</Tab>
        <Tab active={tab === 'optimize'} onClick={() => setTab('optimize')} icon={<Wand2 size={11} />}>Optimize</Tab>
        <Tab active={tab === 'history'} onClick={() => setTab('history')} icon={<History size={11} />}>History</Tab>
        <Tab active={tab === 'aria'} onClick={() => setTab('aria')} icon={<Sparkles size={11} />}>Aria</Tab>
      </div>
      <div className="min-h-0 flex-1 overflow-auto pr-0.5">
        {tab === 'analyze' ? analysisTab : tab === 'optimize' ? optimizeTab
          : tab === 'history' ? historyTab : ariaTab}
      </div>
    </aside>
  );

  const empty = (
    <div className="flex h-full flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-white/12 bg-white/[0.015] p-8 text-center">
      <Box size={28} className="text-zinc-600" />
      <p className="m-0 text-[13px] font-semibold text-zinc-300">No part selected</p>
      <p className="m-0 max-w-sm text-[11.5px] text-zinc-500">
        Import an STL or OBJ to measure, repair and optimise it. Every measured
        change is saved as its own version.
      </p>
      <SmallButton tone="primary" onClick={() => fileInput.current?.click()}>
        <Upload size={12} />Import a part
      </SmallButton>
    </div>
  );

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <main id={mainId} tabIndex={-1}
        className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Parts" panes="parts" />
        <input ref={fileInput} type="file" accept={accept} className="hidden"
          aria-label="Choose a mesh file"
          onChange={(e) => { onPickFile(e.target.files?.[0] || null); e.target.value = ''; }} />
        <div className="flex min-h-0 flex-1 flex-col lg:flex-row"
          onDragOver={(e) => { e.preventDefault(); }}
          onDrop={(e) => {
            e.preventDefault();
            const f = e.dataTransfer.files?.[0];
            if (f) onPickFile(f);
          }}>
          {sidebar}
          <section className="min-h-0 min-w-0 flex-1 overflow-auto p-3">
            {importPanel ? (
              <div className="flex h-full items-start justify-center pt-6">{importPanel}</div>
            ) : !detail ? (
              <div className="h-full min-h-[320px]">{empty}</div>
            ) : (
              <div className="flex min-h-0 flex-col gap-3">
                {header}
                {error && (
                  <div role="alert" className="rounded-lg border border-rose-400/35 bg-rose-400/10 p-2 text-[11.5px] text-rose-200">
                    {error}
                    <button type="button" onClick={() => setError('')} className="ml-2 text-rose-300 underline">dismiss</button>
                  </div>
                )}
                {workspaceMode === 'blender' ? (
                  <BlenderWorkspace
                    part={detail}
                    version={activeVersionId}
                    onChanged={(p) => {
                      setDetail(p);
                      setPreviewVersion(p.current_version || activeVersionId);
                      void loadList();
                    }}
                  />
                ) : (
                  <div className="grid min-h-0 gap-3 xl:grid-cols-[minmax(0,1fr)_340px]">
                    <div className="flex min-w-0 flex-col gap-3">{viewer}</div>
                    {inspector}
                  </div>
                )}
              </div>
            )}
          </section>
        </div>
      </main>
    </div>
  );
}
