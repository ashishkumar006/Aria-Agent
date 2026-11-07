import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import * as monaco from 'monaco-editor';
import type { StudioHunk } from '../../api';
import { editorLanguage } from '../../components/MonacoEditor';

/* Review mode — the diff a human decides, not a patch a human applies.

   WHY A DIFF VIEW AND NOT THE MARKS: the agent's gutter colours (Code.tsx and
   the old rail) say "something changed here". They cannot answer "what was it
   before, and do I want the new thing", which is the actual review question,
   and they are decorations on the buffer rather than a decision surface. This
   renders the server's baseline beside the current text — the same two
   versions the hunks were computed from, so what you click and what the server
   reverses are the same object.

   WHY VIEW ZONES AND NOT MONACO'S OWN DIFF GUTTER: a hunk here needs a name, a
   +/- count and two verbs, and it must sit UNDER its own region so the text
   above it stays readable. Monaco lays view zones out and keeps them anchored
   while the text scrolls, which is the whole reason not to write a scroll
   listener and reposition divs by hand.

   WHY THE ANCHOR IS A CONTENT SEARCH: the hunk's line numbers come from the
   disk. The pane shows the buffer, which may carry unsaved edits above the
   hunk, and an unreverted hunk clicked at a stale line number is a hunk
   reverted in the wrong place. Searching for the hunk's own lines is the same
   rule the server applies when it decides, so the two can only agree or both
   refuse — and the banner says which one you are in. */

export interface ReviewHandle {
  next(): void;
  prev(): void;
  reveal(index: number): void;
  count(): number;
}

interface Props {
  path: string;
  lang: string;
  /** The server's baseline for this path: the state pending hunks are measured
   *  against. After a keep that is NOT the file as the agent found it, which is
   *  why it comes from the route rather than from the buffer's own copy. */
  base: string;
  /** The current text, editable. */
  body: string;
  hunks: StudioHunk[];
  /** Hunk id whose decision is in flight, or "". */
  busy: string;
  /** Unsaved edits in the buffer. A decision writes the file server-side, so
   *  your own unsaved work would land underneath it — which is why this
   *  disables the verbs rather than warning about them. */
  dirty: boolean;
  conflict: string;
  ariaLabel: string;
  onApply: (hunkId: string, action: 'keep' | 'revert') => void;
  onChange: (text: string) => void;
  onHandle: (h: ReviewHandle | null) => void;
}

/** 1-based line the hunk's own block ends on inside `lines`, or -1. */
function anchorLine(lines: string[], h: StudioHunk): number {
  const block = h.new && h.new.length ? h.new : h.old;
  if (!block.length || lines.length < block.length) return -1;
  for (let i = 0; i + block.length <= lines.length; i += 1) {
    let same = true;
    for (let j = 0; j < block.length; j += 1) {
      if (lines[i + j] !== block[j]) { same = false; break; }
    }
    if (same) return i + block.length;
  }
  return -1;
}

export default function ReviewEditor({
  path, lang, base, body, hunks, busy, dirty, conflict, ariaLabel,
  onApply, onChange, onHandle,
}: Props) {
  const host = useRef<HTMLDivElement | null>(null);
  const edRef = useRef<monaco.editor.IStandaloneDiffEditor | null>(null);
  const origRef = useRef<monaco.editor.ITextModel | null>(null);
  const modRef = useRef<monaco.editor.ITextModel | null>(null);
  const zones = useRef<{ id: string; node: HTMLElement; at: number;
                         hunkId: string }[]>([]);
  const active = useRef(0);
  const [shown, setShown] = useState(0);
  /* True while this component mutates a model, so the model's own change event
     does not round-trip as user typing. */
  const applying = useRef(false);
  const onApplyRef = useRef(onApply);
  onApplyRef.current = onApply;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const stateRef = useRef({ hunks, busy, dirty });
  stateRef.current = { hunks, busy, dirty };

  const total = hunks.length;

  /* One bar under one hunk: built as DOM because it lives inside Monaco's
     view, not in React's tree, and its handlers go through refs so a zone made
     three renders ago still calls THIS render's `onApply`.
     The hunk count comes off `stateRef`, never off the render closure:
     `onDidUpdateDiff` fires asynchronously and holds whichever closure created
     the subscription, so a closure-captured `total` is the count from the
     moment the models were built — which is 0, because the review data arrives
     after the file does. It showed as "hunk 1/0". */
  const buildZone = (h: StudioHunk, i: number): HTMLElement => {
    const hs = stateRef.current.hunks;
    const count = hs.length;
    const row = document.createElement('div');
    row.className = 'flex items-center gap-2 border-y border-glass-border '
      + 'bg-surface-2 px-3 py-1 text-[11px]';
    row.dataset.hunk = h.id;
    /* Monaco puts `.view-zones` (index 2) BEFORE `.view-lines` (index 3), both
       absolutely positioned with `z-index: auto`, so the text layer paints over
       the bar and `document.elementFromPoint` on the Keep button answers
       `.view-lines` — the per-hunk decision was unclickable for a real mouse,
       and only a dispatched event reached it. A positioned child with a
       z-index outranks those auto siblings in the same stacking context. */
    row.style.position = 'relative';
    row.style.zIndex = '30';

    const label = document.createElement('span');
    label.className = 'text-zinc-muted';
    label.textContent = `hunk ${i + 1}/${count} · +${h.added} −${h.removed}`;
    row.appendChild(label);

    const spacer = document.createElement('span');
    spacer.className = 'min-w-0 flex-1';
    row.appendChild(spacer);

    if (stateRef.current.dirty) {
      const note = document.createElement('span');
      note.className = 'text-warn';
      note.textContent = 'unsaved edits — save before deciding';
      row.appendChild(note);
      return row;
    }

    const mk = (text: string, cls: string, action: 'keep' | 'revert') => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = stateRef.current.busy === h.id ? '…' : text;
      b.disabled = stateRef.current.busy === h.id;
      b.className = cls;
      b.setAttribute('aria-label',
        `${text} hunk ${i + 1} of ${count} in ${path}`);
      /* mousedown is what Monaco listens to; refusing it keeps the click for
         the button instead of moving the caret into the diff. */
      b.addEventListener('mousedown', (e) => e.stopPropagation());
      b.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        onApplyRef.current(h.id, action);
      });
      row.appendChild(b);
    };
    mk('Keep', 'rounded-lg border border-ok/40 bg-ok-dim '
      + 'px-1.5 py-0.5 text-ok hover:bg-ok/20', 'keep');
    mk('Revert', 'rounded-lg border border-danger/40 bg-danger-dim '
      + 'px-1.5 py-0.5 text-danger hover:bg-danger/20', 'revert');
    return row;
  };

  /* Register, and UNregister, one bar per hunk.

     `acc.addZone()` hands back an id, and the only way to take a zone out of
     Monaco's view model is `acc.removeZone(id)`. Removing the DOM node instead
     — which is what this did first — leaves the zone registered forever: every
     re-sync added N more, so typing in review mode grew the zone list without
     bound and the view had more and more layout work to do for bars that were
     no longer on screen. That is the stuck screen. The ids are kept for exactly
     this reason. */
  const syncZones = (): void => {
    const ed = edRef.current;
    const model = modRef.current;
    if (!ed || !model) return;
    const hs = stateRef.current.hunks;
    const lines = model.getLinesContent();
    const want = hs.map((h, i) => {
      const found = anchorLine(lines, h);
      const at = found > 0 ? found
        : Math.max(1, Math.min(lines.length, h.new_start + h.new_lines - 1));
      return { h, i, at };
    });
    /* Nothing moved, nothing to rebuild. A keystroke that does not change any
       anchor must not tear down and re-create every bar: that is a full DOM
       rebuild per character, on top of the diff Monaco is already doing. */
    const same = want.length === zones.current.length
      && want.every((w, i) => w.at === zones.current[i].at
        && w.h.id === zones.current[i].hunkId);
    if (same) return;
    ed.getModifiedEditor().changeViewZones((acc) => {
      for (const z of zones.current) {
        try { acc.removeZone(z.id); } catch { /* already gone */ }
        z.node.remove();
      }
      zones.current = [];
      for (const w of want) {
        const node = buildZone(w.h, w.i);
        const id = acc.addZone({ afterLineNumber: w.at, domNode: node,
                                 heightInPx: 26, suppressMouseDown: true });
        zones.current.push({ id, node, at: w.at, hunkId: w.h.id });
      }
    });
  };

  const goTo = (index: number): void => {
    const ed = edRef.current;
    const model = modRef.current;
    const hs = stateRef.current.hunks;
    if (!ed || !hs.length) return;
    const wrapped = ((index % hs.length) + hs.length) % hs.length;
    const lines = model ? model.getLinesContent() : [];
    const anchor = anchorLine(lines, hs[wrapped]);
    const at = anchor > 0 ? Math.max(1, hs[wrapped].new_start) : 1;
    active.current = wrapped;
    setShown(wrapped);
    try { ed.getModifiedEditor().revealLineInCenter(at); } catch { /* no layout */ }
  };

  /* The handle is what F8/Shift+F8 and the nav row drive, so the keyboard and
     the mouse walk the same sequence of hunks. Published once; every method
     closes over refs, so a stale render cannot act on a stale hunk list. */
  useEffect(() => {
    const handle: ReviewHandle = {
      next: () => goTo(active.current + 1),
      prev: () => goTo(active.current - 1),
      reveal: (i: number) => goTo(i),
      count: () => stateRef.current.hunks.length,
    };
    onHandle(handle);
    return () => onHandle(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* Create once, tear down on unmount (and on a StrictMode remount, which
     would otherwise leave two live diff editors on one host). */
  useEffect(() => {
    if (!host.current) return undefined;
    const ed = monaco.editor.createDiffEditor(host.current, {
      theme: 'aria-dark',
      automaticLayout: true,
      renderSideBySide: true,
      useInlineViewWhenSpaceIsLimited: false,
      originalEditable: false,
      readOnly: false,
      renderOverviewRuler: true,
      minimap: { enabled: false },
      lineNumbersMinChars: 4,
      scrollBeyondLastLine: false,
      ariaLabel,
      fontFamily: '"JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 12.5,
      lineHeight: 20,
      padding: { top: 8, bottom: 40 },
      scrollbar: { alwaysConsumeMouseWheel: false },
    });
    edRef.current = ed;
    return () => {
      zones.current.forEach((z) => z.node.remove());
      zones.current = [];
      // Null the ref BEFORE disposing. React runs each effect's cleanup in
      // declaration order, so this one runs first and the model effect's
      // cleanup below then reaches for the editor — and `setModel(null)` on a
      // disposed diff editor throws "InstantiationService has been disposed",
      // which the error boundary rendered as a broken studio every time a
      // review tab was opened and then switched away from.
      edRef.current = null;
      ed.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* One pair of models per file, then the zones that belong to them. */
  useEffect(() => {
    const ed = edRef.current;
    if (!ed) return;
    const mk = (suffix: string, text: string) => monaco.editor.createModel(
      text, editorLanguage(lang),
      monaco.Uri.from({ scheme: 'inmemory', authority: 'aria',
                        path: `/review/${path}${suffix}` }));
    const o = mk('-base', base);
    const m = mk('-cur', body);
    origRef.current = o;
    modRef.current = m;
    ed.setModel({ original: o, modified: m });
    const sub = m.onDidChangeContent(() => {
      if (applying.current) return;
      onChangeRef.current?.(m.getValue());
    });
    const done = ed.onDidUpdateDiff(() => syncZones());
    syncZones();
    return () => {
      sub.dispose();
      done.dispose();
      zones.current.forEach((z) => z.node.remove());
      zones.current = [];
      // Only hand the editor back if it is still the live one — see the mount
      // effect above. The models are always ours to dispose.
      if (edRef.current === ed) ed.setModel(null);
      o.dispose();
      m.dispose();
      origRef.current = null;
      modRef.current = null;
    };
    // A path change rebuilds the pair; text changes are pushed in below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  /* Push the authoritative texts in when they changed behind the editor's
     back — a keep, a revert, or the agent writing again. Typing is excluded:
     the caller's copy is then a copy of ours, which keeps the caret alive. */
  useEffect(() => {
    const o = origRef.current;
    if (o && o.getValue() !== base) {
      applying.current = true;
      o.setValue(base);
      applying.current = false;
    }
    const m = modRef.current;
    if (m && m.getValue() !== body) {
      applying.current = true;
      m.setValue(body);
      applying.current = false;
    }
    syncZones();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, body, hunks, busy, dirty]);

  return (
    <div className="relative min-h-0 flex-1">
      <div ref={host} data-editor-host="" className="absolute inset-0" />
      <div className="absolute right-4 top-1 z-10 flex items-center gap-1.5 rounded-lg border border-glass-border bg-surface-2/90 px-1.5 py-0.5">
        <button type="button"
                className="text-zinc-muted hover:text-zinc-100"
                aria-label="Previous hunk (Shift+F8)"
                title="Previous hunk (Shift+F8)"
                onClick={() => goTo(active.current - 1)}><ChevronUp size={12} /></button>
        <span className="min-w-[3.5rem] text-center text-[11px] text-zinc-muted">
          {total ? `hunk ${Math.min(shown + 1, total)} of ${total}` : 'no hunks'}
        </span>
        <button type="button"
                className="text-zinc-muted hover:text-zinc-100"
                aria-label="Next hunk (F8)"
                title="Next hunk (F8)"
                onClick={() => goTo(active.current + 1)}><ChevronDown size={12} /></button>
      </div>
      {conflict && (
        <div className="absolute bottom-0 left-0 right-0 z-10 border-t border-warn/30 bg-warn-dim px-3 py-1.5 text-[11px] text-warn">
          {conflict}
        </div>
      )}
    </div>
  );
}
