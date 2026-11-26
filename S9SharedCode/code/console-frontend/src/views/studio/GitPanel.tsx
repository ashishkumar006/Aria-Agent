import { useCallback, useEffect, useMemo, useState } from 'react';
import { GitBranch, GitCommitHorizontal, PenLine, RefreshCw } from 'lucide-react';
import { api, type StudioGitPath, type StudioGitResult, type StudioRoot } from '../../api';
import { Label } from './chrome';
import { fileVisual } from './fileIcons';

/** One entry from `git worktree list --porcelain`. */
type Worktree = { path: string; branch: string; head: string };

function parseWorktrees(out: string): Worktree[] {
  const res: Worktree[] = [];
  let cur: Partial<Worktree> = {};
  for (const line of out.split('\n')) {
    if (line.startsWith('worktree ')) {
      if (cur.path) res.push({ path: cur.path, branch: cur.branch || '', head: cur.head || '' });
      cur = { path: line.slice(9).trim() };
    } else if (line.startsWith('HEAD ')) {
      cur.head = line.slice(5).trim();
    } else if (line.startsWith('branch ')) {
      cur.branch = line.slice(7).trim().replace(/^refs\/heads\//, '');
    } else if (line.trim() === 'detached') {
      cur.branch = '(detached)';
    }
  }
  if (cur.path) res.push({ path: cur.path, branch: cur.branch || '', head: cur.head || '' });
  return res;
}

/** The sibling folder for a new workspace: `<repo parent>/<repo>-<name>`.
 *  Kept beside the repo (never inside it) so git can check it out cleanly. */
function newWsPath(name: string, repoRoot: string): string {
  const norm = repoRoot.replace(/[\\/]+$/, '');
  const at = Math.max(norm.lastIndexOf('/'), norm.lastIndexOf('\\'));
  const parent = at >= 0 ? norm.slice(0, at) : norm;
  const base = at >= 0 ? norm.slice(at + 1) : norm;
  const sep = norm.includes('\\') ? '\\' : '/';
  return `${parent}${sep}${base}-${name}`;
}

/** The git panel: what the repository says, and the verbs Aria is willing to run.
 *
 *  This is a deliberate widening of what Aria may do to a repository, so the
 *  surface is a CLOSED list rather than a command box. The server owns that
 *  list (`studio_files.GIT_ACTIONS`) and refuses `push`, `reset`, `clean`,
 *  `rebase`, `merge` and `amend` outright — a button that publishes to a
 *  remote other people read, or deletes untracked files with no undo, does not
 *  belong in a chat-driven tool no matter how convenient it looks once.
 *
 *  Two rules the panel is built to keep:
 *   - anything that moves HEAD shows the diff it would commit FIRST, and only
 *     runs after the person confirms that exact diff (the server answers 428
 *     until then, so the panel cannot skip the step by accident);
 *   - every action is journalled by the server, so "what did Aria do to my
 *     repo" has an answer that does not depend on which tab was open.
 */
export function GitPanel({ sid, roots, onChanged }: {
  sid: string;
  roots: StudioRoot[];
  onChanged: () => void;
}) {
  const [rootIdx, setRootIdx] = useState(0);
  const [rows, setRows] = useState<StudioGitPath[]>([]);
  const [head, setHead] = useState('');
  const [branches, setBranches] = useState<string[]>([]);
  const [log, setLog] = useState<string[]>([]);
  const [staged, setStaged] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  /* "Not a git repository" is the one git answer that is a state rather than a
     failure: every fresh folder produces it. Matching the server's wording
     keeps this from having to guess at prose. */
  const isNoRepo = /not (inside a )?git repo/i.test(error);
  const [note, setNote] = useState('');
  const [message, setMessage] = useState('');
  /* The action awaiting confirmation, WITH the arguments that produced it.
     Carrying them is the point: confirming must run exactly the action whose
     diff is on screen, not whatever the checkboxes happen to say by then. */
  const [pending, setPending] = useState<{
    res: StudioGitResult; args: Record<string, unknown>;
  } | null>(null);
  const [newBranch, setNewBranch] = useState('');
  const [show, setShow] = useState<'changes' | 'log' | 'journal' | 'workspaces'>('changes');
  const [journal, setJournal] = useState<{ at: number; kind: string; text: string }[]>([]);
  const [worktrees, setWorktrees] = useState<Worktree[]>([]);
  const [newWs, setNewWs] = useState('');
  const [repoRoot, setRepoRoot] = useState('');

  const refresh = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const st = await api.studioGit(sid, 'status', { root: rootIdx });
      if (st.error) { setError(st.error); setRows([]); return; }
      setRows(st.paths || []);
      const out = String(st.output || '');
      const long = /On branch ([^\s]+)/.exec(out);
      const porcelain = /^##\s+([^\s.]+)/m.exec(out);
      setHead(long ? long[1] : porcelain ? porcelain[1] : out.split('\n')[0] || '');
      const [br, lg, wt] = await Promise.all([
        api.studioGit(sid, 'branches', { root: rootIdx }),
        api.studioGit(sid, 'log', { root: rootIdx }),
        api.studioGit(sid, 'worktrees', { root: rootIdx }),
      ]);
      setBranches(br.branches || []);
      setLog(lg.log || []);
      setWorktrees(parseWorktrees(String(wt.output || '')));
      setRepoRoot(wt.repo || '');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'git did not answer');
    } finally {
      setBusy(false);
    }
  }, [sid, rootIdx]);

  useEffect(() => { void refresh(); }, [refresh]);

  const loadJournal = useCallback(async () => {
    try {
      const got = await api.studioJournal(sid);
      setJournal((got.entries || []).map((r) => ({
        at: r.at, kind: String(r.kind || ''),
        text: r.kind === 'git'
          ? `${r.action} · ${r.summary || ''} · ${r.ok ? 'ok' : 'failed'}`
          : r.kind === 'replace'
            ? `replace ${r.files} file(s), ${r.occurrences} occurrence(s)`
            : `${r.ops} file operation(s)${r.refused ? `, ${r.refused} refused` : ''}`,
      })));
    } catch { setJournal([]); }
  }, [sid]);

  useEffect(() => { if (show === 'journal') void loadJournal(); }, [show, loadJournal]);

  const toggle = (p: string) => setStaged((s) => {
    const n = new Set(s);
    if (n.has(p)) n.delete(p); else n.add(p);
    return n;
  });

  const run = useCallback(async (action: string, args: Record<string, unknown>,
                                  confirm = false) => {
    setBusy(true);
    setError('');
    setNote('');
    try {
      const r = await api.studioGit(sid, action, args, confirm);
      if (r.confirm_required) { setPending({ res: r, args }); setBusy(false); return; }
      if (r.error) { setError(r.error); setBusy(false); return; }
      setPending(null);
      /* git puts its explanation on stdout for the cases that matter most
         ("no changes added to commit", "nothing to commit"), so showing only
         stderr would render a refused action as `exit 1` with no reason. */
      setNote(`${r.summary || action} · exit ${r.exit_code ?? 0}`
              + (String(r.output || '').trim() ? `\n${String(r.output).slice(0, 1500)}` : '')
              + (String(r.stderr || '').trim() ? `\n${String(r.stderr).slice(0, 600)}` : '')
              + (r.ok ? '' : '  ← git refused this action'));
      void refresh();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'the action failed');
    } finally {
      setBusy(false);
    }
  }, [sid, refresh, onChanged]);

  const picked = useMemo(() => Array.from(staged), [staged]);
  /* Draft a message from what is actually staged. Same generator the composer
     runs, same honesty: it names the diff it read, and it says which hunks
     were left out of the model's view. Nothing is committed by drafting. */
  const draft = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const r = await api.studioCommitMsg(sid, true);
      if (r.error) { setError(r.error); return; }
      setMessage(String(r.text || ''));
      setNote(`drafted from the ${r.staged ? 'STAGED' : 'WORKING'} diff · `
              + `${r.files?.length ?? 0} file(s)`
              + (r.truncated ? ` · ${r.omitted?.length ?? 0} with hunks left out` : '')
              + '\nnothing has been committed.');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'the draft failed');
    } finally {
      setBusy(false);
    }
  }, [sid]);

  const unstaged = rows.filter((r) => !r.staged && !r.untracked);
  const untracked = rows.filter((r) => r.untracked);
  const stagedRows = rows.filter((r) => r.staged);

  /* A folder with no repository is a STATE, not a failed repo. The old panel
     said so politely and then still rendered commit / branch / stash controls
     underneath, which reads as "the tool is broken". For this state the git
     panel is just the empty state and a way to re-check the folder. */
  if (isNoRepo) {
    return (
      <div className="flex min-h-0 flex-1 flex-col text-[12px]">
        <div className="flex flex-none items-center gap-2 border-b border-glass-border px-3 py-2">
          <GitBranch size={13} aria-hidden className="flex-none text-zinc-500" />
          <span className="min-w-0 flex-1 truncate text-[12px] text-zinc-200">
            {roots.length === 1 ? roots[0].name : 'This folder'}
          </span>
          <button type="button" onClick={() => void refresh()} disabled={busy}
                  aria-label="Re-read the repository" title="Re-read the repository"
                  className="flex h-[26px] w-[26px] flex-none items-center justify-center rounded-md border border-glass-border text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-100 disabled:opacity-40">
            <RefreshCw size={12} />
          </button>
        </div>
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
          <p className="text-[13px] font-medium text-zinc-200">Not a git repository</p>
          <p className="max-w-[42ch] text-[11.5px] leading-relaxed text-zinc-muted">
            Ask the agent to run <span className="font-mono text-zinc-400">git init</span>,
            or run it in the terminal. History, diff and commit controls appear
            here once this folder is tracked.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col text-[12px]">
      {/* Two rows, not one: a folder picker, a branch name, three view tabs and
          a refresh button cannot share 248px — the branch was being squeezed
          to zero width and the refresh button hung 10px past the drawer's
          edge. The console's own card heads are a label row over a control
          row, so the git header follows that shape. */}
      <div className="flex flex-none flex-col gap-1.5 border-b border-glass-border px-2 py-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="flex min-w-0 flex-1 items-center gap-1.5 rounded-md border border-glass-border bg-black/40 px-2 py-1"
                title={head ? `On branch ${head}` : 'no branch'}>
            <GitBranch size={11} aria-hidden className="flex-none text-zinc-300" />
            <span className="min-w-0 truncate font-mono text-[11px] text-zinc-200">{head || '…'}</span>
          </span>
          {roots.length > 1 && (
            <select value={rootIdx} onChange={(e) => setRootIdx(Number(e.target.value))}
                    aria-label="Which folder's repository"
                    className="max-w-[92px] flex-none rounded-md border border-glass-border bg-black/40 px-1 py-1 text-[11px] text-zinc-300 outline-none">
              {roots.map((r, i) => <option key={r.path} value={i}>{r.name}</option>)}
            </select>
          )}
          <button type="button" onClick={() => void refresh()} disabled={busy}
                  aria-label="Re-read the repository"
                  title="Re-read the repository"
                  className="flex h-[26px] w-[26px] flex-none items-center justify-center rounded-md border border-glass-border text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-100 disabled:opacity-40">
            <RefreshCw size={12} />
          </button>
        </div>
        <div className="flex flex-none items-center gap-0.5 rounded-md border border-glass-border bg-black/40 p-0.5" role="tablist" aria-label="Git view">
          {(['changes', 'log', 'journal', 'workspaces'] as const).map((k) => (
            <button key={k} type="button" onClick={() => setShow(k)}
                    role="tab" aria-selected={show === k}
                    className={'flex-1 rounded-[6px] px-2 py-1 text-[11px] font-medium capitalize transition-colors '
                      + (show === k
                        ? 'bg-surface-3 text-zinc-100 shadow-sm ring-1 ring-white/10'
                        : 'text-zinc-500 hover:text-zinc-200')}>
              {k}
            </button>
          ))}
        </div>
      </div>

      {error && !isNoRepo && (
        <p role="alert" className="flex-none border-b border-danger/30 bg-danger-dim px-2 py-1 text-[12px] text-danger">
          {error}
        </p>
      )}
      {isNoRepo && (
        /* A folder that is not a repository is not a failure — it is a state,
           and the red alert made a slide-over with nothing else in it look
           broken. Say what is true and what to do, in the panel's own voice. */
        <div className="flex-none border-b border-glass-border bg-surface-0 px-3 py-3">
          <p className="text-[12px] text-zinc-300">
            {roots.length === 1 ? roots[0].name : 'This folder'} is not a git repository.
          </p>
          <p className="mt-1 text-[11px] leading-relaxed text-zinc-muted">
            Ask the agent to run <span className="font-mono text-zinc-400">git init</span>,
            or run it in the terminal. Until then there is no history, diff or
            commit to show here.
          </p>
        </div>
      )}
      {note && (
        <p className="flex-none whitespace-pre-wrap border-b border-glass-border bg-surface-0 px-2 py-1 text-[12px] text-zinc-300">
          {note}
        </p>
      )}

      {pending && (
        /* The confirmation carries the diff, because approving a summary is
           approving words rather than the change. */
        <div className="flex-none border-b border-warn/40 bg-warn-dim p-2">
          <p className="text-[12px] text-warn">
            {pending.res.summary} — this moves HEAD or discards work.
          </p>
          {pending.res.preview?.diff?.output && (
            <pre className="mt-1 max-h-[180px] overflow-auto rounded-md border border-glass-border bg-void p-1.5 text-[11px] leading-snug text-zinc-300">
              {pending.res.preview.diff.output.slice(0, 6000)}
            </pre>
          )}
          <div className="mt-1.5 flex gap-2">
            <button type="button"
                    onClick={() => void run(String(pending.res.action), pending.args, true)}
                    className="rounded-md border border-warn/50 bg-warn/15 px-2 py-0.5 text-[12px] text-warn hover:bg-warn/25">
              Confirm
            </button>
            <button type="button" onClick={() => setPending(null)}
                    className="rounded-md border border-glass-border px-2 py-0.5 text-[12px] text-zinc-300 hover:bg-white/5">
              Cancel
            </button>
          </div>
        </div>
      )}

      {show === 'changes' && (
        <div className="min-h-0 flex-1 overflow-auto">
          {!rows.length && !busy && (
            <p className="px-3 py-2 text-[12px] text-zinc-muted">
              the repository reports no changes.
            </p>
          )}
          {stagedRows.length > 0 && (
            <Section title={`staged (${stagedRows.length})`}>
              {stagedRows.map((r) => (
                <Row key={r.path} row={r} onToggle={() => void run('unstage', { paths: [r.path] })}
                     label="unstage" checked={false} />
              ))}
            </Section>
          )}
          {(unstaged.length > 0 || untracked.length > 0) && (
            <Section title="changes">
              {[...unstaged, ...untracked].map((r) => (
                <Row key={r.path} row={r} checked={picked.includes(r.path)}
                     onToggle={() => toggle(r.path)} label="select" />
              ))}
            </Section>
          )}
          {/* Three groups, each with one job. This used to be nine buttons in
              one undifferentiated column — commit, branch and destructive
              actions all the same weight, all adjacent — and the message field
              was squeezed to "commit messag" by the row it shared with them. */}
          <div className="border-t border-glass-border bg-surface-1/50 px-2.5 pb-2 pt-2.5">
            <GroupTitle>Commit</GroupTitle>
            <div className="relative">
              <input value={message} onChange={(e) => setMessage(e.target.value)}
                     placeholder="Commit message" aria-label="Commit message"
                     autoComplete="off" spellCheck={false}
                     className="w-full rounded-md border border-glass-border bg-black/40 py-1.5 pl-2 pr-8 text-[12px] text-zinc-100 outline-none transition-shadow placeholder:text-zinc-600 focus:border-white/30 focus:ring-1 focus:ring-white/20" />
              <button type="button" onClick={() => void draft()} disabled={busy}
                      title="Draft a message from the staged diff"
                      aria-label="Draft a message from the staged diff"
                      className="absolute right-1 top-1 flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-300 disabled:opacity-40">
                <PenLine size={12} />
              </button>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <button type="button" disabled={!picked.length || busy}
                      onClick={() => void run('stage', { paths: picked })}
                      className="rounded-md border border-glass-border px-2 py-1 text-[12px] text-zinc-200 transition-colors hover:bg-white/5 disabled:opacity-40">
                stage{picked.length ? ` ${picked.length}` : ''}
              </button>
              <button type="button" disabled={!stagedRows.length || !message.trim() || busy}
                      onClick={() => void run('commit',
                                              { paths: stagedRows.map((r) => r.path), message })}
                      className="flex items-center gap-1.5 rounded-md bg-zinc-100 px-2.5 py-1 text-xs font-bold text-[#161616] transition-colors hover:brightness-110 disabled:opacity-40">
                <GitCommitHorizontal size={12} />
                {stagedRows.length ? `commit ${stagedRows.length} staged` : 'commit staged'}
              </button>
            </div>
          </div>
          <div className="border-t border-glass-border px-2.5 pb-2 pt-2.5">
            <GroupTitle>Branch</GroupTitle>
            <div className="flex flex-wrap items-center gap-1.5">
              <select aria-label="Switch branch" value=""
                      disabled={!branches.length || busy}
                      onChange={(e) => e.target.value && void run('branch_switch', { name: e.target.value })}
                      className="min-w-0 max-w-[190px] flex-1 rounded-md border border-glass-border bg-black/40 px-1.5 py-1 text-[12px] text-zinc-300 outline-none transition-shadow focus:border-white/30 focus:ring-1 focus:ring-white/20">
                <option value="">switch branch…</option>
                {branches.map((b) => <option key={b} value={b}>{b}</option>)}
              </select>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <input value={newBranch} onChange={(e) => setNewBranch(e.target.value)}
                     placeholder="new-branch-name" aria-label="Branch name"
                     autoComplete="off" spellCheck={false}
                     className="min-w-0 flex-1 rounded-md border border-glass-border bg-black/40 px-2 py-1 text-[12px] text-zinc-100 outline-none transition-shadow placeholder:text-zinc-600 focus:border-white/30 focus:ring-1 focus:ring-white/20" />
              <button type="button" disabled={!newBranch.trim() || busy}
                      onClick={() => { void run('branch_create', { name: newBranch.trim() }); setNewBranch(''); }}
                      className="rounded-md border border-glass-border px-2 py-1 text-[12px] text-zinc-200 transition-colors hover:bg-white/5 disabled:opacity-40">
                create
              </button>
            </div>
          </div>
          <div className="border-t border-glass-border px-2.5 pb-1.5 pt-2.5">
            <GroupTitle>Set aside</GroupTitle>
            <div className="flex flex-wrap items-center gap-1.5">
              <button type="button" disabled={busy}
                      onClick={() => void run('stash', {})}
                      title="Put the work in progress aside (undo with stash pop)"
                      className="rounded-md border border-glass-border px-2 py-1 text-[12px] hover:bg-white/5">
                stash
              </button>
              <button type="button" disabled={busy}
                      onClick={() => void run('stash_pop', {})}
                      className="rounded-md border border-glass-border px-2 py-1 text-[12px] hover:bg-white/5 disabled:opacity-40">
                stash pop
              </button>
              <button type="button" disabled={!picked.length || busy}
                      onClick={() => void run('restore', { paths: picked })}
                      title="Discard the changes in the selected files"
                      className="ml-auto rounded-md border border-danger/40 bg-danger-dim px-2 py-1 text-[12px] text-danger hover:bg-danger/20 disabled:opacity-40">
                {picked.length ? `discard ${picked.length}` : 'discard selected'}
              </button>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-zinc-muted">
              Push, reset, clean, rebase, merge and amend are refused by the
              server — not hidden here.
            </p>
          </div>
        </div>
      )}

      {show === 'log' && (
        <ul className="min-h-0 flex-1 overflow-auto px-3 py-2.5">
          {log.map((line, i) => {
            const m = /^([0-9a-f]{7,})\s+(.*)$/.exec(line.trim());
            const hash = m?.[1] ?? '';
            const msg = m?.[2] ?? line;
            return (
              <li key={i} className="relative grid grid-cols-[12px_1fr] gap-2 pb-3 last:pb-1">
                {i < log.length - 1 && (
                  <span aria-hidden
                        className="absolute left-[5px] top-3 h-[calc(100%-10px)] w-px bg-white/10" />
                )}
                <span aria-hidden
                      className="mt-1 h-2 w-2 rounded-full border border-white/30 bg-zinc-100/25" />
                <span className="min-w-0 text-[11.5px] leading-snug">
                  {hash && <span className="mr-2 font-mono text-[10.5px] text-zinc-300">{hash}</span>}
                  <span className="text-zinc-300" title={line}>{msg}</span>
                </span>
              </li>
            );
          })}
          {!log.length && (
            <li className="py-2 text-[12px] text-zinc-muted">no commits to show.</li>
          )}
        </ul>
      )}

      {show === 'journal' && (
        <ul className="min-h-0 flex-1 divide-y divide-white/5 overflow-auto">
          {journal.map((r, i) => (
            <li key={i} className="grid grid-cols-[46px_1fr] gap-2 px-3 py-2">
              <time className="pt-0.5 font-mono text-[10px] tabular-nums text-zinc-600">
                {new Date(r.at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
              </time>
              <span className="min-w-0 text-[11.5px] leading-snug text-zinc-300">
                <span className="mr-1.5 inline-block rounded border border-glass-border bg-white/5 px-1 py-px align-[1px] font-mono text-[8.5px] font-bold uppercase tracking-[0.08em] text-zinc-500">{r.kind}</span>
                {r.text}
              </span>
            </li>
          ))}
          {!journal.length && (
            <li className="px-3 py-2 text-[12px] text-zinc-muted">
              nothing has been journalled in this session.
            </li>
          )}
        </ul>
      )}

      {show === 'workspaces' && (
        <div className="min-h-0 flex-1 overflow-auto">
          <div className="border-b border-glass-border p-2.5">
            <GroupTitle>New workspace</GroupTitle>
            <div className="flex items-center gap-1.5">
              <input value={newWs} onChange={(e) => setNewWs(e.target.value)}
                     placeholder="feature-x" aria-label="Workspace name"
                     autoComplete="off" spellCheck={false}
                     className="min-w-0 flex-1 rounded-md border border-glass-border bg-black/40 px-2 py-1 text-[12px] text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-white/30" />
              <button type="button" disabled={!newWs.trim() || busy || !repoRoot}
                      onClick={() => {
                        const name = newWs.trim().replace(/^opencode\//, '');
                        const path = newWsPath(name, repoRoot);
                        setNewWs('');
                        void run('worktree_add', { name, path, root: rootIdx });
                      }}
                      className="rounded-md bg-zinc-100 px-2.5 py-1 text-xs font-bold text-[#161616] transition-colors hover:brightness-110 disabled:opacity-40">
                create
              </button>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-zinc-muted">
              A workspace is an isolated git worktree on branch
              <span className="font-mono text-zinc-400"> opencode/&lt;name&gt;</span>, in a
              sibling folder. Each can run its own session without touching the others.
            </p>
          </div>
          <ul className="divide-y divide-white/5">
            {worktrees.map((w, i) => {
              const main = i === 0;
              return (
                <li key={w.path} className="flex min-w-0 items-center gap-2 px-3 py-2">
                  <GitBranch size={12} aria-hidden
                             className={main ? 'flex-none text-zinc-500' : 'flex-none text-zinc-300'} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-mono text-[11.5px] text-zinc-200" title={w.path}>
                      {w.path}
                    </div>
                    <div className="truncate text-[10.5px] text-zinc-500">
                      {main ? 'main worktree' : w.branch || '(detached)'}
                    </div>
                  </div>
                  {!main && (
                    <button type="button" disabled={busy}
                            onClick={() => void run('worktree_remove', { path: w.path, root: rootIdx })}
                            className="flex-none rounded-md border border-danger/40 bg-danger-dim px-2 py-0.5 text-[11px] text-danger hover:bg-danger/20 disabled:opacity-40">
                      remove
                    </button>
                  )}
                </li>
              );
            })}
            {!worktrees.length && (
              <li className="px-3 py-2 text-[12px] text-zinc-muted">
                no worktrees reported — is this folder a git repository?
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-glass-border">
      <p className="flex items-center gap-2 px-3 pb-1 pt-2.5">
        <Label>{title}</Label>
        <span aria-hidden className="h-px min-w-4 flex-1 bg-white/5" />
      </p>
      <ul className="pb-1.5">{children}</ul>
    </div>
  );
}

/* A named block of actions. Without it nine buttons read as one pile, and the
   only way to know what `stash` costs you is to hover it. */
function GroupTitle({ children }: { children: React.ReactNode }) {
  return (
    <p className="flex items-center gap-2 pb-1.5">
      <Label>{children}</Label>
      <span aria-hidden className="h-px min-w-4 flex-1 bg-white/5" />
    </p>
  );
}

/* The SCM row, as every editor built after VS Code draws it: the directory in
   muted grey, the file name in its status colour, and one plain status letter —
   no chip, no border, the way VS Code's source-control tree reads. */
function Row({ row, checked, onToggle, label }: {
  row: StudioGitPath; checked: boolean; onToggle: () => void; label: string;
}) {
  const flag = row.unmerged ? 'U' : row.deleted ? 'D' : row.untracked ? 'A' : 'M';
  const tone = row.unmerged || row.deleted ? 'text-rose-400'
    : row.untracked ? 'text-emerald-400'
      : row.staged ? 'text-sky-400'
        : 'text-amber-400';
  const flagTitle = row.unmerged ? 'conflicted'
    : row.deleted ? 'deleted'
      : row.untracked ? 'added (untracked)'
        : row.staged ? 'staged' : 'modified';
  const v = fileVisual(row.path);
  const FIcon = v.icon;
  const cut = row.path.lastIndexOf('/');
  const dir = cut < 0 ? '' : row.path.slice(0, cut + 1);
  const base = cut < 0 ? row.path : row.path.slice(cut + 1);
  return (
    <li className="flex min-w-0 items-center gap-2 px-3 py-[3px] transition-colors hover:bg-white/[0.04]">
      <input type="checkbox" checked={checked} onChange={onToggle}
             aria-label={`${label} ${row.path}`}
             className="h-3.5 w-3.5 flex-none cursor-pointer accent-zinc-300" />
      <FIcon size={12} strokeWidth={1.5} color={v.cls} className="flex-none" />
      <span className="min-w-0 flex-1 truncate font-mono text-[11.5px]" title={row.path}>
        <span className="text-zinc-500">{dir}</span>
        <span className={tone}>{base}</span>
      </span>
      <span aria-hidden title={flagTitle}
            className={`w-3 flex-none text-center font-mono text-[10px] font-bold leading-none ${tone}`}>
        {flag}
      </span>
    </li>
  );
}
