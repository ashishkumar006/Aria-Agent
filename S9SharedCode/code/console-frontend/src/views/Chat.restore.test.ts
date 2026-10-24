import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * The first send used to wipe its own live answer. `send()` mints a cid and
 * calls rememberCid() while the answer is still streaming; the restore effect
 * re-ran, fetched the not-yet-persisted thread (empty), and called setMsgs([]).
 * The guard is the whole fix, so pin it: restore must not run while send owns
 * the transcript.
 */
const SRC = readFileSync(resolve(__dirname, 'Chat.tsx'), 'utf-8');

describe('Chat restore guard', () => {
  it('does not restore an empty thread while a send is in flight', () => {
    expect(SRC).toMatch(
      /if \(restoredRef\.current \|\| !cid \|\| sendingRef\.current\) return;/,
    );
  });

  it('remembers a separate open thread for each engine', () => {
    expect(SRC).toMatch(/aria_chat_last_cid_\$\{m\}/);
    expect(SRC).toMatch(/lastCidKey\(next\)/);
  });

  it('reattaches to a server-owned pending turn instead of showing it empty', () => {
    expect(SRC).toMatch(/const hasPending = msgs\.some\(\(m\) => m\.pending\)/);
    expect(SRC).toMatch(/api\.chatThread\(cid\)/);
  });

  it('stops the server run, not only the local stream', () => {
    expect(SRC).toMatch(/api\.cancelRun\(\{ conversation_id: liveCid \}\)/);
    expect(SRC).toMatch(/const liveCid = cidRef\.current/);
  });

  it('does not write a DAG turn a second time from the client', () => {
    expect(SRC).not.toMatch(/chatThreadTurn/);
  });
});
