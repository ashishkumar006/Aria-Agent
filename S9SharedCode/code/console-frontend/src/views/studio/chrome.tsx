import type { ReactNode } from 'react';

/* The studio's chrome, in the console's own idiom.

   Every console section draws its cards the same way: 10px radius, a surface-1
   fill, a glass hairline, a 2px accent line along the top edge
   (`.instrument-card` in index.css), 10px uppercase micro-labels with wide
   tracking, a bordered icon tile beside each label, and a violet-400 primary
   action. The section rails are a step darker (surface-0) and the page canvas
   darker still (#090909, `bg-void`).

   These helpers pin the studio to those same tokens, so the two cannot drift
   into different palettes again; the studio keeps its own rail-free workbench
   layout, which is a deliberate product decision (CODE_IDE_DESIGN §4.1), not a
   styling one. */

/** A panel: the console's card, without the hover lift (these are not tiles
 *  you click, and a workbench panel that rises under the cursor while you are
 *  reading it is noise). `accent` draws the hairline; omit it for chrome. */
export function Panel({ children, className = '', accent }: {
  children: ReactNode;
  className?: string;
  accent?: string;
}) {
  return (
    <div
      className={'relative overflow-hidden rounded-[10px] border border-glass-border bg-surface-1 ' + className}
      style={accent ? ({ '--accent-line': accent } as React.CSSProperties) : undefined}
    >
      {accent && (
        <span aria-hidden
              className="pointer-events-none absolute inset-x-0 top-0 h-[2px]"
              style={{ background: `linear-gradient(90deg, ${accent} 0%, transparent 100%)`,
                       opacity: 0.7 }} />
      )}
      {children}
    </div>
  );
}

/** 10px bold, wide-tracked, uppercase: the label every console card carries. */
export function Label({ children, className = '' }: {
  children: ReactNode; className?: string;
}) {
  return (
    <span className={'text-[10px] font-bold uppercase tracking-[0.14em] text-zinc-muted ' + className}>
      {children}
    </span>
  );
}

/** The bordered tile that sits left of a card's label. */
export function Tile({ children, tone = 'muted' }: {
  children: ReactNode; tone?: 'muted' | 'accent';
}) {
  return (
    <span aria-hidden
          className={'flex h-6 w-6 flex-none items-center justify-center rounded-md border border-glass-border '
            + (tone === 'accent' ? 'bg-white/[0.06] text-zinc-400' : 'bg-white/[0.04] text-zinc-400')}>
      {children}
    </span>
  );
}

/** One row of a card head: tile, label, then anything right-aligned. */
export function Head({ icon, children, right }: {
  icon?: ReactNode; children: ReactNode; right?: ReactNode;
}) {
  return (
    <div className="flex flex-none items-center gap-2 px-3 py-2.5">
      {icon && <Tile>{icon}</Tile>}
      <Label>{children}</Label>
      <span className="min-w-0 flex-1" />
      {right}
    </div>
  );
}

const BTN = {
  primary: 'rounded-md bg-zinc-100 px-3.5 py-1.5 text-xs font-bold text-[#161616] '
    + 'transition-colors hover:brightness-110 disabled:opacity-40',
  ghost: 'rounded-md px-2 py-1 text-xs text-zinc-300 transition-colors '
    + 'hover:bg-white/5 hover:text-zinc-100 disabled:opacity-40',
  outline: 'rounded-md border border-white/15 bg-white/5 px-3 py-1.5 text-xs text-zinc-200 '
    + 'transition-colors hover:border-white/25 disabled:opacity-40',
  danger: 'rounded-md border border-danger/40 bg-danger-dim px-2.5 py-1 text-xs '
    + 'text-danger transition-colors hover:bg-danger/20 disabled:opacity-40',
} as const;

export function Btn({ kind = 'outline', children, onClick, disabled, title, ariaLabel, className = '' }: {
  kind?: keyof typeof BTN;
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  title?: string;
  ariaLabel?: string;
  className?: string;
}) {
  return (
    <button type="button" onClick={onClick} disabled={disabled}
            title={title} aria-label={ariaLabel}
            className={BTN[kind] + ' ' + className}>
      {children}
    </button>
  );
}
