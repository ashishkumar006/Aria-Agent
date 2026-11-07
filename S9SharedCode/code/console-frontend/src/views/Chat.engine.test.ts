import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/* The Lightweight/DAG choice decides whether a turn costs ~2s or ~60s, whether
   it fans out into a planner and a fleet, and whether it appears in Runs at all.
   It lived only in the left rail — which is narrow, and which `ui.pane_hiding`
   lets the user hide entirely. The user asked for the two options to be ADDED,
   having read the "23 tools · no pipeline" label as evidence that no choice
   existed. A control the footer names but the interface hides is not a control.

   So the toggle is repeated in the composer footer. Both must drive the SAME
   state (`mode` via `switchMode`), because two controls that disagree about the
   engine is worse than one. */
const SRC = readFileSync(resolve(__dirname, 'Chat.tsx'), 'utf-8');

describe('chat engine toggle', () => {
  it('offers the engine choice at the composer, not only in the rail', () => {
    const composerIdx = SRC.indexOf('placeholder="Message Aria…"');
    expect(composerIdx).toBeGreaterThan(-1);
    // Everything after the textarea is the composer's own footer.
    const after = SRC.slice(composerIdx);
    const groups = after.match(/role="group" aria-label="Engine/g) || [];
    expect(groups.length).toBeGreaterThan(0);
    expect(after).toMatch(/switchMode\('light'\)/);
    expect(after).toMatch(/switchMode\('dag'\)/);
  });

  it('has the two controls share one state', () => {
    // Same handler and same `mode` value, so the rail and the composer can
    // never show different engines for one conversation.
    const pressed = SRC.match(/aria-pressed=\{mode === '(light|dag)'\}/g) || [];
    expect(pressed.length).toBeGreaterThanOrEqual(4);
    expect(SRC).not.toMatch(/composerMode|secondMode|localMode/);
  });

  it('labels what each engine actually costs', () => {
    // A title that says only "fast" / "slower" is not enough to choose with:
    // the difference is a visible pipeline and a row in Runs.
    expect(SRC).toMatch(/not recorded in Runs/);
    expect(SRC).toMatch(/recorded in Runs/);
  });
});
