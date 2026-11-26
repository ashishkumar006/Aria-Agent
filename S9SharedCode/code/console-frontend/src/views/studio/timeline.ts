import type {
  StudioChange, StudioCheckpoint, StudioCheckpointRestoreResult,
} from '../../api';

/* Which checkpoint `/undo` and `/redo` act on.
 *
 * The server keeps ONE list for both directions. A restore captures the tree it
 * is about to replace as a checkpoint of its own (so a mistaken restore is
 * recoverable), which means the list holds two kinds of entry:
 *
 *   - a task's "before task N" / "before renaming x.py" state, and
 *   - the "before restoring …" snapshot a previous restore recorded.
 *
 * Undo has to walk the first kind only. Taking the newest entry blindly — which
 * is what this did — makes a second `/undo` restore the state captured by the
 * first one, so undo silently cancels itself instead of stepping back. Redo is
 * the mirror image: it restores the newest restore-capture, which is exactly
 * the tree the last undo replaced.
 *
 * Both take the list newest-first (the order `/api/studio/checkpoints` returns)
 * and answer `null` when there is nothing to do, so the caller has one string
 * to show instead of two code paths.
 */
export function isRestoreCapture(cp: Pick<StudioCheckpoint, 'turn' | 'name' | 'capture'>): boolean {
  return Boolean(cp.capture)
    || String(cp.turn || '').startsWith('undo-')
    || /^before restoring /i.test(String(cp.name || ''));
}

/** `/undo`: the newest state the walk has not passed yet. Task checkpoints
 *  first, then the snapshots a redo left behind — that is what makes undo able
 *  to take back a redo. */
export function pickUndo(cps: StudioCheckpoint[],
                         acted: string[] = []): StudioCheckpoint | null {
  const done = new Set(acted);
  return cps.find((c) => (!isRestoreCapture(c) || c.capture === 'undo')
                         && !done.has(c.id)) || null;
}

/** `/redo`: the newest snapshot an undo left behind and the walk has not
 *  passed yet. */
export function pickRedo(cps: StudioCheckpoint[],
                         acted: string[] = []): StudioCheckpoint | null {
  const done = new Set(acted);
  return cps.find((c) => c.capture === 'redo' && !done.has(c.id)) || null;
}

/** The checkpoint's own label with the restore boilerplate stripped, for a
 *  sentence a person can read: "before restoring undo cp-1a2b3c" → "cp-1a2b3c". */
export function restoreLabel(name: string): string {
  return String(name || '').replace(/^before restoring /i, '') || 'the undone work';
}

/** One change set: every file a single task left pending, in the order the
 *  panel shows them (biggest first, then by label). Files with no turn predate
 *  task tracking and share one "older work" group.
 *
 *  The group is the reviewable unit: its header says "this was one change" and
 *  carries the keep/undo that applies to all of it. */
export interface ChangeSet {
  /** The checkpoint the group's hunks were measured against — what "undo this
   *  change" would restore. This is the group's real identity. */
  key: string;
  turn: string;
  name: string;
  files: StudioChange[];
  hunks: number;
  added: number;
  removed: number;
}

export function groupChanges(rows: StudioChange[],
                              cps: StudioCheckpoint[]): ChangeSet[] {
  /* Keyed on the checkpoint id, NOT on the turn. A file edited by task 3 while
     still pending from task 2 keeps its original `turn` (the turn that took its
     baseline), but its hunks are recomputed against task 3's snapshot. Grouping
     by the turn therefore puts task 3's hunks under a "before task 2" header —
     and offers a restore of a checkpoint those hunks were never measured
     against. `checkpoint` is the snapshot actually used, so it is the honest
     key; `turn` stays for display and for callers that only have it. */
  const byKey = new Map<string, StudioChange[]>();
  for (const r of rows) {
    const key = r.checkpoint || r.turn || '';
    const list = byKey.get(key);
    if (list) list.push(r);
    else byKey.set(key, [r]);
  }
  return [...byKey.entries()]
    .map(([key, files]) => {
      const cp = cps.find((c) => c.id === key)
                 || cps.find((c) => c.turn === (files[0].turn || ''));
      return {
        key,
        turn: files[0].turn || '',
        name: cp?.name || (key ? `task ${key}` : 'older work'),
        files,
        hunks: files.reduce((n, f) => n + (f.hunks?.length || 0), 0),
        added: files.reduce((n, f) => n + (f.added || 0), 0),
        removed: files.reduce((n, f) => n + (f.removed || 0), 0),
      };
    })
    .sort((a, b) => b.files.length - a.files.length
                   || (cps.find((c) => c.id === b.key)?.when || 0)
                      - (cps.find((c) => c.id === a.key)?.when || 0)
                   || a.name.localeCompare(b.name));
}

/** One-line report of what a restore did, shared by `/undo`, `/redo` and the
 *  checkpoint list so the three cannot drift into three different stories.
 *  `skipped` reads as "left alone" because both of its causes are a deliberate
 *  refusal: outside the session's folders, or edited after the change being
 *  undone. */
export function restoreParts(r: StudioCheckpointRestoreResult | null): string[] {
  return [
    r?.restored?.length ? `restored ${r.restored.length} file(s)` : '',
    r?.removed?.length ? `removed ${r.removed.length} moved file(s)` : '',
    r?.skipped?.length ? `${r.skipped.length} left alone` : '',
    r?.failed?.length ? `${r.failed.length} failed` : '',
  ].filter(Boolean);
}