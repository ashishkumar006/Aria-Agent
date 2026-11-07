import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronRight, FolderOpen, GitCompareArrows, Sparkles } from 'lucide-react';
import { api, type CodeAgentFrame } from '../../api';
import type { StudioRoot, StudioRun, StudioRunCost } from '../../api';
import { renderMarkdown } from '../../components/markdown';
import { Pill } from '../../components/ui';
import { Head, Label, Panel } from './chrome';
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
function AnswerBody({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const html = useMemo(() => renderMarkdown(text), [text]);
  const long = text.length > 1800 || (text.match(/\n/g) || []).length > 22;
  return (
    <div className="min-w-0">
      <div className={
        'prose-chat relative overflow-hidden text-[13.5px] leading-relaxed text-zinc-200 '
        + (long && !open ? 'max-h-[420px]' : '')}
        dangerouslySetInnerHTML={{ __html: html }} />
      {long && !open && (
        <div aria-hidden
             className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-surface-0 to-transparent" />
      )}
      {long && (
        <button type="button" onClick={() => setOpen((v) => !v)}
                className="mt-1.5 rounded-md border border-glass-border px-2 py-0.5 text-[11px] text-zinc-muted hover:bg-white/5 hover:text-zinc-200">
          {open ? 'fold this answer' : `show all (${text.length.toLocaleString()} characters)`}
        </button>
      )}
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
}

export interface ChangeRow {
  path: string;
  kind?: string;
  ok?: boolean;
  skipped?: boolean;
  error?: string;
  note?: string;
  /** The same hunks the editor's review mode and the Changes drawer show. The
   *  panel used to list files, which made "revert this file" the only verb on
   *  offer for work a user had only half read. */
  added?: number;
  removed?: number;
  created?: boolean;
  hunks?: { id: string; added: number; removed: number; new_start: number }[];
}

let seq = 0;
const nextId = () => `t${++seq}`;

/** The collapsed activity line: what kind of work, not a log of it. Grouped by
 *  what the verb DOES, because "read_file ×3, edit_file ×2" is a diff of the
 *  tool registry rather than an answer to "what has it been doing". */
export function activitySummary(tools: ToolRow[]): string {
  const bucket = (name: string): string => {
    if (name.startsWith('browser_')) return 'browser';
    if (name === 'run_command') return 'command';
    if (name === 'run_checks' || /^verify/.test(name)) return 'check';
    if (/^(edit_file|create_file|delete_file|make_dir|replace)/.test(name)) return 'edit';
    if (/^(read_file|list_dir|search|grep|document_symbols|code_intel|find)/.test(name)) return 'read';
    return 'other';
  };
  const WORD: Record<string, [string, string]> = {
    read: ['read', 'reads'], edit: ['edit', 'edits'], check: ['check', 'checks'],
    command: ['command', 'commands'], browser: ['page', 'pages'], other: ['step', 'steps'],
  };
  const order = ['read', 'edit', 'check', 'command', 'browser', 'other'];
  const counts = new Map<string, number>();
  for (const t of tools) {
    const b = bucket(t.name || '');
    counts.set(b, (counts.get(b) || 0) + 1);
  }
  const parts = order.filter((k) => counts.has(k))
    .map((k) => {
      const n = counts.get(k) as number;
      const [one, many] = WORD[k];
      return `${n} ${n === 1 ? one : many}`;
    });
  if (parts.length <= 3) return parts.join(' · ');
  return `${parts.slice(0, 2).join(' · ')} · ${parts.length - 2} more`;
}
const MODEL_KEY = 'aria.studio.model';

/** Fold one frame into a turn, in place.
 *
 *  Shared by the live stream and the replay of a finished run. That sharing is
 *  the point: a transcript rebuilt from disk used to be a second
 *  implementation of "what this run looked like", and the two drifted — the
 *  replayed version lost tool cards and the live one kept them. One reducer,
 *  two callers. */
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
                   state: 'running', note: 'waiting for approval' });
  } else if (kind === 'thinking') {
    if (!f.thinking_dropped) t.thinking = (t.thinking || '') + String(f.text || '');
  } else if (kind === 'tool_call') {
    t.tools.push({ id: String(f.id || f.name || nextId()),
                   name: String(f.name || 'tool'),
                   detail: String(f.detail || ''), state: 'running' });
  } else if (kind === 'tool_result') {
    const id = String(f.id || '');
    const row = [...t.tools].reverse()
      .find((r) => r.id === id || (r.state === 'running' && r.name === String(f.name || '')));
    if (row) {
      row.ok = f.ok !== false;
      row.state = f.skipped ? 'skipped' : 'done';
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

export function AgentPanel({ sid, activePath, activeText, openFiles,
                             onOpenPath, model: modelInitial, disabled, onPending,
                             heroTitle, heroHint, roots, onAddFolder,
                             onFirstTask, onChanges, runs, onWatch,
                             onRunStarted, onNewSession, onUndo, onTerminal,
                             onRefresh }: {
  sid: string;
  activePath: string;
  activeText: string;
  openFiles: string[];
  onOpenPath: (p: string) => void;
  /** The picker is GLOBAL by decision: a session records which model answered
   *  it, it does not configure one. This prop is only the initial value. */
  model?: string;
  disabled: boolean;
  /** Tells the shell its unreviewed set is stale: one fetch owns that truth,
   *  and a second copy inside this panel is how the two disagree. */
  onPending?: () => void;
  heroTitle: string;
  heroHint: string;
  roots: StudioRoot[];
  onAddFolder: () => void;
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
  onTerminal?: () => void;
  /** Re-read the shell's own state (used by `/checks` to refresh the rail). */
  onRefresh?: () => void;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [changes, setChanges] = useState<ChangeRow[]>([]);
  const [running, setRunning] = useState(false);
  /* The run this tab is driving. Set from the `started` frame, and the only
     way to stop the AGENT rather than this fetch. */
  const [runId, setRunId] = useState('');
  /* The model this tab will ask for, remembered across sessions because the
     choice is global. `''` means "let the gateway route". */
  const [model, setModel] = useState(
    () => localStorage.getItem(MODEL_KEY) || modelInitial || '');
  useEffect(() => { setModel(localStorage.getItem(MODEL_KEY) || modelInitial || ''); },
    [sid, modelInitial]);
  const chooseModel = useCallback((v: string) => {
    setModel(v);
    try { localStorage.setItem(MODEL_KEY, v); } catch { /* private mode */ }
  }, []);
  /* Problems the person attached with `#`. Held here, not in the composer, so
     a replayed transcript and the live chips read the same list. */
  const [problems, setProblems] = useState<ProblemRow[]>([]);
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
  const [menu, setMenu] = useState<'' | 'mode' | 'approval' | 'folder' | 'model'>('');
  const [branch, setBranch] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
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
  useEffect(() => {
    if (replayedFor.current === sid) return;
    if (turns.length) { replayedFor.current = sid; return; }
    const newest = runs.find((r) => r.status !== 'running'
      && r.status !== 'stopping') || runs[0];
    if (!newest) return;                       // the list has not arrived yet
    replayedFor.current = sid;
    let live = true;
    (async () => {
      try {
        const got = await api.studioRunFrames(sid, newest.id);
        if (!live || !got?.frames?.length) return;
        const rebuilt: Turn[] = [
          { id: 'replay-q', role: 'user', text: newest.question,
            tools: [], running: false },
        ];
        const aria: Turn = { id: 'replay-a', role: 'aria', text: '',
                             tools: [], running: false };
        for (const f of got.frames) foldFrame(aria, f);
        /* A replayed run is over, so its seconds and its money come from the
           registry and the ledger rather than from frames that were only ever
           sent while it was live. Without this, reloading the studio shows a
           finished answer with no cost — and a run with no cost beside it
           reads as a run that was not billed. */
        aria.elapsed = Math.max(0, Math.round((newest.ended || 0)
          - (newest.started || 0)));
        rebuilt.push(aria);
        if (live) setTurns(rebuilt);
        if (newest.status !== 'running' && newest.status !== 'stopping') {
          void api.studioRunCost(sid, newest.id)
            .then((c) => setTurns((all) => all.map((t) => (t.id === 'replay-a'
              ? { ...t, cost: c } : t))))
            .catch(() => { /* the token line stays, which is still the truth */ });
        }
      } catch { /* an empty transcript is simply an empty panel */ }
    })();
    return () => { live = false; };
    // `runs` is a dep because the list arrives after the first render, and
    // `replayedFor` — not the key — is what keeps this to one replay per
    // session. Every later runs update returns at the first line.
  }, [sid, runs, turns.length]);

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

  const refreshBranch = useCallback(async () => {
    // A session with no folders cannot have a repository, and asking produces a
    // 409 that the browser logs as a failed resource on every cold load — the
    // chip used to read the legacy route, which answered 200 with ok:false.
    // Not asking is the only way a 4xx stops appearing in the console.
    if (!roots.length) { setBranch(null); return; }
    try {
      /* `/api/code/git` resolves the PROCESS workspace, so in a studio session
         this chip read "no git folder" about a folder that is a repository —
         the studio route is the one that knows the session's own roots, and
         its `--porcelain=v1 --branch` output names the branch as `## master`. */
      const r = await api.studioGit(sid, 'status', {});
      if (r?.ok === false || !r?.repo) {
        setBranch('no repository here');
        return;
      }
      const out = String(r.output || '');
      const m = /^## (\S+)/m.exec(out) || /On branch ([^\s]+)/.exec(out);
      setBranch(m ? m[1] : 'detached HEAD');
    } catch {
      setBranch(null);
    }
  }, [sid, roots.length]);

  useEffect(() => { void refreshBranch(); }, [refreshBranch, roots.length]);

  const patch = useCallback((id: string, fn: (t: Turn) => Turn) => {
    setTurns((all) => all.map((t) => (t.id === id ? fn(t) : t)));
  }, []);

  const refreshChanges = useCallback(async () => {
    try {
      /* The studio route, not `/api/code/agent/changes`. Both read the same
         store, but the legacy one answers without hunk detail — so the card
         above the composer and the Changes drawer were showing two shapes of
         the same fact, and every cold load paid for both (92ms each). The
         studio's rows carry the legacy fields *and* the hunks:
         `StudioChange extends CodeAgentChange`. */
      const r = await api.studioChanges(sid);
      setChanges(((r?.changes || []) as ChangeRow[]));
      onPending?.();
    } catch { /* the rail simply shows nothing new */ }
  }, [sid, onPending]);

  const send = useCallback(async () => {
    const question = input.trim();
    if (!question || running) return;
    setInput('');
    if (!turns.length) onFirstTask?.(question);
    setRunning(true);
    const userId = nextId();
    const ariaId = nextId();
    setTurns((all) => [...all,
      { id: userId, role: 'user', text: question, tools: [], running: false },
      { id: ariaId, role: 'aria', text: '', tools: [], running: true }]);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let sawText = false;
    let hops = 0;
    let ridRun = '';
    const startedAt = Date.now() / 1000;
    setMeta({ startedAt, running: true, hops: 0,
             contextChars: question.length + activeText.length });
    try {
      for await (const f of api.codeAgentStream(
        question, activePath, activeText, [], ctrl.signal,
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
              detail: String(frame.summary || ''), state: 'running' }] }));
        } else if (kind === 'delta' || kind === 'text') {
          sawText = true;
          patch(ariaId, (t) => ({ ...t, text: t.text + (frame.text || '') }));
        } else if (kind === 'thinking') {
          patch(ariaId, (t) => ({ ...t,
            thinking: (t.thinking || '') + (frame.text || '') }));
        } else if (kind === 'tool_call') {
          /* A tool call means a model call happened to make it, so this is the
             hop counter — read off the stream rather than guessed from elapsed
             time, and bounded by the server's own cap in the chip below. */
          hops += 1;
          setMeta((m) => ({ ...m, hops }));
          patch(ariaId, (t) => ({ ...t, tools: [...t.tools, {
            id: String(frame.id || frame.name || nextId()),
            name: String(frame.name || 'tool'),
            detail: frame.detail, state: 'running' }] }));
        } else if (kind === 'tool_result') {
          patch(ariaId, (t) => ({ ...t, tools: t.tools.map((row) => (
            row.id === String(frame.id || '') || row.name === String(frame.name || '')
              && row.state === 'running'
              ? { ...row, ok: frame.ok !== false,
                  state: frame.skipped ? 'skipped' : 'done',
                  note: frame.note || row.note }
              : row)) }));
          /* Any write tool, not just one of them. The trigger used to name
             `write_file` alone, which is not a tool this agent has: an
             `edit_file` or `create_file` landed on disk and the panel kept
             showing nothing until the next reload — the review list looked
             empty right after the change that created it. */
          if (frame.write || /^(edit_file|create_file|update_file|write_file|delete_file|make_dir)$/.test(String(frame.name || ''))) {
            void refreshChanges();
          }
        } else if (kind === 'status') {
          if (!sawText) patch(ariaId, (t) => ({ ...t, text: t.text || '' }));
        } else if (kind === 'verify') {
          patch(ariaId, (t) => ({ ...t, tools: [...t.tools, {
            id: `v${t.tools.length}`, name: 'verify',
            detail: frame.ok ? 'checks passed' : (frame.error || 'checks failed'),
            ok: !!frame.ok, state: 'done' }] }));
        } else if (kind === 'done') {
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
          setMeta((m) => ({ ...m, running: false }));
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
      setRunning(false);
      abortRef.current = null;
      void refreshChanges();
    }
  }, [input, running, turns.length, activePath, activeText, openFiles, approval,
      readOnly, model, sid, patch, refreshChanges, onFirstTask, onWatch,
      onRunStarted, problems, browserPage]);

  /* A local note in the transcript. Commands answer here rather than in a
     toast: the record of what was asked and what happened belongs in the same
     column as the conversation, and a toast is gone before it is read. */
  const note = useCallback((text: string, tone: 'info' | 'warn' = 'info') => {
    setTurns((all) => [...all, { id: nextId(), role: 'note', tone,
                                 text, tools: [], running: false } as Turn]);
  }, []);

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
      onUndo, onRefresh, setInput]);

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

  const empty = !turns.length;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={scroller}
           className={'min-h-0 flex-1 overflow-auto '
             + (empty ? 'flex flex-col items-center justify-center px-6' : 'px-6 py-5')}>
        {empty && (
          <div className="flex w-full max-w-[720px] flex-col items-center">
            {/* The mark above the hero was "⌥" — the keyboard option symbol,
                left over from a shortcut hint and meaningless here. It now says
                which of the two states this is: nothing to work on, or a
                session that has folders and is waiting for a question. */}
            <span aria-hidden
                  className={
                    'mb-5 flex h-11 w-11 items-center justify-center rounded-xl border '
                    + (roots.length
                      ? 'border-accent/30 bg-accent/10 text-accent'
                      : 'border-glass-border bg-surface-1 text-zinc-muted')}>
              {roots.length ? <Sparkles size={19} /> : <FolderOpen size={19} />}
            </span>
            <h1 className="mb-6 text-center text-[22px] font-bold leading-snug tracking-tight text-zinc-50">
              {heroTitle}
            </h1>
            <Composer {...{ input, setInput, disabled, readOnly, setReadOnly,
                             approval, setApproval, menu, setMenu, roots,
                             onAddFolder, branch, refreshBranch, running, send, stop,
                             composerRef, wide: true, sid, model, setModel: chooseModel,
                             onCommand: (n: string, r: string) => { void runCommand(n, r); },
                             meta, problems, setProblems, onPickFile: onOpenPath,
                             browserPage, setBrowserPage,
                             refreshBrowser: refreshBrowserPage }} />
            {heroHint && (
              <p className="mt-4 max-w-[54ch] text-center text-[13px] leading-relaxed text-zinc-muted">
                {heroHint}
              </p>
            )}
            {/* With no folder the composer is disabled, and the only way to
                enable it was a small grey pill in its footer row — a first
                screen whose one real action was the least prominent thing on
                it. The route out is now the obvious thing. */}
            {!roots.length && (
              <button type="button" onClick={onAddFolder}
                      className="mt-5 flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-[13px] font-semibold text-black transition-colors hover:bg-accent/90">
                <FolderOpen size={14} /> Open a folder
              </button>
            )}
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
                         : 'border-glass-border bg-surface-1/60 text-zinc-300')}>
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
                    <div className="max-w-[85%] rounded-xl border border-accent/25 bg-accent/10 px-3.5 py-2.5">
                      <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-zinc-100">
                        {t.text}
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-2">
                    {!!t.plan?.length && (
                      /* The agent's own checklist, pinned at the top of its
                         turn while it works and collapsed once it stops. It is
                         the run's plan, not a todo the user wrote, so it is
                         labelled as the agent's. */
                      <details open={t.running}
                               className="rounded-md border border-glass-border bg-surface-1/60 px-3 py-2">
                        <summary className="cursor-pointer text-[12px] text-zinc-muted">
                          plan · {t.plan.filter((s) => /done|complete/i.test(s.status)).length}
                          /{t.plan.length}
                        </summary>
                        <ol className="mt-1.5 space-y-0.5">
                          {t.plan.map((s, i) => (
                            <li key={`${s.title}-${i}`}
                                className="flex items-center gap-2 text-[12px] text-zinc-300">
                              <span aria-hidden
                                    className={/done|complete/i.test(s.status)
                                      ? 'text-ok'
                                      : /progress/i.test(s.status) ? 'text-accent' : 'text-zinc-muted'}>
                                {/done|complete/i.test(s.status) ? '✓'
                                  : /progress/i.test(s.status) ? '◐' : '○'}
                              </span>
                              <span className="min-w-0 flex-1 truncate">{s.title}</span>
                            </li>
                          ))}
                        </ol>
                      </details>
                    )}
                    {t.tools.length > 0 && (
                      /* One line about what the agent DID, expandable into the
                         log. A flat list of `✓ read_file / … edit_file / ✓
                         verify` down the left of every turn is what made this
                         screen read like a console rather than a conversation
                         — and it is the first thing a person scrolling back to
                         find an answer has to get past. Collapsed once the run
                         is over, open while it works, because "what is it
                         doing now" is the only question during a run. */
                      <details open={t.running}
                               className="group/act rounded-md border border-glass-border bg-surface-1/40">
                        <summary className="flex cursor-pointer list-none items-center gap-2 px-2.5 py-1.5 text-[12px] text-zinc-muted hover:text-zinc-300">
                          <span aria-hidden
                                className="flex-none transition group-open/act:rotate-90"><ChevronRight size={11} /></span>
                          <span className="min-w-0 flex-1 truncate">
                            {t.tools.length} step{t.tools.length === 1 ? '' : 's'}
                            {activitySummary(t.tools) ? ` · ${activitySummary(t.tools)}` : ''}
                          </span>
                          {!!t.tools.filter((x) => x.state === 'running').length && (
                            <span className="flex-none text-accent">working</span>
                          )}
                          {!t.running && !!t.tools.filter((x) => x.ok === false).length && (
                            <span className="flex-none text-danger">
                              {t.tools.filter((x) => x.ok === false).length} failed
                            </span>
                          )}
                        </summary>
                        <ul className="space-y-1 border-t border-glass-border px-2.5 py-2">
                          {t.tools.map((row) => (
                            <li key={row.id}
                                className="flex min-w-0 items-center gap-2 text-[12px] text-zinc-muted">
                              <span className={
                                'flex-none rounded px-1.5 '
                                + (row.state === 'running' ? 'bg-white/10'
                                   : row.ok === false ? 'bg-danger-dim text-danger'
                                   : row.state === 'skipped' ? 'bg-warn-dim text-warn'
                                   : 'bg-ok-dim text-ok')}>
                                {row.state === 'running' ? '…'
                                  : row.ok === false ? 'x' : row.state === 'skipped' ? '—' : '✓'}
                              </span>
                              <span className="flex-none text-zinc-300">{row.name}</span>
                              <span className="min-w-0 flex-1 truncate" title={row.detail || row.note || ''}>
                                {row.detail || row.note || ''}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                    {t.text && <AnswerBody text={t.text} />}
                    {t.stopped && !/\[stopped:/.test(t.text || '') && (
                      /* The run's reason arrives twice when there is no answer
                         to carry it: the server writes it into `answer` as
                         "[stopped: …]" AND the done frame keeps `stopped`.
                         Printing both showed the same sentence twice in two
                         typefaces, which reads as two different failures. */
                      <p className="text-[12px] text-warn">stopped early: {t.stopped}</p>
                    )}
                    {t.error && (
                      <p className="text-[12px] text-danger">{t.error}</p>
                    )}
                    {/* One line, four facts, all of them measured: the money
                        comes from the gateway ledger under this run's own
                        session, the seconds from the run's start, the hops
                        from the tool frames. `cost unknown` is spelled out — a
                        missing price rendered as $0.00 is the difference
                        between a readout and a guess.
                        *
                        * The condition is one boolean expression with an
                        * explicit ternary: `A || B && <jsx>` parses as
                        * `A || (B && <jsx>)`, which renders the literal `true`
                        * — i.e. nothing at all — whenever A holds. That is how
                        * this line silently disappeared once. */}
                    {(!!t.usage && (t.usage.in || t.usage.out)) || t.elapsed !== undefined ? (
                      <p className="text-[12px] text-zinc-muted">
                        {t.cost && !t.cost.unknown
                          ? `$${(t.cost.usd ?? 0).toFixed(4)} · ${t.cost.calls ?? 0} call(s)`
                          : t.cost?.unknown
                            ? 'cost unknown (ledger unreachable)'
                            : t.running ? 'cost pending' : ''}
                        {t.elapsed !== undefined && (t.elapsed > 0 ? ` · ${t.elapsed}s` : '')}
                        {!!t.usage && (t.usage.in || t.usage.out)
                          ? ` · ${t.usage.in} in`
                            + `${t.usage.out ? ` · ${t.usage.out} out` : ' · out not counted'}`
                            : ''}
                        {!!t.maxHops && !!t.tools.length
                          ? ` · ${t.tools.length}/${t.maxHops} steps` : ''}
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
                   className="rounded-xl border border-warn/40 bg-warn-dim p-3">
                <p className="text-[12px] text-warn">
                  The agent wants to {String(asked.tool || 'write')}
                  {asked.path ? <> · <span className="font-mono">{String(asked.path)}</span></> : null}
                </p>
                {asked.summary ? (
                  <p className="mt-1 text-[12px] text-zinc-300">{String(asked.summary)}</p>
                ) : null}
                {asked.diff ? (
                  <pre className="mt-2 max-h-[220px] overflow-auto rounded-md border border-glass-border bg-surface-0 p-2 text-[12px] leading-snug text-zinc-300">
                    {String(asked.diff).slice(0, 4000)}
                  </pre>
                ) : null}
                <div className="mt-2.5 flex flex-wrap items-center gap-2">
                  <button type="button"
                          onClick={() => void answer(String(asked.id || ''), true)}
                          className="rounded-lg border border-ok/40 bg-ok-dim px-2.5 py-1 text-[12px] text-ok hover:bg-ok/20">
                    Approve
                  </button>
                  {asked.rememberable ? (
                    <button type="button"
                            onClick={() => void answer(String(asked.id || ''), true, true)}
                            title="Allow this exact command for the rest of this session"
                            className="rounded-lg border border-glass-border px-2.5 py-1 text-[12px] text-zinc-200 hover:bg-white/5">
                      Approve and remember
                    </button>
                  ) : null}
                  <button type="button"
                          onClick={() => void answer(String(asked.id || ''), false)}
                          className="rounded-lg border border-danger/40 bg-danger-dim px-2.5 py-1 text-[12px] text-danger hover:bg-danger/20">
                    Refuse
                  </button>
                  <span className="min-w-0 flex-1 text-[12px] text-zinc-muted">
                    Refusing is an answer: the model is told and carries on.
                  </span>
                </div>
              </div>
            )}

            {changes.length > 0 && (
              <Panel accent="var(--color-warn)">
                <Head icon={<GitCompareArrows size={12} />}
                      right={(
                        <button type="button" onClick={() => onChanges?.()}
                                className="rounded-lg border border-glass-border px-2 py-1 text-[11px] text-zinc-200 transition-colors hover:bg-white/5"
                                title="Open the workbench on the unreviewed changes">
                          review all
                        </button>
                      )}>
                  Changes waiting
                </Head>
                <ul className="divide-y divide-edge/70">
                  {changes.map((c) => (
                    <li key={c.path} className="flex min-w-0 items-center gap-2 px-3 py-2">
                      <button type="button" onClick={() => onOpenPath(c.path)}
                              className="min-w-0 flex-1 truncate text-left text-[12.5px] text-zinc-200 hover:text-accent"
                              title={`${c.path} — opens in review, where each hunk can be kept or undone`}>
                        {c.path}
                      </button>
                      {c.created && <Pill tone="ok">new</Pill>}
                      {c.skipped && <Pill tone="warn">skipped</Pill>}
                      {c.error && (
                        <span title={c.error}><Pill tone="err">error</Pill></span>
                      )}
                      {!!c.hunks?.length && (
                        <span className="flex-none text-[11px] text-zinc-muted">
                          {c.hunks.length} hunk{c.hunks.length === 1 ? '' : 's'}
                        </span>
                      )}
                      {/* Same rule as the Changes drawer: "+0 −0" is a fact
                          about the counter, not about the file. A change with
                          nothing in it says so. */}
                      {(c.added ?? 0) + (c.removed ?? 0) > 0 ? (
                        <span className="flex flex-none items-center gap-1.5 text-[11px] tabular-nums">
                          {!!c.added && <span className="text-ok">+{c.added}</span>}
                          {!!c.removed && <span className="text-danger">−{c.removed}</span>}
                        </span>
                      ) : (
                        <span className="flex-none text-[11px] text-zinc-muted">
                          no line change
                        </span>
                      )}
                      <button type="button" onClick={() => onOpenPath(c.path)}
                              title="Review this file hunk by hunk"
                              className="flex-none rounded-lg border border-glass-border px-2 py-1 text-[11px] text-zinc-200 transition-colors hover:bg-white/5">
                        review
                      </button>
                    </li>
                  ))}
                </ul>
              </Panel>
            )}
          </div>
        )}
      </div>

      {/* With a transcript the composer stays the same card, pinned below it —
          one shape in both states, so the thing you type into never moves. */}
      {!empty && (
        <div className="flex-none px-6 pb-4">
          <div className="mx-auto w-full max-w-[760px]">
            <Composer {...{ input, setInput, disabled, readOnly, setReadOnly,
                             approval, setApproval, menu, setMenu, roots,
                             onAddFolder, branch, refreshBranch, running, send, stop,
                             composerRef, wide: false, sid, model,
                             setModel: chooseModel,
                             onCommand: (n: string, r: string) => { void runCommand(n, r); },
                             meta, problems, setProblems, onPickFile: onOpenPath,
                             browserPage, setBrowserPage,
                             refreshBrowser: refreshBrowserPage }} />
          </div>
        </div>
      )}
    </div>
  );
}
