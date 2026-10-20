import type { ReactNode } from 'react';

/* A very small YAML highlighter for the read-only config viewer.
 *
 * Deliberately NOT shiki. That is a full grammar engine and an async highlight
 * pass, and this view shows a read-only copy of one file: the whole cost would
 * be latency and a dependency to colour four kinds of token.
 *
 * It returns React NODES, never an HTML string. `dangerouslySetInnerHTML` would
 * put the config file's own text into the DOM unescaped - the file contains
 * prompts and prose, so a `<script>` or a stray `&` in it would be parsed as
 * markup. Nodes cannot do that: the text goes through as text.
 *
 * STATEFUL, and it has to be. `agent_config.yaml` embeds whole prompts under
 * `prompt: |`, and inside a block scalar a line like "Note: this is a
 * sentence" is literal text even though it is shaped exactly like a mapping.
 * Colouring it as a key means the viewer invents structure the file does not
 * have. A per-line function cannot tell the two apart, so this tracks whether
 * it is inside a block and what indent that block started at.
 */

type Span = { text: string; cls: string };

const C = {
  comment: 'text-zinc-600 italic',
  key: 'text-sky-300',
  str: 'text-emerald-300',
  num: 'text-amber-300',
  bool: 'text-violet-300',
  nullv: 'text-zinc-500',
  dash: 'text-zinc-500',
  plain: '',
};

export interface YamlHighlighter {
  /** Spans for one line, advancing the block-scalar state. */
  line: (src: string) => Span[];
  /** Forget any block-scalar state (new document, or a config reload). */
  reset: () => void;
}

export function yamlHighlighter(): YamlHighlighter {
  // Indent of the line that opened a block scalar, or null when not in one.
  // -1 is a sentinel for "in a block whose body may be unindented" which YAML
  // does not allow, so null is enough.
  let blockIndent: number | null = null;

  return {
    reset() { blockIndent = null; },
    line(src: string): Span[] {
      if (!src) return [];
      const indent = src.match(/^\s*/)?.[0].length ?? 0;

      // Inside a block scalar: everything more-indented than the opener is
      // literal. A line at or below the opener's indent ends the block, and is
      // then tokenised normally.
      if (blockIndent !== null) {
        if (src.trim() === '' || indent > blockIndent) {
          return [{ text: src, cls: C.plain }];
        }
        blockIndent = null;
      }

      // A block-scalar OPENER: `key: |` / `key: >` with an optional chomping
      // indicator. Its body is literal, so the next lines must not be
      // tokenised as mappings.
      const opener = src.match(/^(\s*)([^:#]+?):\s*([|>][-+]?)\s*(#.*)?$/);
      if (opener) {
        blockIndent = indent;
        const spans: Span[] = [];
        if (opener[1]) spans.push({ text: opener[1], cls: '' });
        spans.push({ text: opener[2], cls: C.key }, { text: ': ', cls: '' },
          { text: opener[3], cls: C.str });
        if (opener[4]) spans.push({ text: opener[4], cls: C.comment });
        return spans;
      }

      return tokenise(src);
    },
  };
}

/** Tokenise one line that is known to be structural YAML. */
function tokenise(line: string): Span[] {
  const out: Span[] = [];
  const indent = line.match(/^\s*/)?.[0] ?? '';
  if (indent) out.push({ text: indent, cls: '' });

  const rest = line.slice(indent.length);
  if (!rest) return out;

  if (rest.startsWith('#')) {
    out.push({ text: rest, cls: C.comment });
    return out;
  }

  // List item: `- value` or `- key: value`.
  const item = rest.match(/^(\s*)(-\s+)(.*)$/);
  let body = rest;
  if (item) {
    out.push({ text: item[1] + item[2], cls: C.dash });
    body = item[3];
  }

  // A trailing comment. The scan is quote-aware, so a '#' inside a string is
  // part of the value - the single easiest thing to get wrong here.
  const hashAt = findCommentHash(body);
  const valuePart = hashAt === -1 ? body : body.slice(0, hashAt);
  const commentPart = hashAt === -1 ? '' : body.slice(hashAt);

  const kv = valuePart.match(/^([A-Za-z0-9_.-]+)(\s*):([ \t]+|$)(.*)$/);
  if (kv) {
    out.push({ text: kv[1], cls: C.key }, { text: kv[2] + ':' + kv[3], cls: '' });
    pushValue(out, kv[4]);
  } else {
    pushValue(out, valuePart);
  }

  if (commentPart) out.push({ text: commentPart, cls: C.comment });
  return out;
}

/** Index of a `#` that starts a comment, ignoring ones inside quotes. */
function findCommentHash(s: string): number {
  let q: string | null = null;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (q) {
      if (ch === q) q = null;
      continue;
    }
    if (ch === '"' || ch === "'") { q = ch; continue; }
    if (ch === '#' && (i === 0 || /\s/.test(s[i - 1]))) return i;
  }
  return -1;
}

/** Colour a bare value: quoted string, number, boolean/null, or prose. */
function pushValue(out: Span[], v: string): void {
  if (!v) return;
  const quoted = v.match(/^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*')(.*)$/);
  if (quoted) {
    out.push({ text: quoted[1], cls: C.str });
    if (quoted[2]) out.push({ text: quoted[2], cls: C.comment });
    return;
  }
  const num = v.match(/^(-?\d[\d_]*\.?\d*)(.*)$/);
  if (num) {
    out.push({ text: num[1], cls: C.num });
    if (num[2]) out.push({ text: num[2], cls: C.comment });
    return;
  }
  const kw = v.match(/^(true|false|yes|no|on|off)(.*)$/i);
  if (kw) {
    out.push({ text: kw[1], cls: C.bool });
    if (kw[2]) out.push({ text: kw[2], cls: C.comment });
    return;
  }
  if (/^(null|None)\b/.test(v)) {
    out.push({ text: v, cls: C.nullv });
    return;
  }
  out.push({ text: v, cls: C.plain });
}

/** Render one line from an existing highlighter's spans. */
export function yamlLine(spans: Span[], dim = false): ReactNode {
  return (
    <span className={dim ? 'opacity-25' : undefined}>
      {spans.map((s, i) => (
        <span key={i} className={s.cls || undefined}>{s.text}</span>
      ))}
    </span>
  );
}
