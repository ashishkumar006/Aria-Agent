import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * Sorting an A2UI table must stay accessible.
 *
 * The header button carried `aria-sort`, but ARIA only allows that attribute
 * on the columnheader (`<th scope="col">`). Lighthouse scored the console
 * 0.95 with exactly this failure, and a screen reader reading the header had
 * no sort state to announce. The value belongs on the `<th>`; the button keeps
 * its "Sort by …" label.
 */
const SRC = readFileSync(resolve(__dirname, 'A2ui.tsx'), 'utf-8');

describe('A2UI table sort state', () => {
  it('puts aria-sort on the column header', () => {
    expect(SRC).toMatch(/<th key=\{c\.key\} scope="col"\s*\n?\s*aria-sort=/);
  });

  it('does not put aria-sort on the button', () => {
    const button = SRC.slice(SRC.indexOf('<button', SRC.indexOf('<thead')), SRC.indexOf('</button>', SRC.indexOf('<thead')));
    expect(button).toMatch(/aria-label=\{`Sort by \$\{c\.label\}`\}/);
    expect(button).not.toMatch(/aria-sort/);
  });
});
