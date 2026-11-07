import { describe, expect, it, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * Per-pane hiding, and the one flag that turns it off.
 *
 * The failures this pins are all silent. A flag that is declared but never
 * read looks completely wired up and does nothing — `apps.views.table` already
 * does exactly that in this repo. A pane that hides without leaving a way back
 * strands the user inside a view. And a feature that wraps its children in a
 * div when disabled changes the layout it was supposed to leave alone, which is
 * the residue the whole kill switch exists to avoid.
 */
const PANES = readFileSync(resolve(__dirname, 'panes.tsx'), 'utf-8');
const FLAGS = readFileSync(resolve(__dirname, '../../../flags.py'), 'utf-8');
const AUTHORING = readFileSync(resolve(__dirname, '../views/Authoring.tsx'), 'utf-8');
const UI = readFileSync(resolve(__dirname, '../components/ui.tsx'), 'utf-8');

describe('the kill switch', () => {
  it('is defined in the backend flag registry', () => {
    expect(FLAGS).toMatch(/"ui\.pane_hiding":\s*\{/);
  });

  it('is actually read by the gate — not declared and consulted nowhere', () => {
    /* The dead-flag pattern: a name in DEFINITIONS, a toggle in Settings, and
       no production code that asks. Asserting the literal here means renaming
       or dropping the flag breaks a test instead of quietly doing nothing. */
    expect(PANES).toMatch(/export const PANE_FLAG = 'ui\.pane_hiding'/);
    expect(PANES).toMatch(/find\(\(f\) => f\.name === PANE_FLAG\)/);
  });

  it('is consulted in exactly one file', () => {
    /* Off has to mean every pane renders as before. That only holds while no
       view asks the flag itself, so a second consumer is a design break. */
    const consumers = [PANES, AUTHORING, UI].filter((src) => /ui\.pane_hiding|usePanesEnabled/.test(src));
    expect(consumers).toHaveLength(1);
  });

  it('fails open when the flag cannot be read', () => {
    expect(PANES).toMatch(/catch \{\s*\n?\s*next = true;/);
    expect(PANES).toMatch(/const getGate = \(\) => gate !== false/);
  });

  it('reads the flag live, so switching it off takes effect immediately', () => {
    /* api.flags() is served from a 30s read-through cache by default; a gate
       on that cache would keep hiding panes for half a minute after the flip. */
    expect(PANES).toMatch(/api\.flags\(true\)/);
  });

  it('leaves no residue when off: the gate reports shown and children render bare', () => {
    expect(PANES).toContain("if (!enabled) return 'shown';");
    expect(PANES).toContain("if (state === 'hidden') return null;");
    // Shown must not gain a wrapper element; these panes are flex children and
    // an extra layer would change the layout the feature is meant to leave alone.
    expect(PANES).toContain('return <>{children}</>;');
  });
});

describe('the registry', () => {
  beforeEach(() => { /* state is module-scope; these tests only read it */ });

  it('lists every pane under a view key that exists', async () => {
    const { PANE_REGISTRY } = await import('./panes');
    for (const [view, specs] of Object.entries(PANE_REGISTRY)) {
      for (const p of specs) {
        expect(p.id.startsWith(`${view}.`)).toBe(true);
        expect(['hide', 'collapse']).toContain(p.mode);
        expect(p.label.length).toBeGreaterThan(3);
      }
      const ids = specs.map((p) => p.id);
      expect(new Set(ids).size).toBe(ids.length);
    }
  });

  it('wraps every pane it advertises', async () => {
    /* The dead-toggle failure mode, and the reason this test exists: a registry
       entry with no <Pane> or usePaneMode around anything gives the user a
       Layout row that says "hidden" while the pane stays on screen. That is
       strictly worse than not offering it, because it teaches the user the
       control is a lie. Same class of bug as a flag that is defined but never
       read. */
    const { PANE_REGISTRY } = await import('./panes');
    for (const [view, specs] of Object.entries(PANE_REGISTRY)) {
      const src = readFileSync(resolve(__dirname, `../views/${view}.tsx`), 'utf-8');
      for (const p of specs) {
        const wrapped = src.includes(`<Pane id="${p.id}"`)
          || src.includes(`usePaneMode('${p.id}')`)
          || src.includes(`PaneStrip id="${p.id}"`);
        expect(`${view}/${p.id} wrapped: ${wrapped}`).toBe(`${view}/${p.id} wrapped: true`);
      }
    }
  });

  it('mounts a Layout control in every view that registers panes', async () => {
    /* A registered pane with no Layout control in its view is unreachable:
       the user can hide it by hand in another browser and never restore it. */
    const { PANE_REGISTRY } = await import('./panes');
    for (const view of Object.keys(PANE_REGISTRY)) {
      const src = readFileSync(resolve(__dirname, `../views/${view}.tsx`), 'utf-8');
      expect(`${view} has TopBar panes prop: ${src.includes(`panes="${view}"`)}`).toBe(`${view} has TopBar panes prop: true`);
    }
  });

  it('offers no tabpanel, because its tab would point at nothing', async () => {
    /* The Run log is a role=tabpanel inside a role=tablist. Hiding the panel
       alone breaks aria-controls, so it stays out until the strip itself can
       consult the registry. */
    const { PANE_REGISTRY } = await import('./panes');
    expect(PANE_REGISTRY.authoring?.some((p) => p.id.includes('log'))).toBe(false);
  });
});

describe('the stored layout', () => {
  const mem = new Map<string, string>();
  const stub = {
    getItem: (k: string) => (mem.has(k) ? mem.get(k)! : null),
    setItem: (k: string, v: string) => { mem.set(k, String(v)); },
    removeItem: (k: string) => { mem.delete(k); },
    clear: () => mem.clear(),
    key: (i: number) => [...mem.keys()][i] ?? null,
    get length() { return mem.size; },
  };

  beforeEach(async () => {
    mem.clear();
    (globalThis as { localStorage?: unknown }).localStorage = stub;
  });

  it('round-trips a pane through hidden and back to shown', async () => {
    const { setPaneState, peekLayout } = await import('./panes');
    setPaneState('authoring.pills', 'hidden');
    expect(peekLayout().authoring?.hidden).toContain('authoring.pills');
    expect(mem.get('aria.panes.v1')).toContain('authoring.pills');

    setPaneState('authoring.pills', 'shown');
    expect(peekLayout().authoring?.hidden ?? []).not.toContain('authoring.pills');
  });

  it('keeps hide and collapse records separate so a collapse is not a delete', async () => {
    const { setPaneState, peekLayout } = await import('./panes');
    setPaneState('authoring.discussRail', 'collapsed');
    const st = peekLayout().authoring;
    expect(st?.collapsed).toContain('authoring.discussRail');
    expect(st?.hidden ?? []).not.toContain('authoring.discussRail');
  });

  it('clears a view without touching the others', async () => {
    const { setPaneState, resetView, peekLayout } = await import('./panes');
    setPaneState('authoring.pills', 'hidden');
    setPaneState('research.files', 'hidden');
    resetView('authoring');
    const st = peekLayout();
    expect(st.authoring).toBeUndefined();
    expect(st.research?.hidden).toContain('research.files');
  });

  it('survives a disabled localStorage without throwing', async () => {
    const { setPaneState } = await import('./panes');
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem() { throw new Error('private mode'); },
      setItem() { throw new Error('private mode'); },
    };
    expect(() => setPaneState('authoring.pills', 'hidden')).not.toThrow();
  });
});

describe('the Discuss exit', () => {
  it('is not inside the pane the user can fold away', () => {
    /* It used to live in the Documents rail. Collapsing that rail then removed
       the only way out of Discuss — an exit that disappears with the thing it
       exits from. It sits in the tab strip now, which is never hideable. */
    const rail = AUTHORING.match(/<Pane id="authoring\.discussRail">[\s\S]*?<\/Pane>/);
    expect(rail).not.toBeNull();
    expect(rail![0]).not.toMatch(/Back to the run/);
    expect((AUTHORING.match(/Back to the run/g) || []).length).toBe(1);
  });

  it('mounts the restore control from the top bar, not from a Pane', () => {
    expect(UI).toMatch(/\{panes && <LayoutMenu view=\{panes\} \/>\}/);
  });
});
