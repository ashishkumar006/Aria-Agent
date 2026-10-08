import { useEffect, useRef, type RefObject } from 'react';

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'textarea:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

/** Focus trap for modal overlays (dialogs, drawers, palettes).
 *  While `active`, Tab and Shift+Tab cycle inside `ref` instead of
 *  escaping to the page behind the overlay, Escape invokes
 *  `onEscape`, focus lands on the first focusable element (or
 *  `initialFocus`) on mount, and returns to the trigger on unmount.
 *  The overlay must also carry `aria-modal="true"` and a backdrop
 *  click handler; the trap only covers the keyboard half. */
export function useFocusTrap<T extends HTMLElement>(
  ref: RefObject<T | null>,
  opts: {
    active?: boolean;
    initialFocus?: RefObject<HTMLElement | null>;
    onEscape?: () => void;
  } = {},
) {
  const { active = true, initialFocus } = opts;
  /* Latest-ref for the callback so the listener installed once per
     mount always calls the current handler without re-arming — and
     re-stealing focus — on every render. */
  const escapeRef = useRef(opts.onEscape);
  escapeRef.current = opts.onEscape;

  useEffect(() => {
    if (!active) return undefined;
    const root = ref.current;
    if (!root) return undefined;
    const prev = document.activeElement as HTMLElement | null;
    const focusables = () => Array.from(
      root.querySelectorAll<HTMLElement>(FOCUSABLE),
    ).filter((el) => el.tabIndex >= 0);
    (initialFocus?.current ?? focusables()[0])?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        /* Another handler (the app-level capture listener) may
           have already consumed this Escape — closing the overlay
           itself. Acting on it too would close a second overlay
           stacked underneath on the same keypress. */
        if (e.defaultPrevented) return;
        e.stopPropagation();
        escapeRef.current?.();
        return;
      }
      if (e.key !== 'Tab') return;
      const items = focusables();
      if (!items.length) { e.preventDefault(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      const cur = document.activeElement;
      if (e.shiftKey && (cur === first || !root.contains(cur))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (cur === last || !root.contains(cur))) {
        e.preventDefault();
        first.focus();
      }
    };
    root.addEventListener('keydown', onKey);
    return () => {
      root.removeEventListener('keydown', onKey);
      if (prev && document.contains(prev)) prev.focus();
    };
  }, [ref, active, initialFocus]);
}
