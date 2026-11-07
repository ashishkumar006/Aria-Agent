import { useState } from 'react';
import { Check, ChevronDown, ChevronRight, ExternalLink, Loader2, Undo2, X } from 'lucide-react';
import type { CodeAgentChange } from '../api';
import { api } from '../api';

/** One hunk of a unified diff, kept as display lines. */
export type HunkLine = { kind: 'add' | 'del' | 'ctx'; text: string };
export type HunkView = { index: number; header: string; lines: HunkLine[];
                         added: number; deleted: number };

/** Mirror of the server's `_code_hunks` for display.
 *
 *  The index has to line up with the server's, because "Undo this hunk" sends
 *  it straight to `/api/code/agent/revert-hunk`. Both read the same diff the
 *  server produced, so the only rule that matters is: count `@@` headers in
 *  order and ignore file headers. */
export function splitHunks(diff: string): HunkView[] {
  const out: HunkView[] = [];
  let cur: HunkView | null = null;
  for (const raw of (diff || '').split(/\r?\n/)) {
    if (raw.startsWith('@@')) {
      if (cur) out.push(cur);
      cur = { index: out.length, header: raw, lines: [], added: 0, deleted: 0 };
      continue;
    }
    if (!cur || raw.startsWith('---') || raw.startsWith('+++')) continue;
    if (raw.startsWith('+')) { cur.lines.push({ kind: 'add', text: raw.slice(1) }); cur.added++; }
    else if (raw.startsWith('-')) { cur.lines.push({ kind: 'del', text: raw.slice(1) }); cur.deleted++; }
    else if (raw.startsWith('\\')) continue;          // "\ No newline at end of file"
    else cur.lines.push({ kind: 'ctx', text: raw.replace(/^ /, '') });
  }
  if (cur) out.push(cur);
  return out;
}

/** The agent surface's right column: everything the agent wrote that the user
 *  has not accepted yet, per file and per hunk.
 *
 *  File-level Keep/Revert already existed in the IDE, but it lives in the
 *  editor's status strip — invisible when the transcript owns the window. Here
 *  it is the primary object, and each hunk carries its own undo, because a
 *  turn that got three edits right and one wrong should not cost the user all
 *  three. */
export function ChangesRail({ changes, busy, onKeep, onRevert,
                              onOpen, onResolved, onChanged }: {
  changes: CodeAgentChange[];
  busy: string | null;
  onKeep: (path: string) => void;
  onRevert: (path: string) => void;
  onOpen: (path: string) => void;
  /** The last hunk of a file was undone, so the server dropped the review
   *  entry. Without this the rail would keep offering controls for a change
   *  that no longer exists. */
  onResolved?: (path: string) => void;
  /** Any successful hunk undo, partial or complete. The editor's own line
   *  marks are built from the same diff, so they are stale the moment one
   *  hunk goes back — the parent has to re-read, not just the rail. */
  onChanged?: () => void;
}) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  const undoHunk = async (path: string, hunk: number) => {
    setErrors((e) => { const n = { ...e }; delete n[`${path}#${hunk}`]; return n; });
    try {
      const r = await api.codeRevertHunk(path, hunk);
      if (r && r.ok === false && r.error) {
        setErrors((e) => ({ ...e, [`${path}#${hunk}`]: r.error as string }));
      } else if (r && r.ok) {
        onChanged?.();
        if ((r.hunks_remaining ?? 0) === 0) onResolved?.(path);
      }
    } catch (err) {
      setErrors((e) => ({ ...e, [`${path}#${hunk}`]:
        err instanceof Error ? err.message : String(err) }));
    }
  };

  if (!changes.length) {
    /* Informational only, so it may stay width-gated: unlike the real list it
       asks nothing of the user, and a placeholder that overlays the composer on
       a narrow window would cost them the thing they need. */
    return (
      <aside aria-label="Changes"
             className="hidden w-[300px] flex-none overflow-y-auto border-l border-glass-border bg-surface-1 px-3 py-3 md:block">
        <p className="text-[11px] font-semibold tracking-wide text-zinc-500">CHANGES</p>
        <p className="mt-2 text-[11.5px] leading-relaxed text-zinc-muted">
          Nothing unreviewed. When the agent writes, each file lands here with
          its hunks — Keep accepts it, Undo on a single hunk rolls back just
          that part.
        </p>
      </aside>
    );
  }

  return (
    /* Below md there is no room for a third column, and dropping the rail at
       that width would drop the only way to accept or undo what the agent
       wrote — in this surface the editor column is hidden, so nothing else
       shows it. So it becomes a drawer over the transcript instead. */
    <aside aria-label="Changes"
           className="fixed inset-y-0 right-0 z-40 w-[min(340px,88vw)] overflow-y-auto border-l border-glass-border bg-surface-1 shadow-2xl md:static md:z-auto md:w-[300px] md:max-w-none md:shadow-none">
      <div className="sticky top-0 flex items-center gap-2 border-b border-glass-border bg-surface-1 px-3 py-2">
        <span className="text-[11px] font-semibold tracking-wide text-zinc-400">
          CHANGES
        </span>
        <span className="rounded bg-white/10 px-1.5 text-[10.5px] text-zinc-300">
          {changes.length}
        </span>
      </div>

      {changes.map((c) => {
        const hunks = splitHunks(String(c.diff || ''));
        const isOpen = !!open[c.path];
        const added = hunks.reduce((n, h) => n + h.added, 0);
        const deleted = hunks.reduce((n, h) => n + h.deleted, 0);
        const isBusy = busy === c.path;
        return (
          <div key={c.path} className="border-b border-glass-border/70">
            <div className="flex items-start gap-1.5 px-2.5 py-2">
              <button type="button"
                      onClick={() => setOpen((o) => ({ ...o, [c.path]: !o[c.path] }))}
                      aria-expanded={isOpen}
                      title={isOpen ? 'Hide the diff' : 'Show the diff'}
                      className="mt-0.5 flex-none text-zinc-muted hover:text-zinc-200">
                {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              </button>
              <div className="min-w-0 flex-1">
                <button type="button" onClick={() => onOpen(c.path)}
                        title={`Open ${c.path}`}
                        className="block w-full truncate text-left text-[11.5px] text-zinc-200 hover:underline">
                  {c.path}
                </button>
                <p className="mt-0.5 truncate text-[10.5px] text-zinc-muted">
                  {c.created ? 'created' : (c.tool || 'changed')}
                  {added > 0 && <span style={{ color: '#3fb950' }}> +{added}</span>}
                  {deleted > 0 && <span style={{ color: '#f85149' }}> −{deleted}</span>}
                </p>
              </div>
              {isBusy && <Loader2 size={12} className="mt-1 animate-spin text-zinc-400" />}
            </div>

            <div className="flex items-center gap-1.5 px-2.5 pb-2">
              <button type="button" disabled={isBusy} onClick={() => onRevert(c.path)}
                      title="Put the whole file back as it was before the agent touched it"
                      className="flex items-center gap-1 rounded border border-glass-border px-1.5 py-[2px] text-[10.5px] text-zinc-300 hover:border-rose-400/60 hover:text-rose-300 disabled:opacity-40">
                <Undo2 size={11} /> Revert file
              </button>
              <button type="button" disabled={isBusy} onClick={() => onKeep(c.path)}
                      title="Keep it and stop tracking this file"
                      className="flex items-center gap-1 rounded border border-emerald-500/40 px-1.5 py-[2px] text-[10.5px] text-emerald-300 hover:bg-emerald-500/10 disabled:opacity-40">
                <Check size={11} /> Keep
              </button>
              <button type="button" onClick={() => onOpen(c.path)}
                      title="Open in the editor"
                      className="ml-auto flex-none text-zinc-muted hover:text-zinc-200">
                <ExternalLink size={12} />
              </button>
            </div>

            {isOpen && (
              <div className="px-2.5 pb-2.5">
                {hunks.length === 0 && (
                  <p className="text-[11px] text-zinc-muted">
                    No line detail for this change.
                  </p>
                )}
                {hunks.map((h) => (
                  <div key={h.index} className="mb-1.5 overflow-hidden rounded border border-glass-border">
                    <div className="flex items-center gap-2 bg-white/[0.04] px-2 py-1">
                      <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-zinc-muted">
                        {h.header}
                      </span>
                      <button type="button"
                              onClick={() => void undoHunk(c.path, h.index)}
                              disabled={isBusy}
                              title="Undo just this hunk"
                              className="flex flex-none items-center gap-1 rounded border border-glass-border px-1.5 py-[1px] text-[10px] text-zinc-300 hover:border-rose-400/60 hover:text-rose-300 disabled:opacity-40">
                        <X size={10} /> Undo hunk
                      </button>
                    </div>
                    {errors[`${c.path}#${h.index}`] && (
                      <p className="border-t border-rose-500/30 bg-rose-500/10 px-2 py-1 text-[10.5px] text-rose-200">
                        {errors[`${c.path}#${h.index}`]}
                      </p>
                    )}
                    <pre className="max-h-56 overflow-auto px-2 py-1 font-mono text-[10.5px] leading-[1.45]">
                      {h.lines.map((l, i) => (
                        <div key={i}
                             className={l.kind === 'add'
                               ? 'whitespace-pre-wrap text-emerald-300'
                               : l.kind === 'del'
                                 ? 'whitespace-pre-wrap text-rose-300'
                                 : 'whitespace-pre-wrap text-zinc-muted'}>
                          {l.kind === 'add' ? '+ ' : l.kind === 'del' ? '- ' : '  '}{l.text}
                        </div>
                      ))}
                    </pre>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </aside>
  );
}

export default ChangesRail;
