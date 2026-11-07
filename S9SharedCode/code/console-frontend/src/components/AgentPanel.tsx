import {
  memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState,
} from 'react';
import {
  AlertTriangle, Check, ChevronDown, ChevronRight, Circle, Clock, Copy,
  FileCode2, History, Loader2, Plus, RotateCcw, ShieldQuestion, Sparkles,
  Square, ThumbsDown, ThumbsUp, Trash2, Wrench, X,
} from 'lucide-react';
import { ModelPicker } from './ModelPicker';
import { renderMarkdown } from './markdown';

/**
 * The agent panel, modelled on Cursor / Copilot rather than a single-question
 * box.
 *
 * What changed and why:
 *
 *  - CONVERSATION, not one question. The panel keeps turns and sends the recent
 *    ones back, so "and what calls it?" refers to something.
 *  - STEPS ARE VISIBLE. A coding agent that reads six files and then answers in
 *    one paragraph has told you nothing about its work. Each tool call, its
 *    identifying argument and its outcome are shown, so a wrong answer is
 *    diagnosable rather than mysterious.
 *  - THE ANSWER STREAMS. `delta` frames arrive from the model as it writes, so
 *    the text appears token by token instead of after a blank wait.
 *  - EDITS ARE PROPOSED, NOT APPLIED. The panel produces a diff you read and
 *    accept. It never rewrites a buffer behind your back — the same rule the
 *    server already enforced, kept honest on the client too.
 *  - CONTEXT IS EXPLICIT. The active file goes in automatically; anything else
 *    is attached deliberately, and you can see what is attached.
 *  - SESSIONS. A session list, kept in this browser, so more than one thread of
 *    work is possible without losing the last one.
 */

/** `kind` chooses the step's icon and tone, so a failed tool call cannot read
 *  like a successful one. `text` marks a step as prose rather than a one-line
 *  status, so the panel can collapse it instead of rendering a wall of text. */
export type PlanStatus = 'pending' | 'doing' | 'done' | 'blocked';

export type PlanStep = {
  id?: string;
  title: string;
  status: PlanStatus;
};

export type ApprovalMode = 'ask' | 'auto-edit' | 'auto-all' | 'read-only';

export type Step = {
  label: string;
  detail?: string;
  text?: string;
  /** A tool's raw result. Kept for the conversation history (so a follow-up
   *  knows what was actually read) but NEVER rendered — a JSON dump in the
   *  transcript is noise. */
  result?: string;
  /** Stable id for a tool call, so its result updates the SAME row instead of
   *  appending a duplicate. */
  id?: string;
  kind?: 'status' | 'call' | 'ok' | 'err' | 'read' | 'model' | 'think' | 'ask' | 'plan';
  /** A plan frame: the whole checklist, applied to the turn. */
  plan?: PlanStep[];
};

export type Turn = {
  id: string;
  role: 'user' | 'agent';
  text: string;
  steps?: Step[];
  error?: string | null;
  busy?: boolean;
  /** The model's own reasoning, collapsed above the answer. Never the answer. */
  thinking?: string;
  /** Context accounting for the status bar. */
  usage?: { input: number; output: number };
  /** What actually answered (provider · model), once known. */
  model?: string;
  /** Epoch ms, for the session list. */
  at?: number;
  /** Local thumbs vote, so the control reflects the user's own action. */
  vote?: 1 | -1 | 0;
  /** Visible task checklist emitted by the agent's update_plan tool. */
  plan?: PlanStep[];
};

type Props = {
  /** Active file path, or '' with none open. */
  path: string;
  /** Live buffer for the active file, unsaved edits included. */
  body: string;
  /** Posts one turn and streams the reply.
   *
   *  `onText` is called with each answer token as it arrives (live streaming);
   *  `onStep` with each tool/status step; `onMeta` with thinking + usage. The
   *  returned string is the FINAL, authoritative answer. */
  onAsk: (
    question: string,
    history: { role: 'user' | 'assistant'; content: string }[],
    onStep: (s: Step) => void,
    onMeta: (m: { thinking?: string; usage?: Turn['usage']; model?: string;
                thinkingDropped?: boolean }) => void,
    signal: AbortSignal,
    onText?: (chunk: string) => void,
    /** Workspace-relative paths to read into the prompt, besides the open
     *  file. The server caps them; the panel shows every one as a chip. */
    paths?: string[],
    /** Clear the in-flight answer when the server starts a verification fix
     *  round, so the corrected answer does not concatenate onto the old one. */
    onReset?: () => void,
  ) => Promise<string>;
  /** The workspace's file list, for the attach picker. */
  files?: string[];
  /** What the agent changed on disk, so the user reviews it before trusting it. */
  gitView?: 'status' | 'diff' | 'log';
  gitBusy?: boolean;
  gitOut?: { text: string; ok: boolean } | null;
  onGit?: (which: 'status' | 'diff' | 'log') => void;
  /** Model id chosen in the picker; '' means "let the gateway decide". */
  model?: string;
  onModelChange?: (id: string) => void;
  /** Ask = every write/command needs approval; Auto-edit = writes apply with
   *  Keep/Revert and commands still ask; Read-only = no writes or commands. */
  approvalMode?: ApprovalMode;
  onApprovalModeChange?: (mode: ApprovalMode) => void;
  /** Close the panel. Only rendered on the stacked layout, where the panel is
   *  an overlay and the top-bar toggle is the only other way out. */
  onClose?: () => void;
  /** Agent surface: threads live in a tab strip instead of a drawer. Same
   *  store, same `openSession`, same bounded history — just always visible,
   *  because on a full-width surface "which session am I in" has to be
   *  answerable without opening a menu first. */
  tabs?: boolean;
};

let seq = 0;
const nextId = () => `t${++seq}`;

/**
 * Conversation storage.
 *
 * `turns` lived only in `useState`, so every reload - and every agent
 * restart, since the launcher restarts the server - wiped the thread and the
 * panel came back empty. That is the single most obvious difference from
 * Cursor or Copilot, where asking a follow-up after a restart is normal.
 *
 * `localStorage`, not the server: the transcript is the browser's business,
 * the agent is stateless per request, and persisting it server-side would mean
 * a new table, a retention policy and a way to delete it.
 *
 * Bounded, and stripped of anything transient: a busy turn must not be
 * restored half-finished, and a runaway loop must not fill the quota.
 */
const STORE_KEY = 'aria.code.agent.turns';
const SESSIONS_KEY = 'aria.code.sessions';
const MAX_TURNS = 60;
const MAX_TEXT = 8_000;

type StoredTurn = Omit<Turn, 'busy'> & { busy?: false };

function loadTurns(): Turn[] {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((t): t is StoredTurn =>
        !!t && typeof t === 'object'
        && typeof (t as { id?: unknown }).id === 'string'
        && typeof (t as { text?: unknown }).text === 'string')
      .map((t) => ({
        // never restore a turn that was still in flight: its answer was
        // truncated mid-stream and re-sending it as history would poison the
        // next question.
        ...t,
        busy: false,
        text: String(t.text ?? '').slice(0, MAX_TEXT),
      }));
  } catch {
    // a corrupt or unreadable store must not take the panel down with it
    return [];
  }
}

function saveTurns(turns: Turn[]): void {
  try {
    // Keep only the tail, and only settled turns. Everything the panel
    // renders is persisted: dropping `usage` (and the vote) meant a reload
    // showed a settled conversation with a dead context meter and unvoted
    // thumbs, which is the "persistence is broken" shape all over again.
    const keep = turns.filter((t) => !t.busy).slice(-MAX_TURNS)
      .map(({ id, role, text, steps, error, at, model, usage, thinking, vote, plan }) => ({
        id, role, text, steps, error, at, model, usage, thinking, vote, plan,
      }));
    localStorage.setItem(STORE_KEY, JSON.stringify(keep));
  } catch {
    // quota exceeded or storage disabled: the conversation still works for
    // this session, it just will not survive a reload.
  }
}

/* ── archived sessions ───────────────────────────────────────────────────────
 * The active conversation lives under STORE_KEY (above); older ones are
 * snapshotted here when the user starts a new chat, so switching back is a
 * click rather than a lost thread. Bounded the same way, for the same reason. */
type Session = { id: string; title: string; at: number; turns: Turn[] };
const MAX_SESSIONS = 20;

function loadSessions(): Session[] {
  try {
    const raw = localStorage.getItem(SESSIONS_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((s): s is Session =>
      !!s && typeof s === 'object'
      && typeof (s as { id?: unknown }).id === 'string'
      && Array.isArray((s as { turns?: unknown }).turns));
  } catch {
    return [];
  }
}

function saveSessions(sessions: Session[]): void {
  try {
    localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions.slice(0, MAX_SESSIONS)));
  } catch { /* storage disabled: sessions simply do not persist */ }
}

/** Title for a thread: its first user line, trimmed to something list-sized. */
function titleOf(turns: Turn[]): string {
  const first = turns.find((t) => t.role === 'user')?.text ?? '';
  const line = first.split('\n')[0].trim();
  return (line || 'New chat').slice(0, 48);
}

/** "2m", "3h", "5d" - the compact age the session list shows. */
function ago(at: number): string {
  const s = Math.max(0, Math.round((Date.now() - at) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

/* ── step row ──────────────────────────────────────────────────────────────── */

function stepIcon(kind: Step['kind'], spin: boolean) {
  if (spin) return <Loader2 size={11} className="animate-spin text-zinc-400" />;
  switch (kind) {
    case 'call': return <Wrench size={11} className="text-sky-300" />;
    case 'ok': return <Check size={11} className="text-emerald-300" />;
    case 'err': return <AlertTriangle size={11} className="text-rose-300" />;
    case 'read': return <FileCode2 size={11} className="text-zinc-400" />;
    case 'model': return <Sparkles size={11} className="text-zinc-500" />;
    case 'ask': return <ShieldQuestion size={11} className="text-amber-300" />;
    default: return <ChevronRight size={11} className="text-zinc-500" />;
  }
}

function StepRow({ step, count = 1, busy, open, onToggle }:
  { step: Step; count?: number; busy: boolean; open: boolean; onToggle: () => void }) {
  // Reasoning renders inline, at the point in the sequence it actually
  // happened, so the activity reads think -> call -> result -> think. Collapsed
  // because it is the model's scratchpad, not the answer.
  if (step.kind === 'think') {
    return (
      <li className="py-0.5">
        <button type="button" onClick={onToggle} aria-expanded={open}
                className="flex items-center gap-1 text-[10.5px] font-semibold uppercase tracking-wide text-zinc-600 hover:text-zinc-400">
          {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          thinking
          {busy && !step.text ? <Loader2 size={10} className="animate-spin" /> : null}
        </button>
        {open && step.text ? (
          <pre className="mt-1 max-h-56 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-white/10 bg-black/30 p-2 font-mono text-[10.5px] leading-relaxed text-zinc-500">
            {step.text}
          </pre>
        ) : null}
      </li>
    );
  }
  // A 'call' row that has not turned into ok/err yet is still running. This is
  // per-row, not just the last one, so a parallel batch shows every in-flight
  // call spinning rather than only the final one.
  const pending = busy && step.kind === 'call';
  return (
    <li className="flex items-start gap-1.5 py-0.5">
      <span className="mt-[1px] flex-none">{stepIcon(step.kind, pending)}</span>
      <span className="min-w-0 flex-1 text-[11px] leading-snug">
        <span className="text-zinc-300">{step.label}</span>
        {step.detail ? (
          <span className="text-zinc-500"> {step.detail}</span>
        ) : null}
        {count > 1 && (
          <span className="ml-1 rounded-full bg-white/10 px-1.5 text-[10px] text-zinc-400">
            ×{count}
          </span>
        )}
      </span>
    </li>
  );
}

function PlanCard({ steps }: { steps: PlanStep[] }) {
  const done = steps.filter((s) => s.status === 'done').length;
  return (
    <div className="mb-2 rounded-xl border border-white/8 bg-black/20 px-2 py-1.5">
      <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
        <span>plan</span>
        <span className="font-normal normal-case tracking-normal text-zinc-600">
          {done}/{steps.length} done
        </span>
      </div>
      <ul className="space-y-0.5">
        {steps.map((s, i) => (
          <li key={s.id || i}
              className="flex items-start gap-1.5 text-[11px] leading-snug">
            <span className="mt-[1px] flex-none">
              {s.status === 'done'
                ? <Check size={10} className="text-emerald-400" />
                : s.status === 'doing'
                  ? <Loader2 size={10} className="animate-spin text-sky-300" />
                  : s.status === 'blocked'
                    ? <AlertTriangle size={10} className="text-rose-300" />
                    : <Circle size={10} className="text-zinc-600" />}
            </span>
            <span className={
              s.status === 'done'
                ? 'text-zinc-500 line-through decoration-zinc-700'
                : s.status === 'doing' ? 'text-zinc-200' : 'text-zinc-400'
            }>{s.title}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* The wait before the first token is 8-15 seconds on a cold model. A static
 * "thinking…" gives no evidence that anything is happening; a clock does. It
 * ticks inside its own component so a second passing never re-renders the
 * transcript around it. */
function WaitNote({ from }: { from?: number }) {
  const at = from ?? Date.now();
  const [s, setS] = useState(() => Math.max(0, Math.round((Date.now() - at) / 1000)));
  useEffect(() => {
    const id = window.setInterval(() => {
      setS(Math.max(0, Math.round((Date.now() - at) / 1000)));
    }, 1000);
    return () => window.clearInterval(id);
  }, [at]);
  return (
    <p className="flex items-center gap-1.5 text-[11.5px] text-zinc-500">
      <Loader2 size={11} className="animate-spin" />
      <span className="tabular-nums">{s >= 3 ? `thinking… ${s}s` : 'thinking…'}</span>
    </p>
  );
}

/* ── one transcript row ────────────────────────────────────────────────────── */

/* Extracted and memoised for the same reason Chat's MessageRow is: an answer
 * streams in token by token, and without this every finished turn re-rendered
 * (and re-parsed its markdown) on every token of the newest one. `t` is a stable
 * object between tokens — the panel only replaces the live turn — so the
 * identity comparison is enough.
 *
 * The activity list follows the turn's OWN `busy`, not the panel's. The old
 * `stepsOpen || busy` used the panel flag, so asking a follow-up force-opened
 * every earlier turn's tool list at once: the transcript ballooned, the scroll
 * jumped, and it all collapsed again when the answer settled. */
type TurnRowProps = {
  t: Turn;
  stepsOpen: boolean;
  thinkOpen: Record<string, boolean>;
  onToggleSteps: (id: string) => void;
  onToggleThink: (key: string) => void;
  onRegen: (t: Turn) => void;
  onCopy: (text: string) => void;
  onVote: (id: string, v: 1 | -1) => void;
};

const TurnRow = memo(function TurnRow({
  t, stepsOpen, thinkOpen, onToggleSteps, onToggleThink, onRegen, onCopy, onVote,
}: TurnRowProps) {
  // Parsed once per distinct answer, not once per token (and not at all for the
  // finished turns above the live one).
  const html = useMemo(() => (t.text ? renderMarkdown(t.text) : ''), [t.text]);

  // Same tool, several times in a row, is the normal case for an agent that
  // reads a folder ("read_file ×6"). Collapsing them keeps a real
  // investigation readable instead of a wall of rows. The DETAIL must match
  // too, or a parallel batch reading three DIFFERENT files collapses into one
  // row and hides two of them.
  const displaySteps: { key: string; step: Step; count: number }[] = [];
  for (const [i, s] of (t.steps ?? []).entries()) {
    const key = s.id ?? `s${i}`;
    const prev = displaySteps[displaySteps.length - 1];
    if (s.kind === 'think' || !prev
        || s.kind !== prev.step.kind || s.label !== prev.step.label
        || (s.detail ?? '') !== (prev.step.detail ?? '')) {
      displaySteps.push({ key, step: s, count: 1 });
      continue;
    }
    // A repeat is one we have NOT already collapsed, so it counts.
    prev.count += 1;
  }
  const toolCount = (t.steps ?? []).filter(
    (s) => s.kind === 'call' || s.kind === 'ok' || s.kind === 'err'
  ).length;
  // A turn that only carries context switches has no tools and no prose, so
  // the agent bubble above renders as an "ACTIVITY · 0 tools" card per file
  // click. It reads as a one-line marker.
  const ctxOnly = t.role === 'agent' && !t.text && !t.error
    && !!t.steps?.length && t.steps.every((s) => s.kind === 'status');

  return (
    <li className="space-y-1.5">
      {t.role === 'user' ? (
        <div className="ml-8 rounded-2xl rounded-br-md bg-white/[0.05] px-3 py-2 text-[12px] leading-relaxed text-zinc-200">
          {t.text}
        </div>
      ) : ctxOnly ? (
        <div className="flex items-center gap-1.5 px-1 text-[10.5px] text-zinc-500">
          <FileCode2 size={11} className="flex-none text-zinc-600" />
          <span className="font-semibold text-zinc-500">{(t.steps ?? [])[0]?.label}</span>
          <span className="min-w-0 truncate">{(t.steps ?? [])[0]?.detail}</span>
        </div>
      ) : (
        <div className="rounded-2xl rounded-bl-md border border-white/8 bg-surface-0 px-3 py-2">
          {t.plan && t.plan.length > 0 && <PlanCard steps={t.plan} />}
          {t.steps && t.steps.length > 0 && (
            <div className="mb-2 overflow-hidden rounded-xl border border-white/8 bg-black/20">
              <button type="button"
                      onClick={() => onToggleSteps(t.id)}
                      aria-expanded={stepsOpen}
                      className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-[10.5px] font-semibold uppercase tracking-wide text-zinc-500 hover:text-zinc-300">
                {stepsOpen
                  ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                {t.busy && !t.text ? 'working' : 'activity'}
                <span className="font-normal normal-case tracking-normal text-zinc-600">
                  {toolCount} tool{toolCount === 1 ? '' : 's'}
                </span>
                <span className="flex-1" />
                {t.busy && <Loader2 size={10} className="animate-spin" />}
              </button>
              {(stepsOpen || t.busy) && (
                <ul className="max-h-56 overflow-y-auto border-t border-white/8 px-2 py-1">
                  {displaySteps.map((s) => (
                    <StepRow key={s.key} step={s.step}
                             count={s.count} busy={!!t.busy}
                             open={!!thinkOpen[s.key]}
                             onToggle={() => onToggleThink(s.key)} />
                  ))}
                </ul>
              )}
            </div>
          )}
          {t.error && (
            <div className="flex items-start gap-1.5 text-[11.5px] text-rose-300">
              <AlertTriangle size={12} className="mt-0.5 flex-none" />
              {t.error}
            </div>
          )}
          {t.text ? (
            <div className="prose-chat text-[12.5px] leading-relaxed text-zinc-200"
                 dangerouslySetInnerHTML={{ __html: html }} />
          ) : t.busy ? (
            <WaitNote from={t.at} />
          ) : null}

          {/* message actions */}
          {!t.busy && (t.text || t.error) && (
            <div className="mt-1.5 flex items-center gap-0.5 text-zinc-500">
              <button type="button" onClick={() => onRegen(t)}
                      title="Regenerate"
                      className="rounded p-1 hover:bg-white/5 hover:text-zinc-200">
                <RotateCcw size={11} />
              </button>
              <button type="button" onClick={() => onCopy(t.text)}
                      title="Copy" aria-label="Copy answer"
                      className="rounded p-1 hover:bg-white/5 hover:text-zinc-200">
                <Copy size={11} />
              </button>
              <button type="button" onClick={() => onVote(t.id, 1)}
                      title="Good answer" aria-pressed={t.vote === 1}
                      className={`rounded p-1 hover:bg-white/5 ${
                        t.vote === 1 ? 'text-emerald-300' : 'hover:text-zinc-200'}`}>
                <ThumbsUp size={11} />
              </button>
              <button type="button" onClick={() => onVote(t.id, -1)}
                      title="Bad answer" aria-pressed={t.vote === -1}
                      className={`rounded p-1 hover:bg-white/5 ${
                        t.vote === -1 ? 'text-rose-300' : 'hover:text-zinc-200'}`}>
                <ThumbsDown size={11} />
              </button>
              {t.model && (
                <span className="ml-1 truncate text-[10px] text-zinc-600">
                  {t.model}
                </span>
              )}
            </div>
          )}
        </div>
      )}
    </li>
  );
});

/* ── panel ─────────────────────────────────────────────────────────────────── */

export default function AgentPanel({
  path, body, onAsk, files = [],
  gitView = 'status', gitBusy = false, gitOut = null, onGit,
  model = '', onModelChange, approvalMode = 'ask', onApprovalModeChange,
  onClose, tabs = false,
}: Props) {
  void body;   // sent through onAsk by the caller; kept in Props for the shape
  const [turns, setTurns] = useState<Turn[]>(() => loadTurns());
  // Persist on every settled change. Skipping a busy turn means an in-flight
  // answer is never written, so a reload mid-stream cannot leave a truncated
  // reply in the history that the next question is sent against.
  //
  // Deferred by a tick: the write serialises the whole transcript (tool results
  // included) and hits localStorage synchronously. Doing that inside the commit
  // that hides the spinner delayed the first frame of the finished answer.
  useEffect(() => {
    if (turns.some((t) => t.busy)) return;
    const id = window.setTimeout(() => saveTurns(turns), 0);
    return () => window.clearTimeout(id);
  }, [turns]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  /* Follow-ups typed while a turn is still running. They run in order, one at
     a time, as each turn settles — a queue, not a second concurrent turn,
     because the transcript (and the model's history) is sequential. Each
     queued item carries its own attached files, so queueing mid-turn does not
     silently drop them. */
  const [queued, setQueued] = useState<{ text: string; extra?: string[] }[]>([]);
  /* Workspace files attached to the next question, besides the open file. */
  const [attached, setAttached] = useState<string[]>([]);
  /* null = the attach picker is closed; a string = its filter. */
  const [pickQ, setPickQ] = useState<string | null>(null);
  const [showSteps, setShowSteps] = useState<Record<string, boolean>>({});
  const [view, setView] = useState<'chat' | 'sessions'>('chat');
  const [sessions, setSessions] = useState<Session[]>(() => loadSessions());
  const ac = useRef<AbortController | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const ta = useRef<HTMLTextAreaElement | null>(null);

  // A new file changes the ground truth. Say so rather than silently carrying
  // an answer about the previous buffer forward.
  const [contextPath, setContextPath] = useState(path);
  useEffect(() => {
    if (path !== contextPath) {
      setContextPath(path);
      if (turns.length) {
        setTurns((t) => {
          // Coalesce a run of file switches into ONE marker. Appending a turn
          // per click turned browsing the tree into a wall of "ACTIVITY · 0
          // tools" cards, each holding a single context line; the marker only
          // needs to say what the agent is looking at NOW.
          const marker: Turn = {
            id: nextId(), role: 'agent', text: '', at: Date.now(),
            steps: [{ label: 'context', detail: `switched to ${path}`,
                      kind: 'status' }],
          };
          const last = t[t.length - 1];
          const lastIsMarker = !!last && last.role === 'agent' && !last.text
            && !last.error && !!last.steps?.length
            && last.steps.every((s) => s.kind === 'status');
          return lastIsMarker ? [...t.slice(0, -1), marker] : [...t, marker];
        });
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  // Sticky auto-scroll: follow the answer, but never fight the reader.
  //
  // `scrollTop = scrollHeight` on every `turns` change meant every streamed
  // token yanked the view back to the bottom, so scrolling up to re-read
  // something mid-answer was impossible — the transcript jumped on every token.
  // Now the panel tracks its own position: while you are at (or near) the
  // bottom it keeps following; the moment you scroll up to read, it freezes
  // until you come back. A scroll is only a "follow" request if the user made
  // it within ~64px of the bottom, which is thumb-sized, not exact.
  const stick = useRef(true);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const onScroll = () => {
      stick.current =
        el.scrollHeight - el.scrollTop - el.clientHeight < 64;
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, []);
  /* Pinned BEFORE paint, not after it. As a passive effect the browser painted
     the freshly grown answer at the old scroll offset and then jumped to the
     bottom on the next frame — one visible hitch per token. A layout effect
     runs after React commits the new text and before the frame is painted, so
     the text is already at the bottom the first time it is seen. */
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [turns, view]);

  // Auto-grow the composer up to a few lines.
  useEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(140, el.scrollHeight)}px`;
  }, [draft]);

  /** The one place a turn is asked. `send()` and Regenerate both come here. */
  const ask = useCallback(async (question: string, replaceId?: string,
                                extra: string[] = []) => {
    const q = question.trim();
    if (!q || busy) return;
    // A turn's text alone is not its whole story. An assistant turn that ran a
    // dozen tools and produced no prose — because it errored, or stopped on a
    // tool call — would otherwise vanish from history entirely, so the next
    // question ("so what did you read?") arrived with no record that anything
    // was read and the model answered "this is the start of our interaction".
    // Carry a one-line summary of the tools each turn actually ran, next to its
    // text, so the transcript the model sees matches the one on screen.
    const summarise = (t: Turn): string => {
      const acts = (t.steps ?? [])
        .filter((s) => s.kind === 'call' || s.kind === 'read'
          || s.kind === 'ok' || s.kind === 'err')
        .map((s) => (s.detail ? `${s.label} ${s.detail}` : s.label));
      const uniq = Array.from(new Set(acts)).slice(-12);
      const plain = t.text.trim();
      const parts: string[] = [];
      if (plain) parts.push(plain);
      if (uniq.length) parts.push(`[tools used: ${uniq.join('; ')}]`);
      // The raw tool results are what the next question needs to answer from:
      // without them a follow-up ("so how is this codebase?") arrives with no
      // record of what was read and the model says it has nothing. They are
      // carried here, next to the text, but never rendered on screen.
      const seen = new Set<string>();
      const res = (t.steps ?? [])
        .filter((s) => {
          const r = (s.result ?? '').trim();
          if (!r || seen.has(r)) return false;
          seen.add(r);
          return true;
        })
        .map((s) => `${s.label}: ${(s.result ?? '').trim()}`.slice(0, 1200))
        .slice(-6);
      if (res.length) parts.push(`[results:\n${res.join('\n')}]`);
      return parts.join('\n');
    };
    const history = turns
      .filter((t) => t.role === 'user' || t.text || (t.steps?.length ?? 0) > 0)
      .filter((t) => t.id !== replaceId)
      // The wire format calls the assistant "assistant"; the view calls it
      // "agent" so it reads as a role in the transcript.
      .map((t) => ({
        role: (t.role === 'user' ? 'user' : 'assistant') as 'user' | 'assistant',
        content: t.role === 'user' ? t.text : summarise(t),
      }))
      .filter((m) => m.content.trim().length > 0);

    const agentId = nextId();
    setTurns((t) => {
      const base = replaceId ? t.filter((x) => x.id !== replaceId) : t;
      const withUser: Turn[] = replaceId
        ? base
        : [...base, { id: nextId(), role: 'user', text: q, at: Date.now() }];
      return [...withUser, {
        id: agentId, role: 'agent', text: '', steps: [], busy: true,
        at: Date.now(),
      }];
    });
    setDraft('');
    setView('chat');
    setBusy(true);

    const ctrl = new AbortController();
    ac.current = ctrl;
    /* Coalesce token paints to one per animation frame. The gateway delivers
       deltas in bursts (a dozen can land in the same millisecond), and each one
       used to be its own React update: the whole transcript re-rendered and
       every answer's markdown was re-parsed for a handful of characters. A
       screen cannot show more than one frame of that anyway. */
    let pending = '';
    let frame = 0;
    const flush = () => {
      frame = 0;
      if (!pending) return;
      const chunk = pending;
      pending = '';
      setTurns((t) => t.map((x) => (
        x.id === agentId ? { ...x, text: x.text + chunk } : x
      )));
    };
    const flushNow = () => {
      if (frame) { cancelAnimationFrame(frame); frame = 0; }
      flush();
    };
    const queue = (chunk: string) => {
      pending += chunk;
      if (!frame) frame = requestAnimationFrame(flush);
    };

    try {
      const answer = await onAsk(
        q,
        history,
        (s: Step) => {
          if (s.kind === 'plan' && s.plan) {
            setTurns((t) => t.map((x) => (
              x.id === agentId ? { ...x, plan: s.plan } : x
            )));
            return;
          }
          setTurns((t) => t.map((x) => {
            if (x.id !== agentId) return x;
            const steps = x.steps ?? [];
            // A tool_result carries the same `id` as its tool_call, so it
            // updates that row in place. Without this the call and its result
            // both rendered, doubling every tool in the activity list.
            const i = s.id ? steps.findIndex((p) => p.id === s.id) : -1;
            if (i >= 0) {
              const next = steps.slice();
              next[i] = { ...next[i], ...s };
              return { ...x, steps: next };
            }
            return { ...x, steps: [...steps, s] };
          }));
        },
        (m) => {
          if (!m) return;
          // The server found the reasoning was just the answer again (some
          // models send both). Every think row that streamed is now a duplicate,
          // so drop them instead of rendering the reply twice.
          if (m.thinkingDropped) {
            setTurns((t) => t.map((x) => (
              x.id === agentId
                ? { ...x, thinking: '', steps: (x.steps ?? []).filter((s) => s.kind !== 'think') }
                : x
            )));
            return;
          }
          setTurns((t) => t.map((x) => (
            x.id === agentId
              ? {
                  ...x,
                  thinking: m.thinking !== undefined ? m.thinking : x.thinking,
                  usage: m.usage ?? x.usage,
                  model: m.model ?? x.model,
                }
              : x
          )));
        },
        ctrl.signal,
        // Live token streaming: buffer as the model writes; flush happens once
        // per frame (see above).
        (chunk: string) => queue(chunk),
        extra.length ? extra : undefined,
        () => setTurns((t) => t.map((x) => (
          x.id === agentId ? { ...x, text: '' } : x
        ))),
      );
      flushNow();
      setTurns((t) => t.map((x) => (
        x.id === agentId ? { ...x, text: answer || x.text, busy: false } : x
      )));
    } catch (e) {
      // Keep every token that arrived before the failure/Stop, not just the
      // ones that happened to be committed to state already.
      flushNow();
      if ((e as Error).name === 'AbortError') {
        setTurns((t) => t.map((x) => (
          x.id === agentId
            ? { ...x, text: x.text || '_stopped_', busy: false } : x
        )));
      } else {
        setTurns((t) => t.map((x) => (
          x.id === agentId
            ? { ...x, error: String((e as Error).message ?? e), busy: false } : x
        )));
      }
    } finally {
      flushNow();
      setBusy(false);
      ac.current = null;
    }
  }, [busy, onAsk, turns]);

  /* Latest-value mirrors so the memoised rows can receive callbacks that never
     change identity. Without this, every token produced a new `regen` (it
     closes over `turns`), which re-rendered every finished row and defeated the
     memo. `ask` itself still gets the current state through these refs. */
  const askRef = useRef(ask);
  const turnsRef = useRef(turns);
  useEffect(() => { askRef.current = ask; }, [ask]);
  useEffect(() => { turnsRef.current = turns; }, [turns]);

  const send = useCallback(() => {
    const q = draft.trim();
    if (!q) return;
    const extra = attached.length ? attached : undefined;
    if (busy) {
      // The composer stays live while the agent works. Pressing Enter mid-turn
      // queues the message instead of dropping it: `ask` returns early when
      // busy, so without this the text was lost with no feedback at all.
      setQueued((list) => [...list, { text: q, extra }]);
      setDraft('');
      setAttached([]);
      return;
    }
    setAttached([]);
    void ask(q, undefined, extra ?? []);
  }, [ask, draft, busy, attached]);
  const regen = useCallback((turn: Turn) => {
    // Re-run the question that produced this answer. Read through the ref: the
    // row is memoised, so its callback is the one from the first render.
    const all = turnsRef.current;
    const idx = all.findIndex((t) => t.id === turn.id);
    let q = '';
    for (let i = idx - 1; i >= 0; i--) {
      if (all[i].role === 'user') { q = all[i].text; break; }
    }
    if (q) void askRef.current(q, turn.id);
  }, []);
  const stop = () => { setQueued([]); ac.current?.abort(); };

  /* Stable toggles for the memoised rows: they take the row's id, not a
     closure over its current open state. */
  const toggleSteps = useCallback((id: string) => {
    setShowSteps((s) => ({ ...s, [id]: !s[id] }));
  }, []);
  const toggleThink = useCallback((key: string) => {
    setShowThinking((s) => ({ ...s, [key]: !s[key] }));
  }, []);

  /* Run the next queued follow-up as soon as the current turn settles.
     Dequeuing here rather than inside `ask` keeps exactly one turn in flight. */
  useEffect(() => {
    if (busy || queued.length === 0) return;
    const [next, ...rest] = queued;
    setQueued(rest);
    void ask(next.text, undefined, next.extra ?? []);
  }, [busy, queued, ask]);

  const vote = useCallback((id: string, v: 1 | -1) => {
    setTurns((t) => t.map((x) => (
      x.id === id ? { ...x, vote: x.vote === v ? 0 : v } : x)));
  }, []);
  const copy = useCallback((text: string) => {
    void navigator.clipboard?.writeText(text).catch(() => {});
  }, []);
  // The selected model's context window, reported by the picker; 0 = unknown.
  const [ctxLen, setCtxLen] = useState(0);
  const onCtx = useCallback((m: { contextLength: number; id: string }) => {
    setCtxLen(m.contextLength || 0);
  }, []);

  /** Archive the current thread and start blank. */
  const newChat = useCallback(() => {
    if (turns.length) {
      const snap: Session = {
        id: `s${Date.now()}`, title: titleOf(turns), at: Date.now(),
        turns: turns.filter((t) => !t.busy),
      };
      setSessions((prev) => {
        const next = [snap, ...prev].slice(0, MAX_SESSIONS);
        saveSessions(next);
        return next;
      });
    }
    ac.current?.abort();
    setTurns([]);
    setAttached([]);
    try { localStorage.removeItem(STORE_KEY); } catch { /* storage off */ }
    setView('chat');
  }, [turns]);

  const openSession = useCallback((s: Session) => {
    if (turns.length) {
      const snap: Session = {
        id: `s${Date.now()}`, title: titleOf(turns), at: Date.now(),
        turns: turns.filter((t) => !t.busy),
      };
      setSessions((prev) => {
        const next = [snap, ...prev.filter((x) => x.id !== s.id)]
          .slice(0, MAX_SESSIONS);
        saveSessions(next);
        return next;
      });
    } else {
      setSessions((prev) => {
        const next = prev.filter((x) => x.id !== s.id);
        saveSessions(next);
        return next;
      });
    }
    setTurns(s.turns.map((t) => ({ ...t, busy: false })));
    setView('chat');
  }, [turns]);

  const clear = () => {
    ac.current?.abort();
    setTurns([]);
    setAttached([]);
    // The button says "clear", so it must clear what is on disk too -
    // otherwise a reload brings the whole conversation straight back.
    try {
      localStorage.removeItem(STORE_KEY);
    } catch { /* storage disabled: nothing to remove */ }
  };

  const title = useMemo(() => titleOf(turns), [turns]);

  const [showThinking, setShowThinking] = useState<Record<string, boolean>>({});

  // Context meter: how full the model's window got on the last exchange.
  const usedTokens = useMemo(() => {
    const u = [...turns].reverse().find((t) => t.usage)?.usage;
    return u ? u.input + u.output : 0;
  }, [turns]);
  const pct = ctxLen > 0 ? Math.min(100, Math.round((usedTokens / ctxLen) * 100)) : 0;
  const kfmt = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}K` : `${n}`);

  return (
    <div className="agent-panel flex min-h-0 min-w-0 flex-1 flex-col bg-surface-0">
      {/* ── header ─────────────────────────────────────────────────────────── */}
      {/* ── session tabs ──────────────────────────────────────────────────
          The agent surface shows every open thread at once, the way an editor
          shows every open file. Same `sessions` store, same `openSession`,
          same 20-session bound — the drawer just stops being the only door. */}
      {tabs && (
        <div role="tablist" aria-label="Agent sessions"
             className="flex flex-none items-stretch gap-px overflow-x-auto border-b border-glass-border bg-surface-1">
          <button type="button" role="tab" aria-selected
                  onClick={() => setView('chat')}
                  title={title}
                  className="flex max-w-[220px] flex-none items-center gap-1.5 border-b-2 border-violet-400/70 bg-white/[0.06] px-2.5 py-1.5 text-left">
            <span className="h-1.5 w-1.5 flex-none rounded-full bg-violet-300" />
            <span className="min-w-0 flex-1 truncate text-[11.5px] font-medium text-zinc-100">
              {title}
            </span>
            {turns.length > 0 && (
              <span className="flex-none text-[10px] text-zinc-muted">
                {turns.filter((t) => t.role === 'user').length}
              </span>
            )}
          </button>
          {sessions.map((s) => (
            <button key={s.id} type="button" role="tab" aria-selected={false}
                    onClick={() => { openSession(s); setView('chat'); }}
                    title={`${s.title} - ${ago(s.at)} ago`}
                    className="flex max-w-[200px] flex-none items-center gap-1.5 border-b-2 border-transparent px-2.5 py-1.5 text-left hover:bg-white/5">
              <span className="h-1.5 w-1.5 flex-none rounded-full bg-zinc-600" />
              <span className="min-w-0 flex-1 truncate text-[11.5px] text-zinc-400">
                {s.title}
              </span>
              <span className="flex-none text-[10px] text-zinc-muted">{ago(s.at)}</span>
            </button>
          ))}
          <button type="button" onClick={newChat}
                  title="New session" aria-label="New session"
                  className="ml-0.5 flex flex-none items-center rounded my-1 px-1.5 text-zinc-muted hover:bg-white/5 hover:text-zinc-200">
            <Plus size={13} />
          </button>
        </div>
      )}

      <header className="flex flex-none items-center gap-1.5 border-b border-glass-border px-2.5 py-2">
        {view === 'sessions' ? (
          <>
            <span className="text-[11px] font-bold uppercase tracking-[0.14em] text-zinc-300">
              Sessions
            </span>
            <button type="button" onClick={() => setSessions(loadSessions())}
                    title="Refresh" className="ml-auto text-zinc-muted hover:text-zinc-200">
              <RotateCcw size={12} />
            </button>
            <button type="button" onClick={newChat} title="New chat"
                    className="text-zinc-muted hover:text-zinc-200">
              <Plus size={13} />
            </button>
          </>
        ) : (
          <>
            {/* One way into Sessions, not two. The back chevron and the
                history icon were the same `setView('sessions')` call sitting
                three centimetres apart, and the file chip repeated the tab,
                the breadcrumb and the composer's context chip. */}
            <Sparkles size={12} className="flex-none text-zinc-500" />
            <span className="min-w-0 flex-1 truncate text-[12px] font-semibold text-zinc-200">
              {title}
            </span>
            <button type="button" onClick={() => setView('sessions')}
                    title="All sessions" aria-label="All sessions"
                    className="text-zinc-muted hover:text-zinc-200">
              <History size={12} />
            </button>
            {turns.length > 0 && (
              <button type="button" onClick={clear}
                      className="text-zinc-muted hover:text-zinc-200"
                      aria-label="Clear the conversation">
                <Trash2 size={12} />
              </button>
            )}
            {/* Only on the stacked layout: there the panel is an overlay, so
                it needs a way out of itself. On desktop the splitter and the
                top-bar toggle already close it. */}
            {onClose && (
              <button type="button" onClick={onClose}
                      className="text-zinc-muted hover:text-zinc-200 lg:hidden"
                      aria-label="Close the agent panel" title="Close">
                <X size={13} />
              </button>
            )}
          </>
        )}
      </header>

      {/* ── body ───────────────────────────────────────────────────────────── */}
      {view === 'sessions' ? (
        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
          <button type="button" onClick={newChat}
                  className="mb-2 flex w-full items-center gap-2 rounded-lg border border-white/10 px-2.5 py-2 text-[12px] text-zinc-300 hover:border-zinc-600 hover:text-zinc-100">
            <Plus size={13} /> New chat
          </button>
          {turns.length > 0 && (
            <button type="button" onClick={() => setView('chat')}
                    className="mb-1 flex w-full items-center gap-2 rounded-lg bg-white/[0.06] px-2.5 py-2 text-left">
              <span className="h-1.5 w-1.5 flex-none rounded-full bg-zinc-400" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[12px] text-zinc-100">
                  {title}
                </span>
                <span className="block text-[10.5px] text-zinc-muted">current</span>
              </span>
            </button>
          )}
          {sessions.length === 0 && turns.length === 0 && (
            <p className="px-1 py-6 text-center text-[11.5px] text-zinc-500">
              No sessions yet. Ask the agent something to start one.
            </p>
          )}
          {sessions.map((s) => (
            <button key={s.id} type="button" onClick={() => openSession(s)}
                    className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left hover:bg-white/5">
              <span className="h-1.5 w-1.5 flex-none rounded-full bg-zinc-600" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[12px] text-zinc-300">
                  {s.title}
                </span>
                <span className="block text-[10.5px] text-zinc-muted">
                  {s.turns.filter((t) => t.role === 'user').length} msg · {ago(s.at)} ago
                </span>
              </span>
            </button>
          ))}
        </div>
      ) : (
        <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto px-3 py-2.5">
          {/* What the agent changed on disk, ABOVE the conversation. An edit
              nobody looked at is an edit nobody approved. */}
          {onGit && gitOut && (
            <div className="mb-3">
              <div className="flex items-center gap-1">
                {(['status', 'diff', 'log'] as const).map((t) => (
                  <button key={t} type="button" onClick={() => onGit(t)}
                          disabled={gitBusy}
                          className={`rounded px-1.5 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide ${
                            gitView === t
                              ? 'bg-white/[0.08] text-zinc-100'
                              : 'text-zinc-muted hover:text-zinc-200'}`}>
                    {t}
                  </button>
                ))}
                <span className="flex-1" />
                <span className="text-[10.5px] text-zinc-muted">
                  on-disk changes
                </span>
              </div>
              <pre className="mt-1 max-h-40 overflow-auto rounded bg-black/40 p-1.5 font-mono text-[10.5px] leading-snug text-zinc-400">
                {gitOut.text || '(clean)'}
              </pre>
            </div>
          )}

          {turns.length === 0 && (
            <div className="mt-3">
              <p className="px-1.5 text-[11.5px] leading-relaxed text-zinc-500">
                {path
                  ? 'Ask about this file, or send the agent into the workspace.'
                  : 'The agent reads the workspace with its own tools.'}
              </p>
              {/* Three suggestions as plain rows. Four bordered pills wrapped
                  into a block of boxes at panel width, which read as form
                  fields and was the single busiest thing in the panel. The
                  seed text also has to change with the context: with no file
                  open, "this module" refers to nothing. */}
              <ul className="mt-1.5 space-y-0.5 px-0.5">
                {(path
                  ? ['Explain this file', 'Where is this used?', 'What could break?']
                  : ['Explore the workspace', 'Create a hello world script', 'Add a README']
                ).map((s) => (
                  <li key={s}>
                    <button type="button" onClick={() => setDraft(s)}
                            className="group flex w-full items-center gap-1.5 rounded-md px-1.5 py-1 text-left text-[11.5px] text-zinc-400 transition-colors hover:bg-white/[0.04] hover:text-zinc-200">
                      <ChevronRight size={11}
                                    className="flex-none text-zinc-600 group-hover:text-zinc-400" />
                      {s}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <ol className="space-y-3">
            {turns.map((t) => (
              <TurnRow
                key={t.id}
                t={t}
                stepsOpen={!!showSteps[t.id]}
                thinkOpen={showThinking}
                onToggleSteps={toggleSteps}
                onToggleThink={toggleThink}
                onRegen={regen}
                onCopy={copy}
                onVote={vote}
              />
            ))}
          </ol>

        </div>
      )}

      {/* ── composer ───────────────────────────────────────────────────────── */}
      <form onSubmit={(e) => { e.preventDefault(); void send(); }}
            className="flex-none border-t border-glass-border p-2">
        <div className="relative rounded-xl border border-white/[0.08] bg-surface-1 focus-within:border-zinc-600">
          {/* Attachments only. The active file used to get a chip here too,
              but it is already in the placeholder and in the breadcrumb; the
              row existed to repeat one filename and cost the composer a line. */}
          {attached.length > 0 && (
            <div className="flex flex-wrap items-center gap-1 px-2 pt-2">
              {attached.map((f) => (
                <span key={f} title={f}
                      className="flex items-center gap-1 rounded-md border border-white/10 bg-white/[0.05] px-1.5 py-0.5 text-[10.5px] text-zinc-300">
                  <FileCode2 size={10} />
                  {f.split('/').pop()}
                  <button type="button"
                          onClick={() => setAttached((a) => a.filter((x) => x !== f))}
                          aria-label={`Detach ${f}`}
                          className="text-zinc-500 hover:text-zinc-200">
                    <X size={9} />
                  </button>
                </span>
              ))}
            </div>
          )}
          <textarea
            ref={ta}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            rows={1}
            placeholder={busy
              ? 'Queue a follow-up…'
              : path
                ? `Ask about ${path.split('/').pop()}…`
                : 'Describe what to build…'}
            aria-label="Ask the agent about this code"
            className="max-h-36 min-h-[36px] w-full resize-none bg-transparent px-2.5 py-2 text-[12.5px] leading-snug text-zinc-100 outline-none placeholder:text-zinc-600 disabled:opacity-50"
          />
          {/* toolbar */}
          <div className="flex min-w-0 items-center gap-1 px-1.5 pb-1.5">
            <button type="button"
                    onClick={() => setPickQ((v) => (v === null ? '' : null))}
                    disabled={!files.length}
                    aria-pressed={pickQ !== null}
                    title={attached.length >= 6
                      ? 'Up to 6 attached files per question'
                      : files.length
                        ? 'Attach workspace files (read into the prompt)'
                        : 'The workspace file list is empty'}
                    aria-label="Attach workspace files"
                    className={`flex-none rounded-md p-1.5 hover:bg-white/5 disabled:opacity-40 ${
                      pickQ !== null ? 'text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'}`}>
              <Plus size={13} />
            </button>
            <ModelPicker model={model} onChange={onModelChange} busy={busy} onMeta={onCtx} />
            <select
              value={approvalMode}
              onChange={(e) => onApprovalModeChange?.(e.target.value as ApprovalMode)}
              aria-label="Agent approval mode"
              title={approvalMode === 'ask'
                ? 'Ask: every write and command waits for your approval'
                : approvalMode === 'auto-edit'
                  ? 'Auto-edit: writes apply immediately with Keep/Revert; commands still ask'
                  : approvalMode === 'auto-all'
                    ? 'Auto-all: writes AND commands run without asking. Destructive commands are still refused, and every command is audited.'
                    : 'Read-only: the agent may read and analyse but cannot write or run commands'}
              className="w-[92px] flex-none rounded border border-white/10 bg-black/40 px-1 py-1 text-[10px] font-semibold text-zinc-300 outline-none focus:border-violet-400"
            >
              <option value="ask">Ask</option>
              <option value="auto-edit">Auto edit</option>
              <option value="auto-all">Auto all</option>
              <option value="read-only">Read only</option>
            </select>
            {/* The context meter moved here from a second status bar under the
                composer. It only earns the width once a turn has actually used
                the window, so a fresh chat shows no meter at all. */}
            {usedTokens > 0 && ctxLen > 0 && (
              <span className="flex flex-none items-center gap-1"
                    title={`context window: ${usedTokens.toLocaleString()} of ${ctxLen.toLocaleString()} tokens used`}>
                <span className="h-1 w-8 overflow-hidden rounded-full bg-white/10">
                  <span className="block h-full rounded-full bg-violet-400/70"
                        style={{ width: `${pct}%` }} />
                </span>
                <span className="tabular-nums text-[10px] text-zinc-500">{kfmt(usedTokens)}</span>
              </span>
            )}
            <span className="min-w-0 flex-1" />
            {queued.length > 0 && (
              <span
                title={`${queued.length} follow-up${queued.length === 1 ? '' : 's'} waiting`}
                className="flex items-center gap-1 rounded-md border border-white/10 bg-white/[0.05] px-1.5 py-0.5 text-[10.5px] text-zinc-300">
                <Clock size={10} /> {queued.length} queued
                <button type="button" onClick={() => setQueued([])}
                        aria-label="Clear queued messages"
                        className="ml-0.5 text-zinc-500 hover:text-zinc-200">
                  <X size={10} />
                </button>
              </span>
            )}
            <button type="submit"
                    disabled={!draft.trim()}
                    aria-label={busy ? 'Queue message' : 'Send'}
                    title={busy ? 'Queue — runs when the current turn finishes' : 'Send'}
                    className="rounded-md bg-accent p-1.5 text-white hover:brightness-110 disabled:opacity-40">
              {busy ? <Clock size={12} /> : <Sparkles size={12} />}
            </button>
            {busy && (
              <button type="button" onClick={stop} aria-label="Stop"
                      className="rounded-md border border-white/15 p-1.5 text-zinc-200 hover:border-rose-400">
                <Square size={12} />
              </button>
            )}
          </div>
          {pickQ !== null && (
            <div className="absolute bottom-full left-1 z-20 mb-1 w-72 overflow-hidden rounded-lg border border-glass-border bg-surface-1 shadow-lg">
              <input
                autoFocus
                value={pickQ}
                onChange={(e) => setPickQ(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Escape') setPickQ(null); }}
                placeholder="Filter workspace files…"
                aria-label="Filter files to attach"
                className="w-full border-b border-glass-border bg-transparent px-2 py-1.5 text-[11.5px] text-zinc-200 outline-none placeholder:text-zinc-600"
              />
              <ul className="max-h-48 overflow-auto py-1">
                {files
                  .filter((f) => f !== path && !attached.includes(f))
                  .filter((f) => f.toLowerCase().includes(pickQ.toLowerCase()))
                  .slice(0, 40)
                  .map((f) => (
                    <li key={f}>
                      <button type="button"
                        onClick={() => {
                          setAttached((a) => (a.length >= 6 ? a : [...a, f]));
                          setPickQ(null);
                        }}
                        title={f}
                        className="block w-full truncate px-2 py-1 text-left text-[11.5px] text-zinc-300 hover:bg-white/10">
                        {f}
                      </button>
                    </li>
                  ))}
              </ul>
            </div>
          )}
        </div>
      </form>
    </div>
  );
}
