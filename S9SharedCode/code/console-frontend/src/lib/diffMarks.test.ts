import { describe, expect, it } from 'vitest';
import { parseUnifiedMarks } from './diffMarks';

describe('parseUnifiedMarks', () => {
  it('marks added lines with their new-file numbers', () => {
    const d = [
      '--- a/greet.py',
      '+++ b/greet.py',
      '@@ -0,0 +1,2 @@',
      '+def hello():',
      "+    return 'hi'",
    ].join('\n');
    expect(parseUnifiedMarks(d)).toEqual({
      added: [1, 2], modified: [], removedAt: [], removed: 0, deleted: 0,
    });
  });

  it('a replacement is one yellow edit, not a red deletion', () => {
    const d = [
      '--- a/x.py',
      '+++ b/x.py',
      '@@ -1,4 +1,3 @@',
      ' keep',
      '-gone one',
      '-gone two',
      '+replacement',
      ' tail',
      ' tail2',
    ].join('\n');
    // new file: 1 keep, 2 replacement, 3 tail, 4 tail2
    expect(parseUnifiedMarks(d)).toEqual({
      added: [], modified: [2], removedAt: [], removed: 2, deleted: 0,
    });
  });

  it('a replacement block marks its first line and adds the rest', () => {
    const d = [
      '@@ -1,3 +1,4 @@',
      ' keep',
      '-old a',
      '-old b',
      '+new a',
      '+new b',
      ' tail',
    ].join('\n');
    // new: 1 keep, 2 new a (edit), 3 new b (addition), 4 tail
    expect(parseUnifiedMarks(d)).toEqual({
      added: [3], modified: [2], removedAt: [], removed: 2, deleted: 0,
    });
  });

  it('handles multiple hunks independently', () => {
    const d = [
      '@@ -1,2 +1,3 @@',
      ' a',
      '+b1',
      ' c',
      '@@ -9,1 +10,1 @@',
      '-old',
      '+new',
    ].join('\n');
    expect(parseUnifiedMarks(d)).toEqual({
      added: [2], modified: [10], removedAt: [], removed: 1, deleted: 0,
    });
  });

  it('handles a pure deletion hunk: red point, nothing yellow or green', () => {
    const d = [
      '@@ -3,3 +3,1 @@',
      ' a',
      '-x',
      '-y',
      ' b',
    ].join('\n');
    expect(parseUnifiedMarks(d)).toEqual({
      added: [], modified: [], removedAt: [3], removed: 2, deleted: 2,
    });
  });

  it('empty or header-only diffs are empty', () => {
    expect(parseUnifiedMarks('')).toEqual({
      added: [], modified: [], removedAt: [], removed: 0, deleted: 0,
    });
    expect(parseUnifiedMarks('--- a\n+++ b')).toEqual({
      added: [], modified: [], removedAt: [], removed: 0, deleted: 0,
    });
  });
});
