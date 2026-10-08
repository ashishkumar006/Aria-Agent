import { useEffect, useMemo, useRef, useState } from 'react';
import {
  previewDocument, SAMPLE_SPEC, PAGE_SIZES, STYLES, CITATIONS, SLIDE_SIZES,
  MARGINS, LENGTHS, DEFAULT_SETUP,
  type DocSetup, type DocPreviewResult,
} from './docpreview';

/** Which document is on screen. */
type Source =
  | { kind: 'spec'; setup: DocSetup }
  | { kind: 'artifact'; artifact: string; format: string; label: string };

function classNames(...xs: (string | false | undefined)[]) {
  return xs.filter(Boolean).join(' ');
}

/* the format names people actually say. 'auto' is a decision in itself -
 * hand the file type to the agent - so it stays first. */
const FORMATS: { id: DocSetup['format']; label: string }[] = [
  { id: 'auto', label: 'Auto' },
  { id: 'pdf', label: 'PDF' },
  { id: 'pptx', label: 'Deck' },
  { id: 'docx', label: 'Word' },
  { id: 'xlsx', label: 'Excel' },
];

const ORIENTATIONS: { id: DocSetup['orientation']; label: string }[] = [
  { id: 'portrait', label: 'Portrait' },
  { id: 'landscape', label: 'Landscape' },
];

const COLUMN_COUNTS = [1, 2, 3].map((n) => ({ id: n, label: String(n) }));

/** The choices, asked before the work starts.
 *
 * This asked nine questions in a row - format, paper, orientation, columns,
 * typeface, citations, margins, length and three toggles - which is a settings
 * dialog, not a first step, and a first-time user can answer almost none of
 * it. Only format, paper, typeface and length decide what the document IS; the
 * rest are refinements on a document that is already right at the defaults, so
 * they sit behind one disclosure and the preview beside the panel still shows
 * what they do. Defaults are chosen so a user who touches nothing gets a
 * sensible document: A4, portrait, one column, moderate margins, the report
 * typeface, a cover page and a contents list.
 */
function SetupPanel({
  setup, onChange, disabled,
}: { setup: DocSetup; onChange: (s: DocSetup) => void; disabled?: boolean }) {
  const set = <K extends keyof DocSetup>(k: K, v: DocSetup[K]) =>
    onChange({ ...setup, [k]: v });
  const isDeck = setup.format === 'pptx';
  const isSheet = setup.format === 'xlsx';

  return (
    <div className="setup" aria-label="Document setup">
      <div className="setup-row">
        {/* wide: five buttons are wider than a field's flex basis, so sharing
            a row let the segmented control spill over its neighbour - and, at
            390px, over the card and off the page. */}
        <Field label="Format" hint="what the user opens" wide>
          <Segmented label="Format" value={setup.format} options={FORMATS}
            disabled={disabled} onChange={(v) => set('format', v)} />
        </Field>

        {isDeck ? (
          <Field label="Canvas" hint="aspect ratio" htmlFor="setup-canvas">
            <select id="setup-canvas" className="setup-sel" disabled={disabled}
              value={setup.slide_size}
              onChange={(e) => set('slide_size', e.target.value)}>
              {SLIDE_SIZES.map((s) =>
                <option key={s.id} value={s.id}>{s.label}</option>)}
            </select>
          </Field>
        ) : !isSheet ? (
          <Field label="Page" hint="paper the document is set for" htmlFor="setup-page">
            <select id="setup-page" className="setup-sel" disabled={disabled}
              value={setup.page_size}
              onChange={(e) => set('page_size', e.target.value)}>
              {PAGE_SIZES.map((s) =>
                <option key={s.id} value={s.id}>
                  {s.label} — {s.mm} mm
                </option>)}
            </select>
          </Field>
        ) : null}
      </div>

      <div className="setup-row">
        {/* The option text is the SHORT name only. "Report — Sans body, numbered
          sections" was truncated to "Report — Sans body, numb" inside a
          2-column grid, so the control could not tell you what you had picked.
          The explanation belongs under the control as a hint, where there is
          room for all of it and where it describes the SELECTED option. */}
        <Field label="Style" hint={`typeface, measure, spacing${
          STYLES.find((s) => s.id === setup.style)?.what
            ? ` · ${STYLES.find((s) => s.id === setup.style)!.what}`
            : ''}`} htmlFor="setup-style">
          <select id="setup-style" className="setup-sel" disabled={disabled}
            value={setup.style} onChange={(e) => set('style', e.target.value)}>
            {STYLES.map((s) =>
              <option key={s.id} value={s.id}>{s.label}</option>)}
          </select>
        </Field>

        <Field label="Length" hint="aimed at your request if set" htmlFor="setup-length">
          <select id="setup-length" className="setup-sel" disabled={disabled}
            value={setup.length} onChange={(e) => set('length', e.target.value)}>
            {LENGTHS.map((l) =>
              <option key={l.id || 'auto'} value={l.id}>{l.label}</option>)}
          </select>
        </Field>
      </div>

      {/* one disclosure, not four rows. every control in here reaches the
          render, so they stay reachable - but a user cannot choose margins
          before they have seen a page, and the preview beside the panel
          already shows what the defaults produced. A sheet has no page
          furniture at all, so for one there is nothing to disclose. */}
      {!isSheet && (
        <details className="setup-more">
          <summary className="setup-more-sum">
            More options
            <em className="setup-hint">page shape, columns, references, cover pages</em>
          </summary>

          {!isDeck && (
            <div className="setup-row">
              <Field label="Orientation" hint="page shape">
                <Segmented label="Orientation" value={setup.orientation}
                  options={ORIENTATIONS} disabled={disabled}
                  onChange={(v) => set('orientation', v)} />
              </Field>
              <Field label="Columns" hint="text columns">
                <Segmented label="Columns" value={setup.columns}
                  options={COLUMN_COUNTS} disabled={disabled}
                  onChange={(v) => set('columns', v)} />
              </Field>
            </div>
          )}

          <div className="setup-row">
            <Field label="Margins" hint="page edge" htmlFor="setup-margins">
              <select id="setup-margins" className="setup-sel" disabled={disabled}
                value={setup.margins} onChange={(e) => set('margins', e.target.value)}>
                {MARGINS.map((m) =>
                  <option key={m.id} value={m.id}>{m.label}</option>)}
              </select>
            </Field>

            <Field label="Citations" hint={`reference list${
              CITATIONS.find((c) => c.id === setup.citation_style)?.what
                ? ` · ${CITATIONS.find((c) => c.id === setup.citation_style)!.what}`
                : ''}`} htmlFor="setup-citations">
              <select id="setup-citations" className="setup-sel" disabled={disabled}
                value={setup.citation_style}
                onChange={(e) => set('citation_style', e.target.value)}>
                {CITATIONS.map((c) =>
                  <option key={c.id} value={c.id}>{c.label}</option>)}
              </select>
            </Field>
          </div>

          <div className="setup-row setup-toggles">
            <Toggle label="Cover page" checked={setup.cover}
              disabled={disabled} onChange={(v) => set('cover', v)} />
            {!isDeck && (
              <>
                <Toggle label="Contents + numbered sections" checked={setup.toc}
                  disabled={disabled} onChange={(v) => set('toc', v)} />
                <Toggle label="Running header" checked={setup.running_header}
                  disabled={disabled} onChange={(v) => set('running_header', v)} />
              </>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

/** A row of exclusive choices that behaves like the single control it claims
 *  to be.
 *
 * role=radio on one button per option gave the group five tab stops and no
 * arrow-key movement, so reaching one control cost the width of the panel and
 * the arrows a screen reader announces did nothing. The checked option is now
 * the only tab stop and the arrows move the check with the focus, which is
 * what role=radiogroup promises.
 */
function Segmented<T extends string | number>({ label, value, options, disabled, onChange }: {
  label: string; value: T; options: { id: T; label: string }[];
  disabled?: boolean; onChange: (v: T) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  /* -1 would leave the group with no tab stop at all; the value always comes
   * from the list, so this only guards a list that lost it. */
  const at = Math.max(0, options.findIndex((o) => o.id === value));
  const pick = (i: number) => {
    const n = (i + options.length) % options.length;
    onChange(options[n].id);
    /* focus follows the check: the roving tabindex has already moved the stop
     * away from the button the key was pressed on. */
    const btns = box.current?.querySelectorAll<HTMLButtonElement>('button');
    btns?.[n]?.focus();
  };
  return (
    <div className="seg" role="radiogroup" aria-label={label} ref={box}
      onKeyDown={(e) => {
        const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1
          : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
        if (step) { e.preventDefault(); pick(at + step); return; }
        if (e.key === 'Home') { e.preventDefault(); pick(0); }
        if (e.key === 'End') { e.preventDefault(); pick(options.length - 1); }
      }}>
      {options.map((o, i) => (
        <button key={o.id} type="button" role="radio"
          aria-checked={value === o.id} tabIndex={i === at ? 0 : -1}
          disabled={disabled}
          className={classNames('seg-btn', value === o.id && 'on')}
          onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** One control, with its caption.
 *
 * A <label> wrapping the segmented groups was labelling nothing - a div is not
 * a labelable element - so the wrapper is a div and a real <label for> names
 * the selects it sits next to.
 */
function Field({ label, hint, wide, htmlFor, children }:
  { label: string; hint?: string; wide?: boolean; htmlFor?: string;
    children: React.ReactNode }) {
  const caption = (
    <>
      {label}
      {hint && <em className="setup-hint">{hint}</em>}
    </>
  );
  return (
    <div className={classNames('setup-field', wide && 'setup-field-wide')}>
      {htmlFor
        ? <label className="setup-label" htmlFor={htmlFor}>{caption}</label>
        : <span className="setup-label">{caption}</span>}
      {children}
    </div>
  );
}

function Toggle({ label, checked, disabled, onChange }:
  { label: string; checked: boolean; disabled?: boolean;
    onChange: (v: boolean) => void }) {
  return (
    <label className="setup-toggle">
      <input type="checkbox" checked={checked} disabled={disabled}
        onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

/** Shows the rendered document itself: page images, drawn slides, or the
 *  saved file's own content as HTML. */
// 'auto' has no single format to draw, so the preview shows the default
// (PDF) while the run is told to choose. Showing a Word page because the
// user left the control on Auto would be its own kind of lie.
const previewFormat = (f: DocSetup['format']) => (f === 'auto' ? 'pdf' : f);

export function DocumentViewer({ source }: { source: Source }) {
  const [data, setData] = useState<DocPreviewResult | null>(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState(0);
  const [zoom, setZoom] = useState<'fit' | '100'>('fit');
  const abort = useRef<AbortController | null>(null);
  const key = source.kind === 'spec'
    ? `spec:${JSON.stringify(source.setup)}`
    : `artifact:${source.artifact}`;

  useEffect(() => {
    abort.current?.abort();
    const ctl = new AbortController();
    abort.current = ctl;
    setBusy(true);
    setErr('');
    setPage(0);
    const payload = source.kind === 'spec'
      ? { format: previewFormat(source.setup.format),
          spec: SAMPLE_SPEC(source.setup) }
      : { artifact: source.artifact, format: source.format };
    previewDocument(payload, ctl.signal)
      .then((r) => { if (!ctl.signal.aborted) setData(r); })
      .catch((e) => {
        if (!ctl.signal.aborted) setErr(String(e?.message || e));
      })
      .finally(() => { if (!ctl.signal.aborted) setBusy(false); });
    return () => ctl.abort();
  }, [key]);

  const pages = data?.pages || [];
  const shown = Math.min(page, Math.max(0, pages.length - 1));

  /* Fit means "the whole page is visible without scrolling", which is a
     HEIGHT constraint - the stage is a tall narrow box for A4 and a wide short
     one for a landscape page. Constraining width instead drew a letterboxed
     sliver, and because `max-height` was on the element rather than the
     inline style, Actual size was byte-for-byte the same picture. Actual size
     drops both constraints so the stage scrolls at natural pixels. */
  const styleFor = (w: number) => zoom === '100'
    ? { width: `${w}px`, maxWidth: 'none', maxHeight: 'none' as const }
    : { width: 'auto', maxWidth: '100%', maxHeight: '100%' as const,
        objectFit: 'contain' as const };

  return (
    <div className="docview">
      <div className="docview-bar">
        <span className="docview-title">
          {source.kind === 'artifact' ? source.label : 'Layout preview'}
        </span>
        {data && (
          <span className="docview-meta">
            {data.page_count} {data.format === 'pptx' ? 'slides' : 'pages'}
            {data.truncated && ' (first pages shown)'}
            {data.note && <em> · {data.note}</em>}
          </span>
        )}
        <span className="docview-actions">
          {pages.length > 1 && (
            <>
              <button type="button" className="docview-nav"
                aria-label="Previous page" disabled={shown <= 0}
                onClick={() => setPage(Math.max(0, shown - 1))}>‹</button>
              <span className="docview-count">{shown + 1} / {pages.length}</span>
              <button type="button" className="docview-nav"
                aria-label="Next page"
                disabled={shown >= pages.length - 1}
                onClick={() => setPage(Math.min(pages.length - 1, shown + 1))}>›</button>
            </>
          )}
          <button type="button" className="docview-zoom"
            aria-pressed={zoom === '100'}
            onClick={() => setZoom((z) => (z === 'fit' ? '100' : 'fit'))}>
            {zoom === 'fit' ? 'Actual size' : 'Fit'}
          </button>
        </span>
      </div>

      {busy && <div className="docview-state">Rendering the document…</div>}
      {err && <div className="docview-state err">{err}</div>}

      {!busy && !err && data?.mode === 'html' && (
        // Sandboxed: the markup is server-generated from the document's own
        // content, and sandbox="" keeps it away from the console's origin.
        <iframe className="docview-doc" title="Document preview"
          sandbox="" srcDoc={htmlDoc(data.html || '', data.format)} />
      )}

      {!busy && !err && data?.mode === 'pages' && pages[shown] && (
        <div className="docview-stage">
          {pages[shown].kind === 'png' ? (
            <img className="docview-page" alt={`Page ${shown + 1}`}
              src={pages[shown].data}
              style={styleFor(pages[shown].width_pt)} />
          ) : (
            <div className="docview-slide"
              style={styleFor(pages[shown].width_pt)}
              dangerouslySetInnerHTML={{ __html: pages[shown].data }} />
          )}
        </div>
      )}

      {pages.length > 1 && !busy && (
        <div className="docview-strip" role="tablist" aria-label="Pages">
          {pages.map((p, i) => (
            <button key={p.index} type="button" role="tab"
              aria-selected={i === shown} title={`Page ${i + 1}`}
              className={classNames('docview-thumb', i === shown && 'on')}
              onClick={() => setPage(i)}>
              {p.kind === 'png'
                ? <img src={p.data} alt="" />
                : <span className="docview-thumb-svg"
                    dangerouslySetInnerHTML={{ __html: p.data }} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Wrap server-rendered markup in the same page furniture the format implies. */
function htmlDoc(inner: string, format: string) {
  const wide = format === 'pptx';
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    :root { color-scheme: light }
    body { margin:0; background:#f5f5f8; font: 15px/1.55 -apple-system,
      "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color:#1d1d22;
      padding: 24px; }
    .pg { background:#fff; max-width:${wide ? 900 : 760}px; margin:0 auto;
      padding: 48px 56px; box-shadow: 0 1px 3px rgba(0,0,0,.12);
      border-radius: 2px; }
    h1 { font-size: 22px; margin: 22px 0 8px; letter-spacing:-.01em }
    h2 { font-size: 17px; margin: 18px 0 6px }
    h3 { font-size: 15px; margin: 14px 0 4px }
    p { margin: 0 0 10px }
    li { margin: 0 0 4px }
    blockquote { margin: 10px 0; padding: 6px 0 6px 14px;
      border-left: 3px solid #d8d8e0; color:#33333c; font-style: italic }
    table { border-collapse: collapse; width: 100%; margin: 12px 0;
      font-size: 13.5px }
    td, th { border: 1px solid #e2e2ea; padding: 6px 9px; text-align: left }
    tr:first-child td { background:#5b4bd6; color:#fff; font-weight: 600 }
    table.grid td.num { text-align: right; font-variant-numeric: tabular-nums }
  </style></head><body>${inner}</body></html>`;
}

export { SetupPanel, DEFAULT_SETUP, useMemo };