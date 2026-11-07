/** LaTeX → readable text, for a console that has no maths renderer.
 *
 *  The models were never told this. Asked to quote a formula out of a paper,
 *  a model writes what the paper wrote, and the reader got the source code of
 *  the maths instead of the maths:
 *
 *    $$\text{Attention}(Q, K, V) = \softmax\left(\frac{QK^T}{\sqrt{d_k}}\right)V$$
 *
 *  The prompt now forbids it, but a prompt is a request and this file is the
 *  guarantee: whatever still arrives becomes something a person can read,
 *  with no dependency on KaTeX and no second font to load.
 *
 *  This is deliberately a degrader, not a renderer. It does not lay out
 *  fractions or align matrices; it turns control words into the characters a
 *  human would type. `√(d_k)` is not beautiful, and it beats `\sqrt{d_k}`.
 */

/** Command name → the character a reader would type.
 *
 *  Looked up in ONE pass over `\\([A-Za-z]+)`, not with one regex per command.
 *  The per-command version used `\b` to stop `\le` matching inside `\left`,
 *  and `\b` does not hold before `_` — which is a word character — so
 *  `\sum_{i=1}^{n}` matched nothing and the ∑ simply vanished from the
 *  answer, leaving `_{i=1}^{n}` behind. A name table cannot have that bug,
 *  because the name is captured greedily and then looked up whole.
 */
const TABLE: Record<string, string> = {
  // relations
  leqslant: '≤', leq: '≤', le: '≤',
  geqslant: '≥', geq: '≥', ge: '≥',
  neq: '≠', ne: '≠',
  approx: '≈', equiv: '≡', propto: '∝', sim: '∼', cong: '≅',
  // sets and logic
  in: '∈', notin: '∉', ni: '∋', subset: '⊂', subseteq: '⊆',
  supset: '⊃', supseteq: '⊇', cup: '∪', cap: '∩',
  emptyset: '∅', varnothing: '∅',
  forall: '∀', exists: '∃', neg: '¬', land: '∧', lor: '∨',
  // calculus and operators
  nabla: '∇', partial: '∂', sum: '∑', prod: '∏', int: '∫',
  oint: '∮', infty: '∞', cdots: '…', ldots: '…', dots: '…',
  times: '×', div: '÷', pm: '±', mp: '∓', circ: '∘', bullet: '•',
  ast: '∗', star: '⋆', odot: '⊙', oplus: '⊕', otimes: '⊗',
  // arrows
  rightarrow: '→', to: '→',longrightarrow: '⟶',
  leftarrow: '←', gets: '←',
  Rightarrow: '⇒', Leftarrow: '⇐', leftrightarrow: '↔',
  Leftrightarrow: '⇔', uparrow: '↑', downarrow: '↓',
  mapsto: '↦',
  // delimiters
  langle: '⟨', rangle: '⟩', lceil: '⌈', rceil: '⌉',
  lfloor: '⌊', rfloor: '⌋', prime: '′', degree: '°',
  // greek, lower
  alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε',
  varepsilon: 'ε', zeta: 'ζ', eta: 'η', theta: 'θ', vartheta: 'ϑ',
  iota: 'ι', kappa: 'κ', lambda: 'λ', mu: 'μ', nu: 'ν',
  xi: 'ξ', pi: 'π', rho: 'ρ', sigma: 'σ', tau: 'τ',
  upsilon: 'υ', phi: 'φ', varphi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω',
  // greek, upper
  Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ',
  Pi: 'Π', Sigma: 'Σ', Upsilon: 'Υ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
};

/** Words that are maths only because of the backslash. */
const WORDS = new Set([
  'softmax', 'argmax', 'argmin', 'log', 'ln', 'exp', 'max', 'min', 'lim',
  'det', 'dim', 'sg', 'sigmoid', 'relu', 'tanh', 'sinh', 'cosh', 'mod',
  'sin', 'cos', 'tan', 'if', 'case', 'align', 'mid', 'colon', 'gcd',
]);

/** Strip the braces of a wrapper command, keeping its content. */
function unwrap(src: string, cmd: string): string {
  const open = `\\${cmd}{`;
  let guard = 0;
  while (src.includes(open) && guard++ < 200) {
    const i = src.indexOf(open);
    let depth = 0;
    let j = i + open.length - 1;
    for (; j < src.length; j += 1) {
      if (src[j] === '{') depth += 1;
      else if (src[j] === '}') { depth -= 1; if (depth === 0) break; }
    }
    if (j >= src.length) break;          // unbalanced: stop rather than eat the string
    src = src.slice(0, i) + src.slice(i + open.length, j) + src.slice(j + 1);
  }
  return src;
}

/** Read one balanced `{...}` group starting at `i`. Returns the inner text
 *  and the index just past the closing brace, or null if it is not a group. */
function readGroup(src: string, i: number): { body: string; end: number } | null {
  while (src[i] === ' ') i += 1;
  if (src[i] !== '{') {
    // TeX also allows a single-token operand: \frac12, ^2, _i.
    if (i < src.length && src[i] !== '\\') return { body: src[i], end: i + 1 };
    return null;
  }
  let depth = 0;
  for (let j = i; j < src.length; j += 1) {
    if (src[j] === '{') depth += 1;
    else if (src[j] === '}') {
      depth -= 1;
      if (depth === 0) return { body: src.slice(i + 1, j), end: j + 1 };
    }
  }
  return null;
}

/** `\frac{A}{B}` → `(A)/(B)`, with balanced operands.
 *
 *  The first version matched operands as `[^{}]*`, which is fine until the
 *  common case: `\frac{QK^T}{\sqrt{d_k}}` has braces inside its second
 *  operand, so the pattern never fired, the fallback replaced `\frac` with a
 *  bare `/`, and the reader saw `/{QK^T}{√(d_k)}` — braces and all.
 */
function fractions(src: string): string {
  let out = src;
  let guard = 0;
  for (;;) {
    const i = out.indexOf('\\frac');
    if (i === -1 || guard++ > 200) break;
    let at = i + 5;
    const a = readGroup(out, at);
    if (!a) { out = out.slice(0, i) + '/' + out.slice(i + 5); continue; }
    const b = readGroup(out, a.end);
    if (!b) { out = out.slice(0, i) + '/' + out.slice(i + 5); continue; }
    out = out.slice(0, i) + `(${a.body})/(${b.body})` + out.slice(b.end);
  }
  return out;
}

function scripts(src: string): string {
  return src
    .replace(/\^\s*\{([^{}]+)\}/g, '<sup>$1</sup>')
    .replace(/\^\s*([A-Za-z0-9+\-*='])/g, '<sup>$1</sup>')
    .replace(/_\s*\{([^{}]+)\}/g, '<sub>$1</sub>')
    .replace(/_\s*([A-Za-z0-9+\-*='])/g, '<sub>$1</sub>');
}

/** Convert one LaTeX body (delimiters already removed). */
export function latexToText(body: string): string {
  let s = body;
  for (const cmd of ['text', 'textrm', 'mathrm', 'mathbf', 'mathit', 'boldsymbol',
                     'mathcal', 'mathbb', 'operatorname', 'mbox', 'hat', 'bar',
                     'vec', 'tilde', 'overline']) {
    s = unwrap(s, cmd);
  }
  s = s.replace(/\\(?:left|right|bigl|bigr|Bigl|Bigr|biggl|biggr|Biggl|Biggr|big|Big|bigg|Bigg)(?![A-Za-z])/g, '');
  s = fractions(s);
  s = s.replace(/\\sqrt\s*\{([^{}]*)\}/g, '√($1)');
  s = s.replace(/\\sqrt\s*([A-Za-z0-9])/g, '√$1');
  // Spacing commands.
  s = s.replace(/\\[,;:!> ]/g, ' ');
  // One pass over every remaining command name.
  s = s.replace(/\\([A-Za-z]+)/g, (_m, name: string) =>
    TABLE[name] ?? (WORDS.has(name.toLowerCase()) ? name : ''));
  s = scripts(s);
  return s.replace(/[ \t]{2,}/g, ' ').trim();
}

/** Does this span look like maths rather than money?
 *
 *  `$5 and $7` is a price pair, and the first version of this file turned it
 *  into a maths span and printed `5 and` in monospace. Real LaTeX carries a
 *  command, a superscript, a subscript or a brace; prose does not.
 */
function looksLikeMath(body: string): boolean {
  return /\\[A-Za-z]/.test(body) || /[\^_{}]/.test(body);
}

/** Replace every math span in already-escaped text with readable HTML.
 *
 *  Runs before the other inline rules so `_` inside `d_k` is consumed as a
 *  subscript instead of being read as the start of an italic span — which is
 *  how `$d_k$` used to come out as `d<em>k</em>` around a stray dollar. */
export function renderMath(src: string): string {
  let out = src.replace(/\$\$([\s\S]+?)\$\$/g,
    (_m, body: string) => `<span class="md-math md-math-block">${latexToText(body)}</span>`);
  out = out.replace(/\\\(([\s\S]+?)\\\)/g,
    (_m, body: string) => `<span class="md-math">${latexToText(body)}</span>`);
  out = out.replace(/(^|[^\\])\$([^$\n]+?)\$/g, (_m, lead: string, body: string) =>
    looksLikeMath(body)
      ? `${lead}<span class="md-math">${latexToText(body)}</span>`
      : _m);
  return out;
}
