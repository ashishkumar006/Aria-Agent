import { describe, expect, it, vi } from 'vitest';
import { api } from './api';
import { invalidate } from './lib/getCache';

/**
 * A write must invalidate the read-through cache (`getCache`), whatever verb it
 * uses. `Scheduler.remove()` deleted a schedule, then re-read the list to
 * refresh the view — but the DELETE went through `j()` with no invalidation
 * (only the POST helper `pj` invalidated), so the re-read was served from the
 * 30s cache: the row stayed on screen until the TTL expired or the page was
 * reloaded, and the delete looked like it had silently failed.
 *
 * This pins the contract at the api boundary: after any mutating call, the next
 * read hits the network again instead of the stale cache.
 */
describe('api write invalidates the read cache', () => {
  it('serves a repeat GET from cache, then refetches after a DELETE', async () => {
    invalidate();
    const calls: string[] = [];
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${(init?.method || 'GET').toUpperCase()} ${String(url)}`);
      return new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }) as unknown as typeof fetch;

    try {
      const params = new URLSearchParams('limit=1');
      await api.memory(params);            // populates the cache
      await api.memory(params);            // served from cache — no second fetch
      expect(calls.filter((c) => c.startsWith('GET'))).toHaveLength(1);

      await api.deleteMemory('mem-abc');   // a write must drop the cached list
      await api.memory(params);            // must refetch, not replay stale state

      expect(calls).toEqual([
        'GET /api/memory?limit=1',
        'DELETE /api/memory/mem-abc',
        'GET /api/memory?limit=1',
      ]);
    } finally {
      globalThis.fetch = origFetch;
    }
  });
});

/**
 * Liveness probes must never be served from the 30s read cache. The
 * gateway-down banner's Retry called `api.health()` and got the cached
 * "gateway_up: false" straight back, so the banner could not be cleared for
 * up to 30s after the gateway recovered — the button looked broken.
 */
describe('health bypasses the read cache', () => {
  it('hits the network on every call and reports the fresh value', async () => {
    invalidate();
    const calls: string[] = [];
    const origFetch = globalThis.fetch;
    let up = false;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${(init?.method || 'GET').toUpperCase()} ${String(url)}`);
      return new Response(JSON.stringify({ agent: 'ready', gateway_up: up }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }) as unknown as typeof fetch;

    try {
      const a = await api.health();
      up = true;                          // gateway recovers
      const b = await api.health();       // must NOT replay the cached failure
      expect(calls).toEqual(['GET /api/health', 'GET /api/health']);
      expect(a.gateway_up).toBe(false);
      expect(b.gateway_up).toBe(true);
    } finally {
      globalThis.fetch = origFetch;
    }
  });
});
