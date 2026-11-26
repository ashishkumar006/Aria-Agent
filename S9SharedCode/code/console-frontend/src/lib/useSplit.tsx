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

/** Like `useSplit`, except the size is measured in pixels against the pane's
 *  own edge rather than as a share of the parent. Used by the studio's
 *  section panel, which should be ~250px wherever it sits — a percentage of
 *  "whatever the window happens to be" stretched the tree across half the
 *  screen at 1920px and squeezed it to 150px at 900px.
 *
 *  The divider sits on the panel's inner edge and the panel is anchored to
 *  its far edge, so dragging the divider towards the editor widens the panel.
 *  Pointer, keyboard and persistence semantics match `useSplit`. */
export function useSplitPx(opts: { storageKey: string; initial: number; min: number; max: number }) {
  const { storageKey, initial, min, max } = opts;
  const [value, setValue] = useState<number>(() =>
    clamp(readStored(storageKey, initial), min, max));
  const drag = useRef<{ startX: number; startValue: number } | null>(null);

  const set = useCallback((next: number) => {
    const v = clamp(next, min, max);
    setValue(v);
    try { localStorage.setItem(storageKey, String(v)); } catch { /* private mode */ }
  }, [storageKey, min, max]);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    const target = e.currentTarget as HTMLElement;
    drag.current = { startX: e.clientX, startValue: value };
    target.setPointerCapture?.(e.pointerId);
    const move = (ev: PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      set(d.startValue + (d.startX - ev.clientX));
    };
    const up = () => {
      drag.current = null;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }, [value, set]);

  /* Same contract as `useSplit`: arrows nudge, Shift+arrows move far,
     Home/End go to the bounds. Left widens, because left is the direction the
     pane grows in. */
  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    const step = e.shiftKey ? 64 : 16;
    if (e.key === 'ArrowLeft') { set(value + step); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { set(value - step); e.preventDefault(); }
    else if (e.key === 'Home') { set(min); e.preventDefault(); }
    else if (e.key === 'End') { set(max); e.preventDefault(); }
  }, [value, min, max, set]);

  return { value, set, onPointerDown, onKeyDown, live: true, range: { min, max } };
}

/** The divider element every studio split renders. `split` may carry a pixel
 *  `range` and identity `live`; `aria-valuemin/max` follow it when present. */
export function Splitter({ split, label, vertical = true }: {
  split: Split & { range?: { min: number; max: number } };
  label: string;
  /** `vertical` = this is a vertical divider between columns (a row split).
     A column split (`aria-orientation="horizontal"`) is rendered by callers
     that supply their own divider. */
  vertical?: boolean;
}) {
  const range = split.range;
  return (
    <div
      role="separator"
      aria-orientation={vertical ? 'vertical' : 'horizontal'}
      aria-label={label}
      aria-valuenow={split.live ? Math.round(split.value) : undefined}
      aria-valuemin={range?.min ?? 0}
      aria-valuemax={range?.max ?? 100}
      tabIndex={split.live ? 0 : -1}
      onPointerDown={split.onPointerDown}
      onKeyDown={split.onKeyDown}
      title={split.live
        ? 'Drag to resize · arrows nudge · Shift+arrows move far'
        : 'Not resizable at this width'}
      className={
        'group/split relative flex-none bg-glass-border hover:bg-accent/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-1px] focus-visible:outline-accent '
        + (vertical ? 'w-[4px] cursor-col-resize ' : 'h-[4px] cursor-row-resize ')
        + (split.live ? '' : 'opacity-40 ')
      }
    >
      {/* A 3px divider is invisible until you find it by accident. A short
          accent grip that appears on hover says "this edge moves" without
          adding chrome to the resting layout. */}
      <span aria-hidden className={
        'pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white/15 transition-colors group-hover/split:bg-accent/60 '
        + (vertical ? 'h-5 w-[6px] ' : 'h-[6px] w-5 ')
      } />
    </div>
  );
}
