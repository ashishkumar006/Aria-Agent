import {
  createContext, useContext, useEffect, useMemo, useState,
} from 'react';
import type { A2uiComponent, A2uiProps, A2uiSurface, A2uiActionHandler } from '../api';
import { Stat, Pill } from './ui';

export type { A2uiComponent, A2uiProps, A2uiSurface, A2uiActionHandler };

/* Tone words are the catalog's; Stat/Pill speak the console's. */
const STAT_TONE: Record<string, 'ok' | 'warn' | 'bad' | undefined> = {
  good: 'ok', warn: 'warn', bad: 'bad', neutral: undefined,
};
const PILL_TONE: Record<string, 'ok' | 'err' | 'warn' | 'info' | 'muted'> = {
  good: 'ok', warn: 'warn', bad: 'err', info: 'info', neutral: 'muted',
};

/* A2UI surface renderer.

   The catalog is declarative and closed: `a2ui_catalog.validate_surface` has
   already rejected anything unknown, so this file renders a fixed set of
   components and has no "execute whatever the payload said" path. That is the
   whole security model - there is no dangerouslySetInnerHTML, no src/href, and
   every string arrives as a React text node.

   `Table` and `Sparkline` exist because the Apps board needs them: without a
   real table, `DataList` (a flat label/value list) could not render an app's
   columns, so A2UI would have been a downgrade of the prefab renderer rather
   than a peer of it. */

function str(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  return String(v);
}

function num(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v.trim() && Number.isFinite(Number(v))) return Number(v);
  return null;
}

/* ── Sparkline ──────────────────────────────────────────────────────────────
   A real polyline, not a bar chart: the point is shape-over-time at 74px wide.
   `preserveAspectRatio="none"` on a viewBox is what lets 1 data point still
   draw a flat line instead of an empty box. */
function Sparkline({ series, label, max }: { series: number[]; label?: string; max?: number | null }) {
  const w = 240, h = 40;
  const path = useMemo(() => {
    const pts = series.map(num).filter((n): n is number => n !== null);
    if (pts.length === 0) return '';
    if (pts.length === 1) return `M0,${h / 2} L${w},${h / 2}`;
    const lo = Math.min(...pts);
    const rawHi = Math.max(...pts);
    // A declared `max` is the scale's ceiling (e.g. 0..1 progress): without it
    // every series is normalised to its own peak, so "0.42 of 1" and "0.98 of
    // 1" draw the same line. Data above the declared max still wins, so the
    // path can never leave the box.
    const hi = max != null && Number.isFinite(max) && max > lo ? Math.max(max, rawHi) : rawHi;
    const span = hi - lo || 1;
    return pts
      .map((v, i) => {
        const x = (i / (pts.length - 1)) * w;
        const y = h - ((v - lo) / span) * (h - 4) - 2;
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  }, [series, max]);

  if (!path) return null;
  return (
    <figure className="m-0">
      {/* max-w: the series is 40 device pixels tall, so stretched across a
          1200px card it flattens into a hairline that reads as a rule, not a
          trend. Bounded, it stays a sparkline — the shape the label describes. */}
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"
        className="h-10 w-full max-w-[420px]" role="img"
        aria-label={`${label || 'series'}: ${series.length} points, from ${series[0]} to ${series[series.length - 1]}`}>
        <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5"
          vectorEffect="non-scaling-stroke" className="text-accent" />
      </svg>
      {label ? (
        <figcaption className="text-[10.5px] text-zinc-muted">{label}</figcaption>
      ) : null}
    </figure>
  );
}

/* ── Table ──────────────────────────────────────────────────────────────────
   Search + sort + paging live here rather than in the payload. The surface
   carries data and column keys; how it is interacted with is the console's
   business, and baking a page size into a generated payload would mean the
   model could not get it wrong because it is not in the payload at all. */
function Table({ columns, rows, rowKey, searchable, paginated, emptyText }: {
  columns: { key: string; label: string }[];
  rows: Record<string, unknown>[];
  rowKey?: string;
  searchable?: boolean;
  paginated?: boolean;
  emptyText?: string;
}) {
  const [q, setQ] = useState('');
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const [page, setPage] = useState(0);
  const per = 25;

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    let out = rows;
    if (needle) {
      out = out.filter((r) => columns.some((c) => str(r[c.key]).toLowerCase().includes(needle)));
    }
    if (sort) {
      out = [...out].sort((a, b) => {
        const av = a[sort.key], bv = b[sort.key];
        const an = num(av), bn = num(bv);
        if (an !== null && bn !== null) return (an - bn) * sort.dir;
        return str(av).localeCompare(str(bv)) * sort.dir;
      });
    }
    return out;
  }, [rows, q, sort, columns]);

  const pages = paginated ? Math.max(1, Math.ceil(filtered.length / per)) : 1;
  const shown = paginated ? filtered.slice(page * per, page * per + per) : filtered;

  /* Right-align a column whose every non-empty value is a number. A
     "Questions"/"Size"/"Count" column reads far faster flush right, and the
     catalog carries no alignment prop for Table, so the data itself is the
     only honest signal. */
  const numeric = useMemo(() => {
    const keys = new Set<string>();
    for (const c of columns) {
      const vals = filtered.map((r) => str(r[c.key]).trim()).filter(Boolean);
      if (vals.length > 0 && vals.every((v) => num(v) !== null)) keys.add(c.key);
    }
    return keys;
  }, [columns, filtered]);

  // A stale page index after the data changes makes the table lie: paged to
  // page 3 of a 3-page feed, then a refresh shrinks it to one page — `shown`
  // becomes empty and the panel prints "No items" while rows exist.
  //
  // Object identity is not data identity: the parent rebuilds `columns`, and
  // the transcript poll rebuilds the surface, on every tick. Depending on the
  // arrays themselves reset the page every 1.5s while the user was reading.
  // Compare their contents instead.
  const dataKey = useMemo(() => JSON.stringify([columns, rows]), [columns, rows]);
  useEffect(() => setPage(0), [dataKey]);
  useEffect(() => {
    if (page >= pages) setPage(pages - 1);
  }, [page, pages]);

  return (
    <div className="min-w-0">
      {/* A filter row above a four-row table is chrome, not a tool: a
          dashboard with three small tables ended up with three search boxes
          and looked like a form. Deep tables keep theirs. */}
      {searchable && rows.length > 8 ? (
        <input
          value={q}
          onChange={(e) => { setQ(e.target.value); setPage(0); }}
          placeholder="filter rows…"
          aria-label="Filter table rows"
          className="mb-1.5 w-full rounded border border-glass-border bg-surface-0 px-2 py-1 text-[11.5px] text-zinc-100 outline-none focus:border-accent/50"
        />
      ) : null}

      {shown.length === 0 ? (
        <p className="py-2 text-[11.5px] text-zinc-muted">{emptyText || 'Nothing to show.'}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-[11.5px]">
            <thead>
              <tr>
                {/* `aria-sort` belongs on the columnheader, not on the button
                    inside it. On a button it is not an allowed ARIA attribute
                    (Lighthouse flagged it), and a screen reader announcing the
                    header would never see it. */}
                {columns.map((c) => (
                  <th key={c.key} scope="col"
                    aria-sort={sort?.key === c.key ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'}
                    className={`border-b border-glass-border bg-white/[0.02] px-2 py-1.5 font-medium text-zinc-400 ${numeric.has(c.key) ? 'text-right' : ''}`}>
                    <button
                      type="button"
                      onClick={() => setSort((s) => (s?.key === c.key ? (s.dir === 1 ? { key: c.key, dir: -1 } : null) : { key: c.key, dir: 1 }))}
                      className="inline-flex items-center gap-1 transition-colors hover:text-zinc-100"
                      aria-label={`Sort by ${c.label}`}
                    >
                      {c.label}
                      {sort?.key === c.key ? <span aria-hidden>{sort.dir === 1 ? '▲' : '▼'}</span> : null}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((r, i) => {
                const key = rowKey && r[rowKey] !== undefined ? str(r[rowKey]) : String(i);
                return (
                  <tr key={key} className="row-in border-b border-glass-border/40 transition-colors last:border-b-0 hover:bg-white/[0.035]">
                    {columns.map((c) => {
                      const isNew = c.key === '_new' && str(r[c.key]) === 'yes';
                      return (
                        <td key={c.key}
                          className={`px-2 py-1.5 align-top text-zinc-200 ${numeric.has(c.key) ? 'text-right tabular-nums' : ''}`}>
                          {isNew ? <span className="text-accent">{str(r[c.key])}</span> : str(r[c.key])}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {pages > 1 ? (
        <div className="mt-1.5 flex items-center gap-2 text-[11px] text-zinc-muted">
          <button type="button" disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            className="rounded border border-glass-border px-1.5 py-0.5 disabled:opacity-40">Prev</button>
          <span>page {page + 1} of {pages} · {filtered.length} rows</span>
          <button type="button" disabled={page >= pages - 1}
            onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
            className="rounded border border-glass-border px-1.5 py-0.5 disabled:opacity-40">Next</button>
        </div>
      ) : null}
    </div>
  );
}

/* Inputs live at surface level: every Input contributes to one value bag, and
   an Action submits that bag under `input`. The bag is plain strings and never
   executes anything; it is only sent back to the app's own action endpoint. */
const InputsContext = createContext<{
  values: Record<string, string>;
  set: (name: string, value: string) => void;
}>({ values: {}, set: () => { /* no surface */ } });

const FIELD = 'w-full rounded-md border border-glass-border bg-black/30 px-2 py-1.5 text-[12px] text-zinc-200 outline-none focus:border-accent';

function InputField({ c }: { c: A2uiComponent }) {
  const { values, set } = useContext(InputsContext);
  const name = str(c.props.name) || c.id;
  const label = str(c.props.label);
  const value = values[name] ?? str(c.props.value);
  const kind = str(c.props.kind)
    || (c.props.multiline === true ? 'textarea' : 'text');
  return (
    <label className="flex min-w-[160px] flex-col gap-1 text-[10.5px] text-zinc-500">
      {label ? <span>{label}</span> : null}
      {kind === 'textarea'
        ? <textarea rows={3} value={value} onChange={(e) => set(name, e.target.value)}
            aria-label={label || name} placeholder={str(c.props.placeholder)}
            className={FIELD} />
        : <input type={kind === 'number' ? 'number' : 'text'} value={value}
            onChange={(e) => set(name, e.target.value)}
            aria-label={label || name} placeholder={str(c.props.placeholder)}
            className={FIELD} />}
    </label>
  );
}

function optionsOf(c: A2uiComponent): { value: string; label: string }[] {
  return ((c.props.options as unknown[]) || []).map((o) => {
    if (o && typeof o === 'object') {
      const row = o as Record<string, unknown>;
      const value = str(row.value ?? row.id);
      return { value, label: str(row.label ?? value) };
    }
    return { value: str(o), label: str(o) };
  }).filter((o) => o.value !== '');
}

function SelectField({ c }: { c: A2uiComponent }) {
  const { values, set } = useContext(InputsContext);
  const name = str(c.props.name) || c.id;
  const options = optionsOf(c);
  const current = values[name] ?? str(c.props.value) ?? options[0]?.value ?? '';
  return (
    <label className="flex min-w-[160px] flex-col gap-1 text-[10.5px] text-zinc-500">
      {str(c.props.label) ? <span>{str(c.props.label)}</span> : null}
      <select value={current} onChange={(e) => set(name, e.target.value)}
        aria-label={str(c.props.label) || name} className={FIELD}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

function CheckboxField({ c }: { c: A2uiComponent }) {
  const { values, set } = useContext(InputsContext);
  const name = str(c.props.name) || c.id;
  const checked = (values[name] ?? String(c.props.checked === true)) === 'true';
  return (
    <label className="flex items-center gap-2 text-[12px] text-zinc-300">
      <input type="checkbox" checked={checked}
        onChange={(e) => set(name, e.target.checked ? 'true' : 'false')}
        aria-label={str(c.props.label) || name}
        className="h-3.5 w-3.5 accent-violet-400" />
      {str(c.props.label)}
    </label>
  );
}

function ToggleField({ c }: { c: A2uiComponent }) {
  const { values, set } = useContext(InputsContext);
  const name = str(c.props.name) || c.id;
  const on = (values[name] ?? String(c.props.checked === true)) === 'true';
  return (
    <div className="flex items-center gap-2 text-[12px] text-zinc-300">
      <button type="button" role="switch" aria-checked={on}
        aria-label={str(c.props.label) || name}
        onClick={() => set(name, on ? 'false' : 'true')}
        className={`relative h-5 w-9 rounded-full transition-colors ${on ? 'bg-accent' : 'bg-white/15'}`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${on ? 'left-[18px]' : 'left-0.5'}`} />
      </button>
      {str(c.props.label)}
    </div>
  );
}

function SliderField({ c }: { c: A2uiComponent }) {
  const { values, set } = useContext(InputsContext);
  const name = str(c.props.name) || c.id;
  const min = num(c.props.min) ?? 0;
  const max = num(c.props.max) ?? 100;
  const step = num(c.props.step) ?? 1;
  const current = values[name] ?? (str(c.props.value) || String(min));
  return (
    <label className="flex min-w-[160px] flex-col gap-1 text-[10.5px] text-zinc-500">
      <span className="flex items-center justify-between gap-2">
        <span>{str(c.props.label)}</span>
        <span className="tabular-nums text-zinc-300">{current}</span>
      </span>
      <input type="range" min={min} max={max} step={step} value={current}
        onChange={(e) => set(name, e.target.value)}
        aria-label={str(c.props.label) || name}
        className="accent-violet-400" />
    </label>
  );
}

function RadioGroupField({ c }: { c: A2uiComponent }) {
  const { values, set } = useContext(InputsContext);
  const name = str(c.props.name) || c.id;
  const options = optionsOf(c);
  const current = values[name] ?? str(c.props.value) ?? options[0]?.value ?? '';
  return (
    <fieldset className="flex flex-col gap-1 text-[10.5px] text-zinc-500">
      <legend>{str(c.props.label)}</legend>
      <div className="flex flex-wrap gap-3">
        {options.map((o) => (
          <label key={o.value} className="flex items-center gap-1.5 text-[12px] text-zinc-300">
            <input type="radio" name={name} value={o.value} checked={current === o.value}
              onChange={() => set(name, o.value)}
              className="accent-violet-400" />
            {o.label}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function FilterChipsField({ c, onAction }: { c: A2uiComponent; onAction?: A2uiActionHandler }) {
  const { values } = useContext(InputsContext);
  const action = str(c.props.action);
  const options = ((c.props.options as unknown[]) || []).map((raw) => {
    const o = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
    return {
      id: str(o.id ?? o.value),
      label: str(o.label ?? o.id ?? o.value),
      selected: o.selected === true,
    };
  }).filter((o) => o.id);
  if (!options.length) return <p className="m-0 text-[11.5px] text-zinc-500">No filters.</p>;
  const enabled = !!onAction && !!action;
  return (
    <div role="group" className="flex flex-wrap gap-1">
      {options.map((o) => (
        <button key={o.id} type="button" aria-pressed={o.selected} disabled={!enabled}
          onClick={() => { if (enabled) onAction(action, { input: { ...values, filter: o.id } }); }}
          className={`rounded-full border px-2 py-0.5 text-[10.5px] transition ${
            o.selected ? 'border-accent/50 bg-accent/10 text-accent' : 'border-glass-border text-zinc-muted'
          } ${enabled ? 'hover:border-white/25 hover:text-zinc-200' : ''}`}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

function ActionButton({ c, onAction }: { c: A2uiComponent; onAction?: A2uiActionHandler }) {
  const { values } = useContext(InputsContext);
  const press = (c.props.onPress as Record<string, unknown> | undefined) || {};
  const action = str(c.props.action || press.action || '');
  const args = (c.props.args ?? press.args ?? press.params ?? {}) as Record<string, unknown>;
  const primary = str(c.props.variant) === 'primary';
  const primaryCls = 'rounded-md bg-accent px-3 py-1.5 text-[11.5px] font-semibold text-[#0b0b0e] transition hover:brightness-110 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60';
  const secondaryCls = 'rounded-md border border-glass-border bg-white/[0.03] px-3 py-1.5 text-[11.5px] text-zinc-200 transition hover:border-white/20 hover:bg-white/[0.07] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40';
  if (onAction && action) {
    return (
      <button
        type="button"
        onClick={() => onAction(action, { ...args, input: { ...values } })}
        title={str(c.props.tooltip || '')}
        className={primary ? primaryCls : secondaryCls}
      >
        {str(c.props.label)}
      </button>
    );
  }
  return (
    <button type="button" disabled
      title="Agent-generated actions are not enabled"
      className={`${primary ? primaryCls : secondaryCls} cursor-not-allowed opacity-60`}>
      {str(c.props.label)}
    </button>
  );
}

function StringList({ c, ordered }: { c: A2uiComponent; ordered?: boolean }) {
  const items = ((c.props.items as unknown[]) || []).map(str).filter(Boolean);
  if (!items.length) return <p className="m-0 text-[11.5px] text-zinc-500">No items.</p>;
  const List = ordered ? 'ol' : 'ul';
  return (
    <List className={`m-0 space-y-1 pl-4 text-[12px] leading-relaxed text-zinc-300 ${
      ordered ? 'list-decimal' : 'list-disc'}`}>
      {items.map((item, i) => <li key={i}>{item}</li>)}
    </List>
  );
}

const CALLOUT_TONE: Record<string, string> = {
  info: 'border-sky-400/30 bg-sky-400/5 text-sky-100',
  good: 'border-emerald-400/30 bg-emerald-400/5 text-emerald-100',
  warn: 'border-amber-400/30 bg-amber-400/5 text-amber-100',
  bad: 'border-rose-400/30 bg-rose-400/5 text-rose-100',
  neutral: 'border-glass-border bg-white/[0.03] text-zinc-200',
};

function Callout({ c }: { c: A2uiComponent }) {
  const tone = CALLOUT_TONE[str(c.props.tone)] || CALLOUT_TONE.neutral;
  return (
    <div className={`rounded-md border px-3 py-2 ${tone}`}>
      {str(c.props.title) ? <div className="text-[11.5px] font-semibold">{str(c.props.title)}</div> : null}
      <p className="m-0 whitespace-pre-wrap text-[11.5px] leading-relaxed opacity-90">{str(c.props.text)}</p>
    </div>
  );
}

const CHART_COLORS = ['#8b7cf6', '#38bdf8', '#34d399', '#fbbf24', '#fb7185', '#a3e635', '#f472b6'];

function BarChart({ c }: { c: A2uiComponent }) {
  const items = ((c.props.items as unknown[]) || []).map((raw) => {
    const o = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
    return { label: str(o.label), value: num(o.value) ?? 0 };
  }).filter((o) => o.label);
  const declared = num(c.props.max);
  const max = declared != null && declared > 0
    ? declared
    : Math.max(1, ...items.map((i) => i.value));
  if (!items.length) return <p className="m-0 text-[11.5px] text-zinc-500">No chart data.</p>;
  return (
    <div className="space-y-1.5">
      {items.map((item, i) => (
        <div key={`${item.label}-${i}`} className="flex items-center gap-2 text-[11px]">
          <span className="w-24 flex-none truncate text-zinc-400" title={item.label}>{item.label}</span>
          <span className="h-2 flex-1 overflow-hidden rounded bg-white/10">
            <span className="block h-full rounded bg-accent"
              style={{ width: `${Math.min(100, (item.value / max) * 100)}%` }} />
          </span>
          <span className="w-12 flex-none text-right tabular-nums text-zinc-300">{item.value}</span>
        </div>
      ))}
    </div>
  );
}

function DonutChart({ c }: { c: A2uiComponent }) {
  const segments = ((c.props.segments as unknown[]) || []).map((raw) => {
    const o = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
    return { label: str(o.label), value: Math.max(0, num(o.value) ?? 0) };
  }).filter((s) => s.label);
  const total = segments.reduce((a, s) => a + s.value, 0);
  if (!total) return <p className="m-0 text-[11.5px] text-zinc-500">No chart data.</p>;
  let acc = 0;
  const stops = segments.map((s, i) => {
    const from = (acc / total) * 360;
    acc += s.value;
    const to = (acc / total) * 360;
    return `${CHART_COLORS[i % CHART_COLORS.length]} ${from}deg ${to}deg`;
  }).join(', ');
  return (
    <div className="flex items-center gap-3">
      <div className="relative h-20 w-20 flex-none rounded-full"
           style={{ background: `conic-gradient(${stops})` }}>
        <div className="absolute inset-[22%] flex items-center justify-center rounded-full bg-surface-1 text-[10.5px] font-semibold text-zinc-200">
          {str(c.props.center) || total}
        </div>
      </div>
      <ul className="min-w-0 space-y-0.5 text-[10.5px] text-zinc-400">
        {segments.map((s, i) => (
          <li key={`${s.label}-${i}`} className="flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 flex-none rounded-full" style={{ background: CHART_COLORS[i % CHART_COLORS.length] }} />
            <span className="truncate">{s.label}</span>
            <span className="ml-auto tabular-nums text-zinc-300">{s.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Gauge({ c }: { c: A2uiComponent }) {
  const min = num(c.props.min) ?? 0;
  const max = num(c.props.max) ?? 100;
  const value = num(c.props.value) ?? min;
  const pct = max > min ? Math.max(0, Math.min(1, (value - min) / (max - min))) : 0;
  return (
    <div className="min-w-[140px]">
      <div className="flex items-baseline justify-between text-[11px]">
        <span className="text-zinc-400">{str(c.props.label)}</span>
        <span className="font-semibold tabular-nums text-zinc-100">{value}</span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded bg-white/10">
        <div className={`h-full rounded transition-all ${
          pct >= 0.7 ? 'bg-emerald-400' : pct >= 0.35 ? 'bg-amber-300' : 'bg-rose-400'}`}
          style={{ width: `${Math.round(pct * 100)}%` }} />
      </div>
    </div>
  );
}

function Timeline({ c }: { c: A2uiComponent }) {
  const items = ((c.props.items as unknown[]) || []).map((raw) => {
    const o = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
    return { title: str(o.title), detail: str(o.detail), time: str(o.time) };
  }).filter((o) => o.title || o.detail);
  if (!items.length) return <p className="m-0 text-[11.5px] text-zinc-500">No events.</p>;
  return (
    <ol className="m-0 space-y-2 border-l border-white/10 pl-3">
      {items.map((item, i) => (
        <li key={i} className="relative">
          <span className="absolute -left-[17px] top-1 h-2 w-2 rounded-full border border-surface-1 bg-accent" />
          <div className="flex items-baseline gap-2">
            <span className="text-[11.5px] font-semibold text-zinc-200">{item.title}</span>
            {item.time && <span className="text-[10px] text-zinc-500">{item.time}</span>}
          </div>
          {item.detail && <p className="m-0 mt-0.5 text-[11px] text-zinc-500">{item.detail}</p>}
        </li>
      ))}
    </ol>
  );
}

/* ── everything else ──────────────────────────────────────────────────────── */
function Leaf({ c, onAction }: { c: A2uiComponent; onAction?: A2uiActionHandler }) {
  const p = c.props;
  switch (c.component) {
    case 'Text': {
      /* `variant` is the catalog's whole point for Text: without it a
         dashboard title, its section headings and its body copy all rendered
         as identical 12px paragraphs, which is why a correct surface still
         looked like a wall of text. */
      const variant = str(p.variant) || 'body';
      const text = str(p.text);
      if (variant === 'heading') {
        return <h4 className="m-0 whitespace-pre-wrap text-[13.5px] font-semibold tracking-tight text-zinc-50">{text}</h4>;
      }
      if (variant === 'caption') {
        return <p className="m-0 whitespace-pre-wrap text-[10.5px] leading-snug text-zinc-muted">{text}</p>;
      }
      if (variant === 'mono') {
        return <p className="m-0 whitespace-pre-wrap font-mono text-[11.5px] leading-relaxed text-zinc-200">{text}</p>;
      }
      return <p className="m-0 whitespace-pre-wrap text-[12px] leading-relaxed text-zinc-200">{text}</p>;
    }
    case 'CodeBlock':
      return <pre className="m-0 overflow-x-auto rounded bg-surface-0 p-2 text-[11.5px] text-zinc-200"><code>{str(p.text)}</code></pre>;
    case 'Badge':
      return <Pill tone={PILL_TONE[str(p.tone)] || 'muted'}>{str(p.text)}</Pill>;
    case 'KeyValue':
      /* KeyValue is documented as "the console's Stat", so render exactly
         that: a real metric card, with the tone preserved. The old markup
         silently dropped `tone`, so "0% progress" in warn looked the same as
         a healthy number. */
      return <Stat k={str(p.label)} v={str(p.value)} tone={STAT_TONE[str(p.tone)]} />;
    case 'Divider':
      return str(p.axis) === 'vertical'
        ? <span role="separator" aria-orientation="vertical" className="mx-0.5 inline-block w-px self-stretch bg-glass-border" />
        : <hr className="my-1 border-0 border-t border-glass-border" />;
    case 'Spacer': {
      const size = str(p.size) || 'normal';
      return <div className={size === 'small' ? 'h-1' : size === 'large' ? 'h-5' : 'h-2'} />;
    }
    case 'Link':
      /* `target` is validated to enum:console - a surface cannot emit an
         external URL, so this never becomes an anchor to somewhere else. */
      return <span className="text-[11.5px] text-accent underline">{str(p.label)}</span>;
    case 'Table': {
      /* The catalog says {key,label}. Older chat payloads and AG-UI samples
         use {key,title}; without the fallback every header rendered blank,
         which looks like an empty grid rather than a table. */
      const cols = ((p.columns as { key: string; label?: string; title?: string }[]) || [])
        .map((c) => ({ key: c.key, label: c.label || c.title || c.key }));
      return (
        <Table
          columns={cols}
          rows={(p.rows as Record<string, unknown>[]) || []}
          rowKey={p.rowKey ? str(p.rowKey) : undefined}
          searchable={p.searchable !== false}
          paginated={p.paginated !== false}
          emptyText={p.emptyText ? str(p.emptyText) : undefined}
        />
      );
    }
    case 'Sparkline':
      return <Sparkline series={(p.series as number[]) || []} label={p.label ? str(p.label) : undefined} max={num(p.max)} />;
    case 'DataList': {
      const rows = (p.rows as { label: unknown; value: unknown }[]) || [];
      if (!rows.length) return <p className="m-0 text-[11.5px] text-zinc-muted">{str(p.emptyText) || 'Nothing to show.'}</p>;
      return (
        <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11.5px]">
          {rows.map((r, i) => (
            <div key={i} className="contents">
              <dt className="text-zinc-muted">{str(r.label)}</dt>
              <dd className="m-0 text-zinc-200">{str(r.value)}</dd>
            </div>
          ))}
        </dl>
      );
    }
    case 'Input':
      return <InputField c={c} />;
    case 'Select':
      return <SelectField c={c} />;
    case 'Checkbox':
      return <CheckboxField c={c} />;
    case 'Toggle':
      return <ToggleField c={c} />;
    case 'Slider':
      return <SliderField c={c} />;
    case 'RadioGroup':
      return <RadioGroupField c={c} />;
    case 'BulletList':
      return <StringList c={c} />;
    case 'NumberedList':
      return <StringList c={c} ordered />;
    case 'Quote':
      return (
        <blockquote className="m-0 border-l-2 border-accent/50 pl-3 text-[12px] italic leading-relaxed text-zinc-300">
          {str(p.text)}
          {str(p.cite) ? <footer className="mt-1 text-[10.5px] not-italic text-zinc-500">— {str(p.cite)}</footer> : null}
        </blockquote>
      );
    case 'Callout':
      return <Callout c={c} />;
    case 'TagList': {
      const tags = ((p.tags as unknown[]) || []).map(str).filter(Boolean);
      const tone = PILL_TONE[str(p.tone)] || 'muted';
      return <div className="flex flex-wrap gap-1">{tags.map((t, i) => <Pill key={`${t}-${i}`} tone={tone}>{t}</Pill>)}</div>;
    }
    case 'Avatar': {
      const name = str(p.name) || '?';
      const initials = name.split(/\s+/).filter(Boolean).slice(0, 2)
        .map((w) => w[0]?.toUpperCase() || '').join('');
      return (
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 flex-none items-center justify-center rounded-full bg-accent/20 text-[11px] font-bold text-violet-200">
            {initials || '?'}
          </span>
          <span className="min-w-0">
            <span className="block truncate text-[12px] font-semibold text-zinc-200">{name}</span>
            {str(p.label) && <span className="block truncate text-[10.5px] text-zinc-500">{str(p.label)}</span>}
          </span>
        </div>
      );
    }
    case 'Timeline':
      return <Timeline c={c} />;
    case 'BarChart':
      return <BarChart c={c} />;
    case 'DonutChart':
      return <DonutChart c={c} />;
    case 'Gauge':
      return <Gauge c={c} />;
    case 'Footer':
      return <p className="m-0 border-t border-glass-border pt-2 text-[10.5px] text-zinc-500">{str(p.text)}</p>;
    case 'Action':
    case 'Button':
      /* `Action` is the catalog name; `Button` is the AG-UI name older chat
         payloads used. ActionButton reads the surface's input bag and sends
         it with the event, so a generated form can submit real values. */
      return <ActionButton c={c} onAction={onAction} />;
    /* Legacy, display-only: these were advertised by an older chat prompt but
       are not in the catalog (which deliberately has no form submission). A
       stored surface from before the prompt was corrected still names them,
       and a read-only view of the value beats an invisible hole. */
    case 'TextField':
      return (
        <div className="rounded border border-glass-border bg-surface-0 px-2 py-1 text-[11.5px] text-zinc-muted">
          {str(p.value) || str(p.placeholder) || '…'}
        </div>
      );
    case 'Dropdown': {
      const opts = (p.options as { value: string; label: string }[]) || [];
      const sel = opts.find((o) => o.value === p.value);
      return (
        <div className="rounded border border-glass-border bg-surface-0 px-2 py-1 text-[11.5px] text-zinc-200">
          {str(sel?.label ?? p.value ?? 'Select…')}
        </div>
      );
    }
    case 'Checkbox':
      return (
        <span className="inline-flex items-center gap-1.5 text-[11.5px] text-zinc-200">
          <input type="checkbox" checked={!!p.checked} disabled
            aria-label={str(p.label) || 'checkbox'} />
          {str(p.label)}
        </span>
      );
    case 'Slider':
      return (
        <span className="inline-flex items-center gap-1.5 text-[11.5px] text-zinc-200">
          {str(p.label)}
          <span className="text-zinc-muted">
            {str(p.value)} ({str(p.min)}–{str(p.max)})
          </span>
        </span>
      );
    case 'FilterChips':
      return <FilterChipsField c={c} onAction={onAction} />;
    case 'StatusRow': {
      const items = (p.items as A2uiComponent[]) || [];
      return (
        <div className="flex flex-wrap items-center gap-1">
          {items.map((it, i) => {
            const props = (it.props || {}) as A2uiProps;
            const tone = str(props.tone) || str((it as unknown as { tone?: unknown }).tone);
            return (
              <Pill key={i} tone={PILL_TONE[tone] || 'muted'}>
                {str(props.text ?? (it as unknown as { text?: unknown }).text)}
              </Pill>
            );
          })}
        </div>
      );
    }
    case 'Empty':
      return (
        <div className="rounded border border-dashed border-glass-border px-3 py-4 text-center">
          <p className="m-0 text-[12px] text-zinc-200">{str(p.title)}</p>
          {p.sub ? <p className="m-0 mt-0.5 text-[11.5px] text-zinc-muted">{str(p.sub)}</p> : null}
        </div>
      );
    case 'Progress': {
      const v = num(p.value);
      const pct = v === null ? 0 : Math.max(0, Math.min(1, v));
      return (
        <div>
          {p.label ? <div className="mb-0.5 text-[11.5px] text-zinc-muted">{str(p.label)}</div> : null}
          <div className="h-1.5 w-full overflow-hidden rounded bg-glass-border"
            role="progressbar" aria-valuemin={0} aria-valuemax={1}
            aria-valuenow={pct}
            aria-label={p.label ? str(p.label) : 'Progress'}>
            <div className="h-full rounded bg-accent/70" style={{ width: `${pct * 100}%` }} />
          </div>
        </div>
      );
    }
    default:
      /* A validated surface never reaches this branch, but a stored surface
         written before the catalog was enforced can. Rendering nothing made
         the component vanish silently (the "A2UI is not rendering" bug);
         naming it keeps the gap visible and debuggable. */
      return (
        <p className="m-0 text-[11px] text-zinc-muted">
          [{str(c.component)}]
        </p>
      );
  }
}

function Node({ c, byId, depth, onAction }: { c: A2uiComponent; byId: Map<string, A2uiComponent>; depth: number; onAction?: A2uiActionHandler }) {
  const kids = ((c.props.children as string[]) || []).map((id) => byId.get(id)).filter(Boolean) as A2uiComponent[];
  const gap = c.props.gap === 'tight' ? 'gap-1' : c.props.gap === 'loose' ? 'gap-4' : 'gap-2';

  if (c.component === 'Section') {
    return (
      <section className="rounded-lg border border-glass-border bg-surface-1/60 p-3">
        {str(c.props.title) ? (
          <h3 className="m-0 text-[12px] font-semibold text-zinc-100">{str(c.props.title)}</h3>
        ) : null}
        {str(c.props.subtitle) ? (
          <p className="m-0 mt-0.5 text-[10.5px] leading-snug text-zinc-500">{str(c.props.subtitle)}</p>
        ) : null}
        <div className={`mt-2 flex min-w-0 flex-col ${gap}`}>
          {kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} onAction={onAction} />)}
        </div>
      </section>
    );
  }
  if (c.component === 'Grid') {
    /* Every step has to pass through the step below it. The old map gave
       `2` no breakpoint at all (two crushed columns on a phone), jumped `3`
       from one column straight to three, and — the one that mattered most —
       capped `auto` at two columns for any width, because `auto` is what a
       model reaches for when it means "as many as fit". A six-card dashboard
       then rendered as three rows of two across a 1600px pane. `auto` is now
       a real auto-fit track list, so it fills the container it is in. */
    const cols = c.props.columns === '2' ? 'grid-cols-1 sm:grid-cols-2'
      : c.props.columns === '3' ? 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3'
        : c.props.columns === '4' ? 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-4'
          : 'grid-cols-[repeat(auto-fit,minmax(210px,1fr))]';
    return (
      <div className={`grid min-w-0 ${cols} ${gap}`}>
        {kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} onAction={onAction} />)}
      </div>
    );
  }
  if (c.component === 'Accordion') {
    return (
      <details className="rounded-lg border border-glass-border bg-surface-1/40">
        <summary className="cursor-pointer px-3 py-2 text-[11.5px] font-semibold text-zinc-200">
          {str(c.props.title) || 'Details'}
        </summary>
        <div className={`flex min-w-0 flex-col border-t border-glass-border/60 p-3 ${gap}`}>
          {kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} onAction={onAction} />)}
        </div>
      </details>
    );
  }
  if (c.component === 'Toolbar') {
    const align = c.props.align === 'center' ? 'justify-center'
      : c.props.align === 'end' ? 'justify-end' : 'justify-start';
    return <div className={`flex flex-wrap items-center ${align} ${gap} [&>*]:min-w-0`}>
      {kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} onAction={onAction} />)}
    </div>;
  }
  if (c.component === 'Card') {
    const child = c.props.child ? byId.get(str(c.props.child)) : undefined;
    return (
      <section className="rounded-lg border border-glass-border bg-surface-1 p-3">
        {c.props.title ? <h3 className="m-0 mb-1.5 text-[12px] font-semibold text-zinc-100">{str(c.props.title)}</h3> : null}
        {kids.length ? (
          <div className={`flex min-w-0 flex-col ${gap}`}>
            {kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} onAction={onAction} />)}
          </div>
        ) : child ? <Node c={child} byId={byId} depth={depth + 1} onAction={onAction} /> : null}
      </section>
    );
  }
  if (c.component === 'MetricRow') {
    /* KeyValues are Stat cards, which carry their own flex-1/min-width, so the
       strip only has to wrap them evenly. The old gap-4 left a metric strip
       noticeably looser than the sections under it.

       `[&>*]:max-w-[300px]` is the part that fixes the look: `flex-1` with no
       ceiling meant three metrics showing "12", "0" and "3" stretched to a
       third of the pane each — 400px-wide cards around a two-character number.
       They now grow to fill the row but stop at a width a number can live in,
       and the strip wraps instead of crushing on a narrow pane. */
    return (
      <div className="flex flex-wrap items-stretch gap-2 [&>*]:min-w-0 [&>*]:max-w-[300px]">
        {kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} onAction={onAction} />)}
      </div>
    );
  }
  if (c.component === 'Row') {
    const align = c.props.align === 'center' ? 'items-center' : c.props.align === 'bottom' ? 'items-end' : 'items-start';
    // min-w-0 on the children: a flex item's default min-width is its content
    // width, so one Table or long mono string in a Row pushed the whole app
    // card wider than its pane and the board scrolled sideways.
    return <div className={`flex flex-wrap ${align} ${gap} [&>*]:min-w-0`}>{kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} onAction={onAction} />)}</div>;
  }
  if (c.component === 'Column') {
    return <div className={`flex min-w-0 flex-col ${gap}`}>{kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} onAction={onAction} />)}</div>;
  }
  return <Leaf c={c} onAction={onAction} />;
}

function defaultInputs(surface: A2uiSurface): Record<string, string> {
  const out: Record<string, string> = {};
  for (const c of surface.components) {
    const name = str(c.props.name) || c.id;
    switch (c.component) {
      case 'Input':
        out[name] = str(c.props.value);
        break;
      case 'Select':
      case 'RadioGroup': {
        const opts = optionsOf(c);
        out[name] = str(c.props.value) || opts[0]?.value || '';
        break;
      }
      case 'Checkbox':
      case 'Toggle':
        out[name] = String(c.props.checked === true);
        break;
      case 'Slider':
        out[name] = str(c.props.value) || String(num(c.props.min) ?? 0);
        break;
      default:
        break;
    }
  }
  return out;
}

export function A2ui({ surface, notes, onAction }: { surface: A2uiSurface; notes?: string[]; onAction?: A2uiActionHandler }) {
  const byId = useMemo(() => new Map(surface.components.map((c) => [c.id, c])), [surface]);
  const defaults = useMemo(() => defaultInputs(surface), [surface]);
  const [inputs, setInputs] = useState<Record<string, string>>(defaults);
  // A different surface is a different form; do not leak one app's answers
  // into another's input bag when the panel switches apps.
  useEffect(() => { setInputs(defaults); }, [surface.surfaceId, defaults]);
  const root = byId.get(surface.rootComponent);
  if (!root) {
    return (
      <p className="text-[11.5px] text-amber-300">
        Surface {surface.surfaceId} has no root component ({surface.rootComponent}).
      </p>
    );
  }
  return (
    <div className="surface-in min-w-0">
      {notes && notes.length ? (
        <ul className="mb-2 space-y-0.5">
          {notes.map((n, i) => (
            <li key={i} className="text-[10.5px] text-zinc-muted">{n}</li>
          ))}
        </ul>
      ) : null}
      <InputsContext.Provider value={{
        values: inputs,
        set: (name, value) => setInputs((prev) => ({ ...prev, [name]: value })),
      }}>
        <Node c={root} byId={byId} depth={0} onAction={onAction} />
      </InputsContext.Provider>
    </div>
  );
}