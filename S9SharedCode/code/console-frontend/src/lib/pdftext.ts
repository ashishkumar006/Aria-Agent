/** Matching a quoted phrase to the words on a rendered PDF page.
 *
 *  A PDF page is not text; it is positioned fragments. `getTextContent()`
 *  returns runs — sometimes a whole line, sometimes two words, sometimes a
 *  number whose font is loaded separately — each with its own rectangle. So
 *  "highlight what Aria just quoted" needs an index that (a) puts the fragments
 *  back into reading order, (b) knows which fragment every character came
 *  from, and (c) survives the ways a quote stops matching exactly: a line break
 *  inside the sentence, curly versus straight quotes, a word hyphenated across
 *  lines.
 *
 *  Matching is token-wise rather than one long `indexOf`, which is what lets a
 *  partially-recovered phrase still light up instead of silently highlighting
 *  nothing. A wrong-looking highlight is a bug a reader can see; an absent one
 *  reads as "Aria made that up".
 */

import { foldText, normQuote } from './citations';

export type TextCell = {
  /** The fragment as the PDF stores it. */
  str: string;
  /** Page-space rectangle already scaled to the viewport. */
  left: number;
  top: number;
  width: number;
  height: number;
};

type Token = {
  s: string;
  /** Index into `cells`. */
  cell: number;
  /** Character offsets inside that cell's own string. */
  a: number;
  b: number;
};

export type Rect = { left: number; top: number; width: number; height: number };

/* A partial match has to be a real piece of the quote, not a word or two that
   happen to sit next to each other. Measured against a live document: the
   citation "Stationary Storage Pack 127 70 -45%" (a table row on page 1) also
   found "stationary storage" inside page 2's sentence "the dramatic 45% drop in
   stationary storage costs…" — coverage 2/6 — and that painted a bar across a
   line that has nothing to do with the quote. Two words is coincidence; most of
   the phrase is a match. */
export const MIN_PARTIAL_TOKENS = 3;
export const MIN_PARTIAL_COVERAGE = 0.6;
export type Match = {
  rects: Rect[];
  /** Every token of the quote was found in sequence. */
  exact: boolean;
  /** Tokens matched / tokens in the quote. */
  coverage: number;
};

export type PageIndex = {
  cells: TextCell[];
  tokens: Token[];
  /** Folded text of the whole page, for debugging and for a last-resort search. */
  text: string;
};

/** Punctuation a quote and a glyph disagree about.
 *
 *  A page stores `years.` — the period arrives inside the same fragment as the
 *  word — while a model quoting the sentence writes `years`. Without trimming
 *  both sides the highlight silently misses the end of every sentence it is
 *  asked about, which is exactly the phrase a reader checks first.
 */
const EDGE_PUNCT = /^[-–—“”"'`([{.+,;:!?]+|[)\]}.,;:!?–—"”"'`+]+/;

function trimEdge(s: string): string {
  return s.replace(EDGE_PUNCT, '').replace(/[-–—“”"'`)\].,;:!?+]+$/, '');
}

export function buildIndex(cells: TextCell[]): PageIndex {
  const tokens: Token[] = [];
  const parts: string[] = [];
  cells.forEach((cell, ci) => {
    const raw = String(cell.str || '');
    let a = 0;
    while (a < raw.length) {
      while (a < raw.length && /\s/.test(raw[a])) a += 1;
      let b = a;
      while (b < raw.length && !/\s/.test(raw[b])) b += 1;
      if (b > a) {
        const rawTok = raw.slice(a, b);
        const trimmed = trimEdge(rawTok);
        if (trimmed) {
          // Keep the offsets pointing at the words themselves: a token trimmed
          // of its period must not highlight one character wider than it reads.
          const lead = rawTok.indexOf(trimmed);
          tokens.push({
            s: foldText(trimmed).toLowerCase(),
            cell: ci,
            a: a + Math.max(0, lead),
            b: a + Math.max(0, lead) + trimmed.length,
          });
        }
        a = b;
      }
    }
    // A cell boundary is a word boundary: "Line" and "7" arriving as two
    // fragments must not fuse into "Line7".
    parts.push(raw);
    parts.push(' ');
  });
  return { cells, tokens, text: foldText(parts.join('')).replace(/\s+/g, ' ').trim() };
}

/** Rectangle of one token, proportioned inside its cell.
 *
 *  Glyph widths are not uniform, so a character-fraction x-range is an
 *  approximation. It is the same approximation pdf.js's own find-bar makes for
 *  partial-word matches, and it is good enough that a reader reads it as the
 *  right words.
 */
function tokenRect(idx: PageIndex, t: Token): Rect {
  const c = idx.cells[t.cell];
  const len = Math.max(1, String(c.str || '').length);
  const x0 = c.left + c.width * (t.a / len);
  const x1 = c.left + c.width * (t.b / len);
  return { left: Math.min(x0, x1), top: c.top, width: Math.abs(x1 - x0), height: c.height };
}

/** Join the tokens of one phrase that sit on the same line into one bar.
 *
 *  These tokens are consecutive in the quote by construction, so a gap between
 *  two of them on the same baseline is the space between words — and a
 *  highlighter that leaves the spaces out turns "the deck stayed open" into six
 *  separate marks a reader cannot follow. A line change is the only thing that
 *  starts a new bar.
 */
function coalesce(rects: Rect[]): Rect[] {
  const out: Rect[] = [];
  for (const r of rects) {
    const prev = out[out.length - 1];
    if (prev && Math.abs(prev.top - r.top) < 1) {
      const right = Math.max(prev.left + prev.width, r.left + r.width);
      prev.left = Math.min(prev.left, r.left);
      prev.width = right - prev.left;
      prev.height = Math.max(prev.height, r.height);
      continue;
    }
    out.push({ ...r });
  }
  return out;
}

/** Longest consecutive run of the quote's tokens that this page actually has,
 *  starting at `at`. */
function runFrom(idx: PageIndex, q: string[], at: number): number {
  let n = 0;
  while (n < q.length && at + n < idx.tokens.length && idx.tokens[at + n].s === q[n]) n += 1;
  return n;
}

/** The quote in the same folded, punctuation-trimmed shape as the index. */
function quoteTokens(quote: string): string[] {
  return normQuote(quote).split(' ')
    .map((w) => foldText(trimEdge(w)).toLowerCase())
    .filter(Boolean);
}

export function matchQuote(idx: PageIndex, quote: string): Match | null {
  const q = quoteTokens(quote);
  if (!q.length || !idx.tokens.length) return null;

  let best: { at: number; run: number } | null = null;
  for (let i = 0; i < idx.tokens.length; i += 1) {
    if (idx.tokens[i].s !== q[0]) continue;
    const run = runFrom(idx, q, i);
    if (!best || run > best.run) best = { at: i, run };
    if (run === q.length) break;  // exact match found; stop scanning
  }
  if (!best) return null;

  const exact = best.run === q.length;
  // A weak run is not a match at all, for painting or for the chip label: a
  // "found on p.2" that rests on two shared words sends the reader to a page
  // that does not carry the sentence, which is worse than saying nothing.
  if (!exact && (best.run < MIN_PARTIAL_TOKENS
                 || best.run / q.length < MIN_PARTIAL_COVERAGE)) return null;

  const rects: Rect[] = [];
  for (let k = 0; k < best.run; k += 1) {
    rects.push(tokenRect(idx, idx.tokens[best.at + k]));
  }
  return {
    rects: coalesce(rects),
    exact,
    coverage: best.run / q.length,
  };
}

/** Words a page contains that the quote was reaching for — the fallback when
 *  the phrase itself is absent, so the reader at least lands on the page. */
export function sharesWords(idx: PageIndex, quote: string): number {
  const q = new Set(quoteTokens(quote).filter((w) => w.length > 3));
  if (!q.size) return 0;
  // Distinct words, not occurrences: a page that says "capacity" nine times
  // matches one of the quote's words, not nine of them.
  const found = new Set<string>();
  for (const t of idx.tokens) if (q.has(t.s)) found.add(t.s);
  return found.size / q.size;
}
