import { describe, expect, it } from 'vitest';
import { caretToken, checksSummary, checksToRows, mentionTokens } from './Composer';

/* The composer's two derived functions, tested because both decide what the
 * agent is handed and neither can be seen in a screenshot: a popup that opens
 * on the wrong slice of text looks fine until the path it inserts is the one
 * the model then cannot read.
 *
 * These are pure — caret in, token out — which is the point of keeping the
 * trigger detection out of the component body. */

describe('caretToken', () => {
  it('sees an @ mention under the caret', () => {
    const text = 'explain @alpha/src/app';
    const t = caretToken(text, text.length);
    expect(t).toEqual({ kind: '@', frag: 'alpha/src/app',
                        from: text.length - 'alpha/src/app'.length - 1 });
  });

  it('sees an empty @ as a request for the whole list', () => {
    const t = caretToken('look at @', 9);
    expect(t.kind).toBe('@');
    expect(t.frag).toBe('');
  });

  it('does not turn an email into a mention', () => {
    const text = 'ask dev@example.com about it';
    expect(caretToken(text, text.indexOf('dev@') + 4).kind).toBe('');
  });

  it('treats a slash as a command only at the very start', () => {
    expect(caretToken('/und', 4)).toEqual({ kind: '/', frag: 'und', from: 0 });
    // "check /tmp/x" is a path in prose. Treating it as a verb would run a
    // command nobody asked for.
    expect(caretToken('check /tmp/x', 12).kind).toBe('');
  });

  it('stops being a token once the caret has moved past it', () => {
    const text = 'explain @app.py please';
    expect(caretToken(text, text.length).kind).toBe('');
    expect(caretToken(text, 'explain @app'.length).kind).toBe('@');
  });

  it('reads a # problem trigger the same way', () => {
    const text = 'fix #prob';
    const t = caretToken(text, text.length);
    expect(t.kind).toBe('#');
    expect(t.frag).toBe('prob');
  });

  it('ignores a mention inside a word', () => {
    expect(caretToken('use handle@foo', 14).kind).toBe('');
  });
});

describe('mentionTokens', () => {
  it('lists each mention once, in order', () => {
    expect(mentionTokens('@a/b.py and @c/d.ts, then @a/b.py again'))
      .toEqual(['a/b.py', 'c/d.ts']);
  });

  it('drops the punctuation that ended the sentence, not the extension', () => {
    expect(mentionTokens('read @src/app.py, then @src/db.py.')).toEqual(
      ['src/app.py', 'src/db.py']);
  });

  it('is the same list the server derives', () => {
    // The chip row and the prompt must agree. If the frontend strips a
    // trailing dot the server keeps (or the reverse), the chip shows a file
    // the agent was handed a different name for.
    const text = 'compare @alpha/src/app.py with @beta/tool.ts';
    expect(mentionTokens(text)).toEqual(['alpha/src/app.py', 'beta/tool.ts']);
  });

  it('says nothing when there is nothing to attach', () => {
    expect(mentionTokens('no context here')).toEqual([]);
    expect(mentionTokens('')).toEqual([]);
  });
});

describe('checksToRows', () => {
  it('hands the agent the compiler line, not a count', () => {
    const rows = checksToRows({
      ok: false, checks_run: 1, cwd: '',
      results: [{ tool: 'tsc', ok: false, exit_code: 2,
                  stdout: '', stderr: 'src/a.ts(1,2): error TS1: missing ;\n'
                                   + 'src/b.ts(3,4): error TS2: unknown type' }],
    } as never);
    expect(rows.length).toBe(2);
    expect(rows[0].message).toContain('error TS1');
    expect(rows[0].severity).toBe('error');
  });

  it('names a tool that is not installed instead of hiding it', () => {
    const rows = checksToRows({
      ok: true, checks_run: 0,
      results: [{ tool: 'mypy', ok: true, exit_code: null, available: false }],
    } as never);
    expect(rows[0].message).toContain('not installed');
  });

  /* The counter next to the composer means "things are wrong". A passing
     pytest prints two lines of stdout, and turning those into rows made a green
     run read "2 problems" — the fastest way to teach someone to ignore it. */
  it('reports nothing for a check that passed', () => {
    const rows = checksToRows({
      ok: true, checks_run: 1,
      results: [{ tool: 'pytest', ok: true, exit_code: 0,
                  stdout: '..\n4 passed in 0.1s\n' }],
    } as never);
    expect(rows).toEqual([]);
  });

  it('still reports a failure line when a check fails', () => {
    const rows = checksToRows({
      ok: false, checks_run: 1,
      results: [{ tool: 'pytest', ok: false, exit_code: 1,
                  stdout: '', stderr: 'FAILED test_x.py::test_y - assert 1 == 2' }],
    } as never);
    expect(rows[0].severity).toBe('error');
    expect(rows[0].message).toContain('test_y');
  });

  it('caps at twelve rows and never invents a row from silence', () => {
    const many = Array.from({ length: 30 }, (_x, i) => `line ${i}`);
    const rows = checksToRows({
      ok: false, checks_run: 1,
      results: [{ tool: 'pytest', ok: false, exit_code: 1,
                  stdout: '', stderr: many.join('\n') }],
    } as never);
    expect(rows.length).toBeLessThanOrEqual(12);
    expect(checksToRows({ ok: true, checks_run: 0, results: [] } as never))
      .toEqual([]);
  });
});

/* The summary is the one line a person reads instead of the output. "No checks
 * were runnable · all runnable checks passed" is two true clauses that add up to
 * a lie, so the verdict clause is dropped when nothing ran. */
describe('checksSummary', () => {
  it('says what passed when checks ran', () => {
    expect(checksSummary({
      ok: true, checks_run: 2,
      results: [{ tool: 'pytest', ok: true }, { tool: 'tsc', ok: true }],
    } as never)).toContain('2 check(s) ran · all runnable checks passed');
  });

  it('names the failing tool', () => {
    expect(checksSummary({
      ok: false, checks_run: 2,
      results: [{ tool: 'pytest', ok: false }, { tool: 'tsc', ok: true }],
    } as never)).toContain('failed: pytest');
  });

  it('refuses to call a run with no checker a pass', () => {
    const s = checksSummary({ ok: false, checks_run: 0, results: [],
                              error: 'no checker detected' } as never);
    expect(s).toContain('no checks were runnable');
    expect(s).not.toContain('passed');
    expect(s).toContain('no checker detected');
  });

  it('does not claim a pass when every checker is missing', () => {
    const s = checksSummary({
      ok: false, checks_run: 0,
      results: [{ tool: 'pytest', ok: false, available: false }],
    } as never);
    expect(s).toContain('not installed: pytest');
    expect(s).not.toContain('all runnable checks passed');
  });
});
