/* Route → chunk loader, in one place so both the idle preloader (App) and
   the rail's hover prefetch can warm a section before it is navigated to.

   Every view is code-split (see App.tsx). A first visit to a section whose
   chunk is not yet in the module cache suspends, and the Suspense fallback
   replaces the WHOLE shell — rail, header and all — with a skeleton. That is
   the visible "the app is reloading" jolt on every hop to a section that has
   not been visited yet. Warming the chunk while the pointer is already on the
   nav item (or, worst case, within the idle timeout) makes the hop itself a
   pure CSS show/hide from the keep-alive host: instant, no fallback.
   Kept beside the loaders it mirrors so the two cannot drift. */
const LOADERS: Record<string, () => Promise<unknown>> = {
  '/console': () => import('../views/Chat'),
  '/runs': () => import('../views/Runs'),
  '/memory': () => import('../views/Memory'),
  '/documents': () => import('../views/Documents'),
  '/research': () => import('../views/Research'),
  '/scheduler': () => import('../views/Scheduler'),
  '/skills': () => import('../views/Skills'),
  '/apps': () => import('../views/Apps'),
  '/ledger': () => import('../views/Ledger'),
  '/mission': () => import('../views/Mission'),
  '/settings': () => import('../views/Settings'),
  '/code': () => import('../views/Code'),
  '/authoring': () => import('../views/Authoring'),
  '/parts': () => import('../views/Parts'),
};

/** Warm one section's chunk. A miss (unknown id) is not an error. */
export function prefetchSection(id: string): void {
  void LOADERS[id]?.().catch(() => undefined);
}

/**
 * Chunks that must NOT be warmed speculatively.
 *
 * `prefetchAll` runs on idle, which sounds harmless - it is not. The Code view
 * transitively imports Monaco, so warming it on idle made EVERY page load
 * download the editor: measured, opening the Ledger fetched 5.34 MB, of which
 * 4.42 MB was `editor.api` (2.6 MB) plus the Code chunk (1.7 MB) - for a page
 * that renders a table and never shows an editor. Every other section is 1-36
 * KB, so this one entry was the entire cost of the prefetcher.
 *
 * These still warm on INTENT, which is the whole point of the hover handler:
 * the pointer is already on "Code" when the click lands, so the chunk is
 * usually there before the navigation does. The prefetcher's job is to make a
 * hop free, not to make a page load expensive.
 */
const NEVER_PRELOADED = new Set<string>(['/code']);

/** Warm every section's chunk EXCEPT the ones that cost more than they save. */
export function prefetchAll(): void {
  for (const [id, load] of Object.entries(LOADERS)) {
    if (NEVER_PRELOADED.has(id)) continue;
    void load().catch(() => undefined);
  }
}
