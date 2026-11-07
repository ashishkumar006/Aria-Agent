import { describe, expect, it } from 'vitest';
import { latexToText, renderMath } from './math';

/** The answer the user actually got, verbatim from the Discuss pane. */
const REAL = '$$\\text{Attention}(Q, K, V) = \\softmax\\left(\\frac{QK^T}{\\sqrt{d_k}}\\right)V$$';

describe('latexToText', () => {
  it('turns the reported formula into something readable', () => {
    const out = latexToText('\\text{Attention}(Q, K, V) = \\softmax\\left(\\frac{QK^T}{\\sqrt{d_k}}\\right)V');
    expect(out).toBe('Attention(Q, K, V) = softmax((QK<sup>T</sup>)/(√(d<sub>k</sub>)))V');
    // The whole complaint, in one assertion: no control words may survive.
    for (const junk of ['\\text', '\\frac', '\\sqrt', '\\left', '\\right', '\\softmax', '$$']) {
      expect(out).not.toContain(junk);
    }
  });

  it('maps symbols and greek', () => {
    expect(latexToText('\\alpha \\leq \\beta \\neq \\gamma \\times \\pi')).toBe('α ≤ β ≠ γ × π');
    expect(latexToText('\\sum_{i=1}^{n} x_i \\to \\infty')).toBe('∑<sub>i=1</sub><sup>n</sup> x<sub>i</sub> → ∞');
  });

  it('nests fractions without running away', () => {
    expect(latexToText('\\frac{\\frac{a}{b}}{c}')).toBe('((a)/(b))/(c)');
  });

  it('leaves an unbalanced brace alone rather than eating the string', () => {
    const src = '\\text{Attention(Q, K, V) = 1';
    const out = latexToText(src);
    expect(out.length).toBeGreaterThan(0);
    expect(out).not.toContain('\\text');
  });
});

describe('renderMath', () => {
  it('rewrites a display block and keeps the surrounding prose', () => {
    const html = renderMath(`The engine is ${REAL} where the keys scale.`);
    expect(html).toContain('class="md-math md-math-block"');
    expect(html).not.toContain('$$');
    expect(html).toContain('The engine is');
    expect(html).toContain('where the keys scale.');
  });

  it('rewrites inline spans', () => {
    expect(renderMath('divide by $\\sqrt{d_k}$ first')).toBe('divide by <span class="md-math">√(d<sub>k</sub>)</span> first');
  });

  it('consumes the underscore that the italic rule would otherwise claim', () => {
    // Before this, `$d_k$` reached the italic rule and rendered as d<em>k</em>.
    const html = renderMath('$d_k$');
    expect(html).not.toContain('<em>');
    expect(html).toContain('<sub>k</sub>');
  });

  it('leaves text with no maths completely alone', () => {
    const plain = 'A normal sentence with **bold** and `code` and 3 < 4.';
    expect(renderMath(plain)).toBe(plain);
  });

  it('does not treat a single stray dollar as a span', () => {
    // A lone `$` must not swallow the rest of the paragraph looking for a close.
    expect(renderMath('cost is $5 and $7 later')).toContain('$5');
  });
});
