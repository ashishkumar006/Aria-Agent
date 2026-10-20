import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * `useLocation()` reads the Router context, and `App` is the component that
 * RETURNS `<BrowserRouter>`. Calling the hook in `App`'s own body therefore
 * reads a context that does not exist yet and React throws
 *
 *   "useLocation() may be used only in the context of a <Router> component"
 *
 * which blanks the entire console with no fallback - every section dead. The
 * build passed and tsc passed, because the rule is about RUNTIME position in
 * the tree, not types.
 *
 * The only reliable guard is to assert the hook is not called from the
 * component that mounts the router.
 */
const APP = readFileSync(
  resolve(__dirname, 'App.tsx'), 'utf-8');
/** Source with comments stripped: this file documents the router in prose, and
 *  a `<BrowserRouter>` written in a comment must not read as an element. */
const CODE = APP.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');

describe('App router shell', () => {
  it('does not call useLocation from the component that mounts the router', () => {
    // Find `export default function App()` and the end of its body.
    const start = APP.indexOf('export default function App()');
    expect(start).toBeGreaterThan(-1);
    const rest = APP.slice(start);
    // The App function is short and its body ends before the next top-level
    // declaration or EOF; anything longer means the shape changed and this
    // check should be re-read by a human.
    const body = rest.slice(0, rest.indexOf('\n}\n') + 3);
    expect(body).not.toContain('useLocation(');
  });

  it('mounts the router exactly once', () => {
    // `</BrowserRouter>` matches the same substring, so count openings only.
    const opened = (CODE.match(/<BrowserRouter>/g) || []).length;
    const closed = (APP.match(/<\/BrowserRouter>/g) || []).length;
    expect(opened).toBe(1);
    expect(closed).toBe(1);
  });

  it('has a component inside the router that reads the location', () => {
    // The hook has to live somewhere INSIDE the router, otherwise the
    // console cannot know which section to show.
    expect(APP).toMatch(/function RouterShell\(\)/);
    const shell = APP.slice(APP.indexOf('function RouterShell()'));
    expect(shell.slice(0, 400)).toContain('useLocation()');
  });

  it('still mounts every registered section exactly once', () => {
    const ids = [...CODE.matchAll(/\{ id: '(\/[\w-]*)'/g)].map((m) => m[1]);
    expect(ids.length).toBeGreaterThan(0);
    expect(new Set(ids).size).toBe(ids.length);
  });
});