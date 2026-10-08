import { headers } from '../api';

/** One rendered page of a document, as the server drew it. */
export type PreviewPage = {
  index: number;
  /** `png` for a rasterised page, `svg` for a drawn slide. */
  kind: 'png' | 'svg';
  data: string;
  width_pt: number;
  height_pt: number;
};

export type DocPreviewResult = {
  format: string;
  /** `pages` for images/SVG, `html` for a document with no raster path. */
  mode: 'pages' | 'html';
  pages: PreviewPage[];
  html?: string;
  page_count: number;
  truncated?: boolean;
  /** Shown honestly when a preview is not a pixel-exact raster. */
  note?: string;
  error?: string;
};

export type DocSetup = {
  /** uto lets the agent choose; anything else is a user decision and is
   *  sent to the renderer as an explicit ormat=. */
  format: 'auto' | 'pdf' | 'pptx' | 'docx' | 'xlsx';
  page_size: string;
  orientation: 'portrait' | 'landscape';
  style: string;
  columns: number;
  margins: string;
  citation_style: string;
  slide_size: string;
  toc: boolean;
  cover: boolean;
  running_header: boolean;
  length: string;
};

/** A small, representative document for the live layout preview.
 *
 * It has to contain every block type the viewer can draw, because the point
 * of the setup panel is to show what the CHOICES look like - a paragraph
 * alone would hide whether a chart fits the column or the margins are right.
 */
export const SAMPLE_SPEC = (setup: DocSetup) => ({
  title: 'Aqueduct Engineering',
  subtitle: 'What the evidence shows',
  style: setup.style,
  page_size: setup.page_size,
  orientation: setup.orientation,
  columns: setup.columns,
  margins: setup.margins,
  citation_style: setup.citation_style,
  slide_size: setup.slide_size,
  toc: setup.toc,
  running_header: setup.running_header ? 'Aqueduct Engineering' : '',
  cover: setup.cover,
  blocks: [
    { type: 'cover', title: 'Aqueduct Engineering',
      subtitle: 'What the evidence shows',
      meta: ['Layout preview'] },
    { type: 'heading', level: 1, text: 'Hydraulic design' },
    { type: 'paragraph',
      text: 'A Roman aqueduct\'s gradient was set by the surveyor and preserved by the builder. Falling of roughly 1 in 200 sustained a steady flow without intervention, which is why the surviving channels are so uniform.' },
    { type: 'heading', level: 1, text: 'Components' },
    { type: 'bullets', items: [
      'Specus: the covered channel',
      'Ponteoli: the settling tanks',
      'Castellum: the distribution tank',
    ] },
    { type: 'heading', level: 1, text: 'Surviving structures' },
    { type: 'chart', kind: 'bar',
      categories: ['Specus', 'Pons', 'Cistern'],
      series: [{ name: 'Surviving', data: [41, 18, 60] }],
      title: 'Surviving structures by type' },
    { type: 'table', header: ['Structure', 'Purpose'],
      rows: [['Specus', 'conveys water'], ['Pons', 'crosses a valley']] },
  ],
  references: [
    { authors: 'Zhang, Jane', year: '2015',
      title: 'Hydraulic engineering and Roman aqueducts',
      container: 'Journal of Hydraulic History', volume: '12', issue: '3',
      pages: '44-59' },
    { authors: 'Ruiz, Ana', year: '2021',
      title: 'Graph recall at scale',
      container: 'Proceedings on Indexing', pages: '10-21' },
  ],
});

/** Ask the agent to render the document itself for the console to show. */
export async function previewDocument(
  payload: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<DocPreviewResult> {
  const r = await fetch('/api/doc/preview', {
    method: 'POST',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(payload),
    signal,
  });
  if (!r.ok) {
    let msg = `preview failed (${r.status})`;
    try {
      const j = await r.json();
      if (j?.error) msg = j.error;
    } catch { /* keep the status message */ }
    throw new Error(msg);
  }
  return (await r.json()) as DocPreviewResult;
}

export const PAGE_SIZES: { id: string; label: string; mm: string }[] = [
  { id: 'a4', label: 'A4', mm: '210 × 297' },
  { id: 'letter', label: 'US Letter', mm: '216 × 279' },
  { id: 'a3', label: 'A3', mm: '297 × 420' },
  { id: 'a5', label: 'A5', mm: '148 × 210' },
  { id: 'legal', label: 'US Legal', mm: '216 × 356' },
  { id: 'b5', label: 'B5', mm: '176 × 250' },
  { id: 'tabloid', label: 'Tabloid', mm: '279 × 432' },
  { id: 'executive', label: 'Executive', mm: '184 × 267' },
  { id: 'pocket', label: 'Pocket', mm: '108 × 178' },
];

export const STYLES: { id: string; label: string; what: string }[] = [
  { id: 'report', label: 'Report', what: 'Sans body, numbered sections' },
  { id: 'brief', label: 'Brief', what: 'Larger type, more air' },
  { id: 'memo', label: 'Memo', what: 'No title page, no numbering' },
  { id: 'academic', label: 'Academic', what: 'Serif, justified, first-line indents' },
  { id: 'whitepaper', label: 'Whitepaper', what: 'Long-form serif, wide margins' },
  { id: 'manual', label: 'Manual', what: 'Procedural, scannable headings' },
  { id: 'newsletter', label: 'Newsletter', what: 'Two columns' },
  { id: 'technical', label: 'Technical', what: 'Dense, compact leading' },
  { id: 'book', label: 'Book', what: 'Serif trade page' },
  { id: 'plain', label: 'Plain', what: 'Neutral' },
];

export const CITATIONS: { id: string; label: string; what: string }[] = [
  { id: 'apa', label: 'APA', what: 'Social science, alphabetical' },
  { id: 'mla', label: 'MLA', what: 'Humanities, alphabetical' },
  { id: 'chicago', label: 'Chicago', what: 'History and arts' },
  { id: 'harvard', label: 'Harvard', what: 'UK business and social science' },
  { id: 'ieee', label: 'IEEE', what: 'Engineering, numbered' },
  { id: 'vancouver', label: 'Vancouver', what: 'Medicine, numbered' },
  { id: 'ama', label: 'AMA', what: 'JAMA-style, numbered' },
  { id: 'bluebook', label: 'Bluebook', what: 'US law' },
  { id: 'oscola', label: 'OSCOLA', what: 'UK law' },
  { id: 'plain', label: 'None', what: 'List sources as given' },
];

export const SLIDE_SIZES: { id: string; label: string }[] = [
  { id: '16:9', label: '16:9 widescreen' },
  { id: '16:10', label: '16:10' },
  { id: '4:3', label: '4:3 classic' },
  { id: 'a4', label: 'A4 slide' },
  { id: 'letter', label: 'US Letter slide' },
  { id: '1:1', label: 'Square' },
];

export const MARGINS: { id: string; label: string }[] = [
  { id: 'narrow', label: 'Narrow' },
  { id: 'normal', label: 'Normal' },
  { id: 'moderate', label: 'Moderate' },
  { id: 'wide', label: 'Wide' },
  { id: 'generous', label: 'Generous' },
];

export const LENGTHS: { id: string; label: string }[] = [
  { id: '', label: 'Match the request' },
  { id: 'one page', label: 'One page' },
  { id: '3 pages', label: '3 pages' },
  { id: '6 pages', label: '6 pages' },
  { id: '12 pages', label: '12 pages' },
  { id: '20 pages', label: '20 pages' },
  { id: '15 slide', label: '15-slide deck' },
  { id: '40 pages', label: 'A whitepaper (40+)' },
];

export const DEFAULT_SETUP: DocSetup = {
  format: 'auto',
  page_size: 'a4',
  orientation: 'portrait',
  style: 'report',
  columns: 1,
  margins: 'moderate',
  citation_style: 'apa',
  slide_size: '16:9',
  toc: true,
  cover: true,
  running_header: true,
  length: '',
};