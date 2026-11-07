/* Shared shell bits: rail, topbar, stats, cards, empty states, hooks. */
import {
  createContext, memo, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef,
  useState, useSyncExternalStore, type ReactNode,
} from 'react';
import { Link, useLocation } from 'react-router-dom';
import { motion } from 'motion/react';
import {
  Zap, MessageSquare, Play, Database, Search, CalendarDays, Package,
  LayoutGrid, Receipt, Terminal, Settings, SearchX, FileText, Code2, FileOutput, Braces,
  TriangleAlert, Boxes, Monitor, PanelLeftClose, PanelLeftOpen,
} from 'lucide-react';
import { openCommandPalette } from '../lib/palette';
import { LayoutMenu } from '../lib/panes';
import { prefetchSection } from '../lib/prefetch';
import { useSectionActive } from './KeepAlive';

/* One health poll for the whole shell.
 *
 * The gateway being down is not a per-view accident — it breaks every paid
 * action on every page — so the state lives in one provider and the shell
 * renders a single banner from it. Before this, the Rail, Chat and Research
 * each ran their own 15 s poll, and no page outside those three said anything
 * at all when the gateway died. */
type HealthState = { up: boolean; checked: boolean; refresh: (fresh?: boolean) => Promise<void> };

const HealthCtx = createContext<HealthState | null>(null);

/** Consecutive failed polls before the console says the gateway is down.
 *
 *  One blip used to be enough, and the gateway genuinely blips: its /health
 *  shares one event loop with the ledger's sqlite writes, so a spike over the
 *  probe budget produced a false "unreachable" that cleared on the next poll
 *  fifteen seconds later. The user saw a wall of amber text for a healthy
 *  system. A real outage is not fixed by waiting one more poll interval, so the
 *  cost of two strikes is small and the cost of one is not.
 *
 *  A manual Retry (fresh=true) is decisive on a single failure — someone who
 *  clicked the button asked us to check now, not to average. */
const HEALTH_STREAK = 2;

export function HealthProvider({ children }: { children: ReactNode }) {
  // Starts optimistic and unchecked: the banner needs `checked`, so nothing
  // can be claimed about the gateway before the first real verdict.
  const [state, setState] = useState<{ up: boolean; checked: boolean }>(
    { up: true, checked: false });
  const misses = useRef(0);
  const tick = useCallback(async (fresh = false) => {
    let ok = false;
    try {
      const h = await api.health(fresh);
      ok = h.gateway_up !== false;
    } catch {
      ok = false;
    }
    misses.current = ok ? 0 : misses.current + 1;
    if (ok) setState({ up: true, checked: true });
    else if (fresh || misses.current >= HEALTH_STREAK) setState({ up: false, checked: true });
  }, []);
  useEffect(() => {
    void tick();
    const t = setInterval(() => { void tick(); }, CONF.healthMs);
    return () => clearInterval(t);
  }, [tick]);
  const value = useMemo(() => ({ ...state, refresh: tick }), [state, tick]);
  return <HealthCtx.Provider value={value}>{children}</HealthCtx.Provider>;
}

/** True while the gateway is reachable. Defaults to true so a view rendered
 *  outside the provider never shows a false alarm. */
export function useHealth(): boolean {
  return useContext(HealthCtx)?.up ?? true;
}

/** The gateway-down strip. Persistent rather than a toast: every paid action
 *  on every page fails until it clears, and it must be visible on mobile too,
 *  which the rail's desktop-only dot never was. */
export function GatewayDownBanner() {
  const h = useContext(HealthCtx);
  const [busy, setBusy] = useState(false);
  if (!h?.checked || h.up) return null;
  return (
    <div role="alert"
      className="flex flex-wrap items-center gap-3 border-b border-amber-400/40 bg-amber-400/10 px-4 py-2 text-[12.5px] text-amber-100">
      <TriangleAlert size={14} className="flex-none" />
      <span className="min-w-0 flex-1">
        The LLM gateway on :{CONF.gatewayPort} is unreachable, so anything that
        needs a model will fail. The agent is still up.
      </span>
      <button type="button" disabled={busy}
        onClick={async () => {
          setBusy(true);
          /* `true` = ask the server to probe NOW. The plain poll may answer
             from its 8s cache; a Retry that re-read the cached "up" cleared
             this banner while the gateway was still dead. */
          try { await h.refresh(true); } finally { setBusy(false); }
        }}
        className="flex-none rounded-md border border-amber-400/50 px-2.5 py-1 text-[11.5px] font-semibold hover:bg-amber-400/20 disabled:opacity-60">
        {busy ? 'Checking…' : 'Retry'}
      </button>
    </div>
  );
}

export const RAIL: { group: string; items: { to: string; label: string; icon: ReactNode }[] }[] = [
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
    { to: '/studio', label: 'Studio', icon: <Braces size={16} /> },
    { to: '/authoring', label: 'Authoring', icon: <FileOutput size={16} /> },
  ]},
  { group: 'Insight', items: [
    { to: '/apps', label: 'Apps', icon: <LayoutGrid size={16} /> },
    { to: '/ledger', label: 'Ledger', icon: <Receipt size={16} /> },
    { to: '/mission', label: 'Console', icon: <Terminal size={16} /> },
    /* Parts was routed and had a full page - rail, breadcrumb, title, the lot -
       but was never added to RAIL, so the ONLY way to reach it was to type the
       URL. It was also absent from the command palette, which is built from
       this same list, so Ctrl+K could not find it either. A section nobody can
       navigate to is not shipped. */
    { to: '/parts', label: 'Parts', icon: <Boxes size={16} /> },
    { to: '/computer', label: 'Computer', icon: <Monitor size={16} /> },
  ]},
  { group: 'System', items: [
    { to: '/settings', label: 'Settings', icon: <Settings size={16} /> },
  ]},
];

/* ── rail pinning ───────────────────────────────────────────────────────────
 * The nav is a fixed 200px every view pays for, on every screen. On a 1440px
 * laptop that is the difference between a ~580px editor and a ~780px one.
 *
 * Pinned by default: the nav stays discoverable, the tab order is unchanged and
 * existing routes/tests keep working. The brand row carries a collapse button.
 * Once collapsed the rail leaves the flex flow entirely — the view reclaims the
 * full width — and an 8px strip on the left edge (or keyboard focus) slides it
 * back as an overlay. Pinning it again puts it back in flow.
 *
 * The state lives at module scope, not in the component: every visited section
 * keeps its own `<Rail/>` mounted, and per-component state would leave the next
 * section pinned after the user collapsed it on this one.
 */
const RAIL_PIN_KEY = 'aria.rail.pinned';
const readRailPinned = () => {
  try { return localStorage.getItem(RAIL_PIN_KEY) !== '0'; } catch { return true; }
};
let railPinned = readRailPinned();
const railListeners = new Set<() => void>();
function setRailPinned(next: boolean): void {
  railPinned = next;
  try { localStorage.setItem(RAIL_PIN_KEY, next ? '1' : '0'); } catch { /* storage off */ }
  for (const f of railListeners) f();
}
const subscribeRail = (f: () => void) => {
  railListeners.add(f);
  return () => { railListeners.delete(f); };
};
const getRailPinned = () => railPinned;

/* The rail is a top strip below `lg` and a column above it, so a rule that only
 * applies to the column (out of flow, inert while closed) has to ask the
 * viewport. matchMedia rather than a resize listener: it fires on the exact
 * boundary the CSS uses. */
const WIDE_Q = '(min-width: 1024px)';
function subscribeWide(f: () => void) {
  const mq = window.matchMedia(WIDE_Q);
  mq.addEventListener('change', f);
  return () => mq.removeEventListener('change', f);
}
const useWide = () => useSyncExternalStore(
  subscribeWide, () => window.matchMedia(WIDE_Q).matches, () => false);

export const Rail = memo(function Rail() {
  const loc = useLocation();
  const up = useHealth();
  /* Pinned = in flow. Unpinned = out of flow, hidden to the left, revealed on
     demand. `peek` is the transient hover/focus reveal; it is local because
     only the visible section's rail can be pointed at. */
  const pinned = useSyncExternalStore(subscribeRail, getRailPinned, getRailPinned);
  const [peek, setPeek] = useState(false);
  const hideTimer = useRef(0);
  const cancelHide = useCallback(() => {
    if (hideTimer.current) {
      window.clearTimeout(hideTimer.current);
      hideTimer.current = 0;
    }
  }, []);
  const show = useCallback(() => {
    cancelHide();
    setPeek(true);
  }, [cancelHide]);
  const hideSoon = useCallback(() => {
    cancelHide();
    hideTimer.current = window.setTimeout(() => setPeek(false), 180);
  }, [cancelHide]);
  useEffect(() => cancelHide, [cancelHide]);
  /* Escape closes a transient reveal without touching the pin state. */
  useEffect(() => {
    if (!peek) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setPeek(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [peek]);
  /* The aside only receives `pointerenter` when the pointer crosses its edge.
     A rail that appeared UNDER a stationary pointer (the usual case: the edge
     strip opened it) never gets one, so its `pointerleave` alone can leave the
     panel stuck open. Watch the pointer position instead: 208px is the rail
     plus a small grace band on its border. */
  useEffect(() => {
    if (pinned || !peek) return;
    const onMove = (e: PointerEvent) => {
      if (e.clientX > 208) hideSoon();
    };
    window.addEventListener('pointermove', onMove, { passive: true });
    return () => window.removeEventListener('pointermove', onMove);
  }, [pinned, peek, hideSoon]);
  const open = pinned || peek;
  const wide = useWide();
  /* The phone rail is a horizontal strip, and the active item can be off the
   * right edge: opening /settings or /computer directly (bookmark, the desktop
   * shell restoring the last route, a deep link from a run) showed the first
   * seven items and no sign of where you were. Bring the active item into
   * view BEFORE paint, and only scroll as far as needed so clicking a
   * partially visible item does not jerk the strip to the centre. */
  const navRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const nav = navRef.current;
    if (!nav || wide) return;
    if (nav.scrollWidth <= nav.clientWidth + 1) return;
    const act = nav.querySelector<HTMLElement>('[aria-current="page"]');
    if (!act) return;
    const nr = nav.getBoundingClientRect();
    const ar = act.getBoundingClientRect();
    if (ar.left < nr.left) nav.scrollLeft += ar.left - nr.left - 8;
    else if (ar.right > nr.right) nav.scrollLeft += ar.right - nr.right + 8;
  }, [loc.pathname, wide]);
  /* A nav slid off-screen must not stay in the tab order. Without this, the
     first Tab from the address bar focused an invisible link 200px to the
     left (which then revealed the rail under the user's cursor), and getting
     to the page meant tabbing through seventeen invisible stops. `inert` is
     the only way to remove a subtree from both focus order and the a11y tree;
     it is applied only where the rail is actually out of flow. */
  const closed = wide && !open;
  return (
    <>
      {/* The edge trigger. Invisible apart from a 2px hairline, so the collapsed
          state still shows WHERE the nav comes back from. Hidden while pinned
          (the rail itself is there) and below lg (where the rail is a top
          strip, not a column). */}
      {!pinned && wide && (
        <div
          aria-hidden
          onPointerEnter={show}
          className="group fixed inset-y-0 left-0 z-30 hidden w-2 lg:block"
        >
          <span className="absolute inset-y-0 left-0 w-[2px] bg-white/10 transition-colors group-hover:bg-accent/60" />
        </div>
      )}
      <aside
        id="primary-rail"
        inert={closed}
        onPointerEnter={pinned ? undefined : show}
        onPointerLeave={pinned ? undefined : hideSoon}
        onFocusCapture={pinned ? undefined : show}
        onBlurCapture={pinned ? undefined : (e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) hideSoon();
        }}
        className={`flex w-full flex-none flex-row items-center gap-1 overflow-x-auto border-b border-glass-border bg-surface-0 px-2 py-1.5 lg:h-full lg:w-[200px] lg:flex-col lg:items-stretch lg:overflow-x-hidden lg:overflow-y-auto lg:border-b-0 lg:border-r lg:px-0 lg:py-0 ${
          pinned
            ? 'lg:relative'
            : `lg:absolute lg:inset-y-0 lg:left-0 lg:z-40 lg:shadow-2xl lg:transition-transform lg:duration-200 lg:ease-out motion-reduce:lg:transition-none ${
              open ? 'lg:translate-x-0' : 'lg:-translate-x-full'
            }`
        }`}
      >
      {/* Brand — the topbar already names the product, so the strip skips it. */}
      <div className="hidden flex-none items-center gap-2.5 px-4 pb-3 pt-4 lg:flex">
        <div className="flex h-7 w-7 flex-none items-center justify-center rounded-lg bg-accent/15" title="Aria">
          <Zap size={14} className="text-accent" />
        </div>
        <span className="text-[15px] font-bold tracking-tight text-zinc-100">Aria</span>
        {/* Search affordance for the command palette. Ctrl/Cmd+K is the fast
            path, but a visible control is what tells a first-time user the
            palette exists at all. */}
        <button
          type="button"
          onClick={openCommandPalette}
          title="Search and commands (Ctrl+K)"
          aria-label="Search and commands"
          className="ml-auto flex h-6 flex-none items-center rounded-md px-1.5 text-zinc-500 transition-colors hover:bg-glass-hover hover:text-zinc-300"
        >
          <Search size={13} />
        </button>
        {/* Collapse / pin. Kept in the brand row, not a floating control: it is
            a property of the nav, and it needs to be visible exactly when the
            nav is (so it can put it back). */}
        <button
          type="button"
          onClick={(e) => {
            const next = !pinned;
            setRailPinned(next);
            setPeek(false);
            if (!next) e.currentTarget.blur();
          }}
          title={pinned
            ? 'Hide the navigation — hover the left edge to bring it back'
            : 'Keep the navigation open'}
          aria-label={pinned ? 'Hide navigation' : 'Pin navigation'}
          aria-expanded={open}
          aria-controls="primary-rail"
          className="flex h-6 flex-none items-center rounded-md px-1 text-zinc-500 transition-colors hover:bg-glass-hover hover:text-zinc-300"
        >
          {pinned ? <PanelLeftClose size={13} /> : <PanelLeftOpen size={13} />}
        </button>
      </div>

      {/* Nav groups (icon-only below lg).

          `min-w-0` is load-bearing: as a flex item the nav defaults to
          `min-width: auto`, so it refuses to shrink below its content and
          grows the whole document instead of scrolling. That stayed hidden
          while the strip fitted a 390px phone; adding a 12th item pushed it
          over and every page became 594px wide with one icon per line. */}
      {/* The nav scrolls on its own at desktop. With `overflow-y: visible` a
          700px-tall window (the desktop shell's own minimum) overflowed by
          32px and the last item, Settings, painted over the status footer.
          The rail's own overflow-y-auto never engaged because the flex item
          clipped the growth instead of contributing scroll height. */}
      <nav ref={navRef} className="flex min-h-0 min-w-0 flex-1 items-center gap-1 overflow-x-auto px-0.5 lg:flex-col lg:items-stretch lg:overflow-x-hidden lg:overflow-y-auto lg:px-2 lg:pb-3" aria-label="Primary">
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
                    className={`group relative flex flex-none items-center justify-center gap-2.5 rounded-lg px-3 py-3 text-[13px] transition-colors duration-200 ease-out lg:flex-initial lg:justify-start lg:px-2.5 lg:py-[6px] ${
                      on
                        ? 'bg-white/[0.06] font-semibold text-zinc-100'
                        : 'text-zinc-400 hover:bg-glass-hover hover:text-zinc-200'
                    }`}
                    /* Warm the target section's chunk as soon as the pointer
                       or keyboard focus arrives, so the hop is a CSS show/hide
                       instead of a Suspense fallback over the whole shell. */
                    onMouseEnter={() => prefetchSection(it.to)}
                    onFocus={() => prefetchSection(it.to)}
                    onPointerDown={() => prefetchSection(it.to)}
                  >
                    {/* Active indicator bar. A shared `layoutId` makes Motion
                        slide it between items as you navigate (and between the
                        vertical and horizontal rail layouts) instead of the old
                        hard cut where it vanished from one row and appeared on
                        the next. */}
                    {on && (
                      <motion.span
                        layoutId="rail-active"
                        className="absolute left-0 top-1 bottom-1 w-[3px] rounded-full bg-accent"
                        transition={{ type: 'spring', stiffness: 520, damping: 42 }}
                      />
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

      {/* Status footer (desktop only). The dot is the rail's at-a-glance
          heartbeat; the authoritative, all-breakpoint signal is the
          GatewayDownBanner the shell renders above every view. */}
      <div className="hidden flex-none items-center gap-2.5 border-t border-glass-border px-4 py-3 lg:flex">
        <span className={`status-dot flex-none ${up ? 'status-dot-ok' : 'status-dot-bad'}`} role="status" aria-label={up ? 'agent and gateway connected' : 'connecting'} />
        <span className="text-[11px] text-zinc-muted">{up ? 'agent + gateway' : 'connecting…'}</span>
      </div>
      </aside>
    </>
  );
});

export const TopBar = memo(function TopBar({ crumb, children, panes }: {
  crumb: string;
  children?: ReactNode;
  /** This view's pane-registry slug. Renders the Layout control, which is the
   *  only restore path for a hidden pane, so it is mounted by the top bar and
   *  never by a <Pane> — a feature must not be able to hide its own escape
   *  hatch. Views that register nothing pass nothing. */
  panes?: string;
}) {
  /* Every view renders a TopBar, so this is the one place that can give each
     route an identity. `document.title` was the static "Aria · Research" on
     all fifteen routes, so a screen reader, a bookmark and the browser tab
     history could not tell you where you were.

     It must follow VISIBILITY, not just mounting. Keep-alive hides a visited
     view instead of unmounting it, so going Back to Documents did not remount
     its TopBar and the tab kept saying "Aria · Runs" while the URL and page
     were Documents. The effect now runs when the section becomes active
     again; hidden sections are explicitly skipped so their (still mounted)
     TopBars cannot fight over the title. */
  const active = useSectionActive();
  useEffect(() => {
    if (active) document.title = `Aria · ${crumb}`;
  }, [active, crumb]);
  const pinned = useSyncExternalStore(subscribeRail, getRailPinned, getRailPinned);
  const wide = useWide();
  return (
    /* The control cluster used to be a bare `flex-1` spacer plus siblings, so
       a wide pill row (status + Download + Copy) pushed the header past the
       viewport and gave the whole page a horizontal scrollbar. It wraps now
       and right-aligns itself. */
    <div className="glass sticky top-0 z-10 flex items-center gap-2 px-4 py-2.5">
      {/* The way back. A collapsed nav that can only be restored by finding a
          two-pixel edge is not restorable in practice: the toggle went off
          screen with the rail, so the top bar carries one too. It reads as
          "show", not "pin", because one click puts the nav back in flow. */}
      {wide && !pinned && (
        <button
          type="button"
          onClick={() => setRailPinned(true)}
          title="Show the navigation"
          aria-label="Show navigation"
          className="flex h-6 flex-none items-center rounded-md px-1 text-zinc-500 transition-colors hover:bg-glass-hover hover:text-zinc-300"
        >
          <PanelLeftOpen size={13} />
        </button>
      )}
      <span className="flex flex-none items-center gap-1.5 text-[13px] font-semibold">
        {/* Below lg the rail's brand row is hidden, so the wordmark rides
            here; on desktop the rail already says "Aria", and a second badge
            one pane over was pure duplication. */}
        <Zap size={11} className="flex-none text-accent lg:hidden" />
        <span className="flex-none text-zinc-300 lg:hidden">Aria</span>
        <span className="flex-none text-zinc-muted lg:hidden">/</span>
        <span className="max-w-[46vw] truncate text-zinc-200 sm:max-w-[280px]">{crumb}</span>
      </span>
      <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-2">
        {panes && <LayoutMenu view={panes} />}
        {children}
      </div>
    </div>
  );
});

export const Stat = memo(function Stat({ k, v, s, tone, accent, loading }: {
  k: string;
  v: string;
  s?: string;
  tone?: 'ok' | 'bad' | 'warn';
  accent?: string;
  /** First paint: the view has not read its list yet. Rendering the `0` /
   *  `down` default here asserts a fact nobody has checked — a fresh
   *  Documents page said "DOCUMENTS 0" for 650ms and an Apps tile said
   *  "GATEWAY down" (red) until the health read landed. */
  loading?: boolean;
}) {
  const accentColor = loading ? 'var(--color-muted)' : tone === 'ok' ? 'var(--color-ok)' : tone === 'bad' ? 'var(--color-danger)' : tone === 'warn' ? 'var(--color-warn)' : accent || 'var(--color-accent)';
  const valueColor = loading ? 'text-zinc-muted' : tone === 'ok' ? 'text-ok' : tone === 'bad' ? 'text-danger' : tone === 'warn' ? 'text-warn' : '';

  return (
    <div className="instrument-card min-w-[116px] flex-1 p-3.5" style={{ '--accent-line': accentColor } as React.CSSProperties}>
      <div className="text-[10px] font-bold tracking-[0.14em] text-zinc-muted uppercase">{k}</div>
      {/* `whitespace-nowrap` + a stable min-width: the value is the one thing
          in a card whose WIDTH changes with the data ("$0.0010" -> "$0.00",
          "200" -> "1,284"). Inside a `flex-wrap` row of `flex-1` cards that
          re-derives the whole row's distribution and rewraps it, pushing every
          section below down - 0.17 CLS on Apps, the worst remaining on the
          site. Numbers must not reflow their container. */}
      <div
        className={`mt-1 min-w-[3ch] whitespace-nowrap text-xl font-bold tabular-nums ${valueColor}`}
        aria-busy={loading || undefined}
      >{loading ? '—' : v}</div>
      {s && <div className="mt-0.5 truncate text-[11px] text-zinc-muted">{s}</div>}
    </div>
  );
});

/* Empty / Skel / Pill live in primitives.tsx so the rail-free studio can share
   them without importing this module (which exports the Rail). Re-exported
   here so every existing console import keeps working. */
export { Empty, Skel, Pill } from './primitives';

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
        // Only the visible keep-alive section owns `id="main"`, so
        // getElementById can never land on a hidden copy (H14).
        document.getElementById('main')?.focus();
      }}
      className="sr-only z-[100] focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:rounded-md focus:bg-accent focus:px-3 focus:py-2 focus:text-[12px] focus:font-bold focus:text-[#0b0b0e] focus:outline-none"
    >
      Skip to main content
    </a>
  );
}
