import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type StudioSearchMatch } from '../../api';
import { Label } from './chrome';

/** Find in files, with expressions and a two-step replace.
 *
 *  The replace flow is deliberately two actions, not one: the preview lists
 *  the files and counts the occurrences, and the apply carries the token that
 *  preview was issued with. The server then re-reads every file and refuses
 *  the whole batch if any of them changed in between — so what lands on disk
 *  is what was approved, and a race with the agent shows up as a refusal
 *  rather than as a half-applied edit.
 *
 *  Regex is on by default with the reason written next to it: an unsafe
 *  expression is refused by the server with an explanation, and the message
 *  is shown here rather than turned into "0 results".
 */
export function SearchReplace({ sid, roots, onOpenPath, onChanged }: {
  sid: string;
  roots: { path: string; name: string }[];
  onOpenPath: (path: string, line?: number) => void;
  onChanged: () => void;
}) {
  const [query, setQuery] = useState('');
  const [repl, setRepl] = useState('');
  /* Off by default: a search box that starts in regex mode turns `def foo(x)`
     into a group reference and reports "no matches" for text that is plainly
     on screen. The toggle is right there for the people who want it. */
  const [regex, setRegex] = useState(false);
  const [caseOn, setCaseOn] = useState(false);
  const [glob, setGlob] = useState('');
  const [showReplace, setShowReplace] = useState(false);
  const [hits, setHits] = useState<StudioSearchMatch[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [preview, setPreview] = useState<{
    token: string; files: { path: string; count: number }[]; count: number;
    refused: string[];
  } | null>(null);
  const ctrl = useRef<AbortController | null>(null);

  useEffect(() => () => ctrl.current?.abort(), []);

  const find = useCallback(async () => {
    const q = query.trim();
    ctrl.current?.abort();
    const mine = new AbortController();
    ctrl.current = mine;
    if (!q) { setHits([]); setError(''); setPreview(null); return; }
    setBusy(true);
    setError('');
    try {
      const r = await api.studioSearch(sid, q,
        { regex, case: caseOn, glob: glob.trim() || undefined }, mine.signal);
      setHits(r.matches || []);
      setPreview(null);
      if (r.error) setError(r.error);
      else if (r.truncated) setNote(`showing ${r.count} of more — the match or `
        + `file budget was reached (${r.scanned} files scanned`
        + (r.binary_skipped ? `, ${r.binary_skipped} unreadable/binary skipped` : '')
        + ')');
      else setNote(`${r.count} match(es) in ${r.scanned} file(s) scanned`
        + (r.binary_skipped ? ` · ${r.binary_skipped} skipped as binary` : ''));
    } catch (e) {
      if (mine.signal.aborted) return;
      setHits([]);
      setError(e instanceof Error ? e.message : 'the search failed');
    } finally {
      setBusy(false);
    }
  }, [query, regex, caseOn, glob, sid]);

  const doPreview = useCallback(async () => {
    if (!hits.length || !repl.length && repl !== '') return;
    setBusy(true);
    setError('');
    try {
      const r = await api.studioReplacePreview(sid, {
        hits, pattern: query.trim(), replacement: repl, regex, case: caseOn });
      if (r.error) { setError(r.error); setPreview(null); return; }
      setPreview({
        token: String(r.token || ''),
        files: (r.files || []).map((f) => ({ path: f.path, count: f.count })),
        count: Number(r.count || 0),
        refused: r.refused || [],
      });
      if (r.note) setNote(r.note);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'the preview failed');
    } finally {
      setBusy(false);
    }
  }, [hits, query, repl, regex, caseOn, sid]);

  const doApply = useCallback(async () => {
    if (!preview?.token) return;
    setBusy(true);
    setError('');
    try {
      const r = await api.studioReplaceApply(sid, {
        hits, pattern: query.trim(), replacement: repl, regex, case: caseOn,
        token: preview.token });
      if (r.error) { setError(r.error); return; }
      setPreview(null);
      const verdict = `replaced ${r.applied} file(s)`
        + (r.stale?.length ? ` — ${r.stale.length} skipped because the file `
            + 'changed after the preview' : '')
        + (r.checkpoint ? ' · undoable from the Changes panel' : '');
      /* Awaited, and the note set afterwards: the re-search writes its own
         note, so the sentence that matters — what the replace actually did —
         would otherwise vanish under "0 matches". */
      await find();
      setNote(verdict);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'the replace failed');
    } finally {
      setBusy(false);
    }
  }, [preview, hits, query, repl, regex, caseOn, sid, find, onChanged]);

  const byFile = new Map<string, StudioSearchMatch[]>();
  for (const h of hits) {
    const list = byFile.get(h.path) || [];
    list.push(h);
    byFile.set(h.path, list);
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-none gap-1 px-2 py-1.5">
        <input value={query}
               onChange={(e) => setQuery(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter') void find(); }}
               placeholder={regex ? 'regular expression' : 'text in any folder here'}
               aria-label="Search the workspace"
               className="min-w-0 flex-1 rounded-md border border-glass-border bg-surface-2 px-1.5 py-1 text-[12px] outline-none" />
        <button type="button" onClick={() => void find()} disabled={busy}
                className="rounded-lg border border-glass-border px-1.5 text-[11px] text-zinc-200 disabled:opacity-40">
          {busy ? '…' : 'find'}
        </button>
        <button type="button" aria-pressed={showReplace}
                aria-label="Show the replace controls"
                onClick={() => setShowReplace((v) => !v)}
                title="Show the replace controls"
                className={'rounded-md border px-1.5 text-[11px] '
                  + (showReplace ? 'border-accent/50 text-zinc-100'
                    : 'border-glass-border text-zinc-muted hover:bg-white/5')}>
          replace
        </button>
      </div>

      <div className="flex flex-none flex-wrap items-center gap-1.5 px-2 pb-1 text-[11px] text-zinc-muted">
        <Toggle on={regex} set={setRegex} label="regex" title="Treat the query as a regular expression" />
        <Toggle on={caseOn} set={setCaseOn} label="match case" />
        <input value={glob} onChange={(e) => setGlob(e.target.value)}
               placeholder="*.py" aria-label="File filter"
               className="w-[70px] rounded-md border border-glass-border bg-surface-2 px-1 py-0.5 text-[11px] outline-none" />
        {/* No second run button: `find` above and Enter both re-run, and a
            third way to do the same thing is clutter, not affordance. */}
      </div>

      {showReplace && (
        <div className="flex flex-none gap-1 px-2 pb-1.5">
          <input value={repl} onChange={(e) => setRepl(e.target.value)}
                 placeholder={regex ? 'replacement — \\1 back-references work' : 'replacement'}
                 aria-label="Replacement text"
                 className="min-w-0 flex-1 rounded-md border border-glass-border bg-surface-2 px-1.5 py-1 text-[12px] outline-none" />
          <button type="button" disabled={!hits.length || busy}
                  onClick={() => void doPreview()}
                  className="rounded-lg border border-glass-border px-1.5 text-[11px] text-zinc-200 hover:bg-white/5 disabled:opacity-40">
            preview
          </button>
        </div>
      )}

      {showReplace && preview && (
        <div className="flex-none border-y border-glass-border bg-surface-1/60 px-2 py-1.5">
          <p className="text-[12px] text-zinc-200">
            {preview.count} occurrence(s) in {preview.files.length} file(s)
            {preview.refused.length
              ? ` · ${preview.refused.length} refused` : ''}
          </p>
          <ul className="mt-1 max-h-[120px] overflow-auto">
            {preview.files.map((f) => (
              <li key={f.path} className="truncate text-[11px] text-zinc-300"
                  title={f.path}>{f.count}× {f.path}</li>
            ))}
          </ul>
          <div className="mt-1.5 flex gap-2">
            <button type="button" onClick={() => void doApply()} disabled={busy}
                    className="rounded-lg bg-accent px-2.5 py-0.5 text-[12px] font-semibold text-black transition-colors hover:bg-accent/90 disabled:opacity-40">
              apply
            </button>
            <button type="button" onClick={() => setPreview(null)}
                    className="rounded-lg border border-glass-border px-2 py-0.5 text-[12px] text-zinc-300 hover:bg-white/5">
              cancel
            </button>
            <span className="min-w-0 flex-1 self-center text-[11px] leading-snug text-zinc-muted">
              nothing has been written yet.
            </span>
          </div>
        </div>
      )}

      {error && (
        <p role="alert" className="flex-none border-y border-danger/30 bg-danger-dim px-2 py-1 text-[11px] text-danger">
          {error}
        </p>
      )}

      <div className="min-h-0 flex-1 overflow-auto px-1 pb-2">
        {!roots.length && (
          <p className="px-2 py-1 text-[12px] text-zinc-muted">
            Nothing to search: this session holds no folders.
          </p>
        )}
        {!!roots.length && !query.trim() && !busy && (
          /* A blank column under two inputs reads as broken. One line says the
             scope and the gesture; it used to be four lines of instructions. */
          <p className="px-2 py-1.5 text-[12px] text-zinc-muted">
            Search {roots.length} folder{roots.length === 1 ? '' : 's'} —
            Enter runs, a hit opens at that line.
          </p>
        )}
        {!!note && !error && (
          <p className="px-2 py-1 text-[11px] leading-snug text-zinc-muted">{note}</p>
        )}
        {Array.from(byFile.entries()).map(([path, list]) => (
          <div key={path}>
            <p className="px-2 pt-1.5" title={path}>
              <Label className="block truncate">{path} · {list.length}</Label>
            </p>
            {list.map((h, i) => (
              <button key={`${h.line}:${i}`} type="button"
                      onClick={() => onOpenPath(path, h.line)}
                      className="block w-full truncate rounded px-2 py-0.5 text-left text-[12px] text-zinc-300 hover:bg-white/5"
                      title={`${path}:${h.line}:${h.col} ${h.text}`}>
                <span className="text-zinc-muted">{h.line}:{h.col}</span>
                {' '}{h.text.slice(0, 100)}
              </button>
            ))}
          </div>
        ))}
        {!!query.trim() && !hits.length && !busy && !error && (
          <p className="px-2 py-1 text-[12px] text-zinc-muted">no matches</p>
        )}
      </div>
    </div>
  );
}

function Toggle({ on, set, label, title }: {
  on: boolean; set: (v: boolean) => void; label: string; title?: string;
}) {
  return (
    <button type="button" aria-pressed={on} title={title} onClick={() => set(!on)}
            className={'rounded-md border px-1.5 py-0.5 '
              + (on ? 'border-accent/50 bg-accent/10 text-zinc-100'
                : 'border-glass-border text-zinc-muted hover:bg-white/5')}>
      {label}
    </button>
  );
}
