/* Language analysis for the Code editor.

   Highlighting, code folding and the symbol outline are three views of one
   token stream, so they share this module rather than each re-scanning the
   text with its own ad-hoc regexes.

   Hand-written on purpose: a real highlighter (Prism, Shiki, tree-sitter WASM)
   would mean shipping a parser plus grammar bundles to the browser. The
   languages in this repo are few and conventional, so a compact scanner is
   both smaller and easier to keep honest.

   This is a *highlighter*, not a parser. It is allowed to be wrong about
   syntax in edge cases, and it never decides anything that matters for
   correctness - diagnostics come from the server's real parser
   (`POST /api/code/check`), not from here.
*/

export type TokKind =
  | 'plain' | 'keyword' | 'builtin' | 'type' | 'function' | 'property'
  | 'string' | 'comment' | 'number' | 'operator' | 'punctuation'
  | 'tag' | 'attr' | 'heading' | 'meta' | 'link' | 'invalid';

export interface Tok { kind: TokKind; text: string }

const WORDS = (s: string) => new Set(s.split(/\s+/).filter(Boolean));

const PY_KW = WORDS(`False None True and as assert async await break class continue def del elif else
  except finally for from global if import in is lambda nonlocal not or pass raise return try while with
  yield match case type`);
const PY_BUILTIN = WORDS(`abs all any bool bytes callable chr dict dir enumerate eval filter float
  format frozenset getattr hasattr hash id input int isinstance issubclass iter len list map max min next
  object open ord pow print range repr reversed round set setattr slice sorted str sum super tuple type
  zip self cls Exception ValueError TypeError KeyError IndexError RuntimeError NotImplementedError`);

const TS_KW = WORDS(`abstract as async await break case catch class const continue debugger declare
  default delete do else enum export extends finally for from function get if implements import in
  instanceof interface is keyof let namespace new of private protected public readonly return set static
  super switch this throw try type typeof var void while with yield`);
const TS_TYPE = WORDS(`any bigint boolean never number object string symbol unknown unknown Array
  Promise Record Partial Readonly Pick Omit Map Set Date RegExp Error JSON Math Object`);
const TS_LIT = WORDS(`true false null undefined NaN Infinity`);

const SQL_KW = WORDS(`select from where insert into values update set delete create table alter drop
  index view join inner left right outer on group by order having limit offset union all distinct as
  and or not null primary key foreign references default unique check constraint begin commit
  rollback transaction returning with case when then else end exists in between like ilike`);

const SH_KW = WORDS(`if then else elif fi for while do done case esac function return in export local
  readonly declare source alias unset shift trap exit set echo source`);

/* Rule = [kind, source]. Tried in order at each position; the first match
   wins, so put multi-line and ambiguous constructs before simpler ones. */
type Rule = [TokKind, string];

const RX = {
  pyTriple: /'''[\s\S]*?'''|"""[\s\S]*?"""/,
  cBlock: /\/\*[\s\S]*?\*\//,
  htmlComment: /<!--[\s\S]*?-->/,
  mdCode: /```[\s\S]*?```|`[^`\n]+`/,
  dq: /"(?:\\.|[^"\\\n])*"?/,
  sq: /'(?:\\.|[^'\\\n])*'?/,
  bt: /`(?:\\.|[^`\\])*`?/,
  num: /(?:0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)[a-zA-Z_]*/,
  ident: /[A-Za-z_$][\w$]*/,
  ws: /[ \t]+/,
  newline: /\n/,
  op: /[+\-*/%=<>!&|^~?:]+/,
  punct: /[{}()[\];,.]/,
  dunder: /__\w+__/,
  decorator: /@[\w.]+/,
};

function rules(lang: string): Rule[] {
  switch (lang) {
    case 'python':
      return [
        ['comment', RX.cBlock.source],
        /* `#` line comments. These were MISSING: the rule list had no entry
           for them, so every Python comment fell through to `plain` and
           rendered in body colour. It went unnoticed because docstrings were
           wrongly classified as comments, so the comment colour appeared in
           the file anyway and a token-class assertion passed for the wrong
           reason. Fixed docstrings made the absence visible. */
        ['comment', /#[^\n]*/.source],
        /* A triple-quoted run is a string, not a comment - it evaluates. */
        ['string', RX.pyTriple.source],
        ['string', '[frbu]?' + RX.dq.source], ['string', '[frbu]?' + RX.sq.source],
        ['meta', RX.decorator.source],
        ['number', RX.num.source], ['builtin', RX.dunder.source],        ['operator', RX.op.source], ['punctuation', RX.punct.source],
      ];
    case 'typescript':
    case 'typescriptreact':
    case 'javascript':
      return [
        ['comment', RX.cBlock.source], ['comment', /\/\/[^\n]*/.source],
        ['string', RX.bt.source], ['string', RX.dq.source], ['string', RX.sq.source],
        ['number', RX.num.source], ['meta', RX.dunder.source],
        ['operator', RX.op.source], ['punctuation', RX.punct.source],
      ];
    case 'json':
      return [
        ['property', /"(?:\\.|[^"\\])*"(?=\s*:)/.source],
        ['string', RX.dq.source], ['number', RX.num.source],
        ['keyword', /\b(?:true|false|null)\b/.source],
        ['punctuation', RX.punct.source],
      ];
    case 'yaml':
      return [
        ['comment', /#[^\n]*/.source],
        ['property', /[A-Za-z_][\w.-]*(?=\s*:)/.source],
        ['meta', /[&*][\w-]+/.source],
        ['string', RX.dq.source], ['string', RX.sq.source],
        ['number', RX.num.source],
        ['keyword', /\b(?:true|false|null|yes|no|on|off|~)\b/.source],
        ['punctuation', /[-?:,\[\]{}]/.source],
      ];
    case 'toml':
    case 'ini':
      return [
        ['comment', /[#;][^\n]*/.source],
        ['heading', /^\s*\[[^\]\n]*\]|^\s*\[[^\]\n]*\]\s*$/m.source],
        ['property', /[A-Za-z_][\w.-]*(?=\s*=)/.source],
        ['string', RX.dq.source], ['string', RX.sq.source],
        ['number', RX.num.source],
        ['keyword', /\b(?:true|false)\b/.source],
      ];
    case 'markdown':
      return [
        ['heading', /^#{1,6}[^\n]*/m.source],
        ['string', RX.mdCode.source], ['link', /!?\[[^\]\n]*\]\([^)\n]*\)/.source],
        ['meta', /^ {0,3}(?:[-*+]|\d+\.)\s/m.source],
        ['keyword', /\*\*[^*\n]+\*\*|__[^_\n]+__/.source],
        ['invalid', /^>[^\n]*/m.source],
      ];
    case 'css':
      return [
        ['comment', RX.cBlock.source], ['string', RX.dq.source], ['string', RX.sq.source],
        ['meta', '@[a-zA-Z-]+'], ['number', RX.num.source],
        ['property', /[a-zA-Z-]+(?=\s*:)/.source],
        ['type', /[.#][\w-]+|::?[\w-]+(?:\([^)]*\))?/.source],
        ['punctuation', RX.punct.source],
      ];
    case 'html':
      return [
        ['comment', RX.htmlComment.source],
        ['tag', /<\/?[a-zA-Z][\w-]*/.source],
        ['tag', /<\/?>/.source],
        ['attr', /[a-zA-Z-]+(?=\s*=)/.source],
        ['string', RX.dq.source], ['string', RX.sq.source],
        ['punctuation', />/.source],
      ];
    case 'shell':
      return [
        ['comment', /#[^\n]*/.source], ['string', RX.dq.source], ['string', RX.sq.source],
        ['meta', /\$\{[^}\n]*\}|\$\w+/.source], ['number', RX.num.source],
        ['punctuation', /[|&;()<>$]/.source],
      ];
    case 'sql':
      return [
        ['comment', /--[^\n]*/.source], ['comment', RX.cBlock.source],
        ['string', RX.sq.source], ['string', RX.dq.source],
        ['number', RX.num.source],
        ['punctuation', RX.punct.source],
      ];
    default:
      return [
        ['comment', RX.cBlock.source], ['comment', /#[^\n]*/.source],
        ['string', RX.dq.source], ['string', RX.sq.source],
        ['number', RX.num.source],
      ];
  }
}

/* Compiled per language, once. A sticky regex per rule, tried in order. */
const compiled = new Map<string, RegExp[]>();

function compiledFor(lang: string): RegExp[] {
  let c = compiled.get(lang);
  if (!c) {
    c = rules(lang).map(([, src]) => new RegExp(src, 'y'));
    compiled.set(lang, c);
  }
  return c;
}

const isWordStart = (ch: string) => /[A-Za-z_$]/.test(ch);

/** Tokenize one language's text. Returns a flat token list covering every
    character, so the renderer can lay tokens out without gaps. */
export function tokenize(text: string, lang: string): Tok[] {
  const out: Tok[] = [];
  const rs = compiledFor(lang);
  const kw = lang === 'python' ? PY_KW
    : lang.startsWith('typescript') || lang === 'javascript' ? TS_KW
    : lang === 'shell' ? SH_KW
    : lang === 'sql' ? SQL_KW
    : undefined;

  let i = 0;
  const n = text.length;
  let plain = '';

  const flush = () => {
    if (plain) { out.push({ kind: 'plain', text: plain }); plain = ''; }
  };

  while (i < n) {
    const ch = text[i];

    if (ch === '\n') { flush(); out.push({ kind: 'plain', text: '\n' }); i++; continue; }
    if (ch === ' ' || ch === '\t' || ch === '\r') { plain += ch; i++; continue; }

    let matched = false;

    /* Keywords / builtins / literals are only special when they are whole
       words - `iffy` must not light up as `if`. */
    if (isWordStart(ch)) {
      const m = new RegExp(RX.ident.source, 'y').exec(text.slice(i));
      if (m) {
        const w = m[0];
        let kind: TokKind | null = null;
        if (kw?.has(w)) kind = 'keyword';
        else if (lang === 'python' && PY_BUILTIN.has(w)) kind = 'builtin';
        else if ((lang === 'typescript' || lang === 'typescriptreact') && TS_TYPE.has(w)) kind = 'type';
        else if (TS_LIT.has(w) && (lang === 'typescript' || lang === 'typescriptreact' || lang === 'javascript')) kind = 'keyword';
        else if (lang === 'yaml' && /^(true|false|null|yes|no|on|off|~)$/.test(w)) kind = 'keyword';
        if (kind) {
          flush();
          out.push({ kind, text: w });
          i += w.length;
          matched = true;
        } else if (text[i + w.length] === '(') {
          flush();
          out.push({ kind: 'function', text: w });
          i += w.length;
          matched = true;
        }
      }
    }

    if (!matched) {
      const list = rules(lang);
      for (let k = 0; k < rs.length; k++) {
        const re = rs[k];
        re.lastIndex = i;
        const m = re.exec(text);
        if (m && m[0].length > 0) {
          flush();
          out.push({ kind: list[k][0], text: m[0] });
          i += m[0].length;
          matched = true;
          break;
        }
      }
    }

    if (!matched) { plain += ch; i++; }
  }
  flush();
  return out;
}

/* ── structure: folds and symbols, both from the text ──────────────────── */

export interface Fold {
  /** 0-based line where the region starts. */
  start: number;
  /** 0-based last line of the region (inclusive). */
  end: number;
  kind: 'indent' | 'brace';
}

/** Foldable regions: indentation for the indentation-scoped languages, brace
    depth for the brace-scoped ones. */
export function foldRanges(text: string, lang: string): Fold[] {
  const lines = text.split('\n');
  const folds: Fold[] = [];
  const indentScoped = lang === 'python' || lang === 'yaml' || lang === 'markdown';

  if (indentScoped) {
    if (lang === 'markdown') {
      lines.forEach((l, i) => { if (/^#{1,6}\s/.test(l)) folds.push({ start: i, end: i, kind: 'indent' }); });
      return folds;
    }
    const stack: { line: number; indent: number }[] = [];
    lines.forEach((raw, i) => {
      if (!raw.trim()) return;
      const indent = raw.match(/^\s*/)?.[0].replace(/\t/g, '    ').length ?? 0;
      while (stack.length && stack[stack.length - 1].indent >= indent) stack.pop();
      if (stack.length) {
        const parent = stack[stack.length - 1];
        if (i - parent.line >= 1) folds.push({ start: parent.line, end: i - 1, kind: 'indent' });
      }
      stack.push({ line: i, indent });
    });
    return folds;
  }

  /* Brace languages: emit a region for every opening line that closes later. */
  let depth = 0;
  const openAt: number[] = [];
  const closes: Record<number, number> = {};
  lines.forEach((l, i) => {
    for (const ch of l) {
      if (ch === '{' || ch === '[') {
        if (depth === 0) openAt.push(i);
        depth++;
      } else if (ch === '}' || ch === ']') {
        depth = Math.max(0, depth - 1);
        if (depth === 0) {
          const start = openAt.pop();
          if (start !== undefined && start < i) closes[start] = i;
        }
      }
    }
  });
  for (const [s, e] of Object.entries(closes)) {
    folds.push({ start: Number(s), end: e, kind: 'brace' });
  }
  return folds;
}

export interface Symbol {
  name: string;
  line: number;      /* 0-based */
  endLine: number;   /* 0-based, inclusive */
  kind: 'function' | 'class' | 'method' | 'section';
  detail?: string;
}

const DEF = [
  /^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)/,
  /^\s*(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)/,
  /^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)/,
  /^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(/,
  /^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)/,
  /^\s*(?:public|private|protected|static|\s)*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{/,
];

/** Outline of the file. Deliberately regex-based and deliberately shallow: it
    is a navigation aid, not a type checker. */
export function symbols(text: string, lang: string): Symbol[] {
  const lines = text.split('\n');
  const out: Symbol[] = [];

  if (lang === 'markdown') {
    lines.forEach((l, i) => {
      const m = l.match(/^(#{1,6})\s+(.*)$/);
      if (m) out.push({ name: m[2].trim(), line: i, endLine: i, kind: 'section', detail: `h${m[1].length}` });
    });
    return out;
  }

  const folds = foldRanges(text, lang);
  const endFor = (line: number) => {
    let best = line;
    for (const f of folds) if (f.start === line && f.end > best) best = f.end;
    return best;
  };

  lines.forEach((l, i) => {
    for (const [k, re] of DEF.entries()) {
      const m = l.match(re);
      if (!m || !m[1]) continue;
      /* The bare `name(...) {` pattern also matches calls; require that the
         line does not start with a call-shaped expression. */
      if (k === 5 && /^[\s]*[)\];,]/.test(l)) continue;
      const kind: Symbol['kind'] =
        k === 0 ? (/^\s*def\s/.test(l) ? 'function' : 'method')
        : k === 2 ? 'class'
        : 'function';
      out.push({
        name: m[1],
        line: i,
        endLine: endFor(i),
        kind,
        detail: m[2] ? m[2].trim().slice(0, 60) : undefined,
      });
      break;
    }
  });
  return out;
}

/** Matching bracket for a caret offset, or null. Used for bracket highlight. */
export function matchBracket(text: string, offset: number): number | null {
  const open = '([{';
  const close = ')]}';
  const at = text[offset];
  const before = text[offset - 1];
  let want: string | null = null;
  let i = -1;
  if (open.includes(at)) { want = close[open.indexOf(at)]; i = offset; }
  else if (close.includes(before)) { want = open[close.indexOf(before)]; i = offset - 1; }
  if (want === null || i < 0) return null;

  let depth = 0;
  const forward = open.includes(text[i]);
  for (let k = i; forward ? k < text.length : k >= 0; forward ? k++ : k--) {
    const c = text[k];
    if (c === text[i]) depth++;
    else if (c === want) { depth--; if (depth === 0) return k; }
  }
  return null;
}

/** Leading-whitespace width of a line, tabs expanded. Drives indent guides. */
export function indentOf(line: string): number {
  const m = line.match(/^[ \t]*/)?.[0] ?? '';
  return m.replace(/\t/g, '    ').length;
}

export const INDENT = 4;
