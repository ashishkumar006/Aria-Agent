/* Shared shell bits: rail, topbar, stats, cards, empty states, hooks. */
import { memo, useEffect, useState, type ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';
import {
  Zap, MessageSquare, Play, Database, Search, CalendarDays, Package,
  LayoutGrid, Receipt, Terminal, Settings, SearchX, FileText, Code2, FileOutput,
} from 'lucide-react';
import { api, CONF } from '../api';

export function useHealth() {
  const [up, setUp] = useState(false);
  useEffect(() => {
    let live = true;
    const tick = async () => {
      try {
        const h = await api.health();
        if (live) setUp(h.gateway_up !== false);
      } catch {
        if (live) setUp(false);
      }
    };
    tick();
    const t = setInterval(tick, CONF.healthMs);
    return () => { live = false; clearInterval(t); };
  }, []);
  return up;
}

const RAIL: { group: string; items: { to: string; label: string; icon: ReactNode }[] }[] = [
  { group: 'Core', items: [
    { to: '/console', label: 'Chat', icon: <MessageSquare size={16} /> },
    { to: '/runs', label: 'Runs', icon: <Play size={16} /> },
    { to: '/memory', label: 'Memory', icon: <Database size={16} /> },
    { to: '/documents', label: 'Documents', icon: <FileText size={16} /> },
    { to: '/research', label: 'Research', icon: <Search size={16} /> },
  ]},
  { group: 'Automate', items: [
    { to: '/scheduler', label: 'Scheduler', icon: <CalendarDays size={16} /> },
    { to: '/skills', label: 'Skills', icon: <Package size={16} /> },
    { to: '/code', label: 'Code', icon: <Code2 size={16} /> },
    { to: '/authoring', label: 'Authoring', icon: <FileOutput size={16} /> },
  ]},
  { group: 'Insight', items: [
    { to: '/apps', label: 'Apps', icon: <LayoutGrid size={16} /> },
    { to: '/ledger', label: 'Ledger', icon: <Receipt size={16} /> },
    { to: '/mission', label: 'Console', icon: <Terminal size={16} /> },
  ]},
  { group: 'System', items: [
    { to: '/settings', label: 'Settings', icon: <Settings size={16} /> },
  ]},
];

export const Rail = memo(function Rail() {
  const loc = useLocation();
  const up = useHealth();
  return (
    /* Below lg the rail is a horizontal, scrollable icon strip pinned to the
       top of the stack. It used to stay a fixed 52px *column* beside a fixed
       248px list pane, so on a 390px viewport the content column collapsed to
       ~90px and every page scrolled sideways with one letter per line. */
    <aside className="flex w-full flex-none flex-row items-center gap-1 overflow-x-auto border-b border-glass-border bg-surface-0 px-2 py-1.5 lg:h-full lg:w-[200px] lg:flex-col lg:items-stretch lg:overflow-x-hidden lg:overflow-y-auto lg:border-b-0 lg:border-r lg:px-0 lg:py-0">
      {/* Brand — the topbar already names the product, so the strip skips it. */}
      <div className="hidden flex-none items-center gap-2.5 px-4 pb-3 pt-4 lg:flex">
        <div className="flex h-7 w-7 flex-none items-center justify-center rounded-lg bg-accent/15" title="Aria">
          <Zap size={14} className="text-accent" />
        </div>
        <span className="text-[15px] font-bold tracking-tight text-zinc-100">Aria</span>
      </div>

      {/* Nav groups (icon-only below lg).

          `min-w-0` is load-bearing: as a flex item the nav defaults to
          `min-width: auto`, so it refuses to shrink below its content and
          grows the whole document instead of scrolling. That stayed hidden
          while the strip fitted a 390px phone; adding a 12th item pushed it
          over and every page became 594px wide with one icon per line. */}
      <nav className="flex min-h-0 min-w-0 flex-1 items-center gap-1 overflow-x-auto px-0.5 lg:flex-col lg:items-stretch lg:overflow-x-visible lg:px-2 lg:pb-3" aria-label="Primary">
        {RAIL.map((g, gi) => (
          <div key={g.group} className={gi > 0 ? 'ml-1 lg:ml-0 lg:mt-3' : ''}>
            <div className="mb-1.5 hidden px-2.5 text-[10px] font-bold tracking-[0.16em] text-zinc-muted uppercase lg:block">
              {g.group}
            </div>
            <div className="flex gap-0.5 lg:block lg:space-y-0.5">
              {g.items.map((it) => {
                const on = loc.pathname === it.to || (it.to === '/research' && loc.pathname === '/');
                return (
                  <Link
                    key={it.to}
                    to={it.to}
                    title={it.label}
                    aria-label={it.label}
                    aria-current={on ? 'page' : undefined}
                    className={`group relative flex flex-none items-center justify-center gap-2.5 rounded-lg px-3 py-3 text-[13px] transition-all duration-[250ms] ease-out lg:flex-initial lg:justify-start lg:px-2.5 lg:py-[6px] ${
                      on
                        ? 'bg-accent/10 font-semibold text-zinc-100'
                        : 'text-zinc-400 hover:bg-glass-hover hover:text-zinc-200'
                    }`}
                  >
                    {/* Active indicator bar */}
                    {on && (
                      <span className="absolute left-0 top-1 bottom-1 w-[3px] rounded-full bg-accent shadow-[0_0_10px_rgba(139,124,246,0.5)]" />
                    )}
                    <span className={`transition-colors duration-[250ms] ${on ? 'text-accent' : 'text-zinc-muted group-hover:text-zinc-400'}`}>
                      {it.icon}
                    </span>
                    <span className="hidden lg:inline">{it.label}</span>
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      {/* Status footer — every view's topbar already carries a live status
          pill, so the strip drops it rather than burning a second row. */}
      <div className="hidden flex-none items-center gap-2.5 border-t border-glass-border px-4 py-3 lg:flex">
        <span className={`status-dot flex-none ${up ? 'status-dot-ok' : 'status-dot-bad'}`} role="status" aria-label={up ? 'agent and gateway connected' : 'connecting'} />
        <span className="text-[11px] text-zinc-muted">{up ? 'agent + gateway' : 'connecting…'}</span>
      </div>
    </aside>
  );
});

export const TopBar = memo(function TopBar({ crumb, children }: { crumb: string; children?: ReactNode }) {
  /* Every view renders a TopBar, so this is the one place that can give each
     route an identity. `document.title` was the static "Aria · Research" on
     all fifteen routes, so a screen reader, a bookmark and the browser tab
     history could not tell you where you were. */
  useEffect(() => {
    document.title = `Aria · ${crumb}`;
  }, [crumb]);
  return (
    /* The control cluster used to be a bare `flex-1` spacer plus siblings, so
       a wide pill row (status + Download + Copy) pushed the header past the
       viewport and gave the whole page a horizontal scrollbar. It wraps now
       and right-aligns itself. */
    <div className="glass sticky top-0 z-10 flex items-center gap-2 px-4 py-2.5">
      <span className="flex flex-none items-center gap-1.5 text-[13px] font-semibold">
        <Zap size={11} className="flex-none text-accent" />
        <span className="flex-none text-zinc-300">Aria</span>
        <span className="flex-none text-zinc-muted">/</span>
        <span className="max-w-[46vw] truncate text-zinc-400 sm:max-w-[280px]">{crumb}</span>
      </span>
      <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-2">{children}</div>
    </div>
  );
});

export const Stat = memo(function Stat({ k, v, s, tone, accent }: {
  k: string;
  v: string;
  s?: string;
  tone?: 'ok' | 'bad' | 'warn';
  accent?: string;
}) {
  const accentColor = tone === 'ok' ? 'var(--color-ok)' : tone === 'bad' ? 'var(--color-danger)' : tone === 'warn' ? 'var(--color-warn)' : accent || 'var(--color-accent)';
  const valueColor = tone === 'ok' ? 'text-ok' : tone === 'bad' ? 'text-danger' : tone === 'warn' ? 'text-warn' : '';

  return (
    <div className="instrument-card min-w-[116px] flex-1 p-3.5" style={{ '--accent-line': accentColor } as React.CSSProperties}>
      <div className="text-[10px] font-bold tracking-[0.14em] text-zinc-muted uppercase">{k}</div>
      <div className={`mt-1 text-xl font-bold tabular-nums ${valueColor}`}>{v}</div>
      {s && <div className="mt-0.5 truncate text-[11px] text-zinc-muted">{s}</div>}
    </div>
  );
});

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

/* First focusable element on every page: a keyboard or screen-reader
   user can jump straight to the view content instead of tabbing
   through the rail and the topbar on all twelve sections. The
   target is the <main id="main"> each view renders; focusing it
   directly keeps the SPA router out of the hash. */
export function SkipLink() {
  return (
    <a
      href="#main"
      onClick={(e) => {
        e.preventDefault();
        document.getElementById('main')?.focus();
      }}
      className="sr-only z-[100] focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:rounded-md focus:bg-accent focus:px-3 focus:py-2 focus:text-[12px] focus:font-bold focus:text-[#0b0b0e] focus:outline-none"
    >
      Skip to main content
    </a>
  );
}
