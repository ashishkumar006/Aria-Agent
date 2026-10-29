import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * Uploading a file must show it.
 *
 * The gateway indexes writes asynchronously: the GET issued immediately after
 * the POST can return the pre-upload list. Nothing else retried, because the
 * 3 s index poll only runs when a row on screen has a pending status — with
 * the new file absent there was no pending row, so the upload stayed invisible
 * for up to the 15 s list poll. The POST response carries the document, so it
 * is used to insert the row immediately and the normal poll reconciles it.
 */
const SRC = readFileSync(resolve(__dirname, 'Documents.tsx'), 'utf-8');

describe('document upload shows its row', () => {
  it('keeps the document returned by the upload', () => {
    expect(SRC).toMatch(/const added: DocItem\[\] = \[\];/);
    expect(SRC).toMatch(/if \(r\.document\) added\.push\(r\.document\)/);
  });

  it('puts the confirmed row on screen before the racing list read', () => {
    const firstMerge = SRC.indexOf('if (added.length) { mergeAdded(added); setLoaded(true); }');
    const loadAt = SRC.indexOf('if (ok.length) await load()');
    const secondMerge = SRC.indexOf('if (added.length) mergeAdded(added);');
    expect(firstMerge).toBeGreaterThan(-1);
    expect(loadAt).toBeGreaterThan(firstMerge);
    // And again after it: the read can still be the pre-upload list.
    expect(secondMerge).toBeGreaterThan(loadAt);
  });

  it('still bypasses the cache for the indexing poll', () => {
    expect(SRC).toMatch(/load\(true\)/);
  });
});
