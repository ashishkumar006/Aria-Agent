import { describe, expect, it } from 'vitest';
import { checkCaveat, checkRows, checkWeakness, langForPath, outputRowsFrom } from './Dock';

/* The dock's two readouts are pure mappings, and each one is a claim about
 * evidence:
 *   Problems says what CHECKED the file — a delimiter heuristic reporting
 *   "clean" is not the same claim as the parser doing it;
 *   Output says what the shell PRINTED, and when it had to cut.
 * Both are tested on the mapping because a pane that renders a confident
 * nothing is worse than a pane that is missing. */

describe('checkRows', () => {
  it('tags every problem with the mode that found it', () => {
    const rows = checkRows('a/app.py', {
      mode: 'ast', checked: true,
      problems: [{ line: 3, col: 1, message: 'expected ":"', severity: 'error' }],
    });
    expect(rows).toEqual([{ path: 'a/app.py', line: 3, severity: 'error',
                            message: '[ast] expected ":"' }]);
  });

  it('keeps a heuristic answer visibly weaker than a parser answer', () => {
    const rows = checkRows('a.tsx', {
      mode: 'balance', checked: true,
      problems: [{ line: 1, col: 9, message: 'unbalanced )', severity: 'warning' }],
    });
    expect(rows[0].message).toMatch(/^\[balance\]/);
  });

  it('returns nothing for a clean file instead of inventing a row', () => {
    expect(checkRows('a.py', { mode: 'ast', checked: true, problems: [] }))
      .toEqual([]);
    expect(checkRows('a.py', {})).toEqual([]);
  });
});

describe('checkCaveat', () => {
  it('says so when there is nothing open to check', () => {
    expect(checkCaveat([], [], 0)).toMatch(/no file is open/);
  });

  it('names the weak parser rather than claiming a clean bill', () => {
    expect(checkCaveat(['a.py', 'b.tsx'], ['b.tsx: delimiter heuristic'], 0))
      .toMatch(/not a full type check.*delimiter heuristic/);
  });

  it('counts what it found when the check was real', () => {
    expect(checkCaveat(['a.py'], [], 2)).toMatch(/2 problem\(s\) in 1 open file/);
    expect(checkCaveat(['a.py'], [], 0)).toMatch(/syntax clean .*not a type check/);
  });
});

describe('outputRowsFrom', () => {
  it('mirrors a command with its own clipped output and says it was cut', () => {
    const rows = outputRowsFrom([{
      t: 'output', tool: 'run_command', command: 'pytest -q', ok: true,
      text: 'x'.repeat(20000), clipped: true, chars: 91234, latency: 3.2,
    }]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ command: 'pytest -q', ok: true,
                                    clipped: true, chars: 91234, tool: 'run_command' });
  });

  it('treats a failed command as evidence, not as an absence', () => {
    const rows = outputRowsFrom([{
      t: 'output', tool: 'run_command', command: 'npm test', ok: false,
      text: 'FAIL', clipped: false, chars: 4, latency: 0.4,
    }]);
    expect(rows[0].ok).toBe(false);
  });

  it('lists the verify pass and a refused mention', () => {
    const rows = outputRowsFrom([
      { t: 'verify', ok: false, checked: 2, error: 'app.py: expected ":"' },
      { t: 'status', text: 'not attached: ../outside/x.py — outside this session' },
      { t: 'delta', text: 'noise that is not an event' },
    ]);
    expect(rows.map((r) => r.command)).toEqual(['verify 2 file(s)', 'context']);
    expect(rows[1].ok).toBe(false);
  });

  it('says nothing when the run ran nothing', () => {
    expect(outputRowsFrom([])).toEqual([]);
  });
});

/* Two live defects, both found in the browser and both about a pane being
 * confident about something it never looked at:
 *   the check was sent the file's EXTENSION ("py") while the route speaks
 *   LANGUAGE ids ("python"), so every Python buffer answered mode "none" with
 *   no problems and Problems printed "syntax clean";
 *   the effect was keyed on the list of open paths, so typing a syntax error
 *   into an already-open file never re-ran anything. */
describe('langForPath', () => {
  it('names the language the check route actually understands', () => {
    expect(langForPath('demo/src/app.py')).toBe('python');
    expect(langForPath('lib/helper.ts')).toBe('typescript');
    expect(langForPath('web/App.tsx')).toBe('typescriptreact');
    expect(langForPath('web/legacy.js')).toBe('javascript');
    expect(langForPath('web/legacy.jsx')).toBe('javascript');
    expect(langForPath('conf/settings.json')).toBe('json');
  });

  it('says "text" where there is no parser, instead of inventing a language', () => {
    expect(langForPath('README.md')).toBe('text');
    expect(langForPath('notes.yaml')).toBe('text');
    expect(langForPath('Makefile')).toBe('text');
  });
});

describe('checkWeakness', () => {
  it('calls an unchecked file unchecked, never clean', () => {
    const why = checkWeakness('demo/src/app.py',
      { mode: 'none', language: 'py', checked: false, problems: [],
        note: 'no parser available for this language' });
    expect(why).toContain('demo/src/app.py');
    expect(why).toContain('no parser available for this language');
  });

  it('names the heuristic when the check only balanced delimiters', () => {
    expect(checkWeakness('a/x.js',
      { mode: 'balance', language: 'javascript', checked: true, problems: [] }))
      .toBe('a/x.js: delimiter heuristic');
  });

  it('is silent when a real parser looked and found nothing', () => {
    expect(checkWeakness('a/app.py',
      { mode: 'ast', language: 'python', checked: true, problems: [] })).toBe('');
    expect(checkWeakness('a/app.py', null)).toContain('nothing here can check');
  });
});

describe('checkCaveat with an unchecked file in the set', () => {
  it('never claims syntax clean about a file nothing parsed', () => {
    const note = checkCaveat(['a/app.py', 'a/notes.md'],
      ['a/notes.md: no parser available for this language'], 0);
    expect(note).not.toMatch(/clean/);
    expect(note).toContain('no parser available');
  });
});
