import { describe, it, expect } from 'vitest';
import { RAIL } from '../components/ui';
import { prefetchSection } from '../lib/prefetch';

/* The Computer page is the human half of the computer-use gate.
 *
 * The safety gates on the server are explicit that a sensitive action
 * "returns a pending token; the web UI resolves it (approve/reject) before
 * execution" (safety/gates.py). The agent server had the endpoints and the
 * console had no page: nothing could approve anything, so a pending action
 * was a dead end - the agent asked, and the request sat in memory until
 * the process restarted.
 *
 * These tests pin the wiring that makes the page reachable and
 * self-consistent. They do not test the browser: the approve/reject
 * round-trip is covered by the live E2E (the gate, the endpoint and the
 * audit trail), because what matters there is that the SERVER resolves the
 * approval, not that a button exists.
 */

const ROUTES = [
  '/console', '/runs', '/memory', '/documents', '/research', '/scheduler',
  '/skills', '/code', '/authoring', '/apps', '/ledger', '/mission', '/settings',
  '/parts', '/computer',
];

const railRoutes = RAIL.flatMap((g) => g.items.map((i) => i.to));

describe('the Computer section', () => {
  it('is reachable from the rail', () => {
    // A section nobody can navigate to is not shipped - the same bug that
    // left /parts reachable only by typing the URL.
    expect(railRoutes).toContain('/computer');
  });

  it('is listed in the router', async () => {
    // The router registry is the source of truth for what is routable, and
    // the rail test checks the two lists against each other.
    const { SECTIONS } = await import('../App');
    expect(SECTIONS.map((s) => s.id)).toContain('/computer');
  });

  it('has a chunk that can be pre-warmed', () => {
    // The rail's hover prefetch is built from the same list as the router.
    // A section missing from it suspends the whole shell on first visit.
    expect(() => prefetchSection('/computer')).not.toThrow();
  });

  it('does not duplicate an existing route', () => {
    const seen = new Set<string>();
    for (const r of railRoutes) {
      expect(seen.has(r)).toBe(false);
      seen.add(r);
    }
  });

  it('keeps every route routable', () => {
    for (const r of railRoutes) {
      expect(ROUTES).toContain(r);
    }
  });
});
