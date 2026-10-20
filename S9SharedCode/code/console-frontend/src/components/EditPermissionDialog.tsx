import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Check, X } from 'lucide-react';

/** One write the coding agent wants to make, waiting on a decision. */
export interface PendingEdit {
  id: string;
  tool: string;
  summary: string;
  diff: string;
}

/** The agent's write gate.
 *
 *  The coding agent can edit, but nothing reaches disk unattended: the run
 *  parks on `/api/code/permission/{id}` and this dialog is the only thing that
 *  releases it. So the diff has to be readable BEFORE the decision, and the
 *  decision has to be a real one - declining returns control to the model with
 *  "the user declined this change", which is what lets it adapt instead of
 *  stalling or retrying the same write.
 *
 *  Deliberately not dismissible by backdrop-click or Escape: there is no
 *  default. A write that is neither accepted nor refused must NOT proceed, so
 *  the only ways out are the two buttons. (The server's own timeout is the
 *  backstop if the tab goes away entirely.) */
export function EditPermissionDialog({ pending, onDecide, busy }: {
  pending: PendingEdit | null;
  onDecide: (id: string, approve: boolean) => Promise<void>;
  busy: boolean;
}) {
  const [deciding, setDeciding] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const acceptRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (pending) { setDeciding(false); acceptRef.current?.focus(); }
  }, [pending?.id]);

  // Only the two buttons are reachable by keyboard: Tab cycles inside the
  // dialog rather than escaping to the page behind it, so a stray Enter can
  // never land on some other control while a write is pending.
  useEffect(() => {
    if (!pending) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      const nodes = panelRef.current?.querySelectorAll<HTMLElement>('button:not([disabled])');
      if (!nodes || nodes.length === 0) return;
      const first = nodes[0], last = nodes[nodes.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (active === first || !panelRef.current?.contains(active))) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault(); first.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [pending]);

  const decide = async (approve: boolean) => {
    if (!pending || deciding) return;
    setDeciding(true);
    try {
      await onDecide(pending.id, approve);
    } finally {
      setDeciding(false);
    }
  };

  return (
    <AnimatePresence>
      {pending && (
        <motion.div
          className="fixed inset-0 z-[320] flex items-start justify-center bg-black/60 p-4 pt-[14vh] backdrop-blur-[3px]"
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          transition={{ duration: 0.14 }}
          role="dialog" aria-modal="true" aria-label="Approve this change"
        >
          <motion.div
            ref={panelRef}
            className="glass w-full max-w-[760px] overflow-hidden rounded-xl border border-glass-border bg-surface-1 shadow-depth"
            initial={{ opacity: 0, y: -12, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.98 }}
            transition={{ type: 'spring', stiffness: 430, damping: 34 }}
          >
            <div className="flex items-start gap-2.5 border-b border-glass-border px-3.5 py-3">
              <div className="min-w-0 flex-1">
                <h2 className="text-[13px] font-semibold text-zinc-100">
                  The agent wants to change a file
                </h2>
                <p className="mt-0.5 truncate text-[11.5px] text-zinc-muted">
                  {pending.summary || pending.tool}
                </p>
              </div>
              <span className="flex-none rounded bg-violet-400/15 px-1.5 py-0.5 font-mono text-[10.5px] text-violet-200">
                {pending.tool}
              </span>
            </div>

            {pending.diff ? (
              <pre className="max-h-[46vh] overflow-auto border-b border-glass-border bg-black/25 px-3.5 py-3 font-mono text-[11.5px] leading-[1.55]">
                {pending.diff}
              </pre>
            ) : (
              <div className="border-b border-glass-border px-3.5 py-4 text-[12px] text-zinc-muted">
                No preview could be built for this change. Review the tool call
                ({pending.tool}) before you allow it — a missing diff is not a
                reason to allow it blind.
              </div>
            )}

            <div className="flex items-center justify-between gap-3 px-3.5 py-3">
              <p className="text-[11px] text-zinc-500">
                Declining tells the agent you refused, and it continues.
              </p>
              <div className="flex flex-none items-center gap-2">
                <button
                  type="button" disabled={busy || deciding} onClick={() => decide(false)}
                  className="flex items-center gap-1.5 rounded border border-glass-border px-2.5 py-1.5 text-[12px] text-zinc-300 hover:bg-white/5 disabled:opacity-40"
                >
                  <X size={13} /> Decline
                </button>
                <button
                  ref={acceptRef} type="button" disabled={busy || deciding}
                  onClick={() => decide(true)}
                  className="flex items-center gap-1.5 rounded bg-violet-500/90 px-2.5 py-1.5 text-[12px] font-medium text-white hover:bg-violet-500 disabled:opacity-40"
                >
                  <Check size={13} /> Accept
                </button>
              </div>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
