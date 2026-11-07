import type { ReactNode } from 'react';

/* The studio's chrome, in the console's own idiom.

   Docs, Skills and Authoring share one visual grammar: 12px radii, a
   surface-1 card with a 2px accent hairline along its top edge
   (`.instrument-card` in index.css), 10px uppercase micro-labels with wide
   tracking, a bordered icon tile beside each label, and one violet primary
   button per card. `/studio` was built against a different reference and had
   none of it — flat 4-6px corners, every heading the same weight, six
   bordered buttons competing in each panel — so moving between Studio and the
   rest of Aria read as two different products.

   These are deliberately tiny. They exist so all ten studio panels can say
   "card", "label", "primary action" once instead of each re-deriving the
   scale, which is how the drift started. */

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
      className={'relative overflow-hidden rounded-xl border border-edge bg-surface-1 ' + className}
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
            + (tone === 'accent' ? 'bg-accent/10 text-accent' : 'bg-white/[0.04] text-zinc-400')}>
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
  primary: 'rounded-lg bg-accent px-3 py-1.5 text-[12px] font-semibold text-black '
    + 'transition-colors hover:bg-accent/90 disabled:opacity-40',
  ghost: 'rounded-lg px-2 py-1 text-[12px] text-zinc-300 transition-colors '
    + 'hover:bg-white/5 hover:text-zinc-100 disabled:opacity-40',
  outline: 'rounded-lg border border-glass-border px-2.5 py-1 text-[12px] text-zinc-200 '
    + 'transition-colors hover:bg-white/5 disabled:opacity-40',
  danger: 'rounded-lg border border-danger/40 bg-danger-dim px-2.5 py-1 text-[12px] '
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
