import { describe, expect, it } from 'vitest';
import { NATIVE_MIME, isImageFormat, pickPanel } from './docsource';

/* One panel per document, and only one.
 *
 *   The document window decided its raster panel with `!native || fallback ||
 *   !showPdf`. `showPdf` is `pdf && …`, so `!showPdf` is true for EVERY
 *   non-PDF: a txt file mounted its own readable text frame AND the server's
 *   raster pager beneath it. The pager endpoint answers 415 for txt/md/html,
 *   so 165 of the 182 rows in the library displayed a perfectly readable
 *   document with a red "no server-side preview for this format" strip under
 *   it — and Authoring's Preview tab and Research's file view had the mirror
 *   mistake, sending every non-PDF artifact to that same 415.
 *
 *   The choice is a pure function now, so the whole truth table is checkable
 *   without a browser. */
describe('pickPanel', () => {
  it('draws a pdf with pdf.js', () => {
    expect(pickPanel('pdf')).toBe('pdf');
    expect(pickPanel('PDF')).toBe('pdf');
  });

  it('gives a pdf the pager only when asked or when drawing failed', () => {
    expect(pickPanel('pdf', { pages: true })).toBe('raster');
    expect(pickPanel('pdf', { failed: true })).toBe('raster');
  });

  it('hands text and html to the browser, never to the pager', () => {
    for (const f of ['txt', 'text', 'log', 'md', 'markdown', 'csv', 'tsv',
                     'html', 'htm', 'json']) {
      expect(pickPanel(f)).toBe('frame');
    }
  });

  it('keeps a failed text load on the frame, where the error can be said', () => {
    // `failed` is a pdf.js signal; sending a txt to the pager would answer 415
    // and replace a legible error with an unrelated one.
    expect(pickPanel('txt', { failed: true })).toBe('frame');
  });

  it('shows a deck as its slide outline, not the raster pager', () => {
    // There is no LibreOffice/PowerPoint on this machine to rasterise a .pptx,
    // so the old 'raster' panel drew a red "no server-side preview" strip under
    // a file that opens fine in PowerPoint. The outline is read from the
    // artifact's own XML instead.
    expect(pickPanel('pptx')).toBe('slides');
    expect(pickPanel('PPTX')).toBe('slides');
  });

  it('falls back to the pager for a format neither can draw', () => {
    for (const f of ['docx', 'xlsx', '']) {
      expect(pickPanel(f)).toBe('raster');
    }
  });

  it('never sends a format the browser can read to the pager', () => {
    // The old condition was a disjunction over three tests, so two panels
    // could be true at once. A single returned value makes that impossible:
    // whatever the flags, a readable format never lands on the 415 endpoint.
    for (const f of ['txt', 'md', 'html', 'json', 'csv']) {
      for (const failed of [false, true]) {
        for (const pages of [false, true]) {
          expect(pickPanel(f, { failed, pages })).not.toBe('raster');
        }
      }
    }
  });

  /* A fetched photograph had no display path anywhere in the console: an image
     format fell through to the server's page pager, which does not rasterise
     images, so the Chat transcript showed an artifact that could not be looked
     at. That is why `fetch_image` seemed to do nothing. */
  it('draws an image in its own panel, never through the pager', () => {
    for (const f of ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'avif']) {
      for (const failed of [false, true]) {
        for (const pages of [false, true]) {
          expect(pickPanel(f, { failed, pages })).toBe('image');
        }
      }
    }
  });

  it('recognises an image format only when it is safe to use as an img source', () => {
    expect(isImageFormat('PNG')).toBe(true);
    expect(isImageFormat('jpeg')).toBe(true);
    expect(isImageFormat('pdf')).toBe(false);
    expect(isImageFormat(undefined)).toBe(false);
    /* SVG is XML that can carry a script element. Loaded from library bytes into
       a blob URL and used as an <img> src it is a stored-XSS path against the
       console's own origin, so it is excluded here and routed to the sandboxed
       frame instead — the one format deliberately NOT in NATIVE_MIME despite
       being in NATIVE_FORMATS. */
    expect(isImageFormat('svg')).toBe(false);
    expect(pickPanel('svg')).toBe('frame');
    expect(NATIVE_MIME.svg).toBeUndefined();
  });
});
