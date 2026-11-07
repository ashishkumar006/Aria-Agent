/** Citations that point at a page and the exact words on it.
 *
 *  A bound document turn asks the model for them (`_doc_binding_note`, agent
 *  server) because the reader is looking at the document while the answer
 *  arrives: "page 3, the clause about retired lines" is worth far less than
 *  lighting up that clause on page 3. The marker is deliberately awkward plain
 *  text — `[[p:3|Line 7 retired in August]]` — so a model can emit it reliably
 *  mid-sentence, which is why every surface that renders assistant text strips
 *  it (see `renderMarkdown`). A marker that reached the screen as typed would
 *  be a leak, not a citation.
 */

export type Citation = {
  /** 1-based page, or 0 when the model could not tell which page. */
  page: number;
  /** Verbatim words copied from the document. */
  quote: string;
};

/* `[[p:12|some words]]`, page optional (`[[p:|words]]`), and tolerant of ONE
   closing bracket: observed live, a model that has written six correct markers
   drops the seventh's second `]` — `[[p:7|Supervised Fine-Tuning (SFT)]` — and
   the strict form then leaves the whole marker on the reader's screen. The
   quote body cannot contain a bracket, so the terminator is never ambiguous. */
const MARKER = /\[\[\s*p\s*:\s*(\d{1,4})?\s*\|\s*([^[\]\n]{1,400}?)\s*\]{1,2}/gi;
/** Anything the model *tried* to make a marker, so a malformed one never
 *  reaches the reader as noise. */
const MALFORMED = /\[\[p[^\]\n]{0,600}\]{0,2}/gi;
/** A marker cut in half by the stream: nothing after it is displayable yet.
 *  Anchored on the doubled bracket, because a single `[` is an ordinary
 *  markdown link still on its way in. */
const TRAILING_PARTIAL = /\[\[[^\]]*$/;

/** Quote characters and dashes that a PDF renders typographically and a model
 *  re-types as ASCII (or the reverse). Folding both sides is what lets a quote
 *  copied out of a chunk match the glyphs on the page. */
export function foldText(s: string): string {
  return String(s || '')
    .replace(/[‘’‛ʼ]/g, "'")
    .replace(/[“”]/g, '"')
    .replace(/[–—]/g, '-')
    .replace(/ /g, ' ')
    .normalize('NFKC');
}

/** Whitespace-collapsed, fold-normalised form used for matching. */
export function normQuote(s: string): string {
  return foldText(s).replace(/\s+/g, ' ').trim();
}

export function extractCitations(text: string): { clean: string; cites: Citation[] } {
  const source = String(text || '');
  const cites: Citation[] = [];
  /* The removal leaves a double space where the marker sat. HTML collapses it,
     so it is left alone rather than squashed across the whole answer — a global
     space squeeze would also re-flow the alignment inside a code block, which
     is the user's text and none of our business. */
  const clean = source
    .replace(MARKER, (_m, page: string | undefined, quote: string) => {
      const q = normQuote(quote.replace(/^[“”"'`]+|[“”"'`]+$/g, ''));
      if (q) cites.push({ page: Number(page || 0) || 0, quote: q });
      return '';
    })
    .replace(MALFORMED, '');
  return { clean, cites };
}

/** Drop markers for display, including one the stream has not finished.
 *
 *  Two behaviours, not one: a complete marker becomes invisible, and a partial
 *  trailing one is hidden until the closing bracket arrives — otherwise every
 *  streamed answer flashes `[p:3|Line 7 ret…` at the reader for a second. */
export function stripCitations(text: string): string {
  const source = String(text || '');
  if (!source.includes('[[')) return source;
  return source
    .replace(MARKER, '')
    .replace(TRAILING_PARTIAL, '')
    .replace(MALFORMED, '');
}

/** True while a marker is still arriving, so a caller can wait rather than
 *  highlight half a quote. */
export function hasPartialCitation(text: string): boolean {
  return TRAILING_PARTIAL.test(String(text || ''));
}

/** The citations of one whole answer, oldest first, de-duplicated. */
export function answerCitations(text: string): Citation[] {
  const seen = new Set<string>();
  return extractCitations(text).cites.filter((c) => {
    const k = `${c.page}\u0000${c.quote}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
