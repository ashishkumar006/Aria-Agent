import { describe, expect, it } from 'vitest';
import { highlightTokens, langFor, langOf } from './highlight';

/* The transcript's code blocks are read by a person scanning for the shape of a
 * change, so the token boundaries matter more than the colours: a comment that
 * swallows the rest of the line, or a string that ends early, makes the snippet
 * lie. These pin the boundaries, not the palette. */
describe('highlight', () => {
  it('marks a python comment, string, keyword and number', () => {
    const out = highlightTokens(
      `# set the key\ns.set("a", 1, ttl=10)\nassert s.ttl("a") == 10`, 'python');
    const joined = out.join('');
    expect(joined).toContain('<tok-com># set the key</tok-com>');
    expect(joined).toContain('<tok-str>"a"</tok-str>');
    expect(joined).toContain('<tok-kw>assert</tok-kw>');
    expect(joined).toContain('<tok-num>10</tok-num>');
  });

  it('keeps python triple-quoted strings whole', () => {
    const out = highlightTokens('"""a\nb"""\nx = 1', 'python').join('');
    expect(out).toContain('<tok-str>"""a\nb"""</tok-str>');
    // The assignment target is not a keyword: it must survive as plain text.
    expect(out).toContain('x = ');
  });

  it('does not end a python string at an escaped quote', () => {
    const out = highlightTokens(`s = "a\\"b"`, 'python').join('');
    expect(out).toContain('<tok-str>"a\\"b"</tok-str>');
  });

  it('marks // and /* */ comments in javascript', () => {
    const js = highlightTokens('// hi\n/* block */\nconst a = 1', 'javascript').join('');
    expect(js).toContain('<tok-com>// hi</tok-com>');
    expect(js).toContain('<tok-com>/* block */</tok-com>');
    expect(js).toContain('<tok-kw>const</tok-kw>');
  });

  it('treats a json key differently from a json string', () => {
    const js = highlightTokens('{"a": "b", "n": 2}', 'json').join('');
    expect(js).toContain('<tok-key>"a"</tok-key>');
    expect(js).toContain('<tok-str>"b"</tok-str>');
    expect(js).toContain('<tok-num>2</tok-num>');
  });

  it('leaves an unknown language alone', () => {
    expect(highlightTokens('echo hi', 'text')).toEqual(['echo hi']);
    expect(highlightTokens('', 'python')).toEqual(['']);
  });

  it('maps fence labels to a tokenizer', () => {
    expect(langOf('py')).toBe('python');
    expect(langOf('TypeScript')).toBe('javascript');
    expect(langOf('sh')).toBe('bash');
    expect(langOf('')).toBe('text');
    expect(langOf('diff')).toBe('text');
  });

  /* Every fence in a real session arrived with no language tag, so the guess is
     what makes colouring appear at all. */
  it('guesses python from the snippet', () => {
    expect(langFor('', 's.set("a", 1)\nassert s.ttl("a") == 10')).toBe('python');
    expect(langFor('', 'def f(x):\n    return x')).toBe('python');
  });

  it('guesses javascript from the snippet', () => {
    expect(langFor('', 'const a = 1;\nfunction f() {}')).toBe('javascript');
  });

  it('leaves a diff and prose as plain text', () => {
    expect(langFor('', '--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@')).toBe('text');
    expect(langFor('', 'the value is 10')).toBe('text');
  });

  it('trusts an explicit label over the guess', () => {
    expect(langFor('bash', 'ls -la')).toBe('bash');
  });

  it('never loses a character', () => {
    const code = 'def f(x):\n    return x + 1  # noqa\n';
    const joined = highlightTokens(code, 'python').join('');
    const text = joined.replace(/<[^>]+>/g, '');
    expect(text).toBe(code);
  });
});