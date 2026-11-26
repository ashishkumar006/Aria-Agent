/** Where a document's bytes come from, and what type they are.
 *
 *  Two addresses reach the same file: an artifact (the exact bytes a run
 *  delivered) and a Documents-library row (what those bytes were indexed into).
 *  The viewer used to hardcode `application/pdf` for the blob URL, which made a
 *  text upload arrive labelled as a PDF. The type is derived from the format
 *  here, once, so no viewer can get it wrong again.
 */
import { api } from '../api';

/** What a viewer shows: an artifact's bytes, or a stored document's. */
export type DocumentTarget =
  | { kind: 'artifact'; artifact: string; format: string; label: string }
  | { kind: 'document'; docId: string; format: string; label: string };

export const targetKey = (t: DocumentTarget | null): string =>
  (t && (t.kind === 'artifact' ? `a:${t.artifact}` : `d:${t.docId}`)) || '';

/** Formats worth rendering rather than rasterising server-side. */
export const NATIVE_FORMATS = new Set([
  'pdf', 'txt', 'text', 'log', 'md', 'markdown', 'csv', 'tsv', 'html', 'htm', 'json',
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'avif',
  // The library's `doc_type` word for a picture. It has no extension twin, and
  // `pickPanel` tests `isImageFormat` before this set, so listing it here only
  // affects `isNativeViewable` — which said a PNG was not worth viewing.
  'image',
]);

/** The MIME a blob needs for the browser to display it instead of offering it
 *  as a download. */
export const NATIVE_MIME: Record<string, string> = {
  pdf: 'application/pdf',
  txt: 'text/plain', text: 'text/plain', log: 'text/plain',
  md: 'text/plain', markdown: 'text/plain',
  csv: 'text/plain', tsv: 'text/plain', json: 'text/plain',
  html: 'text/html', htm: 'text/html',
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif',
  webp: 'image/webp', bmp: 'image/bmp', avif: 'image/avif',
  // SVG is deliberately NOT an image MIME here. An SVG loaded into a blob URL
  // can script against the embedding document, so it is only ever shown through
  // the sandboxed frame, never as an <img> source built from library bytes.
};

export const isImageFormat = (format: string | undefined): boolean => {
  const f = (format || '').toLowerCase();
  // `image` is not an extension — it is the library's `doc_type` word for a
  // picture, and a target can carry it with no filename to refine it. An
  // `<img>` src does not need the exact type: the browser sniffs image bytes
  // whatever the blob is labelled. Routing it to the pager instead was the
  // live failure — the pager endpoint answers 415 for an image.
  if (f === 'image') return true;
  return f !== 'svg' && !!NATIVE_MIME[f] && NATIVE_MIME[f].startsWith('image/');
};

/** A target's real format, resolved from the most specific thing it carries.
 *
 *  `format` arrives in two vocabularies. An artifact target gets the file
 *  extension ("png"); a Documents-library target was filled from `doc_type`,
 *  which answers "what kind of thing is this" and collapses every picture to
 *  the single word "image" — a value with no extension twin. `pickPanel`
 *  matched neither branch for it, so a PNG fell through to the server-side
 *  raster pager, whose endpoint answers 415 for an image, and the viewer
 *  printed "no server-side preview for image" over a file the browser displays
 *  natively. Verified live in the document window, not in theory.
 *
 *  The label is the filename, which states both kinds at once
 *  (`probe.png` is a png AND an image), so it wins wherever it has an
 *  extension to offer.
 */
export const formatOf = (
  t: { format?: string; label?: string } | null | undefined,
): string => {
  // A real extension starts with a letter and is at most five characters, so a
  // name like "model v1.2024" cannot invent a format out of its version.
  const ext = /\.([A-Za-z][A-Za-z0-9]{0,4})$/.exec(t?.label || '')?.[1] || '';
  return ext.toLowerCase() || (t?.format || '').toLowerCase();
};

export const isPdf = (t: DocumentTarget | null): boolean =>
  formatOf(t) === 'pdf';

export const isNativeViewable = (t: DocumentTarget): boolean =>
  NATIVE_FORMATS.has(formatOf(t));

/** Which of the three panels shows a document — the one decision every viewer
 *  makes, kept in one place so they cannot disagree.
 *
 *  `failed` means the in-page drawing of a PDF did not work; `pages` means the
 *  user asked for the server's page raster instead of the continuous scroll.
 *
 *  Encoding this as a function is the fix for a bug that was invisible in the
 *  JSX: the raster test used to read `!showPdf`, which is true for EVERY
 *  non-PDF, so a txt file mounted its own text frame AND the raster pager under
 *  it. The pager's endpoint answers 415 for txt/md/html, so 165 of the 182
 *  library rows displayed a readable document with a red "no server-side
 *  preview" strip beneath it. Exactly one panel is now possible by
 *  construction, and the truth table is testable without a browser.
 */
export type Panel = 'pdf' | 'image' | 'frame' | 'raster' | 'slides';
export function pickPanel(
  format: string | undefined,
  { failed = false, pages = false }: { failed?: boolean; pages?: boolean } = {},
): Panel {
  const f = (format || '').toLowerCase();
  if (f === 'pdf') return failed || pages ? 'raster' : 'pdf';
  // A deck has no browser-native rendering and no server rasteriser on this
  // machine (no LibreOffice/PowerPoint), so the old 'raster' panel drew a red
  // "no server-side preview" strip under a file that opens fine in PowerPoint.
  // Show the slide OUTLINE instead - the titles, bullets and notes the server
  // already extracts from the file's own XML (docpreview._pptx). It is honest
  // (read from the artifact, not the model's `slides` metadata) and useful.
  if (f === 'pptx') return 'slides';
  // An image gets its own panel rather than the sandboxed frame, because the
  // frame renders it as a browser document: white margins, a scroll pair, and
  // no fit-to-width. This is the panel the Chat transcript needed — a fetched
  // photograph had no display path anywhere in the console.
  if (isImageFormat(f)) return 'image';
  return NATIVE_FORMATS.has(f) ? 'frame' : 'raster';
}

export type DocumentBytes = { bytes: Uint8Array; mime: string; size: number };

/** Decoded bytes, keyed by target.
 *
 *  Three surfaces can show the same document at once — the Preview tab, the
 *  Discuss workspace and the Open modal — and each used to fetch and decode
 *  its own copy, so opening the modal over an already-visible document
 *  transferred and decoded the file a second time. The HTTP layer now marks
 *  these URLs immutable, which stops the round trip; this stops the decode too
 *  and keeps the cost off the main thread.
 *
 *  Bounded by total bytes rather than entry count, because the entries are raw
 *  document bytes: a cap on entries would happily hold 12 × 5 MB. The largest
 *  produced PDF measured here is 125 KB, so 24 MB is a generous ceiling.
 */
const BYTES_CACHE_MAX = 24 * 1024 * 1024;
const bytesCache = new Map<string, DocumentBytes>();

export function cachedDocumentBytes(t: DocumentTarget): DocumentBytes | null {
  return bytesCache.get(targetKey(t)) || null;
}

/** Fetch a document's bytes for rendering.
 *
 *  An artifact is the deliverable, so it is what a viewer should show; the
 *  document row is the same bytes through the library, used when only the row
 *  is at hand (a Documents-library open, a bound discussion whose artifact
 *  handle did not survive a reload).
 *
 *  The returned buffer is SHARED with the cache. pdf.js transfers whatever
 *  buffer it is handed, which would detach the cached copy and leave every
 *  later open with a zero-length file, so callers must copy before parsing —
 *  PdfViewer does (`bytes.slice()`), and the modal only wraps it in a Blob,
 *  which copies too. A new caller that passes `bytes` straight to getDocument
 *  would corrupt the cache for everyone else.
 */
export async function loadDocumentBytes(
  t: DocumentTarget, signal?: AbortSignal,
): Promise<DocumentBytes> {
  const key = targetKey(t);
  const hit = bytesCache.get(key);
  if (hit) return hit;
  const blob = t.kind === 'artifact'
    ? await api.artifactBlob(t.artifact)
    : await api.documentContent(t.docId);
  if (signal?.aborted) throw new DOMException('aborted', 'AbortError');
  const buffer = new Uint8Array(await blob.arrayBuffer());
  const mime = NATIVE_MIME[formatOf(t)]
    || (blob.type && blob.type !== 'application/octet-stream' ? blob.type : '')
    || 'application/octet-stream';
  const out = { bytes: buffer, mime, size: buffer.length };
  // Oldest-first eviction until the total fits. Insertion order is also
  // approximate use order here, because each surface loads a document once.
  let total = [...bytesCache.values()].reduce((n, v) => n + v.size, 0);
  while (total + out.size > BYTES_CACHE_MAX && bytesCache.size > 0) {
    const oldest = bytesCache.keys().next().value as string;
    total -= (bytesCache.get(oldest)?.size || 0);
    bytesCache.delete(oldest);
  }
  if (out.size <= BYTES_CACHE_MAX) bytesCache.set(key, out);
  return out;
}
