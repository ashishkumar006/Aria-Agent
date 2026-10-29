import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * On a phone the rail is a horizontal strip with 650px of content in a 374px
 * window. Opening `/settings` or `/computer` directly (a bookmark, the desktop
 * shell restoring the last route, a deep link from a run) left the strip at
 * scrollLeft 0: the active item was 230px off the right edge, so the page gave
 * no indication of where you were and the nav looked broken.
 *
 * The fix is a layout effect that reveals the current item before paint, and
 * only by the few pixels needed.
 */
const SRC = readFileSync(resolve(__dirname, 'ui.tsx'), 'utf-8');

describe('phone rail reveals the active section', () => {
  it('scrolls the strip to the current item before paint', () => {
    expect(SRC).toMatch(/useLayoutEffect\(\(\) => \{[\s\S]*?\[aria-current="page"\]/);
    expect(SRC).toMatch(
      /if \(ar\.left < nr\.left\) nav\.scrollLeft \+= ar\.left - nr\.left - 8;/,
    );
    expect(SRC).toMatch(
      /else if \(ar\.right > nr\.right\) nav\.scrollLeft \+= ar\.right - nr\.right \+ 8;/,
    );
  });

  it('leaves a fitted strip and the desktop rail alone', () => {
    expect(SRC).toMatch(/if \(!nav \|\| wide\) return;/);
    expect(SRC).toMatch(/if \(nav\.scrollWidth <= nav\.clientWidth \+ 1\) return;/);
  });

  it('binds the ref to the element that actually scrolls', () => {
    // The <aside> is the column; the horizontal overflow is on the <nav>.
    expect(SRC).toMatch(/<nav ref=\{navRef\}/);
  });
});
