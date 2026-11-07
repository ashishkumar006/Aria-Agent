import { useEffect, useMemo, useState } from 'react';
import { Archive, ChevronDown, ChevronRight, Code2, FolderTree, Plus, RefreshCw, RotateCcw, Search, Settings, X } from 'lucide-react';
import { api } from '../../api';
import type { StudioSession } from '../../api';
import { Label } from './chrome';
import { Skel } from '../../components/ui';

/* The studio's left column: sessions, not files.
 *
 * This is the framing the reference product gets right and that a VS
 * Code-shaped tree gets wrong. In an agent-first surface the object you manage
 * is the conversation that is working on your code — so the list shows those,
 * names them by what you asked for, and says which folders each one touches.
 * Files live in the workbench beside the conversation, where they belong.
 *
 * Every row here is real data from `/api/studio/sessions`. Nothing is
 * placeholder: a session list that shows invented rows is worse than no list,
 * because clicking one and finding nothing reads as the app being broken
 * rather than as a feature not existing yet. */

interface Props {
  sid: string;
  sessions: StudioSession[];
  loading: boolean;
  error: string;
  onSwitch: (sid: string) => void;
  onNew: () => void;
  onArchive: (sid: string, archived: boolean) => void;
  /** Opens the workspace picker's "add a folder" path — the same route the
   *  workbench uses, so there is one way to declare a folder. */
  onAddFolder: () => void;
  onReload: () => void;
}

const ago = (ts: number): string => {
  if (!ts) return '';
  const s = Math.max(1, Math.round((Date.now() / 1000) - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};

/** A session's display name. Five rows reading "New session - 2026-11-06" is
 *  what the studio looked like in the browser pass, because the date is the
 *  fallback and a day produces identical labels: so fall back to the folders
 *  it holds, which is the one thing that does distinguish them, and only then
 *  to "Untitled session". */
const nameOf = (s: StudioSession): string => {
  if (s.label) return s.label;
  const paths = (s.paths || []).map((p) => p.replace(/[/\\]+$/, '').split(/[/\\]/).pop() || p);
  if (paths.length === 1) return paths[0];
  if (paths.length > 1) return `${paths[0]} +${paths.length - 1}`;
  return 'Untitled session';
};

export function Sessions({ sid, sessions, loading, error, onSwitch, onNew,
                           onArchive, onAddFolder, onReload }: Props) {
  const [filter, setFilter] = useState('');
  const [workspace, setWorkspace] = useState('');
  const [picking, setPicking] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [who, setWho] = useState('');

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const r = await api.gatewayUser();
        if (!live) return;
        setWho(r?.user?.name || (r?.configured ? 'Signed in' : ''));
      } catch { /* the identity row simply stays blank */ }
    })();
    return () => { live = false; };
  }, []);

  const workspaces = useMemo(() => {
    const set = new Set<string>();
    for (const s of sessions) for (const p of s.paths || []) if (p) set.add(p);
    return [...set].sort();
  }, [sessions]);

  const visible = useMemo(() => sessions.filter((s) => {
    if (workspace && !(s.paths || []).includes(workspace)) return false;
    const q = filter.trim().toLowerCase();
    if (!q) return true;
    return (s.label || s.sid).toLowerCase().includes(q)
      || (s.paths || []).some((p) => p.toLowerCase().includes(q));
  }), [sessions, filter, workspace]);

  const active = visible.filter((s) => !s.archived);
  const archived = visible.filter((s) => s.archived);

  return (
    <nav aria-label="Studio sessions"
         className="flex h-full min-h-0 w-full flex-col border-r border-edge bg-surface-1">
      <div className="flex flex-none flex-col gap-2 px-3 pb-2 pt-3.5">
        {/* The rail's identity row, in the console's own shape: a tinted icon
            tile, the name in bold, a muted count. It used to be one 13px word
            with a sentence glued to it, which read as a caption rather than
            the title of the column. */}
        <div className="flex items-center gap-2 px-1">
          <span aria-hidden
                className="flex h-7 w-7 flex-none items-center justify-center rounded-lg bg-accent/15 text-accent">
            <Code2 size={14} />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-[14px] font-bold leading-tight tracking-tight text-zinc-100">Studio</span>
            <span className="block truncate text-[10px] uppercase tracking-[0.14em] text-zinc-muted">
              sessions · folders · agents
            </span>
          </span>
        </div>
        <button type="button" onClick={onNew}
                className="flex items-center justify-center gap-1.5 rounded-lg bg-accent px-2.5 py-2 text-[12px] font-semibold text-black transition-colors hover:bg-accent/90">
          <Plus size={13} />
          New session
        </button>
        <div className="flex h-8 items-center gap-2 rounded-lg border border-glass-border bg-surface-2/40 px-2 transition-colors focus-within:border-accent/50">
          <Search size={12} className="flex-none text-zinc-muted" />
          <input value={filter} onChange={(e) => setFilter(e.target.value)}
                 placeholder="Search sessions"
                 aria-label="Search sessions"
                 className="min-w-0 flex-1 bg-transparent text-[12px] outline-none placeholder:text-zinc-muted" />
          {!!filter && (
            <button type="button" onClick={() => setFilter('')}
                    aria-label="Clear the search"
                    className="flex h-5 w-5 flex-none items-center justify-center rounded text-zinc-muted hover:bg-white/5 hover:text-zinc-100">
              <X size={12} />
            </button>
          )}
        </div>
      </div>

      <div className="relative flex-none px-3 pb-2">
        <div className="flex items-center gap-1">
          <button type="button"
                  onClick={() => setPicking((v) => !v)}
                  aria-expanded={picking}
                  className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-glass-border px-2 py-1.5 text-left text-[12px] text-zinc-200 transition-colors hover:bg-white/5">
            <FolderTree size={12} aria-hidden className="flex-none text-zinc-muted" />
            <span className="min-w-0 flex-1 truncate">
              {workspace || 'All workspaces'}
            </span>
            <ChevronDown size={12} aria-hidden className="flex-none text-zinc-muted" />
          </button>
          <button type="button" onClick={onReload} title="Re-read the session list"
                  aria-label="Re-read the session list"
                  className="flex h-[30px] w-[30px] items-center justify-center rounded-lg border border-glass-border text-zinc-muted transition-colors hover:bg-white/5 hover:text-zinc-100">
            <RefreshCw size={12} />
          </button>
        </div>
        {picking && (
          <div role="menu"
               className="absolute left-3 right-3 top-full z-20 mt-1 overflow-hidden rounded-md border border-edge bg-surface-2 shadow-depth">
            <button type="button" role="menuitem"
                    onClick={() => { setWorkspace(''); setPicking(false); }}
                    className={'block w-full px-3 py-1.5 text-left text-[13px] hover:bg-white/5 '
                      + (workspace ? 'text-zinc-300' : 'text-zinc-100')}>
              All workspaces
            </button>
            {workspaces.map((w) => (
              <button key={w} type="button" role="menuitem"
                      onClick={() => { setWorkspace(w); setPicking(false); }}
                      className={'block w-full truncate px-3 py-1.5 text-left text-[13px] hover:bg-white/5 '
                        + (workspace === w ? 'text-zinc-100' : 'text-zinc-300')}>
                {w}
              </button>
            ))}
            <button type="button" role="menuitem"
                    onClick={() => { setPicking(false); onAddFolder(); }}
                    className="block w-full border-t border-glass-border px-3 py-1.5 text-left text-[13px] text-zinc-muted hover:bg-white/5">
              Open a folder in this session…
            </button>
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-2 pb-2">
        {loading && !sessions.length && <Skel n={2} />}
        {error && <p className="px-2 py-1 text-[12px] text-danger">{error}</p>}
        {!loading && !visible.length && (
          <p className="px-2 py-1 text-[12px] leading-relaxed text-zinc-muted">
            {filter || workspace
              ? 'No session matches this filter.'
              : 'No sessions yet. The one you are in appears here as soon as it has a folder.'}
          </p>
        )}

        {!!active.length && (
          <>
            <p className="px-2 pb-1 pt-1">
              <Label>Active</Label>
            </p>
            {active.map((s) => (
              <Row key={s.sid} s={s} current={s.sid === sid} onSwitch={onSwitch}
                   onArchive={onArchive} />
            ))}
          </>
        )}

        {!!archived.length && (
          <div className="mt-2">
            <button type="button" onClick={() => setShowArchived((v) => !v)}
                    aria-expanded={showArchived}
                    className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1 text-left text-zinc-500 transition-colors hover:bg-white/[0.04] hover:text-zinc-300">
              {showArchived
                ? <ChevronDown size={10} aria-hidden className="flex-none" />
                : <ChevronRight size={10} aria-hidden className="flex-none" />}
              <Label>Archived</Label>
              <span className="ml-auto text-[10px] tabular-nums text-zinc-muted">{archived.length}</span>
            </button>
            {showArchived && archived.map((s) => (
              <Row key={s.sid} s={s} current={s.sid === sid} onSwitch={onSwitch}
                   onArchive={onArchive} />
            ))}
          </div>
        )}
      </div>

      <div className="flex flex-none items-center gap-2 border-t border-edge px-3 py-2.5">
        <span aria-hidden
              className="flex h-6 w-6 flex-none items-center justify-center rounded-full border border-glass-border bg-white/[0.06] text-[10px] font-semibold text-zinc-300">
          {(who || 'local').slice(0, 2).toUpperCase()}
        </span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-400" title={who}>
          {who || 'this machine'}
        </span>
        <a href="/settings" title="Aria settings" aria-label="Aria settings"
           className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-200">
          <Settings size={12} />
        </a>
      </div>
    </nav>
  );
}

function Row({ s, current, onSwitch, onArchive }: {
  s: StudioSession;
  current: boolean;
  onSwitch: (sid: string) => void;
  onArchive: (sid: string, archived: boolean) => void;
}) {
  const title = nameOf(s);
  const folders = (s.paths || []).map((p) => p.replace(/[/\\]+$/, '').split(/[/\\]/).pop() || p);
  const meta = s.roots
    ? `${folders.slice(0, 2).join(', ')}${folders.length > 2 ? ` +${folders.length - 2}` : ''} · ${ago(s.updated)}`
    : `no folder yet · ${ago(s.updated)}`;
  return (
    /* One flex row, not a button with an absolutely-positioned cluster over
       it: the overlay is what made the label lose 127px of its own text to a
       phantom ellipsis, and the 13×17px glyph it floated was the only archive
       target in the app. */
    <div className={
      'group relative my-0.5 flex items-stretch gap-1 rounded-lg pr-1 transition-colors '
      + (current ? 'bg-white/[0.06]' : 'hover:bg-white/[0.035]')}>
      {current && (
        <span aria-hidden
              className="absolute left-0 top-1/2 h-[58%] w-[2px] -translate-y-1/2 rounded-full bg-accent" />
      )}
      <button type="button" onClick={() => onSwitch(s.sid)}
              title={`${title}\n${s.paths?.join('\n') || 'no folders yet'}\n${s.sid}`}
              className="flex min-w-0 flex-1 flex-col items-start gap-1 rounded-lg px-3 py-2 text-left">
        <span className={
          'w-full truncate text-[12.5px] leading-tight '
          + (current ? 'font-medium text-zinc-50' : 'text-zinc-200')}>
          {title}
        </span>
        <span className={
          'flex w-full items-center gap-1.5 text-[10.5px] leading-tight '
          + (s.roots ? 'text-zinc-muted' : 'text-warn/80')}>
          <span className="min-w-0 truncate">{meta}</span>
          {!!s.roots && (
            <span className="flex-none tabular-nums text-zinc-muted/70">
              {s.roots} folder{s.roots === 1 ? '' : 's'}
            </span>
          )}
        </span>
      </button>
      <button type="button"
              onClick={(e) => { e.stopPropagation(); onArchive(s.sid, !s.archived); }}
              aria-label={s.archived ? `Restore ${title}` : `Archive ${title}`}
              title={s.archived ? 'Restore to Active — nothing on disk changes'
                                 : 'Archive — nothing on disk changes'}
              className="flex h-6 w-6 flex-none items-center justify-center self-center rounded-md text-zinc-500 opacity-0 transition-opacity hover:bg-white/5 hover:text-zinc-100 focus-visible:opacity-100 group-focus-within:opacity-100 group-hover:opacity-100">
        {s.archived ? <RotateCcw size={12} /> : <Archive size={12} />}
      </button>
    </div>
  );
}
