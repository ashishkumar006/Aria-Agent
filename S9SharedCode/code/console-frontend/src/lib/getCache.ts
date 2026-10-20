/**
 * A tiny read-through cache in front of the GET pipeline.
 *
 * Every section refetched on each navigation, because `<Routes>` remounted the
 * view and the view fetched in a `useEffect` on mount. Keeping the component
 * alive fixes the remount (see `KeepAlive.tsx`), but the network round-trip
 * still happened, so a section switch still showed a spinner over stale data
 * it already had.
 *
 * This sits inside `api.ts`'s single `j()` funnel, so every view benefits
 * without touching a single call site.
 *
 * Rules, deliberately conservative:
 *  - GET only. A POST must always reach the server.
 *  - Keyed by full URL including its query string.
 *  - `ttlMs` bounds staleness; 0 disables caching for that key.
 *  - In-flight requests are shared, so ten components asking for the same URL
 *    at once produce one fetch, not ten.
 *  - `invalidate()` is called after any mutating call so the next read is
 *    fresh.
 */

type Entry<T> = {
  value: T;
  at: number;
};

const store = new Map<string, Entry<unknown>>();
const inflight = new Map<string, Promise<unknown>>();

/** Bumped by every invalidation. A response whose request started before the
 *  bump describes state the write just replaced, so it must not be cached. */
let generation = 0;

/** Default lifetime for a cached GET. Long enough to make a section switch
 *  instant, short enough that a reload prompt still refreshes the rail. */
export const DEFAULT_TTL_MS = 30_000;

export function cachedGet<T>(
  key: string,
  load: () => Promise<T>,
  ttlMs: number = DEFAULT_TTL_MS,
): Promise<T> {
  if (ttlMs <= 0) return load();

  const now = Date.now();
  const hit = store.get(key);
  if (hit && now - hit.at < ttlMs) {
    return Promise.resolve(hit.value as T);
  }

  const existing = inflight.get(key);
  if (existing) return existing as Promise<T>;

  const started = generation;
  const p: Promise<T> = load()
    .then((value) => {
      // A write that landed while this was in flight wins. Storing the old
      // response here pinned pre-write state over the server for the full TTL
      // and made an Enable/Disable click look ignored.
      if (started === generation) store.set(key, { value, at: Date.now() });
      return value;
    })
    .finally(() => {
      // A new request may already own this key; only clear our own entry.
      if (inflight.get(key) === p) inflight.delete(key);
    });

  inflight.set(key, p as Promise<unknown>);
  return p;
}

/** Drop everything, or just the keys matching a substring. Used after writes. */
export function invalidate(pattern?: string): void {
  generation += 1;
  // In-flight reads are not exempt: a response that started before this
  // invalidation must not be allowed to repopulate the store, and a caller
  // arriving now must start a fresh request instead of joining the old one.
  inflight.clear();
  if (!pattern) {
    store.clear();
    return;
  }
  for (const k of [...store.keys()]) {
    if (k.includes(pattern)) store.delete(k);
  }
}

/** Test/diagnostic helper: how many keys are currently held. */
export function cacheSize(): number {
  return store.size;
}
