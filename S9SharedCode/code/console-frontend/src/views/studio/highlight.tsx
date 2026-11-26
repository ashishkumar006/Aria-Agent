import type { ReactNode } from 'react';

/* Colour for the code the agent writes into its own transcript.
 *
 * The transcript is where a coding agent shows its work, and until now every
 * fence in it was one flat grey — a function, a string and a comment all read
 * the same, which is exactly the distinction a reviewer is scanning for.
 *
 * This is a deliberately small tokenizer, not a parser: one regex per language,
 * each alternative a named group, and the text between matches passed through
 * untouched. It cannot be wrong in a way that matters — a token it does not
 * recognise keeps the colour it had — and it costs nothing to bundle.
 * `monaco-editor` is in the app for the editor, but pulling it into the chat
 * transcript to colour a five-line snippet would weigh more than the feature.
 */

export type Lang = 'python' | 'javascript' | 'json' | 'bash' | 'text';

/* Order matters: the first group that matched decides the class. */
const CLASS_ORDER = ['com', 'key', 'str', 'dec', 'kw', 'num', 'fn'] as const;

/* A quoted run: single, double or (for JS) a template literal. Python also has
 * the r/b/f prefixes and triple quotes, which is why it gets its own pattern. */
const PATTERNS: Record<Exclude<Lang, 'text'>, RegExp> = {
  python: /(?<com>#[^\n]*)|(?<str>[rbfuRBFU]{0,2}(?:'''[\s\S]*?'''|"""[\s\S]*?"""|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"))|(?<dec>@[\w.]+)|(?<kw>\b(?:def|class|return|if|elif|else|for|while|in|not|and|or|import|from|as|with|try|except|finally|raise|yield|lambda|global|nonlocal|assert|pass|break|continue|del|is|async|await|None|True|False)\b)|(?<num>\b(?:0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?)\b)|(?<fn>\b[A-Za-z_]\w*(?=\())/g,

  javascript: /(?<com>\/\/[^\n]*|\/\*[\s\S]*?\*\/)|(?<str>'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`)|(?<kw>\b(?:const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|class|extends|super|this|typeof|instanceof|in|of|try|catch|finally|throw|async|await|yield|import|from|export|default|null|undefined|true|false)\b)|(?<num>\b(?:0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?)\b)|(?<fn>\b[A-Za-z_]\w*(?=\())/g,

  json: /(?<key>"(?:\\.|[^"\\])*"(?=\s*:))|(?<str>"(?:\\.|[^"\\])*")|(?<kw>\b(?:true|false|null)\b)|(?<num>\b(?:0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?)\b)/g,

  bash: /(?<com>#[^\n]*)|(?<str>'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*")|(?<kw>\b(?:if|then|else|elif|fi|for|while|do|done|case|esac|function|return|export|local|echo|exit|set|unset|source|alias)\b)|(?<num>\b(?:0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?)\b)/g,
};

/** Map a fence label to something worth tokenising. */
export function langOf(label?: string): Lang {
  const l = (label || '').toLowerCase().trim();
  if (/^(py|python|py3|python3)$/.test(l)) return 'python';
  if (/^(js|jsx|javascript|node|mjs|cjs|ts|tsx|typescript)$/.test(l)) return 'javascript';
  if (/^(json|jsonc|json5)$/.test(l)) return 'json';
  if (/^(sh|bash|zsh|shell|console|powershell|ps1)$/.test(l)) return 'bash';
  return 'text';
}

/** A guess from the snippet itself, for the fence the model forgot to label.
 *
 *  Live measurement: 117 of 117 fences in one studio session carried no
 *  language tag, so a label-only rule coloured nothing at all. The signals are
 *  cheap and one-directional — when in doubt the answer is `text`, which is what
 *  the block looked like before. */
export function guessLang(code: string): Lang {
  const s = (code || '').trimStart();
  if (!s) return 'text';
  if (/^#!.*\bpython/.test(s)) return 'python';
  if (/^#!.*\b(sh|bash|zsh)\b/.test(s)) return 'bash';
  if (/^(diff --git|--- a\/|\+\+\+ b\/|@@ )/.test(s)) return 'text';
  if (/^(def |class |from \w+ import |import \w)/m.test(s) && /\b(def|class)\s+\w+\s*\(/.test(s)) return 'python';
  if (/\b(assert|elif|None|True|False)\b/.test(s) && !/\b(const|let|var|function)\b/.test(s)) return 'python';
  if (/\b(const|let|var|function|=>|require\(|module\.exports)\b/.test(s)) return 'javascript';
  if (/^\s*[\[{]/.test(s) && /"\s*:\s*"/.test(s)) return 'json';
  if (/^\s*(\$ |npm |pip |sudo |git |uv |python -m |pytest|cd )/m.test(s)) return 'bash';
  return 'text';
}

/** The fence label when it says something, else the content's best guess. */
export function langFor(label: string | undefined, code: string): Lang {
  const named = langOf(label);
  return named === 'text' ? guessLang(code) : named;
}

function classOf(groups: Record<string, string | undefined> | undefined): string {
  if (!groups) return '';
  for (const k of CLASS_ORDER) if (groups[k] !== undefined) return k;
  return '';
}

/** Colourise `code`. Returns strings and spans, safe to drop into a `<code>`
 *  element; text is never turned into HTML, so nothing here can inject markup. */
export function highlight(code: string, lang: Lang = 'text'): ReactNode[] {
  if (lang === 'text' || !code) return [code];
  const re = PATTERNS[lang];
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  re.lastIndex = 0;
  while ((m = re.exec(code))) {
    if (m.index > last) out.push(code.slice(last, m.index));
    const cls = classOf(m.groups);
    out.push(<span key={`t${i++}`} className={cls ? `tok-${cls}` : undefined}>
      {m[0]}
    </span>);
    last = m.index + m[0].length;
    if (m[0].length === 0) re.lastIndex++;   // never stall on an empty match
  }
  if (last < code.length) out.push(code.slice(last));
  return out;
}

/** The same, as strings with markers — what the tests assert against, since
 *  React nodes are awkward to compare and the boundaries are the contract. */
export function highlightTokens(code: string, lang: Lang = 'text'): string[] {
  const out: string[] = [];
  for (const node of highlight(code, lang)) {
    if (typeof node === 'string') { out.push(node); continue; }
    const el = node as { props?: { className?: string; children?: unknown } };
    const cls = el.props?.className || '';
    out.push(`<${cls}>${String(el.props?.children ?? '')}</${cls}>`);
  }
  return out;
}