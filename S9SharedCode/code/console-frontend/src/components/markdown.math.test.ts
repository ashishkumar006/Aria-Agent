import { describe, expect, it } from 'vitest';
import { renderMarkdown } from './markdown';

/** End to end through the function the chat panes actually call, not just the
 *  helper. The reported answer went through here and printed the source code
 *  of the maths to the user. */
const ANSWER = [
  'The document defines Scaled Dot-Product Attention with the formula:',
  '',
  '$$\\text{Attention}(Q, K, V) = \\softmax\\left(\\frac{QK^T}{\\sqrt{d_k}}\\right)V$$',
  '',
  'where `$d_k$` is the key dimension, and it costs $5 to run and $7 to store.',
].join('\n');

describe('renderMarkdown with LaTeX', () => {
  const html = renderMarkdown(ANSWER);

  it('emits no raw control words', () => {
    for (const junk of ['\\text', '\\frac', '\\sqrt', '\\left', '\\softmax', '$$']) {
      expect(html).not.toContain(junk);
    }
  });

  it('renders the formula as readable maths', () => {
    expect(html).toContain('md-math-block');
    expect(html).toContain('softmax');
    expect(html).toContain('<sup>T</sup>');
    expect(html).toContain('√(d');
  });

  it('does not turn money into maths', () => {
    // `$5 ... $7` closed a span in the first version and printed "5 to run
    // and" in monospace. A price pair has no command and no script.
    expect(html).toContain('$5 to run and $7 to store');
  });

  it('still renders the markdown that was already working', () => {
    const h = renderMarkdown('## Section\n\nSome **bold** and `code` here.');
    // fitTopLevel anchors the shallowest heading the model wrote to h2, so a
    // report made entirely of `##` sections still has a top level.
    expect(h).toContain('<h2>Section</h2>');
    expect(h).toContain('<b>bold</b>');
    expect(h).toContain('<code>code</code>');
  });

  it('escapes what the model wrote as HTML', () => {
    const h = renderMarkdown('a <script>alert(1)</script> b');
    expect(h).not.toContain('<script>');
    expect(h).toContain('&lt;script&gt;');
  });
});
