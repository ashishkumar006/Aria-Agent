import {
  createContext, memo, Suspense, useContext, useEffect, useRef,
  type ReactNode,
} from 'react';

const SectionActive = createContext(true);

/** A first visit to a cold route chunk suspends. The boundary lives HERE,
 *  around one section, rather than in App around the whole keep-alive host:
 *  a shell-level fallback unmounts every other section, which threw away the
 *  state keep-alive exists to protect (and made every list reload after a hop
 *  to a section whose chunk had not been fetched yet - Code is never
 *  prefetched). */
function SectionFallback() {
  return (
    <div className="flex h-full items-center justify-center p-6" role="status" aria-label="Loading section">
      <div className="h-4 w-32 animate-pulse rounded bg-white/[0.06]" />
    </div>
  );
}

/** True while this section is the visible one. Pollers use it to stop
 *  fetching from hidden, kept-alive views (H26). */
/** The id SkipLink targets. Only the visible section owns it, so
 *  keep-alive views never duplicate `id="main"` (H14). */
export function useMainId(): string | undefined {
  return useSectionActive() ? 'main' : undefined;
}

export function useSectionActive(): boolean {
  return useContext(SectionActive);
}

/**
 * Keep a section mounted after its first visit.
 *
 * `<Routes>` unmounts the outgoing view and mounts the incoming one on every
 * navigation, so each hop to a section threw away everything the view had:
 * its `useState`, its in-flight fetches, its scroll position and its expanded
 * rows. The result was a visible reload on every switch even though the
 * JavaScript chunk was already in the module cache (App.tsx's `CHUNKS`
 * preloader handles the *download*; this handles the *component*).
 *
 * So render every visited view once and keep it in the tree, hiding the
 * inactive ones with CSS instead of removing them.
 *
 * - `active` toggles visibility and `aria-hidden`; it does NOT unmount.
 * - `hidden` keeps an idle inactive section out of the layout, the a11y tree
 *   and the tab order. A switch has no animation at all, so the swap happens
 *   in one commit - the way an editor swaps tabs.
 * - The rendered element is created once and reused. Rebuilding `<View />`
 *   on every host render forced React to reconcile all 15 kept-alive views on
 *   every navigation; a stable element lets it skip the unchanged subtrees.
 */
export type KeepAliveProps = {
  /** Stable identity for this section - the route path. */
  id: string;
  active: boolean;
  children: ReactNode;
};

/**
 * Tracks which ids have been mounted. A section is mounted lazily on first
 * visit (so a cold start does not pay for 13 views) and then kept forever.
 */
export function KeepAliveHost({
  sections,
  current,
}: {
  /** Ordered list of every known section; drives stable ordering. */
  sections: ReadonlyArray<{ id: string; render: () => ReactNode }>;
  current: string | null;
}) {
  const mounted = useRef<Set<string>>(new Set());
  /* One element per section, created once. `render()` returns a fresh
   * `<View />` on every parent render, so React re-rendered all 15 kept-alive
   * views (Chat and the Monaco-backed Code among them) on every single
   * navigation. A stable element identity lets React bail out of every
   * subtree whose props did not change. */
  const nodes = useRef<Map<string, ReactNode>>(new Map());

  if (current) mounted.current.add(current);

  return (
    <>
      {sections.map(({ id, render }) => {
        const seen = mounted.current.has(id);
        if (seen && !nodes.current.has(id)) {
          nodes.current.set(id, (
            <Suspense fallback={<SectionFallback />}>{render()}</Suspense>
          ));
        }
        return (
          <SectionShell key={id} active={id === current}>
            {nodes.current.get(id) ?? null}
          </SectionShell>
        );
      })}
    </>
  );
}

/**
 * The box every section lives in. Split out so its visibility cannot be reset
 * by a host re-render.
 *
 * A section switch is INSTANT - the way an editor swaps tabs. The outgoing
 * view is `display: none`d and the incoming one shown in the same commit, with
 * no animation on either side, so there is no frame where the content area is
 * empty and no frame where two pages share pixels.
 *
 * Every animated switch was tried and rejected:
 *   - a fade from `opacity: 0` left the content area empty for ~80-220ms (a
 *     blink);
 *   - a crossfade blended two text-dense pages, so the Runs list stayed
 *     legible through the Chat transcript;
 *   - a left-to-right edge wipe fixed the blend but read as a sweep;
 *   - a fade through black dimmed the whole content area every time.
 * VS Code's editor swap is the behaviour being matched: the new page is simply
 * there, at full opacity, in the frame after the click.
 *
 * The outgoing view is only hidden, never unmounted, so its state, scroll
 * position and data survive the switch. `aria-hidden` and `inert` keep the
 * hidden copies out of the a11y tree and the tab order.
 */
const SectionShell = memo(function SectionShell({
  active,
  children,
}: {
  active: boolean;
  children: ReactNode;
}) {
  return (
    <SectionActive.Provider value={active}>
      <div
        hidden={!active}
        style={active ? undefined : { display: 'none' }}
        aria-hidden={active ? undefined : true}
        inert={active ? undefined : true}
        className="h-full min-h-0"
      >
        {children}
      </div>
    </SectionActive.Provider>
  );
});

/** Fetch now (if the view's own cadence says it is due) and then on every
 *  interval while this section is visible.
 *
 *  Hidden keep-alive views stop polling, and coming back used to fire `fn()`
 *  unconditionally - so every hop to Runs/Apps/Settings re-read its whole
 *  list even when the user had left two seconds earlier. Coming back inside
 *  one cadence window now reuses what the view already has; staying away for
 *  longer than `ms` still refreshes immediately, so data is never stale. */
export function useActivePoll(fn: () => void, ms: number) {
  const active = useSectionActive();
  const last = useRef(0);
  useEffect(() => {
    if (!active) return;
    if (Date.now() - last.current >= ms) {
      last.current = Date.now();
      fn();
    }
    const t = setInterval(() => {
      last.current = Date.now();
      fn();
    }, ms);
    return () => clearInterval(t);
  }, [active, fn, ms]);
}

export function useRefreshOnActive(active: boolean, fn: () => void) {
  const was = useRef(active);
  useEffect(() => {
    if (active && !was.current) fn();
    was.current = active;
  }, [active, fn]);
}