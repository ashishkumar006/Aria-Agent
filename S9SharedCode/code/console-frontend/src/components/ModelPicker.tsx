import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type KiloModel, type ModelRoster } from '../api';

/* ── model picker ────────────────────────────────────────────────────────── */

/** The roster fetch lives here rather than in the caller so the control owns
 *  its own failure state: an unreachable gateway has to disable the select and
 *  say why, and that message belongs next to the control it applies to.
 *
 *  Shared by the Code panel, Chat and any section that needs a model choice,
 *  so all of them read the same catalogue, group free models first and honour
 *  the gateway's default identically. */
export function ModelPicker({ model, onChange, busy, onMeta, label, applyDefault = true }: {
  model: string;
  onChange?: (id: string) => void;
  busy: boolean;
  /** Reports the selected model's context window, so the panel can show a
   *  context meter. "Auto" resolves to the gateway default, so the meter means
   *  something before the first reply. */
  onMeta?: (m: { contextLength: number; id: string }) => void;
  /** Accessible name for the control. Sections differ in what the pick applies
   *  to ("the coding agent" vs "chat"), and screen readers should hear which. */
  label?: string;
  /** Adopt the roster's default model when nothing is chosen yet. Correct for
   *  the Code section (whose default is meaningful) and WRONG for Chat, where
   *  the roster default is a *code* model: leaving Chat on "Auto" must let the
   *  gateway route for `agent="chat"` rather than silently pin the code model. */
  applyDefault?: boolean;
}) {
  const [roster, setRoster] = useState<ModelRoster | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async (refresh: boolean) => {
    try {
      // all=true: the whole catalogue. Free ones are grouped on top, but a
      // picker that only offered the free tier would hide the ~380 models a
      // paid key unlocks - which is the whole reason to look at a catalogue.
      const r = await api.models(refresh, true);
      setRoster(r);
      setErr(r.error ?? null);
    } catch (e) {
      setRoster(null);
      setErr(e instanceof Error ? e.message : 'could not reach the gateway');
    }
  }, []);

  useEffect(() => { void load(false); }, [load]);

  // Pre-select the gateway's current default so a fresh section runs on the
  // intended model rather than on whatever the general ladder would have
  // chosen. Deliberately one-shot per roster load: it fires only while the
  // picker is still unset, so it can never stomp a choice the user made or a
  // stored preference.
  const appliedDefault = useRef<string | null>(null);
  useEffect(() => {
    if (!applyDefault) return;
    if (!onChange || model || !roster?.default) return;
    if (appliedDefault.current === roster.default) return;
    // Only adopt a default that is actually selectable, or the <select> would
    // hold a value with no matching <option> and render blank.
    if (!roster.models.some((m) => m.id === roster.default)) return;
    appliedDefault.current = roster.default;
    onChange(roster.default);
  }, [roster, model, onChange, applyDefault]);

  // Free first, then the rest. An <optgroup> is the only native way to
  // actually separate them in a <select> - it renders as a labelled, visually
  // distinct band rather than relying on a text prefix in every option.
  // Paid models are capped so the list stays navigable, with an opt-in to see
  // the remainder; type-ahead still works across the whole list either way.
  const [expanded, setExpanded] = useState(false);
  const all = roster?.models ?? [];
  const free = all.filter((m) => m.is_free);
  const paid = all.filter((m) => !m.is_free);
  const shownPaid = expanded ? paid : paid.slice(0, 60);

  // Report the selected model's context window. Depends on `roster` (stable
  // state) rather than the derived `all` array, which is a new identity every
  // render and would re-run this effect continuously.
  useEffect(() => {
    if (!onMeta) return;
    const models = roster?.models ?? [];
    const sel = models.find((m) => m.id === model)
      ?? models.find((m) => m.id === roster?.default);
    onMeta({ contextLength: sel?.context_length ?? 0, id: sel?.id ?? model });
  }, [roster, model, onMeta]);

  /* A 78-entry list of bare ids - "reflection/beam-free", "openai/gpt-5.6-luna-pro"
     - is technically complete and practically unreadable: the vendor is buried
     in the slug, there is no way to tell two same-vendor models apart at a
     glance, and only the FREE half carried any cost signal, which read as
     "the paid ones are free" rather than the reverse.

     So each row is: a cost marker, a readable name, the vendor, and the
     context window. The id is still the VALUE, so nothing about routing
     changes - this is only about making the choice legible. The id is kept as
     a title too, because it is the only field that identifies the model
     unambiguously once the friendly name is shown. */
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
  /* The closed <select> can show roughly twenty characters before the browser
     ellipsises it, so the model name and vendor win; cost still leads because
     "free vs paid" is the one distinction a glance must get right. The context
     window moved to the title — it is reference data, not a label. */
  const label_of = (m: KiloModel) =>
    `${cost_of(m)}  ${pretty(m)}  ·  ${m.vendor || '—'}`;
  const title_of = (m: KiloModel) =>
    `${m.id} — ${m.vendor || '—'} · ${ctx(m)} context — ${m.description || ''}`.trim();

  return (
    <span className="flex min-w-0 items-center gap-1">
      <select
        value={model}
        disabled={busy || !onChange || all.length === 0}
        onChange={(e) => onChange?.(e.target.value)}
        title={
          err
            ? `model roster unavailable: ${err}`
            : `${all.length} model(s): ${free.length} free, ${paid.length} paid`
        }
        aria-label={label ?? 'Model for the coding agent'}
        className="min-w-0 max-w-[12rem] truncate rounded-md border border-white/10 bg-black/40 px-1.5 py-1 text-[11px] text-zinc-100 outline-none focus:border-violet-400 disabled:opacity-40"
      >
        <option value="">Auto (gateway default)</option>
        {free.length > 0 && (
          <optgroup label={`Free — ${free.length} models`}>
            {free.map((m) => (
              <option key={m.id} value={m.id} title={title_of(m)}>{label_of(m)}</option>
            ))}
          </optgroup>
        )}
        {shownPaid.length > 0 && (
          <optgroup label={`Paid — ${paid.length} total`}>
            {shownPaid.map((m) => (
              <option key={m.id} value={m.id} title={title_of(m)}>{label_of(m)}</option>
            ))}
          </optgroup>
        )}
      </select>
      {paid.length > shownPaid.length && (
        <button type="button" onClick={() => setExpanded(true)}
                title={`Show the remaining ${paid.length - shownPaid.length} models`}
                className="flex-none whitespace-nowrap text-[10px] text-zinc-muted hover:text-zinc-200">
          +{paid.length - shownPaid.length} more
        </button>
      )}
    </span>
  );
}
