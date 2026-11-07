import { useCallback, useEffect, useRef, useState } from 'react';

/** One drag-to-resize divider, used by every split in the studio.
 *
 *  Written once on purpose. The console already has two hand-rolled pointer
 *  drags (`views/Code.tsx` `dragAgent`, `views/Authoring.tsx` the focus
 *  split), and each one re-derives the same three things that are easy to get
 *  subtly wrong: clamping against a live minimum instead of a stale one,
 *  refusing to resize when the pane is not actually side-by-side, and leaving
 *  the divider unreachable by keyboard. A fourth split should not be a fourth
 *  chance to get those wrong.
 *
 *  `storageKey` persists the fraction per browser, matching `aria.*` elsewhere
 *  — sizes are a property of the person's window, not of the project.
 */
export interface Split {
  /** 0..100, the share taken by the FIRST pane. */
  value: number;
  set: (next: number) => void;
  /** Attach to the divider element. `vertical` = a row split (left/right). */
  onPointerDown: (e: React.PointerEvent) => void;
  onKeyDown: (e: React.KeyboardEvent) => void;
  /** False when the layout cannot honour a split right now (narrow screen). */
  live: boolean;
}

const clamp = (n: number, lo: number, hi: number) =>
  Math.min(hi, Math.max(lo, Number.isFinite(n) ? n : lo));

export function readStored(key: string, fallback: number): number {
  try {
    const raw = localStorage.getItem(key);
    const n = raw === null ? NaN : Number(raw);
    return Number.isFinite(n) ? n : fallback;
  } catch {
    return fallback;
  }
}

export function useSplit(opts: {
  storageKey: string;
  /** Default share of the first pane, 0..100. */
  default: number;
  min: number;
  max: number;
  /** CSS media query that must hold for the split to mean anything, e.g. the
   *  layout stacks below a width and a percentage is then meaningless. */
  liveQuery?: string;
}): Split {
  const { storageKey, default: dflt, min, max, liveQuery } = opts;
  const [value, setValue] = useState<number>(() =>
    clamp(readStored(storageKey, dflt), min, max));
  const [live, setLive] = useState(() =>
    !liveQuery || (typeof window !== 'undefined'
      && window.matchMedia(liveQuery).matches));
  const drag = useRef<{ x: number; w: number; start: number } | null>(null);

  useEffect(() => {
    if (!liveQuery || typeof window === 'undefined') return;
    const mq = window.matchMedia(liveQuery);
    const on = () => setLive(mq.matches);
    on();
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, [liveQuery]);

  const set = useCallback((next: number) => {
    const v = clamp(next, min, max);
    setValue(v);
    try { localStorage.setItem(storageKey, String(v)); } catch { /* private mode */ }
  }, [min, max, storageKey]);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    if (!live) return;
    const parent = (e.currentTarget as HTMLElement).parentElement;
    if (!parent) return;
    drag.current = {
      x: e.clientX, w: parent.getBoundingClientRect().width, start: value,
    };
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    const move = (ev: PointerEvent) => {
      const d = drag.current;
      if (!d || d.w <= 0) return;
      set(d.start + ((ev.clientX - d.x) / d.w) * 100);
    };
    const up = () => {
      drag.current = null;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }, [live, value, set]);

  /* Arrow keys move the divider, Home/End go to the bounds. A divider you
     cannot reach from the keyboard is a divider that hides a whole pane from
     anyone not using a mouse, and this one controls both. */
  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (!live) return;
    const step = e.shiftKey ? 10 : 2;
    if (e.key === 'ArrowLeft') { set(value - step); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { set(value + step); e.preventDefault(); }
    else if (e.key === 'Home') { set(min); e.preventDefault(); }
    else if (e.key === 'End') { set(max); e.preventDefault(); }
  }, [live, value, min, max, set]);

  return { value, set, onPointerDown, onKeyDown, live };
}

/** The divider element every studio split renders. */
export function Splitter({ split, label, vertical = true }: {
  split: Split; label: string; vertical?: boolean;
}) {
  return (
    <div
      role="separator"
      aria-orientation={vertical ? 'vertical' : 'horizontal'}
      aria-label={label}
      aria-valuenow={split.live ? Math.round(split.value) : undefined}
      aria-valuemin={0}
      aria-valuemax={100}
      tabIndex={split.live ? 0 : -1}
      onPointerDown={split.onPointerDown}
      onKeyDown={split.onKeyDown}
      title={split.live
        ? 'Drag to resize · arrows nudge · Shift+arrows move 10%'
        : 'Not resizable at this width'}
      className={
        'flex-none bg-glass-border hover:bg-accent/40 '
        + (vertical ? 'w-[3px] cursor-col-resize ' : 'h-[3px] cursor-row-resize ')
        + (split.live ? '' : 'opacity-40 ')
      }
    />
  );
}
