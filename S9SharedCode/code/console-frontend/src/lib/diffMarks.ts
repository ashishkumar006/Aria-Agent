/* Which lines of a file did the agent just change?
 *
 * The write's diff is the record of what changed - the pre-edit content is
 * gone - so parsing the hunks gives the editor everything it needs for
 * Cursor-style marks without keeping a second copy of the file:
 *
 *  - `added`      1-based line numbers in the NEW file the agent added
 *                 outright (green);
 *  - `modified`   the first line of a replacement - an addition directly
 *                 following removed lines (yellow: the agent edited this
 *                 line);
 *  - `removedAt`  the line in the NEW file after which lines were removed
 *                 with nothing put back (red; a normal editor cannot colour
 *                 text that no longer exists, so the deletion point is the
 *                 mark), one entry per deleted block;
 *  - `removed`    total lines removed, for the "−N" in the change bar.
 *  - `deleted`    the lines behind those red points - the subset of `removed`
 *                 that left nothing in its place. A replacement's old line is
 *                 in `removed` but not here: the yellow line stands in its
 *                 place, so calling it deleted would count a mark the user
 *                 cannot see.
 *
 * Only hunk headers are trusted for position; the body walk counts '+'
 * lines into the new file and '-' lines out of it, which is exactly what
 * the @@ ranges describe. */

export interface DiffMarks {
  added: number[];
  modified: number[];
  removedAt: number[];
  removed: number;
  deleted: number;
}

const HUNK = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

export function parseUnifiedMarks(diff: string): DiffMarks {
  const added: number[] = [];
  const modified: number[] = [];
  const removedAt: number[] = [];
  let removed = 0;
  let deleted = 0;
  if (!diff) return { added, modified, removedAt, removed, deleted };

  let newLine = 0;
  let inHunk = false;
  let pendingRemoved = 0;
  const flushDeletion = () => {
    if (pendingRemoved > 0) {
      removedAt.push(Math.max(1, newLine - 1));
      deleted += pendingRemoved;
    }
    pendingRemoved = 0;
  };
  for (const raw of diff.split(/\r?\n/)) {
    const h = HUNK.exec(raw);
    if (h) {
      if (inHunk) flushDeletion();
      newLine = Number(h[1]);
      inHunk = true;
      continue;
    }
    if (!inHunk) continue;
    if (raw.startsWith('+++') || raw.startsWith('---')) continue;
    if (raw.startsWith('+')) {
      if (pendingRemoved > 0) {
        /* A replacement: the removed block came back as this line. It is an
           edit, not an outright addition, and its removal point is not shown
           in red - the yellow mark already says the line changed. */
        modified.push(newLine);
        pendingRemoved = 0;
      } else {
        added.push(newLine);
      }
      newLine += 1;
    } else if (raw.startsWith('-')) {
      removed += 1;
      pendingRemoved += 1;
    } else if (raw.startsWith(' ') || raw === '') {
      flushDeletion();
      newLine += 1;
    }
  }
  if (inHunk) flushDeletion();
  return { added, modified, removedAt, removed, deleted };
}
