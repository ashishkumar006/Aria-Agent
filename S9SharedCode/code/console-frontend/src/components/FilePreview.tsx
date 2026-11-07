import { useEffect, useState } from 'react';
import { DocumentViewer } from '../views/DocSetup';
import { PdfViewer, type PdfQuote, type QuoteHit } from './PdfViewer';
import {
  NATIVE_MIME, formatOf, loadDocumentBytes, pickPanel, targetKey, type DocumentTarget,
} from '../lib/docsource';

/** Blob URL for a document the browser can display itself (text, HTML).
 *
 *  Both the document window and the in-page file viewers need this, and the
 *  part worth sharing is not the fetch — it is the revoke: a blob URL that is
 *  never released keeps the whole document alive for the session, and these
 *  views stay mounted behind KeepAlive after you leave them.
 *
 *  The returned panel is what `pickPanel` chooses when nothing has failed; each
 *  caller re-runs that choice with its own failure flags, and this hook's own
 *  decision is what says whether fetching bytes is worth it at all. */
export function useBlobFrame(target: DocumentTarget | null, active: boolean) {
  const panel = pickPanel(formatOf(target));
  const key = targetKey(target);
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setUrl('');
    setError('');
    // Both byte-rendered panels need the bytes: the frame shows them as a
    // document, the image shows them as an `<img>`. The image panel was added
    // after this hook existed, and the guard still said `panel !== 'frame'`,
    // so it never fetched and the photograph slot printed "nothing to show"
    // for a file that had just been downloaded successfully.
    if (!active || !target || (panel !== 'frame' && panel !== 'image')) return;
    let live = true;
    let revoked = '';
    setBusy(true);
    loadDocumentBytes(target).then(({ bytes, mime }) => {
      if (!live) return;
      const want = NATIVE_MIME[formatOf(target)] || mime;
      revoked = URL.createObjectURL(new Blob([bytes as BlobPart], { type: want }));
      setUrl(revoked);
      setBusy(false);
    }).catch((e: unknown) => {
      if (!live) return;
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    });
    return () => {
      live = false;
      if (revoked) URL.revokeObjectURL(revoked);
    };
    // `target` itself changes identity every render in these views; `key` is
    // the value that says "a different document arrived".
  }, [active, key, panel]);                // eslint-disable-line react-hooks/exhaustive-deps

  return { url, busy, error };
}

/** The sandbox is load-bearing, not cosmetic, and it only ever wraps a format
 *  the browser renders as a document — HTML or text. The src is a blob: URL, so
 *  the frame shares this page's origin: an uploaded .html would otherwise run
 *  with the console's own privileges, read the per-launch token out of the
 *  parent DOM and make authenticated calls as the user. `allow-same-origin`
 *  WITHOUT `allow-scripts` is the measured combination — scripts off stops the
 *  theft, same-origin keeps the document's own styling. A PDF never reaches
 *  this frame, so the plugin that sandboxing disables is no longer something
 *  anyone can trip over. tests/test_iframe_sandbox.py reads these tags. */
export function BlobFrame({ url, label, className }: {
  url: string; label: string; className?: string;
}) {
  return <iframe title={label || 'document'} src={url}
                 sandbox="allow-same-origin" referrerPolicy="no-referrer"
                 className={className || 'min-h-0 w-full flex-1 border-0 bg-[#0b0b0f]'} />;
}

/** A photograph, not a document: centred, fit to the width it has, on the dark
 *  ground so a white-background scan does not glare. `object-contain` keeps the
 *  aspect ratio — a stretched photo is worse than none.
 *
 *  Exported, and used by both viewers, because the image case was added to the
 *  in-page preview and not to `DocumentModal`, and the modal has no else-branch:
 *  a PNG opened from the Documents list drew a titled, empty panel with nothing
 *  in it at all. Verified live. One component means the two cannot disagree
 *  about what an image looks like again. */
export function ImagePane({ url, label, busy, error, className }: {
  url: string; label: string; busy?: boolean; error?: string;
  className?: string;
}) {
  if (!url) {
    return <p className={className || 'p-6 text-[12px] text-zinc-muted'}>
      {busy ? 'loading the image…'
        : error ? 'this image could not be opened.'
        : 'nothing to show'}
    </p>;
  }
  return (
    <div className={(className || '') + ' flex min-h-0 flex-1 items-center justify-center overflow-auto bg-black/30 p-3'}>
      <img src={url} alt={label} className="max-h-full max-w-full object-contain" />
    </div>
  );
}

/** A produced file, shown in place: pdf.js for a PDF, the browser's own text or
 *  HTML view for those formats, and the server's page raster only for what
 *  neither can draw.
 *
 *  This used to read `if (!isPdf) return <DocumentViewer/>`, which sent every
 *  txt, md, csv, json and html artifact to a rasterising endpoint that answers
 *  415 for all of them — the Preview tab and Research's file view therefore
 *  showed a red "no server-side preview" strip under a file that opens fine
 *  anywhere else.
 *
 *  Module scope on purpose: a component defined inside a view gets a new
 *  identity every render, which remounts the viewer, re-downloads the document
 *  and resets the zoom.
 *
 *  `className` sizes the panels that scroll inside a definite height;
 *  `rasterClassName` sizes the pager, whose natural box differs per host — a
 *  document window wants `docview-frame`, the Discuss page wants the flex
 *  remainder of its own column. */
export function FilePreview({ target, className, rasterClassName,
                              quotes, active, onHits }: {
  target: DocumentTarget;
  className?: string;
  rasterClassName?: string;
  quotes?: PdfQuote[];
  active?: { id: string; nonce: number } | null;
  onHits?: (hits: QuoteHit[]) => void;
}) {
  const doc = targetKey(target);
  // A pager decision belongs to one document. Without this reset, a PDF that
  // pdf.js could not open would leave every later file in this slot on the
  // one-page-at-a-time view — the "it shows slides instead of scrolling"
  // report, which is really about the file that came before.
  const [failed, setFailed] = useState(false);
  useEffect(() => { setFailed(false); }, [doc]);

  const panel = pickPanel(formatOf(target), { failed });
  const box = className || 'flex h-[70vh] min-h-[420px] flex-col overflow-hidden';
  const pager = rasterClassName || 'docview-frame';
  const frame = useBlobFrame(target, true);

  if (panel === 'pdf') {
    return (
      <div className={box}>
        <PdfViewer key={doc} target={target} quotes={quotes} active={active}
                   onHits={onHits} onError={() => setFailed(true)} />
      </div>
    );
  }
  if (panel === 'image') {
    return <div className={box}>
      <ImagePane url={frame.url} label={target.label} busy={frame.busy}
                 error={frame.error} />
    </div>;
  }
  if (panel === 'frame') {
    return <div className={box}>
      {frame.url
        ? <BlobFrame url={frame.url} label={target.label} />
        : <p className="p-6 text-[12px] text-zinc-muted">
            {frame.busy ? 'loading the document…'
              : frame.error ? 'this document could not be opened.'
              : 'nothing to show'}
          </p>}
    </div>;
  }
  return <div className={pager}><DocumentViewer source={target} /></div>;
}
