/* IDE intelligence for the Code view: side-by-side diff, go-to-definition,
   find-references, rename and hover, all backed by /api/code/intel.

   `document_symbols`/`find_references` already run on Python's real AST, so these
   are exact for .py and a conservative scan elsewhere — the same source of truth
   the agent's own tools use, rather than a second implementation that drifts. */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle, Check, CornerDownLeft, FileCode2, Loader2, Replace, X,
} from 'lucide-react';
import { api } from '../api';

/* ── line diff ─────────────────────────────────────────────────────────────── */

export type DiffRow =
  | { kind: 'ctx'; text: string; oldN: number; newN: number }
  | { kind: 'del'; text: string; oldN: number; newN: number }
  | { kind: 'add'; text: string; oldN: number; newN: number };

/** LCS line diff. Bounded so a big file cannot turn into a 2000x2000 DP table. */
export function lineDiff(a: string[], b: string[], cap = 1500): DiffRow[] {
  if (a.length > cap) a = a.slice(0, cap);
  if (b.length > cap) b = b.slice(0, cap);
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const rows: DiffRow[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ kind: 'ctx', text: a[i], oldN: i + 1, newN: j + 1 });
      i++; j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ kind: 'del', text: a[i], oldN: i + 1, newN: j });
      i++;
    } else {
      rows.push({ kind: 'add', text: b[j], oldN: i, newN: j + 1 });
      j++;
    }
  }
  while (i < n) { rows.push({ kind: 'del', text: a[i], oldN: i + 1, newN: j }); i++; }
  while (j < m) { rows.push({ kind: 'add', text: b[j], oldN: i, newN: j + 1 }); j++; }
  return rows;
}

/** Parse a unified `diff` string from the server into rows with line numbers. */
export function parseUnified(diff: string): DiffRow[] {
  const rows: DiffRow[] = [];
  let oldN = 0;
  let newN = 0;
  for (const line of String(diff || '').split('\n')) {
    const hunk = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (hunk) {
      newN = parseInt(hunk[1], 10);
      const oldM = /-(\d+)/.exec(line);
      oldN = oldM ? parseInt(oldM[1], 10) : newN;
      rows.push({ kind: 'ctx', text: line, oldN: 0, newN: 0 });
      continue;
    }
    if (line.startsWith('+')) rows.push({ kind: 'add', text: line.slice(1), oldN, newN: newN + 1 });
    else if (line.startsWith('-')) rows.push({ kind: 'del', text: line.slice(1), oldN: oldN + 1, newN });
    else if (line.startsWith(' ')) rows.push({ kind: 'ctx', text: line.slice(1), oldN: oldN + 1, newN: newN + 1 });
  }
  return rows.filter((r) => r.oldN !== 0 || r.newN !== 0 || r.kind === 'ctx');
}

/* ── diff view ─────────────────────────────────────────────────────────────── */

export function DiffView({ title, subtitle, rows, applyLabel, onApply,
                          onClose, busy, note, error }:
  { title: string; subtitle?: string; rows: DiffRow[];
    applyLabel: string; onApply?: () => void; onClose: () => void;
    busy?: boolean; note?: string | null; error?: string | null }) {
  const added = rows.filter((r) => r.kind === 'add').length;
  const removed = rows.filter((r) => r.kind === 'del').length;
  return (
    <div role="dialog" aria-modal="true" aria-label={title}
         className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="flex max-h-full w-full max-w-5xl flex-col overflow-hidden
                      rounded-xl border border-glass-border bg-surface-1">
        <header className="flex flex-none items-center gap-2 border-b border-glass-border px-3 py-2">
          <FileCode2 size={13} className="flex-none text-emerald-300" />
          <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold text-zinc-100">
            {title}
          </span>
          {subtitle && (
            <span className="hidden truncate text-[11px] text-zinc-muted sm:block">
              {subtitle}
            </span>
          )}
          <span className="text-[11px] tabular-nums text-emerald-300">+{added}</span>
          <span className="text-[11px] tabular-nums text-rose-300">−{removed}</span>
          <button type="button" onClick={onClose} aria-label="Close diff"
                  className="rounded p-1 text-zinc-muted hover:text-zinc-200">
            <X size={13} />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-auto bg-black/30">
          <table className="w-full border-collapse font-mono text-[11.5px] leading-relaxed">
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className={
                  r.kind === 'add' ? 'bg-emerald-500/10'
                    : r.kind === 'del' ? 'bg-rose-500/10' : ''}>
                  <td className="w-10 select-none border-r border-glass-border px-1.5
                                 text-right align-top text-zinc-600">
                    {r.kind === 'del' ? r.oldN : r.oldN || ''}
                  </td>
                  <td className="w-10 select-none border-r border-glass-border px-1.5
                                 text-right align-top text-zinc-600">
                    {r.kind === 'add' ? r.newN : r.newN || ''}
                  </td>
                  <td className="w-4 select-none text-center align-top">
                    {r.kind === 'add' ? <span className="text-emerald-300">+</span>
                      : r.kind === 'del' ? <span className="text-rose-300">−</span>
                        : <span className="text-zinc-700"> </span>}
                  </td>
                  <td className="whitespace-pre-wrap break-all px-1.5 py-0 align-top">
                    <span className={r.kind === 'add' ? 'text-emerald-200'
                      : r.kind === 'del' ? 'text-rose-200' : 'text-zinc-400'}>
                      {r.text || ' '}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {error && (
          <p className="flex-none border-t border-glass-border px-3 py-2 text-[11.5px] text-rose-300">
            <AlertTriangle size={11} className="mr-1 inline" />{error}
          </p>
        )}
        {note && (
          <p className="flex-none border-t border-glass-border px-3 py-2 text-[11.5px] text-emerald-300">
            <Check size={11} className="mr-1 inline" />{note}
          </p>
        )}
        <footer className="flex flex-none items-center gap-2 border-t border-glass-border px-3 py-2">
          {busy && <Loader2 size={12} className="animate-spin text-violet-300" />}
          <span className="flex-1" />
          <button type="button" onClick={onClose}
                  className="rounded-md border border-white/10 px-2.5 py-1 text-[11.5px]
                             text-zinc-300 hover:bg-white/5">
            Close
          </button>
          {onApply && (
            <button type="button" onClick={onApply} disabled={busy}
                    className="rounded-md bg-violet-500/80 px-2.5 py-1 text-[11.5px]
                               font-semibold text-white hover:bg-violet-500
                               disabled:opacity-40">
              {applyLabel}
            </button>
          )}
        </footer>
      </div>
    </div>
  );
}

/* ── go-to-definition / references / rename ────────────────────────────────── */

type Ref = { file: string; line: number; text: string; kind?: string };

export function IntelModal({ path, word: initialWord, onJump, onClose }:
  { path: string; word: string; onJump: (file: string, line: number) => void;
    onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [refs, setRefs] = useState<Ref[]>([]);
  const [word, setWord] = useState(initialWord);
  const [newName, setNewName] = useState('');
  const [sites, setSites] = useState<Ref[] | null>(null);
  const [renamed, setRenamed] = useState<string[] | null>(null);

  const go = useCallback(async (op: 'references' | 'rename') => {
    if (!word) return;
    setBusy(true); setErr(null);
    try {
      const r = await api.codeIntel(op, { path, name: word });
      if (r.error) setErr(String(r.error));
      if (op === 'references') {
        setRefs([...((r.definitions ?? []) as Ref[]),
                 ...((r.references ?? []) as Ref[])]);
      } else {
        setSites((r.sites ?? []) as Ref[]);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'lookup failed');
    } finally { setBusy(false); }
  }, [path, word]);

  const doRename = useCallback(async () => {
    if (!word || !newName) return;
    setBusy(true); setErr(null);
    try {
      const r = await api.codeIntel('rename', {
        path, name: word, new_name: newName, confirm: true });
      if (r.error) { setErr(String(r.error)); return; }
      setRenamed((r.changed ?? []) as string[]);
      setSites(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'rename failed');
    } finally { setBusy(false); }
  }, [path, word, newName]);

  return (
    <div role="dialog" aria-modal="true" aria-label="Code intelligence"
         className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="flex max-h-full w-full max-w-2xl flex-col overflow-hidden
                      rounded-xl border border-glass-border bg-surface-1">
        <header className="flex flex-none items-center gap-2 border-b border-glass-border px-3 py-2">
          <Replace size={13} className="flex-none text-violet-300" />
          <span className="text-[12.5px] font-semibold text-zinc-100">
            Find references / rename
          </span>
          <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-muted">
            {path}
          </span>
          <button type="button" onClick={onClose} aria-label="Close"
                  className="rounded p-1 text-zinc-muted hover:text-zinc-200">
            <X size={13} />
          </button>
        </header>
        <div className="flex flex-none items-center gap-2 border-b border-glass-border px-3 py-2">
          <input value={word} onChange={(e) => setWord(e.target.value)}
                 placeholder="symbol name"
                 aria-label="Symbol"
                 className="min-w-0 flex-1 rounded-md border border-white/10 bg-black/40
                            px-2 py-1 font-mono text-[12px] text-zinc-100 outline-none
                            focus:border-violet-400" />
          <button type="button" onClick={() => go('references')} disabled={busy || !word}
                  className="rounded-md border border-white/10 px-2.5 py-1 text-[11.5px]
                             text-zinc-200 hover:bg-white/5 disabled:opacity-40">
            References
          </button>
          <button type="button" onClick={() => go('rename')} disabled={busy || !word}
                  className="rounded-md border border-white/10 px-2.5 py-1 text-[11.5px]
                             text-zinc-200 hover:bg-white/5 disabled:opacity-40">
            Rename…
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
          {busy && <p className="text-[11.5px] text-zinc-muted"><Loader2 size={11} className="mr-1 inline animate-spin" />working…</p>}
          {err && <p className="text-[11.5px] text-rose-300"><AlertTriangle size={11} className="mr-1 inline" />{err}</p>}
          {renamed && (
            <div className="space-y-1">
              <p className="text-[11.5px] text-emerald-300">
                <Check size={11} className="mr-1 inline" />
                Renamed in {renamed.length} file{renamed.length === 1 ? '' : 's'}:
              </p>
              {renamed.map((f) => (
                <button key={f} type="button" onClick={() => onJump(f, 1)}
                        className="block w-full truncate text-left font-mono text-[11px]
                                   text-zinc-300 hover:text-violet-200">
                  <CornerDownLeft size={9} className="mr-1 inline" />{f}
                </button>
              ))}
            </div>
          )}
          {sites && !renamed && (
            <div className="space-y-2">
              <p className="text-[11.5px] text-zinc-400">
                {sites.length} site{sites.length === 1 ? '' : 's'} in{' '}
                {new Set(sites.map((s) => s.file)).size} file(s) — comments and
                string literals are excluded.
              </p>
              <div className="flex items-center gap-2">
                <input value={newName} onChange={(e) => setNewName(e.target.value)}
                       placeholder="new name"
                       aria-label="New name"
                       className="min-w-0 flex-1 rounded-md border border-white/10
                                  bg-black/40 px-2 py-1 font-mono text-[12px]
                                  text-zinc-100 outline-none focus:border-violet-400" />
                <button type="button" onClick={doRename}
                        disabled={busy || !newName}
                        className="rounded-md bg-violet-500/80 px-2.5 py-1 text-[11.5px]
                                   font-semibold text-white hover:bg-violet-500
                                   disabled:opacity-40">
                  Rename all
                </button>
              </div>
              <ul className="space-y-0.5">
                {sites.map((s, i) => (
                  <li key={`${s.file}:${s.line}:${i}`}>
                    <button type="button" onClick={() => onJump(s.file, s.line)}
                            className="block w-full truncate text-left font-mono
                                       text-[11px] text-zinc-400 hover:text-violet-200">
                      <span className="text-zinc-600">{s.file.split('/').pop()}:{s.line}</span>{' '}
                      {s.text.slice(0, 90)}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {refs.length > 0 && !sites && !renamed && (
            <ul className="space-y-0.5">
              {refs.map((s, i) => (
                <li key={`${s.file}:${s.line}:${i}`}>
                  <button type="button" onClick={() => onJump(s.file, s.line)}
                          className="block w-full truncate text-left font-mono
                                     text-[11px] text-zinc-400 hover:text-violet-200">
                    <span className="text-zinc-600">{s.file}:{s.line}</span>{' '}
                    {s.text.slice(0, 90)}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── hover ─────────────────────────────────────────────────────────────────── */

export function HoverTip({ x, y, word, signature, doc, kind, onRefs }:
  { x: number; y: number; word: string; signature: string; doc: string;
    kind: string; onRefs: () => void }) {
  return (
    <div role="tooltip"
         style={{ left: Math.min(x, window.innerWidth - 380), top: y + 14 }}
         className="fixed z-40 max-w-[360px] rounded-lg border border-glass-border
                    bg-surface-1 p-2 shadow-lg">
      <p className="flex items-center gap-1.5 font-mono text-[11px] text-emerald-200">
        <span className="rounded bg-white/10 px-1 text-[9px] uppercase text-zinc-400">
          {kind || 'sym'}
        </span>
        {word}
      </p>
      {signature && (
        <p className="mt-1 whitespace-pre-wrap break-words font-mono text-[10.5px] text-zinc-400">
          {signature}
        </p>
      )}
      {doc && (
        <p className="mt-1 whitespace-pre-wrap break-words text-[11px] text-zinc-400">
          {doc}
        </p>
      )}
      <button type="button" onClick={onRefs}
              className="mt-1.5 text-[10.5px] text-violet-300 hover:text-violet-200">
        find references →
      </button>
    </div>
  );
}

/** Debounced hover lookup for a word under the cursor. */
export function useHover(path: string, line: number, col: number, word: string,
                         enabled: boolean) {
  const [tip, setTip] = useState<{ word: string; signature: string; doc: string;
                                   kind: string } | null>(null);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!enabled || !word || !path) { setTip(null); return; }
    const ac = new AbortController();
    const t = window.setTimeout(() => {
      api.codeIntel('hover', { path, name: word }, ac.signal)
        .then((r) => {
          if (!alive.current || ac.signal.aborted) return;
          if (r.found) {
            setTip({ word, signature: String(r.signature ?? ''),
                     doc: String(r.doc ?? ''), kind: String(r.kind ?? '') });
          } else setTip(null);
        })
        .catch(() => {});
    }, 350);
    return () => { ac.abort(); window.clearTimeout(t); };
  }, [path, word, line, col, enabled]);
  return tip;
}

/** The identifier under the editor caret, for go-to-definition. */
export function useWordAtCursor(body: string, line: number, col: number,
                                enabled: boolean): string {
  return useMemo(() => {
    if (!enabled || !body) return '';
    const lines = body.split('\n');
    if (line < 1 || line > lines.length) return '';
    const s = lines[line - 1];
    const c = Math.max(0, Math.min(col, s.length));
    let i = c;
    while (i > 0 && /[\w$]/.test(s[i - 1])) i--;
    let j = c;
    while (j < s.length && /[\w$]/.test(s[j])) j++;
    const w = s.slice(i, j);
    return w.length > 1 && !/^\d+$/.test(w) ? w : '';
  }, [body, line, col, enabled]);
}
