import { describe, expect, it, beforeEach, vi } from 'vitest';
import { cachedGet, invalidate, cacheSize, DEFAULT_TTL_MS } from '../lib/getCache';

/**
 * A section switch was refetching everything the user had just looked at:
 * `<Routes>` remounted the view (see KeepAlive.tsx) and the view fetched in a
 * mount effect. Keep-alive stops the remount; this cache stops the round trip.
 *
 * The rules that matter: GET-shaped reads are cached, in-flight requests are
 * SHARED (ten components asking at once must produce one fetch), and any write
 * invalidates.
 */
describe('getCache', () => {
  beforeEach(() => {
    invalidate();
    vi.useRealTimers();
  });

  it('serves a repeat read without calling the loader again', async () => {
    const load = vi.fn().mockResolvedValue({ v: 1 });
    const a = await cachedGet('k', load);
    const b = await cachedGet('k', load);
    expect(a).toEqual(b);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('shares one in-flight request between concurrent callers', async () => {
    let resolve!: (v: unknown) => void;
    const load = vi.fn(() => new Promise((r) => { resolve = r; }));
    const p1 = cachedGet('k', load);
    const p2 = cachedGet('k', load);
    resolve({ v: 2 });
    expect(await p1).toEqual(await p2);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('does not cache when ttl is zero', async () => {
    const load = vi.fn().mockResolvedValue({ v: 1 });
    await cachedGet('k', load, 0);
    await cachedGet('k', load, 0);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('refetches once the entry is older than the ttl', async () => {
    vi.useFakeTimers();
    const load = vi.fn().mockResolvedValue({ v: 1 });
    await cachedGet('k', load, 1000);
    expect(load).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1500);
    await cachedGet('k', load, 1000);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('keys on the full url, query string included', async () => {
    const a = vi.fn().mockResolvedValue('a');
    const b = vi.fn().mockResolvedValue('b');
    await cachedGet('/api/runs/summary?limit=10', a);
    await cachedGet('/api/runs/summary?limit=200', b);
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
  });

  it('invalidate() with no argument drops everything', async () => {
    const load = vi.fn().mockResolvedValue(1);
    await cachedGet('/api/a', load);
    await cachedGet('/api/b', load);
    expect(cacheSize()).toBe(2);
    invalidate();
    expect(cacheSize()).toBe(0);
  });

  it('invalidate(pattern) drops only the matching keys', async () => {
    const load = vi.fn().mockResolvedValue(1);
    await cachedGet('/api/runs/summary', load);
    await cachedGet('/api/memory', load);
    invalidate('/api/runs');
    expect(cacheSize()).toBe(1);
  });

  it('does not cache a response that started before invalidate()', async () => {
    let resolve!: (v: unknown) => void;
    const load = vi.fn(() => new Promise((r) => { resolve = r; }));
    const pending = cachedGet('k', load);

    invalidate();
    resolve({ v: 'stale' });
    await pending;

    expect(cacheSize()).toBe(0);
    const next = vi.fn().mockResolvedValue({ v: 'fresh' });
    await expect(cachedGet('k', next)).resolves.toEqual({ v: 'fresh' });
    expect(next).toHaveBeenCalledTimes(1);
  });

  it('a failed load is not cached', async () => {
    const load = vi.fn().mockRejectedValue(new Error('boom'));
    await expect(cachedGet('k', load)).rejects.toThrow('boom');
    // the in-flight entry must be cleared, or the retry returns the same
    // rejection forever
    await expect(cachedGet('k', load)).rejects.toThrow('boom');
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('has a default ttl short enough to still refresh', () => {
    expect(DEFAULT_TTL_MS).toBeGreaterThan(0);
    expect(DEFAULT_TTL_MS).toBeLessThanOrEqual(120_000);
  });
});