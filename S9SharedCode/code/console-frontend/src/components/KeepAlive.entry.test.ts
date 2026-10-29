import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * A section switch must be an INSTANT swap, the way an editor swaps tabs.
 *
 * Every animated design was measured and rejected on the live console:
 *   - the old `display:none` + fade-from-0 left the content area empty for
 *     ~80-220ms, which read as a blink;
 *   - a crossfade blended two text-dense pages, so the Runs list was legible
 *     through the Chat transcript;
 *   - a left-to-right `clip-path` wipe fixed the blend but read as a sweep;
 *   - a fade through the page background dimmed the whole content area.
 *
 * VS Code has no transition at all: the new editor is simply there, fully
 * opaque, in the frame after the click. That is the contract here - no
 * animation, no opacity animation, no delay, and no held outgoing layer.
 */
const SRC = readFileSync(resolve(__dirname, 'KeepAlive.tsx'), 'utf-8');
const CSS = readFileSync(resolve(__dirname, '../index.css'), 'utf-8');

describe('keep-alive section switch', () => {
  it('swaps sections in one commit, with no animation on either side', () => {
    expect(SRC).toMatch(/hidden=\{!active\}/);
    expect(SRC).toMatch(/style=\{active \? undefined : \{ display: 'none' \}\}/);
    // No transition machinery may come back: no held outgoing layer, no
    // animation classes, no timing constant.
    expect(SRC).not.toMatch(/view-in|view-out|leaving|entering|EXIT_MS|clip-path/);
    expect(SRC).not.toMatch(/setTimeout/);
    // ...and the stylesheet has no section keyframes or class rules at all.
    expect(CSS).not.toMatch(/@keyframes view-(in|out)/);
    expect(CSS).not.toMatch(/\.view-(in|out)\b/);
    expect(CSS).not.toMatch(/clip-path/);
  });

  it('keeps a hidden section mounted, out of the a11y tree and the tab order', () => {
    // Hidden is a visibility state, never an unmount - that is what preserves
    // scroll position, fetched data and in-flight work across a switch.
    expect(SRC).toMatch(/aria-hidden=\{active \? undefined : true\}/);
    expect(SRC).toMatch(/inert=\{active \? undefined : true\}/);
    expect(SRC).toMatch(/nodes\.current\.set\(id, \(/);
    expect(SRC).toMatch(/nodes\.current\.get\(id\) \?\? null/);
  });

  it('does not refetch on every activation inside the poll cadence', () => {
    expect(SRC).toMatch(/if \(Date\.now\(\) - last\.current >= ms\)/);
    // Hidden views must still stop their timer.
    expect(SRC).toMatch(/if \(!active\) return;/);
  });

  it('suspends a cold chunk inside its own section, not over the shell', () => {
    expect(SRC).toMatch(/<Suspense fallback=\{<SectionFallback \/>\}>\{render\(\)\}<\/Suspense>/);
    expect(SRC).toMatch(/<SectionShell key=\{id\} active=\{id === current\}>/);
  });
});
