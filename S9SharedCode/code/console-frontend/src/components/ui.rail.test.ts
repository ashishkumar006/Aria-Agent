import { describe, it, expect } from 'vitest';
import { RAIL } from './ui';

/* Every routable section must be reachable from the rail.
 *
 * `/parts` was routed, had a full page (rail, breadcrumb, title, empty state)
 * and was reachable ONLY by typing the URL: it had never been added to RAIL.
 * Because the command palette is built from the same list, Ctrl+K could not
 * find it either - so there were two independent ways to lose a section and
 * neither would have failed a test. */

const ROUTES = [
  '/console', '/runs', '/memory', '/documents', '/research', '/scheduler',
  '/skills', '/code', '/authoring', '/apps', '/ledger', '/mission', '/settings',
  '/parts', '/computer',
];

const railRoutes = RAIL.flatMap((g) => g.items.map((i) => i.to));

describe('RAIL', () => {
  it('offers every routable section', () => {
    for (const r of ROUTES) {
      expect(railRoutes).toContain(r);
    }
  });

  it('has no duplicate destinations', () => {
    const seen = new Set<string>();
    for (const r of railRoutes) {
      expect(seen.has(r)).toBe(false);
      seen.add(r);
    }
  });

  it('offers nothing that is not routable', () => {
    // A stale entry is a dead nav item, which is the same class of bug in the
    // other direction: a rail link that goes nowhere.
    for (const r of railRoutes) {
      expect(ROUTES).toContain(r);
    }
  });

  it('gives every item a label and a group', () => {
    for (const g of RAIL) {
      expect(g.group.trim()).not.toBe('');
      for (const it of g.items) {
        expect(it.label.trim()).not.toBe('');
      }
    }
  });
});
