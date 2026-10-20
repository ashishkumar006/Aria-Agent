import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Command } from 'cmdk';
import { AnimatePresence, motion } from 'motion/react';
import { CornerDownLeft, RotateCw, Search } from 'lucide-react';
import { RAIL } from './ui';
import { OPEN_PALETTE_EVENT } from '../lib/palette';

/* The OS-style command palette.
 *
 * Ctrl/Cmd+K (or a `aria:open-palette` event, dispatched by the rail's search
 * affordance) opens a floating panel that filters every section and a couple of
 * app actions. cmdk owns filtering, the roving selection and Enter-to-select;
 * Motion owns the open/close spring; this file owns the overlay, the focus trap
 * and the Escape/backdrop dismissal.
 *
 * Why not cmdk's own `CommandDialog`: it wraps Radix Dialog, which wants to own
 * entrance/exit via its Presence. Rendering the panel ourselves keeps the motion
 * and the styling in one place while still giving us the pieces that matter
 * (escape, backdrop click, autofocus, a Tab cycle that cannot leave the panel).
 */
type Cmd = { id: string; label: string; group: string; icon: React.ReactNode; run: () => void };

export default function CommandPalette() {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const panelRef = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);

  /* The global chord. Registered in the CAPTURE phase and stopped there so a
   * view's own handlers (Code.tsx watches window keydown in capture too) cannot
   * swallow it, and so Ctrl+K never reaches the browser's own address-bar
   * shortcut. */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        e.stopPropagation();
        setOpen((v) => !v);
      }
    };
    const onOpen = () => setOpen(true);
    window.addEventListener('keydown', onKey, true);
    window.addEventListener(OPEN_PALETTE_EVENT, onOpen);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener(OPEN_PALETTE_EVENT, onOpen);
    };
  }, []);

  /* While open: lock background scroll, move focus into the field, trap Tab
   * inside the panel, close on Escape, and hand focus back to whatever had it
   * when the palette closes. */
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const focusTimer = window.setTimeout(() => {
      panelRef.current?.querySelector<HTMLElement>('input')?.focus();
    }, 20);

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        setOpen(false);
        return;
      }
      if (e.key !== 'Tab' || !panelRef.current) return;
      const nodes = Array.from(panelRef.current.querySelectorAll<HTMLElement>(
        'input, [cmdk-item], button:not([disabled])',
      ));
      if (!nodes.length) return;
      // cmdk keeps focus on its input for arrow navigation, so the old
      // first/last-only test never matched and Tab escaped to the page
      // behind the modal (H13). Move focus explicitly on every Tab.
      e.preventDefault();
      const cur = document.activeElement as HTMLElement | null;
      const at = cur ? nodes.indexOf(cur) : -1;
      const next = e.shiftKey
        ? (at <= 0 ? nodes[nodes.length - 1] : nodes[at - 1])
        : (at < 0 || at === nodes.length - 1 ? nodes[0] : nodes[at + 1]);
      next.focus();
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => {
      window.clearTimeout(focusTimer);
      window.removeEventListener('keydown', onKeyDown, true);
      document.body.style.overflow = prevOverflow;
      previous?.focus?.();
    };
  }, [open]);

  const commands = useMemo<Cmd[]>(() => {
    const go = (to: string) => () => { setOpen(false); navigate(to); };
    const nav: Cmd[] = RAIL.flatMap((g) => g.items.map((it) => ({
      id: `nav:${it.to}`, label: it.label, group: g.group, icon: it.icon, run: go(it.to),
    })));
    const actions: Cmd[] = [
      {
        id: 'act:reload', label: 'Reload console', group: 'Actions',
        icon: <RotateCw size={16} />,
        run: () => { setOpen(false); window.location.reload(); },
      },
    ];
    return [...nav, ...actions];
  }, [navigate]);

  const groups = useMemo(() => {
    const out: { name: string; items: Cmd[] }[] = [];
    for (const c of commands) {
      const last = out[out.length - 1];
      if (last && last.name === c.group) last.items.push(c);
      else out.push({ name: c.group, items: [c] });
    }
    return out;
  }, [commands]);

  const itemCls =
    'flex cursor-pointer items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] text-zinc-300 ' +
    'data-[selected=true]:bg-accent/15 data-[selected=true]:text-zinc-100';

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[300] flex items-start justify-center bg-black/55 p-4 pt-[12vh] backdrop-blur-[3px]"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.14 }}
          onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}
          role="dialog"
          aria-modal="true"
          aria-label="Command menu"
        >
          <motion.div
            ref={panelRef}
            className="glass w-full max-w-[560px] overflow-hidden rounded-xl border border-glass-border bg-surface-1 shadow-depth"
            initial={{ opacity: 0, y: -12, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.98 }}
            transition={{ type: 'spring', stiffness: 430, damping: 34 }}
          >
            <Command
              loop
              label="Command menu"
              className="[&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2.5 [&_[cmdk-group-heading]]:text-[10px] [&_[cmdk-group-heading]]:font-bold [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[0.14em] [&_[cmdk-group-heading]]:text-zinc-muted"
            >
              <div className="flex items-center gap-2.5 border-b border-glass-border px-3.5">
                <Search size={15} className="flex-none text-zinc-500" />
                <Command.Input
                  autoFocus
                  placeholder="Jump to a section or run a command…"
                  className="h-11 w-full bg-transparent text-[13.5px] text-zinc-100 outline-none placeholder:text-zinc-500"
                />
              </div>

              <Command.List className="max-h-[52vh] overflow-y-auto p-1.5">
                <Command.Empty className="px-3 py-7 text-center text-[12.5px] text-zinc-500">
                  No sections or commands match that.
                </Command.Empty>
                {groups.map((g) => (
                  <Command.Group key={g.name} heading={g.name}>
                    {g.items.map((c) => (
                      <Command.Item
                        key={c.id}
                        value={`${c.label} ${c.group}`}
                        onSelect={c.run}
                        className={itemCls}
                      >
                        <span className="flex-none text-zinc-500">{c.icon}</span>
                        <span className="min-w-0 flex-1 truncate">{c.label}</span>
                      </Command.Item>
                    ))}
                  </Command.Group>
                ))}
              </Command.List>

              <div className="flex items-center gap-3 border-t border-glass-border px-3.5 py-1.5 text-[10.5px] text-zinc-600">
                <span className="flex items-center gap-1"><CornerDownLeft size={10} /> select</span>
                <span className="flex items-center gap-1">↑ ↓ navigate</span>
                <span className="ml-auto flex items-center gap-1">Esc close</span>
              </div>
            </Command>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
