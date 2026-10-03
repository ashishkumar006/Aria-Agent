/* The editing surface: a transparent <textarea> over a highlighted <pre>.

   A textarea cannot render styled runs, so highlighting has to live in a layer
   behind it. That makes metric parity load-bearing — if the two layers disagree
   by even one pixel of padding or line height, the caret appears to drift away
   from the text it is editing. Every measurement below is duplicated in
   METRICS so the two layers cannot drift apart by editing one and forgetting
   the other.
*/

import { memo, useEffect, useMemo, useRef } from 'react';
import {
  INDENT, foldRanges, matchBracket, tokenize, type Tok, type TokKind,
} from '../lib/lang';

/* Must match the textarea's classes exactly. */
export const METRICS = {
  fontSize: 12.5,
  lineHeight: 24.8,      /* leading-[1.55rem] */
  padY: 8,                /* p-2 */
  padX: 8,
  gutterWidth: 46,
};

const KIND_CLASS: Record<TokKind, string> = {
  plain: 'text-zinc-100',
  keyword: 'text-purple-300',
  builtin: 'text-sky-300',
  type: 'text-amber-200',
  function: 'text-emerald-300',
  property: 'text-sky-200',
  string: 'text-amber-300',
  comment: 'text-zinc-500 italic',
  number: 'text-orange-300',
  operator: 'text-pink-300',
  punctuation: 'text-zinc-400',
  tag: 'text-rose-300',
  attr: 'text-yellow-200',
  heading: 'text-emerald-200 font-semibold',
  meta: 'text-fuchsia-300',
  link: 'text-sky-300 underline',
  invalid: 'text-rose-300',
};

export interface EditorProps {
  body: string;
  lang: string;
  path: string;
  folded: Set<number>;
  cursorLine: number;
  caretOffset: number;
  selectionStart: number;
  selectionEnd: number;
  problems: { line: number }[];
  ariaLabel: string;
  onChange: (v: string) => void;
  onCursor: () => void;
  onFoldToggle: (line: number) => void;
  registerRef: (el: HTMLTextAreaElement | null) => void;
  scrollerRef: React.RefObject<HTMLDivElement | null>;
}

/** Colour the tokens, but only when the file is small enough that doing so on
    every keystroke is cheap. Past this size a 400-line file's worth of spans
    costs more than the highlight is worth, and the editor drops to plain text
    rather than dropping frames. */
const HIGHLIGHT_MAX_BYTES = 220_000;

function Highlighted({ body, lang, folded }: {
  body: string; lang: string; folded: Set<number>;
}) {
  const toks = useMemo<Tok[]>(() => {
    if (body.length > HIGHLIGHT_MAX_BYTES) return [{ kind: 'plain', text: body }];
    return tokenize(body, lang);
  }, [body, lang]);

  /* Group tokens into visual lines, dropping the interior of folded regions
     and replacing it with a single marker. */
  const { rows, foldMarkers } = useMemo(() => {
    const out: Tok[][] = [];
    const markers: Map<number, number> = new Map();
    let cur: Tok[] = [];
    let line = 0;
    let skipping = false;
    for (const t of toks) {
      if (t.kind === 'plain') {
        const parts = t.text.split('\n');
        parts.forEach((p, i) => {
          if (i > 0) {
            out.push(cur);
            cur = [];
            line++;
            if (skipping) {
              const closed = [...folded].some((f) => line > f);
              if (closed) skipping = false;
              else markers.set(line - 1, f_end(folded, line - 1));
            }
          }
          if (p) cur.push({ kind: t.kind, text: p });
        });
      } else {
        cur.push(t);
      }
      if (folded.has(line) && t.text.includes('\n')) skipping = true;
    }
    out.push(cur);
    return { rows: out, foldMarkers: markers };
  }, [toks, folded]);

  const visible: Tok[][] = [];
  rows.forEach((r, i) => { if (!foldMarkers.has(i)) visible.push(r); });

  return (
    <>
      {visible.map((row, i) => (
        <div key={i} className="whitespace-pre">
          {foldMarkers.has(i)
            ? <span className="text-zinc-600">⋯ {foldMarkers.get(i)} folded</span>
            : row.length === 0
              ? <span> </span>
              : row.map((t, k) => (
                <span key={k} className={KIND_CLASS[t.kind]}>{t.text}</span>
              ))}
        </div>
      ))}
    </>
  );
}

function f_end(folded: Set<number>, line: number): number {
  let best = 0;
  for (const f of folded) if (f > line) best = Math.max(best, f - line);
  return best || 1;
}

const Editor = memo(function Editor(props: EditorProps) {
  const {
    body, lang, folded, cursorLine, caretOffset, problems,
    ariaLabel, onChange, onCursor, onFoldToggle, registerRef, scrollerRef,
  } = props;

  const preRef = useRef<HTMLPreElement>(null);

  /* The overlay and the textarea scroll together. They are separate elements,
     so nothing keeps them in step but this handler. */
  useEffect(() => {
    const sc = scrollerRef.current;
    const pre = preRef.current;
    if (!sc || !pre) return;
    const sync = () => { pre.scrollLeft = sc.scrollLeft; pre.scrollTop = sc.scrollTop; };
    sc.addEventListener('scroll', sync, { passive: true });
    sync();
    return () => sc.removeEventListener('scroll', sync);
  }, [scrollerRef, body]);

  const foldStarts = useMemo(() => {
    const set = new Set<number>();
    for (const f of foldRanges(body, lang)) set.add(f.start);
    return set;
  }, [body, lang]);

  const problemLines = useMemo(() => new Set(problems.map((p) => p.line)), [problems]);

  /* The bracket adjacent to the caret, and its partner. `here` is the offset
     actually under the caret, which is the character BEFORE it when the caret
     sits just after an opening delimiter. */
  const bracketPair = useMemo(() => {
    if (caretOffset < 0) return null;
    const other = matchBracket(body, caretOffset);
    if (other === null) return null;
    const here = '([{'.includes(body[caretOffset]) ? caretOffset : caretOffset - 1;
    return { here, other };
  }, [body, caretOffset]);

  return (
    <div ref={scrollerRef} className="relative min-h-0 min-w-0 flex-1 overflow-auto">
      <div className="flex min-w-max">
        {/* The gutter is NOT aria-hidden: it contains the fold buttons, which
            are real controls. Hiding the whole strip made folding impossible
            to reach by keyboard or screen reader. Only the numbers are
            decorative, so only they are hidden. */}
        <div
          className="sticky left-0 z-20 flex-none select-none border-r border-glass-border bg-surface-0 px-2 py-2 text-right font-mono text-[11px] leading-[1.55rem] text-zinc-muted/60">
          {Array.from({ length: body.split('\n').length }).map((_, i) => (
            <div key={i} className="flex items-center justify-end gap-1">
              {problemLines.has(i) && (
                <span aria-hidden className="inline-block h-[7px] w-[7px] flex-none rounded-full bg-rose-400" />
              )}
              <span aria-hidden className={cursorLine === i ? 'text-zinc-100' : undefined}>{i + 1}</span>
              {foldStarts.has(i) && (
                <button
                  type="button"
                  aria-label={`Fold line ${i + 1}`}
                  onClick={() => onFoldToggle(i)}
                  className="ml-0.5 text-zinc-muted hover:text-zinc-200"
                >▾</button>
              )}
            </div>
          ))}
        </div>

        {/* The content box holds BOTH layers at `inset-0`, so their geometry is
            identical by construction rather than by keeping two sets of padding
            and margin in sync. Getting this wrong is what made every line look
            clipped by a character: the gutter was floated AND the textarea was
            given a margin, so the textarea ended up offset twice while the
            overlay was offset once. The width lives here too, so the two layers
            cannot disagree about where the text ends either. */}
        <div className="relative flex-none"
             style={{ width: maxCol(body) * METRICS.fontSize * 0.6 + METRICS.padX * 2 + 40 }}>
          {/* Height comes from a transparent sizer, so both absolutely
              positioned layers can fill the box without either of them
              scrolling internally. */}
          <div aria-hidden className="invisible py-2 pl-3 pr-4 font-mono text-[12.5px] leading-[1.55rem]">
            {body.split('\n').map((_, i) => <div key={i}>{i === 0 ? ' ' : ' '}</div>)}
          </div>

          {/* current-line band, behind everything */}
          <div aria-hidden
            className="pointer-events-none absolute inset-x-0 z-0 bg-white/[0.035]"
            style={{ top: METRICS.padY + cursorLine * METRICS.lineHeight, height: METRICS.lineHeight }} />

          {/* indent guides + matching-bracket markers */}
          <div aria-hidden className="pointer-events-none absolute inset-0 z-0">
            {Array.from({ length: 12 }).map((_, i) => (
              <div key={i} className="absolute inset-y-0 w-px bg-white/[0.03]"
                   style={{ left: METRICS.padX + i * INDENT * METRICS.fontSize * 0.6 }} />
            ))}
            {bracketPair && (
              <>
                <span className="absolute w-[2px] bg-accent/70"
                      style={{
                        left: METRICS.padX + bracketPair.here * METRICS.fontSize * 0.6,
                        top: METRICS.padY + lineOf(body, bracketPair.here) * METRICS.lineHeight,
                        height: METRICS.lineHeight,
                      }} />
                <span className="absolute w-[2px] bg-accent/70"
                      style={{
                        left: METRICS.padX + bracketPair.other * METRICS.fontSize * 0.6,
                        top: METRICS.padY + lineOf(body, bracketPair.other) * METRICS.lineHeight,
                        height: METRICS.lineHeight,
                      }} />
              </>
            )}
          </div>

          {/* highlighted layer */}
          <pre ref={preRef} aria-hidden
            className="pointer-events-none absolute inset-0 z-10 overflow-hidden whitespace-pre bg-transparent py-2 pl-3 pr-4 font-mono text-[12.5px] leading-[1.55rem]">
            <Highlighted body={body} lang={lang} folded={folded} />
          </pre>

          {/* the editable layer: transparent text, visible caret+selection.
              `absolute inset-0` matches the <pre> exactly, so the caret cannot
              drift away from the glyphs it is editing. */}
          <textarea
            ref={registerRef}
            value={body}
            onChange={(e) => onChange(e.target.value)}
            onSelect={onCursor}
            onKeyUp={onCursor}
            onClick={onCursor}
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            wrap="off"
            aria-label={ariaLabel}
            className="absolute inset-0 z-30 resize-none overflow-hidden whitespace-pre bg-transparent py-2 pl-3 pr-4 font-mono text-[12.5px] leading-[1.55rem] text-transparent caret-accent outline-none selection:bg-accent/25"
          />
        </div>
      </div>
    </div>
  );
});

function lineOf(text: string, off: number): number {
  let n = 0;
  for (let i = 0; i < off && i < text.length; i++) if (text[i] === '\n') n++;
  return n;
}

function maxCol(text: string): number {
  let best = 0;
  for (const l of text.split('\n')) if (l.length > best) best = l.length;
  return Math.max(80, Math.min(best, 400));
}

export default Editor;
