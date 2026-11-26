import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, FilePlus, Folder, FolderOpen } from 'lucide-react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { api, type CodeEntry, type StudioRoot } from '../../api';
import { isUnderRoot, joinPath } from '../../lib/studioSession';
import { fileVisual, FOLDER_CLS } from './fileIcons';

/** The studio file tree.
 *
 *  Virtualized from the start, which the old explorer is not: `_CODE_MAX_FILES`
 *  allows 4000 rows, and `@tanstack/react-virtual` was already a dependency in
 *  this app with zero uses in `src/`. Rendering four thousand DOM nodes is what
 *  makes a tree feel dead when you click a big repo.
 *
 *  The first level is the SESSION'S ROOTS, never the base directory's contents.
 *  Those are different things and mixing them up is how `sessions/`,
 *  `threads/` and the rest of the state directory ended up browsable once the
 *  base was one level above the workspace. The server enforces that in
 *  `_code_listing`; this mirrors it so the UI cannot offer what a request would
 *  refuse.
 */
interface Node {
  /** Base-relative path, e.g. "proj-a/src/app.ts". "" for a root is impossible
   *  — a root's own path is its name. */
  path: string;
  name: string;
  dir: boolean;
  depth: number;
  size?: number;
  /** A transient in-tree input row (VS Code-style): the inline editor for a
   *  new file / new folder, spliced into the flat list where the item will
   *  land instead of living in a detached bar under the tree. */
  pseudo?: 'newfile' | 'mkdir';
}

/* A row verb. Revealed with its row rather than always on screen; `!` on the
   colour so a hovered `del` still reads as the dangerous one. */
const rowAct = 'rounded-md px-1.5 py-0.5 text-[10px] font-semibold text-zinc-muted '
  + 'transition-colors hover:bg-white/10 hover:!text-zinc-100';

/* VS Code's tree indent, in px. The chevron sits in a 16px slot; each depth
   level shifts content by INDENT, and guides are drawn on the same ladder so
   an open folder's lines connect with its children's. */
const INDENT = 12;
const GUIDE_X = (depth: number) => 6 + depth * INDENT + 7;

export function Explorer({ sid, roots, onOpen, activePath,
                           onRenamed, pending = {} }: {
  sid: string;
  roots: StudioRoot[];
  onOpen: (path: string) => void;
  activePath: string;
  /** Tell the shell a path changed on disk under it, so a stale buffer or a
   *  review entry cannot be shown for a file that has moved. */
  onRenamed?: (message: string) => void;
  /** Unreviewed agent changes, shown as VS Code-style M↓ markers. */
  pending?: Record<string, unknown>;
}) {
  const [kids, setKids] = useState<Record<string, Node[]>>({});
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [newFor, setNewFor] = useState('');
  const [newName, setNewName] = useState('');

  /* One inline operation at a time, and its kind, so the row can say what it
     is about to do before it does it. */
  const [acting, setActing] = useState<{ path: string; kind: 'rename' | 'delete' | 'mkdir' } | null>(null);
  const [actValue, setActValue] = useState('');
  const parentRef = useRef<HTMLDivElement>(null);

  /* Root nodes are the declared folders themselves. No fetch, no listing: they
     are known from the session, so the tree paints immediately even when a
     folder is slow or gone. */
  const rootNodes: Node[] = useMemo(() => roots.map((r) => ({
    path: r.name, name: r.name, dir: true, depth: 0,
  })), [roots]);

  const flat = useMemo(() => {
    const out: Node[] = [];
    /* The root row is the folder NAME, which the session already says; show its
       contents directly (always open), the way VS Code opens a folder. */
    const walk = (nodes: Node[], isRoot = false) => {
      for (const n of nodes) {
        if (!isRoot) out.push(n);
        if (n.dir && (isRoot || open[n.path]) && kids[n.path]) walk(kids[n.path]);
      }
    };
    walk(rootNodes, true);
    /* VS Code puts the inline editor IN the tree, as the first row of the
       folder it is creating into — not in a detached bar under the list. A
       collapsed target is expanded by the click that starts the editor, so
       the row lands in view instead of behind a closed folder. */
    const inserted: Node[] = [];
    for (const n of out) {
      inserted.push(n);
      if (newFor === n.path) {
        inserted.push({ path: `__newfile:${n.path}`, name: '', dir: false,
                        depth: n.depth + 1, pseudo: 'newfile' });
      }
      if (acting?.kind === 'mkdir' && acting.path === n.path) {
        inserted.push({ path: `__mkdir:${n.path}`, name: '', dir: false,
                        depth: n.depth + 1, pseudo: 'mkdir' });
      }
    }
    return inserted;
  }, [rootNodes, open, kids, newFor, acting]);

  const virt = useVirtualizer({
    count: flat.length,
    getScrollElement: () => parentRef.current,
    // 20px was the virtualizer's row height, and the browser pass measured the
    // result as a 13-17px hit target: a file tree you have to aim at. 26 keeps
    // the density a tree wants and puts the row above the comfortable-click
    // floor.
    estimateSize: () => 30,
    overscan: 12,
  });

  const load = useCallback(async (path: string) => {
    setLoading((l) => ({ ...l, [path]: true }));
    try {
      const r = await api.codeTree(path, undefined, sid);
      const entries = (r?.entries || []).filter(
        (e: CodeEntry) => !!e && !!e.name);
      const nodes = entries.map((e: CodeEntry) => ({
        path: joinPath(path, e.name), name: e.name, dir: !!e.dir,
        depth: Math.max(0, path.split('/').length - 1), size: e.size,
      }));
      setKids((k) => ({ ...k, [path]: nodes }));
      setErrors((x) => { const n = { ...x }; delete n[path]; return n; });
      return '';
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setErrors((x) => ({ ...x, [path]: msg }));
      return msg;
    } finally {
      setLoading((l) => ({ ...l, [path]: false }));
    }
  }, [sid]);

  /* Load each declared folder's children up front, so the tree shows contents
     immediately rather than a root row waiting to be expanded. */
  const loadedRef = useRef({ sid: '', set: new Set<string>() });
  useEffect(() => {
    if (loadedRef.current.sid !== sid) loadedRef.current = { sid, set: new Set() };
    let live = true;
    (async () => {
      for (const r of rootNodes) {
        if (!live || loadedRef.current.set.has(r.path)) continue;
        loadedRef.current.set.add(r.path);
        await load(r.path);
      }
    })();
    return () => { live = false; };
  }, [rootNodes, load, sid]);


  /* VS Code's new-item flow: expand the target, then drop the inline editor
     in as its first row. Used by the row verbs and by the header icons. */
  const startNewIn = useCallback((dir: string, kind: 'file' | 'folder') => {
    if (!dir) return;
    setOpen((o) => ({ ...o, [dir]: true }));
    if (!kids[dir]) void load(dir);
    if (kind === 'file') {
      setNewFor(dir); setNewName(''); setActing(null);
    } else {
      setActing({ path: dir, kind: 'mkdir' }); setActValue(''); setNewFor('');
    }
  }, [kids, load]);

  const toggle = useCallback((n: Node) => {
    if (!n.dir) { onOpen(n.path); return; }
    setOpen((o) => {
      const next = { ...o, [n.path]: !o[n.path] };
      return next;
    });
    if (!kids[n.path]) void load(n.path);
  }, [kids, load, onOpen]);

  /* A file opened from elsewhere (an agent card, a search hit, a restored
     buffer) must reveal itself in the tree rather than sit invisible under a
     collapsed folder — otherwise the user cannot tell where they are. */
  useEffect(() => {
    if (!activePath || !isUnderRoot(activePath, roots)) return;
    const parts = activePath.split('/');
    const chain: string[] = [];
    for (let i = 1; i < parts.length; i++) chain.push(parts.slice(0, i + 1).join('/'));
    let live = true;
    (async () => {
      const opened: Record<string, boolean> = {};
      for (const dir of chain.slice(0, -0)) {
        if (!live) return;
        if (!kids[dir]) await load(dir);
        opened[dir] = true;
      }
      if (live) setOpen((o) => ({ ...o, ...opened }));
    })();
    return () => { live = false; };
    // kids/load are stable per sid; re-running on every key is what would make
    // this slow, so the effect keys on the path only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePath, sid, roots]);

  const createFile = useCallback(async () => {
    const dir = newFor;
    const name = newName.trim();
    setNewFor(''); setNewName('');
    if (!dir || !name) return;
    try {
      // `dir` is the tree's base-relative path WITH the root name baked in,
      // which is the vocabulary /api/code/newfile resolves (_code_abs is
      // base-relative; stripping the root here made every create land outside
      // the session's folders and 403).
      const r = await api.codeNewFile(name, dir, sid);
      if (r?.error) { setErrors((x) => ({ ...x, [dir]: r.error as string })); return; }
      await load(dir);
      if (r?.path) onOpen(r.path);
    } catch (e) {
      setErrors((x) => ({ ...x, [dir]: e instanceof Error ? e.message : String(e) }));
    }
  }, [newFor, newName, load, onOpen, sid]);

  /* Rename, move, delete, new folder — through the server, which plans them
     against the session's folders and captures what it overwrites into a
     checkpoint. The refusal reasons come back per operation and are shown
     rather than swallowed: a tree that keeps listing a file it did not delete
     is worse than one that says no. */
  const runOp = useCallback(async (kind: 'rename' | 'delete' | 'mkdir',
                                   path: string, to: string) => {
    setErrors((x) => { const n = { ...x }; delete n[path]; return n; });
    // Parent directory to re-read. Rename/delete move items INSIDE a folder,
    // so the parent is `path` minus its last segment. mkdir CREATES inside
    // `path`, so the directory to re-read is `path` itself — slicing it for a
    // slash that does not exist produced "st-ea42792" and the new folder
    // never appeared in the tree.
    const idx = path.lastIndexOf('/');
    const parent = idx < 0 ? '' : path.slice(0, idx);
    const refreshDir = kind === 'mkdir' ? path : parent;
    try {
      const r = await api.studioFiles(sid, kind === 'delete'
        ? [{ op: 'delete', from: path }]
        : kind === 'mkdir'
          ? [{ op: 'mkdir', from: to }]
          : [{ op: 'rename', from: path, to }]);
      if (r?.error) { setErrors((x) => ({ ...x, [path]: r.error as string })); return; }
      const bad = (r?.results || []).filter((x) => !x.ok);
      if (bad.length) {
        setErrors((x) => ({ ...x, [path]: String(bad[0].reason || 'refused') }));
        onRenamed?.(`${kind} refused: ${bad[0].reason || 'no reason given'}`);
        return;
      }
      if (kind === 'delete') {
        setKids((k) => ({ ...k, [parent]: (k[parent] || []).filter((n) => n.path !== path) }));
        onRenamed?.(`deleted ${path} — undoable from the Changes panel`);
      } else {
        if (refreshDir) await load(refreshDir);
        onRenamed?.(kind === 'mkdir' ? `created ${to}`
                                : `renamed ${path} → ${to}`);
      }
    } catch (e) {
      setErrors((x) => ({ ...x, [path]: e instanceof Error ? e.message : String(e) }));
    }
  }, [sid, load, onRenamed]);

  const commitAct = useCallback(async () => {
    if (!acting) return;
    const { path, kind } = acting;
    const value = actValue.trim();
    setActing(null); setActValue('');
    if (kind === 'delete') { void runOp('delete', path, ''); return; }
    if (!value) return;
    const idx = path.lastIndexOf('/');
    const dir = idx < 0 ? '' : path.slice(0, idx);
    if (kind === 'mkdir') void runOp('mkdir', path, `${path}/${value}`);
    else void runOp('rename', path, `${dir}/${value}`);
  }, [acting, actValue, runOp]);

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-surface-0">
      {/* The drawer chrome above already lists the declared folders and the
          re-read; this row is the EXPLORER's own action bar (VS Code keeps
          New File / New Folder there), not a second copy of that. */}
      {!roots.length && (
        <p className="px-3 py-2 text-[12px] leading-relaxed text-zinc-muted">
          This session holds no folders yet, so nothing can be read or written
          — it does not fall back to a shared workspace. Add one above, or make
          an empty playground.
        </p>
      )}
      <div ref={parentRef} className="min-h-0 flex-1 overflow-auto pb-6">
        {virt.getTotalSize() > 0 && (
          <div className="relative w-full" style={{ height: virt.getTotalSize() }}>
            {virt.getVirtualItems().map((row) => {
              const n = flat[row.index];
              const isOpen = !!open[n.path];
              const err = errors[n.path];
              /* VS Code's inline editor, in the tree: a spliced row for a new
                 file/folder, or REPLACING the label of the row being renamed. */
              const renaming = acting?.kind === 'rename' && acting.path === n.path;
              const editState = n.pseudo === 'newfile'
                ? { value: newName, set: setNewName, commit: createFile,
                    cancel: () => { setNewFor(''); setNewName(''); },
                    placeholder: 'name.py', aria: 'New file name' }
                : n.pseudo === 'mkdir'
                  ? { value: actValue, set: setActValue, commit: commitAct,
                      cancel: () => { setActing(null); setActValue(''); },
                      placeholder: 'folder-name', aria: 'New folder name' }
                  : renaming
                    ? { value: actValue, set: setActValue, commit: commitAct,
                        cancel: () => { setActing(null); setActValue(''); },
                        placeholder: n.name, aria: `Rename ${n.name}` }
                    : null;
              const visual = fileVisual(n.name);
              const Visual = visual.icon;
              const active = activePath === n.path;
              return (
                <div
                  key={n.path}
                  className={'group absolute left-0 top-0 flex w-full items-center gap-1 pr-2 '
                    + (active ? 'bg-zinc-100/[0.12] hover:bg-zinc-100/[0.15]'
                              : 'hover:bg-white/[0.04]')}
                  style={{
                    height: row.size,
                    transform: `translateY(${row.start}px)`,
                    paddingLeft: 6 + n.depth * INDENT,
                  }}
                >
                  {/* VS Code's indent guides: one 1px hairline per ancestor
                      level, on the same ladder the rows are indented by. */}
                  {Array.from({ length: n.depth }, (_, a) => (
                    <span key={a} aria-hidden
                          className="pointer-events-none absolute top-0 h-full w-px bg-white/[0.06]"
                          style={{ left: GUIDE_X(a) }} />
                  ))}
                  {n.dir ? (
                    <button
                      type="button"
                      onClick={() => toggle(n)}
                      aria-label={`${isOpen ? 'Collapse' : 'Expand'} ${n.path}`}
                      className="z-10 flex h-4 w-4 flex-none items-center justify-center text-zinc-muted">
                      {loading[n.path] ? '…' : (isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />)}
                    </button>
                  ) : <span className="z-10 w-4 flex-none" />}
                  {!editState && !n.pseudo && (
                    n.dir
                      ? (isOpen
                          ? <FolderOpen size={13} strokeWidth={1.5} color={FOLDER_CLS} className="z-10 flex-none" />
                          : <Folder size={13} strokeWidth={1.5} color={FOLDER_CLS} className="z-10 flex-none" />)
                      : <Visual size={13} strokeWidth={1.5} color={visual.cls} className="z-10 flex-none" />
                  )}
                  {editState ? (
                    <>
                      {n.pseudo === 'newfile' && (
                        <FilePlus size={12} aria-hidden className="flex-none text-zinc-500" />
                      )}
                      {n.pseudo === 'mkdir' && (
                        <Folder size={12} aria-hidden className="flex-none text-zinc-500" />
                      )}
                      <input
                        autoFocus
                        value={editState.value}
                        onChange={(e) => editState.set(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') void editState.commit();
                          if (e.key === 'Escape') editState.cancel();
                        }}
                        onBlur={() => void editState.commit()}
                        placeholder={editState.placeholder}
                        aria-label={editState.aria}
                        className="min-w-0 flex-1 rounded-md border border-white/20 bg-surface-1 px-1.5 py-0.5 text-[12px] text-zinc-100 outline-none placeholder:text-zinc-600" />
                    </>
                  ) : (
                    <button
                      type="button"
                      onClick={() => (n.dir ? toggle(n) : onOpen(n.path))}
                      className={
                        'min-w-0 flex-1 truncate text-left text-[13.5px] '
                        + (n.dir ? 'text-zinc-100' : 'text-zinc-300')
                        + (active ? ' text-zinc-200' : '')
                      }
                      title={n.path}>
                      {n.name}
                    </button>
                  )}
                  {!editState && !n.dir && Boolean(pending[n.path]) && (
                    <span title="modified by the agent" aria-label="modified"
                          className="flex-none font-mono text-[10px] font-bold text-accent">M</span>
                  )}

                  {/* File operations live on the row but only while the row is
                      pointed at: five always-visible verbs per row turned the
                      tree into a button board, and the path — the thing the
                      tree exists to show — was the quietest element on it.
                      Hover or keyboard focus reveals them; the row itself is
                      still the click target for open/expand. A root row gets no
                      actions: removing a folder from the session is a different
                      act, and the server refuses it as a file operation. */}
                  {!err && !editState && (n.depth > 0 || n.dir) && (
                    <div className={
                      'flex flex-none items-center gap-1 transition-opacity '
                      + (acting?.path === n.path || newFor === n.path
                        ? 'opacity-100'
                        : 'opacity-0 group-hover:opacity-100 focus-within:opacity-100')
                    }>
                      {n.dir && (
                        <>
                          <button type="button" onClick={() => startNewIn(n.path, 'file')}
                                  title="New file here" className={rowAct}>
                            +file
                          </button>
                          {n.depth > 0 && (
                            <button type="button" title="New folder inside"
                                    onClick={() => startNewIn(n.path, 'folder')}
                                    className={rowAct}>
                              +dir
                            </button>
                          )}
                        </>
                      )}
                      {n.depth > 0 && (
                        <>
                          <button type="button" title={`Rename ${n.name}`}
                                  onClick={() => { setActing({ path: n.path, kind: 'rename' }); setActValue(n.name); }}
                                  className={rowAct}>
                            rename
                          </button>
                          <button type="button" title={`Delete ${n.name} (undoable from Changes)`}
                                  onClick={() => { setActing({ path: n.path, kind: 'delete' }); setActValue(''); }}
                                  className={rowAct + ' hover:!text-danger'}>
                            del
                          </button>
                        </>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {/* Delete keeps a confirm affordance here — the only op that used to
            live in this bar; rename/mkdir/create edit in the tree itself,
            where VS Code puts them. */}
        {acting?.kind === 'delete' && (
          <div className="px-3 py-1" style={{ paddingLeft: 12 }}>
            <p className="mb-0.5 text-[11px] text-zinc-muted">
              {`delete ${acting.path}? the bytes are kept in a checkpoint you can restore`}
            </p>
            <div className="flex gap-1.5">
              <button type="button" onClick={() => void commitAct()}
                      className="rounded-md border border-danger/40 bg-danger-dim px-2 py-0.5 text-[12px] text-danger hover:bg-danger/20">
                delete
              </button>
              <button type="button" onClick={() => setActing(null)}
                      className="rounded-md border border-glass-border px-2 py-0.5 text-[12px] text-zinc-300 hover:bg-white/5">
                cancel
              </button>
            </div>
          </div>
        )}
        {Object.entries(errors).filter(([p]) => open[p] && errors[p]).map(([p, msg]) => (
          <p key={p} className="px-3 py-1 text-[11px] text-warn">
            {p}: {msg}
          </p>
        ))}
      </div>
    </div>
  );
}
