import { Suspense, lazy, useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import ErrorBoundary from './components/ErrorBoundary';

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

/* The split chunks, kept as thunks so we can warm them before they are
   needed. Suspense otherwise blanks the whole shell on every hop to a
   section that hasn't been visited yet, because the fallback replaces the
   rail and header too. */
const CHUNKS = [
  () => import('./views/Chat'),
  () => import('./views/Runs'),
  () => import('./views/Memory'),
  () => import('./views/Documents'),
  () => import('./views/Research'),
  () => import('./views/Scheduler'),
  () => import('./views/Skills'),
  () => import('./views/Apps'),
  () => import('./views/Ledger'),
  () => import('./views/Mission'),
  () => import('./views/Settings'),
  () => import('./views/Code'),
];

function preloadAll() {
  for (const load of CHUNKS) void load().catch(() => undefined);
}

/* Warm the split chunks as soon as the browser is idle so that only the
   very first paint can suspend; afterwards every hop is served from the
   module cache and renders straight into place. */
function Preloader() {
  const loc = useLocation();
  useEffect(() => {
    const idle = (globalThis as { requestIdleCallback?: (cb: () => void) => number }).requestIdleCallback;
    const cancel = idle
      ? (globalThis as unknown as { cancelIdleCallback?: (id: number) => void }).cancelIdleCallback
      : undefined;
    if (idle) {
      const id = idle(() => preloadAll());
      return () => cancel?.(id);
    }
    const t = window.setTimeout(preloadAll, 200);
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

/* Aria console SPA — one React tree for every section. The server serves
   this same bundle at each route; the router picks the view client-side,
   so hopping sections never reloads the document. */
export default function App() {
  return (
    <BrowserRouter>
      <ErrorBoundary>
        <Preloader />
        <Suspense fallback={<Fallback />}>
          <Routes>
            <Route path="/" element={<Navigate to="/research" replace />} />
            <Route path="/console" element={<Chat />} />
            <Route path="/runs" element={<Runs />} />
            <Route path="/memory" element={<Memory />} />
            <Route path="/documents" element={<Documents />} />
            <Route path="/research" element={<Research />} />
            <Route path="/scheduler" element={<Scheduler />} />
            <Route path="/skills" element={<Skills />} />
            <Route path="/apps" element={<Apps />} />
            <Route path="/ledger" element={<Ledger />} />
            <Route path="/mission" element={<Mission />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/code" element={<Code />} />
            <Route path="/authoring" element={<Authoring />} />
            <Route path="*" element={<Navigate to="/research" replace />} />
          </Routes>
        </Suspense>
      </ErrorBoundary>
    </BrowserRouter>
  );
}
