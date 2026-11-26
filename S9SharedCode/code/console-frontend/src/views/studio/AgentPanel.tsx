import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactNode } from 'react';
import { ArrowDown, CheckCircle2, ChevronRight, FileText, FolderOpen, GitBranch, Globe, GripVertical, Loader2, MinusCircle, ListChecks, Pencil, Sparkles, Terminal, Wrench, XCircle } from 'lucide-react';
import { api, type CodeAgentFrame } from '../../api';
import type { StudioRoot, StudioRun, StudioRunCost, StudioSession } from '../../api';
import { renderMarkdown } from '../../components/markdown';
import { highlight, langFor } from './highlight';
import { Label } from './chrome';
import { InlineChange } from './InlineChange';
import { Composer, checksToRows, checksSummary, type BrowserPageRef, type ProblemRow, type RunMeta } from './Composer';

/** The agent's answer, rendered the way the rest of the console renders an
 *  answer. The studio used to print `t.text` into a `whitespace-pre-wrap`
 *  paragraph, which is what made this screen look cluttered rather than
 *  calm: a reply containing a code fence showed its backticks, a reply with
 *  `**bold**` showed the asterisks, and a reply containing an HTML file —
 *  which is exactly what "create menu_tracker.html" produces — dumped
 *  several hundred lines of raw markup at 14px down the whole column,
 *  because nothing distinguished prose from source.
 *
 *  Long answers are folded rather than truncated: the text is all there, one
 *  click away, and the screen is readable when it arrives. `fold` measures
 *  characters, never drops them. */
function AnswerBody({ text, sid, onOpenPath, onDecision }: {
  text: string;
  sid: string;
  onOpenPath?: (p: string) => void;
  /** A keep/revert decision inside an inline diff — let the shell re-read. */
  onDecision?: () => void;
}) {
  /* The file links this answer expanded, in click order. A file link shows
     its change IN THE CONVERSATION — green lines added, red lines removed —
     instead of swapping the screen for a review editor. */
  const [inlineRefs, setInlineRefs] = useState<string[]>([]);
  const html = useMemo(() => renderMarkdown(text, 0, false, true), [text]);
  /* File paths in the answer are clickable (`file-ref` buttons in the markdown
     output). A click toggles that file's inline diff here; it no longer
     navigates anywhere. */
  const openRef = (e: ReactMouseEvent) => {
    const el = (e.target as Element | null)?.closest?.('[data-file-ref]') as
      HTMLElement | null | undefined;
    if (!el) return;
    e.preventDefault();
    e.stopPropagation();
    const ref = el.getAttribute('data-file-ref') || '';
    if (!ref) return;
    setInlineRefs((all) => all.includes(ref)
      ? all.filter((x) => x !== ref)
      : [...all, ref]);
  };
  return (
    <div className="min-w-0" onClick={openRef}>
      <div className="prose-chat relative overflow-hidden"
           dangerouslySetInnerHTML={{ __html: html }} />
      {inlineRefs.map((ref) => (
        <InlineChange key={ref} sid={sid} refStr={ref}
                      onClose={() => setInlineRefs((all) => all.filter((x) => x !== ref))}
                      onOpenInEditor={(p) => onOpenPath?.(p)}
                      onDecision={onDecision} />
      ))}
    </div>
  );
}

/** The studio's conversation: the transcript and the composer that feeds it.
 *
 *  This is the surface the studio opens on, which is the point of the whole
 *  layout — in an agent-first IDE the object you manage is the agent working on
 *  your code, so it gets the centre of the window rather than a sidebar. The
 *  reference product's empty state is a question ("What should we work on in
 *  <folder>?") and one large composer; matching that is not decoration, it is
 *  the difference between a surface that invites a task and one that waits for
 *  you to find a file.
 *
 *  What is deliberately NOT here yet, because it belongs to later slices:
 *  transcript persistence and resume (slice 5), `@`/`/` context sources and the
 *  cost readout (slice 6), approval cards beyond the raw frame (slice 6), and
 *  the browser (slices 9-10). Each is a hole with a number on it, not an
 *  oversight.
 */
export interface Turn {
  id: string;
  role: 'user' | 'aria' | 'note';
  text: string;
  thinking?: string;
  tools: ToolRow[];
  stopped?: string;
  error?: string;
  running: boolean;
  usage?: { in: number; out: number };
  /** The agent's own plan checklist, streamed as `plan` frames. Pinned above
   *  the answer while the run lives and collapsed once it does not. */
  plan?: { title: string; status: string }[];
  /** Seconds the run took, from the registry's own clock. */
  elapsed?: number;
  /** The run this turn belongs to, so Stop can address it. */
  run?: string;
  /** The ceilings this run was given, from its `started` frame — so "hop 7"
   *  is measured against the cap the SERVER used, not a constant in here. */
  maxHops?: number;
  contextCap?: number;
  /** The gateway session this run billed into. Real money is read from it. */
  gwSession?: string;
  /** `undefined` = not fetched yet; `unknown: true` = the ledger was not
   *  reachable, which is shown as a gap and never as $0.00. */
  cost?: StudioRunCost;
  /** Only for `role: 'note'` turns: a command's own report, which has to look
   *  different from the agent's answer or it reads as a second reply. */
  tone?: 'info' | 'warn';
}

export interface ToolRow {
  id: string;
  name: string;
  detail?: string;
  ok?: boolean;
  state: 'running' | 'done' | 'skipped';
  note?: string;
  /** The call arguments, so the UI can show WHICH file / lines / edit. */
  args?: Record<string, unknown>;
  /** A short excerpt of the tool result (read range, edit confirmation, output). */
  result?: string;
  /** Where in the thinking stream this call happened (in characters of the
   *  accumulated thinking). Replay and live both push tool calls as separate
   *  frames, so the index is stable: the thinking text is append-only. The
   *  transcript interleaves the tool rows into the thinking text at these
   *  offsets instead of dumping them all at the end. */
  atLen?: number;
}

let seq = 0;
const nextId = () => `t${++seq}`;

/** The collapsed activity line: what kind of work, not a log of it. Grouped by
 *  what the verb DOES, because "read_file ×3, edit_file ×2" is a diff of the
 *  tool registry rather than an answer to "what has it been doing". */
export function activitySummary(tools: ToolRow[]): string {
  const kind = (name: string): string => {
    const n = name || '';
    if (n.startsWith('browser_')) return 'Browser';
    if (n === 'run_command' || n === 'run_checks' || /^verify/.test(n)) return 'Shell';
    if (/^(edit_file|create_file|delete_file|make_dir|replace)/.test(n)) return 'Edit';
    if (/^(read_file|list_dir|search|grep|document_symbols|code_intel|find)/.test(n)) return 'Read';
    if (/^git/.test(n)) return 'Git';
    return 'Tool';
  };
  /* "Used 22 Shell, Read, Write, Edit" — a total followed by the kinds of
     work, the way an activity log reads rather than a per-tool tally. */
  const order = ['Read', 'Edit', 'Shell', 'Git', 'Browser', 'Tool'];
  const kinds = new Set(tools.map((t) => kind(t.name || '')));
  return `${tools.length} ${order.filter((k) => kinds.has(k)).join(', ') || 'steps'}`;
}

/** The icon and tint for one tool, grouped by what it DOES (same buckets as
 *  `activitySummary`), so a run reads as "explored the tree, edited two files,
 *  ran the checks" instead of twenty identical rows with a glyph. */
export function toolVisual(name: string): { icon: React.ReactNode; tile: string } {
  const n = (name || '').toLowerCase();
  if (n.startsWith('browser_')) return { icon: <Globe size={11} />, tile: 'border-info/30 bg-info-dim text-info' };
  if (n === 'run_command' || n === 'run_checks' || /^verify/.test(n) || /^(lint|test|build|cmd)/.test(n)) {
    return { icon: <Terminal size={11} />, tile: 'border-white/15 bg-white/[0.06] text-zinc-300' };
  }
  if (/^(edit_file|create_file|delete_file|make_dir|replace|write|apply)/.test(n)) {
    return { icon: <Pencil size={11} />, tile: 'border-warn/25 bg-warn-dim text-warn' };
  }
  if (/^(git_|git$|commit|branch|diff_)/.test(n)) {
    return { icon: <GitBranch size={11} />, tile: 'border-glass-border bg-white/5 text-zinc-300' };
  }
  if (/plan/.test(n)) {
    return { icon: <ListChecks size={11} />, tile: 'border-ok/25 bg-ok-dim text-ok' };
  }
  if (/^(read_file|list_dir|search|grep|document_symbols|code_intel|find|recall)/.test(n)) {
    return { icon: <FileText size={11} />, tile: 'border-sky-400/25 bg-sky-400/10 text-sky-300' };
  }
  return { icon: <Wrench size={11} />, tile: 'border-glass-border bg-white/5 text-zinc-400' };
}
/** Fold one frame into a turn, in place.
 *
 *  Shared by the live stream and the replay of a finished run. That sharing is
 *  the point: a transcript rebuilt from disk used to be a second
 *  implementation of "what this run looked like", and the two drifted — the
 *  replayed version lost tool cards and the live one kept them. One reducer,
 *  two callers. */
/** Mirror of the server's `_tool_detail` for replayed (raw) frames: the
 *  arguments that identify WHICH file/symbol/query, one short line. */
function summarizeArgs(args: unknown): string {
  if (!args || typeof args !== 'object') return '';
  const a = args as Record<string, unknown>;
  for (const key of ['path', 'name', 'query', 'pattern', 'target', 'content',
                     'text', 'root', 'dir', 'command', 'url']) {
    const v = a[key];
    if (typeof v === 'string' && v.trim()) {
      const s = v.split(/\s+/).join(' ');
      return s.length <= 120 ? s : s.slice(0, 117) + '...';
    }
  }
  return '';
}

/** Human-readable "which file / which lines" for a tool call, plus the key
 *  argument pairs to show when expanded. Keeps the transcript legible: a read
 *  shows its range, an edit shows the range and the find→replace, a command
 *  shows the command, a search shows the pattern. */
function toolLineInfo(name: string, a?: Record<string, unknown>): {
  summary: string; pairs: [string, string][];
} {
  const pairs: [string, string][] = [];
  if (!a) return { summary: '', pairs };
  const str = (k: string) => (a[k] != null ? String(a[k]) : '');
  const path = str('path');
  const s = a.start_line ?? a.offset;
  const lines = s != null
    ? `L${s}–${a.end_line ?? (a.limit ? Number(s) + Number(a.limit) - 1 : s)}`
    : '';
  const clip = (v: string, n = 200) => (v.length > n ? v.slice(0, n) + '…' : v);
  if (path) pairs.push(['path', path]);
  if (lines) pairs.push(['lines', lines]);
  let summary = [path, lines].filter(Boolean).join(' · ');
  if (name === 'edit_file') {
    if (str('find')) pairs.push(['find', clip(str('find'))]);
    if (a.replace !== undefined) pairs.push(['replace', clip(str('replace')) || '(delete)']);
    summary = [path, lines, 'edit'].filter(Boolean).join(' · ');
  } else if (name === 'read_file') {
    summary = [path, lines || 'whole file'].filter(Boolean).join(' · ');
  } else if (name === 'create_file' || name === 'update_file') {
    if (str('content')) pairs.push(['content', clip(str('content'))]);
    summary = path;
  } else if (name === 'run_command' || name === 'run_checks') {
    if (str('command')) pairs.push(['command', str('command')]);
    summary = str('command') || name;
  } else if (name === 'search_files') {
    if (str('pattern')) pairs.push(['pattern', str('pattern')]);
    if (str('include')) pairs.push(['include', str('include')]);
    summary = `"${str('pattern')}"` + (str('include') ? ` in ${str('include')}` : '');
  } else if (name === 'list_dir') {
    summary = path || '.';
  }
  return { summary, pairs };
}

/** What a prior turn contributes to the NEXT call's history. The final text
 *  alone is not memory: a run that ended "done." tells the model nothing about
 *  what it actually did. Append a compact digest of the turn's tool activity
 *  (files read/edited + lines, commands, outcomes) so follow-up calls retain the
 *  operational context instead of behaving as if the work never happened. */
function historyContent(t: Turn): string {
  const text = (t.text || '').trim();
  if (t.role === 'user') return text;
  const acts = (t.tools || []).map((r) => {
    const info = toolLineInfo(r.name, r.args);
    const base = info.summary || r.detail || '';
    return `${r.name}${base ? ' ' + base : ''}${r.ok === false ? ' (failed)' : ''}`;
  }).filter(Boolean);
  const digest = acts.length ? `\n[actions: ${acts.join('; ').slice(0, 700)}]` : '';
  return (text + digest).trim();
}

export function foldFrame(t: Turn, f: Record<string, unknown>): void {
  const kind = String(f.t || f.type || '');
  if (kind === 'started') {
    /* The run's identity and its ceilings. Replayed runs need them too, or a
       transcript opened after a reload shows an answer with no idea which run
       it came from — and Stop, cost and replay all key off that id. */
    if (f.run) t.run = String(f.run);
    if (f.max_hops !== undefined) t.maxHops = Number(f.max_hops);
    if (f.max_context_chars !== undefined) t.contextCap = Number(f.max_context_chars);
    if (f.gateway_session) t.gwSession = String(f.gateway_session);
  } else if (kind === 'delta' || kind === 'text') {
    t.text += String(f.text || f.delta || '');
  } else if (kind === 'permission') {
    t.tools.push({ id: String(f.id || 'ask'),
                   name: String(f.tool || 'write'),
                   detail: String(f.summary || ''),
                   state: 'running', note: 'waiting for approval',
                   atLen: (t.thinking || '').length });
  } else if (kind === 'thinking') {
    /* Replay frames are the raw model events (`{t:'thinking', delta:…}`), not
       the SSE wrapper (`{t:'thinking', text:…}`). Live and replay both land
       here, so read both — a thought stream dropped on load looks like a run
       that reasoned in silence. */
    if (!f.thinking_dropped) t.thinking = (t.thinking || '') + String(f.text || f.delta || '');
  } else if (kind === 'tool_call') {
    t.tools.push({ id: String(f.id || f.name || nextId()),
                   name: String(f.name || 'tool'),
                   /* Wire frames carry a server-made `detail`; raw transcript
                      frames don't — derive the same shape from arguments. */
                   detail: String(f.detail || (f.arguments ? summarizeArgs(f.arguments) : '')),
                   args: (f.arguments && typeof f.arguments === 'object')
                     ? f.arguments as Record<string, unknown> : undefined,
                   state: 'running',
                   atLen: (t.thinking || '').length });
  } else if (kind === 'tool_result') {
    const id = String(f.id || '');
    const row = [...t.tools].reverse()
      .find((r) => r.id === id || (r.state === 'running' && r.name === String(f.name || '')));
    if (row) {
      row.ok = f.ok !== false;
      row.state = f.skipped ? 'skipped' : 'done';
      row.result = String(f.result || f.error || '').slice(0, 600);
    }
  } else if (kind === 'verify') {
    t.tools.push({ id: `v${t.tools.length}`, name: 'verify',
                   detail: f.ok ? 'checks passed' : String(f.error || 'checks failed'),
                   ok: !!f.ok, state: 'done' });
  } else if (kind === 'plan') {
    t.plan = (f.steps as Turn['plan']) || t.plan;
  } else if (kind === 'done') {
    t.running = false;
    if (!t.text) t.text = String(f.answer || '');
    if (f.stopped) t.stopped = String(f.stopped);
    t.usage = { in: Number(f.input_tokens || 0), out: Number(f.output_tokens || 0) };
  } else if (kind === 'error') {
    t.running = false;
    t.error = String(f.text || f.error || 'the run failed');
  }
}

/** One step of the tool timeline. Kept as its own component so the timeline
 *  renderer stays readable — the glyphs and the cut-tool honesty live in one
 *  place. */
function ToolRowItem({ row, running }: { row: ToolRow; running: boolean }) {
  const vis = toolVisual(row.name);
  const info = toolLineInfo(row.name, row.args);
  const detail = info.summary || row.detail || row.note || '';
  /* A tool cut off by the stop keeps no "running" label: in a finished run
     that state only exists for the call the stop interrupted. */
  const effState = running ? row.state : (row.state === 'running' ? 'skipped' : row.state);
  const expandable = info.pairs.length > 0 || !!row.result;
  const head = (
    <>
      <span aria-hidden
            className={'flex h-5 w-5 flex-none items-center justify-center rounded-md border ' + vis.tile}>
        {vis.icon}
      </span>
      <span className="flex-none font-mono text-[11px] text-zinc-400">{row.name}</span>
      <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-zinc-muted" title={detail}>
        {detail}
      </span>
      <span className="flex flex-none items-center justify-center"
            title={effState === 'skipped' && row.state === 'running' ? 'cut before it answered — the run was stopped first' : undefined}>
        {effState === 'running'
          ? <Loader2 size={11} className="animate-spin text-zinc-400" />
          : row.ok === false
            ? <XCircle size={11} className="text-danger" />
            : effState === 'skipped'
              ? <MinusCircle size={11} className="text-warn" />
              : <CheckCircle2 size={11} className="text-ok/80" />}
      </span>
    </>
  );
  if (!expandable) {
    return <li className="flex min-w-0 items-center gap-2.5 px-3 py-[3px]">{head}</li>;
  }
  return (
    <li className="min-w-0">
      <details className="group/tool">
        <summary className="flex min-w-0 cursor-pointer list-none items-center gap-2.5 px-3 py-[3px] hover:bg-white/[0.03]">
          {head}
          <ChevronRight size={10} className="flex-none text-zinc-600 transition group-open/tool:rotate-90" />
        </summary>
        <div className="mx-3 mb-1.5 ml-10 space-y-1 rounded-md border border-white/5 bg-white/[0.02] px-2.5 py-1.5">
          {info.pairs.map(([k, v]) => (
            <div key={k} className="flex gap-2 font-mono text-[10.5px] leading-snug">
              <span className="flex-none text-zinc-600">{k}</span>
              <span className="min-w-0 whitespace-pre-wrap break-words text-zinc-400">{v}</span>
            </div>
          ))}
          {row.result && (
            <div className="flex gap-2 font-mono text-[10.5px] leading-snug">
              <span className="flex-none text-zinc-600">result</span>
              <span className={'min-w-0 whitespace-pre-wrap break-words ' + (row.ok === false ? 'text-danger/80' : 'text-zinc-500')}>
                {row.result.slice(0, 300)}
              </span>
            </div>
          )}
        </div>
      </details>
    </li>
  );
}

/** Render a slice of the thinking stream: prose stays italic, but fenced code
 *  blocks (```lang … ```) become formatted monospace blocks. Handles streaming:
 *  an opened-but-not-yet-closed fence renders its partial body as code. */
function thinkingNodes(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  /* Thinking is prose too: `**bold**`, `## headings`, lists and `code` used to
     render as literal markdown characters. It goes through the same renderer as
     the answer, at thinking size/colour (`.thinking-md`). */
  const proseCls = 'prose-chat thinking-md px-3 py-1';
  const re = /```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)(?:```|$)/g;
  let last = 0; let i = 0; let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) {
      out.push(<div key={`${keyBase}p${i}`} className={proseCls}
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(text.slice(last, m.index), 0, false, false) }} />);
    }
    out.push(
      <div key={`${keyBase}c${i}`} className="mx-3 my-1 overflow-hidden rounded-md border border-glass-border bg-black/40">
        <div className="flex items-center border-b border-glass-border bg-white/[0.03] px-2 py-0.5">
          <span className="font-mono text-[10px] lowercase tracking-wide text-zinc-500">{m[1] || 'code'}</span>
        </div>
        <pre className="overflow-x-auto p-2 font-mono text-[11px] leading-snug text-zinc-300">
          <code>{highlight(m[2].replace(/\n$/, ''), langFor(m[1], m[2]))}</code>
        </pre>
      </div>,
    );
    last = re.lastIndex; i++;
  }
  if (last < text.length) {
    out.push(<div key={`${keyBase}tail`} className={proseCls}
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(text.slice(last), 0, false, false) }} />);
  }
  return out;
}

/** Where the tool calls land inside the thinking stream. Tool events arrive
 *  as their own frames between thinking deltas, so the timeline interleaves
 *  them at the offset they fired (each row carries the `atLen` of the
 *  thinking text at that moment). Rendering one block of text then a list
 *  of tools the reader watched happen would flatten exactly the bit that
 *  makes the agent's work legible. */
function ThinkingTimeline({ thinking, tools, running }: {
  thinking?: string;
  tools: ToolRow[];
  running: boolean;
}) {
  const text = thinking || '';
  const byAt = [...tools].sort((a, b) => (a.atLen ?? 0) - (b.atLen ?? 0));
  const parts: ReactNode[] = [];
  let cursor = 0;
  for (const row of byAt) {
    const at = row.atLen ?? text.length;
    if (at > cursor) {
      parts.push(...thinkingNodes(text.slice(cursor, at), `t${cursor}`));
      cursor = at;
    }
    parts.push(<ToolRowItem key={row.id} row={row} running={running} />);
  }
  if (cursor < text.length) {
    parts.push(...thinkingNodes(text.slice(cursor), 'tail'));
  }
  return <ol className="ml-1.5 border-l border-glass-border py-1 pl-3">{parts}</ol>;
}

/** The Ctrl+Enter stack: queries waiting for the agent to finish. Rendered
 *  above the composer so a queued thought is visible, ordered and removable —
 *  a queue you cannot see is just a message that vanished. */
function QueueStack({ items, onRemove }: {
  items: string[];
  onRemove: (index: number) => void;
}) {
  if (!items.length) return null;
  return (
    <div className="-mb-px flex flex-col">
      {items.map((text, i) => (
        <div key={`${i}-${text.slice(0, 12)}`}
             className="animate-queue-in group/q relative z-10 flex items-center gap-2 rounded-t-xl border border-b-0 border-glass-border bg-surface-2 px-3 py-2 text-[12px]">
          <GripVertical size={13} aria-hidden
                        className="flex-none cursor-grab text-zinc-600" />
          <span className="min-w-0 flex-1 truncate text-zinc-300" title={text}>{text}</span>
          {items.length > 1 && (
            <span className="flex-none font-mono text-[10px] text-zinc-500">{i + 1}/{items.length}</span>
          )}
          <button type="button" onClick={() => onRemove(i)}
                  aria-label={`Remove queued message ${i + 1}`}
                  title="Remove from queue"
                  className="flex-none rounded p-0.5 text-zinc-500 opacity-60 transition hover:text-danger group-hover/q:opacity-100">
            <XCircle size={12} />
          </button>
        </div>
      ))}
    </div>
  );
}

export function AgentPanel({ sid, activePath, activeText, openFiles,
                             onOpenPath, model, disabled, onPending,
                             roots, onAddFolder,
                             onContext, onGit, onBrowseDisk,
                             projects, onOpenProject, onSwitchSession,
                             onFirstTask, onChanges, runs, onWatch,
                             onRunStarted, onNewSession, onUndo, onRedo, onTerminal,
                             onRefresh, branch, refreshBranch, setModel,
                             runsReady = true }: {
  sid: string;
  activePath: string;
  activeText: string;
  openFiles: string[];
  onOpenPath: (p: string) => void;
  /** The picker is GLOBAL by decision: a session records which model answered
   *  it, it does not configure one. The state lives in Studio so the session
   *  column and this panel show the same truth. */
  model: string;
  setModel: (v: string) => void;
  disabled: boolean;
  /** Tells the shell its unreviewed set is stale: one fetch owns that truth,
   *  and a second copy inside this panel is how the two disagree. */
  onPending?: () => void;

  roots: StudioRoot[];
  onAddFolder: () => void;
  /** Reports the context readout (chars used / ceiling) to the top bar. */
  onContext?: (chars: number, cap: number) => void;
  /** Open the Git panel. */
  onGit?: () => void;
  /** Open the in-app disk browser. */
  onBrowseDisk?: () => void;

  /** Every project the studio knows, with its sessions. */
  projects?: { path: string; name: string; sessions: StudioSession[] }[];
  onOpenProject?: (path: string) => void;
  onSwitchSession?: (sid: string) => void;
  /** Called with the first task text, so the session gets a readable name in
   *  the left column instead of "New session - 2026-11-06". */
  onFirstTask?: (text: string) => void;
  /** Opens the workbench on the unreviewed changes. */
  onChanges?: () => void;
  /** The session's runs, newest first. Owned by the shell because the left
   *  column shows the same numbers, and two copies of a live count drift. */
  runs: StudioRun[];
  /** Open the watcher stream. Called when a run starts, since the server
   *  closes the stream whenever nothing is live. */
  onWatch?: () => void;
  /** Tell the shell a run began, so it can refresh the list immediately. */
  onRunStarted?: (run: StudioRun) => void;
  /** `/new`, `/undo` and `/terminal` are the shell's verbs, not this panel's:
   *  a session switch, a checkpoint restore and a dock. Missing ones say so
   *  in the transcript instead of quietly sending the word to the model. */
  onNewSession?: () => void;
  onUndo?: () => Promise<string> | string;
  /** `/redo`: put back what the last `/undo` replaced. */
  onRedo?: () => Promise<string> | string;
  onTerminal?: () => void;
  /** Re-read the shell's own state (used by `/checks` to refresh the rail). */
  onRefresh?: () => void;
  /** The branch the session's folders point at, read by the shell (it shares
   *  the read with the session column rather than paying for a second
   *  `git status` on mount). */
  branch: string | null;
  refreshBranch: () => void;
  /** False until the session's run list is in. While it is false the panel shows
   *  a calm skeleton instead of the empty-state hero — otherwise a session with
   *  a finished, billed answer flashed the hero for a frame before the replay
   *  landed, which is what "switching sessions blinks" was. */
  runsReady?: boolean;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  /* Queries typed while the agent is busy. Ctrl+Enter pushes here instead of
     sending; the stack drains one at a time as each run finishes. */
  const [queue, setQueue] = useState<string[]>([]);
  /* True from mount until the replay says "here are the turns" or "there are
     none". Without this gate the empty hero paints for one frame between the
     runs list arriving and the frames loading — the refresh flash. */
  const [replaying, setReplaying] = useState(true);
  /* The floating ↓ appears only when the reader has scrolled away from the
     newest turn — when they are already at the bottom it would be noise. */
  const [showScrollDown, setShowScrollDown] = useState(false);
  const [running, setRunning] = useState(false);
  /* The run this tab is driving. Set from the `started` frame, and the only
     way to stop the AGENT rather than this fetch. */
  const [runId, setRunId] = useState('');
  /* Problems the person attached with `#`. Held here, not in the composer, so
     a replayed transcript and the live chips read the same list. */
  const [problems, setProblems] = useState<ProblemRow[]>([]);
  /* The newest plan that still has work left. It is painted on the composer,
     not in the transcript: a checklist belongs beside the box you are typing in
     while the agent works, and it disappears when the last step is done. */
  const livePlan = useMemo(() => {
    for (let i = turns.length - 1; i >= 0; i--) {
      const p = turns[i].plan;
      if (p?.length) {
        return p.every((s) => /done|complete/i.test(s.status)) ? undefined : p;
      }
    }
    return undefined;
  }, [turns]);
  /* `@browser` — the page this session's browser is on, read on demand. */
  const [browserPage, setBrowserPage] = useState<BrowserPageRef | null>(null);
  const refreshBrowserPage = useCallback(async () => {
    try {
      const s = await api.studioBrowserStatus(sid);
      if (!s.running) { setBrowserPage(null); return; }
      const p = await api.studioBrowserPage(sid, 'read');
      const log = await api.studioBrowserEvents(sid, 'console');
      setBrowserPage({
        url: String(p.url || s.url || ''),
        text: String(p.text || ''),
        title: '',
        errors: (log.events || []).filter((e) => e.level === 'error')
          .map((e) => String(e.text || '')).slice(0, 8),
      });
    } catch { setBrowserPage(null); }
  }, [sid]);
  /* The composer's live readout: hops used against the server's own cap, and
     the wall clock of the run rather than of the render loop. */
  const [meta, setMeta] = useState<RunMeta>({});
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!running) return undefined;
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [running]);
  void tick;
  /* Ask ⇄ Agent is the read-only switch, surfaced as a mode rather than a
     checkbox: it decides whether the agent can touch a file at all, which is
     the first thing a user wants to see, not a footnote. */
  const [readOnly, setReadOnly] = useState(false);
  /* Manual → auto files → auto files+commands. Three levels, one control,
     because "auto" as a checkbox hid the fact that running the project's own
     checks is a different risk from editing a file. */
  const [approval, setApproval] = useState<'manual' | 'files' | 'files+exec'>('manual');
  /* An approval this tab owes an answer to. Rendered as a card in the
     transcript, never a modal: a modal blocks the rest of the surface while a
     decision that belongs inline is being asked for. */
  const [asked, setAsked] = useState<Record<string, unknown> | null>(null);
  const [menu, setMenu] = useState<'' | 'mode' | 'approval' | 'folder' | 'model' | 'projects'>('');
  const abortRef = useRef<AbortController | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);
  /* Only chase the bottom when the reader is already there — a person who
     scrolled up to reread an answer must not be yanked down by the next
     delta. */
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 160) {
      el.scrollTop = el.scrollHeight;
    }
  }, [turns]);

  /* Replay on mount: the run this session finished last is rebuilt from its
     transcript, so a reload does not throw away an answer that was already
     paid for.
     *
     Frames are folded through the same reducer the live stream uses
     (`foldFrame`), which is the only reason a replay cannot disagree with a
     live run about what the transcript looked like.
     *
     It waits for the run LIST rather than running on the mount tick: on a
     cold load `runs` is still empty when this panel first renders, and an
     effect keyed only on `sid` then decided there was nothing to replay —
     which is why reloading the studio landed on the hero question with a
     finished, billed answer missing from the transcript. `replayedFor` keeps
     it to exactly one replay per session, so a `runs` update that arrives
     while the person is working cannot overwrite the live transcript. */
  const replayedFor = useRef('');
  const sidRef = useRef(sid);
  sidRef.current = sid;
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  /* A session switch is a different conversation: clear the transcript, or the
     next session shows the previous one's turns (or nothing, if the replay was
     skipped because the old turns were still in state). */
  const prevSid = useRef(sid);
  useEffect(() => {
    if (prevSid.current === sid) return;
    prevSid.current = sid;
    setTurns([]);
    setRunning(false);
    setRunId('');
    replayedFor.current = '';
    setReplaying(true);
  }, [sid]);
  useEffect(() => {
    if (!runsReady) return;                            // the run list is still in flight
    if (replayedFor.current === sid) return;
    replayedFor.current = sid;                         // claim the decision once
    if (turns.length) { setReplaying(false); return; }
    /* Load ALL prior runs, oldest first, up to _CODE_ASK_HISTORY_TURNS
       turns (8) so the agent can see the full conversation history rather
       than just the most recent turn. */
    const finished = runs.filter((r) => r.status !== 'running'
      && r.status !== 'stopping');
    const candidates = finished.length ? finished : runs;
    if (!candidates.length) { setReplaying(false); return; }  // genuinely empty session
    const mySid = sid;
    (async () => {
      try {
        /* Oldest first — runs are newest-first; take the last 8 (the history
           window) and reverse so a reload restores the full conversation the
           next call will see, not just the most recent exchange. */
        const toLoad = candidates.slice(0, 8).reverse();
        const rebuilt: Turn[] = [];
        for (const run of toLoad) {
          const got = await api.studioRunFrames(mySid, run.id);
          if (!mounted.current || sidRef.current !== mySid) return;
          if (!got?.frames?.length) continue;
          rebuilt.push({ id: `replay-q-${run.id}`, role: 'user',
                         text: run.question, tools: [], running: false });
          const aria: Turn = { id: `replay-a-${run.id}`, role: 'aria',
                                text: '', tools: [], running: false };
          for (const f of got.frames) foldFrame(aria, f);
          aria.elapsed = Math.max(0, Math.round((run.ended || 0)
            - (run.started || 0)));
          if (run.status !== 'running' && run.status !== 'stopping') {
            void api.studioRunCost(mySid, run.id)
              .then((c) => setTurns((all) => all.map((t) => (t.id === `replay-a-${run.id}`
                ? { ...t, cost: c } : t))))
              .catch(() => { /* the token line stays, which is still the truth */ });
          }
          rebuilt.push(aria);
        }
        if (mounted.current && sidRef.current === mySid && rebuilt.length) setTurns(rebuilt);
      } catch { /* an empty transcript is simply an empty panel */ }
      finally { if (mounted.current && sidRef.current === mySid) setReplaying(false); }
    })();
    // `runs` is a dep because the list arrives after the first render, and
    // `replayedFor` — not the key — is what keeps this to one replay per
    // session. Every later runs update returns at the first line.
  }, [sid, runs, runsReady, turns.length]);

  /* Close any open composer menu on an outside click. The menus are the only
     overlay in this column, so one listener serves all three rather than three
     listeners racing each other shut. */
  useEffect(() => {
    if (!menu) return undefined;
    const onDown = (e: MouseEvent) => {
      const t = e.target;
      if (t instanceof Element && !t.closest('[data-composer-menu]')) setMenu('');
    };
    window.addEventListener('mousedown', onDown);
    return () => window.removeEventListener('mousedown', onDown);
  }, [menu]);

  /* The branch is read by the shell (it shares the read with the session
     column). Kept as props so the two never disagree. */

  const patch = useCallback((id: string, fn: (t: Turn) => Turn) => {
    setTurns((all) => all.map((t) => (t.id === id ? fn(t) : t)));
  }, []);

  /* The unreviewed-set truth is the shell's: one fetch owns it (the shell's
     `loadPending` feeds the session column and the changes bar). This panel's
     copy of it was how the two disagreed. */
  const refreshChanges = useCallback(() => {
    onPending?.();
  }, [onPending]);

  /* Ctrl+Enter lands here. The composer clears its own box; the stack is the
     visible record of what will run next. */
  const enqueue = useCallback((text: string) => {
    const t = text.trim();
    if (t) setQueue((q) => [...q, t]);
  }, []);
  const removeQueued = useCallback((index: number) => {
    setQueue((q) => q.filter((_, i) => i !== index));
  }, []);

  const send = useCallback(async (override?: string) => {
    const question = (override ?? input).trim();
    if (!question || running) return;
    setInput('');
    if (!turns.length) onFirstTask?.(question);
    setRunning(true);
    const userId = nextId();
    const ariaId = nextId();
    setTurns((all) => [...all,
      { id: userId, role: 'user', text: question, tools: [], running: false },
      { id: ariaId, role: 'aria', text: '', tools: [], running: true }]);
    const history = turns
    .slice(-(8 * 2))
    .map((t) => ({
      role: (t.role === 'user' ? 'user' : 'assistant') as 'user' | 'assistant',
      content: historyContent(t),
    }))
    .filter((m) => m.content !== '');

  const ctrl = new AbortController();
    abortRef.current = ctrl;
    let sawText = false;
    let hops = 0;
    let ridRun = '';
    const startedAt = Date.now() / 1000;
    const deltaBuffer = { text: '', thinking: '' };
    let deltaScheduled = false;
    const flushDeltas = () => {
      deltaScheduled = false;
      const { text, thinking } = deltaBuffer;
      deltaBuffer.text = '';
      deltaBuffer.thinking = '';
      if (text) patch(ariaId, (t) => ({ ...t, text: t.text + text }));
      if (thinking) patch(ariaId, (t) => ({ ...t, thinking: (t.thinking || '') + thinking }));
    };
    /* One flush per animation frame. The 45ms timer coalesced too coarsely —
       the stream read as "blocks of text landing" rather than text flowing;
       painting every delta at 60fps is what the rest of the console looks
       like. The cost is one patch() per frame, same work small deltas did
       before the batching existed. */
    const queueDelta = (kind: 'text' | 'thinking', chunk: string) => {
      deltaBuffer[kind] += chunk;
      if (deltaScheduled) return;
      deltaScheduled = true;
      requestAnimationFrame(flushDeltas);
    };
    setMeta({ startedAt, running: true, hops: 0,
             contextChars: question.length + activeText.length });
    try {
      for await (const f of api.codeAgentStream(
        question, activePath, activeText, history, ctrl.signal,
        model || undefined, undefined, openFiles,
        approval !== 'manual', readOnly,
        { tabs: openFiles, diagnostics: problems,
          ...(browserPage ? { browser_page: browserPage } : {}) },
        approval === 'files+exec',
        sid, sid)) {
        /* Widened on purpose. `CodeAgentFrame` types the frames this app has
           always consumed; the skip/note/write fields come from the same
           `tool_result` frame and are simply not in the interface yet, and
           inventing optional fields there would make the old Code page's
           compiler check weaker to fix a studio lint. */
        const frame = f as CodeAgentFrame & {
          skipped?: boolean; note?: string; write?: boolean;
          /* `run` arrives on the `started` frame and is the id every later
             verb — Stop, cost, replay — is keyed on. Not in the shared
             interface yet, for the same reason as the fields above. */
          run?: string; sid?: string; turn?: string;
          /* The ceilings this run was given, and the gateway session its
             money lands in. Also slice-6 additions to a shared frame type. */
          max_hops?: number; max_context_chars?: number;
          gateway_session?: string;
          /* Tool-call arguments and result excerpt, so the timeline can show
             which file / lines / edit each call touched. */
          arguments?: Record<string, unknown>; result?: string;
          context_chars?: number;
          chars?: number; cap?: number;
        };
        const kind = frame.type || '';
        if (kind === 'started') {
          /* The run's own id, from the server. Everything about stopping,
             listing and costing this agent hangs off it, so it is taken before
             any other frame can arrive — and the watcher stream is opened now,
             because the server closes it whenever nothing is live.
             The ceilings come on the same frame: the chip that says "hop 7"
             needs the cap the SERVER is enforcing, because a cap copied into
             this file is a number that quietly goes out of date. */
          const rid = String(frame.run || '');
          ridRun = rid;
          if (rid) {
            setRunId(rid);
            patch(ariaId, (t) => ({ ...t, run: rid,
                                    maxHops: frame.max_hops === undefined
                                      ? t.maxHops : Number(frame.max_hops),
                                    contextCap: frame.max_context_chars === undefined
                                      ? t.contextCap : Number(frame.max_context_chars),
                                    gwSession: frame.gateway_session
                                      ? String(frame.gateway_session) : t.gwSession }));
            setMeta((m) => ({ ...m,
                              maxHops: frame.max_hops === undefined
                                ? m.maxHops : Number(frame.max_hops),
                              contextCap: frame.max_context_chars === undefined
                                ? m.contextCap : Number(frame.max_context_chars) }));
            onWatch?.();
            onRunStarted?.({ id: rid, sid, question, status: 'running',
                             started: Date.now() / 1000, live: true });
          }
        } else if (kind === 'permission') {
          /* An approval request is a card in the transcript, never a modal: a
             modal freezes the surface that is asking to be read while the
             answer is being decided, and the run is parked either way. */
          setAsked(frame as unknown as Record<string, unknown>);
          patch(ariaId, (t) => ({ ...t,
            tools: [...t.tools, {
              id: String(frame.id || 'ask'), name: String(frame.tool || 'write'),
              detail: String(frame.summary || ''), state: 'running',
              atLen: (t.thinking || '').length }] }));
        } else if (kind === 'delta' || kind === 'text') {
          sawText = true;
          queueDelta('text', frame.text || '');
        } else if (kind === 'thinking') {
          queueDelta('thinking', frame.text || '');
        } else if (kind === 'tool_call') {
          /* A tool call means a model call happened to make it, so this is the
             hop counter — read off the stream rather than guessed from elapsed
             time, and bounded by the server's own cap in the chip below. */
          hops += 1;
          setMeta((m) => ({ ...m, hops }));
          patch(ariaId, (t) => ({ ...t, tools: [...t.tools, {
            id: String(frame.id || frame.name || nextId()),
            name: String(frame.name || 'tool'),
            detail: frame.detail, state: 'running',
            args: frame.arguments,
            atLen: (t.thinking || '').length }] }));
        } else if (kind === 'tool_result') {
          patch(ariaId, (t) => ({ ...t, tools: t.tools.map((row) => (
            row.id === String(frame.id || '') || row.name === String(frame.name || '')
              && row.state === 'running'
              ? { ...row, ok: frame.ok !== false,
                  state: frame.skipped ? 'skipped' : 'done',
                  note: frame.note || row.note,
                  result: String(frame.result || '').slice(0, 600) || row.result }
              : row)) }));
          /* Any write tool, not just one of them. The trigger used to name
             `write_file` alone, which is not a tool this agent has: an
             `edit_file` or `create_file` landed on disk and the panel kept
             showing nothing until the next reload — the review list looked
             empty right after the change that created it. */
          if (frame.write || /^(edit_file|create_file|update_file|write_file|delete_file|make_dir)$/.test(String(frame.name || ''))) {
            void refreshChanges();
          }
        } else if (kind === 'context') {
          setMeta((m) => ({ ...m,
            contextChars: Number(frame.chars || 0) || m.contextChars,
            contextCap: Number(frame.cap || 0) || m.contextCap }));
        } else if (kind === 'status') {
          if (!sawText) patch(ariaId, (t) => ({ ...t, text: t.text || '' }));
        } else if (kind === 'verify') {
          patch(ariaId, (t) => ({ ...t, tools: [...t.tools, {
            id: `v${t.tools.length}`, name: 'verify',
            detail: frame.ok ? 'checks passed' : (frame.error || 'checks failed'),
            ok: !!frame.ok, state: 'done' }] }));
        } else if (kind === 'done') {
          flushDeltas();
          setAsked(null);
          setRunId('');
          const elapsed = Math.max(0, Math.round(Date.now() / 1000 - startedAt));
          patch(ariaId, (t) => ({
            ...t, running: false,
            text: t.text || String(frame.answer || ''),
            stopped: frame.stopped || undefined,
            elapsed,
            usage: { in: Number(frame.input_tokens || 0),
                     out: Number(frame.output_tokens || 0) },
          }));
          setMeta((m) => ({ ...m, running: false,
            contextChars: Number(frame.context_chars || 0) || m.contextChars }));
          /* The money, from the gateway ledger under this run's own session.
             Tokens x a price typed into this file is not a cost: it misses
             cache reads, reasoning tokens and every hop that failed after
             being billed. A failure here leaves `cost` unset and the panel
             shows the token counts it has, which is honest, rather than $0. */
          if (ridRun) {
            void api.studioRunCost(sid, ridRun)
              .then((c) => patch(ariaId, (t) => ({ ...t, cost: c })))
              .catch(() => patch(ariaId, (t) => ({ ...t,
                cost: { unknown: true, usd: null,
                        note: 'the ledger was not reachable' } })));
          }
        } else if (kind === 'error') {
          patch(ariaId, (t) => ({ ...t, running: false,
            error: frame.error || frame.text || 'the run failed' }));
          setMeta((m) => ({ ...m, running: false }));
        }
      }
    } catch (e) {
      patch(ariaId, (t) => ({ ...t, running: false,
        error: e instanceof Error ? e.message : String(e) }));
    } finally {
      flushDeltas();
      setRunning(false);
      abortRef.current = null;
      void refreshChanges();
    }
  }, [input, running, turns.length, turns, activePath, activeText, openFiles, approval,
      readOnly, model, sid, patch, refreshChanges, onFirstTask, onWatch,
      onRunStarted, problems, browserPage]);

  /* Drain the Ctrl+Enter stack: each finished run releases the next query.
     Keyed on `running` and `queue`, so it fires exactly when a run ends (or
     when a query is queued while idle) and never mid-run. */
  useEffect(() => {
    if (running || !queue.length) return;
    const [next, ...rest] = queue;
    setQueue(rest);
    void send(next);
  }, [running, queue, send]);

  /* A local note in the transcript. Commands answer here rather than in a
     toast: the record of what was asked and what happened belongs in the same
     column as the conversation, and a toast is gone before it is read. */
  const note = useCallback((text: string, tone: 'info' | 'warn' = 'info') => {
    setTurns((all) => [...all, { id: nextId(), role: 'note', tone,
                                 text, tools: [], running: false } as Turn]);
  }, []);

  /* A `file-ref` button in the answer: a path goes to the review surface;
     a bare filename (`renamed.txt`, `app.py`) goes through the mentions
     route — exactly one exact match opens it, anything else says so in the
     transcript rather than guessing. */
  const resolveFileRef = useCallback((ref: string) => {
    /* A leading ./ is the composer's own relative vocabulary; strip it before
       deciding it is a bare filename or a path. */
    const cleaned = ref.replace(/^(?:\.[\\/])+/, '');
    const bare = !/[\\/]/.test(cleaned) && !/^[A-Za-z]:/.test(cleaned);
    if (!bare) { onOpenPath?.(cleaned); return; }
    void (async () => {
      try {
        const r = await api.studioMentions(sid, cleaned);
        const exact = (r.matches || []).filter((m) =>
          !m.dir && m.name && m.name.toLowerCase() === cleaned.toLowerCase());
        if (exact.length === 1) { onOpenPath?.(exact[0].path); return; }
        note(exact.length
          ? `${exact.length} files are called ${cleaned} — open one from the tree instead`
          : `no file called ${cleaned} in this session's folders`, 'warn');
      } catch {
        note(`could not look for ${cleaned}`, 'warn');
      }
    })();
  }, [sid, onOpenPath, note]);

  /** The `/` commands. Each one either is a verb this panel owns, delegates
   *  to the shell through a prop, or says plainly that it is not wired — the
   *  last of those being the only honest alternative to sending "/undo" to the
   *  model as a question and letting it pretend. */
  const runCommand = useCallback(async (name: string, rest: string) => {
    if (name === 'stop') { if (running) await stopRef.current?.(); else note('nothing is running', 'warn'); return; }
    if (name === 'new') {
      if (onNewSession) { onNewSession(); note('started a new session'); }
      else note('the shell did not give this panel a way to start a session', 'warn');
      return;
    }
    if (name === 'undo') {
      if (!onUndo) { note('undo is not wired into this shell', 'warn'); return; }
      const msg = await onUndo();
      note(msg || 'restored the last checkpoint');
      return;
    }
    if (name === 'redo') {
      if (!onRedo) { note('redo is not wired into this shell', 'warn'); return; }
      const msg = await onRedo();
      note(msg || 'put back what the last undo replaced');
      return;
    }
    if (name === 'review') { onChanges?.(); note('opened the review editor'); return; }
    if (name === 'terminal') {
      if (onTerminal) { onTerminal(); note('showed the dock'); }
      else note('the terminal is a later slice', 'warn');
      return;
    }
    if (name === 'roots') {
      note(roots.length
        ? `this session holds: ${roots.map((r) => r.name).join(', ')}`
        : 'this session holds no folder yet', roots.length ? 'info' : 'warn');
      return;
    }
    if (name === 'checks') {
      note('running the project checks…');
      try {
        const r = await api.codeChecks(undefined, undefined, sid);
        setProblems(checksToRows(r));
        note(`${checksSummary(r)}\n\n`
             + (r.results || []).filter((x) => !x.ok && x.available !== false)
               .map((x) => `--- ${x.tool} ---\n${String(x.stderr || x.stdout || '').slice(0, 1200)}`)
               .join('\n\n') || 'no output', r.ok ? 'info' : 'warn');
        onRefresh?.();
      } catch (e) {
        note(`the checks could not run: ${e instanceof Error ? e.message : e}`, 'warn');
      }
      return;
    }
    if (name === 'verify') {
      /* The verification half of the loop: run the project's checks and say
         plainly whether the work passes, so a turn cannot report success on
         an unverified change. */
      note('verifying the project…');
      try {
        const r = await api.codeChecks(undefined, undefined, sid);
        setProblems(checksToRows(r));
        const ran = (r.results || []).filter((x) => x.available !== false);
        const bad = ran.filter((x) => !x.ok);
        /* Nothing ran is not a pass. A project with no detectable checker used
           to fall through to "verify passed", which is the one sentence this
           command exists to prevent. */
        note(!ran.length
          ? `verify could not run: ${String(r.error || 'no checker detected — '
             + 'nothing verified this change')}`
          : bad.length
            ? `verify failed: ${bad.map((x) => x.tool).join(', ')}`
            : `verify passed · ${checksSummary(r)}`,
             bad.length || !ran.length ? 'warn' : 'info');
        onRefresh?.();
      } catch (e) {
        note(`verify could not run: ${e instanceof Error ? e.message : e}`, 'warn');
      }
      return;
    }
    if (name === 'commit-msg') {
      note('drafting a commit message from the staged diff…');
      try {
        const r = await api.studioCommitMsg(sid, rest !== 'working', model || undefined);
        if (r?.error) { note(`no message: ${r.error}`, 'warn'); return; }
        const text = String(r.text || '');
        setInput(text);
        composerRef.current?.focus();
        note([
          `wrote from the ${r.staged ? 'STAGED' : 'WORKING'} diff in ${r.repo}`,
          `${r.files?.length ?? 0} file(s) changed, `
            + `${r.files_with_diffs?.length ?? 0} with their hunks`,
          r.truncated
            ? `hunks left out (named to the model, not shown): ${r.omitted?.join(', ')}`
            : '',
          'the text is in the composer — nothing has been committed.',
        ].filter(Boolean).join('\n'));
      } catch (e) {
        note(`the commit message failed: ${e instanceof Error ? e.message : e}`, 'warn');
      }
      return;
    }
    note(`/${name} is not a command this studio has. The list the server owns `
         + 'is what this popup shows, so an unknown verb is a real gap, not a '
         + 'typo to guess at.', 'warn');
  }, [running, sid, model, roots, note, onChanges, onNewSession, onTerminal,
      onUndo, onRedo, onRefresh, setInput]);

  const patchPendingStop = useCallback((why: string) => {
    setTurns((all) => all.map((t) => (t.running
      ? { ...t, running: false, stopped: why }
      : t)));
  }, []);

  /* Stop the AGENT, not just this view. Two verbs, both needed:
       - the registry stop ends the loop at its next hop, from any tab;
       - aborting the fetch closes this stream.
     Doing only the second is what the old panel did, and it left the model
     buying calls after the user pressed Stop. The abort follows the stop
     rather than replacing it, and the label says which one happened. */
  const stop = useCallback(async () => {
    if (runId) {
      const r = await api.studioRunStop(sid, runId).catch(() => null);
      if (r && r.ok === false) {
        // Not live any more: the run finished between the last frame and the
        // click. Say that instead of pretending the button worked.
        patchPendingStop(String(r.error || 'that run had already finished'));
        return;
      }
    }
    abortRef.current?.abort();
    patchPendingStop(runId ? 'stopped at the next hop' : 'closed this view');
  }, [runId, sid, patchPendingStop]);

  /* `/stop` is defined above `stop` in this file, and a callback that closed
     over it would capture the first render's version. A ref keeps one truth. */
  const stopRef = useRef<typeof stop | null>(null);
  stopRef.current = stop;

  /** Answer one parked write. The run is waiting on exactly this POST, so a
   *  card with no answer path is a hung agent, and a refusal is a real answer
   *  — the model is told and carries on rather than stalling. */
  const answer = useCallback(async (id: string, approve: boolean, remember = false) => {
    setAsked(null);
    try {
      await api.codePermission(id, approve, remember);
    } catch (e) {
      setTurns((all) => all.map((t) => (t.running
        ? { ...t, error: `the approval could not be sent: ${e instanceof Error ? e.message : e}` }
        : t)));
    }
  }, []);

  /* The file-level keep/revert verbs used to live in this column. They moved
     to the review surface, where a decision is made per hunk against the
     baseline the server will actually restore to — two panels offering two
     different "revert this file" buttons is how a user ends up undoing work
     they had already accepted. */
  /* Refresh on mount and whenever the session changes — keyed on `sid`, NOT on
     `refreshChanges`. The callback's identity depends on a prop the parent
     recreates each render, so depending on it made this effect re-run on every
     render: fetch → setChanges → render → new callback → fetch. Unbounded,
     three requests a round, and the page stops responding. A child must never
     be able to be driven in circles by an unstable parent. */
  const refreshRef = useRef(refreshChanges);
  refreshRef.current = refreshChanges;
  useEffect(() => { void refreshRef.current(); }, [sid]);

  /* Two states, not one: `empty` = nothing is coming (render the hero), `settling`
     = the list hasn't arrived yet (render a calm skeleton). Showing the hero
     before runsReady made every session switch flash it. */
  const empty = !turns.length && runsReady && !replaying;
  const settling = !turns.length && (!runsReady || replaying);
  /* The context ring: how much of the selected model's window this session has
     used. Before the first run there is no measurement, and the ring says so
     rather than claiming "0%" of a window it never saw. */
  const ctxChars = meta.contextChars || 0;
  const ctxCap = meta.contextCap || 0;
  /* The top bar draws the ring; this is the only wire it needs. */
  useEffect(() => { onContext?.(ctxChars, ctxCap); }, [ctxChars, ctxCap, onContext]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={scroller}
           onScroll={(e) => {
             const el = e.currentTarget;
             setShowScrollDown(el.scrollHeight - el.scrollTop - el.clientHeight > 160);
           }}
           className={'relative min-h-0 flex-1 overflow-auto '
             + (empty ? 'flex flex-col items-center justify-center px-6' : 'px-6 py-5')}>
        {settling && (
          <div aria-hidden
               className="mx-auto flex w-full max-w-[760px] flex-col gap-4 pt-8">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-[92px] animate-pulse rounded-[10px] border border-white/5 bg-white/[0.03]" />
            ))}
          </div>
        )}
        {empty && (
          <div className="flex w-full max-w-[720px] flex-col items-center">
            {/* Pixel wordmark. No icon tile, no sentence, no second call to
                action — the composer is the action. */}
            <h1 aria-label="Aria"
                className="mb-6 select-none text-center font-mono text-[44px] font-black uppercase leading-none tracking-[0.24em] text-zinc-50"
                style={{ textShadow: '3px 3px 0 rgba(139,124,246,0.30)' }}>
              ARIA
            </h1>
            {/* The only thing possible with no folder is opening one — make it
                the primary action instead of the small chip under the composer. */}
            <button type="button" onClick={() => onBrowseDisk?.()}
                    className="mb-4 flex items-center gap-2 rounded-lg border border-accent/40 bg-accent/10 px-4 py-2 text-[13px] font-medium text-zinc-100 transition-colors hover:bg-accent/20">
              <FolderOpen size={15} className="text-accent" aria-hidden />
              Open a project folder
            </button>
            <Composer {...{ input, setInput, disabled, readOnly, setReadOnly,
                             approval, setApproval, menu, setMenu, roots,
                             onAddFolder, branch, refreshBranch, running, send, stop,
                             composerRef, wide: true, sid, model, setModel, plan: livePlan, onEnqueue: enqueue,
                             onCommand: (n: string, r: string) => { void runCommand(n, r); },
                             meta, problems, setProblems, onPickFile: onOpenPath,
                             browserPage, setBrowserPage, onGit, onBrowseDisk, projects, onOpenProject, onSwitchSession,
                             refreshBrowser: refreshBrowserPage }} />
          </div>
        )}

        {!empty && (
          <div className="mx-auto flex w-full max-w-[760px] flex-col gap-5">
            {turns.map((t) => (
              <div key={t.id} className="min-w-0">
                {t.role === 'note' ? (
                  /* A command's report, in the transcript's order but visibly
                     not the agent's voice — the alternative is a `/checks`
                     output that reads like an answer nobody asked for. */
                  <p role="status"
                     className={'whitespace-pre-wrap break-words rounded-md border px-3 py-1.5 '
                       + 'text-[12px] leading-relaxed '
                       + (t.tone === 'warn'
                         ? 'border-warn/40 bg-warn-dim text-warn'
                         : 'border-glass-border bg-surface-1 text-zinc-300')}>
                    {t.text}
                  </p>
                ) : t.role === 'user' ? (
                  /* A chat reads by side: what YOU said sits right, what the
                     agent answered sits left. The old full-width left card
                     made every turn the same shape at the same edge, so a
                     scrolled transcript was a wall you had to parse. The
                     bubble is tinted and capped in measure so the eye lands
                     on the question, then travels left to the answer. */
                  <div className="flex flex-col items-end">
                    <p className="mb-1"><Label>You</Label></p>
                    <div className="max-w-[85%] rounded-[10px] border border-white/15 bg-white/[0.05] px-3.5 py-2.5">
                      <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-zinc-100">
                        {t.text}
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {(!!t.thinking || t.tools.length > 0) && (
                      /* One line, one expandable thing: the run's own words AND
                         its work log, together. A separate "Work" card and a
                         separate thinking block made the transcript two cards
                         tall; the agent's thought and its tools are the same
                         activity and now read as one small line. */
                      <details open={t.running}
                               className="group/act">
                        {/* Sticky so the way OUT of a long thought is always on
                            screen. The transcript is the scroll container; with
                            the bar pinned to its top, a 200-line thought can be
                            collapsed from anywhere instead of only from its
                            first line. No `overflow-hidden` here — that would
                            make the details the scroll container and the bar
                            would scroll away with it. */}
                        <summary className="sticky top-0 z-10 flex cursor-pointer list-none items-center gap-2 bg-[#0e0e12] px-3 py-1.5 text-[12.5px] text-zinc-400 transition-colors hover:text-zinc-200">
                          <span aria-hidden
                                className="flex-none text-zinc-600 transition group-open/act:rotate-90"><ChevronRight size={11} /></span>
                          <Sparkles size={10} aria-hidden className="flex-none text-zinc-500" />
                          <span className="flex-none">{t.running ? 'Working' : 'Used'}</span>
                          {!!t.tools.length && (
                            <span className="min-w-0 truncate font-medium text-zinc-300">{activitySummary(t.tools)}</span>
                          )}
                          {!!t.tools.filter((x) => x.ok === false).length && (
                            <span className="flex-none text-danger">{t.tools.filter((x) => x.ok === false).length} failed</span>
                          )}
                        </summary>
                        <div className="border-t border-white/5">
                          {(t.thinking || t.tools.length > 0) && (
                            <ThinkingTimeline thinking={t.thinking} tools={t.tools}
                                              running={t.running} />
                          )}
                        </div>
                      </details>
                    )}
                    {t.text && <AnswerBody text={t.text} sid={sid}
                               onOpenPath={resolveFileRef}
                               onDecision={refreshChanges} />}
                    {t.stopped && !/\[stopped:/.test(t.text || '') && (
                      /* The run's reason arrives twice when there is no answer
                         to carry it: the server writes it into `answer` as
                         "[stopped: …]" AND the done frame keeps `stopped`.
                         Printing both showed the same sentence twice in two
                         typefaces, which reads as two different failures. */
                      /already-successful call/i.test(t.stopped || '') ? (
                        <p className="text-[12px] text-zinc-muted">
                          Paused after repeating a step — the work above is complete;
                          continue when you are ready.
                        </p>
                      ) : (
                        <p className="text-[12px] text-warn">stopped early: {t.stopped}</p>
                      )
                    )}
                    {t.error && (
                      <p className="text-[12px] text-danger">{t.error}</p>
                    )}
                    {/* One line: the money and the seconds, both measured.
                        `cost unknown` is spelled out rather than drawn as a
                        fake $0. The call and step counts live in the session
                        column (Usage) — they do not need to repeat here. */}
                    {(!!t.usage && (t.usage.in || t.usage.out)) || t.elapsed !== undefined ? (
                      <p className="font-mono text-[12px] text-zinc-muted">
                        {t.cost && !t.cost.unknown
                          ? `$${(t.cost.usd ?? 0).toFixed(4)}`
                          : t.cost?.unknown
                            ? 'cost unknown (ledger unreachable)'
                            : t.running ? 'cost pending' : ''}
                        {t.elapsed !== undefined && (t.elapsed > 0 ? ` · ${t.elapsed}s` : '')}
                        {model ? ` · ${model.split('/').slice(-1)[0]}` : ''}
                        {t.usage?.out && t.elapsed && t.elapsed > 0
                          ? ` · ${Math.round(t.usage.out / t.elapsed)} tok/s`
                          : ''}
                      </p>
                    ) : null}
                    {t.running && !t.text && (
                      <p className="text-[12px] text-zinc-muted">working…</p>
                    )}
                  </div>
                )}
              </div>
            ))}

            {/* The parked write. Inline, in the order it happened, because the
                run is stopped right here and the context that explains the
                diff is the two lines above it. */}
            {asked && (
              <div role="group" aria-label="Approval requested"
                   className="rounded-[10px] border border-warn/40 bg-warn-dim p-3">
                <p className="text-[12px] text-warn">
                  The agent wants to {String(asked.tool || 'write')}
                  {asked.path ? <> · <span className="font-mono">{String(asked.path)}</span></> : null}
                </p>
                {asked.summary ? (
                  <p className="mt-1 text-[12px] text-zinc-300">{String(asked.summary)}</p>
                ) : null}
                {asked.diff ? (
                  <pre className="mt-2 max-h-[220px] overflow-auto rounded-md border border-glass-border bg-void p-2 text-[12px] leading-snug text-zinc-300">
                    {String(asked.diff).slice(0, 4000)}
                  </pre>
                ) : null}
                <div className="mt-2.5 flex flex-wrap items-center gap-2">
                  <button type="button"
                          onClick={() => void answer(String(asked.id || ''), true)}
                          className="rounded-md border border-ok/40 bg-ok-dim px-2.5 py-1 text-[12px] text-ok hover:bg-ok/20">
                    Approve
                  </button>
                  {asked.rememberable ? (
                    <button type="button"
                            onClick={() => void answer(String(asked.id || ''), true, true)}
                            title="Allow this exact command for the rest of this session"
                            className="rounded-md border border-glass-border px-2.5 py-1 text-[12px] text-zinc-200 hover:bg-white/5">
                      Approve and remember
                    </button>
                  ) : null}
                  <button type="button"
                          onClick={() => void answer(String(asked.id || ''), false)}
                          className="rounded-md border border-danger/40 bg-danger-dim px-2.5 py-1 text-[12px] text-danger hover:bg-danger/20">
                    Refuse
                  </button>
                  <span className="min-w-0 flex-1 text-[12px] text-zinc-muted">
                    Refusing is an answer: the model is told and carries on.
                  </span>
                </div>
              </div>
            )}

          </div>
        )}
        {showScrollDown && (
          <button type="button"
                  onClick={() => scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' })}
                  aria-label="Scroll to bottom" title="Scroll to bottom"
                  className="sticky bottom-3 left-1/2 z-20 flex h-7 w-7 -translate-x-1/2 items-center justify-center rounded-md border border-glass-border bg-surface-1 text-zinc-300 shadow-md transition-colors hover:text-zinc-50">
            <ArrowDown size={14} />
          </button>
        )}
      </div>

      {/* With a transcript the composer stays the same card, pinned below it —
          one shape in both states, so the thing you type into never moves. */}
      {!empty && (
        <div className="flex-none px-6 pb-4">
          <div className="mx-auto w-full max-w-[760px]">
            <QueueStack items={queue} onRemove={removeQueued} />
            <Composer {...{ input, setInput, disabled, readOnly, setReadOnly,
                             approval, setApproval, menu, setMenu, roots,
                             onAddFolder, branch, refreshBranch, running, send, stop,
                             composerRef, wide: false, sid, model, onEnqueue: enqueue,
                             setModel, plan: livePlan,
                             onCommand: (n: string, r: string) => { void runCommand(n, r); },
                             meta, problems, setProblems, onPickFile: onOpenPath,
                             browserPage, setBrowserPage, onGit, onBrowseDisk, projects, onOpenProject, onSwitchSession,
                             refreshBrowser: refreshBrowserPage }} />
          </div>
        </div>
      )}
    </div>
  );
}
