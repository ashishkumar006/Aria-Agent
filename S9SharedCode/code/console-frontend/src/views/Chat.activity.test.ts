import { describe, expect, it } from 'vitest';
import { activityLabel } from './Chat';

/* A run used to report only "working… 40s elapsed" between node completions, so
   a forty-second planner call was indistinguishable from a forty-second
   six-way fan-out, and every other stdout line (the `====` session banner,
   "[memory.read] 8 hit(s)") overwrote the status when it was not actually
   activity at all.
   The executor now emits one line per node start, and this decides what reaches
   the reader. The contract worth pinning is the negative one: a line that is
   not activity must return null so the status keeps saying what is running. */
describe('activityLabel', () => {
  it('turns a node start into the work being done', () => {
    expect(activityLabel(
      '[n:2] researcher         running · Construction materials: mortar and pozzolana',
    )).toBe('researcher reading: Construction materials: mortar and pozzolana');
  });

  it('keeps the timing off a completion', () => {
    expect(activityLabel('[n:1] planner            complete (51.9s)'))
      .toBe('planner complete in 51.9s');
  });

  it('survives a line with no brief at all', () => {
    expect(activityLabel('[n:1] formatter          running'))
      .toBe('formatter reading');
  });

  it('reports a failure as a failure', () => {
    const out = activityLabel('[n:3] browser            failed (12.0s)  err=timeout');
    expect(out).toContain('failed');
    expect(out).not.toContain('reading');
  });

  /* The whole point. These are all real lines the executor prints, and each one
     used to become the status verbatim. */
  it('ignores everything that is not node activity', () => {
    for (const noise of [
      '',
      '==============================================================================',
      'session s8-4852b475  -  query: What is 2+2?',
      '[memory.read] 8 hit(s) visible to every skill this run',
      '[n:1] research plan stored: 1 facet(s)',
      '[chat] tool loop failed, plain-text fallback: RuntimeError: mcp child died',
      'working… 10s elapsed',
    ]) {
      // Vitest's `expect` takes one argument, so the identifying text is
      // thrown rather than passed as a message — otherwise a failure here
      // would say "expected null" with no clue which line caused it.
      if (activityLabel(noise) !== null) {
        throw new Error(`treated as activity: ${noise.slice(0, 48)}`);
      }
      expect(activityLabel(noise)).toBeNull();
    }
  });
});
