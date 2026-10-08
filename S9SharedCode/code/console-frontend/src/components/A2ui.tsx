import { useMemo, useState } from 'react';
import type { A2uiComponent, A2uiProps, A2uiSurface } from '../api';

export type { A2uiComponent, A2uiProps, A2uiSurface };

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
function Sparkline({ series, label }: { series: number[]; label?: string }) {
  const w = 240, h = 40;
  const path = useMemo(() => {
    const pts = series.map(num).filter((n): n is number => n !== null);
    if (pts.length === 0) return '';
    if (pts.length === 1) return `M0,${h / 2} L${w},${h / 2}`;
    const lo = Math.min(...pts), hi = Math.max(...pts);
    const span = hi - lo || 1;
    return pts
      .map((v, i) => {
        const x = (i / (pts.length - 1)) * w;
        const y = h - ((v - lo) / span) * (h - 4) - 2;
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  }, [series]);

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

  return (
    <div className="min-w-0">
      {searchable ? (
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
                {columns.map((c) => (
                  <th key={c.key} scope="col"
                    className="border-b border-glass-border px-2 py-1 font-medium text-zinc-muted">
                    {searchable || true ? (
                      <button
                        type="button"
                        onClick={() => setSort((s) => (s?.key === c.key ? (s.dir === 1 ? { key: c.key, dir: -1 } : null) : { key: c.key, dir: 1 }))}
                        className="inline-flex items-center gap-1 hover:text-zinc-100"
                        aria-label={`Sort by ${c.label}`}
                      >
                        {c.label}
                        {sort?.key === c.key ? <span aria-hidden>{sort.dir === 1 ? '▲' : '▼'}</span> : null}
                      </button>
                    ) : c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((r, i) => {
                const key = rowKey && r[rowKey] !== undefined ? str(r[rowKey]) : String(i);
                return (
                  <tr key={key} className="border-b border-glass-border/40">
                    {columns.map((c) => {
                      const isNew = c.key === '_new' && str(r[c.key]) === 'yes';
                      return (
                        <td key={c.key} className="px-2 py-1 align-top text-zinc-200">
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
function Leaf({ c }: { c: A2uiComponent }) {
  const p = c.props;
  switch (c.component) {
    case 'Text':
      return <p className="m-0 whitespace-pre-wrap text-[12px] text-zinc-200">{str(p.text)}</p>;
    case 'CodeBlock':
      return <pre className="m-0 overflow-x-auto rounded bg-surface-0 p-2 text-[11.5px] text-zinc-200"><code>{str(p.text)}</code></pre>;
    case 'Badge':
      return <span className="inline-flex items-center rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-[10.5px] text-accent">{str(p.text)}</span>;
    case 'KeyValue':
      return (
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-wide text-zinc-muted">{str(p.label)}</div>
          <div className="truncate text-[15px] text-zinc-100">{str(p.value)}</div>
        </div>
      );
    case 'Divider':
      return <hr className="my-1 border-0 border-t border-glass-border" />;
    case 'Spacer':
      return <div className="h-2" />;
    case 'Link':
      /* `target` is validated to enum:console - a surface cannot emit an
         external URL, so this never becomes an anchor to somewhere else. */
      return <span className="text-[11.5px] text-accent underline">{str(p.label)}</span>;
    case 'Table':
      return (
        <Table
          columns={(p.columns as { key: string; label: string }[]) || []}
          rows={(p.rows as Record<string, unknown>[]) || []}
          rowKey={p.rowKey ? str(p.rowKey) : undefined}
          searchable={p.searchable !== false}
          paginated={p.paginated !== false}
          emptyText={p.emptyText ? str(p.emptyText) : undefined}
        />
      );
    case 'Sparkline':
      return <Sparkline series={(p.series as number[]) || []} label={p.label ? str(p.label) : undefined} />;
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
      /* Read-only catalog: there is no action route on the server, so a button
         that emitted an event would be inert. Rendered disabled and honest
         rather than as a control that silently does nothing. */
      return (
        <button type="button" disabled
          title="Agent-generated actions are not enabled"
          className="rounded border border-glass-border px-2 py-1 text-[11.5px] text-zinc-muted opacity-60">
          {str(p.label)}
        </button>
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
    default:
      /* Unreachable for a validated surface; kept so a future catalog addition
         degrades to visible nothing rather than throwing mid-render. */
      return null;
  }
}

function Node({ c, byId, depth }: { c: A2uiComponent; byId: Map<string, A2uiComponent>; depth: number }) {
  const kids = ((c.props.children as string[]) || []).map((id) => byId.get(id)).filter(Boolean) as A2uiComponent[];
  const gap = c.props.gap === 'tight' ? 'gap-1' : c.props.gap === 'loose' ? 'gap-4' : 'gap-2';

  if (c.component === 'Card') {
    const child = c.props.child ? byId.get(str(c.props.child)) : undefined;
    return (
      <section className="rounded border border-glass-border bg-surface-1 p-2">
        {c.props.title ? <h3 className="m-0 mb-1 text-[12px] font-medium text-zinc-100">{str(c.props.title)}</h3> : null}
        {child ? <Node c={child} byId={byId} depth={depth + 1} /> : null}
      </section>
    );
  }
  if (c.component === 'MetricRow') {
    return (
      <div className="flex flex-wrap gap-4">
        {kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} />)}
      </div>
    );
  }
  if (c.component === 'Row') {
    return <div className={`flex flex-wrap items-start ${gap}`}>{kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} />)}</div>;
  }
  if (c.component === 'Column') {
    return <div className={`flex min-w-0 flex-col ${gap}`}>{kids.map((k) => <Node key={k.id} c={k} byId={byId} depth={depth + 1} />)}</div>;
  }
  return <Leaf c={c} />;
}

export function A2ui({ surface, notes }: { surface: A2uiSurface; notes?: string[] }) {
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
    <div className="min-w-0">
      {notes && notes.length ? (
        <ul className="mb-2 space-y-0.5">
          {notes.map((n, i) => (
            <li key={i} className="text-[10.5px] text-zinc-muted">{n}</li>
          ))}
        </ul>
      ) : null}
      <Node c={root} byId={byId} depth={0} />
    </div>
  );
}