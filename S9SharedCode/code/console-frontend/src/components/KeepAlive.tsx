import {
  createContext, useContext, useEffect, useRef, type ReactNode,
} from 'react';

const SectionActive = createContext(true);

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
 * - `inert`-style pointer blocking is done with `hidden`, which also stops
 *   focus and clicks reaching an inactive section.
 * - `keepAlive` is memoised on the render path, not on mount, so a view that
 *   changes its own props while hidden still updates.
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
  if (current) mounted.current.add(current);

  return (
    <>
      {sections.map(({ id, render }) => {
        const seen = mounted.current.has(id);
        const isActive = id === current;
        // `hidden` keeps the subtree mounted but out of the a11y tree and
        // non-interactive. `display:none` via the attribute also stops the
        // browser from painting it.
        return (
          <SectionActive.Provider key={id} value={isActive}>
            <div
              hidden={!isActive}
              style={isActive ? undefined : { display: 'none' }}
              aria-hidden={isActive ? undefined : true}
              className="h-full min-h-0"
            >
              {seen ? render() : null}
            </div>
          </SectionActive.Provider>
        );
      })}
    </>
  );
}

/**
 * Fire a refresh callback when a section becomes active again.
 *
 * Views fetch on mount. Once kept alive they will not remount, so anything
 * that must stay current on revisit (polling, re-reading a session) needs to
 * be driven by visibility instead. Returns whether it fired, for tests.
 */
/** Fetch now and on every interval only while this section is visible.
 *  Hidden keep-alive views stop polling; coming back triggers an immediate
 *  refresh, so the data is never stale (H26). `fn` must be stable. */
export function useActivePoll(fn: () => void, ms: number) {
  const active = useSectionActive();
  useEffect(() => {
    if (!active) return;
    fn();
    const t = setInterval(fn, ms);
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