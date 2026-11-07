import { describe, expect, it } from 'vitest';
import { baseName, isUnderRoot, joinPath, langFor, relWithinRoot, rootNameOf } from './studioSession';

/* Path arithmetic for the studio: which file a click opens, which folder a
 * save is confined to, and how a buffer decides its language. Small functions
 * that read as obviously right until a SECOND root is declared — which is the
 * whole premise of the studio, so they are checked rather than trusted. */
describe('studio paths', () => {
  it('splits a base-relative path into its root and the rest', () => {
    expect(rootNameOf('proj-a/src/app.ts')).toBe('proj-a');
    expect(rootNameOf('proj-a')).toBe('proj-a');
    expect(rootNameOf('/proj-a/x.py')).toBe('proj-a');
    expect(relWithinRoot('proj-a/src/app.ts', 'proj-a')).toBe('src/app.ts');
    expect(relWithinRoot('proj-a', 'proj-a')).toBe('');
  });

  it('rejoins them without doubling a separator', () => {
    expect(joinPath('proj-a', 'src/app.ts')).toBe('proj-a/src/app.ts');
    expect(joinPath('proj-a/', '/src/app.ts')).toBe('proj-a/src/app.ts');
    expect(joinPath('proj-a', '')).toBe('proj-a');
  });

  it('refuses a path whose root was never declared', () => {
    const roots = [{ path: 'C:/a/proj-a', name: 'proj-a' }];
    expect(isUnderRoot('proj-a/main.py', roots)).toBe(true);
    expect(isUnderRoot('proj-b/main.py', roots)).toBe(false);
    // Not the security boundary — `_code_abs` on the server is. This only
    // stops the UI offering a click the request would refuse.
    expect(isUnderRoot('', roots)).toBe(false);
  });

  it('picks a language from the extension and never invents one', () => {
    expect(langFor('a/b/c.tsx')).toBe('typescript');
    expect(langFor('script.py')).toBe('python');
    expect(langFor('Makefile')).toBe('plaintext');
    expect(langFor('data.jsonl')).toBe('plaintext');
    expect(baseName('a/b/c.tsx')).toBe('c.tsx');
    expect(baseName('')).toBe('');
  });
});
