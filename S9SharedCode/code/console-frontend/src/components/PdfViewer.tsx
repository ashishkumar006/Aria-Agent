import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getDocument, GlobalWorkerOptions, Util } from 'pdfjs-dist';
import type { PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist';
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { Maximize2, ZoomIn, ZoomOut } from 'lucide-react';
import { buildIndex, matchQuote, type PageIndex } from '../lib/pdftext';
import { loadDocumentBytes, targetKey, type DocumentTarget } from '../lib/docsource';
import './PdfViewer.css';

/** A phrase Aria quoted out of the document, with the page she says it is on.
 *  `page` 0 means she could not tell, so every rendered page is searched. */
export type PdfQuote = { id: string; page: number; quote: string };

/** Where a quote actually landed. `exact` false means the page has the words
 *  around it but not the phrase whole — enough to point at, not enough to
 *  claim a verbatim match. */
export type QuoteHit = { id: string; page: number; exact: boolean };

/** pdf.js parses in a module worker. Only the parser: painting stays on this
 *  thread, so the pages below are ordinary DOM canvases. */
GlobalWorkerOptions.workerSrc = workerSrc;

const MIN_SCALE = 0.4;
const MAX_SCALE = 3.6;

/** A page is drawn once it comes within this many pixels of the viewport, so
 *  scrolling stays ahead of the ink without painting sixty pages up front.
 *  Deferred, never dropped: every page of the document is reachable. */
const RENDER_MARGIN = 700;

type Cell = { str: string; left: number; top: number; width: number; height: number };

/** Fragments in scale-1 page units, so multiplying by `scale` is the only
 *  thing that changes on a zoom and the text index is built once per page
 *  however many times it is re-rendered. */
async function readCells(page: PDFPageProxy): Promise<Cell[]> {
  const vp = page.getViewport({ scale: 1 });
  const tc = await page.getTextContent();
  const out: Cell[] = [];
  for (const raw of tc.items) {
    const item = raw as { str?: string; transform?: number[]; width?: number; height?: number };
    if (typeof item.str !== 'string' || !item.str.trim() || !item.transform) continue;
    const tx = Util.transform(vp.transform, item.transform as number[]);
    const fontHeight = Math.hypot(tx[2], tx[3]);
    const height = fontHeight || item.height || 0;
    if (!height) continue;
    out.push({
      str: item.str,
      left: tx[4],
      top: tx[5] - height,
      width: item.width || 0,
      height,
    });
  }
  return out;
}

const clampScale = (n: number): number =>
  Number.isFinite(n) && n > 0 ? Math.max(MIN_SCALE, Math.min(MAX_SCALE, n)) : 1;

/** One page: the painted glyphs, invisible words over them, citations over
 *  those. */
function PdfPage({ doc, n, scale, quotes, activeId, scrollRoot, onMatched }: {
  doc: PDFDocumentProxy;
  n: number;
  scale: number;
  quotes: PdfQuote[];
  activeId: string | null;
  scrollRoot: HTMLElement | null;
  onMatched: (n: number, hits: QuoteHit[]) => void;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const hlRef = useRef<HTMLDivElement>(null);
  const cellsRef = useRef<Cell[] | null>(null);
  const idxRef = useRef<PageIndex | null>(null);
  const quotesRef = useRef(quotes);
  quotesRef.current = quotes;
  const activeRef = useRef(activeId);
  activeRef.current = activeId;
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const [visible, setVisible] = useState(false);

  /* Geometry first, before any ink: the placeholder needs its real height or
     the scrollbar lies and every lazy page jumps as it arrives. */
  useEffect(() => {
    let live = true;
    void doc.getPage(n).then((p) => {
      if (!live) return;
      const v = p.getViewport({ scale: 1 });
      setSize({ w: v.width, h: v.height });
      p.cleanup();
    }).catch(() => { /* reported when the canvas is asked to paint */ });
    return () => { live = false; };
  }, [doc, n]);

  useEffect(() => {
    const el = holder.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) setVisible(true);
    }, { root: scrollRoot, rootMargin: `${RENDER_MARGIN}px 0px` });
    io.observe(el);
    return () => io.disconnect();
  }, [scrollRoot]);

  /* A zoom re-paints and re-positions; a new quote elsewhere in the document
     must not repaint the fifty pages that do not contain it, which is why the
     dependency is the signature of the list rather than the list. */
  const sig = useMemo(
    () => `${quotes.map((q) => `${q.id}\u0000${q.page}\u0000${q.quote}`).join('|')}#${activeId || ''}`,
    [quotes, activeId],
  );

  useEffect(() => {
    if (!visible || !size) return;
    let live = true;
    let task: { promise: Promise<void>; cancel(): void } | null = null;
    void (async () => {
      const page = await doc.getPage(n);
      if (!live) { page.cleanup(); return; }
      const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
      const cssV = page.getViewport({ scale });
      const inkV = page.getViewport({ scale: scale * dpr });
      const canvas = canvasRef.current;
      if (canvas) {
        /* The ink is drawn at device resolution and shown at CSS size, or the
           page reads as bitmap on a hi-dpi screen. pdf.js takes the context
           itself (alpha off) but does not size the element — that is ours. */
        canvas.width = Math.floor(inkV.width);
        canvas.height = Math.floor(inkV.height);
        canvas.style.width = `${cssV.width}px`;
        canvas.style.height = `${cssV.height}px`;
        try {
          task = page.render({ canvas, viewport: inkV });
          await task.promise;
        } catch (e) {
          if ((e as Error)?.name !== 'RenderingCancelledException') {
            console.warn(`pdf page ${n} did not paint`, e);
          }
        }
      }
      if (!live) { page.cleanup(); return; }

      if (!cellsRef.current) {
        cellsRef.current = await readCells(page);
        idxRef.current = buildIndex(cellsRef.current);
      }
      const idx = idxRef.current;
      const cells = cellsRef.current;
      const textEl = textRef.current;
      if (textEl && idx && cells) {
        textEl.replaceChildren();
        const frag = document.createDocumentFragment();
        cells.forEach((c, i) => {
          const span = document.createElement('span');
          span.textContent = c.str;
          span.style.left = `${c.left * scale}px`;
          span.style.top = `${c.top * scale}px`;
          span.style.fontSize = `${c.height * scale}px`;
          const w = idx.cells[i].width * scale;
          if (w > 0) span.style.width = `${w}px`;
          frag.appendChild(span);
        });
        textEl.appendChild(frag);
      }

      /* Citations. Every quote is tested against this page, so a phrase put on
         the wrong page is still FOUND somewhere and the chip can say so — but it
         is only painted on the page the model named when it named one.
         That is a deliberate reversal. Testing every page for a bar as well lit
         the same common words up all over the document: a citation for page 1's
         table row also matched "stationary storage" inside page 2's prose, and a
         running header repeated on all seven pages, so the reader saw lines
         highlighted that had nothing to do with the answer. The chip's "found on
         p.5" and the click that scrolls there still work — a quote is reported
         wherever it lands, it just isn't painted there. */
      const hlEl = hlRef.current;
      if (hlEl && idx) {
        hlEl.replaceChildren();
        const hits: QuoteHit[] = [];
        const frag = document.createDocumentFragment();
        for (const q of quotesRef.current) {
          const m = matchQuote(idx, q.quote);
          if (!m) continue;
          hits.push({ id: q.id, page: n, exact: m.exact });
          if (q.page > 0 && q.page !== n) continue;   // found, not painted
          for (const r of m.rects) {
            const bar = document.createElement('i');
            bar.style.left = `${r.left * scale - 1}px`;
            bar.style.top = `${r.top * scale - 1}px`;
            bar.style.width = `${Math.max(2, r.width * scale) + 2}px`;
            bar.style.height = `${Math.max(3, r.height * scale) + 2}px`;
            if (!m.exact) bar.className = 'soft';
            if (q.id === activeRef.current) bar.classList.add('focus');
            frag.appendChild(bar);
          }
        }
        hlEl.appendChild(frag);
        onMatched(n, hits);
      }
      page.cleanup();
    })();
    return () => {
      live = false;
      task?.cancel();
    };
    // `sig` carries the quote list and the active id, so a quote on another
    // page does not repaint these fifty. `scale` and `size` must be here too:
    // the page box is styled from them by React, so leaving them out grew the
    // box while the canvas kept its old width and zoom showed a blank gutter
    // instead of a bigger page. `size` also gates the start below, and it
    // arrives after mount from the geometry effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, size, scale, sig, doc, n, onMatched]);

  return (
    <div ref={holder} data-page={n} className="pdfv-block"
         style={{ width: size ? `${size.w * scale}px` : undefined,
                  height: size ? `${size.h * scale}px` : undefined }}>
      <div className="pdfv-page paged"
           style={{ width: size ? `${size.w * scale}px` : undefined,
                    height: size ? `${size.h * scale}px` : undefined }}>
        <canvas ref={canvasRef} className="pdfv-canvas" />
        <div className="pdfv-text" ref={textRef} aria-hidden="true" />
        <div className="pdfv-hl" ref={hlRef} />
      </div>
    </div>
  );
}

export function PdfViewer({ target, quotes, active, onHits, onError, toolbar = true }: {
  target: DocumentTarget;
  /** Every quote worth lighting up, from any turn of the discussion. */
  quotes?: PdfQuote[];
  /** Quote to scroll to and pulse. `nonce` lets the same chip be clicked twice. */
  active?: { id: string; nonce: number } | null;
  /** What the pages reported back, so the chat can say where a quote landed. */
  onHits?: (hits: QuoteHit[]) => void;
  /** Reported once, when the file could not be opened at all — the caller then
   *  decides whether to show the server-rendered pages instead. */
  onError?: (message: string) => void;
  toolbar?: boolean;
}) {
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [scale, setScale] = useState(1);
  const [fit, setFit] = useState(true);
  const [page, setPage] = useState(1);
  const scrollRef = useRef<HTMLDivElement>(null);
  const baseW = useRef(595);
  const hitsRef = useRef(new Map<string, QuoteHit[]>());
  const onHitsRef = useRef(onHits);
  onHitsRef.current = onHits;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  useEffect(() => {
    let live = true;
    let task: ReturnType<typeof getDocument> | null = null;
    setLoading(true);
    setError('');
    setDoc(null);
    hitsRef.current = new Map();
    void (async () => {
      try {
        const { bytes } = await loadDocumentBytes(target);
        if (!live) return;
        if (!bytes.byteLength) throw new Error('the file held no bytes');
        task = getDocument({
          // A copy: pdf.js hands the buffer to its worker and detaches the
          // original, and a re-open of the same target must not find it empty.
          data: bytes.slice(),
          verbosity: 0,
        });
        const opened = await task.promise;
        if (!live) { void task.destroy(); return; }
        const first = await opened.getPage(1);
        baseW.current = first.getViewport({ scale: 1 }).width || 595;
        first.cleanup();
        setDoc(opened);
        setLoading(false);
      } catch (e) {
        if (!live) return;
        const msg = String((e as Error)?.message || e);
        setError(msg);
        setLoading(false);
        onErrorRef.current?.(msg);
      }
    })();
    return () => {
      live = false;
      /* Destroying the loading task tears down the document and its worker;
         holding the proxy open after unmount leaks the parsed page tree. */
      if (task) void task.destroy();
      else setDoc(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetKey(target)]);

  /* Fit-to-width, re-measured when the pane resizes: a document that stopped
     fitting is the oldest complaint about every PDF viewer. */
  useEffect(() => {
    if (!fit || !doc) return;
    const el = scrollRef.current;
    if (!el) return;
    const apply = () => setScale(clampScale((el.clientWidth - 34) / baseW.current));
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(el);
    return () => ro.disconnect();
  }, [fit, doc]);

  /* Stable identity on purpose: this runs inside the paint effect, and a
     callback that changed every render would repaint every visible page each
     time a quote was reported. */
  const onMatched = useCallback((n: number, hits: QuoteHit[]) => {
    const map = hitsRef.current;
    const key = String(n);
    const before = (map.get(key) || []).map((h) => `${h.id}:${h.exact}`).join(',');
    map.set(key, hits);
    const after = hits.map((h) => `${h.id}:${h.exact}`).join(',');
    if (before !== after) onHitsRef.current?.([...map.values()].flat());
  }, []);

  const quotesList = useMemo(() => quotes || [], [quotes]);
  const activeId = active?.id || null;

  /* Jumping to a citation: the page that reported the quote, else the page the
     model named. A quote found nowhere still moves the reader to where Aria
     said to look — saying nothing would hide that the words were not there. */
  useEffect(() => {
    if (!active || !doc) return;
    const root = scrollRef.current;
    if (!root) return;
    const found = [...hitsRef.current.values()].flat().find((h) => h.id === active.id);
    const want = found?.page
      || (quotesList.find((q) => q.id === active.id)?.page || 0);
    const el = root.querySelector<HTMLElement>(`[data-page="${want}"]`)
      || root.querySelector<HTMLElement>('[data-page="1"]');
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active?.id, active?.nonce, doc]);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root || !doc) return;
    const onScroll = () => {
      const mid = root.scrollTop + root.clientHeight * 0.35;
      let n = 1;
      root.querySelectorAll<HTMLElement>('[data-page]').forEach((el) => {
        if (el.offsetTop <= mid) n = Number(el.dataset.page) || n;
      });
      setPage(n);
    };
    root.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
    return () => root.removeEventListener('scroll', onScroll);
  }, [doc]);

  const zoomBy = (f: number) => { setFit(false); setScale((s) => clampScale(s * f)); };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {toolbar && (
        <div className="flex flex-none items-center gap-1.5 border-b border-white/10 px-2 py-1
                        text-[11px] text-zinc-400">
          <button type="button" onClick={() => zoomBy(1 / 1.2)} title="Zoom out"
                  aria-label="Zoom out"
                  className="rounded border border-white/10 p-1 hover:border-violet-400 hover:text-zinc-100">
            <ZoomOut size={12} />
          </button>
          <span className="w-11 text-center tabular-nums">{Math.round(scale * 100)}%</span>
          <button type="button" onClick={() => zoomBy(1.2)} title="Zoom in"
                  aria-label="Zoom in"
                  className="rounded border border-white/10 p-1 hover:border-violet-400 hover:text-zinc-100">
            <ZoomIn size={12} />
          </button>
          <button type="button" onClick={() => setFit(true)} title="Fit the page to this pane"
                  className={`flex items-center gap-1 rounded border px-1.5 py-0.5 ${
                    fit ? 'border-violet-400 text-zinc-100' : 'border-white/10 hover:text-zinc-100'}`}>
            <Maximize2 size={11} /> Fit width
          </button>
          <span className="flex-1" />
          {doc && <span className="tabular-nums">page {page} / {doc.numPages}</span>}
        </div>
      )}
      <div ref={scrollRef} className="pdfv-scroll min-h-0 flex-1 p-3">
        {loading && <p className="p-6 text-[12px] text-zinc-muted">drawing the pages…</p>}
        {error && (
          <p role="alert"
             className="m-3 rounded border border-red-400/30 bg-red-400/10 p-2.5 text-[12px] text-red-200">
            This PDF could not be drawn: {error}
          </p>
        )}
        {doc && Array.from({ length: doc.numPages }, (_, i) => i + 1).map((n) => (
          <PdfPage key={`${targetKey(target)}:${n}`}
                   doc={doc} n={n} scale={scale} quotes={quotesList} activeId={activeId}
                   scrollRoot={scrollRef.current} onMatched={onMatched} />
        ))}
      </div>
    </div>
  );
}

export default PdfViewer;
