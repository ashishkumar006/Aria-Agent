import {
  useCallback, useEffect, useMemo, useRef, useState, type FormEvent,
} from 'react';
import {
  Download, Eye, FileText, Folder, FolderPlus, Pencil, Plus,
  RefreshCw, Search, Trash2, Upload,
} from 'lucide-react';
import { Rail, TopBar, Empty, Stat, Pill, Skel, SkipLink } from '../components/ui';
import { useFocusTrap } from '../lib/a11y';
import { useMainId, useRefreshOnActive, useSectionActive } from '../components/KeepAlive';
import { api, type DocFolder, type DocItem } from '../api';
import { flattenFolders, folderSubtree } from '../lib/folders';
import { DocumentModal } from '../components/DocumentModal';
import { Pane, PaneStrip, usePaneMode } from '../lib/panes';

/* Statuses the gateway reports. A document may only be ENABLED when it is
   `ready`; anything else means its vectors are incomplete, and enabling it
   would silently give partial answers. The gateway refuses that too - this
   list is here so the UI explains it before the user tries. */
const STATUS_TONE: Record<string, 'ok' | 'err' | 'warn' | 'info' | 'muted'> = {
  ready: 'ok', blocked: 'warn', failed: 'err',
  embedding: 'info', chunking: 'info', parsing: 'info', pending: 'muted',
};

const ACCEPT = '.pdf,.docx,.md,.markdown,.txt,.text,.log,.html,.htm,.csv,.tsv,.xlsx,.xlsm,.png,.jpg,.jpeg';
const MB = (n: number) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB`
  : n > 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);

export default function Documents() {
  const mainId = useMainId();
  const [docs, setDocs] = useState<DocItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [sel, setSel] = useState<DocItem | null>(null);
  const [detail, setDetail] = useState<DocItem | null>(null);
  const [busy, setBusy] = useState('');
  const [uploading, setUploading] = useState(false);
  const [dropping, setDropping] = useState(false);
  const [note, setNote] = useState('');
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<any[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [preview, setPreview] = useState<{ doc: DocItem } | null>(null);
  const [previewing, setPreviewing] = useState('');
  /* Folder tree. The selection is persisted so returning to Documents lands
     where the user was, not back at the root. */
  const [folders, setFolders] = useState<DocFolder[]>([]);
  const [foldersLoaded, setFoldersLoaded] = useState(false);
  const [folderSel, setFolderSel] = useState<string>(() => {
    try { return localStorage.getItem('aria.docs.folder') || '__all__'; }
    catch { return '__all__'; }
  });
  const [newFolderParent, setNewFolderParent] = useState<string | null>(null);
  const [newFolderName, setNewFolderName] = useState('');
  const [editingFolder, setEditingFolder] = useState('');
  const [editingName, setEditingName] = useState('');
  const [folderErr, setFolderErr] = useState('');
  const [folderBusy, setFolderBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const reqRef = useRef(0);

  const loadFolders = useCallback(async () => {
    try {
      const d = await api.documentFolders();
      setFolders(d.folders || []);
    } catch (e) {
      setFolderErr(String((e as Error)?.message || e));
    } finally {
      setFoldersLoaded(true);
    }
  }, []);
  useEffect(() => { void loadFolders(); }, [loadFolders]);
  useEffect(() => {
    try { localStorage.setItem('aria.docs.folder', folderSel); } catch { /* ignore */ }
  }, [folderSel]);

  const folderNodes = useMemo(() => flattenFolders(folders), [folders]);
  const selectedFolder = folderNodes.find((f) => f.id === folderSel);
  const selectedLabel = folderSel === '__all__' ? 'All documents'
    : folderSel === '__unfiled__' ? 'Unfiled'
      : selectedFolder?.path || 'All documents';
  const uploadTargetLabel = folderSel.startsWith('__') ? 'Unfiled' : selectedLabel;
  const scopeIds = useMemo(
    () => (folderSel === '__all__' || folderSel === '__unfiled__'
      ? new Set<string>()
      : folderSubtree(folderSel, folders)),
    [folderSel, folders]);
  const scopedDocs = useMemo(() => docs.filter((d) => {
    if (folderSel === '__all__') return true;
    if (folderSel === '__unfiled__') return !d.folder_id;
    return !!d.folder_id && scopeIds.has(d.folder_id);
  }), [docs, folderSel, scopeIds]);
  const folderCounts = useMemo(() => {
    const byId = new Map(folders.map((f) => [f.id, f]));
    const counts = new Map<string, number>();
    for (const d of docs) {
      let fid = d.folder_id || '';
      const seen = new Set<string>();
      while (fid && !seen.has(fid)) {
        seen.add(fid);
        counts.set(fid, (counts.get(fid) || 0) + 1);
        fid = byId.get(fid)?.parent_id || '';
      }
    }
    return counts;
  }, [docs, folders]);
  const unfiledCount = docs.filter((d) => !d.folder_id).length;
  useEffect(() => {
    if (!foldersLoaded || folderSel.startsWith('__')) return;
    if (!folderNodes.some((f) => f.id === folderSel)) setFolderSel('__all__');
  }, [folderNodes, folderSel, foldersLoaded]);

  const load = useCallback(async (fresh = false) => {
    try {
      const d = await api.documents(undefined, fresh);
      setDocs(d.documents || []);
      setLoadError('');
    } catch (e) {
      setLoadError(String((e as Error)?.message || e));
    } finally {
      setLoaded(true);
    }
  }, []);

  const active = useSectionActive();
  useRefreshOnActive(active, load);
  /* Put rows the server has already confirmed on screen without waiting for a
     list read. Kept idempotent because it runs both before the read (instant
     feedback) and after it (the read can still be the pre-upload list). */
  const mergeAdded = useCallback((items: DocItem[]) => {
    setDocs((prev) => {
      const seen = new Set(prev.map((d) => d.id));
      const fresh = items.filter((d) => !seen.has(d.id));
      return fresh.length ? [...fresh, ...prev] : prev;
    });
  }, []);
  // Indexing runs in the background, so poll until nothing is in flight.
  // Hidden views stop polling; coming back refreshes immediately (H26).
  useEffect(() => {
    if (!active) return;
    if (!docs.length) load();
    /* Indexing status can change between polls; bypass the read cache for
       this fast cadence so the progress bar is not 10 s behind. */
    const t = setInterval(() => {
      if (docs.some((d) => ['pending', 'parsing', 'chunking', 'embedding'].includes(d.status))) load(true);
    }, 3000);
    return () => clearInterval(t);
  }, [active, docs, load]);

  const open = async (d: DocItem) => {
    /* Sequence-guard the detail read. Clicking doc A then doc B fired two
       requests; if A's slower response landed last it painted A's metadata
       under B's selected row, and the user acted on the wrong document. */
    const req = ++reqRef.current;
    setSel(d);
    setDetail(null);
    setNote('');
    try {
      const r = await api.documentDetail(d.id);
      if (req !== reqRef.current) return;
      setDetail(r.document ?? null);
    } catch (e) {
      if (req !== reqRef.current) return;
      setNote(String((e as Error)?.message || e));
    }
  };

  const upload = async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (!list.length) return;
    setUploading(true);
    setNote('');
    /* Per-file outcome collection. The old loop threw out of the WHOLE
       batch on the first rejection, so selecting [bad.exe, good.txt]
       uploaded neither and reported only the .exe error — the good file
       vanished with no mention. Collect every result, then report the
       successes AND the failures together. */
    const ok: string[] = [];
    const bad: string[] = [];
    /* Documents the server has already accepted. The list read below races the
       gateway's own write: on a slow indexing queue the GET can return the OLD
       list, and because no row is pending the 3 s index poll never starts — the
       file the user just uploaded was invisible for up to 15 s (measured). The
       POST response is authoritative, so put its row on screen now and let the
       normal poll reconcile it. */
    const added: DocItem[] = [];
    const targetFolder = folderSel.startsWith('__') ? '' : folderSel;
    for (const f of list) {
      try {
        const r = await api.uploadDocument(f, targetFolder);
        if (r.document) added.push(r.document);
        const nw = (r.warnings || []).length;
        const w = nw ? ` (${nw} warning(s))` : '';
        ok.push(`${r.document.filename}${w}`);
      } catch (e) {
        const why = String((e as Error)?.message || e);
        bad.push(`${f.name}: ${why}`);
      }
    }
    if (added.length) { mergeAdded(added); setLoaded(true); }
    if (ok.length) await load().catch(() => { /* keep the notes */ });
    if (added.length) mergeAdded(added);
    const parts: string[] = [];
    if (ok.length) {
      parts.push(`uploaded ${ok.length === 1 ? '' : `${ok.length} files: `}`
        + ok.join(', ').slice(0, 200)
        + ' — indexing has started; enable it when ready');
    }
    if (bad.length) parts.push(`upload failed: ${bad.join('; ').slice(0, 240)}`);
    setNote(parts.join(' | ') || 'nothing uploaded');
    setUploading(false);
    if (fileRef.current) fileRef.current.value = '';
  };

  const toggle = async (d: DocItem) => {
    if (busy) return;
    setBusy(d.id);
    setNote('');
    try {
      await api.setDocumentEnabled(d.id, !d.enabled);
      await load();
      const next: DocItem = { ...d, enabled: !d.enabled };
      if (sel?.id === d.id) open(next);
    } catch (e) {
      // The gateway refuses to enable a partial document; say why.
      setNote(String((e as Error)?.message || e));
    } finally { setBusy(''); }
  };

  const reindex = async (d: DocItem) => {
    if (busy) return;
    setBusy(d.id);
    try { await api.reindexDocument(d.id); setNote('re-indexing started'); await load(); }
    catch (e) { setNote(String((e as Error)?.message || e)); }
    finally { setBusy(''); }
  };

  const remove = async (d: DocItem) => {
    if (busy) return;
    if (!confirm(`Delete ${d.filename}? Its chunks and vectors are removed too. This cannot be undone.`)) return;
    setBusy(d.id);
    try {
      await api.deleteDocument(d.id);
      if (sel?.id === d.id) { setSel(null); setDetail(null); }
      setNote(`deleted ${d.filename}`);
      await load();
    } catch (e) { setNote(String((e as Error)?.message || e)); }
    finally { setBusy(''); }
  };

  const openCreateFolder = (parentId: string) => {
    setNewFolderParent(parentId);
    setNewFolderName('');
    setFolderErr('');
  };

  const createFolder = async (e?: FormEvent) => {
    e?.preventDefault();
    const name = newFolderName.trim();
    if (!name || folderBusy) return;
    setFolderBusy(true);
    setFolderErr('');
    try {
      const r = await api.createDocumentFolder(name, newFolderParent || '');
      if (r.error) throw new Error(r.error);
      await loadFolders();
      setNewFolderParent(null);
      setNewFolderName('');
      if (r.folder?.id) setFolderSel(r.folder.id);
    } catch (err) {
      setFolderErr(String((err as Error)?.message || err));
    } finally { setFolderBusy(false); }
  };

  const renameFolder = async (folderId: string) => {
    const name = editingName.trim();
    if (!name || folderBusy) return;
    setFolderBusy(true);
    setFolderErr('');
    try {
      const r = await api.renameDocumentFolder(folderId, name);
      if (r.error) throw new Error(r.error);
      setEditingFolder('');
      setEditingName('');
      await loadFolders();
    } catch (err) {
      setFolderErr(String((err as Error)?.message || err));
    } finally { setFolderBusy(false); }
  };

  const deleteFolder = async (folderId: string, path: string) => {
    if (folderBusy) return;
    if (!confirm(`Delete the empty folder "${path}"?\n\nFolders with documents or subfolders must be emptied first.`)) return;
    setFolderBusy(true);
    setFolderErr('');
    try {
      const r = await api.deleteDocumentFolder(folderId);
      if (r.error) throw new Error(r.error);
      if (folderSel === folderId) setFolderSel('__all__');
      await loadFolders();
    } catch (err) {
      setFolderErr(String((err as Error)?.message || err));
    } finally { setFolderBusy(false); }
  };

  const moveDoc = async (d: DocItem, folderId: string) => {
    if (busy) return;
    setBusy(d.id);
    setFolderErr('');
    try {
      const r = await api.moveDocument(d.id, folderId);
      if (r.error) throw new Error(r.error);
      if (detail?.id === d.id) setDetail({ ...detail, folder_id: folderId });
      await load(true);
    } catch (err) {
      setFolderErr(String((err as Error)?.message || err));
    } finally { setBusy(''); }
  };

  const fileTone = (d: DocItem) => {
    if (d.status === 'failed') return 'text-rose-300';
    if (d.status === 'blocked') return 'text-amber-300';
    if (['pending', 'parsing', 'chunking', 'embedding'].includes(d.status)) {
      return 'text-sky-300';
    }
    if (d.status === 'ready') return d.enabled ? 'text-emerald-300' : 'text-zinc-300';
    return 'text-zinc-200';
  };

  /* Open the window at once. The viewer inside it fetches what it needs:
     a PDF goes to the browser's own viewer as a blob URL (real page scroll,
     selection, search, zoom), and anything the browser cannot show is rendered
     from server-made pages. Neither path downloads a second copy up front the
     way this used to. */
  const previewDoc = (d: DocItem) => {
    setNote('');
    setPreview({ doc: d });
  };

  const downloadDoc = async (d: DocItem) => {
    setPreviewing(d.id);
    try {
      const blob = await api.documentContent(d.id);
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = d.filename;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    } catch (e) {
      setNote(String((e as Error)?.message || e));
    } finally { setPreviewing(''); }
  };

  const closePreview = () => {
    setPreviewing('');
    setPreview(null);
  };

  const search = async () => {
    const query = q.trim();
    if (!query) { setHits(null); return; }
    const id = ++reqRef.current;
    setSearching(true);
    try {
      const r = await api.searchDocuments(query);
      // Discard a stale response that arrived after a newer query.
      if (id !== reqRef.current) return;
      setHits(r.hits || []);
    } catch (e) {
      if (id === reqRef.current) setNote(String((e as Error)?.message || e));
    } finally { if (id === reqRef.current) setSearching(false); }
  };

  const ready = scopedDocs.filter((d) => d.status === 'ready');
  const enabled = scopedDocs.filter((d) => d.enabled);
  const indexing = scopedDocs.filter((d) =>
    ['pending', 'parsing', 'chunking', 'embedding'].includes(d.status));
  const blocked = scopedDocs.filter((d) => d.status === 'blocked');

  /* The detail drawer is an overlay: a backdrop covers the page behind it,
     so the keyboard must not be able to tab out onto the covered content, and
     Escape closes it like the × button does. Focus returns to the row that
     opened it when it unmounts. The document window traps its own focus. */
  const closeDetail = () => { setSel(null); setDetail(null); };
  const drawerRef = useRef<HTMLDivElement | null>(null);
  useFocusTrap(drawerRef, { active: !!sel, onEscape: closeDetail });

  const foldersMode = usePaneMode('documents.folders');

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      {foldersMode === 'collapsed'
        ? <PaneStrip id="documents.folders" label="Document folders" />
        : (
      <aside className="flex w-full max-h-[32vh] flex-none flex-col border-b border-white/10 bg-[#0b0b0e] lg:h-full lg:max-h-none lg:w-[248px] lg:border-b-0 lg:border-r"
             aria-label="Document folders">
        <div className="flex items-center justify-between px-3 pb-2 pt-3">
          <h2 className="text-xs font-bold tracking-wide">Folders</h2>
          <button
            onClick={() => openCreateFolder('')}
            title="Create a main folder"
            aria-label="New main folder"
            className="flex items-center gap-1 rounded border border-white/15 bg-white/5 px-1.5 py-0.5 text-[10.5px] text-zinc-300 hover:border-violet-400 hover:text-zinc-100">
            <FolderPlus size={11} /> New
          </button>
        </div>
        {newFolderParent !== null && (
          <form onSubmit={(e) => { void createFolder(e); }}
                className="mx-2 mb-2 rounded-md border border-violet-400/40 bg-[#0e0e12] p-1.5">
            <div className="mb-1 truncate text-[10px] text-zinc-500">
              {newFolderParent
                ? `Inside ${folderNodes.find((f) => f.id === newFolderParent)?.path || 'folder'}`
                : 'New main folder'}
            </div>
            <input
              autoFocus
              value={newFolderName}
              onChange={(e) => setNewFolderName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Escape') setNewFolderParent(null); }}
              placeholder="Folder name"
              aria-label="New folder name"
              className="h-7 w-full rounded border border-white/10 bg-black/40 px-2 text-xs outline-none focus:border-violet-400"
            />
            <div className="mt-1 flex justify-end gap-1">
              <button type="button" onClick={() => setNewFolderParent(null)}
                className="rounded px-1.5 py-0.5 text-[10px] text-zinc-500 hover:text-zinc-200">
                cancel
              </button>
              <button type="submit" disabled={!newFolderName.trim() || folderBusy}
                className="rounded bg-violet-400 px-2 py-0.5 text-[10px] font-bold text-[#0b0b0e] disabled:opacity-40">
                {folderBusy ? '…' : 'Add'}
              </button>
            </div>
          </form>
        )}
        {folderErr && (
          <Pane id="documents.foldererr">
          <p role="status" className="mx-2 mb-2 rounded border border-amber-400/30 bg-amber-400/5 px-2 py-1 text-[10.5px] leading-snug text-amber-200">
            {folderErr}
          </p>
          </Pane>
        )}
        <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
          <button
            onClick={() => setFolderSel('__all__')}
            aria-pressed={folderSel === '__all__'}
            className={`flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-[12px] ${folderSel === '__all__' ? 'bg-violet-400/10 text-violet-100' : 'text-zinc-300 hover:bg-white/5'}`}>
            <FileText size={12} className="text-zinc-500" />
            <span className="min-w-0 flex-1 truncate">All documents</span>
            <span className="font-mono text-[10px] text-zinc-500">{docs.length}</span>
          </button>
          {unfiledCount > 0 && (
            <button
              onClick={() => setFolderSel('__unfiled__')}
              aria-pressed={folderSel === '__unfiled__'}
              className={`flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-[12px] ${folderSel === '__unfiled__' ? 'bg-violet-400/10 text-violet-100' : 'text-zinc-400 hover:bg-white/5'}`}>
              <Folder size={12} className="text-zinc-600" />
              <span className="min-w-0 flex-1 truncate">Unfiled</span>
              <span className="font-mono text-[10px] text-zinc-500">{unfiledCount}</span>
            </button>
          )}
          <div className="px-2 pb-1 pt-2 text-[9.5px] font-bold tracking-[0.14em] text-zinc-600">FOLDERS</div>
          {folderNodes.map((f) => {
            const count = folderCounts.get(f.id) || 0;
            const selected = folderSel === f.id;
            return (
              <div key={f.id} style={{ paddingLeft: 6 + f.depth * 12 }}
                   className={`group flex items-center rounded-md ${selected ? 'bg-violet-400/10 text-violet-100' : 'text-zinc-300 hover:bg-white/5'}`}>
                {editingFolder === f.id ? (
                  <form onSubmit={(e) => { e.preventDefault(); void renameFolder(f.id); }}
                        className="flex min-w-0 flex-1 items-center gap-1 py-0.5">
                    <input autoFocus value={editingName}
                           onChange={(e) => setEditingName(e.target.value)}
                           onKeyDown={(e) => { if (e.key === 'Escape') setEditingFolder(''); }}
                           aria-label="Folder name"
                           className="h-6 min-w-0 flex-1 rounded border border-white/15 bg-black/40 px-1.5 text-[11px] outline-none focus:border-violet-400" />
                    <button type="submit" className="px-1 text-[10px] text-emerald-300" title="Save name">✓</button>
                    <button type="button" onClick={() => setEditingFolder('')} className="px-1 text-[10px] text-zinc-500" title="Cancel">×</button>
                  </form>
                ) : (
                  <>
                    <button
                      onClick={() => setFolderSel(f.id)}
                      aria-pressed={selected}
                      title={f.path}
                      className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 text-left text-[11.5px]">
                      <Folder size={11} className={selected ? 'text-violet-300' : 'text-zinc-600'} />
                      <span className="min-w-0 flex-1 truncate">{f.name}</span>
                      <span className="font-mono text-[9.5px] text-zinc-500">{count}</span>
                    </button>
                    <span className="flex flex-none items-center opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
                      <button onClick={() => openCreateFolder(f.id)} title="New subfolder" aria-label={`New subfolder in ${f.path}`}
                        className="flex h-5 w-5 items-center justify-center rounded text-zinc-500 hover:text-violet-200">
                        <Plus size={10} />
                      </button>
                      <button onClick={() => { setEditingFolder(f.id); setEditingName(f.name); }} title="Rename folder" aria-label={`Rename folder ${f.path}`}
                        className="flex h-5 w-5 items-center justify-center rounded text-zinc-500 hover:text-zinc-200">
                        <Pencil size={9} />
                      </button>
                      <button onClick={() => void deleteFolder(f.id, f.path)} title="Delete empty folder" aria-label={`Delete folder ${f.path}`}
                        className="flex h-5 w-5 items-center justify-center rounded text-zinc-500 hover:text-rose-300">
                        <Trash2 size={9} />
                      </button>
                    </span>
                  </>
                )}
              </div>
            );
          })}
          {!folderNodes.length && (
            <Pane id="documents.nofolders">
            <p className="px-2 py-2 text-[10.5px] leading-snug text-zinc-600">
              No folders yet. Create one or use All documents.
            </p>
            </Pane>
          )}
        </div>
      </aside>
        )}
      <main id={mainId} tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Documents" panes="documents">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') search(); }}
            placeholder="Test-search enabled docs…"
            aria-label="Search enabled documents"
            className="w-48 rounded-md border border-white/10 bg-black/40 px-2.5 py-1.5 text-xs outline-none focus:border-violet-400"
          />
          <button onClick={search} disabled={searching} aria-label="Run document search"
            className="rounded-md border border-white/15 bg-white/5 px-2.5 py-1.5 text-xs hover:border-violet-400 disabled:opacity-40">
            <Search size={13} />
          </button>
          <button onClick={() => load()} aria-label="Refresh documents"
            className="flex items-center gap-1.5 rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs hover:border-violet-400">
            <RefreshCw size={13} /> refresh
          </button>
        </TopBar>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {/* A single status line replaces four large tiles. The numbers are
              scoped to the selected folder; the file rows carry the colors. */}
          <Pane id="documents.scope">
          <div className="mx-3.5 mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-white/8 pb-2 text-[11px] text-zinc-400">
            <span className="font-semibold text-zinc-200">{selectedLabel}</span>
            <span><b className="text-zinc-300">{scopedDocs.length}</b> docs</span>
            <span><b className="text-emerald-300">{ready.length}</b> ready</span>
            <span><b className="text-zinc-300">{enabled.length}</b> enabled</span>
            {indexing.length > 0 && (
              <span className="text-sky-300">{indexing.length} indexing</span>
            )}
            {blocked.length > 0 && (
              <span className="text-amber-300">{blocked.length} blocked</span>
            )}
          </div>
          </Pane>

          {/* Drop zone: there is no size cap, so a large file is fine; the
              indexing worker paces itself to leave the model free. */}
          <div
            onDragOver={(e) => { e.preventDefault(); setDropping(true); }}
            onDragLeave={() => setDropping(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDropping(false);
              if (e.dataTransfer.files?.length) upload(e.dataTransfer.files);
            }}
            className={`mx-3.5 mt-3 rounded-[10px] border-2 border-dashed p-3 text-center transition-colors ${
              dropping ? 'border-violet-400 bg-violet-400/5' : 'border-white/10'}`}
          >
            <input ref={fileRef} type="file" multiple accept={ACCEPT} className="hidden"
              onChange={(e) => { if (e.target.files?.length) upload(e.target.files); }}
              aria-label="Upload documents" />
            <p className="mb-2 text-[11px] text-zinc-500">
              Uploading to <span className="font-semibold text-zinc-300">{uploadTargetLabel}</span>
            </p>
            <button onClick={() => fileRef.current?.click()} disabled={uploading}
              className="inline-flex items-center gap-2 rounded-md bg-violet-400/15 px-3.5 py-2 text-[13px] font-semibold text-violet-200 hover:bg-violet-400/25 disabled:opacity-40">
              <Upload size={14} /> {uploading ? 'uploading…' : 'Upload documents'}
            </button>
            <p className="mt-2 text-[11px] leading-snug text-zinc-muted">
              Drop files here or choose. PDF, DOCX, Markdown, text, HTML, CSV,
              Excel and PNG/JPG images, up to 25 MB. A document is switched on
              for agent search as soon as it finishes indexing — switch it off
              here if it should not answer. An image holds no search text: it is
              there for the author to place in a document as a figure.
            </p>
            {note && <p role="status" className="mt-2 text-[12px] text-amber-200">{note}</p>}
          </div>

          {loadError && (
            <p role="alert" className="mx-3.5 mt-3 rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-xs text-red-200">
              couldn&apos;t load documents ({loadError}). <button onClick={() => load()} className="underline">retry</button>
            </p>
          )}

          {/* Search results: retrieval only, no LLM cost. */}
          {hits && (
            <Pane id="documents.results">
            <section className="mx-3.5 mt-3 overflow-hidden rounded-[10px] border border-white/10 bg-[#0e0e12]">
              <div className="flex items-center justify-between border-b border-white/10 px-3.5 py-2.5 text-xs font-bold">
                SEARCH RESULTS
                <span className="text-[11px] font-normal text-zinc-muted">
                  {hits.length} from enabled documents
                </span>
              </div>
              {!hits.length ? (
                <p className="px-3.5 py-3 text-[12px] text-zinc-muted">
                  No matches in the enabled documents.
                </p>
              ) : hits.map((h, i) => (
                <div key={`${h.id}-${i}`} className="border-b border-white/5 px-3.5 py-2 last:border-0">
                  {/* Provenance the API already returns. The row used to
                      print only `chunk N · doc-<hex>`, so a hit could not
                      be attributed without looking the id up by hand.
                      Filename + page + heading is what makes a result
                      usable. */}
                  <div className="font-mono text-[10.5px] text-zinc-muted">
                    <span className="text-zinc-300">{h.filename || h.doc_id}</span>
                    {' · '}chunk {h.chunk_index}
                    {h.page != null ? ` · p.${h.page}` : ''}
                    {h.heading_path?.length ? ` · ${h.heading_path.join(' > ')}` : ''}
                    {h.embed_model ? ` · ${h.embed_model}` : ''}
                  </div>
                  <div className="text-[12.5px] text-zinc-200">{h.chunk}</div>
                </div>
              ))}
            </section>
            </Pane>
          )}

          {/* Document list */}
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">UPLOADED</div>
          {!loaded && <div className="px-3.5"><Skel n={3} /></div>}
          {loaded && !scopedDocs.length && !loadError && (
            <Empty icon={<FileText size={30} />}
              title={folderSel === '__all__' ? 'No documents yet' : 'No documents here'}
              sub={folderSel === '__all__'
                ? 'Upload a file above, wait for indexing, then enable it to make it available to chat and research.'
                : `Nothing is filed in ${selectedLabel}. Upload above or move a document here.`} />
          )}
          <div className="space-y-2 px-3.5 pb-6">
            {scopedDocs.map((d) => (
              <section key={d.id}
                className={`rounded-[10px] border bg-[#0e0e12] ${sel?.id === d.id ? 'border-violet-400/60' : 'border-white/10'}`}>
                <button onClick={() => open(d)}
                  className="flex w-full items-start gap-2.5 px-3.5 py-2.5 text-left">
                    <FileText size={15} className="mt-0.5 flex-none text-zinc-muted" />
                    <div className="min-w-0 flex-1">
                      <div
                        className={`truncate text-[13px] font-semibold ${fileTone(d)}`}
                        title={`${d.filename} · ${d.status}${d.enabled ? ' · enabled' : ''}${
                          ['pending', 'parsing', 'chunking', 'embedding'].includes(d.status)
                            ? ` · ${Math.round((d.progress || 0) * 100)}%`
                            : ''
                        }`}>
                        {d.filename}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        <Pill tone={STATUS_TONE[d.status] || 'muted'}>{d.status}</Pill>
                        {/* Where it came from. Aria files what she makes under
                            the section that made it, so a library row has to
                            say "authoring", "research", "web" or "upload" —
                            otherwise every file looks like something the user
                            dragged in, and neither the user nor the agent can
                            tell what is safe to delete. */}
                        {d.origin && d.origin !== 'upload' && (
                          <Pill tone="info">{d.origin}</Pill>
                        )}
                        {d.folder_id && (
                          <span className="inline-flex items-center gap-1 text-[11px] text-zinc-muted"
                            title={folderNodes.find((f) => f.id === d.folder_id)?.path || d.folder_id}>
                            <Folder size={10} />
                            {folderNodes.find((f) => f.id === d.folder_id)?.path || 'filed'}
                          </span>
                        )}
                        <span className="text-[11px] text-zinc-muted">
                          {d.doc_type} · {MB(d.size_bytes)} · {d.chunk_count} chunk{d.chunk_count === 1 ? '' : 's'}
                        </span>
                      </div>
                      {/* Progress is only meaningful while indexing. */}
                      {['parsing', 'chunking', 'embedding'].includes(d.status) && d.chunk_count > 0 && (
                        <Pane id="documents.progress">
                        <div className="mt-1.5">
                          <div className="h-1 overflow-hidden rounded-full bg-white/10">
                            <div className="h-full bg-violet-400"
                              style={{ width: `${Math.round((d.progress || 0) * 100)}%` }} />
                          </div>
                          <div className="mt-0.5 text-[10.5px] text-zinc-muted">
                            {d.embedded_count}/{d.chunk_count} chunks embedded
                          </div>
                        </div>
                        </Pane>
                      )}
                      <Pane id="documents.blocked">
                      {d.status === 'blocked' && (
                        <div className="mt-1 text-[11px] text-amber-200">
                          waiting for the embedding model — it resumes automatically
                        </div>
                      )}
                      {d.error && <div className="mt-1 text-[11px] text-red-300">{d.error}</div>}
                      </Pane>
                    </div>
                    <Pill tone={d.enabled ? 'ok' : 'muted'}>{d.enabled ? 'in use' : 'off'}</Pill>
                  </button>
                <div className="flex flex-wrap gap-1.5 border-t border-white/10 px-3.5 py-2">
                  <button
                    onClick={() => toggle(d)}
                    disabled={busy === d.id}
                    aria-label={`${d.enabled ? 'Disable' : 'Enable'} ${d.filename}`}
                    className="rounded border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] hover:border-violet-400 disabled:opacity-40"
                  >
                    {d.enabled ? 'Disable' : 'Enable'}
                  </button>
                  <button onClick={() => reindex(d)} disabled={busy === d.id}
                    aria-label={`Re-index ${d.filename}`}
                    className="rounded border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] hover:border-violet-400 disabled:opacity-40">
                    Re-index
                  </button>
                  <button onClick={() => previewDoc(d)}
                    disabled={busy === d.id || previewing === d.id}
                    aria-label={`Preview ${d.filename}`}
                    className="rounded border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] hover:border-violet-400 disabled:opacity-40">
                    <Eye size={12} className="inline" /> preview
                  </button>
                  <button onClick={() => downloadDoc(d)}
                    disabled={busy === d.id || previewing === d.id}
                    aria-label={`Download ${d.filename}`}
                    className="rounded border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] hover:border-violet-400 disabled:opacity-40">
                    <Download size={12} className="inline" /> download
                  </button>
                  <button onClick={() => remove(d)} disabled={busy === d.id}
                    aria-label={`Delete ${d.filename}`}
                    className="rounded border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] text-zinc-400 hover:border-red-400/50 hover:text-red-200 disabled:opacity-40">
                    <Trash2 size={12} className="inline" /> delete
                  </button>
                </div>
              </section>
            ))}
          </div>
        </div>

        {/* Detail drawer: click a document to see exactly what was indexed. */}
        {sel && (
          <div ref={drawerRef} className="fixed inset-0 z-40 flex justify-end bg-black/60"
            onClick={closeDetail}
            role="presentation">
            <aside role="dialog" aria-modal="true" aria-label={`Details for ${sel.filename}`}
              onClick={(e) => e.stopPropagation()}
              className="flex h-full w-full max-w-[560px] flex-col border-l border-white/10 bg-[#0b0b0e]">
              <div className="flex items-center gap-2 border-b border-white/10 px-4 py-3">
                <FileText size={15} className="text-zinc-muted" />
                <span className="flex-1 truncate text-[14px] font-bold">{sel.filename}</span>
                <button onClick={() => { setSel(null); setDetail(null); }}
                  aria-label="Close details" className="rounded px-2 py-1 text-zinc-400 hover:bg-white/5">×</button>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto p-4">
                {!detail ? <Skel n={4} /> : (
                  <>
                    <div className="grid grid-cols-2 gap-2">
                      <Stat k="STATUS" v={detail.status} s={detail.error || 'indexing finished'} />
                      <Stat k="CHUNKS" v={String(detail.chunk_count)} s={`${detail.embedded_count} embedded`} />
                      <Stat k="SIZE" v={MB(detail.size_bytes)} s={detail.doc_type} />
                      <Stat k="MODEL" v={detail.embed_model || '—'} s={detail.embed_dim ? `${detail.embed_dim} dims` : 'not embedded yet'} />
                    </div>
                    <label className="mt-3 flex items-center gap-2 text-[11px] text-zinc-400">
                      <span className="font-bold tracking-wide text-zinc-500">FOLDER</span>
                      <select
                        value={detail.folder_id || ''}
                        onChange={(e) => void moveDoc(detail, e.target.value)}
                        disabled={busy === detail.id}
                        aria-label="Move document to folder"
                        className="h-7 min-w-0 flex-1 rounded border border-white/10 bg-black/40 px-2 text-xs text-zinc-200 outline-none focus:border-violet-400 disabled:opacity-50">
                        <option value="">Unfiled</option>
                        {folderNodes.map((f) => (
                          <option key={f.id} value={f.id}>{f.path}</option>
                        ))}
                      </select>
                    </label>
                    {!!detail.warnings?.length && (
                      <div className="mt-3 rounded-lg border border-amber-400/40 bg-amber-400/5 p-3">
                        <div className="text-[11px] font-bold text-amber-200">PARSER WARNINGS</div>
                        <ul className="mt-1 list-disc pl-4 text-[11.5px] text-amber-100">
                          {detail.warnings.map((w: string, i: number) => <li key={i}>{w}</li>)}
                        </ul>
                      </div>
                    )}
                    <div className="mt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">
                      CHUNKS ({detail.chunks?.length || 0})
                    </div>
                    <div className="mt-1.5 space-y-1.5">
                      {(detail.chunks || []).map((c: any) => (
                        <div key={c.index} className="rounded-[10px] border border-white/10 bg-[#0e0e12] px-3 py-2">
                          <div className="flex items-center gap-2 text-[10.5px] text-zinc-muted">
                            <span className="font-mono">#{c.index}</span>
                            <Pill tone={c.embedded ? 'ok' : 'warn'}>{c.embedded ? 'embedded' : 'pending'}</Pill>
                            <span>{c.kind}</span>
                            <span>{c.words}w</span>
                            {c.page != null && <span>p.{c.page}</span>}
                            {!!c.heading_path?.length && (
                              <span className="truncate">{c.heading_path.join(' › ')}</span>
                            )}
                          </div>
                          <div className="mt-1 text-[12px] whitespace-pre-wrap text-zinc-300">{c.preview}</div>
                        </div>
                      ))}
                    </div>
                  </>
                )}
              </div>
            </aside>
          </div>
        )}

        {/* The same document window Authoring uses: centred over a blurred
            page, with a PDF handed to the browser's own viewer so page scroll,
            text search and zoom are the real ones. The blob sandboxing that
            used to sit here — `allow-same-origin` without `allow-scripts`,
            which is what stops an uploaded .html from reading the console
            token while still letting the PDF plugin render — moved into
            DocumentModal along with the iframe. */}
        <DocumentModal
          open={!!preview}
          target={preview ? {
            kind: 'document', docId: preview.doc.id,
            format: preview.doc.doc_type, label: preview.doc.filename,
          } : null}
          onClose={closePreview}
          onDownload={preview ? () => { void downloadDoc(preview.doc); } : undefined}
        />
      </main>
    </div>
  );
}