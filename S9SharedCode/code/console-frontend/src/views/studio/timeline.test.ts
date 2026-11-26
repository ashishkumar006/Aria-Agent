import { describe, expect, it } from 'vitest';
import type { StudioChange, StudioCheckpoint } from '../../api';
import {
  groupChanges, isRestoreCapture, pickRedo, pickUndo, restoreLabel, restoreParts,
} from './timeline';

type Row = { id: string; name: string; turn: string; capture?: string;
             when: number; files: number; bytes: number };

const task = (id: string, name: string): Row =>
  ({ id, name, turn: 'task-1', when: 0, files: 1, bytes: 10 });
const capture = (id: string, name: string, dir: 'redo' | 'undo'): Row =>
  ({ id, name, turn: `undo-${id}`, capture: dir, when: 0, files: 1, bytes: 10 });

/* The reason this module exists: undo and redo share one checkpoint list, a
 * restore writes a snapshot into it, and checkpoints are kept rather than
 * popped. These pin which entry each direction takes — including the cases
 * that used to break: a second undo, a redo stealing its own snapshot, and undo
 * after a redo. */
describe('studio undo/redo timeline', () => {
  const afterEdit = [task('cp-b', 'before task 1'), task('cp-a', 'before task 0')];
  const redoSnap = capture('cp-u', 'before restoring before task 1', 'redo');

  it('undo takes the newest task checkpoint', () => {
    expect(pickUndo(afterEdit)?.id).toBe('cp-b');
  });

  it('tells a restore snapshot from a task checkpoint', () => {
    expect(isRestoreCapture(redoSnap)).toBe(true);
    expect(isRestoreCapture(task('cp-x', 'before renaming a.py'))).toBe(false);
  });

  it('a second undo goes back again instead of undoing the undo', () => {
    // The snapshot a redo will replay is not an undo target.
    expect(pickUndo([redoSnap, ...afterEdit])?.id).toBe('cp-b');
    // The first undo acted on cp-b, so the next undo steps past it.
    expect(pickUndo([redoSnap, ...afterEdit], ['cp-b'])?.id).toBe('cp-a');
    expect(pickUndo([redoSnap, ...afterEdit], ['cp-b', 'cp-a'])).toBeNull();
  });

  it('redo takes only the snapshots an undo left behind', () => {
    expect(pickRedo([redoSnap, ...afterEdit])?.id).toBe('cp-u');
    expect(pickRedo(afterEdit)).toBeNull();
    expect(pickRedo([redoSnap, ...afterEdit], ['cp-u'])).toBeNull();
  });

  it('a snapshot a redo wrote is undone, not redone', () => {
    const undoneSnap = capture('cp-r', 'before restoring before task 1', 'undo');
    const cps = [undoneSnap, redoSnap, ...afterEdit];
    // ...which reads as "nothing to redo", and undo can take the redo back.
    expect(pickRedo(cps, ['cp-u'])).toBeNull();
    expect(pickUndo(cps, ['cp-u'])?.id).toBe('cp-r');
  });

  it('labels the redo result for the transcript', () => {
    expect(restoreLabel('before restoring before task 1')).toBe('before task 1');
  });

  it('says what a restore did in one line', () => {
    expect(restoreParts({
      restored: ['a.py'], removed: ['b.py'],
      skipped: [{ path: 'c.py', reason: 'outside this session' }],
      failed: [],
    })).toEqual(['restored 1 file(s)', 'removed 1 moved file(s)', '1 left alone']);
    expect(restoreParts(null)).toEqual([]);
  });
});

/* Change sets: the unit a person actually reviews. A task that touched four
 * files is one change; splitting it into four cards is how a third of it gets
 * kept by accident. */
describe('studio change sets', () => {
  const change = (path: string, turn: string, added = 0, removed = 0,
                  hunks = 1, checkpoint = '') => ({
    path, turn, added, removed, checkpoint,
    hunks: Array.from({ length: hunks }, (_, i) => ({ id: `${path}#${i}` })),
  }) as unknown as StudioChange;

  it('groups the files one task touched into one change', () => {
    const sets = groupChanges([
      change('src/b.py', 'task-2', 4, 1, 1, 'cp-2'),
      change('src/a.py', 'task-2', 2, 0, 1, 'cp-2'),
      change('src/c.py', 'task-1', 1, 1, 1, 'cp-1'),
    ], [{ id: 'cp-2', name: 'before renaming b.py', turn: 'task-2',
          when: 0, files: 1, bytes: 1 } as unknown as StudioCheckpoint]);

    expect(sets).toHaveLength(2);
    expect(sets[0].key).toBe('cp-2');
    expect(sets[0].turn).toBe('task-2');
    expect(sets[0].name).toBe('before renaming b.py');
    expect(sets[0].files.map((f) => f.path)).toEqual(['src/b.py', 'src/a.py']);
    expect([sets[0].added, sets[0].removed, sets[0].hunks])
      .toEqual([6, 1, 2]);
  });

  it('groups on the baseline, not the turn that first saw the file', () => {
    /* A file edited by task 3 while still pending from task 2 keeps turn=task-2,
       but its hunks come from task 3's snapshot. The header must name that
       snapshot, or "undo this change" would restore a different one. */
    const cps = [
      { id: 'cp-3', name: 'before task 3', turn: 'task-3',
        when: 200, files: 1, bytes: 1 },
      { id: 'cp-2', name: 'before task 2', turn: 'task-2',
        when: 100, files: 1, bytes: 1 },
    ] as unknown as StudioCheckpoint[];
    const sets = groupChanges([
      change('src/a.py', 'task-2', 5, 0, 1, 'cp-3'),
      change('src/b.py', 'task-2', 1, 0, 1, 'cp-2'),
    ], cps);

    expect(sets.map((s) => [s.key, s.name])).toEqual([
      ['cp-3', 'before task 3'],
      ['cp-2', 'before task 2'],
    ]);
  });

  it('labels work that predates task tracking without inventing a task', () => {
    const sets = groupChanges([change('legacy.py', '')], []);
    expect(sets[0].name).toBe('older work');
    expect(sets[0].key).toBe('');
  });
});