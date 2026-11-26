import { useEffect, useRef, useState, type ReactNode } from 'react';
import { api, type StudioChange, type StudioRoot, type StudioRun, type StudioRunCost } from '../../api';
import { CheckCircle2, Circle, FolderTree, GitBranch, Loader2, MinusCircle, XCircle, BarChart3, FileDiff, Gauge, ListChecks } from 'lucide-react';
import { baseName as BASENAME } from '../../lib/studioSession';
import { Label } from './chrome';
import { fileVisual } from './fileIcons';

/* The always-open session column.
 *
 * The OpenCode reference the user asked for shows its session state as one
 * grouped, always-visible panel rather than a pile of chips and one-off
 * buttons scattered across the workbench. This is Aria's version of that, in
 * the console's own idiom: the CONTEXT / PROJECT / USAGE / RUNS blocks, and
 * the one block that used to crowd the transcript — the unreviewed-changes
 * list with its +/− counts — moved here where the user can watch the work take
 * shape without scrolling the conversation to the bottom.
 *
 * Everything shown is real. Cost and cache are the gateway's ledger, read
 * per run; a ledger that cannot be read says so instead of drawing $0.
 * The selected model's context window comes from the gateway's own
 * catalogue, not from a constant written into this file. */

const RUN_ICON: Record<string, ReactNode> = {
  running: <Loader2 size={12} className="animate-spin text-zinc-400" />,
  waiting: <MinusCircle size={12} className="text-warn" />,
  stopping: <Loader2 size={12} className="animate-spin text-warn" />,
  finished: <CheckCircle2 size={12} className="text-ok" />,
  stopped: <MinusCircle size={12} className="text-warn" />,
  error: <XCircle size={12} className="text-danger" />,
};

function runIcon(status?: string) {
  return RUN_ICON[status || ''] ?? <Circle size={12} className="text-zinc-500" />;
}

function ago(ts?: number): string {
  if (!ts) return '';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function fmtNum(n: number): string {
  return Math.round(n).toLocaleString();
}

export function SessionPanel({ sid, roots, branch, pending, runs, runLive,
                                model,
                                onOpenPath, onChanges, onOpenFolder }: {
  sid: string;
  roots: StudioRoot[];
  branch: string | null;
  pending: Record<string, StudioChange>;
  runs: StudioRun[];
  runLive: number;
  model?: string;
  onOpenPath: (path: string) => void;
  onChanges: () => void;
  /** Add a folder to this session. Both the picker paths lead here. */
  onOpenFolder?: () => void;
}) {
  const pendingList = Object.values(pending);
  const pendingAdded = pendingList.reduce((a, c) => a + (c.added || 0), 0);
  const pendingRemoved = pendingList.reduce((a, c) => a + (c.removed || 0), 0);
  const runRows = runs.slice(0, 4);
  const toolCalls = runs.reduce((a, r) => a + (r.tool_calls || 0), 0);
  const tokensIn = runs.reduce((a, r) => a + (r.input_tokens || 0), 0);
  const tokensOut = runs.reduce((a, r) => a + (r.output_tokens || 0), 0);
  const elapsedSec = runs.reduce((a, r) =>
    a + (r.ended && r.started ? Math.max(0, r.ended - r.started) : 0), 0);

  /* The selected model's real context window, from the gateway's catalogue.
     The panel used to meter against a 600k-character ceiling the coder
     advertises — neither the model's context nor a token budget. The read
     goes through the agent to a possibly-cold gateway; one retry after 3s,
     bounded, then the caption says the roster did not answer. */
  const [promptWindow, setPromptWindow] = useState<number | null>(null);
  const rosterRetried = useRef(false);
  useEffect(() => {
    let live = true;
    rosterRetried.current = false;
    const read = () => {
      void api.models(false, true)
        .then((r) => {
          if (!live) return;
          const models = r?.models ?? [];
          const sel = models.find((m) => m.id === model)
            ?? models.find((m) => m.id === r?.default);
          const ctxWindow = sel && sel.context_length > 0 ? sel.context_length : null;
          if (ctxWindow !== null || rosterRetried.current) { setPromptWindow(ctxWindow); return; }
          rosterRetried.current = true;
          window.setTimeout(() => { if (live) read(); }, 3000);
        })
        .catch(() => {
          if (!live) return;
          if (rosterRetried.current) { setPromptWindow(null); return; }
          rosterRetried.current = true;
          window.setTimeout(() => { if (live) read(); }, 3000);
        });
    };
    read();
    return () => { live = false; };
  }, [model]);

  /* The ledger the panel reads most cheaply: the newest *finished* run's cost.
     One honest call rather than one per run; the transcript makes the same
     call. `null` while it is being read, `{unknown:true}` when the gateway
     cannot be reached — the row says so, not $0. A transient miss (the gateway
     restarted mid-read, which has happened in this repo) used to freeze the
     panel on `unknown` forever, because nothing re-read it; one retry happens
     after 3s, once per run, before the row says so. */
  const finishedRuns = runs.filter((r) => r.status !== 'running'
    && r.status !== 'stopping'
    // A 503 before the first model call burns zero tokens and tells nobody
    // anything about what the conversation is carrying; letting it stand as
    // "the newest finished run" drops the meter to a misleading 0%.
    && !(r.status === 'error' && (r.input_tokens || 0) === 0));
  const newestFinished = finishedRuns[0];
  const [usage, setUsage] = useState<StudioRunCost | null>(null);
  const usageFor = useRef('');
  const retried = useRef('');
  useEffect(() => {
    if (!newestFinished) { setUsage(null); usageFor.current = ''; retried.current = ''; return; }
    if (usageFor.current === newestFinished.id) return;
    usageFor.current = newestFinished.id;
    retried.current = '';
    let live = true;
    void api.studioRunCost(sid, newestFinished.id)
      .then((c) => {
        if (!live) return;
        setUsage(c);
        if (c?.unknown && retried.current !== newestFinished.id) {
          retried.current = newestFinished.id;
          window.setTimeout(() => {
            if (!live) return;
            void api.studioRunCost(sid, newestFinished.id)
              .then((c2) => { if (live) setUsage(c2); })
              .catch(() => { /* still unknown; the row says so */ });
          }, 3000);
        }
      })
      .catch(() => { if (live) setUsage({ unknown: true }); });
    return () => { live = false; };
  }, [sid, newestFinished?.id]);

  /* A section header in the console's own language: an accent tile, the
     micro-label, an optional right slot — every block below wears the same
     shape so the column reads as one panel, not five loose groups. */
  const block = (icon: ReactNode, label: string, right?: ReactNode) => (
    <div className="flex flex-none items-center gap-1.5 px-3 pt-4">
      <span aria-hidden
            className="flex h-4 w-4 flex-none items-center justify-center rounded-[4px] bg-white/[0.06] text-zinc-300">
        {icon}
      </span>
      <Label>{label}</Label>
      {right && <span className="ml-auto flex items-center gap-1">{right}</span>}
    </div>
  );

  const usageRow = (k: string, v: ReactNode) => (
    <div key={k} className="flex min-w-0 items-baseline justify-between gap-2 px-3 py-1">
      <span className="flex-none text-[11px] text-zinc-muted">{k}</span>
      <span className="min-w-0 truncate text-right font-mono text-[12px] tabular-nums text-zinc-200">{v}</span>
    </div>
  );

  return (
    <aside aria-label="Session details"
           className="flex min-h-0 w-full flex-none flex-col border-l border-glass-border text-[12px]"
           style={{ background: 'radial-gradient(560px 420px at 88% -12%, rgba(167,139,250,0.07), transparent 62%), var(--color-surface-0)' }}>
      <div className="flex flex-none items-center gap-2 border-b border-glass-border px-3 py-2">
        <Label>Session</Label>
        <span className="min-w-0 flex-1" />
        {runLive > 0 ? (
          <span className="flex items-center gap-1 rounded-full border border-white/15 bg-white/[0.06] px-2 py-0.5 text-[10px] font-bold uppercase tracking-[0.12em] text-zinc-200">
            <Loader2 size={10} className="animate-spin" /> working
          </span>
        ) : (
          <span className="flex items-center gap-1 rounded-full border border-glass-border bg-white/5 px-2 py-0.5 text-[10px] font-bold uppercase tracking-[0.12em] text-zinc-muted">
            <Circle size={6} fill="currentColor" /> idle
          </span>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto pb-3">
        {/* CONTEXT — what the model the user picked actually is, from the
            gateway's own catalogue; the bar is the newest *finished* run's
            prompt as a share of that window, from the same ledger the cost
            row reads (nothing invented — an empty bar means "no run yet" or
            "the ledger did not answer", and the caption says which). */}
        {block(<Gauge size={11} />, 'Context')}
        <div className="flex flex-none items-center gap-2 px-3 pt-1.5">
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-zinc-300" title={model || 'auto model'}>
            {model ? model.split('/').slice(-1)[0] : 'auto model'}
          </span>
        </div>
        {promptWindow !== null && (
          <div className="mx-3 mt-1.5 h-1 overflow-hidden rounded-full bg-white/10"
               role="progressbar" aria-label="Last run's prompt as a share of the context window"
               aria-valuemin={0} aria-valuemax={100}
               aria-valuenow={usage && !usage.unknown && usage.in_tok !== undefined
                 ? Math.min(100, Math.round((usage.in_tok / promptWindow) * 1000) / 10)
                 : 0}>
            <div className="h-full rounded-full bg-zinc-100 transition-all duration-500"
                 style={{ width: `${usage && !usage.unknown && usage.in_tok !== undefined
                   ? Math.min(100, (usage.in_tok / promptWindow) * 100)
                   : 0}%` }} />
          </div>
        )}
        <p className="px-3 pt-1 font-mono text-[10.5px] text-zinc-muted">
          {promptWindow === null
            ? 'context window unavailable — the gateway roster did not answer'
            : !usage
              ? `the ${Math.round(promptWindow / 1000)}k window · the bar fills from the gateway ledger after the first finished run`
              : usage.unknown
                ? 'the gateway ledger did not answer — no fill drawn; retrying once in a few seconds'
                : `last run's prompt ≈ ${Math.min(100, Math.round(((usage.in_tok || 0) / promptWindow) * 100))}% of the ${Math.round(promptWindow / 1000)}k window · ${fmtNum(usage.in_tok || 0)} in tokens, per the ledger`}
        </p>

        {/* PROJECT — which folders, which branch, what's unreviewed. The
            pending counts are the same truth the Changes drawer reads. */}
        {block(<FolderTree size={11} />, 'Project')}
        <div className="space-y-1 px-3 pt-1">
          <p className="flex min-w-0 items-center gap-2 text-[12px] text-zinc-200">
            <FolderTree size={12} aria-hidden className="flex-none text-zinc-muted" />
            <span className="min-w-0 truncate"
                  title={roots.map((r) => r.path).join('\n')}>
              {roots.length === 0 ? 'no folder yet'
                : roots.length === 1 ? roots[0].name
                : `${roots[0].name} +${roots.length - 1}`}
            </span>
          </p>
          {roots.length === 0 && onOpenFolder && (
            <button type="button" onClick={onOpenFolder}
                    className="mt-1 flex w-full items-center justify-center gap-1.5 rounded-md bg-zinc-100 px-2.5 py-1.5 text-[11px] font-bold text-[#161616] transition-colors hover:brightness-110">
              <FolderTree size={11} aria-hidden /> Open a folder…
            </button>
          )}
          {branch && (
            <p className="flex min-w-0 items-center gap-2 font-mono text-[12px] text-zinc-200">
              <GitBranch size={12} aria-hidden className="flex-none text-zinc-muted" />
              <span className="min-w-0 truncate" title={String(branch)}>{String(branch)}</span>
            </p>
          )}
          {pendingList.length > 0 && (
            <p className="flex min-w-0 items-center gap-2 font-mono text-[12px] text-zinc-200">
              <span aria-hidden
                    className="flex h-3 w-3 flex-none items-center justify-center rounded-full bg-warn/80 text-[8px] font-bold leading-none text-[#161616]">
                {pendingList.length}
              </span>
              <span className="min-w-0 truncate">
                {pendingList.length} unreviewed ·{' '}
                <span className="text-ok">+{pendingAdded}</span>{' '}
                <span className="text-danger">−{pendingRemoved}</span>
              </span>
              <button type="button" onClick={onChanges}
                      className="flex-none rounded border border-glass-border px-1.5 py-0.5 font-mono text-[10.5px] text-zinc-300 hover:bg-white/5">
                review
              </button>
            </p>
          )}
        </div>

        {/* USAGE — every measured number for this session, in one block. */}
        {block(<BarChart3 size={11} />, 'Usage')}
        <div className="pt-1">
          {(tokensIn > 0 || tokensOut > 0) && usageRow('tokens',
            `${fmtNum(tokensIn)} in · ${fmtNum(tokensOut)} out`)}
          {toolCalls > 0 && usageRow('tool calls', `${toolCalls}`)}
          {elapsedSec > 0 && usageRow('elapsed', `${Math.round(elapsedSec)}s`)}
          {!usage && toolCalls > 0 ? usageRow('cost', '—') : null}
          {usage && usage.unknown ? usageRow('cost',
            'unknown — the ledger was not reachable') : null}
          {usage && !usage.unknown && usageRow('cost', (
            <span title={`in ${fmtNum(usage.in_tok || 0)} · out ${fmtNum(usage.out_tok || 0)} · ${usage.calls ?? 0} calls`}>
              ${(usage.usd ?? 0).toFixed(4)}
            </span>
          ))}
          {usage && !usage.unknown && usageRow('cache', (
            <span title="prompt-cache tokens the provider answered from its own cache vs minted for it this run — read from the gateway's calls ledger. Providers that do not cache (e.g. cohere/north-mini-code:free via Kilo) report 0 honestly; Gemini-class providers report millions.">
              {fmtNum(usage.cache_reads || 0)} cached · {fmtNum(usage.cache_creates || 0)} fresh
            </span>
          ))}
        </div>

        {/* CHANGES WAITING — this is the block the transcript used to carry
            above the composer; it was half the visible conversation. Same
            data, same actions, now where the user goes to watch the work. */}
        {pendingList.length > 0 && (
          <>
            {block(<FileDiff size={11} />, 'Changes waiting', (
              <button type="button" onClick={onChanges}
                      className="rounded border border-glass-border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.1em] text-zinc-300 transition-colors hover:bg-white/5 hover:text-zinc-100">
                review all
              </button>
            ))}
            <ul className="mx-3 mt-1 divide-y divide-white/5 overflow-hidden rounded-[10px] border border-glass-border bg-surface-1">
              {pendingList.map((c) => {
                const v = fileVisual(c.path);
                const FIcon = v.icon;
                return (
                <li key={c.path} className="flex min-w-0 items-center gap-2 px-2 py-1.5 transition-colors hover:bg-white/[0.03]">
                  <FIcon size={13} strokeWidth={1.5} color={v.cls} className="flex-none" />
                  <button type="button" onClick={() => onOpenPath(c.path)}
                          className="min-w-0 flex-1 truncate text-left font-mono text-[12px] text-zinc-200 transition-colors hover:text-zinc-300"
                          title={`${c.path} — opens in review, where each hunk can be kept or undone`}>
                    {BASENAME(c.path)}
                  </button>
                  {c.created && (
                    <span className="flex-none rounded border border-ok/30 bg-ok-dim px-1 py-px font-mono text-[9.5px] font-semibold uppercase tracking-wide text-ok">new</span>
                  )}
                  {(c.added ?? 0) + (c.removed ?? 0) > 0 && (
                    <span className="flex flex-none items-center gap-1.5 font-mono text-[11px] tabular-nums">
                      {!!c.added && <span className="text-ok">+{c.added}</span>}
                      {!!c.removed && <span className="text-danger">−{c.removed}</span>}
                    </span>
                  )}
                </li>
                );
              })}
            </ul>
          </>
        )}

        {/* RUNS — the subagents block, one honest row per run this session
            holds. The same registry the left rail's spinner reads. */}
        {block(<ListChecks size={11} />, 'Runs')}
        <ul className="mt-1 divide-y divide-white/5">
          {runRows.length === 0 && (
            <li className="px-3 py-2 text-[12px] text-zinc-muted">No runs yet.</li>
          )}
          {runRows.map((r) => (
            <li key={r.id} className="flex min-w-0 items-center gap-2 px-3 py-1.5"
                title={`${r.question}\n${r.status}${r.tool_calls ? ` · ${r.tool_calls} tool calls` : ''}`}>
              <span className="flex-none">{runIcon(r.status)}</span>
              <span className="min-w-0 flex-1 truncate text-[12px] text-zinc-300">{r.question}</span>
              <span className="flex-none font-mono text-[10.5px] tabular-nums text-zinc-muted">
                {ago(r.started)}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </aside>
  );
}
