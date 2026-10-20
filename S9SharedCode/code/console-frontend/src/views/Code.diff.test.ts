import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { lineDiff } from '../components/CodeIntel';

/**
 * The save-review diff.
 *
 * The bug this pins: `diffRows` was built with
 * `lineDiff(body.split('\n'), pane.body.split('\n'))`, but `body` IS
 * `pane.body` — the live buffer. So the review diffed the buffer against
 * itself and every save showed "0 added, 0 removed", which is worse than no
 * diff: it tells the user their edit is empty right before they approve it.
 *
 * The on-disk original is `pane.base`; the diff must be base -> buffer.
 *
 * Source-level for the call site (matching AgentPanel.persistence.test.ts),
 * behavioural for the diff itself.
 */
const CODE = readFileSync(resolve(__dirname, 'Code.tsx'), 'utf-8');

describe('save-review diff', () => {
  it('diffs the on-disk base against the live buffer', () => {
    expect(CODE).toMatch(/lineDiff\(pane\.base\.split\('\\n'\), pane\.body\.split\('\\n'\)\)/);
    // The old shape compared the buffer with itself.
    expect(CODE).not.toMatch(/lineDiff\(body\.split\('\\n'\), pane\.body\.split/);
  });

  it('reports an insertion as add / deletion as del', () => {
    const rows = lineDiff(['a', 'b', 'c'], ['a', 'x', 'b', 'c']);
    expect(rows.filter((r) => r.kind === 'add').map((r) => r.text)).toEqual(['x']);
    expect(rows.filter((r) => r.kind === 'del')).toEqual([]);
  });

  it('reports a removal as del and a replacement as one of each', () => {
    expect(lineDiff(['a', 'b'], ['a']).filter((r) => r.kind === 'del').map((r) => r.text))
      .toEqual(['b']);
    const rep = lineDiff(['a', 'b', 'c'], ['a', 'B', 'c']);
    expect(rep.filter((r) => r.kind === 'add').map((r) => r.text)).toEqual(['B']);
    expect(rep.filter((r) => r.kind === 'del').map((r) => r.text)).toEqual(['b']);
  });

  it('is empty, not wrong, when the two sides match', () => {
    expect(lineDiff(['a', 'b'], ['a', 'b']).every((r) => r.kind === 'ctx')).toBe(true);
  });
});
