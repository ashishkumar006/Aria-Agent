import { useEffect, useState } from 'react';
import { Minus, Square, X } from 'lucide-react';
import { useLocation } from 'react-router-dom';

/* Desktop window chrome, drawn by us because the shell hides the OS title bar.
 *
 * Why: `titleBarStyle: 'hidden'` in main.cjs removes the Windows title bar,
 * which reclaims ~32px of window height on every screen — but it also removes
 * the only way to move, minimise, maximise or close the window. This puts that
 * back as a slim strip.
 *
 * Two rules make this work:
 *   - `-webkit-app-region: drag` on the strip lets the OS move the window by
 *     dragging it. Everything else in the app must stay `no-drag`, or text
 *     becomes unselectable and buttons stop responding.
 *   - The buttons are `no-drag`, so clicking them is a click rather than the
 *     start of a window drag.
 *
 * In a plain browser tab `window.ariaDesktop` is undefined and this renders
 * null, so the browser build looks exactly as it did. Detect that once on the
 * client rather than at module scope, because this is client-only code and a
 * module-level read of `window` would break SSR-style prerendering.
 */
const isDesktop = () =>
  typeof window !== 'undefined' &&
  !!(window as unknown as { ariaDesktop?: unknown }).ariaDesktop;

/* Kept as a constant rather than inline-concatenated into JSX: a string
   expression is not valid where an attribute value belongs. */
const DRAGBAR_CLS =
  'aria-dragbar flex h-[34px] flex-none items-center justify-between ' +
  'border-b border-white/10 bg-[#0a0c13] pl-3';

const TITLES: Record<string, string> = {
  '/console': 'Chat',
  '/research': 'Research',
  '/runs': 'Runs',
  '/memory': 'Memory',
  '/documents': 'Documents',
  '/scheduler': 'Scheduler',
  '/skills': 'Skills',
  '/apps': 'Apps',
  '/ledger': 'Ledger',
  '/mission': 'Mission',
  '/settings': 'Settings',
  '/code': 'Code',
  '/authoring': 'Authoring',
};

export default function TitleBar() {
  const [desktop, setDesktop] = useState(false);
  const [maximised, setMaximised] = useState(false);
  const { pathname } = useLocation();

  useEffect(() => {
    setDesktop(isDesktop());
    const w = (window as unknown as { ariaDesktop?: { onMaxChange?: (cb: (m: boolean) => void) => () => void } }).ariaDesktop;
    if (w?.onMaxChange) return w.onMaxChange(setMaximised);
  }, []);

  if (!desktop) return null;

  const api = (window as unknown as {
    ariaDesktop: {
      minimise: () => void;
      toggleMaximise: () => void;
      close: () => void;
    };
  }).ariaDesktop;

  const btn =
    'flex h-full w-11 items-center justify-center text-zinc-400 transition-colors ' +
    'hover:bg-white/10 hover:text-zinc-100 aria-[current=true]:bg-white/10';
  const label = TITLES[pathname] ?? 'Console';

  return (
    <div
      className={DRAGBAR_CLS}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className="text-[13px] font-bold tracking-wide text-zinc-100">Aria</span>
        <span className="text-zinc-600">·</span>
        <span className="truncate text-[12px] text-zinc-400">{label}</span>
      </div>
      <div className="flex h-full">
        <button type="button" aria-label="Minimise" onClick={api.minimise}
          className={btn}><Minus size={14} /></button>
        <button type="button"
          aria-label={maximised ? 'Restore' : 'Maximise'}
          aria-current={maximised}
          onClick={api.toggleMaximise}
          className={btn}><Square size={11} /></button>
        <button type="button" aria-label="Close" onClick={api.close}
          className={`${btn} hover:bg-red-600 hover:text-white`}><X size={14} /></button>
      </div>
    </div>
  );
}
