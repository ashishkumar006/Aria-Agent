import { useEffect, useMemo, useState } from 'react';
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
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"
        className="h-10 w-full" role="img"
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
    case 'Action':
    case 'Button': {
      /* `Action` is the catalog name; `Button` is the AG-UI name older chat
         payloads used. Both carry their event as action/args or, in the old
         shape, as onPress.{action,args|params}. Without threading the handler
         through Node recursion every button nested in a Column rendered
         disabled, which is why an action surface looked inert. */
      const press = (c.props.onPress as Record<string, unknown> | undefined) || {};
      const action = str(c.props.action || press.action || '');
      const args = (c.props.args ?? press.args ?? press.params ?? {}) as Record<string, unknown>;
      /* `variant` decides weight: a dashboard's main call to action has to
         look different from a secondary one, and the old renderer drew both
         as the same hairline outline. */
      const primary = str(c.props.variant) === 'primary';
      const primaryCls = 'rounded-md bg-accent px-3 py-1.5 text-[11.5px] font-semibold text-[#0b0b0e] transition hover:brightness-110 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60';
      const secondaryCls = 'rounded-md border border-glass-border bg-white/[0.03] px-3 py-1.5 text-[11.5px] text-zinc-200 transition hover:border-white/20 hover:bg-white/[0.07] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40';
      if (onAction && action) {
        return (
          <button
            type="button"
            onClick={() => onAction(action, args)}
            title={str((c.props as Record<string, unknown>).tooltip || '')}
            className={primary ? primaryCls : secondaryCls}
          >
            {str(p.label)}
          </button>
        );
      }
      /* Read-only catalog fallback: no action route on the server. */
      return (
        <button type="button" disabled
          title="Agent-generated actions are not enabled"
          className={`${primary ? primaryCls : secondaryCls} cursor-not-allowed opacity-60`}>
          {str(p.label)}
        </button>
      );
    }
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
    case 'FilterChips': {
      const opts = (p.options as { id: string; label: string; selected?: boolean }[]) || [];
      return (
        <div className="flex flex-wrap gap-1">
          {opts.map((o) => (
            <span key={o.id}
              className={`rounded-full border px-2 py-0.5 text-[10.5px] ${o.selected ? 'border-accent/50 bg-accent/10 text-accent' : 'border-glass-border text-zinc-muted'}`}>
              {str(o.label)}
            </span>
          ))}
        </div>
      );
    }
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
       noticeably looser than the sections under it. */
    return (
      <div className="flex flex-wrap items-stretch gap-2">
        {kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} onAction={onAction} />)}
      </div>
    );
  }
  if (c.component === 'Row') {
    const align = c.props.align === 'center' ? 'items-center' : c.props.align === 'bottom' ? 'items-end' : 'items-start';
    return <div className={`flex flex-wrap ${align} ${gap}`}>{kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} onAction={onAction} />)}</div>;
  }
  if (c.component === 'Column') {
    return <div className={`flex min-w-0 flex-col ${gap}`}>{kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} onAction={onAction} />)}</div>;
  }
  return <Leaf c={c} onAction={onAction} />;
}

export function A2ui({ surface, notes, onAction }: { surface: A2uiSurface; notes?: string[]; onAction?: A2uiActionHandler }) {
  const byId = useMemo(() => new Map(surface.components.map((c) => [c.id, c])), [surface]);
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
      <Node c={root} byId={byId} depth={0} onAction={onAction} />
    </div>
  );
}