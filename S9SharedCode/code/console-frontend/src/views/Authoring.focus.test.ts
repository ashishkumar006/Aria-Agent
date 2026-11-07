import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/* Focus is `/authoring/focus`: one document and the conversation about it, with
   the rail, the run list and the tab strip removed. Two things were wrong
   inside it, and both came from treating "hide the furniture" as "hide
   everything that is not the document".

   The gateway model picker was gated on `!focus`. A reader cannot see which
   model answered the paragraph beside them, and cannot change it — the one
   control in the mode that is not furniture.

   The split between the panes was a fixed `xl:max-w-[32%]`, which is a guess
   about whether the reader is working through a forty-page report or asking a
   one-line question. It is now draggable, and the choice survives a reload. */
const SRC = readFileSync(resolve(__dirname, 'Authoring.tsx'), 'utf-8');

describe('Authoring Focus mode', () => {
  it('shows the model picker in Focus as well as in the tab', () => {
    const pick = SRC.indexOf('<ModelPicker model={discModel}');
    expect(pick).toBeGreaterThan(-1);
    // Whatever gates the picker, it must not be the mode.
    const before = SRC.slice(Math.max(0, pick - 320), pick);
    expect(before).not.toMatch(/\{!focus\s*&&\s*\(/);
    expect(before).not.toMatch(/focus\s*\?\s*null\s*:/);
  });

  it('sizes the panes from the slider instead of a fixed 32%', () => {
    expect(SRC).toMatch(/width: `\$\{100 - focusSplit\}%`/);
    /* The old hard-coded width has to be gone, not merely overridden: a class
       and an inline style that disagree is a layout decided by CSS load order. */
    expect(SRC).not.toMatch(/xl:max-w-\[32%\]/);
  });

  it('remembers the split across reloads and clamps it', () => {
    expect(SRC).toMatch(/aria\.focus\.split/);
    expect(SRC).toMatch(/localStorage\.setItem\(FOCUS_SPLIT_KEY, String\(focusSplit\)\)/);
    // Bounded both ways — a drag must not be able to hide either pane entirely.
    expect(SRC).toMatch(/SPLIT_MIN = (2[0-9])/);
    expect(SRC).toMatch(/SPLIT_MAX = (8[0-5])/);
  });

  it('is reachable by keyboard, not only by a mouse', () => {
    expect(SRC).toMatch(/role="separator"/);
    expect(SRC).toMatch(/aria-valuenow=\{focusSplit\}/);
    expect(SRC).toMatch(/ArrowLeft/);
    expect(SRC).toMatch(/ArrowRight/);
  });

  /* Below Tailwind's `xl` the two panes stack. A percentage width applied there
     would squeeze the conversation into a narrow strip on a tall page, so the
     inline width is gated on the media query — and the divider's own hiding
     place is the same breakpoint. */
  it('only applies the split where the panes are side by side', () => {
    expect(SRC).toMatch(/min-width: 1280px/);
    expect(SRC).toMatch(/focus && splitLive/);
  });

  it('leaves the switch that turns the mode off alone', () => {
    // `ui.authoring_focus` is the kill switch; removing the mode's controls
    // while leaving the flag would leave a switch with no visible effect.
    expect(SRC).toMatch(/ui\.authoring_focus/);
  });
});
