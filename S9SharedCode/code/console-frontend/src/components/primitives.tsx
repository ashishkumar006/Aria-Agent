/* Presentational primitives shared by the console AND the studio.
 *
 * These live apart from `ui.tsx` on purpose: `ui.tsx` also exports the Rail,
 * and `/studio` is an isolated route that must never import the rail module
 * (tests/test_studio_routing.py pins that). Sharing the *components* without
 * sharing the *rail* means one implementation of a pill / empty state /
 * skeleton, reachable from both surfaces, with no path by which the studio
 * chunk pulls the navigation in. `ui.tsx` re-exports these so existing console
 * imports keep working. */
import { memo, type ReactNode } from 'react';
import { SearchX } from 'lucide-react';

export const Empty = memo(function Empty({ icon, title, sub }: { icon?: ReactNode; title: string; sub?: string }) {
  return (
    <div className="animate-fade-in-up px-5 py-14 text-center">
      <div className="mb-3 flex justify-center text-zinc-muted animate-pulse-glow">
        {icon || <SearchX size={32} />}
      </div>
      <div className="text-sm font-bold text-zinc-300">{title}</div>
      {sub && <div className="mx-auto mt-1.5 max-w-[360px] text-xs leading-relaxed text-zinc-muted">{sub}</div>}
    </div>
  );
});

export const Skel = memo(function Skel({ n = 3 }: { n?: number }) {
  return (
    <div className="space-y-2 p-2.5 stagger">
      {Array.from({ length: n }).map((_, i) => (
        <div key={i} className="instrument-card p-3">
          <div className="shimmer h-2.5 w-4/5 rounded-md" />
          <div className="shimmer mt-2 h-2.5 w-3/5 rounded-md" />
        </div>
      ))}
    </div>
  );
});

export const Pill = memo(function Pill({ tone, children }: { tone: 'ok' | 'err' | 'warn' | 'info' | 'muted'; children: ReactNode }) {
  const map = {
    ok: 'border-ok/30 bg-ok-dim text-ok',
    err: 'border-danger/30 bg-danger-dim text-danger',
    warn: 'border-warn/30 bg-warn-dim text-warn',
    info: 'border-info/30 bg-info-dim text-info',
    muted: 'border-glass-border bg-glass text-zinc-400',
  } as const;

  const dotMap = {
    ok: 'bg-ok',
    err: 'bg-danger',
    warn: 'bg-warn',
    info: 'bg-info',
    muted: 'bg-zinc-500',
  } as const;

  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-1 text-[10.5px] font-bold ${map[tone]}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${dotMap[tone]}`} />
      {children}
    </span>
  );
});