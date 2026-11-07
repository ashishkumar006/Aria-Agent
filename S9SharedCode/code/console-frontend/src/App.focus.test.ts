import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { SECTIONS, SUB_SECTIONS } from './App';

/* Focus was built as a TAB that re-skinned the Discuss workspace, and that was
   the misunderstanding: a tab still leaves the tab strip, the top bar, the
   badges and the run's chrome on screen, which is everything a reading mode is
   supposed to get rid of.

   It is now its own page — `/authoring/focus` — that renders the document and
   Aria and nothing else. Two things make that work without costing the
   conversation:
     · `SUB_SECTIONS` resolves the path to the SAME keep-alive entry as
       /authoring. A separate section id would mount a second Authoring, and
       the discussion would silently restart the moment you entered focus.
     · `ui.authoring_focus` is its switch, and it fails open.
 */

const SRC = readFileSync(resolve(__dirname, 'views/Authoring.tsx'), 'utf-8');
const CODE = SRC.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');

describe('the focus route', () => {
  it('resolves to the authoring section, not a second mount', () => {
    expect(SUB_SECTIONS['/authoring/focus']).toBe('/authoring');
  });

  it('only ever points at a registered section', () => {
    const ids: Set<string> = new Set(SECTIONS.map((s) => s.id));
    for (const target of Object.values(SUB_SECTIONS)) {
      // A sub-route resolving to an unmounted section renders a blank page.
      expect(ids.has(target)).toBe(true);
    }
  });

  it('is not a tab any more', () => {
    // A tab leaves the strip, the top bar and the badges on screen, which is
    // the whole thing a reading mode is supposed to remove.
    expect(/\['focus',\s*'Focus'/.test(CODE)).toBe(false);
    expect(/type View =[^;]*'focus'/.test(CODE)).toBe(false);
  });

  it('hides the rail and the top bar on the page', () => {
    expect(CODE).toMatch(/\{!focus && <Rail \/>}/);
    expect(CODE).toMatch(/\{!focus && \(\s*<TopBar/);
    expect(CODE).toMatch(/\{!focus && tabStrip\(\)}/);
  });

  it('drops the tabpanel role when there is no tablist to point at', () => {
    // An orphan role="tabpanel" with an aria-labelledby to a tab that is not on
    // the page is an a11y contract broken in silence — no error, no visual.
    expect(CODE).toMatch(/focus \? \{\} : \{ role: 'tabpanel'/);
  });

  it('keeps an exit on the page itself, not only in the chrome it hides', () => {
    expect(CODE).toMatch(/Exit focus/);
    expect(CODE).toMatch(/navigate\(focus \? '\/authoring' : '\/authoring\/focus'\)/);
    expect(CODE).toMatch(/Back to Authoring/);
  });

  it('has a kill switch and fails open without it', () => {
    expect(CODE).toMatch(/ui\.authoring_focus/);
    expect(CODE).toMatch(/setFocusAllowed\(/);
    // On: default true, and a read that throws leaves it on. A feature that
    // quietly vanishes because /api/flags was unreachable is a worse failure
    // than the feature staying on for one extra turn.
    expect(CODE).toMatch(/const \[focusAllowed, setFocusAllowed\] = useState\(true\)/);
    expect(CODE).toMatch(/\}\)\.catch\(/);
  });
});
