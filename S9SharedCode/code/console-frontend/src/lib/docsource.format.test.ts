import { describe, expect, it } from 'vitest';
import { formatOf, isImageFormat, isNativeViewable, isPdf, pickPanel } from './docsource';

/* `format` reaches a viewer in two vocabularies, and only one of them is
 * complete.
 *
 *   An artifact target carries the file extension — "png". A Documents-library
 *   target was filled from `doc_type`, which answers "what KIND of thing is
 *   this" and collapses every picture to the single word "image". For txt, pdf,
 *   docx, md and html the two vocabularies happen to agree, which is why this
 *   only ever showed up on a photograph: `pickPanel("image")` matched no branch,
 *   fell through to the server's page pager, and that endpoint answers 415 for
 *   an image — so the document window printed "no server-side preview for
 *   image" over a PNG the browser can display natively. Reproduced in the live
 *   console against `probe-red-panda.png`.
 *
 *   The label is the filename, which states both kinds at once, so it decides
 *   wherever it has an extension to offer. */
describe('formatOf', () => {
  it('reads a library image row as the picture it is', () => {
    const t = { format: 'image', label: 'probe-red-panda.png' };
    expect(formatOf(t)).toBe('png');
    expect(pickPanel(formatOf(t))).toBe('image');
    // The regression, stated directly: this is what the viewer drew before.
    expect(pickPanel(t.format)).not.toBe('raster');
  });

  it('treats the bare doc_type word as an image too', () => {
    // A target with no filename to refine it must still not reach the pager.
    expect(isImageFormat('image')).toBe(true);
    expect(pickPanel('image')).toBe('image');
    expect(isNativeViewable({ kind: 'document', docId: 'd1',
                              format: 'image', label: 'untitled' })).toBe(true);
  });

  it('leaves every format the two vocabularies already agree on alone', () => {
    for (const f of ['pdf', 'txt', 'docx', 'md', 'html', 'csv', 'json']) {
      expect(formatOf({ format: f, label: `report.${f}` })).toBe(f);
    }
    expect(formatOf({ format: 'pdf', label: 'deck.pdf' })).toBe('pdf');
    expect(isPdf({ kind: 'document', docId: 'd', format: 'pdf',
                   label: 'deck.pdf' })).toBe(true);
  });

  it('prefers the label extension when the two disagree', () => {
    expect(formatOf({ format: 'pdf', label: 'notes.txt' })).toBe('txt');
  });

  it('falls back to the declared format when the label has no extension', () => {
    expect(formatOf({ format: 'JPG', label: 'photo' })).toBe('jpg');
    expect(formatOf({ label: 'no format here' })).toBe('');
  });

  it('is empty rather than a crash when there is no target at all', () => {
    expect(formatOf(null)).toBe('');
    expect(formatOf(undefined)).toBe('');
    // …and an empty format is not viewable, not a picture, not a pdf.
    expect(pickPanel(formatOf(null))).toBe('raster');
    expect(isImageFormat(formatOf(null))).toBe(false);
  });

  it('does not mistake a dotted folder or a long suffix for an extension', () => {
    // Only 1-5 alphanumerics after the last dot count, so a labelled
    // "v1.2024" or an oddball ".backup" cannot invent a format.
    expect(formatOf({ format: 'image', label: 'red panda photo.jpeg' }))
      .toBe('jpeg');
    expect(pickPanel(formatOf({ format: 'image', label: 'v1.2024' })))
      .toBe('image');
  });
});
