import { Suspense, lazy, useEffect, useState } from 'react';
import { BrowserRouter, useLocation } from 'react-router-dom';
import { AlertTriangle } from 'lucide-react';
import { STALE_TOKEN_EVENT } from './api';
import ErrorBoundary from './components/ErrorBoundary';
import TitleBar from './components/TitleBar';
import CommandPalette from './components/CommandPalette';
import { GatewayDownBanner, HealthProvider } from './components/ui';
import { Toaster } from 'sonner';
import { KeepAliveHost } from './components/KeepAlive';
import { copyCodeFromClick } from './components/markdown';
import { prefetchAll } from './lib/prefetch';

/* Route-split: each view (and the heavy @xyflow/react DAG code pulled in
   by Runs/Research) loads on demand instead of bloating first paint. */
const Chat = lazy(() => import('./views/Chat'));
const Runs = lazy(() => import('./views/Runs'));
const Memory = lazy(() => import('./views/Memory'));
const Documents = lazy(() => import('./views/Documents'));
const Research = lazy(() => import('./views/Research'));
const Scheduler = lazy(() => import('./views/Scheduler'));
const Skills = lazy(() => import('./views/Skills'));
const Apps = lazy(() => import('./views/Apps'));
const Ledger = lazy(() => import('./views/Ledger'));
const Mission = lazy(() => import('./views/Mission'));
const Settings = lazy(() => import('./views/Settings'));
const Code = lazy(() => import('./views/Code'));
const Authoring = lazy(() => import('./views/Authoring'));
const Parts = lazy(() => import('./views/Parts'));
const Computer = lazy(() => import('./views/Computer'));

/* Warm the split chunks shortly after first paint so that only the very
   first section can suspend; afterwards every hop is served from the module
   cache and renders straight into place. The route→chunk map lives in
   lib/prefetch so the rail can also warm a section on hover — that is what
   keeps a hop to an unvisited section instant instead of showing the full
   shell-skeleton fallback. */
function Preloader() {
  const loc = useLocation();
  useEffect(() => {
    const idle = (globalThis as { requestIdleCallback?: (cb: () => void, opts?: { timeout?: number }) => number }).requestIdleCallback;
    const cancel = idle
      ? (globalThis as unknown as { cancelIdleCallback?: (id: number) => void }).cancelIdleCallback
      : undefined;
    if (idle) {
      /* `timeout` bounds the wait: without it the browser is free to defer
         these imports indefinitely on a busy page, which is exactly the case
         (a live stream) where a later hop then still suspends. */
      const id = idle(() => prefetchAll(), { timeout: 2000 });
      return () => cancel?.(id);
    }
    const t = window.setTimeout(prefetchAll, 200);
    return () => window.clearTimeout(t);
  }, []);
  /* Touching loc keeps this mounted across route changes. */
  void loc;
  return null;
}

/* A route-scoped skeleton that mirrors the real shell geometry — nav strip,
   header row, then content. The previous fallback drew two short bars
   centred on a full-bleed black panel, which read as a blank frame rather
   than a loading state on every first visit to a section. */
function Fallback() {
  return (
    <div className="flex h-full flex-col bg-[#090909] lg:flex-row">
      <aside
        aria-hidden
        className="flex w-full flex-none flex-row gap-1 overflow-hidden border-b border-glass-border bg-surface-0 px-2 py-1.5 lg:h-full lg:w-[200px] lg:flex-col lg:items-stretch lg:border-b-0 lg:border-r lg:px-0 lg:py-0"
      >
        <div className="hidden flex-none gap-2.5 px-4 pb-3 pt-4 lg:flex">
          <div className="h-7 w-7 flex-none animate-pulse rounded-lg bg-accent/15" />
          <div className="mt-1 h-4 w-12 animate-pulse rounded bg-white/[0.06]" />
        </div>
        <div className="flex min-h-0 flex-1 gap-1 overflow-hidden px-0.5 lg:flex-col lg:px-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="h-9 flex-none animate-pulse rounded-lg bg-white/[0.04]" />
          ))}
        </div>
      </aside>
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex flex-none items-center gap-3 border-b border-glass-border px-4 py-3">
          <div className="h-4 w-24 animate-pulse rounded bg-white/[0.06]" />
          <div className="ml-auto h-7 w-40 animate-pulse rounded-lg bg-white/[0.04]" />
        </div>
        <div className="min-h-0 flex-1 overflow-hidden p-3">
          <div className="stagger">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="mb-2 h-11 animate-pulse rounded-lg bg-white/[0.04]" />
            ))}
          </div>
        </div>
      </main>
    </div>
  );
}

/* The agent's token is regenerated on EVERY launch and injected into this
   document as a <meta> tag, read once at module load. A tab left open while the
   agent restarts therefore holds a dead credential: every route 403s with
   "missing or invalid X-Aria-Token" and the console is a dead end — no retry,
   no login, nothing to click.

   Reloading is the actual fix, because it re-fetches the document and the
   agent re-injects the current token into it. The secret is deliberately not
   persisted to storage, so this cannot be fixed by re-reading a cached copy;
   only a fresh document gets a fresh token. */
function StaleTokenNotice() {
  const [stale, setStale] = useState(false);
  useEffect(() => {
    const on = () => setStale(true);
    window.addEventListener(STALE_TOKEN_EVENT, on);
    return () => window.removeEventListener(STALE_TOKEN_EVENT, on);
  }, []);
  if (!stale) return null;
  return (
    <div role="alert"
      className="flex flex-wrap items-center gap-3 border-b border-amber-400/40 bg-amber-400/10 px-4 py-2 text-[12.5px] text-amber-100">
      <AlertTriangle size={14} className="flex-none" />
      <span className="min-w-0 flex-1">
        The agent restarted, so its access token changed. This tab is holding
        the old one — reload to continue. Nothing you were working on is lost.
      </span>
      <button type="button"
        onClick={() => window.location.reload()}
        className="flex-none rounded-md border border-amber-400/50 px-2.5 py-1 text-[11.5px] font-semibold hover:bg-amber-400/20">
        Reload now
      </button>
    </div>
  );
}

/* Every section, in one registry. Order is only for stable DOM ordering; the
   `id` is the route path and is what the keep-alive host keys on. Kept beside
   the CHUNKS preloader list above so the two cannot drift. */
export const SECTIONS = [
  { id: '/research', render: () => <Research /> },
  { id: '/console', render: () => <Chat /> },
  { id: '/runs', render: () => <Runs /> },
  { id: '/memory', render: () => <Memory /> },
  { id: '/documents', render: () => <Documents /> },
  { id: '/scheduler', render: () => <Scheduler /> },
  { id: '/skills', render: () => <Skills /> },
  { id: '/apps', render: () => <Apps /> },
  { id: '/ledger', render: () => <Ledger /> },
  { id: '/mission', render: () => <Mission /> },
  { id: '/settings', render: () => <Settings /> },
  { id: '/code', render: () => <Code /> },
  { id: '/authoring', render: () => <Authoring /> },
  { id: '/parts', render: () => <Parts /> },
  { id: '/computer', render: () => <Computer /> },
] as const;

/** Map a raw pathname onto a registered section id. */
function sectionFor(pathname: string): string {
  const known = SECTIONS.some((s) => s.id === pathname);
  if (known) return pathname;
  // Strip a trailing slash so /runs/ and /runs are the same section; keeping
  // them distinct would mount the view twice.
  const trimmed = pathname.replace(/\/+$/, '') || '/';
  if (SECTIONS.some((s) => s.id === trimmed)) return trimmed;
  return '/research';
}

/* The part that must live INSIDE <BrowserRouter>.

   `useLocation()` reads the Router context, and `App` is the component that
   RETURNS the router - so calling it in `App`'s own body reads a context that
   does not exist yet and throws "useLocation() may be used only in the context
   of a <Router> component", blanking the whole console. The hook therefore
   belongs in a child that the router already wraps. */
function RouterShell() {
  const { pathname } = useLocation();
  /* One delegated listener serves every renderMarkdown surface - chat, run
     reports, the inspector and authoring. Attaching it only to the chat
     containers left the same Copy button inert everywhere else. */
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const target = e.target;
      if (target instanceof Element
          && target.closest('[data-copy-code]')) {
        void copyCodeFromClick(e);
      }
    };
    document.addEventListener('click', onClick);
    return () => document.removeEventListener('click', onClick);
  }, []);
  return (
    <ErrorBoundary>
      <HealthProvider>
      <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <Preloader />
      <StaleTokenNotice />
      {/* One gateway-down strip for the whole shell: the Rail's dot is desktop
          only, so a failed gateway was invisible on a phone and on every page
          that never checked health. */}
      <GatewayDownBanner />
      {/* The Electron shell hides the OS title bar (`titleBarStyle:
          'hidden'`), which would otherwise leave the window with no way to
          be moved or closed. This draws the replacement. It renders nothing
          outside Electron — see TitleBar — so the browser build is
          unaffected. */}
      <TitleBar />
      {/* Global command palette (Ctrl/Cmd+K) and the toast surface. Both live
          inside the router so the palette can navigate and toasts survive a
          section switch. */}
      <CommandPalette />
      <Toaster
        position="bottom-right"
        theme="dark"
        closeButton
        offset={16}
        toastOptions={{ duration: 4000 }}
      />
      <Suspense fallback={<Fallback />}>
        {/* The sections are the only flexible child: the title bar and status
            banners keep their natural height, and the active view scrolls
            inside this box instead of growing the document. */}
        <main className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
        {/* Keep-alive routing.
            `<Routes>` unmounts the outgoing view on every navigation, so a
            section switch discarded that view's state, in-flight fetches and
            scroll position and refetched from scratch - the chunks were
            already cached (see CHUNKS), the *component* was not. This mounts
            each section once and hides the inactive ones with CSS, and
            `lib/getCache` stops the network round-trip on top of that. */}
        <KeepAliveHost sections={SECTIONS} current={sectionFor(pathname)} />
        </main>
      </Suspense>
      </div>
      </HealthProvider>
    </ErrorBoundary>
  );
}

/* Aria console SPA — one React tree for every section. The server serves
   this same bundle at each route; the router picks the view client-side,
   so hopping sections never reloads the document. */
export default function App() {
  return (
    <BrowserRouter>
      <RouterShell />
    </BrowserRouter>
  );
}
