/* Undo/redo for the Code editor.

   The native textarea's own undo stack cannot be relied on here. Structural
   edits - comment toggle, indent, move line, bracket close, replace-all - are
   applied by writing to `textarea.value` and dispatching an `input` event,
   which is the only way to move the caret correctly from script. React then
   re-renders the controlled value on top of that. The result is that Ctrl+Z
   walks a history that does not correspond to what the user just did, and
   after a programmatic edit it can restore a stale buffer.

   So history is explicit: every change goes through `commit`, snapshots are
   coalesced while typing, and undo/redo restore text AND selection.
*/

import { useCallback, useRef } from 'react';

export interface Snapshot { text: string; start: number; end: number }

const COALESCE_MS = 600;
/** Snapshots are whole buffers, so cap the stack. At 400 KB a buffer this is
    ~120 MB worst case, which is not acceptable; 200 is far more history than
    anyone undoes inside one editing session. */
const MAX_DEPTH = 200;

export interface History {
  commit: (text: string, start: number, end: number, coalesce?: boolean) => void;
  /** Replace the whole buffer without recording (e.g. revert-to-served). */
  reset: (text: string) => void;
  undo: (current: Snapshot) => Snapshot | null;
  redo: (current: Snapshot) => Snapshot | null;
  canUndo: () => boolean;
  canRedo: () => boolean;
  depth: () => { u: number; r: number };
}

export function useHistory(onRestore: (s: Snapshot) => void): History {
  const past = useRef<Snapshot[]>([]);
  const future = useRef<Snapshot[]>([]);
  const last = useRef<{ text: string; at: number } | null>(null);

  /* `text`/`start`/`end` describe the buffer as it was BEFORE the change the
     caller is about to make, which is what undo has to come back to. */
  const commit = useCallback((text: string, start: number, end: number,
                               coalesce = false) => {
    const now = Date.now();
    const prev = past.current[past.current.length - 1];
    const isRepeat = coalesce
      && prev !== undefined
      && last.current !== null
      && now - last.current.at < COALESCE_MS;

    /* While typing, only the first snapshot of a run is kept, so one Ctrl+Z
       removes the whole burst rather than one character. The selection stored
       is deliberately the one from the start of the burst. */
    if (!isRepeat) {
      past.current.push({ text, start, end });
      if (past.current.length > MAX_DEPTH) past.current.shift();
    }
    future.current = [];
    last.current = { text, at: now };
  }, []);

  const reset = useCallback((text: string) => {
    past.current = [];
    future.current = [];
    last.current = null;
    void text;
  }, []);
  const undo = useCallback((current: Snapshot): Snapshot | null => {
    const prev = past.current.pop();
    if (!prev) return null;
    future.current.push(current);
    last.current = null;
    onRestore(prev);
    return prev;
  }, [onRestore]);

  const redo = useCallback((current: Snapshot): Snapshot | null => {
    const next = future.current.pop();
    if (!next) return null;
    past.current.push(current);
    last.current = null;
    onRestore(next);
    return next;
  }, [onRestore]);

  const canUndo = useCallback(() => past.current.length > 0, []);
  const canRedo = useCallback(() => future.current.length > 0, []);
  const depth = useCallback(() => ({ u: past.current.length, r: future.current.length }), []);

  return { commit, reset, undo, redo, canUndo, canRedo, depth };
}
