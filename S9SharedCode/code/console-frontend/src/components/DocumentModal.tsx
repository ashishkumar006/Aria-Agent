import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion } from 'motion/react';
import { Download, FileText, Images, MessageSquareText, X } from 'lucide-react';
import { useFocusTrap } from '../lib/a11y';
import { DocumentViewer } from '../views/DocSetup';
import { PdfViewer, type PdfQuote, type QuoteHit } from './PdfViewer';
import { BlobFrame, ImagePane, useBlobFrame } from './FilePreview';
import { formatOf, isPdf, pickPanel, targetKey, type DocumentTarget } from '../lib/docsource';

export type { DocumentTarget };

/** Only a PDF has two renderings worth switching between; for text there is
 *  nothing to fall back to, so the toggle would offer a blank panel. */
export function hasRasterFallback(target: DocumentTarget): boolean {
  return isPdf(target);
}

/** A document opened as a window over the page: centred, with the rest of the
 *  console blurred behind it — the same shape Chat's "read full report" uses,
 *  because that is how a reader expects a document to arrive.
 *
 *  A PDF is drawn page by page with pdf.js rather than handed to the browser's
 *  built-in viewer. That was tried first and it failed in both shells: the
 *  viewer is a plugin, and a sandboxed frame — which the PDF frame must be, or
 *  an uploaded document would run with the console's own privileges — disables
 *  plugins, so Chrome answered "Blocked by Chrome" and the desktop app painted
 *  a dark rectangle. Drawing the pages ourselves also puts the words in the
 *  DOM, which is the only way a citation can be highlighted. */
export function DocumentModal({ open, target, onClose, onDownload, onDiscuss,
                                quotes, active }: {
  open: boolean;
  target: DocumentTarget | null;
  onClose: () => void;
  onDownload?: () => void;
  /** Hand this document to Chat as the thing being discussed. */
  onDiscuss?: () => void;
  /** Quotes to light up on the page, from a discussion about this document. */
  quotes?: PdfQuote[];
  active?: { id: string; nonce: number } | null;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [error, setError] = useState('');
  // Set when the drawing of a PDF failed; the server-rendered pages then take
  // over without the user having to ask.
  const [fallback, setFallback] = useState(false);
  const [forced, setForced] = useState<'auto' | 'raster'>('auto');

  useFocusTrap(panel, { active: open, initialFocus: closeRef, onEscape: onClose });

  /* Text and HTML are the browser's own views, drawn from a blob URL — which
     is what gives them scrolling and find-in-page. The blob lives in
     `useBlobFrame`, shared with the in-page file viewers because the part
     worth sharing is the revoke. */
  const frame = useBlobFrame(target, open);
  /* One panel, chosen by the same function every other viewer uses. The test
     here used to be `!native || fallback || !showPdf`, and `!showPdf` is true
     for EVERY non-PDF — so a txt file drew its own text frame AND the server
     raster under it, and that endpoint answers 415 for txt/md/html. 165 of the
     182 library rows therefore showed a red "no server-side preview" strip
     beneath a perfectly readable document. */
  const show = pickPanel(formatOf(target),
                          { failed: fallback, pages: forced === 'raster' });
  const shown = error || frame.error;

  // A fresh document resets a fallback that was decided for the last one.
  useEffect(() => {
    setFallback(false);
    setForced('auto');
    setError('');
  }, [targetKey(target)]);

  if (!target) return null;

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[300] flex items-center justify-center bg-black/55 p-4 backdrop-blur-[3px]"
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          transition={{ duration: 0.14 }}
          onClick={onClose}
          role="dialog"
          aria-modal="true"
          aria-label={`${target.label || 'Document'} — full view`}
        >
          <motion.div
            ref={panel}
            className="glass flex h-[92vh] max-h-[92vh] w-full max-w-[1400px] flex-col overflow-hidden rounded-xl border border-glass-border bg-surface-1 shadow-depth"
            initial={{ opacity: 0, y: -10, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -6, scale: 0.99 }}
            transition={{ type: 'spring', stiffness: 420, damping: 34 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex flex-none items-center gap-2 border-b border-glass-border px-3 py-2">
              <FileText size={13} className="flex-none text-zinc-muted" />
              <p className="min-w-0 flex-1 truncate text-[12.5px] font-semibold text-zinc-100"
                 title={target.label}>
                {target.label || 'Document'}
              </p>
              {hasRasterFallback(target) && (
                <button type="button"
                        onClick={() => setForced(forced === 'raster' ? 'auto' : 'raster')}
                        title={forced === 'raster'
                          ? 'Draw the pages with the document viewer again'
                          : 'Show page images made by the server instead'}
                        className="flex flex-none items-center gap-1 rounded border border-glass-border px-1.5 py-0.5 text-[11px] text-zinc-muted hover:text-zinc-200">
                  <Images size={11} /> {forced === 'raster' ? 'Viewer' : 'Pages'}
                </button>
              )}
              {onDiscuss && (
                <button type="button" onClick={onDiscuss}
                        title="Talk to Aria about this document"
                        className="flex flex-none items-center gap-1 rounded border border-glass-border px-1.5 py-0.5 text-[11px] text-zinc-300 hover:bg-white/5">
                  <MessageSquareText size={11} /> Discuss
                </button>
              )}
              {onDownload && (
                <button type="button" onClick={onDownload}
                        title="Download this file"
                        className="flex flex-none items-center gap-1 rounded border border-glass-border px-1.5 py-0.5 text-[11px] text-zinc-300 hover:bg-white/5">
                  <Download size={11} /> Download
                </button>
              )}
              <button ref={closeRef} type="button" onClick={onClose}
                      title="Close (Esc)"
                      className="flex flex-none rounded p-1 text-zinc-muted hover:bg-white/5 hover:text-zinc-100">
                <X size={14} />
              </button>
            </div>

            <div className="docview-frame flex min-h-0 flex-1 flex-col border-0">
              {shown && (
                <p className="flex-none border-b border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-[11.5px] text-amber-200">
                  {shown}
                  {show === 'raster' ? ' — showing the rendered pages instead.' : ''}
                </p>
              )}
              {show === 'pdf' && (
                <PdfViewer key={targetKey(target)} target={target}
                           quotes={quotes} active={active}
                           onError={(m) => { setError(m); setFallback(true); }} />
              )}
              {/* The browser's own view of a text or HTML document. The sandbox
                  that keeps an uploaded .html from reading the per-launch token
                  out of this page lives in `BlobFrame` (components/FilePreview),
                  which is now the one blob frame in the console — both the
                  window and the in-page file viewers go through it, and
                  tests/test_iframe_sandbox.py asserts against that file. */}
              {show === 'frame' && (frame.url
                ? <BlobFrame url={frame.url} label={target.label || 'document'} />
                : <p className="p-6 text-[12px] text-zinc-muted">
                  {frame.busy ? 'loading the document…'
                        : frame.error ? 'this document could not be opened.'
                        : 'nothing to show'}
                </p>)}
              {show === 'image' && (
                /* The panel a photograph gets everywhere else in the console.
                   This branch did not exist: the modal tested for 'pdf',
                   'frame' and 'raster' only, so an image — whose format used to
                   resolve to the library's word "image", matching none of them
                   — opened as a titled, empty panel. */
                <ImagePane url={frame.url} label={target.label || 'image'}
                           busy={frame.busy} error={frame.error} />
              )}
              {show === 'raster' && (
                <DocumentViewer source={target} />
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

export type { PdfQuote, QuoteHit };
export default DocumentModal;
