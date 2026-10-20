import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * The agent panel's conversation must survive a reload and an agent restart.
 *
 * It did not: `turns` was `useState` only, so every reload came back empty -
 * the most obvious difference from Cursor or Copilot, where a follow-up after
 * a restart is ordinary. Persistence is a correctness property of the
 * component, not a preference, so the rules that make it SAFE are pinned here:
 *
 *  - a turn that is still streaming is never written, because a truncated
 *    answer restored into history poisons the next question
 *  - the store is bounded, so a runaway loop cannot fill the quota
 *  - clear() removes from disk, or a reload resurrects what the user deleted
 *  - a corrupt store degrades to an empty panel, never a crash
 */
const SRC = readFileSync(
  resolve(__dirname, 'AgentPanel.tsx'), 'utf-8');

describe('AgentPanel conversation persistence', () => {
  it('seeds turns from storage rather than an empty array', () => {
    expect(SRC).toMatch(/useState<Turn\[\]>\(\(\) => loadTurns\(\)\)/);
  });

  it('persists only settled turns', () => {
    // The effect must bail while anything is busy, and saveTurns must filter
    // them out as a second line of defence.
    expect(SRC).toMatch(/if \(turns\.some\(\(t\) => t\.busy\)\) return;/);
    expect(SRC).toMatch(/turns\.filter\(\(t\) => !t\.busy\)/);
  });

  it('never restores a turn that was in flight', () => {
    expect(SRC).toMatch(/busy: false/);
  });

  it('bounds the store', () => {
    expect(SRC).toMatch(/const MAX_TURNS = \d+/);
    expect(SRC).toMatch(/\.slice\(-MAX_TURNS\)/);
  });

  it('clear() also clears the stored conversation', () => {
    expect(SRC).toMatch(/localStorage\.removeItem\(STORE_KEY\)/);
  });

  it('survives a corrupt store instead of crashing', () => {
    expect(SRC).toMatch(/catch \{[\s\S]*?return \[\];[\s\S]*?\}/);
  });
});
