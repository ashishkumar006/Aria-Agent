import { describe, expect, it } from 'vitest';
import {
  answerCitations, extractCitations, foldText, hasPartialCitation, normQuote,
  stripCitations,
} from '../citations';
import { renderMarkdown } from '../../components/markdown';

describe('document citations', () => {
  it('reads a page-and-words marker out of an answer', () => {
    const { clean, cites } = extractCitations(
      'Line 7 went quiet [[p:2|Line 7 retired in August]], and the deck stayed open '
      + '[[p:5|the deck stayed open]].');
    expect(cites).toEqual([
      { page: 2, quote: 'Line 7 retired in August' },
      { page: 5, quote: 'the deck stayed open' },
    ]);
    expect(clean).not.toContain('[[');
    expect(clean).toContain('Line 7 went quiet');
  });

  it('keeps a marker with no page, because the excerpt header may not show one', () => {
    const { cites, clean } = extractCitations('see [[p:|no page number here]] below');
    expect(cites).toEqual([{ page: 0, quote: 'no page number here' }]);
    // The gap the marker leaves is HTML whitespace, not the answer's own.
    expect(clean).toBe('see  below');
  });

  it('de-duplicates a quote the model repeats, keeping the first page', () => {
    const cites = answerCitations(
      '[[p:3|the forecast held]] … and again [[p:3|the forecast held]] … '
      + '[[p:4|a different line]]');
    expect(cites).toEqual([
      { page: 3, quote: 'the forecast held' },
      { page: 4, quote: 'a different line' },
    ]);
  });

  it('strips what it cannot parse rather than showing the reader brackets', () => {
    // A malformed attempt is still noise on screen.
    expect(stripCitations('x [[p:|]] y')).toBe('x  y');
    expect(stripCitations('x [[p abc]] y')).toBe('x  y');
    expect(stripCitations('x [[p:1|unclosed')).toBe('x ');
  });

  it('hides a marker that is only half-arrived, so streaming does not flash it', () => {
    const partial = 'The report says [[p:2|Line 7 ret';
    expect(hasPartialCitation(partial)).toBe(true);
    expect(stripCitations(partial)).toBe('The report says ');
    expect(hasPartialCitation('plain text with no marker')).toBe(false);
  });

  it('still reads a citation whose second closing bracket was dropped', () => {
    /* Live failure, not a hypothetical: a model that had written six correct
       markers produced `[[p:7|Supervised Fine-Tuning (SFT)]` on the seventh.
       The strict form matched nothing, so the raw marker reached the screen
       while the page it was pointing at stayed unlit. */
    const t = 'Three phases. [[p:7|Supervised Fine-Tuning (SFT)]\n\nThe document goes on.';
    const { clean, cites } = extractCitations(t);
    expect(cites).toEqual([{ page: 7, quote: 'Supervised Fine-Tuning (SFT)' }]);
    expect(clean).not.toContain('[[');
    expect(stripCitations(t)).not.toContain('[[p:');
  });

  it('does not touch a markdown link that is only partly arrived', () => {
    // `[` alone is ordinary prose mid-stream; hiding it would eat the sentence.
    expect(stripCitations('see [the report')).toBe('see [the report');
    expect(hasPartialCitation('see [the report')).toBe(false);
  });
  it('folds the typography a PDF uses against the words a model re-types', () => {
    // normQuote collapses whitespace and folds glyphs; the edge quotes survive
    // it because they are part of what the reader is shown. Matching trims them
    // separately — see the pdftext suite.
    expect(normQuote('\u201CLine 7   \u2014 retired\u201D')).toBe('"Line 7 - retired"');
    expect(foldText('it\u2019s')).toBe("it's");
  });
});

describe('renderMarkdown with citations', () => {
  it('never renders a marker, wherever it sits', () => {
    const html = renderMarkdown(
      '## Heading [[p:1|top]]\n\n- a bullet [[p:2|b]]\n\n`code [[p:3|c]]`', 1, true);
    expect(html).not.toContain('[[p:');
    expect(html).toContain('Heading');
    expect(html).toContain('bullet');
  });

  it('leaves ordinary double brackets alone — only the p: form is a citation', () => {
    expect(renderMarkdown('a [[Chapter 2]] link')).toContain('[[Chapter 2]]');
  });
});
