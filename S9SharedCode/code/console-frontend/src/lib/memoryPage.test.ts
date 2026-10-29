import { describe, expect, it } from 'vitest';
import { mergeMemoryPage } from './memoryPage';
import type { MemItem } from '../api';

const row = (id: string, descriptor = id): MemItem => ({ id, descriptor });

describe('memory pagination', () => {
  it('appends the next page in order', () => {
    const out = mergeMemoryPage([row('a'), row('b')], [row('c'), row('d')]);
    expect(out.map((i) => i.id)).toEqual(['a', 'b', 'c', 'd']);
  });

  it('does not repeat a row when the list shifted between requests', () => {
    // Page 1 ended at "b"; a memory was written before page 2 was requested,
    // so page 2 starts at "b" again.
    const out = mergeMemoryPage([row('a'), row('b')], [row('b'), row('c')]);
    expect(out.map((i) => i.id)).toEqual(['a', 'b', 'c']);
  });

  it('keeps rows without an id rather than dropping them silently', () => {
    const out = mergeMemoryPage([], [{ descriptor: 'unsaved' }, row('a')]);
    expect(out).toHaveLength(2);
  });

  it('is a no-op when the next page is empty', () => {
    const prev = [row('a')];
    expect(mergeMemoryPage(prev, [])).toBe(prev);
  });
});
