import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowUp, ChevronDown, FolderTree, GitBranch, Globe, Plus, TriangleAlert, X } from 'lucide-react';
import { api, type CodeChecks, type StudioCommand, type StudioMention } from '../../api';
import type { StudioRoot } from '../../api';
import { ModelPicker } from '../../components/ModelPicker';

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
    const text = String(one.error || (one.ok ? one.stdout : one.stderr) || '');
    const head = text.split('\n').map((s) => s.trim()).filter(Boolean);
    for (const line of head.slice(0, 4)) {
      rows.push({ message: `${one.tool}: ${line}`.slice(0, 220),
                  severity: one.ok ? 'info' : 'error' });
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
    bad.length ? `failed: ${bad.join(', ')}` : 'all runnable checks passed',
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
                           approval, setApproval, menu, setMenu, roots,
                           onAddFolder, branch, refreshBranch, running, send,
                           stop, composerRef, wide, sid, model, setModel,
                           onCommand, meta, problems, setProblems,
                           browserPage, setBrowserPage, refreshBrowser,
                           onPickFile }: {
  input: string;
  setInput: (v: string) => void;
  disabled: boolean;
  readOnly: boolean;
  setReadOnly: (v: boolean) => void;
  approval: 'manual' | 'files' | 'files+exec';
  setApproval: (v: 'manual' | 'files' | 'files+exec') => void;
  menu: '' | 'mode' | 'approval' | 'folder' | 'model';
  setMenu: (v: '' | 'mode' | 'approval' | 'folder' | 'model') => void;
  roots: StudioRoot[];
  onAddFolder: () => void;
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
}) {
  const [caret, setCaret] = useState(0);
  const [pick, setPick] = useState(0);
  const [dismissed, setDismissed] = useState('');
  const [mentions, setMentions] = useState<StudioMention[]>([]);
  const [mErr, setMErr] = useState('');
  const [commands, setCommands] = useState<StudioCommand[]>([]);
  const [checking, setChecking] = useState(false);
  const [checkNote, setCheckNote] = useState('');
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
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const cmd = /^\/([a-z-]+)([\s\S]*)$/i.exec(input.trim());
      if (cmd) { setInput(''); onCommand(cmd[1].toLowerCase(), (cmd[2] || '').trim()); return; }
      send();
    }
  };

  const modeLabel = readOnly ? 'Ask' : 'Agent';
  const approvalLabel = approval === 'manual' ? 'Manual'
    : approval === 'files' ? 'Auto files' : 'Auto files + commands';

  return (
    <div data-composer-menu=""
         className={'relative w-full overflow-hidden rounded-xl border border-edge bg-surface-1 '
                   + 'shadow-[0_1px_0_rgba(255,255,255,0.03)_inset] transition-colors '
                   + 'focus-within:border-accent/35'}>
      {/* The console's card signature: a 2px accent hairline fading out along
          the top edge. Authoring's BRIEF card and Skills' stat tiles both
          carry it, and it is the single detail that makes a surface read as
          part of Aria rather than parked beside it. */}
      <span aria-hidden
            className="pointer-events-none absolute inset-x-0 top-0 h-[2px]"
            style={{ background: 'linear-gradient(90deg, var(--color-accent) 0%, transparent 100%)',
                     opacity: 0.7 }} />
      {open && (
        <div role="listbox" aria-label="Completions"
             className="absolute bottom-full left-3 z-40 mb-1 w-[min(460px,90%)] overflow-hidden rounded-md border border-edge bg-surface-2 shadow-depth">
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
                  <span className="flex-none truncate text-[13px] text-zinc-100">{r.label}</span>
                  <span className="min-w-0 flex-1 truncate text-[12px] text-zinc-muted"
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
        rows={wide ? 3 : 2}
        disabled={disabled}
        placeholder={disabled
          ? 'open a folder first — this session has nowhere to work'
          : 'Send a message, @ to mention files, # for problems, or / for commands…'}
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
                  className="flex items-center gap-1 rounded-md border border-glass-border bg-surface-2 px-1.5 py-0.5 text-[12px] text-zinc-200">
              <button type="button" onClick={() => onPickFile(p)}
                      title={`Open ${p} in the editor`}
                      className="max-w-[220px] truncate hover:text-accent">{p}</button>
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
                  className="rounded-md border border-glass-border px-1.5 py-0.5 text-[12px] text-zinc-muted">
              hop {meta.hops ?? 0}/{meta.maxHops}
            </span>
          )}
          {!!meta.contextChars && (
            <span className="rounded-md border border-glass-border px-1.5 py-0.5 text-[12px] text-zinc-muted">
              {(meta.contextChars / 1000).toFixed(1)}k
              /{Math.round((meta.contextCap || 0) / 1000)}k ctx
            </span>
          )}
        </div>
      )}

      <div className="flex items-center gap-1.5 px-3 pb-2.5 pt-1.5">
        <button type="button"
                title="Mention a file (types @)"
                aria-label="Add file context"
                onClick={() => {
                  const next = input ? `${input} @` : '@';
                  setInput(next);
                  setCaret(next.length);
                  const el = composerRef.current;
                  if (el) requestAnimationFrame(() => {
                    el.focus();
                    el.setSelectionRange(next.length, next.length);
                  });
                }}
                className="flex h-7 w-7 flex-none items-center justify-center rounded-full border border-glass-border text-zinc-muted hover:bg-white/5">
          <Plus size={14} />
        </button>

        <Menu id="mode" label={modeLabel} open={menu === 'mode'} setOpen={setMenu}>
          <MenuItem onClick={() => { setReadOnly(false); setMenu(''); }}
                    active={!readOnly} label="Agent" hint="reads and edits files in this session's folders" />
          <MenuItem onClick={() => { setReadOnly(true); setMenu(''); }}
                    active={readOnly} label="Ask" hint="read-only: it can look, and it cannot write" />
        </Menu>

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
              open={menu === 'model'} setOpen={setMenu}>
          <div className="px-3 py-2">
            <ModelPicker model={model} onChange={setModel} busy={running}
                         label="the coding agent" />
          </div>
        </Menu>

        <span className="min-w-0 flex-1" />

        {running ? (
          <button type="button" onClick={stop}
                  className="flex-none rounded-md border border-glass-border px-2.5 py-1 text-[12px] text-zinc-200 hover:bg-white/5">
            stop
          </button>
        ) : (
          <button type="button" onClick={() => {
            const cmd = /^\/([a-z-]+)([\s\S]*)$/i.exec(input.trim());
            if (cmd) { setInput(''); onCommand(cmd[1].toLowerCase(), (cmd[2] || '').trim()); return; }
            send();
          }}
                  disabled={!input.trim() || disabled}
                  aria-label="Send"
                  className="flex h-8 w-8 flex-none items-center justify-center rounded-full bg-accent text-black transition disabled:opacity-40">
            <ArrowUp size={15} />
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1.5 border-t border-edge/70 bg-surface-2/30 px-3 py-2">
        <Menu id="folder" label={roots.length === 1 ? roots[0].name
                       : roots.length ? `${roots.length} folders` : 'no folder'}
              open={menu === 'folder'} setOpen={setMenu} glyph={<FolderTree size={12} />}>
          {roots.map((r) => (
            <div key={r.path}
                 className="flex items-center gap-2 px-3 py-1 text-[12px] text-zinc-300">
              <span className="min-w-0 flex-1 truncate" title={r.path}>{r.name}</span>
            </div>
          ))}
          <MenuItem onClick={() => { setMenu(''); onAddFolder(); }}
                    label="Open a folder in this session…" hint="" />
        </Menu>

        <button type="button" onClick={refreshBranch}
                title={branch === null ? 'Re-check git status'
                       : `git status: ${branch}`}
                className="flex items-center gap-1.5 rounded-md px-2 py-1 text-[12px] text-zinc-muted hover:bg-white/5">
          <GitBranch size={12} aria-hidden />
          {branch ?? 'checking…'}
        </button>
        {/* No machine chip: the sessions rail already names the machine in its
            own footer, and a static label in the composer reads as a control
            that does nothing. */}
      </div>
    </div>
  );
}

export function Menu({ id, label, glyph, hint, open, setOpen, children }: {
  id: 'mode' | 'approval' | 'folder' | 'model';
  label: string;
  glyph?: React.ReactNode;
  hint?: string;
  open: boolean;
  setOpen: (v: '' | 'mode' | 'approval' | 'folder' | 'model') => void;
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
                  ? 'border-accent/60 bg-accent/10 text-zinc-50 ring-1 ring-accent/40'
                  : 'border-glass-border text-zinc-200 hover:border-white/20 hover:bg-white/5')}>
        {glyph && <span aria-hidden className="text-zinc-muted">{glyph}</span>}
        <span className="max-w-[180px] truncate">{label}</span>
        <ChevronDown size={10} aria-hidden className="flex-none text-zinc-muted" />
      </button>
      {open && (
        <div role="menu"
             className="absolute bottom-full left-0 z-30 mb-1 w-[300px] overflow-hidden rounded-md border border-edge bg-surface-2 shadow-depth">
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
