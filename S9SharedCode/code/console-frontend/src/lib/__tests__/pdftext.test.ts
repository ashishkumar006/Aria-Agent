import { describe, expect, it } from 'vitest';
import { buildIndex, matchQuote, sharesWords, type TextCell } from '../pdftext';

/** A page is fragments with rectangles, not a string. These stand in for what
 *  `getTextContent()` returns for a rendered report: a heading cell, then a
 *  sentence that arrived in three pieces, then a second line. */
const PAGE: TextCell[] = [
  { str: 'Executive Summary', left: 72, top: 80, width: 118, height: 13 },
  { str: 'Line 7 ', left: 72, top: 100, width: 32, height: 11 },
  { str: 'retired in August 2025 after', left: 104, top: 100, width: 140, height: 11 },
  { str: 'four years.', left: 244, top: 100, width: 48, height: 11 },
  { str: 'The deck stayed open through the winter.', left: 72, top: 112, width: 210, height: 11 },
];

const idx = buildIndex(PAGE);

describe('matching a quote to a page', () => {
  it('finds a phrase that crosses three separate fragments', () => {
    const m = matchQuote(idx, 'Line 7 retired in August 2025');
    expect(m).not.toBeNull();
    expect(m!.exact).toBe(true);
    expect(m!.coverage).toBe(1);
    // Same baseline, so the bars coalesce into the one highlighted line.
    expect(m!.rects.length).toBe(1);
    expect(m!.rects[0].left).toBeCloseTo(72, 0);
    expect(m!.rects[0].top).toBeCloseTo(100, 0);
  });

  it('starts a fragment’s highlight where its own words start, not at the cell edge', () => {
    // "The deck" and "stayed open" live in one cell; the second must not begin
    // at the cell's left edge, or the bar covers words Aria did not quote.
    const a = matchQuote(idx, 'The deck');
    const b = matchQuote(idx, 'stayed open');
    expect(a && b && a.rects[0].left < b.rects[0].left).toBe(true);
  });

  it('keeps a phrase split across two lines as two bars', () => {
    const m = matchQuote(idx, 'four years. The deck stayed open');
    expect(m!.exact).toBe(true);
    expect(m!.rects.length).toBe(2);
    expect(m!.rects.map((r) => Math.round(r.top))).toEqual([100, 112]);
  });

  it('reports a partial match honestly instead of highlighting nothing', () => {
    const m = matchQuote(idx, 'Line 7 retired in September');
    expect(m).not.toBeNull();
    expect(m!.exact).toBe(false);
    expect(m!.coverage).toBeLessThan(1);
    expect(m!.coverage).toBeGreaterThan(0.5);
  });

  it('folds typographic quotes and dashes, which is what a PDF is made of', () => {
    const fancy = buildIndex([
      { str: 'capacity grew “12%” — quietly', left: 10, top: 10, width: 120, height: 10 },
    ]);
    expect(matchQuote(fancy, '12% - quietly')?.exact).toBe(true);
    expect(matchQuote(fancy, '“12%” — quietly')?.exact).toBe(true);
  });

  it('does not fuse two fragments into a word that is not there', () => {
    // "Line" + "7" arrive as separate cells; matching "line7" must fail.
    expect(matchQuote(idx, 'line7')).toBeNull();
  });

  it('returns null for an empty quote or a page with no words', () => {
    expect(matchQuote(idx, '   ')).toBeNull();
    expect(matchQuote(buildIndex([]), 'anything')).toBeNull();
  });

  it('finds the right occurrence when the first words repeat', () => {
    const twice = buildIndex([
      { str: 'revenue rose', left: 10, top: 10, width: 60, height: 10 },
      { str: 'in march', left: 10, top: 20, width: 60, height: 10 },
      { str: 'revenue rose', left: 10, top: 30, width: 60, height: 10 },
      { str: 'in april', left: 10, top: 40, width: 60, height: 10 },
    ]);
    const m = matchQuote(twice, 'revenue rose in april');
    expect(m!.exact).toBe(true);
    expect(m!.rects[0].top).toBe(30);
  });

  it('measures how much of a quote a page carries, for the page-only fallback', () => {
    expect(sharesWords(idx, 'the deck stayed open through winter')).toBeGreaterThan(0.5);
    expect(sharesWords(idx, 'photovoltaic module efficacy')).toBe(0);
  });
});
