import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowUp, Check, Folder, FolderOpen, HardDrive, Monitor, TriangleAlert, X } from 'lucide-react';
import { api, type StudioFsDir, type StudioFsListResult } from '../../api';
import { Label } from './chrome';

/* A folder picker for the studio, drawn as a file-explorer dialog.
 *
 * Nobody should have to type `C:\Users\…` from memory to open a project — this
 * is the Open Folder dialog, in the console's own idiom, backed by
 * `/api/studio/fs/list` (folder NAMES only; file contents are never read and
 * hidden entries are never listed).
 *
 * The layout follows the file-explorer dialog people already know: a quick
 * access column on the left (This PC + the machine's drives), a breadcrumb and
 * Up button on top, a Name / Type list in the middle, and Open/Cancel at the
 * bottom. "Type a path instead" swaps the list header for an input, in place,
 * rather than dismissing the dialog.
 *
 * You can navigate anywhere — it is your machine — the Select button is
 * disabled on a folder that holds the agent's state directory with the refusal
 * text, because picking it is the one thing the add route would refuse. */

export function FolderPicker({ open, onClose, onPick }: {
  open: boolean;
  onClose: () => void;
  onPick: (path: string) => void;
}) {
  const [listing, setListing] = useState<StudioFsListResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [typing, setTyping] = useState(false);
  const [typed, setTyped] = useState('');
  const [drives, setDrives] = useState<StudioFsDir[]>([]);
  const [quick, setQuick] = useState<StudioFsDir[]>([]);
  const gen = useRef(0);

  const load = useCallback(async (p: string) => {
    const mine = ++gen.current;
    setBusy(true);
    setErr('');
    try {
      const got = await api.studioFsList(p);
      if (mine !== gen.current) return;
      setListing(got);
      if (got?.error) setErr(got.error);
      // The drive list is the sidebar; refresh it whenever we are at the top.
      if (!p) setDrives(got?.dirs || []);
      setQuick(got?.quick || []);
    } catch (e) {
      if (mine === gen.current) setErr(e instanceof Error ? e.message : String(e));
    } finally {
      if (mine === gen.current) setBusy(false);
    }
  }, []);

  /* Every open starts at the drive list — the machine's top level — rather
     than at a remembered path: "where is it?" is the question the picker
     exists to answer. */
  useEffect(() => {
    if (open) { setListing(null); setErr(''); setTyping(false); setTyped(''); void load(''); }
  }, [open, load]);

  /* Escape cancels; Enter picks the current folder when it is pickable. */
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); onClose(); }
      if (e.key === 'Enter' && listing?.selectable && listing.path) {
        e.preventDefault();
        onPick(listing.path);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, listing, onClose, onPick]);

  const dirs = useMemo(() => listing?.dirs || [], [listing]);
  const current = listing?.path || '';
  const atDrives = !current;
  const selectable = !!listing?.selectable;
  const refusal = listing?.reason || '';
  const isDrive = (d: StudioFsDir) => !d.path.includes('/') && !d.path.includes('\\');

  if (!open) return null;
  return (
    <div role="dialog" aria-modal="true" aria-label="Open folder"
         className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4 backdrop-blur-[3px]"
         onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="flex h-[540px] max-h-[88vh] w-[720px] max-w-full flex-col overflow-hidden rounded-[10px] border border-glass-border bg-surface-1 shadow-depth">

        {/* Title bar */}
        <div className="flex flex-none items-center gap-2 border-b border-glass-border px-3 py-2">
          <FolderOpen size={14} aria-hidden className="text-accent" />
          <Label className="flex-1">Open folder</Label>
          <button type="button" onClick={onClose}
                  aria-label="Close the picker" title="Close"
                  className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-100">
            <X size={13} />
          </button>
        </div>

        {/* Breadcrumb + Up */}
        <div className="flex flex-none items-center gap-1.5 border-b border-glass-border px-3 py-1.5">
          <button type="button" onClick={() => void load(listing?.parent || '')}
                  disabled={atDrives}
                  title="Up one level" aria-label="Up one level"
                  className="flex h-6 w-6 flex-none items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-white/5 hover:text-zinc-100 disabled:opacity-30">
            <ArrowUp size={13} />
          </button>
          <div className="flex min-w-0 flex-1 items-center gap-1 rounded-md border border-glass-border bg-black/30 px-2 py-1">
            <Monitor size={12} aria-hidden className="flex-none text-zinc-500" />
            <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-zinc-300"
                  title={atDrives ? 'This PC' : current}>
              {atDrives ? 'This PC' : current}
            </span>
            {busy && <span className="flex-none font-mono text-[10px] text-zinc-600">…</span>}
          </div>
          <button type="button" onClick={() => setTyping((v) => !v)}
                  className="flex-none rounded-md px-1.5 py-1 text-[11px] text-zinc-500 transition-colors hover:text-zinc-200">
            {typing ? 'browse' : 'type a path'}
          </button>
        </div>

        {/* Type-ahead, in place — the dialog stays open. */}
        {typing && (
          <div className="flex flex-none items-center gap-1.5 border-b border-glass-border px-3 py-2">
            <input autoFocus value={typed}
                   onChange={(e) => setTyped(e.target.value)}
                   onKeyDown={(e) => {
                     if (e.key === 'Enter') {
                       e.preventDefault();
                       void load(typed.trim());
                       setTyping(false);
                     }
                   }}
                   placeholder="C:\\Users\\…\\my-project"
                   aria-label="Folder path"
                   className="min-w-0 flex-1 rounded-md border border-glass-border bg-black/30 px-2 py-1 font-mono text-[12px] text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-white/20" />
            <button type="button"
                    onClick={() => { void load(typed.trim()); setTyping(false); }}
                    className="rounded-md border border-glass-border px-2 py-1 text-[12px] text-zinc-200 hover:bg-white/5">
              Go
            </button>
          </div>
        )}

        {/* Quick access + file list */}
        <div className="flex min-h-0 flex-1">
          <aside className="flex w-[172px] flex-none flex-col gap-0.5 overflow-y-auto border-r border-glass-border p-1.5">
            <button type="button" onClick={() => void load('')}
                    className={'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] transition-colors '
                      + (atDrives ? 'bg-white/[0.06] text-zinc-100' : 'text-zinc-400 hover:bg-white/5 hover:text-zinc-200')}>
              <Monitor size={13} aria-hidden className="flex-none text-zinc-500" />
              <span className="truncate">This PC</span>
            </button>
            {quick.length > 0 && (
              <>
                <p className="px-2 pb-0.5 pt-2 text-[9.5px] font-bold uppercase tracking-[0.14em] text-zinc-600">Quick access</p>
                {quick.map((d) => (
                  <button key={d.path} type="button" onClick={() => void load(d.path)}
                          title={d.path}
                          className={'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] transition-colors '
                            + (current === d.path ? 'bg-white/[0.06] text-zinc-100'
                                                 : 'text-zinc-400 hover:bg-white/5 hover:text-zinc-200')}>
                    <Folder size={13} aria-hidden className="flex-none text-zinc-500" />
                    <span className="min-w-0 flex-1 truncate">{d.name}</span>
                  </button>
                ))}
              </>
            )}
            {drives.length > 0 && (
              <>
                <p className="px-2 pb-0.5 pt-2 text-[9.5px] font-bold uppercase tracking-[0.14em] text-zinc-600">Devices</p>
                {drives.map((d) => (
                  <button key={d.path} type="button" onClick={() => void load(d.path)}
                          title={d.path}
                          className={'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] transition-colors '
                            + (current === d.path ? 'bg-white/[0.06] text-zinc-100'
                                                 : 'text-zinc-400 hover:bg-white/5 hover:text-zinc-200')}>
                    <HardDrive size={13} aria-hidden className="flex-none text-zinc-500" />
                    <span className="min-w-0 flex-1 truncate">{d.name}</span>
                  </button>
                ))}
              </>
            )}
          </aside>

          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            <div className="flex flex-none items-center gap-2 border-b border-glass-border px-3 py-1 text-[10px] font-bold uppercase tracking-[0.14em] text-zinc-muted">
              <span className="min-w-0 flex-1">Name</span>
              <span className="w-[72px] flex-none">Type</span>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-1.5" role="listbox" aria-label="Folders">
              {err && (
                <p className="flex items-center gap-1.5 px-2 py-2 text-[12px] text-warn">
                  <TriangleAlert size={12} aria-hidden /> {err}
                </p>
              )}
              {!err && dirs.length === 0 && !busy && (
                <p className="px-2 py-4 text-center text-[12px] text-zinc-muted">
                  {atDrives ? 'no drives found' : 'no folders here'}
                </p>
              )}
              {dirs.map((d) => (
                <button key={d.path} type="button" role="option" aria-selected={false}
                        onClick={() => void load(d.path)}
                        title={!d.selectable ? `${d.reason} — you can still open it to reach another folder` : d.path}
                        className="flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-white/5">
                  {isDrive(d)
                    ? <HardDrive size={14} aria-hidden className="flex-none text-zinc-500" />
                    : <Folder size={14} aria-hidden className={d.selectable ? 'flex-none text-zinc-400' : 'flex-none text-zinc-700'} />}
                  <span className={'min-w-0 flex-1 truncate font-mono text-[12px] '
                                   + (d.selectable ? 'text-zinc-200' : 'text-zinc-600')}>
                    {d.name}
                  </span>
                  <span className="w-[72px] flex-none text-[11px] text-zinc-muted">
                    {isDrive(d) ? 'Drive' : 'Folder'}
                  </span>
                </button>
              ))}
              {listing?.truncated && (
                <p className="px-2 py-1.5 font-mono text-[10.5px] text-zinc-600">
                  too many folders here — showing the first 300 alphabetically
                </p>
              )}
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex flex-none items-center gap-2 border-t border-glass-border px-3 py-2">
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-zinc-muted"
                title={current || 'This PC'}>
            {current || 'This PC'}
          </span>
          {!atDrives && !selectable && (
            <span className="min-w-0 max-w-[240px] truncate text-[10.5px] text-warn" title={refusal}>
              {refusal}
            </span>
          )}
          <button type="button" onClick={onClose}
                  className="flex-none rounded-md border border-glass-border px-3 py-1.5 text-[12px] text-zinc-300 transition-colors hover:bg-white/5">
            Cancel
          </button>
          <button type="button"
                  disabled={atDrives || !selectable}
                  onClick={() => onPick(current)}
                  title={atDrives ? 'pick a folder on a drive'
                                   : selectable ? `Open ${current}` : refusal}
                  className="flex flex-none items-center gap-1.5 rounded-md bg-zinc-100 px-3.5 py-1.5 text-[12px] font-bold text-[#161616] transition-colors hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-30">
            <Check size={12} strokeWidth={3} /> Select this folder
          </button>
        </div>
      </div>
    </div>
  );
}