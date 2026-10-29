import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * Browser Back must restore the tab title too.
 *
 * Keep-alive hides visited views instead of unmounting them. TopBar set
 * `document.title` in an effect keyed only on `crumb`, so returning to a
 * cached view (Documents → Runs → Back) never re-ran it: the URL and the page
 * were Documents while the tab still read "Aria · Runs". The effect has to
 * depend on whether the section is the visible one, and hidden sections must
 * not write the title.
 */
const UI = readFileSync(resolve(__dirname, 'ui.tsx'), 'utf-8');

describe('route identity in the tab title', () => {
  it('tracks section visibility, not just the crumb', () => {
    expect(UI).toMatch(/useSectionActive/);
    expect(UI).toMatch(/const active = useSectionActive\(\);/);
    expect(UI).toMatch(/if \(active\) document\.title = `Aria · \$\{crumb\}`;/);
    expect(UI).toMatch(/\}, \[active, crumb\]\);/);
  });

  it('cannot be overwritten by a hidden kept-alive section', () => {
    const start = UI.indexOf('export const TopBar');
    const effect = UI.slice(start, UI.indexOf('const pinned', start));
    expect(effect).toMatch(/if \(active\)/);
    expect(effect).not.toMatch(/^\s*document\.title = /m);
  });
});
