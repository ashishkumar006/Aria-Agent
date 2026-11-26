import { useEffect, useState } from 'react';
import { api, type KiloModel, type ModelRoster } from '../api';

/* The studio's model list as a plain list of menu items.
 *
 * ModelPicker renders a native <select>, which is the right control for
 * static screens (Chat, Authoring) but the wrong one inside the composer's
 * menu: the user had to click the pill, then click the <select>, then read
 * a browser-native popup. This renders the same catalogue as rows, so the
 * choice is one click from the pill and one click from the answer — the same
 * shape as the composer's other menus. Free models lead, paid models capped
 * behind an explicit expand, the id is the cost of truth as the VALUE. */

export function ModelMenu({ model, onSelect, busy, label }: {
  model: string;
  onSelect: (id: string) => void;
  busy?: boolean;
  /** Accessible name for the control — Sections differ in what the pick applies to. */
  label?: string;
}) {
  const [roster, setRoster] = useState<ModelRoster | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let live = true;
    void api.models(false, true)
      .then((r) => { if (!live) return; setRoster(r); setErr(r.error ?? null); })
      .catch((e) => { if (!live) return; setRoster(null);
        setErr(e instanceof Error ? e.message : 'could not reach the gateway'); });
    return () => { live = false; };
  }, []);

  const all = roster?.models ?? [];
  const free = all.filter((m) => m.is_free);
  const paid = all.filter((m) => !m.is_free);
  const shownPaid = expanded ? paid : paid.slice(0, 50);

  const cost_of = (m: KiloModel) => (m.is_free ? 'FREE' : 'PAID');
  const pretty = (m: KiloModel) => {
    const tail = m.id.includes('/') ? m.id.slice(m.id.indexOf('/') + 1) : m.id;
    return tail
      .replace(/:free$/, '')
      .replace(/-/g, ' ')
      .replace(/\bv(\d+)\b/g, 'v$1')
      .trim();
  };
  const ctx = (m: KiloModel) =>
    m.context_length >= 1000 ? `${Math.round(m.context_length / 1000)}k` : `${m.context_length}`;
  const title_of = (m: KiloModel) =>
    `${m.id} — ${m.vendor || '—'} · ${ctx(m)} context${m.description ? ' — ' + m.description : ''}`;

  const Row = ({ m }: { m: KiloModel }) => (
    <button key={m.id} type="button" role="menuitem"
            onClick={() => onSelect(m.id)}
            disabled={busy}
            className={'block w-full px-3 py-1.5 text-left transition-colors '
              + (model === m.id
                ? 'bg-white/10 text-zinc-50'
                : 'text-zinc-300 hover:bg-white/5 hover:text-zinc-100')}>
      <span className="flex items-baseline gap-2">
        <span className={'flex-none text-[9.5px] font-bold uppercase tracking-wide '
          + (m.is_free ? 'text-ok' : 'text-warn')}>{cost_of(m)}</span>
        <span className="min-w-0 truncate text-[12px] font-semibold">{pretty(m)}</span>
        <span className="flex-none text-[11px] text-zinc-muted">
          {m.vendor || '—'} · {ctx(m)}
        </span>
      </span>
      <span className="sr-only">{title_of(m)}</span>
    </button>
  );

  return (
    <div role="menu" aria-label={label ?? 'Model for the coding agent'}
         className="max-h-[320px] overflow-auto py-1">
      <button type="button" role="menuitem"
              onClick={() => onSelect('')}
              disabled={busy}
              className={'block w-full px-3 py-1.5 text-left transition-colors '
                + (!model ? 'bg-white/10 text-zinc-50' : 'text-zinc-300 hover:bg-white/5 hover:text-zinc-100')}>
        <span className="block text-[12px] font-semibold">Auto</span>
        <span className="block text-[11px] text-zinc-muted">the gateway routes this one</span>
      </button>
      {free.length > 0 && (
        <>
          <p className="px-3 pb-1 pt-2 text-[10px] font-bold uppercase tracking-[0.14em] text-zinc-muted">
            Free — {free.length}
          </p>
          {free.map((m) => <Row key={m.id} m={m} />)}
        </>
      )}
      {shownPaid.length > 0 && (
        <>
          <p className="px-3 pb-1 pt-2 text-[10px] font-bold uppercase tracking-[0.14em] text-zinc-muted">
            Paid — {paid.length}
          </p>
          {shownPaid.map((m) => <Row key={m.id} m={m} />)}
        </>
      )}
      {paid.length > shownPaid.length && (
        <button type="button" role="menuitem" onClick={() => setExpanded(true)}
                className="block w-full px-3 py-1.5 text-left text-[11px] text-zinc-muted hover:bg-white/5 hover:text-zinc-200">
          show the remaining {paid.length - shownPaid.length} paid models
        </button>
      )}
      {err && (
        <p className="px-3 py-1.5 text-[11px] text-warn">roster unavailable: {err}</p>
      )}
      {!roster && !err && (
        <p className="px-3 py-1.5 text-[11px] text-zinc-muted">loading the catalogue…</p>
      )}
    </div>
  );
}
