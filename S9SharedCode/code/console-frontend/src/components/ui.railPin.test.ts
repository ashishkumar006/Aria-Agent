import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * The rail collapses to a left-edge overlay and comes back.
 *
 * Four things made that unsafe in the naive version, and each is pinned here
 * because the failure is silent — the nav just stops being reachable:
 *
 *  - the default must stay PINNED, or a first run, a cleared profile or a
 *    private window opens with no navigation and no obvious way to get it
 *  - the pin state must live at module scope, not in the component: every
 *    visited section keeps its own `<Rail/>` mounted, so per-component state
 *    would leave the next section open after the user closed this one
 *  - a rail that is translated off-screen must not stay in the tab order; it
 *    is out of flow only at `lg`, so the `inert` rule has to be gated by the
 *    same breakpoint or the phone strip goes dead
 *  - the toggle must not leave the screen with the rail. A two-pixel edge is
 *    not a control a person can find, so the top bar carries one too.
 */
const SRC = readFileSync(resolve(__dirname, 'ui.tsx'), 'utf-8');

describe('rail pinning', () => {
  it('is pinned by default', () => {
    /* Anything other than a stored '0' is pinned: a missing key (first run),
       a cleared store and a disabled localStorage all land on the full rail. */
    expect(SRC).toMatch(/getItem\(RAIL_PIN_KEY\)\s*!==\s*'0'/);
  });

  it('shares one pin state across the sections KeepAlive keeps mounted', () => {
    expect(SRC).toMatch(/const railListeners = new Set/);
    expect(SRC).toMatch(/subscribeRail/);
    expect(SRC).toMatch(/useSyncExternalStore\(subscribeRail/);
  });

  it('persists the choice', () => {
    expect(SRC).toMatch(/setItem\(RAIL_PIN_KEY/);
  });

  it('takes the closed rail out of the tab order, but only where it is out of flow', () => {
    /* `inert` on every breakpoint would break the horizontal strip below lg,
       where the rail is never collapsed. */
    expect(SRC).toMatch(/inert=\{closed\}/);
    expect(SRC).toMatch(/const closed = wide && !open/);
    expect(SRC).toMatch(/min-width:\s*1024px/);
  });

  it('offers a way back from the top bar, not only from the edge', () => {
    expect(SRC).toMatch(/aria-label="Show navigation"/);
    expect(SRC).toMatch(/setRailPinned\(true\)/);
  });

  it('closes a peek on Escape without changing the pin', () => {
    expect(SRC).toMatch(/e\.key === 'Escape'\)\s*setPeek\(false\)/);
  });

  it('opens the peek from a pointer that never crosses the rail edge', () => {
    /* The edge strip reveals the rail UNDER a stationary pointer, which never
       fires `pointerenter` on the aside, so `pointerleave` alone left it stuck
       open. The position watch is what closes it. */
    expect(SRC).toMatch(/addEventListener\('pointermove', onMove/);
    expect(SRC).toMatch(/clientX > 208/);
  });
});
