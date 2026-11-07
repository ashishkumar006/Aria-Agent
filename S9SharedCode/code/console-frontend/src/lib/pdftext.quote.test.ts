import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  MIN_PARTIAL_COVERAGE, MIN_PARTIAL_TOKENS, buildIndex, matchQuote,
  type TextCell,
} from './pdftext';

/* A quote that shares two words with a sentence is not that sentence.
 *
 *   Captured live from `Grid-Scale Battery Cost Briefing 2025.pdf` after the
 *   user reported "some unrelated lines" lighting up for *what is this report
 *   about*. The model cited page 1's table row
 *   `[[p:1|Stationary Storage Pack 127 70 -45%]]`, and page 2 contains the
 *   sentence "The dramatic 45% drop in stationary storage costs significantly
 *   improves the business case". "stationary storage" is 2 of the quote's 6
 *   tokens, the old floor was 2, so page 2 drew a bar across that line too —
 *   verbatim text the answer never referred to. The same quote was tested
 *   against EVERY rendered page, so any page sharing a bigram got marked, and a
 *   running header repeated on all seven pages lit up everywhere a title was
 *   ever cited.
 *
 *   The fixture below is the real text layer of the two rendered pages, so
 *   these assertions are about the document, not about a convenient mock.
 */

const PAGE1: string[] = [
  'Grid-Scale Battery Cost Briefing 2025', 'page 1',
  'Grid-Scale Battery Cost Briefing 2025',
  'Briefing: Drivers of Grid-Scale Battery Cost Reductions in', '2025',
  'This brief outlines the primary factors contributing to the significant decline in grid-scale',
  'battery energy storage (BESS) costs during 2025. As of December 2025, stationary',
  'storage has emerged as the most cost-effective segment for lithium-ion battery',
  'applications, providing a critical opportunity for municipal energy infrastructure.',
  'Metric', '2024 Price ($/kWh)', '2025 Price ($/kWh)', 'Change (%)',
  'Global Avg. Pack', 'Price', '117', '108', '-8%',
  'Stationary Storage', 'Pack', '127', '70', '-45%',
  'EV Battery Pack', '97', '99', '+2%',
];
const PAGE2: string[] = [
  'Grid-Scale Battery Cost Briefing 2025', 'Grid-Scale Battery Cost Briefing 2025',
  'page 2', 'Strategic Implications for Municipalities',
  'The dramatic 45% drop in stationary storage costs significantly improves the business',
  'case for municipal renewable integration. These systems now offer a viable,',
  'subsidy-independent method to shift solar and wind generation to high-value hours,',
  'References',
  'BloombergNEF. (2025). Lithium-Ion Battery Pack Prices Fall to $108 Per Kilowatt-Hour, Despite',
];

/** Cells carry no geometry here: matching is tokenwise, and the rectangles are
 *  the viewer's problem, not the matcher's. */
const idx = (lines: string[]) => buildIndex(
  lines.map((str, i): TextCell => ({ str, left: i * 10, top: 0, width: 9, height: 9 })));

const TABLE_ROW = 'Stationary Storage Pack 127 70 -45%';
const SENTENCE = '45% drop in stationary storage costs significantly improves the business case';

describe('matchQuote against the real page text', () => {
  it('finds the cited table row on the page that has it', () => {
    const m = matchQuote(idx(PAGE1), TABLE_ROW);
    expect(m?.exact).toBe(true);
    expect(m?.coverage).toBe(1);
  });

  it('does not accept a two-word coincidence on another page', () => {
    // 2 of 6 tokens: this is the bar the user saw on page 2.
    expect(matchQuote(idx(PAGE2), TABLE_ROW)).toBeNull();
  });

  it('still finds the sentence the model cited on page 2', () => {
    expect(matchQuote(idx(PAGE2), SENTENCE)?.exact).toBe(true);
  });

  it('is not a blanket refusal — a quote absent everywhere still returns null', () => {
    expect(matchQuote(idx(PAGE1), 'quantum tunnelling in solid state electrolytes')).toBeNull();
  });
});

describe('the partial-match floor', () => {
  it('is a real floor, not one shared word', () => {
    expect(MIN_PARTIAL_TOKENS).toBeGreaterThanOrEqual(3);
    expect(MIN_PARTIAL_COVERAGE).toBeGreaterThan(0.5);
  });

  it('accepts a run that is most of a short quote', () => {
    // 3 of 4 tokens = 75%: the page has the phrase, the model trimmed it.
    const i = idx(['one two three four five six']);
    const m = matchQuote(i, 'one two three nope');
    expect(m?.exact).toBe(false);
    expect(m?.coverage).toBeGreaterThanOrEqual(MIN_PARTIAL_COVERAGE);
  });

  it('rejects a run that is a third of a long quote', () => {
    const i = idx(['one two three four five six']);
    expect(matchQuote(i, 'one two three nope nope nope nope nope')).toBeNull();
  });
});

/* Painting is a separate decision from finding, and it is the half that was
   wrong: every rendered page painted every quote it could match. */
describe('where a found quote is allowed to be painted', () => {
  const src = readFileSync(resolve(__dirname, '../components/PdfViewer.tsx'), 'utf-8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');

  it('paints only on the page the model named', () => {
    expect(src).toMatch(/q\.page > 0 && q\.page !== n/);
  });

  it('still reports the page it actually landed on, so the chip can say so', () => {
    // The guard must come AFTER the hit is pushed, or "found on p.5" goes blind.
    const at = src.indexOf('hits.push({ id: q.id, page: n, exact: m.exact })');
    const guard = src.indexOf('q.page !== n');
    expect(at).toBeGreaterThan(-1);
    expect(guard).toBeGreaterThan(at);
  });
});
