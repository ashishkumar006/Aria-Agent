import { useCallback, useEffect, useMemo, useState } from 'react';
import { GitBranch, PenLine, RefreshCw } from 'lucide-react';
import { api, type StudioGitPath, type StudioGitResult, type StudioRoot } from '../../api';
import { Label } from './chrome';
import { Pill } from '../../components/ui';

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
  const [note, setNote] = useState('');
  const [message, setMessage] = useState('');
  /* The action awaiting confirmation, WITH the arguments that produced it.
     Carrying them is the point: confirming must run exactly the action whose
     diff is on screen, not whatever the checkboxes happen to say by then. */
  const [pending, setPending] = useState<{
    res: StudioGitResult; args: Record<string, unknown>;
  } | null>(null);
  const [newBranch, setNewBranch] = useState('');
  const [show, setShow] = useState<'changes' | 'log' | 'journal'>('changes');
  const [journal, setJournal] = useState<{ at: number; kind: string; text: string }[]>([]);

  const refresh = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const st = await api.studioGit(sid, 'status', { root: rootIdx });
      if (st.error) { setError(st.error); setRows([]); return; }
      setRows(st.paths || []);
      const m = /On branch ([^\s]+)/.exec(String(st.output || ''));
      setHead(m ? m[1] : String(st.output || '').split('\n')[0] || '');
      const [br, lg] = await Promise.all([
        api.studioGit(sid, 'branches', { root: rootIdx }),
        api.studioGit(sid, 'log', { root: rootIdx }),
      ]);
      setBranches(br.branches || []);
      setLog(lg.log || []);
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

  return (
    <div className="flex min-h-0 flex-1 flex-col text-[12px]">
      <div className="flex flex-none items-center gap-1 border-b border-glass-border px-2 py-1.5">
        {roots.length > 1 && (
          <select value={rootIdx} onChange={(e) => setRootIdx(Number(e.target.value))}
                  aria-label="Which folder's repository"
                  className="rounded-lg border border-glass-border bg-surface-1 px-1 py-0.5 text-[12px]">
            {roots.map((r, i) => <option key={r.path} value={i}>{r.name}</option>)}
          </select>
        )}
        <span className="flex min-w-0 flex-1 items-center gap-1.5 truncate text-zinc-200" title={head}>
          <GitBranch size={12} aria-hidden className="flex-none text-zinc-muted" />
          <span className="min-w-0 truncate">{head || '…'}</span>
        </span>
        {(['changes', 'log', 'journal'] as const).map((k) => (
          <button key={k} type="button" onClick={() => setShow(k)}
                  className={'rounded-md px-1.5 py-0.5 '
                    + (show === k ? 'bg-white/10 text-zinc-100' : 'text-zinc-muted hover:bg-white/5')}>
            {k}
          </button>
        ))}
        <button type="button" onClick={() => void refresh()} disabled={busy}
                aria-label="Re-read the repository"
                className="flex items-center justify-center rounded-lg border border-glass-border px-1.5 py-0.5 text-zinc-muted hover:bg-white/5 hover:text-zinc-100 disabled:opacity-40">
          <RefreshCw size={12} />
        </button>
      </div>

      {error && (
        <p role="alert" className="flex-none border-b border-danger/30 bg-danger-dim px-2 py-1 text-[12px] text-danger">
          {error}
        </p>
      )}
      {note && (
        <p className="flex-none whitespace-pre-wrap border-b border-glass-border bg-surface-1/60 px-2 py-1 text-[12px] text-zinc-300">
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
            <pre className="mt-1 max-h-[180px] overflow-auto rounded-md border border-glass-border bg-surface-0 p-1.5 text-[11px] leading-snug text-zinc-300">
              {pending.res.preview.diff.output.slice(0, 6000)}
            </pre>
          )}
          <div className="mt-1.5 flex gap-2">
            <button type="button"
                    onClick={() => void run(String(pending.res.action), pending.args, true)}
                    className="rounded-lg border border-warn/50 bg-warn/15 px-2 py-0.5 text-[12px] text-warn hover:bg-warn/25">
              Confirm
            </button>
            <button type="button" onClick={() => setPending(null)}
                    className="rounded-lg border border-glass-border px-2 py-0.5 text-[12px] text-zinc-300 hover:bg-white/5">
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
          <div className="border-t border-glass-border px-2 pb-2 pt-2">
            <GroupTitle>Commit</GroupTitle>
            <input value={message} onChange={(e) => setMessage(e.target.value)}
                   placeholder="Commit message" aria-label="Commit message"
                   className="w-full rounded-md border border-glass-border bg-surface-1 px-2 py-1 text-[12px] outline-none" />
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <button type="button" disabled={!picked.length || busy}
                      onClick={() => void run('stage', { paths: picked })}
                      className="rounded-lg border border-glass-border px-2 py-1 text-[12px] hover:bg-white/5 disabled:opacity-40">
                stage{picked.length ? ` ${picked.length}` : ''}
              </button>
              <button type="button" onClick={() => void draft()} disabled={busy}
                      title="Draft a message from the staged diff"
                      className="flex items-center gap-1 rounded-lg border border-glass-border px-2 py-1 text-[12px] text-zinc-muted hover:bg-white/5 disabled:opacity-40">
                <PenLine size={12} /> draft
              </button>
              <button type="button" disabled={!stagedRows.length || !message.trim() || busy}
                      onClick={() => void run('commit',
                                              { paths: stagedRows.map((r) => r.path), message })}
                      className="rounded-lg bg-accent px-2.5 py-1 text-[12px] font-semibold text-black transition-colors hover:bg-accent/90 disabled:opacity-40">
                {stagedRows.length ? `commit ${stagedRows.length} staged` : 'commit staged'}
              </button>
            </div>
          </div>
          <div className="border-t border-glass-border px-2 pb-2 pt-2">
            <GroupTitle>Branch</GroupTitle>
            <div className="flex flex-wrap items-center gap-1.5">
              <select aria-label="Switch branch" value=""
                      disabled={!branches.length || busy}
                      onChange={(e) => e.target.value && void run('branch_switch', { name: e.target.value })}
                      className="min-w-0 max-w-[180px] flex-1 rounded-md border border-glass-border bg-surface-1 px-1.5 py-1 text-[12px]">
                <option value="">switch branch…</option>
                {branches.map((b) => <option key={b} value={b}>{b}</option>)}
              </select>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <input value={newBranch} onChange={(e) => setNewBranch(e.target.value)}
                     placeholder="new-branch-name" aria-label="Branch name"
                     className="min-w-0 flex-1 rounded-md border border-glass-border bg-surface-1 px-2 py-1 text-[12px] outline-none" />
              <button type="button" disabled={!newBranch.trim() || busy}
                      onClick={() => { void run('branch_create', { name: newBranch.trim() }); setNewBranch(''); }}
                      className="rounded-lg border border-glass-border px-2 py-1 text-[12px] hover:bg-white/5 disabled:opacity-40">
                create
              </button>
            </div>
          </div>
          <div className="border-t border-glass-border px-2 pb-1.5 pt-2">
            <GroupTitle>Set aside</GroupTitle>
            <div className="flex flex-wrap items-center gap-1.5">
              <button type="button" disabled={busy}
                      onClick={() => void run('stash', {})}
                      title="Put the work in progress aside (undo with stash pop)"
                      className="rounded-lg border border-glass-border px-2 py-1 text-[12px] hover:bg-white/5">
                stash
              </button>
              <button type="button" disabled={busy}
                      onClick={() => void run('stash_pop', {})}
                      className="rounded-lg border border-glass-border px-2 py-1 text-[12px] hover:bg-white/5 disabled:opacity-40">
                stash pop
              </button>
              <button type="button" disabled={!picked.length || busy}
                      onClick={() => void run('restore', { paths: picked })}
                      title="Discard the changes in the selected files"
                      className="ml-auto rounded-lg border border-danger/40 bg-danger-dim px-2 py-1 text-[12px] text-danger hover:bg-danger/20 disabled:opacity-40">
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
        <ul className="min-h-0 flex-1 overflow-auto divide-y divide-glass-border">
          {log.map((line, i) => (
            <li key={i} className="truncate px-3 py-1 font-mono text-[12px] text-zinc-300"
                title={line}>{line}</li>
          ))}
          {!log.length && (
            <li className="px-3 py-2 text-[12px] text-zinc-muted">no commits to show.</li>
          )}
        </ul>
      )}

      {show === 'journal' && (
        <ul className="min-h-0 flex-1 overflow-auto divide-y divide-glass-border">
          {journal.map((r, i) => (
            <li key={i} className="px-3 py-1.5 text-[12px] text-zinc-300">
              <span className="text-zinc-muted">
                {new Date(r.at * 1000).toLocaleTimeString()} · {r.kind}
              </span>
              <br />
              {r.text}
            </li>
          ))}
          {!journal.length && (
            <li className="px-3 py-2 text-[12px] text-zinc-muted">
              nothing has been journalled in this session.
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-edge">
      <p className="px-3 py-2"><Label>{title}</Label></p>
      <ul>{children}</ul>
    </div>
  );
}

/* A named block of actions. Without it nine buttons read as one pile, and the
   only way to know what `stash` costs you is to hover it. */
function GroupTitle({ children }: { children: React.ReactNode }) {
  return (
    <p className="pb-2"><Label>{children}</Label></p>
  );
}

function Row({ row, checked, onToggle, label }: {
  row: StudioGitPath; checked: boolean; onToggle: () => void; label: string;
}) {
  const flag = row.unmerged ? 'conflict' : row.deleted ? 'deleted'
    : row.untracked ? 'new' : row.staged ? 'staged' : 'modified';
  return (
    <li className="flex min-w-0 items-center gap-2 px-3 py-0.5 hover:bg-white/[0.04]">
      <input type="checkbox" checked={checked} onChange={onToggle}
             aria-label={`${label} ${row.path}`} className="flex-none" />
      <span className="min-w-0 flex-1 truncate" title={row.path}>{row.path}</span>
      <Pill tone={row.unmerged || row.deleted ? 'err'
        : row.untracked ? 'ok' : row.staged ? 'info' : 'muted'}>{flag}</Pill>
    </li>
  );
}
