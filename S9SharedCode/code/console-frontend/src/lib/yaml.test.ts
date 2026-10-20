import { describe, it, expect } from 'vitest';
import { yamlHighlighter } from './yaml';

/* The config viewer highlights a read-only copy of a file that contains whole
 * prompts embedded under `prompt: |`. Two things matter and both are easy to
 * get wrong:
 *
 *  1. A '#' inside a quoted string is NOT a comment.
 *  2. Text inside a block scalar is LITERAL, even when shaped exactly like a
 *     mapping ("Note: this is a sentence"). Colouring it as a key would invent
 *     structure the file does not have - which is why the highlighter is
 *     stateful rather than a per-line function.
 *
 * Tested on spans, not rendered output: this project's vitest runs in the node
 * environment with no DOM.
 *
 * NOTE: the highlighter ADVANCES state per call, so every assertion runs over
 * spans captured in ONE pass. Re-calling line() for the same text walks a
 * different block-scalar state and asserts against the wrong document.
 */

const KEY = 'text-sky-300';
const STR = 'text-emerald-300';
const NUM = 'text-amber-300';
const BOOL = 'text-violet-300';
const COMMENT = 'text-zinc-600';
const DIM = 'text-zinc-500';

type Span = { text: string; cls: string };

/** Run a whole document through the highlighter once, keeping every line. */
function render(src: string): Span[][] {
  const h = yamlHighlighter();
  return src.split('\n').map((l) => h.line(l));
}
const text = (spans: Span[]) => spans.map((s) => s.text).join('');
const find = (spans: Span[], cls: string) => spans.find((s) => s.cls.includes(cls));
const count = (spans: Span[], cls: string) => spans.filter((s) => s.cls.includes(cls)).length;
/** The text of every run carrying `cls`, joined - a comment may be split
 *  across runs (leading gap + text) while still rendering as one comment. */
const clsText = (spans: Span[], cls: string) =>
  spans.filter((s) => s.cls.includes(cls)).map((s) => s.text).join('');

describe('yamlHighlighter', () => {
  it('reassembles every line exactly, whitespace included', () => {
    const lines = [
      '# Session 9 skills catalogue (grown from the Session 8 set).',
      'planner:',
      '  prompt: prompts/planner.md',
      '  temperature: 0.4    # a little breathing room',
      '  tools_allowed: []',
      '  - a',
      '    - nested',
      '  critic: true',
      '  max_tokens: 65536',
      '  name: "a # hash inside a string"',
      "  other: 'it''s # fine'",
      '',
    ];
    const out = render(lines.join('\n'));
    expect(out).toHaveLength(lines.length);
    out.forEach((spans, i) => {
      expect(text(spans)).toBe(lines[i]);
    });
  });

  it('does not treat a # inside a double-quoted string as a comment', () => {
    const [spans] = render('  name: "value # not a comment"');
    expect(find(spans, STR)?.text).toBe('"value # not a comment"');
    expect(count(spans, COMMENT)).toBe(0);
  });

  it('does not treat a # inside a single-quoted string as a comment', () => {
    const [spans] = render("  name: 'it''s # fine'");
    expect(find(spans, STR)?.text).toBe("'it''s # fine'");
    expect(count(spans, COMMENT)).toBe(0);
  });

  it('renders a trailing comment in comment colour, gap included', () => {
    const [spans] = render('  temperature: 0.4    # breathing room');
    // The run may be split (leading whitespace, then the comment body) but
    // every part of it must carry the comment colour, or the gap renders as
    // body text and the comment appears to start mid-line.
    expect(clsText(spans, COMMENT)).toBe('    # breathing room');
  });

  it('colours keys, numbers, booleans and list dashes', () => {
    const out = render('planner:\n  critic: true\n  max_tokens: 65536\n  - item');
    expect(find(out[0], KEY)?.text).toBe('planner');
    expect(find(out[1], BOOL)?.text).toBe('true');
    expect(find(out[2], NUM)?.text).toBe('65536');
    expect(find(out[3], DIM)?.text.trim()).toBe('-');
  });

  it('treats a block scalar body as literal, not as mappings', () => {
    const out = render([
      '  system: |',
      '    # Executive Summary',
      '    Note: this is prose inside the prompt, not a mapping.',
      '    - and this dash is text too',
    ].join('\n'));
    for (const spans of out.slice(1)) {
      expect(find(spans, KEY)).toBeUndefined();
      expect(find(spans, COMMENT)).toBeUndefined();
      expect(find(spans, DIM)).toBeUndefined();
      expect(spans.every((s) => s.cls === '')).toBe(true);
    }
  });

  it('ends the block when a line dedents, and tokenises it normally again', () => {
    const out = render(['  system: |', '    literal text', '  temperature: 0.4'].join('\n'));
    expect(find(out[1], KEY)).toBeUndefined();
    expect(find(out[2], NUM)?.text).toBe('0.4');
  });

  it('reset() forgets the block, so later lines tokenise structurally again', () => {
    // A reset means "this is a new document". Without a block context the
    // line IS a mapping, so it should be coloured as one - the opposite of
    // what it was inside the block. Asserting it stays plain here would pin
    // a bug: reset has to actually clear the state it exists to clear.
    const h = yamlHighlighter();
    h.line('  system: |');
    h.reset();
    expect(find(h.line('    Note: prose'), KEY)?.text).toBe('Note');
  });

  it('preserves indentation as its own run so nesting still reads', () => {
    const [spans] = render('    key: value');
    expect(spans[0]).toEqual({ text: '    ', cls: '' });
  });

  it('emits no span for an empty line (the view supplies the space)', () => {
    expect(render('')[0]).toEqual([]);
  });
});
