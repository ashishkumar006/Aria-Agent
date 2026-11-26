import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, X } from 'lucide-react';
import { api, type CodeCheck, type CodeChecks, type CodeProblem, type StudioRoot, type StudioRun } from '../../api';
import TerminalDock from '../../components/TerminalDock';
import { checksSummary, checksToRows, type ProblemRow } from './Composer';

/** The studio dock: Terminal · Problems · Output · Checks.
 *
 *  Docked at the bottom of the workbench and never a modal, because all four
 *  are read WHILE something else is working — a dialog that owns the screen to
 *  show a build log is a dialog that has to be dismissed before the log can be
 *  read.
 *
 *  Three of the four panes are honest about their source, and that is the
 *  design decision worth stating:
 *
 *   - Terminal is a real PTY on the agent, started inside one of THIS
 *     session's folders. The server re-checks the path (`_terminal_cwd_ok`):
 *     a shell is the widest capability in the product, and letting the query
 *     string name any directory on the machine would have made the editor's
 *     confinement decorative.
 *   - Problems checks the text you are looking at — the UNSAVED buffer, through
 *     the same parser as Ctrl+S — not whatever happens to be on disk.
 *   - Output mirrors the commands the AGENT ran, read back from its transcript.
 *     It is not a copy of the model's tool clip: the run records the shell's
 *     own output, and says when it had to cut it.
 */

export type DockTab = 'terminal' | 'problems' | 'output' | 'checks';

/** The check route speaks language ids, not file extensions. Mapping them is
 *  the difference between "ast parsed, 1 problem" and a mode of "none" that
 *  the panel used to print as "syntax clean". */
const LANG_FOR: Record<string, string> = {
  py: 'python', ts: 'typescript', tsx: 'typescriptreact',
  js: 'javascript', jsx: 'javascript', json: 'json',
};

/** `text` means the route will answer mode `none` — no parser, not a clean
 *  file — and the caller that shows the caveat line has to know which. */
export function langForPath(p: string): string {
  const ext = /\.(py|tsx?|jsx?|json|md|toml|yaml|yml)$/.exec(p)?.[1] || '';
  return LANG_FOR[ext] || 'text';
}

/** What one buffer's check result lets us claim about it. */
export function checkWeakness(path: string,
                              r: CodeCheck | null | undefined): string {
  if (!r || r.checked !== true || r.mode === 'none')
    return `${path}: ${(r && r.note) || 'nothing here can check this file'}`;
  if (r.mode !== 'ast')
    return `${path}: ${r.mode === 'balance' ? 'delimiter heuristic'
              : 'no parser for this language'}`;
  return '';
}

interface Shell {
  id: number;
  /** Absolute path, inside a session root. Empty means "the server default". */
  cwd: string;
  label: string;
}

export interface DockProps {
  sid: string;
  roots: StudioRoot[];
  tab: DockTab;
  setTab: (t: DockTab) => void;
  open: boolean;
  setOpen: (v: boolean) => void;
  /** Paths with open buffers, and their text — what Problems actually checks. */
  buffers: Record<string, { text: string; dirty?: boolean }>;
  /** The session's runs, newest first — what Output replays. */
  runs: StudioRun[];
  /** Height of the dock body, owned by the shell so it survives a tab switch
   *  and can be remembered per window. */
  height: number;
  setHeight: (n: number) => void;
  onOpenPath?: (p: string) => void;
  /** Render as a right-hand column (OpenCode-style) instead of a bottom bar. */
  side?: boolean;
}

export function Dock({ sid, roots, tab, setTab, open, setOpen, buffers,
                       runs, height, setHeight, onOpenPath, side = false }: DockProps) {
  const [shells, setShells] = useState<Shell[]>([]);
  const [activeShell, setActiveShell] = useState(0);
  const [problems, setProblems] = useState<ProblemRow[]>([]);
  const [problemNote, setProblemNote] = useState('');
  const [output, setOutput] = useState<{ run: string; rows: OutRow[]; note: string }>(
    { run: '', rows: [], note: '' });
  const [checks, setChecks] = useState<CodeChecks | null>(null);
  const [checksBusy, setChecksBusy] = useState(false);
  const [checksErr, setChecksErr] = useState('');
  const seq = useRef(0);

  const addShell = useCallback((cwd: string, label: string) => {
    seq.current += 1;
    const id = seq.current;
    /* Two shells in the same folder both read "demo", which is not a label —
       the tab strip then shows "demo × demo ×" and there is no way to tell
       which prompt is which. Number the second one. */
    const same = shells.filter((s) => s.label === label
      || s.label.startsWith(`${label} `)).length;
    const name = same ? `${label} ${same + 1}` : label;
    setActiveShell(shells.length);
    setShells([...shells, { id, cwd, label: name }]);
    setOpen(true);
    setTab('terminal');
  }, [shells, setOpen, setTab]);

  /* One shell per folder the session holds, so a two-project session can run
     `pytest` in one and `npm test` in the other without either being a lie
     about where it ran. */
  useEffect(() => {
    setShells((all) => {
      const kept = all.filter((s) => !s.cwd || roots.some((r) => r.path === s.cwd));
      if (kept.length) return kept;
      if (!roots.length) return [];
      seq.current += 1;
      return [{ id: seq.current, cwd: roots[0].path, label: roots[0].name }];
    });
  }, [roots]);

  /* Problems: check every OPEN buffer, not the whole project. A real language
     server would do it continuously; this re-checks on a debounce after a
     change and says which mode answered, because a delimiter heuristic
     reporting "no problems" is not the same claim as a parser doing it.
     Keyed on a SAMPLED CONTENT signature, not on the list of open paths: keyed
     on paths alone, the panel goes on asserting "syntax clean" about a file you
     have since broken — measured live, typing `def bad(:` into an already-open
     buffer left Problems reporting nothing, because nothing had been opened or
     closed. Sampling both ends keeps a keystroke in a 1 MB file O(1), and the
     700 ms debounce below is what stops this parsing per character. */
  const sampleSig = (t: string) => {
    let h = 0;
    const feed = (s: string) => {
      for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
    };
    if (t.length <= 2048) feed(t);
    else { feed(t.slice(0, 1024)); feed(t.slice(-1024)); }
    return `${t.length}:${h}`;
  };
  const pathSig = Object.keys(buffers).sort()
    .map((p) => `${p}=${sampleSig(buffers[p]?.text || '')}`).join('|');
  useEffect(() => {
    const paths = Object.keys(buffers);
    if (!paths.length) { setProblems([]); setProblemNote(''); return undefined; }
    let live = true;
    const ctrl = new AbortController();
    const t = setTimeout(async () => {
      const rows: ProblemRow[] = [];
      const weaknesses: string[] = [];
      for (const p of paths.slice(0, 12)) {
        const text = buffers[p]?.text ?? '';
        if (!text.trim()) continue;
        try {
          const r = await api.codeCheck(text, langForPath(p), ctrl.signal);
          if (!live || ctrl.signal.aborted) return;
          for (const row of checkRows(p, r)) rows.push(row);
          const why = checkWeakness(p, r);
          if (why) weaknesses.push(why);
        } catch { /* an aborted or failed check simply reports nothing new */ }
      }
      if (!live) return;
      setProblems(rows);
      setProblemNote(checkCaveat(paths, weaknesses, rows.length));
    }, 700);
    return () => { live = false; clearTimeout(t); ctrl.abort(); };
    // pathSig already folds in the sampled text, which is what re-runs this
    // when a buffer changes without any tab opening or closing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathSig, sid]);

  /* Output: the commands this session's agent ran, straight out of the run's
     transcript. Replay is the only source that can be right after a reload. */
  const loadOutput = useCallback(async (runId: string) => {
    if (!runId) { setOutput({ run: '', rows: [], note: 'no run selected' }); return; }
    try {
      const got = await api.studioRunFrames(sid, runId);
      const rows = outputRowsFrom(got?.frames || []);
      const clipped = rows.filter((r) => r.clipped).length;
      setOutput({ run: runId, rows,
                  note: rows.length
                    ? `${rows.length} mirrored item(s) from run ${runId}`
                      + (clipped ? ` · ${clipped} output(s) cut at 20k chars` : '')
                    : 'this run ran no commands and edited no checks'});
    } catch (e) {
      setOutput({ run: runId, rows: [],
                  note: e instanceof Error ? e.message : 'the transcript failed' });
    }
  }, [sid]);

  const shownRun = runs[0]?.id || '';
  useEffect(() => { void loadOutput(shownRun); }, [shownRun, loadOutput]);

  /* Drag-to-size the dock. Anchored on where the grab started rather than on
     the last delta, so a fast pointer cannot accumulate drift into a panel
     that no longer matches the mouse. */
  const [drag, setDrag] = useState<{ y: number; h: number } | null>(null);
  const bound = useCallback((n: number) => Math.round(Math.max(110,
    Math.min(typeof window !== 'undefined' ? window.innerHeight - 200 : 900, n))), []);
  useEffect(() => {
    if (!drag) return undefined;
    const move = (ev: PointerEvent) => setHeight(bound(drag.h + (drag.y - ev.clientY)));
    const up = () => setDrag(null);
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
  }, [drag, bound, setHeight]);

  const runChecks = useCallback(async () => {
    setChecksBusy(true);
    setChecksErr('');
    try {
      const r = await api.codeChecks(undefined, undefined, sid);
      setChecks(r);
    } catch (e) {
      setChecksErr(e instanceof Error ? e.message : 'the checks failed to run');
    } finally {
      setChecksBusy(false);
    }
  }, [sid]);

  const tabs: { id: DockTab; label: string; count?: number }[] = useMemo(
    () => [
      { id: 'terminal', label: 'Terminal', count: shells.length },
      { id: 'problems', label: 'Problems', count: problems.length },
      { id: 'output', label: 'Output', count: output.rows.length },
      { id: 'checks', label: 'Checks' },
    ], [shells.length, problems.length, output.rows.length]);

  return (
    <section aria-label="Dock"
             className={'flex flex-col bg-void '
               + (side ? 'h-full w-full border-l border-glass-border'
                       : 'flex-none border-t border-glass-border')}>
      {open && (
        <div role="separator" aria-orientation="horizontal" tabIndex={0}
             aria-label="Dock height"
             title="Drag to resize · arrow keys too"
             onPointerDown={(e) => { e.preventDefault(); setDrag({ y: e.clientY, h: height }); }}
             onKeyDown={(e) => {
               if (e.key === 'ArrowUp') { e.preventDefault(); setHeight(bound(height + 24)); }
               if (e.key === 'ArrowDown') { e.preventDefault(); setHeight(bound(height - 24)); }
             }}
             className="h-[4px] flex-none cursor-row-resize bg-white/10 hover:bg-zinc-100" />
      )}
      <div className="hidden">
        {tabs.map((t) => (
          <button key={t.id} type="button"
                  onClick={() => { setTab(t.id); setOpen(true); }}
                  aria-selected={tab === t.id}
                  /* NOT uppercase. The all-caps-with-tracking treatment is this
                     console's idiom for a SECTION LABEL (Explorer, GitPanel,
                     the drawer headers); applied to four clickable tabs it
                     shouts where the controls beside it — "+ shell", "+ demo"
                     — whisper, and the strip reads as a heading rather than a
                     switcher. Same size as the buttons on its right now. */
                  className={'flex h-6 items-center gap-1 rounded-md border px-2 text-[12px] '
                    + (tab === t.id
                      ? 'border-glass-border bg-white/10 text-zinc-50'
                      : 'border-transparent text-zinc-muted hover:bg-white/5 hover:text-zinc-200')}>
            {t.label}
            {!!t.count && (
              <span className="rounded-full bg-white/10 px-1.5 text-[11px] tabular-nums text-zinc-300">
                {t.count}
              </span>
            )}
          </button>
        ))}
        <span className="min-w-0 flex-1" />
        {tab === 'terminal' && (
          <>
            <button type="button" onClick={() => addShell(roots[0]?.path || '',
                                                         roots[0]?.name || 'default')}
                    title="New shell in the first folder"
                    className="rounded-md border border-glass-border px-1.5 py-0.5 text-[11px] text-zinc-300 hover:bg-white/5">
              + shell
            </button>
            {roots.length > 1 && roots.map((r) => (
              <button key={r.path} type="button" onClick={() => addShell(r.path, r.name)}
                      title={`New shell in ${r.path}`}
                      className="rounded-md border border-glass-border px-1.5 py-0.5 text-[11px] text-zinc-300 hover:bg-white/5">
                + {r.name}
              </button>
            ))}
          </>
        )}
        {tab === 'checks' && (
          <button type="button" onClick={() => void runChecks()} disabled={checksBusy}
                  className="rounded-md border border-glass-border px-1.5 py-0.5 text-[11px] text-zinc-300 hover:bg-white/5 disabled:opacity-50">
            {checksBusy ? 'running…' : 'run checks'}
          </button>
        )}
        <button type="button" onClick={() => setOpen(!open)}
                aria-label={open ? 'Collapse the dock' : 'Expand the dock'}
                className="flex items-center justify-center rounded-md border border-glass-border px-1.5 py-0.5 text-zinc-300 hover:bg-white/5">
          {open ? <ChevronDown size={12} /> : <ChevronUp size={12} />}
        </button>
      </div>

      {tab === 'terminal' && shells.length > 1 && (
        <div className="flex flex-none items-center gap-1 border-t border-glass-border px-2 py-1">
          {shells.map((s, i) => (
            <span key={s.id}
                  className={'flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] '
                    + (i === activeShell
                      ? 'border-white/30 bg-white/[0.06] text-zinc-100'
                      : 'border-glass-border text-zinc-muted')}>
              <button type="button" onClick={() => setActiveShell(i)}
                      title={s.cwd || 'the server default folder'}>{s.label}</button>
              <button type="button" aria-label={`Close ${s.label}`}
                      onClick={() => setShells((all) => all.filter((x) => x.id !== s.id))}
                      className="text-zinc-muted hover:text-danger"><X size={11} /></button>
            </span>
          ))}
        </div>
      )}

      {open && (
        <div className="min-h-0 overflow-auto" style={{ height }}>
          {tab === 'terminal' && (
            shells.length ? (
              shells.map((s, i) => (
                /* Hidden rather than unmounted: a terminal's scrollback is the
                   reason it exists, and switching shells must not lose it. */
                <div key={s.id} className={i === activeShell ? 'h-full' : 'hidden h-full'}>
                  <TerminalDock bare open nonce={0} cwd={s.cwd} sid={sid} />
                </div>
              ))
            ) : (
              <p className="px-3 py-2 text-[12px] text-zinc-muted">
                this session holds no folder, so there is nowhere to start a shell.
              </p>
            )
          )}

          {tab === 'problems' && (
            <div className="px-3 py-2">
              <p className="mb-1 text-[12px] text-zinc-muted">{problemNote}</p>
              {problems.length === 0 ? (
                <p className="text-[12px] text-zinc-300">nothing reported.</p>
              ) : (
                <ul className="space-y-1">
                  {problems.map((p, i) => (
                    <li key={`${p.path}-${p.line}-${i}`}
                        className="flex min-w-0 items-baseline gap-2 text-[12px]">
                      <span className={p.severity === 'error'
                        ? 'flex-none text-danger' : 'flex-none text-warn'}>
                        {p.severity === 'error' ? 'x' : '!'}
                      </span>
                      <button type="button"
                              onClick={() => p.path && onOpenPath?.(p.path)}
                              className="flex-none max-w-[260px] truncate text-zinc-300 hover:text-zinc-400">
                        {p.path || 'file'}{p.line ? `:${p.line}` : ''}
                      </button>
                      <span className="min-w-0 flex-1 break-words text-zinc-muted">
                        {p.message}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {tab === 'output' && (
            <div className="px-3 py-2">
              <div className="mb-1 flex items-center gap-2">
                <p className="min-w-0 flex-1 truncate text-[12px] text-zinc-muted"
                   title={output.run}>{output.note}</p>
                {runs.length > 1 && (
                  <select aria-label="Which run's output"
                          value={output.run}
                          onChange={(e) => void loadOutput(e.target.value)}
                          className="rounded-md border border-glass-border bg-surface-1 px-1.5 py-0.5 text-[12px] text-zinc-200">
                    {runs.map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.id} · {(r.question || '').slice(0, 40)}
                      </option>
                    ))}
                  </select>
                )}
              </div>
              {output.rows.length === 0 ? (
                <p className="text-[12px] text-zinc-300">
                  no commands have been mirrored yet. The agent runs a command,
                  its output lands here.
                </p>
              ) : (
                <ul className="space-y-2">
                  {output.rows.map((r, i) => (
                    <li key={i} className="min-w-0">
                      <p className="flex items-baseline gap-2 text-[12px]">
                        <span className={r.ok ? 'text-ok' : 'text-danger'}>
                          {r.ok ? '✓' : 'x'}
                        </span>
                        <span className="min-w-0 flex-1 truncate font-mono text-zinc-200"
                              title={r.command}>{r.command || r.tool}</span>
                        <span className="flex-none text-[11px] text-zinc-muted">
                          {r.latency ? `${r.latency}s` : ''}
                          {r.clipped ? ` · cut at 20k of ${r.chars}` : ''}
                        </span>
                      </p>
                      {r.text && (
                        <pre className="mt-0.5 max-h-[140px] overflow-auto rounded-md border border-glass-border bg-surface-1 px-2 py-1 font-mono text-[12px] leading-snug text-zinc-300">
                          {r.text}
                        </pre>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {tab === 'checks' && (
            <div className="px-3 py-2">
              {checksErr && <p className="mb-1 text-[12px] text-danger">{checksErr}</p>}
              {!checks && !checksErr && !checksBusy && (
                <p className="text-[12px] text-zinc-300">
                  run checks to execute this project's own lint, typecheck and
                  tests. It runs repository code, so it is only ever a click
                  away, never automatic.
                </p>
              )}
              {checks && (
                <>
                  <p className={'mb-1 text-[12px] '
                    + (checks.ok ? 'text-ok' : 'text-warn')}>
                    {checksSummary(checks)}
                  </p>
                  <ul className="space-y-2">
                    {checks.results.map((one) => (
                      <li key={one.tool}>
                        <p className="text-[12px]">
                          <span className={one.available === false ? 'text-zinc-muted'
                            : one.ok ? 'text-ok' : 'text-danger'}>
                            {one.available === false ? '—' : one.ok ? '✓' : 'x'}
                          </span>
                          <span className="ml-1.5 font-mono text-zinc-200">{one.tool}</span>
                          {one.exit_code !== null && (
                            <span className="ml-1.5 text-[11px] text-zinc-muted">
                              exit {one.exit_code}
                            </span>
                          )}
                        </p>
                        <pre className="mt-0.5 max-h-[160px] overflow-auto rounded-md border border-glass-border bg-surface-1 px-2 py-1 font-mono text-[12px] leading-snug text-zinc-300">
                          {String(one.error || (one.ok ? one.stdout : one.stderr) || '').slice(0, 4000)
                           || '(no output)'}
                        </pre>
                      </li>
                    ))}
                  </ul>
                  {checksToRows(checks).length > 0 && (
                    <p className="mt-1 text-[12px] text-zinc-muted">
                      {checksToRows(checks).length} line(s) of this are attached to
                      the composer when you use #checks.
                    </p>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

interface OutRow {
  command: string;
  ok: boolean;
  text: string;
  clipped: boolean;
  chars: number;
  tool: string;
  latency: number;
}

/** A run's transcript frames into the dock's Output rows.
 *
 *  Replay is the only honest source: the same list has to be there after a
 *  reload, and the transcript is the one place the command's own output
 *  survives. Deltas and tool cards are deliberately NOT output lines — this
 *  pane answers "what ran and what did it print", not "what was said". */
export function outputRowsFrom(frames: Record<string, unknown>[]): OutRow[] {
  const rows: OutRow[] = [];
  for (const f of frames) {
    const kind = String(f.t || f.type || '');
    if (kind === 'output') {
      rows.push({ command: String(f.command || f.tool || ''),
                  ok: f.ok !== false, text: String(f.text || ''),
                  clipped: !!f.clipped, chars: Number(f.chars || 0),
                  tool: String(f.tool || ''), latency: Number(f.latency || 0) });
    } else if (kind === 'verify') {
      rows.push({ command: `verify ${f.checked ?? 0} file(s)`,
                  ok: !!f.ok, text: String(f.error || f.tail || ''),
                  clipped: false, chars: 0, tool: 'verify', latency: 0 });
    } else if (kind === 'status' && /not attached/.test(String(f.text || ''))) {
      rows.push({ command: 'context', ok: false, text: String(f.text || ''),
                  clipped: false, chars: 0, tool: 'context', latency: 0 });
    }
  }
  return rows;
}

/** One `/api/code/check` reply into problem rows, tagged with the mode that
 *  answered.
 *
 *  The mode travels with the message because "no problems" from a delimiter
 *  heuristic is a weaker claim than "no problems" from the Python parser, and
 *  a person deciding whether to trust the green has to be able to tell. */
export function checkRows(path: string, res: {
  mode?: string; checked?: boolean; problems?: CodeProblem[];
}): ProblemRow[] {
  const mode = res?.mode || 'none';
  return (res?.problems || []).map((p) => ({
    path, line: p.line, severity: p.severity,
    message: `[${mode}] ${p.message}`.slice(0, 220),
  }));
}

/** The caveat line for a batch of buffer checks. */
export function checkCaveat(paths: string[], weak: string[],
                            rows: number): string {
  if (!paths.length) return 'no file is open, so nothing has been checked';
  if (weak.length) {
    return `not a full type check — ${weak.slice(0, 4).join('; ')}`;
  }
  return rows ? `${rows} problem(s) in ${paths.length} open file(s)`
              : 'syntax clean for the open files (not a type check)';
}

