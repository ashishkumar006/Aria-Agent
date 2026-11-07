import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/* The catalog offers Grid, Section, MetricRow and a Text `variant`, and the
 * renderer was the half that quietly threw the layout away.
 *
 *   `columns:"auto"` — what a model reaches for when it means "as many as fit" —
 *   was hard-wired to two columns at every width, so a six-card dashboard
 *   rendered as three rows of two across a 1600px pane. `columns:"2"` had no
 *   breakpoint at all (two crushed columns on a phone), and `"3"` jumped
 *   straight from one column to three. A MetricRow's Stat cards carried `flex-1`
 *   with no ceiling, so three metrics showing "12", "0" and "3" stretched to a
 *   third of the pane each. A Sparkline at `h-10 w-full` across a 1200px card is
 *   a hairline, not a trend. And a Table inside a Row forced the whole app
 *   card wider than its pane, because a flex item's default min-width is its
 *   content width.
 *
 *   These read the source because the assertions are about the class strings
 *   the browser will resolve — the thing no runtime check would catch.
 */

const SRC = readFileSync(resolve(__dirname, './A2ui.tsx'), 'utf-8');
const CODE = SRC.replace(/\/\*[\s\S]*?\*\//g, '');

describe('Grid breakpoints', () => {
  const grid = CODE.slice(CODE.indexOf("c.component === 'Grid'"),
                          CODE.indexOf("c.component === 'Accordion'"));

  it('gives every fixed column count a step below it', () => {
    expect(grid).toMatch(/'2' \? 'grid-cols-1 sm:grid-cols-2'/);
    expect(grid).toMatch(/'3' \? 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3'/);
    expect(grid).toMatch(/'4' \? 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-4'/);
  });

  it('makes "auto" mean fill-the-width, not two columns forever', () => {
    expect(grid).toMatch(/auto-fit/);
    expect(grid).toMatch(/minmax\(/);
    expect(grid).not.toMatch(/:\s*'grid-cols-1 sm:grid-cols-2'\s*;/);
  });
});

/** One `if (c.component === 'X')` branch of the renderer. */
function branch(name: string): string {
  const at = CODE.indexOf(`c.component === '${name}'`);
  if (at < 0) return '';
  const next = CODE.indexOf('if (c.component', at + 10);
  return CODE.slice(at, next < 0 ? CODE.length : next);
}

describe('flex containers that hold real content', () => {
  it('caps a metric strip so numbers do not become full-width cards', () => {
    expect(CODE).toMatch(/max-w-\[300px\]/);
  });

  it('gives every flex child min-w-0 so a table cannot widen the pane', () => {
    // Scoped to the three containers that hold real content. TagList and
    // FilterChips also wrap, but their children are pills and need nothing.
    for (const name of ['MetricRow', 'Row', 'Toolbar']) {
      const b = branch(name);
      expect(b.length).toBeGreaterThan(0);
      const rows = b.match(/className=\{?[`"][^`"]*flex flex-wrap[^`"]*/g) || [];
      expect(rows.length).toBeGreaterThan(0);
      for (const r of rows) expect(r).toContain('min-w-0');
    }
  });

  it('keeps a sparkline a sparkline', () => {
    expect(CODE).toMatch(/h-10 w-full max-w-\[420px\]/);
  });
});

describe('the tracker surface the board actually shows', () => {
  it('renders a heading variant larger than body text', () => {
    // If these two class strings converge, the app's title is body copy again.
    const heading = /variant === 'heading'[\s\S]*?text-\[(\d+(?:\.\d+)?)px\]/.exec(CODE);
    const body = /return <p className="m-0 whitespace-pre-wrap text-\[(\d+(?:\.\d+)?)px\]/.exec(CODE);
    expect(heading).not.toBeNull();
    expect(body).not.toBeNull();
    expect(Number(heading![1])).toBeGreaterThan(Number(body![1]));
  });
});
