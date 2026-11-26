import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowUp, ChevronDown, ChevronRight, FolderOpen, GitBranch, Globe, ListChecks, Plus, Sparkles, Square, TriangleAlert, X } from 'lucide-react';
import { api, type CodeChecks, type StudioCommand, type StudioMention } from '../../api';
import type { StudioRoot, StudioSession } from '../../api';
import { ModelMenu } from '../../components/ModelMenu';
import { sessionName } from '../../lib/studioSession';

/** The composer card: the input, its three context sources, its controls and
 *  the chips that say what the agent will actually be handed.
 *
 *  Extracted from AgentPanel because it appears twice (centred on the empty
 *  state, pinned under a transcript) and the two must not drift — a composer
 *  that loses a control when work starts is a control the user thinks they
 *  still have.
 *
 *  The three sources are the point of this slice, and each one is a different
 *  promise:
 *    `@path`  — this file, in the explorer's own vocabulary, resolved by the
 *               server against the folders THIS session holds. The candidate
 *               list is fetched rather than filtered locally because shipping
 *               a repository tree to the browser per keystroke is not a
 *               feature;
 *    `/cmd`   — a verb the server also knows. The list comes from
 *               `/api/studio/commands`, so a popup cannot offer a button that
 *               does nothing;
 *    `#problem` — what the machine already told us is wrong: the active file's
 *               syntax check and the project's last checks. A coding agent
 *               that is not shown the error it is being asked to fix guesses.
 */

/** What the caret is currently inside of. Derived on every render rather than
 *  stored: a token that got edited out of existence must not keep its popup
 *  open, and the only way to be sure is to recompute it from the text. */
export type TokenKind = '' | '@' | '/' | '#';
export interface Token { kind: TokenKind; frag: string; from: number }

export function caretToken(text: string, caret: number): Token {
  const before = text.slice(0, caret);
  const at = /(?:^|[\s(,;:])@([^\s@]*)$/.exec(before);
  if (at) return { kind: '@', frag: at[1] ?? '', from: caret - (at[1]?.length ?? 0) - 1 };
  const hash = /(?:^|[\s(,;:])#([^\s#]*)$/.exec(before);
  if (hash) return { kind: '#', frag: hash[1] ?? '', from: caret - (hash[1]?.length ?? 0) - 1 };
  // A slash command is only a command at the very start of the message:
  // "check /tmp/x" is a path, not a verb.
  const slash = /^\/([^\s/]*)$/.exec(before);
  if (slash) return { kind: '/', frag: slash[1] ?? '', from: 0 };
  return { kind: '', frag: '', from: caret };
}

/** The `@path` tokens typed into the message, as chips. Recomputed from the
 *  text rather than kept in state, so deleting a mention in the prose removes
 *  its chip — a chip that outlives the text it came from is a lie about what
 *  the agent will read. */
export function mentionTokens(text: string): string[] {
  const out: string[] = [];
  for (const m of text.matchAll(/(?:^|[\s(,;:])@([^\s@]+)/g)) {
    const tok = (m[1] ?? '').replace(/[.,;:!?)\]]+$/, '');
    if (tok && !out.includes(tok)) out.push(tok);
  }
  return out;
}

export interface ProblemRow {
  path?: string;
  line?: number;
  message: string;
  severity?: string;
}

/** Flatten `/api/code/checks` into problem rows.
 *
 *  The output is the tool's own stdout/stderr, line by line, because "3 checks
 *  failed" is not something to hand a coding agent — the compiler message is.
 *  Bounded at 12 rows for the transcript, and the cap is stated in the text so
 *  a person knows the agent saw a summary rather than the whole log. */
export function checksToRows(r: CodeChecks): ProblemRow[] {
  const rows: ProblemRow[] = [];
  for (const one of r.results || []) {
    /* A check that PASSED has no problem to show. Reading its stdout as rows
       made a green run read "2 problems" — the two lines of pytest output — and
       trained the person to ignore the counter that is supposed to mean
       something. `available === false` still reports: a runner that could not
       start is a problem whatever `ok` says. */
    if (one.ok && one.available !== false) continue;
    const text = String(one.error || one.stderr || one.stdout || '');
    const head = text.split('\n').map((s) => s.trim()).filter(Boolean);
    for (const line of head.slice(0, 4)) {
      rows.push({ message: `${one.tool}: ${line}`.slice(0, 220),
                  severity: 'error' });
    }
    /* `available` is optional, and a result that ran is available whether or
       not the server said so — checking `!available` turned every real failure
       into a row claiming the tool was not installed. */
    if (one.available === false) {
      rows.push({ message: `${one.tool}: not installed in this project`,
                  severity: 'warn' });
    }
    if (rows.length >= 12) break;
  }
  if (!rows.length && r.error) rows.push({ message: r.error.slice(0, 220), severity: 'error' });
  return rows.slice(0, 12);
}

export function checksSummary(r: CodeChecks): string {
  const ran = (r.results || []).filter((x) => x.available !== false);
  const bad = ran.filter((x) => !x.ok).map((x) => x.tool);
  const skipped = (r.results || []).filter((x) => x.available === false).map((x) => x.tool);
  return [
    ran.length ? `${ran.length} check(s) ran` : 'no checks were runnable',
    /* "no checks were runnable · all runnable checks passed" reads as a pass.
       When nothing ran there is no verdict, and saying so is the whole point of
       the line. */
    ran.length
      ? (bad.length ? `failed: ${bad.join(', ')}` : 'all runnable checks passed')
      : (r.error ? String(r.error) : 'nothing verified this change'),
    skipped.length ? `not installed: ${skipped.join(', ')}` : '',
    r.note ? String(r.note) : '',
  ].filter(Boolean).join(' · ');
}

export interface BrowserPageRef {
  url: string;
  title?: string;
  text?: string;
  errors?: string[];
}

export interface RunMeta {
  hops?: number;
  maxHops?: number;
  contextChars?: number;
  contextCap?: number;
  /** Epoch seconds the `started` frame arrived, so the elapsed readout is the
   *  run's clock, not the browser's render loop. */
  startedAt?: number;
  running?: boolean;
}

export function Composer({ input, setInput, disabled, readOnly, setReadOnly,
                           approval, setApproval, plan, menu, setMenu, roots,
                           onAddFolder, onBrowseDisk, onGit, projects, onOpenProject, onSwitchSession, running, send,
                           stop, composerRef, wide, sid, model, setModel,
                           onCommand, meta, problems, setProblems,
                           browserPage, setBrowserPage, refreshBrowser,
                           onPickFile, onEnqueue }: {
  input: string;
  setInput: (v: string) => void;
  disabled: boolean;
  readOnly: boolean;
  setReadOnly: (v: boolean) => void;
  approval: 'manual' | 'files' | 'files+exec';
  setApproval: (v: 'manual' | 'files' | 'files+exec') => void;
  menu: '' | 'mode' | 'approval' | 'folder' | 'model' | 'projects';
  setMenu: (v: '' | 'mode' | 'approval' | 'folder' | 'model' | 'projects') => void;
  roots: StudioRoot[];
  onAddFolder: () => void;
  /** Open the Git panel. */
  onGit?: () => void;
  /** Open the in-app disk browser (drive list → folders). */
  onBrowseDisk?: () => void;
  /** Every project the studio knows, with its sessions. */
  /** One entry per project folder, with the sessions in it ordered by when
   *  they were last touched — the flyout is "where was I", not "what exists". */
  /** The live plan for the composer's header row, if any. */
  plan?: { title: string; status: string }[];
  projects?: { path: string; name: string; sessions: StudioSession[] }[];
  /** Open (or start a session for) a project. */
  onOpenProject?: (path: string) => void;
  /** Switch to an existing session. */
  onSwitchSession?: (sid: string) => void;
  branch: string | null;
  refreshBranch: () => void;
  running: boolean;
  send: () => void;
  stop: () => void;
  composerRef: React.MutableRefObject<HTMLTextAreaElement | null>;
  wide: boolean;
  sid: string;
  model: string;
  setModel: (v: string) => void;
  /** A `/command` the person chose. The composer does not run it: it has no
   *  business knowing how to restore a checkpoint. */
  onCommand: (name: string, rest: string) => void;
  meta: RunMeta;
  problems: ProblemRow[];
  setProblems: (v: ProblemRow[]) => void;
  /** `@browser` — the page this session's browser is on, or null when there
   *  is none. An offer, not a guess: with no browser open there is no row. */
  browserPage: BrowserPageRef | null;
  setBrowserPage: (v: BrowserPageRef | null) => void;
  /** Called when the composer gets focus, so `@browser` can offer the page
   *  that is open NOW rather than one that was open when the tab loaded. */
  refreshBrowser?: () => void;
  /** Opens a path in the editor, from a chip. */
  onPickFile: (path: string) => void;
  /** Ctrl+Enter: push the current text onto the agent's queue instead of
   *  sending it now, so it runs after the current turn finishes. */
  onEnqueue?: (text: string) => void;
}) {
  const [caret, setCaret] = useState(0);
  const [pick, setPick] = useState(0);
  const [dismissed, setDismissed] = useState('');
  const [mentions, setMentions] = useState<StudioMention[]>([]);
  const [mErr, setMErr] = useState('');
  const [commands, setCommands] = useState<StudioCommand[]>([]);
  const [checking, setChecking] = useState(false);
  const [checkNote, setCheckNote] = useState('');
  /* Reasoning effort is a per-composer choice; the gateway's own effort flag
     is read from it by the agent layer when a model supports it. */
  const [effort, setEffort] = useState<'Low' | 'Medium' | 'High'>('High');
  /* Projects is its own centred dialog — it never reveals the Explorer. */
  const [projectsOpen, setProjectsOpen] = useState(false);
  /* The session flyout, rendered into document.body rather than inside the
     dialog. It used to be an absolutely-positioned child of the row, and the
     dialog's own `overflow-auto` body clipped it — the list you were reaching
     for was cut off by the container it came from. A portal escapes that, and
     `position: fixed` lets it sit beside the row wherever the row is. */
  const [fly, setFly] = useState<{
    name: string; sessions: StudioSession[]; rect: DOMRect;
  } | null>(null);
  const closeTimer = useRef<number | null>(null);

  const openSessions = useCallback((p: { name: string; sessions: StudioSession[] },
                                   el: HTMLElement) => {
    if (closeTimer.current) { window.clearTimeout(closeTimer.current); closeTimer.current = null; }
    if (!p.sessions.length) { setFly(null); return; }
    setFly({ name: p.name, sessions: p.sessions, rect: el.getBoundingClientRect() });
  }, []);

  const closeSoon = useCallback(() => {
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    // A short grace period: the pointer crosses a gap between the row and the
    // flyout, and leaving the row must not close it before the pointer arrives.
    closeTimer.current = window.setTimeout(() => { setFly(null); closeTimer.current = null; }, 140);
  }, []);

  const keepOpen = useCallback(() => {
    if (closeTimer.current) { window.clearTimeout(closeTimer.current); closeTimer.current = null; }
  }, []);

  useEffect(() => {
    if (!fly) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setFly(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [fly]);

  const gen = useRef(0);

  const token = useMemo(() => caretToken(input, caret), [input, caret]);
  const chipPaths = useMemo(() => mentionTokens(input), [input]);

  /* Roster of commands, once per mount. The server owns the list; this only
     caches it so the popup is instant after the first `/`. */
  useEffect(() => {
    let live = true;
    void api.studioCommands().then((r) => {
      if (live) setCommands(r.commands || []);
    }).catch(() => { /* an empty popup is honest; a fake command is not */ });
    return () => { live = false; };
  }, []);

  /* `@` completions, debounced and generation-guarded. The guard matters more
     than the debounce: two keystrokes in flight resolve out of order and the
     popup ends up listing the FIRST fragment's files, which is how a completion
     feature starts inserting the wrong path. */
  useEffect(() => {
    if (token.kind !== '@') { setMentions([]); setMErr(''); return; }
    const mine = ++gen.current;
    setPick(0);
    const t = setTimeout(() => {
      void api.studioMentions(sid, token.frag).then((r) => {
        if (gen.current !== mine) return;
        setMentions(r.matches || []);
        setMErr(r.error || (r.stopped ? (r.note || '') : ''));
      }).catch((e) => {
        if (gen.current !== mine) return;
        setMentions([]);
        setMErr(e instanceof Error ? e.message : 'the folder list failed');
      });
    }, 110);
    return () => clearTimeout(t);
  }, [token.kind, token.frag, sid]);

  const rows: { label: string; hint: string; value: string }[]
    = token.kind === '@'
      ? [
        // `@browser` is offered first, and only when a page is actually open.
        // Pointing the agent at "the page I'm looking at" is the same act as
        // pointing it at a file, and a row that offered a closed browser
        // would hand the model a context that isn't there.
        ...(browserPage && 'browser'.startsWith(token.frag.toLowerCase())
          ? [{ label: '@browser', hint: `attach this page · ${browserPage.url.slice(0, 60)}`,
               value: '@@browser' }]
          : []),
        ...mentions.map((m) => ({ label: m.name, hint: m.path,
                                 value: m.path + (m.dir ? '/' : '') })),
      ]
      : token.kind === '/'
        ? commands
          .filter((c) => c.name.startsWith(token.frag))
          /* The trailing space is what closes the popup after a completion.
             Without it the caret still sits inside a well-formed `/token`, the
             list stays open, and a second Enter "accepts" the same row again —
             a command you cannot run by pressing Enter twice is a command that
             looks broken. */
          .map((c) => ({ label: `/${c.name}`, hint: c.desc,
                         value: `/${c.name} ` }))
        : token.kind === '#'
          ? [
            { label: '#problems', hint: problems.length
              ? `${problems.length} from the last check`
              : 'run a syntax check on the open file', value: '#problems' },
            { label: '#checks', hint: 'the project\'s own test/build output',
              value: '#checks' },
            ...(problems.length
              ? problems.slice(0, 8).map((p, i) => ({
                label: `${p.path || 'file'}${p.line ? `:${p.line}` : ''}`,
                hint: p.message, value: `#p${i}` }))
              : []),
          ]
          : [];
  const open = rows.length > 0 && dismissed !== `${token.kind}${token.frag}`;

  const accept = useCallback((value: string) => {
    if (value === '@@browser') {
      // Not a path in the text — a chip. Leaving `@browser` in the prose
      // would make the server look for a folder called "browser", fail, and
      // tell the person the mention was unreadable.
      if (browserPage) setBrowserPage(browserPage);
      const from = token.from;
      const next = (input.slice(0, from) + input.slice(caret)).replace(/\s{2,}/g, ' ');
      setInput(next);
      setCaret(from);
      setDismissed('@@@end');
      const el = composerRef.current;
      if (el) requestAnimationFrame(() => {
        el.focus();
        el.setSelectionRange(from, from);
      });
      return;
    }
    const from = token.kind === '/' ? 0 : token.from;
    const to = caret;
    const next = input.slice(0, from) + value + input.slice(to);
    const at = from + value.length;
    setInput(next);
    // The caret state is the popup's input, and a controlled textarea does not
    // always fire a selection event for a programmatic change. Setting it here
    // is what stops the just-accepted path from keeping the old completion
    // list open under the new text.
    setCaret(at);
    setDismissed(`${token.kind}@@end`);
    const el = composerRef.current;
    if (el) requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(at, at);
    });
  }, [input, caret, token, setInput, composerRef, browserPage, setBrowserPage]);

  /** `#` is not a completion, it is an action: it fetches the problems rather
   *  than naming them. Doing it here keeps the popup a pure picker, and the
   *  text it leaves behind names the source the person actually chose — a
   *  `#checks` pick that rewrote itself to `#problems` would be a chip for
   *  something the agent was not given. */
  const runProblemCheck = useCallback(async (which: string) => {
    setChecking(true);
    setCheckNote('');
    let label = which;
    try {
      if (which === '#checks') {
        // Session-scoped: without sid the route resolves the PROCESS workspace,
        // so the agent would be handed another project's test output.
        const r = await api.codeChecks(undefined, undefined, sid);
        const rowsOut = checksToRows(r);
        setProblems(rowsOut);
        if (!rowsOut.length) setCheckNote('the checks produced no output');
      } else {
        const got = await api.studioChanges(sid);
        const rowsOut = ((got?.changes || []) as { path: string; error?: string }[])
          .filter((c) => c.error)
          .map((c) => ({ path: c.path, message: String(c.error).slice(0, 220),
                         severity: 'error' }));
        setProblems(rowsOut);
        label = '#problems';
        if (!rowsOut.length) {
          setCheckNote('nothing flagged: the review list has no failing file. '
                       + 'Checks are per-file syntax, not a full test run.');
        }
      }
    } catch (e) {
      setCheckNote(e instanceof Error ? e.message : 'the check failed');
    } finally {
      setChecking(false);
      const next = input.replace(/(?:^|[\s(,;:])#[^\s#]*$/, ` ${label} `);
      setInput(next);
      setCaret(next.length);
    }
  }, [sid, input, setInput, setProblems]);

  const keyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault();
      setPick((p) => (p + (e.key === 'ArrowDown' ? 1 : rows.length - 1)) % rows.length);
      return;
    }
    if (open && (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey))) {
      e.preventDefault();
      const row = rows[Math.min(pick, rows.length - 1)];
      /* Enter on a highlighted command RUNS it. A popup that only ever
         completes the text would make the person press Enter twice, and the
         second one has to be the one that acts — that is what "/undo" means
         to a person, not "/undo with the word undo in the box". */
      if (token.kind === '/' && e.key === 'Enter') {
        const cmd = /^\/([a-z-]+) ?([\s\S]*)$/i.exec(row.value.trim());
        setInput('');
        setDismissed('');
        if (cmd) onCommand(cmd[1].toLowerCase(), (cmd[2] || '').trim());
        return;
      }
      if (row.value.startsWith('#problems') || row.value === '#checks') {
        void runProblemCheck(row.value === '#checks' ? '#checks' : '#problems');
      } else if (row.value.startsWith('#p')) {
        setDismissed(`${token.kind}${token.frag}`);
      } else {
        accept(row.value);
      }
      return;
    }
    if (e.key === 'Escape' && open) {
      e.preventDefault();
      setDismissed(`${token.kind}${token.frag}`);
      return;
    }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      /* Ctrl/Cmd+Enter queues instead of sending: the text joins the stack and
         runs after the current turn, so a second thought is never lost while
         the agent is busy. */
      e.preventDefault();
      if (onEnqueue && input.trim()) { onEnqueue(input); setInput(''); setDismissed(''); }
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const cmd = /^\/([a-z-]+)([\s\S]*)$/i.exec(input.trim());
      if (cmd) { setInput(''); onCommand(cmd[1].toLowerCase(), (cmd[2] || '').trim()); return; }
      send();
    }
  };

  const approvalLabel = approval === 'manual' ? 'Manual'
    : approval === 'files' ? 'Auto files' : 'Auto files + commands';

  return (
    <div data-composer-menu="" className="w-full">
      <div className={'relative w-full rounded-xl border border-white/15 bg-surface-1 '
                   + 'shadow-[0_1px_0_rgba(255,255,255,0.03)_inset] transition-colors '
                   + 'focus-within:border-white/30'}>
      {/* The agent's plan rides on top of the bar it is working from, and is
          gone the moment the last step is done. A checklist that finishes but
          stays on screen is a receipt nobody needs while typing the next task. */}
      {!!plan?.length && !plan.every((s) => /done|complete/i.test(s.status)) && (
        <div className="border-b border-white/10 px-3 pb-1.5 pt-2">
          <div className="flex items-center gap-1.5">
            <ListChecks size={11} className="flex-none text-zinc-500" aria-hidden />
            <span className="text-[11px] font-medium text-zinc-400">
              plan · {plan.filter((s) => /done|complete/i.test(s.status)).length}/{plan.length}
            </span>
          </div>
          <ol className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5">
            {plan.map((s, i) => (
              <li key={`${s.title}-${i}`}
                  className={'flex min-w-0 items-center gap-1 text-[11.5px] '
                    + (/done|complete/i.test(s.status) ? 'text-zinc-600 line-through'
                      : /progress|doing/i.test(s.status) ? 'text-zinc-200'
                        : 'text-zinc-500')}>
                <span aria-hidden className={/done|complete/i.test(s.status)
                  ? 'text-ok' : /progress|doing/i.test(s.status) ? 'text-accent' : ''}>
                  {/done|complete/i.test(s.status) ? '✓'
                    : /progress|doing/i.test(s.status) ? '◐' : '○'}
                </span>
                <span className="truncate">{s.title}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
      {open && (
        <div role="listbox" aria-label="Completions"
             className="absolute bottom-full left-3 z-40 mb-1 w-[min(460px,90%)] overflow-hidden rounded-md border border-glass-border bg-surface-1 shadow-depth">
          {token.kind === '@' && token.frag === '' && (
            <p className="border-b border-glass-border px-3 py-1.5 text-[12px] text-zinc-muted">
              files and folders in this session's folders
            </p>
          )}
          {checking && (
            <p className="px-3 py-2 text-[12px] text-zinc-muted">checking…</p>
          )}
          <ul className="max-h-[240px] overflow-auto">
            {rows.map((r, i) => (
              <li key={`${r.value}-${i}`}>
                <button type="button" role="option" aria-selected={i === pick}
                        onMouseEnter={() => setPick(i)}
                        onClick={() => {
                          if (r.value === '#checks' || r.value === '#problems') void runProblemCheck(r.value);
                          else accept(r.value);
                        }}
                        className={'flex w-full items-baseline gap-2 px-3 py-1.5 text-left hover:bg-white/5 '
                          + (i === pick ? 'bg-white/5' : '')}>
                  {/* Two weights, not two shades of one: the command is what
                      you press, the hint is what it does. Same size and
                      colour made "/undo restore the last checkpoint" read as
                      one blurred line. */}
                  <span className="flex-none truncate text-[13px] font-medium text-zinc-100">{r.label}</span>
                  <span aria-hidden className="flex-none text-zinc-600">·</span>
                  <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-muted"
                        title={r.hint}>{r.hint}</span>
                </button>
              </li>
            ))}
          </ul>
          {(mErr || checkNote) && (
            <p className="border-t border-glass-border px-3 py-1.5 text-[12px] text-warn">
              {mErr || checkNote}
            </p>
          )}
          {token.kind === '@' && mentions.length > 0 && (
            <p className="border-t border-glass-border px-3 py-1 text-[11px] text-zinc-muted">
              Tab to insert · {rows.length} shown
            </p>
          )}
        </div>
      )}

      <textarea
        ref={composerRef}
        value={input}
        onChange={(e) => {
          setInput(e.target.value);
          setCaret(e.target.selectionStart ?? e.target.value.length);
        }}
        onClick={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
        onFocus={() => refreshBrowser?.()}
        onKeyUp={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
        onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
        onKeyDown={keyDown}
        rows={!input ? 1 : (wide ? 3 : 2)}
        disabled={disabled}
        placeholder={disabled
          ? 'open a folder first'
          : 'Ask the agent to make a change…'}
        aria-label="Ask the agent for a change"
        className="w-full resize-none bg-transparent px-4 pt-3.5 text-[15px] leading-relaxed outline-none placeholder:text-zinc-muted disabled:opacity-50"
      />

      {/* What the agent will be handed, as chips. Removable, because the chip
          and the text are the same thing: dismissing one edits the message. */}
      {(chipPaths.length > 0 || !!meta.maxHops || problems.length > 0
        || !!browserPage) && (
        <div className="flex flex-wrap items-center gap-1.5 px-3 pt-1.5">
          {browserPage && (
            <span title={`${browserPage.title || ''}\n${browserPage.url}`}
                  className="flex max-w-[300px] items-center gap-1 rounded-md border border-info/40 bg-info-dim px-1.5 py-0.5 text-[12px] text-info">
              <Globe size={12} aria-hidden className="flex-none" />
              {browserPage.url.replace(/^https?:\/\//, '').slice(0, 40)}
              {!!browserPage.errors?.length && (
                <span className="text-danger">· {browserPage.errors.length} err</span>
              )}
              <button type="button" aria-label="Remove the page from context"
                      onClick={() => setBrowserPage(null)}
                      className="hover:text-danger"><X size={11} /></button>
            </span>
          )}
          {chipPaths.map((p) => (
            <span key={p}
                  className="flex items-center gap-1 rounded-md border border-glass-border bg-surface-1 px-1.5 py-0.5 font-mono text-[11.5px] text-zinc-200">
              <button type="button" onClick={() => onPickFile(p)}
                      title={`Open ${p} in the editor`}
                      className="max-w-[220px] truncate hover:text-zinc-300">{p}</button>
              <button type="button" aria-label={`Remove ${p}`}
                      onClick={() => setInput(input.replace(
                        new RegExp(`\\s?@${p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\s|$)`, 'g'), ' ').trim() + ' ')}
                      className="text-zinc-muted hover:text-danger"><X size={11} /></button>
            </span>
          ))}
          {problems.length > 0 && (
            <span title={problems.map((p) => `${p.path || ''} ${p.message}`).join('\n')}
                  className="flex items-center gap-1 rounded-md border border-warn/40 bg-warn-dim px-1.5 py-0.5 text-[12px] text-warn">
              <TriangleAlert size={12} aria-hidden className="flex-none" />
              {problems.length} problem{problems.length === 1 ? '' : 's'}
              <button type="button" aria-label="Remove problems from context"
                      onClick={() => setProblems([])}
                      className="hover:text-danger"><X size={11} /></button>
            </span>
          )}
          {!!meta.maxHops && (
            <span title={`the loop stops after this many model calls (${meta.contextCap
              ? `, context ceiling ${meta.contextCap} chars` : ''})`}
                  className="rounded-md border border-glass-border px-1.5 py-0.5 font-mono text-[12px] text-zinc-muted">
              hop {meta.hops ?? 0}/{meta.maxHops}
            </span>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5 px-3 pb-2.5 pt-1.5">
        <button type="button" onClick={() => { setInput(`${input}@`); composerRef.current?.focus(); }}
                aria-label="Attach context" title="Attach context (@)"
                className="flex h-7 w-7 flex-none items-center justify-center rounded-full border border-glass-border text-zinc-muted hover:bg-white/5 hover:text-zinc-100">
          <Plus size={14} />
        </button>
        <button type="button" onClick={onGit}
                aria-label="Git" title="Git"
                className="flex h-7 w-7 flex-none items-center justify-center rounded-full border border-glass-border text-zinc-muted hover:bg-white/5 hover:text-zinc-100">
          <GitBranch size={14} />
        </button>

        {/* Plan / Build — always visible, the way OpenCode's agent switch is.
            A dropdown hid the single most important choice (may the agent write
            to your repo?); this keeps it one click away and impossible to miss. */}
        <div role="group" aria-label="Agent mode"
             className="flex h-7 flex-none items-center rounded-md border border-glass-border p-0.5">
          <button type="button" onClick={() => setReadOnly(true)}
                  aria-pressed={readOnly}
                  title="Plan — read-only: it can investigate and explain, and cannot write"
                  className={'rounded px-2 text-[11.5px] transition '
                    + (readOnly
                      ? 'bg-white/10 text-zinc-50'
                      : 'text-zinc-400 hover:text-zinc-200')}>
            Plan
          </button>
          <button type="button" onClick={() => setReadOnly(false)}
                  aria-pressed={!readOnly}
                  title="Build — it reads and edits files in this session's folders"
                  className={'rounded px-2 text-[11.5px] transition '
                    + (!readOnly
                      ? 'bg-white/10 text-zinc-200'
                      : 'text-zinc-400 hover:text-zinc-200')}>
            Build
          </button>
        </div>

        <Menu id="approval" label={approvalLabel} open={menu === 'approval'} setOpen={setMenu}>
          <MenuItem onClick={() => { setApproval('manual'); setMenu(''); }}
                    active={approval === 'manual'} label="Manual"
                    hint="ask before every write and every command" />
          <MenuItem onClick={() => { setApproval('files'); setMenu(''); }}
                    active={approval === 'files'} label="Auto files"
                    hint="writes land immediately, each hunk reviewed afterwards" />
          <MenuItem onClick={() => { setApproval('files+exec'); setMenu(''); }}
                    active={approval === 'files+exec'} label="Auto files + commands"
                    hint="also runs the project's checks without asking — it executes repo code" />
        </Menu>

        <Menu id="model" label={model ? model.split('/').slice(-1)[0] : 'Auto model'}
              glyph={<Sparkles size={11} />}
              open={menu === 'model'} setOpen={setMenu}>
          <ModelMenu model={model}
                     onSelect={(id) => { setModel(id); setMenu(''); }}
                     busy={running}
                     label="the coding agent" />
        </Menu>

        <button type="button" title="Reasoning effort"
                onClick={() => setEffort((e) => e === 'High' ? 'Low' : e === 'Low' ? 'Medium' : 'High')}
                className="flex h-7 flex-none items-center gap-1 rounded-md px-1.5 text-[11.5px] text-zinc-400 transition-colors hover:text-zinc-100">
          <span>{effort}</span><ChevronDown size={11} />
        </button>

        <span className="min-w-0 flex-1" />

        {running && onEnqueue && input.trim() && (
          <span className="flex-none text-[11px] text-zinc-500">Ctrl+Enter to queue</span>
        )}
        {running ? (
          <button type="button" onClick={stop}
                  title="Stop generation" aria-label="Stop generation"
                  className="flex h-7 w-7 flex-none items-center justify-center rounded-lg bg-zinc-200 text-zinc-900 transition-colors hover:bg-white">
            <Square size={11} fill="currentColor" />
          </button>
        ) : (
          <button type="button" onClick={() => {
            const cmd = /^\/([a-z-]+)([\s\S]*)$/i.exec(input.trim());
            if (cmd) { setInput(''); onCommand(cmd[1].toLowerCase(), (cmd[2] || '').trim()); return; }
            send();
          }}
                  disabled={!input.trim() || disabled}
                  aria-label="Send"
                  className="flex h-8 w-8 flex-none items-center justify-center rounded-full bg-ok text-[#0b0b0e] font-bold transition hover:brightness-110 disabled:opacity-40">
            <ArrowUp size={15} />
          </button>
        )}
      </div>
      </div>
      {/* Projects is its own centred dialog; it never reveals the Explorer. */}
      <div className="mt-1.5 flex items-center gap-1.5">
        <button type="button" onClick={() => setProjectsOpen(true)}
                className="flex h-7 items-center gap-1.5 rounded-md border border-glass-border px-2 text-[12px] text-zinc-200 transition-colors hover:border-white/20 hover:bg-white/5">
          <FolderOpen size={12} className="text-zinc-500" />
          <span className="truncate">{roots.length === 1 ? roots[0].name : roots.length ? `${roots.length} projects` : 'Projects'}</span>
          <ChevronDown size={10} className="text-zinc-500" />
        </button>
      </div>
      {projectsOpen && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/55 backdrop-blur-[3px]"
             onPointerDown={(e) => { if (e.target === e.currentTarget) setProjectsOpen(false); }}>
          <div role="dialog" aria-modal="true" aria-label="Projects"
               className="w-[520px] max-w-[92vw] rounded-xl border border-glass-border bg-surface-1 shadow-2xl">
            <div className="flex items-center gap-2 border-b border-glass-border px-4 py-3">
              <FolderOpen size={15} className="text-accent" />
              <span className="text-[13px] font-semibold text-zinc-100">Projects</span>
              <span className="min-w-0 flex-1" />
              <button type="button" aria-label="Close projects" onClick={() => setProjectsOpen(false)}
                      className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 hover:bg-white/5 hover:text-zinc-100">
                <X size={14} />
              </button>
            </div>
            <div className="max-h-[55vh] overflow-auto p-2">
              {projects && projects.length ? projects.map((p) => (
                <div key={p.path}>
                  <div className="flex items-center gap-1 rounded-md px-1 hover:bg-white/5"
                       onMouseEnter={(e) => openSessions(p, e.currentTarget)}
                       onMouseLeave={closeSoon}>
                    <button type="button" onClick={() => onOpenProject?.(p.path)}
                            className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left text-[13px] text-zinc-200">
                      <FolderOpen size={13} className="text-zinc-500" />
                      <span className="min-w-0 flex-1 truncate" title={p.path}>{p.name}</span>
                      <span className="flex-none text-[11px] text-zinc-muted">{p.sessions.length}</span>
                    </button>
                    {p.sessions.length > 0 && (
                      <span aria-hidden className="flex h-6 w-6 flex-none items-center justify-center text-zinc-500">
                        <ChevronRight size={12} />
                      </span>
                    )}
                  </div>
                </div>
              )) : (
                <p className="px-3 py-4 text-center text-[12px] text-zinc-muted">No projects yet.</p>
              )}
            </div>
            <div className="border-t border-glass-border p-2">
              <button type="button" onClick={() => { setProjectsOpen(false); (onBrowseDisk || onAddFolder)(); }}
                      className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-[13px] text-zinc-200 hover:bg-white/5">
                <Plus size={13} className="text-zinc-500" /> Open a new project…
              </button>
            </div>
          </div>
        </div>
      )}
      {fly && createPortal(
        <div role="menu" aria-label={`Sessions for ${fly.name}`}
             className="fixed z-[130] w-[248px] overflow-hidden rounded-md border border-glass-border bg-surface-2 p-1 shadow-depth"
             style={flyoutStyle(fly.rect)}
             onMouseEnter={keepOpen}
             onMouseLeave={closeSoon}>
          {fly.sessions.map((s) => (
            <button key={s.sid} type="button" role="menuitem"
                    onClick={() => { onSwitchSession?.(s.sid); setFly(null); setProjectsOpen(false); }}
                    title={s.updated ? new Date(s.updated * 1000).toLocaleString() : ''}
                    className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] text-zinc-300 hover:bg-white/5 hover:text-zinc-100">
              {/* The dot is the session you are in, not the one the server
                  thinks is current (it binds per request, so it is false). */}
              <span aria-hidden className={'h-1.5 w-1.5 flex-none rounded-full '
                + (s.sid === sid ? 'bg-accent' : 'bg-transparent')} />
              <span className={'min-w-0 flex-1 truncate'
                + (s.sid === sid ? ' font-medium text-zinc-100' : '')}>{sessionName(s)}</span>
              {!!s.pending_review && (
                <span className="flex-none rounded-full bg-warn/15 px-1.5 py-px text-[10px] font-semibold tabular-nums text-warn"
                      title={`${s.pending_review} unreviewed change(s)`}>
                  {s.pending_review}
                </span>
              )}
              {!!s.live && (
                <span className="flex-none text-[10px] text-ok" title="running now">●</span>
              )}
            </button>
          ))}
        </div>,
        document.body,
      )}
    </div>
  );
}

/** Where the session flyout goes: beside the row, or on its left when the
 *  window is too narrow. Fixed positioning is what lets it escape the dialog's
 *  scrolling body, which used to clip it. */
function flyoutStyle(rect: DOMRect): React.CSSProperties {
  const width = 248;
  const gap = 6;
  const room = typeof window === 'undefined' ? 0 : window.innerWidth;
  const left = room && rect.right + gap + width > room
    ? Math.max(8, rect.left - gap - width)
    : rect.right + gap;
  const top = typeof window === 'undefined' ? rect.top
    : Math.min(Math.max(8, rect.top), Math.max(8, window.innerHeight - 220));
  return { left, top, maxHeight: '60vh', overflowY: 'auto' };
}

export function Menu({ id, label, glyph, hint, open, setOpen, children }: {
  id: 'mode' | 'approval' | 'folder' | 'model' | 'projects';
  label: string;
  glyph?: React.ReactNode;
  hint?: string;
  open: boolean;
  setOpen: (v: '' | 'mode' | 'approval' | 'folder' | 'model' | 'projects') => void;
  children: React.ReactNode;
}) {
  return (
    <div className="relative">
      <button type="button"
              onClick={() => setOpen(open ? '' : id)}
              aria-expanded={open}
              /* Bordered, not bare text. These three controls choose whether
                 the agent may write, whether it may run your code, and which
                 model spends your money — and rendered as plain grey words
                 they read as metadata, so nobody notices they are switches.
                 The open state is a ring as well as a background, because a
                 border colour alone is not visible enough at this size. */
              className={
                'flex h-7 items-center gap-1.5 rounded-md border px-2 text-[12px] '
                + (open
                  ? 'border-glass-border bg-white/10 text-zinc-50 ring-1 ring-white/20'
                  : 'border-glass-border text-zinc-200 hover:border-white/20 hover:bg-white/5')}>
        {glyph && <span aria-hidden className="text-zinc-muted">{glyph}</span>}
        <span className="max-w-[180px] truncate">{label}</span>
        <ChevronDown size={10} aria-hidden className="flex-none text-zinc-muted" />
      </button>
      {open && (
        <div role="menu"
             className="absolute bottom-full left-0 z-30 mb-1 w-[300px] overflow-hidden rounded-md border border-glass-border bg-surface-1 shadow-depth">
          {children}
        </div>
      )}
      {hint && !open && (
        <span className="sr-only">{hint}</span>
      )}
    </div>
  );
}

function MenuItem({ label, hint, active, onClick }: {
  label: string;
  hint?: string;
  active?: boolean;
  onClick: () => void;
}) {
  return (
    <button type="button" role="menuitem" onClick={onClick}
            className={'block w-full px-3 py-2 text-left hover:bg-white/5 '
              + (active ? 'text-zinc-100' : 'text-zinc-300')}>
      <span className="block text-[13px]">{label}</span>
      {hint && <span className="block text-[12px] leading-snug text-zinc-muted">{hint}</span>}
    </button>
  );
}
