import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, ChevronDown, ChevronRight, ExternalLink, Loader2, Undo2, X } from 'lucide-react';
import { api, type StudioChange } from '../../api';
import { splitHunks } from '../../components/ChangesRail';
import { fileVisual } from './fileIcons';

/* The inline review card a file link in the answer expands into.
 *
 * WHY IT LIVES IN THE TRANSCRIPT: clicking a path in the agent's reply used
 * to open the workbench in review mode — a full-screen Monaco diff beside an
 * already wide conversation, which reads as the app swallowing the answer to
 * show the file. The decision a person actually makes at a glance — keep it
 * or put it back — does not need the whole editor. This renders the same
 * server-computed hunks the review surface decides, as one compact section
 * per file: green for the lines the agent added, red for the ones it
 * removed. Opening the file itself stays one click away. */

const norm = (p: string) =>
  p.replace(/\\/g, '/').toLowerCase().replace(/\/+$/g, '');

const pickChange = (rows: StudioChange[] | null,
                    cleaned: string): StudioChange | undefined => {
  if (!rows || !rows.length) return undefined;
  const c = norm(cleaned);
  const exact = rows.find((r) => norm(r.path) === c);
  if (exact) return exact;
  const suffix = rows.filter((r) => norm(r.path).endsWith('/' + c));
  /* An unqualified suffix is still the best answer available; the alternative
     is pretending the file does not exist. */
  if (suffix.length >= 1) return suffix[0];
  if (!c.includes('/')) {
    const byName = rows.filter((r) => norm(r.path).split('/').pop() === c);
    /* A bare filename in prose is ambiguous by construction; the newest
       change under that name is the one the agent most likely meant. */
    if (byName.length) return [...byName].sort((a, b) => (b.when || 0) - (a.when || 0))[0];
  }
  return undefined;
};

export function InlineChange({ sid, refStr, onClose, onOpenInEditor, onDecision }: {
  sid: string;
  /** The raw reference the user clicked, exactly as it appeared. */
  refStr: string;
  onClose: () => void;
  /** Open the full review surface for the raw reference. */
  onOpenInEditor: (ref: string) => void;
  /** Any keep/revert decision landed — let the shell re-read its stores. */
  onDecision?: () => void;
}) {
  const cleaned = refStr.replace(/^(?:\.[\\/])+/, '').replace(/\\/g, '/');
  const [rows, setRows] = useState<StudioChange[] | null>(null);
  const [err, setErr] = useState('');
  /* Which decision is in flight: a hunk fingerprint, or 'all' for the
     file-level verbs. Empty string means idle. */
  const [busy, setBusy] = useState('');
  const [fold, setFold] = useState(false);

  const load = useCallback(async () => {
    try {
      const got = await api.studioChanges(sid);
      setRows(got?.changes || []);
      setErr(got?.error || '');
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [sid]);
  useEffect(() => { void load(); }, [load]);

  const match = useMemo(() => pickChange(rows, cleaned), [rows, cleaned]);
  const unified = useMemo(() => (match?.diff ? splitHunks(match.diff) : []),
                          [match]);
  /* The server's hunks carry the fingerprint ids the apply verb needs; the
     unified split carries the interleaved add/del lines for painting. Both
     count the same @@ headers in the same order, so they merge by index. */
  const hunkRows = useMemo(() => unified.map((h, i) => ({
    ...h, id: match?.hunks?.[i]?.id || '',
  })), [unified, match]);

  const added = match?.added ?? hunkRows.reduce((a, h) => a + h.added, 0);
  const removed = match?.removed ?? hunkRows.reduce((a, h) => a + h.deleted, 0);
  const visual = fileVisual(match?.path || cleaned);
  const FIcon = visual.icon;
  const base = (match?.path || cleaned).split('/').pop() || cleaned;

  const apply = useCallback(async (action: 'keep' | 'revert', hunkId = '') => {
    if (!match) return;
    setBusy(hunkId || 'all');
    setErr('');
    try {
      if (hunkId) {
        const r = await api.studioHunkApply(sid, match.path, hunkId, action);
        if (r && r.ok === false) setErr(String(r.error || 'the decision was refused'));
      } else if (action === 'keep') {
        const r = await api.codeKeep(match.path, sid);
        if (r && r.ok === false) setErr(String(r.error || 'the keep was refused'));
      } else {
        const r = await api.codeRevert(match.path, sid);
        if (r && r.ok === false) setErr(String(r.error || 'the revert was refused'));
      }
      await load();
      onDecision?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  }, [match, sid, load, onDecision]);

  /* Reading — one compact row, so a slow disk looks like progress, not a
     swallowed click. */
  if (rows === null && !err) {
    return (
      <div className="my-2 flex items-center gap-2 rounded-[10px] border border-glass-border bg-surface-1 px-3 py-2 text-[11.5px] text-zinc-muted">
        <Loader2 size={12} className="animate-spin" /> reading {base}…
      </div>
    );
  }

  /* The file is not unreviewed — most honest answer we can give is what a
     file link means everywhere else: opening the file. */
  if (!match) {
    return (
      <div className="my-2 flex min-w-0 items-center gap-2 rounded-[10px] border border-glass-border bg-surface-1 px-2.5 py-2 text-[11.5px] text-zinc-300">
        <FIcon size={13} color={visual.cls} className="flex-none" />
        <span className="min-w-0 flex-1 truncate" title={cleaned}>
          {base} — nothing unreviewed for this file
        </span>
        <button type="button" onClick={() => onOpenInEditor(refStr)}
                className="flex flex-none items-center gap-1 rounded-md border border-glass-border px-2 py-1 text-[10.5px] text-zinc-300 transition-colors hover:bg-white/5 hover:text-zinc-100">
          <ExternalLink size={11} /> open in editor
        </button>
        <button type="button" onClick={onClose} aria-label="Close the diff"
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-200">
          <X size={12} />
        </button>
      </div>
    );
  }

  const blocked = busy !== '';
  return (
    <div className="my-2 overflow-hidden rounded-[10px] border border-glass-border bg-surface-1">
      {/* One section per file. Header: icon, name, counts, and the three
          decisions — keep everything, revert everything, open the file. The
          file verbs are icon-only: the file-refs they sit next to truncate,
          and "inline_probe.txt" deserves the width before the buttons. */}
      <div className="flex min-w-0 items-center gap-1.5 px-2.5 py-1.5">
        <FIcon size={13} color={visual.cls} className="flex-none" />
        <button type="button" onClick={() => onOpenInEditor(match.path)}
                title={`${match.path} — open in the editor`}
                className="min-w-0 flex-1 truncate text-left font-mono text-[12px] text-zinc-100 transition-colors hover:text-zinc-300">
          {base}
        </button>
        {match.created && (
          <span className="flex-none rounded border border-ok/30 bg-ok-dim px-1 py-px font-mono text-[9px] font-semibold uppercase tracking-wide text-ok">new</span>
        )}
        <span className="flex-none font-mono text-[10.5px] tabular-nums">
          {added > 0 && <span className="text-ok">+{added}</span>}
          {added > 0 && removed > 0 && ' '}
          {removed > 0 && <span className="text-danger">−{removed}</span>}
        </span>
        <button type="button" disabled={blocked} onClick={() => void apply('keep')}
                title="Keep the whole file — the marks come off, the bytes stay"
                aria-label="Keep the whole file"
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md border border-ok/40 bg-ok-dim text-ok transition-colors hover:bg-ok/20 disabled:opacity-40">
          {busy === 'all' ? <Loader2 size={11} className="animate-spin" /> : <Check size={12} />}
        </button>
        <button type="button" disabled={blocked} onClick={() => void apply('revert')}
                title="Put the file back as it was before the agent touched it"
                aria-label="Revert the whole file"
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md border border-danger/40 bg-danger-dim text-danger transition-colors hover:bg-danger/20 disabled:opacity-40">
          <Undo2 size={12} />
        </button>
        <button type="button" onClick={() => setFold((v) => !v)}
                aria-expanded={!fold}
                title={fold ? 'Show the diff' : 'Hide the diff'}
                aria-label={fold ? 'Show the diff' : 'Hide the diff'}
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-100">
          {fold ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
        </button>
        <button type="button" onClick={onClose} aria-label="Collapse the diff"
                className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-200">
          <X size={12} />
        </button>
      </div>
      {err && (
        <p role="alert"
           className="border-t border-danger/30 bg-danger-dim px-3 py-1.5 text-[11px] text-danger">
          {err}
        </p>
      )}
      {!fold && (
        <div className="border-t border-white/5">
          {hunkRows.length === 0 && (
            <p className="px-3 py-2 text-[11px] text-zinc-muted">
              No line detail — the file matches its baseline.
            </p>
          )}
          {hunkRows.map((h) => (
            <div key={h.index} className="border-b border-white/5 last:border-b-0">
              {/* Hunk header: where it lands, what it did, and the two hunk
                  decisions. The fingerprint id is what makes the undo a
                  decision about THIS hunk and not about whatever moved. */}
              <div className="flex items-center gap-2 bg-white/[0.03] px-3 py-1">
                <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-zinc-muted"
                      title={h.header}>
                  {h.header}
                  <span className="text-zinc-600"> · </span>
                  <span className="text-emerald-400">+{h.added}</span>{' '}
                  <span className="text-rose-400">−{h.deleted}</span>
                </span>
                {!!h.id && (
                  <>
                    <button type="button" disabled={blocked}
                            onClick={() => void apply('keep', h.id)}
                            title="Keep just this hunk"
                            className="flex flex-none items-center gap-1 rounded border border-emerald-500/40 px-1.5 py-0.5 text-[10px] text-emerald-300 transition-colors hover:bg-emerald-500/10 disabled:opacity-40">
                      {busy === h.id ? <Loader2 size={10} className="animate-spin" /> : <Check size={10} />} Keep
                    </button>
                    <button type="button" disabled={blocked}
                            onClick={() => void apply('revert', h.id)}
                            title="Undo just this hunk"
                            className="flex flex-none items-center gap-1 rounded border border-rose-500/40 px-1.5 py-0.5 text-[10px] text-rose-300 transition-colors hover:bg-rose-500/10 disabled:opacity-40">
                      <Undo2 size={10} /> Undo
                    </button>
                  </>
                )}
              </div>
              <pre className="max-h-56 overflow-auto py-0.5 font-mono text-[11px] leading-[1.55]">
                {h.lines.map((l, i) => (
                  <div key={i}
                       className={'whitespace-pre-wrap px-3 '
                         + (l.kind === 'add'
                           ? 'bg-emerald-400/[0.08] text-emerald-300'
                           : l.kind === 'del'
                             ? 'bg-rose-400/[0.08] text-rose-300'
                             : 'text-zinc-500')}>
                    <span className="select-none opacity-50">{l.kind === 'add' ? '+ ' : l.kind === 'del' ? '- ' : '  '}</span>{l.text}
                  </div>
                ))}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
