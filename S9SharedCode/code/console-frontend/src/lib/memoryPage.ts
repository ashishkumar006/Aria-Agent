import type { MemItem } from '../api';

/** Append a page without duplicating rows.
 *
 *  Offset pagination has a race: a memory written between two page requests
 *  shifts every later row down, so page 2 can repeat the last row of page 1.
 *  The API's ids make the append idempotent, which is cheaper and more
 *  honest than hiding the duplicate after the fact.
 */
export function mergeMemoryPage(prev: MemItem[], next: MemItem[]): MemItem[] {
  if (!next.length) return prev;
  const seen = new Set(prev.map((i) => i.id).filter(Boolean));
  return [...prev, ...next.filter((i) => !i.id || !seen.has(i.id))];
}
