import { useCallback, useEffect, useRef, useState } from 'react';
import { FileText, Upload, RefreshCw, Trash2, Search, Eye, Download } from 'lucide-react';
import { Rail, TopBar, Empty, Stat, Pill, Skel, SkipLink } from '../components/ui';
import { useFocusTrap } from '../lib/a11y';
import { api, type DocItem } from '../api';

/* Statuses the gateway reports. A document may only be ENABLED when it is
   `ready`; anything else means its vectors are incomplete, and enabling it
   would silently give partial answers. The gateway refuses that too - this
   list is here so the UI explains it before the user tries. */
const STATUS_TONE: Record<string, 'ok' | 'err' | 'warn' | 'info' | 'muted'> = {
  ready: 'ok', blocked: 'warn', failed: 'err',
  embedding: 'info', chunking: 'info', parsing: 'info', pending: 'muted',
};

const ACCEPT = '.pdf,.docx,.md,.markdown,.txt,.text,.log,.html,.htm,.csv,.tsv,.xlsx,.xlsm';
const MB = (n: number) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB`
  : n > 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);

export default function Documents() {
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
  const [preview, setPreview] = useState<{ doc: DocItem; url: string } | null>(null);
  const [previewing, setPreviewing] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);
  const reqRef = useRef(0);

  const load = useCallback(async () => {
    try {
      const d = await api.documents();
      setDocs(d.documents || []);
      setLoadError('');
    } catch (e) {
      setLoadError(String((e as Error)?.message || e));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  // Indexing runs in the background, so poll until nothing is in flight.
  useEffect(() => {
    const t = setInterval(() => {
      if (docs.some((d) => ['pending', 'parsing', 'chunking', 'embedding'].includes(d.status))) load();
    }, 3000);
    return () => clearInterval(t);
  }, [docs, load]);

  const open = async (d: DocItem) => {
    setSel(d);
    setDetail(null);
    setNote('');
    try {
      const r = await api.documentDetail(d.id);
      setDetail(r.document ?? null);
    } catch (e) { setNote(String((e as Error)?.message || e)); }
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
    for (const f of list) {
      try {
        const r = await api.uploadDocument(f);
        const nw = (r.warnings || []).length;
        const w = nw ? ` (${nw} warning(s))` : '';
        ok.push(`${r.document.filename}${w}`);
      } catch (e) {
        const why = String((e as Error)?.message || e);
        bad.push(`${f.name}: ${why}`);
      }
    }
    if (ok.length) await load().catch(() => { /* keep the notes */ });
    const parts: string[] = [];
    if (ok.length) {
      parts.push(`uploaded ${ok.length === 1 ? '' : `${ok.length} files: `}`
        + ok.join(', ').slice(0, 200) + ' — indexing has started');
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

  /* The preview renders the browser's own viewer over a blob URL,
     so PDFs open inline with page navigation, zoom and download
     and no extra dependency. Blob URLs must be revoked or the
     document bytes leak for the life of the page. */
  const previewDoc = async (d: DocItem) => {
    setPreviewing(d.id);
    try {
      const blob = await api.documentContent(d.id);
      setPreview((p) => { if (p) URL.revokeObjectURL(p.url); return null; });
      setPreview({ doc: d, url: URL.createObjectURL(blob) });
    } catch (e) {
      setNote(String((e as Error)?.message || e));
    } finally { setPreviewing(''); }
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
    setPreview((p) => { if (p) URL.revokeObjectURL(p.url); return null; });
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

  const ready = docs.filter((d) => d.status === 'ready');
  const enabled = docs.filter((d) => d.enabled);

  /* Both overlays are modal: a backdrop covers the page behind
     them, so the keyboard must not be able to tab out onto the
     covered content, and Escape closes them like the × button
     does. Focus returns to the row that opened each one when it
     unmounts. */
  const closeDetail = () => { setSel(null); setDetail(null); };
  const drawerRef = useRef<HTMLDivElement | null>(null);
  const previewRef = useRef<HTMLDivElement | null>(null);
  useFocusTrap(drawerRef, { active: !!sel, onEscape: closeDetail });
  useFocusTrap(previewRef, { active: !!preview, onEscape: closePreview });

  return (
    <div className="flex h-full flex-col lg:flex-row">
      <SkipLink />
      <Rail />
      <main id="main" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col outline-none">
        <TopBar crumb="Documents">
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
          <button onClick={load} aria-label="Refresh documents"
            className="flex items-center gap-1.5 rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs hover:border-violet-400">
            <RefreshCw size={13} /> refresh
          </button>
        </TopBar>

        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex flex-wrap gap-2.5 p-3.5 pb-0">
            <Stat k="DOCUMENTS" v={String(docs.length)} s="uploaded" />
            <Stat k="READY" v={String(ready.length)} s="fully embedded"
              tone={ready.length ? 'ok' : undefined} />
            <Stat k="IN USE" v={String(enabled.length)} s="enabled for chat" />
            <Stat k="BLOCKED" v={String(docs.filter((d) => d.status === 'blocked').length)}
              s="waiting for the embedder" tone={docs.some((d) => d.status === 'blocked') ? 'warn' : undefined} />
          </div>

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
            className={`mx-3.5 mt-3 rounded-[10px] border-2 border-dashed p-4 text-center transition-colors ${
              dropping ? 'border-violet-400 bg-violet-400/5' : 'border-white/10'}`}
          >
            <input ref={fileRef} type="file" multiple accept={ACCEPT} className="hidden"
              onChange={(e) => { if (e.target.files?.length) upload(e.target.files); }}
              aria-label="Upload documents" />
            <button onClick={() => fileRef.current?.click()} disabled={uploading}
              className="inline-flex items-center gap-2 rounded-md bg-violet-400/15 px-3.5 py-2 text-[13px] font-semibold text-violet-200 hover:bg-violet-400/25 disabled:opacity-40">
              <Upload size={14} /> {uploading ? 'uploading…' : 'Upload documents'}
            </button>
            <p className="mt-2 text-[11.5px] text-zinc-muted">
              Drop files here or click to choose. PDF, DOCX, Markdown, text, HTML,
              CSV and Excel, up to 25 MB each — chunks are embedded in the background.
            </p>
            {note && <p role="status" className="mt-2 text-[12px] text-amber-200">{note}</p>}
          </div>

          {loadError && (
            <p role="alert" className="mx-3.5 mt-3 rounded-lg border border-red-400/30 bg-red-400/5 p-3 text-xs text-red-200">
              couldn&apos;t load documents ({loadError}). <button onClick={load} className="underline">retry</button>
            </p>
          )}

          {/* Search results: retrieval only, no LLM cost. */}
          {hits && (
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
          )}

          {/* Document list */}
          <div className="px-3.5 pb-1 pt-4 text-[10.5px] font-bold tracking-[0.14em] text-zinc-muted">UPLOADED</div>
          {!loaded && <div className="px-3.5"><Skel n={3} /></div>}
          {loaded && !docs.length && !loadError && (
            <Empty icon={<FileText size={30} />} title="No documents yet"
              sub="Upload a file above and it becomes available to chat and research." />
          )}
          <div className="space-y-2 px-3.5 pb-6">
            {docs.map((d) => (
              <section key={d.id}
                className={`rounded-[10px] border bg-[#0e0e12] ${sel?.id === d.id ? 'border-violet-400/60' : 'border-white/10'}`}>
                <button onClick={() => open(d)}
                  className="flex w-full items-start gap-2.5 px-3.5 py-2.5 text-left">
                    <FileText size={15} className="mt-0.5 flex-none text-zinc-muted" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[13px] font-semibold">{d.filename}</div>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        <Pill tone={STATUS_TONE[d.status] || 'muted'}>{d.status}</Pill>
                        <span className="text-[11px] text-zinc-muted">
                          {d.doc_type} · {MB(d.size_bytes)} · {d.chunk_count} chunk{d.chunk_count === 1 ? '' : 's'}
                        </span>
                      </div>
                      {/* Progress is only meaningful while indexing. */}
                      {['parsing', 'chunking', 'embedding'].includes(d.status) && d.chunk_count > 0 && (
                        <div className="mt-1.5">
                          <div className="h-1 overflow-hidden rounded-full bg-white/10">
                            <div className="h-full bg-violet-400"
                              style={{ width: `${Math.round((d.progress || 0) * 100)}%` }} />
                          </div>
                          <div className="mt-0.5 text-[10.5px] text-zinc-muted">
                            {d.embedded_count}/{d.chunk_count} chunks embedded
                          </div>
                        </div>
                      )}
                      {d.status === 'blocked' && (
                        <div className="mt-1 text-[11px] text-amber-200">
                          waiting for the embedding model — it resumes automatically
                        </div>
                      )}
                      {d.error && <div className="mt-1 text-[11px] text-red-300">{d.error}</div>}
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

        {/* Inline preview: the browser's own viewer renders the
            blob URL, so PDFs get page navigation, zoom and
            download without any extra dependency. */}
        {preview && (
          <div ref={previewRef} className="fixed inset-0 z-40 flex items-center justify-center bg-black/70 p-4"
            onClick={closePreview} role="presentation">
            <div role="dialog" aria-modal="true" aria-label={`Preview of ${preview.doc.filename}`}
              onClick={(e) => e.stopPropagation()}
              className="flex h-full w-full max-w-5xl flex-col rounded-[10px] border border-white/10 bg-[#0b0b0e]">
              <div className="flex items-center gap-2 border-b border-white/10 px-4 py-3">
                <FileText size={15} className="text-zinc-muted" />
                <span className="flex-1 truncate text-[14px] font-bold">{preview.doc.filename}</span>
                <a href={preview.url} download={preview.doc.filename}
                  aria-label={`Download ${preview.doc.filename}`}
                  className="rounded border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] hover:border-violet-400">
                  <Download size={12} className="inline" /> download
                </a>
                <button onClick={closePreview} aria-label="Close preview"
                  className="rounded px-2 py-1 text-zinc-400 hover:bg-white/5">×</button>
              </div>
              <iframe src={preview.url} title={`Preview of ${preview.doc.filename}`}
                className="min-h-0 flex-1 w-full rounded-b-[10px] bg-white" />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}