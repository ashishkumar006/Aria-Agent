import { useCallback, useEffect, useMemo, useRef } from 'react';
import * as monaco from 'monaco-editor';
import type { CodeProblem } from '../api';
import type { DiffMarks } from '../lib/diffMarks';
/* Monaco's worker. The specifier must be `monaco-editor/editor/...`, not
   `monaco-editor/esm/vs/...`: the package's exports map is
   `"./*": "./esm/vs/*.js"`, so the shorter form resolves to the real file
   while the longer one double-prefixes to a path that does not exist. With
   the right specifier Vite's worker plugin bundles it and hands back a
   hashed asset URL. */
import EditorWorker from 'monaco-editor/editor/editor.worker?worker';

/* Monaco runs its diff/link/suggest work off the main thread. Without this the
   editor degrades to a broken state the first time a worker is needed. */
(globalThis as { MonacoEnvironment?: unknown }).MonacoEnvironment = {
  getWorker: () => new EditorWorker(),
};
/* ── language ids ────────────────────────────────────────────────────── */

/* Code.tsx speaks the app's own language names (they come from the agent's
   file-type detection); Monaco wants its own ids. Mapped rather than passed
   through so a name change on either side cannot silently disable syntax
   highlighting. */
const LANGS: Record<string, string> = {
  python: 'python',
  typescript: 'typescript',
  javascript: 'javascript',
  json: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  html: 'html',
  css: 'css',
  scss: 'scss',
  markdown: 'markdown',
  md: 'markdown',
  sql: 'sql',
  toml: 'ini',
  shell: 'shell',
  bash: 'shell',
  sh: 'shell',
  powershell: 'powershell',
  go: 'go',
  rust: 'rust',
  java: 'java',
  c: 'c',
  cpp: 'cpp',
  csharp: 'csharp',
  ruby: 'ruby',
  php: 'php',
  dart: 'dart',
  swift: 'swift',
  kotlin: 'kotlin',
  xml: 'xml',
  dockerfile: 'dockerfile',
};

export function editorLanguage(lang: string): string {
  return LANGS[lang] ?? (LANGS[lang.toLowerCase()] ?? 'plaintext');
}

/* Editor operations the rest of the app asks for by name. Each one runs the
   stock Monaco (i.e. VS Code) action rather than a re-implementation, so
   behaviour is identical to the editor a user already knows. */
/* The fold gestures the app can ask for by name. 'cursor'/'expand' are the
   innermost region at the caret, i.e. VS Code's Fold/Unfold. */
export type FoldKind = 'all' | 'none' | 'recursively' | 'level2' | 'cursor' | 'expand';

export type EditorOp =
  | 'dupLine' | 'copyUp' | 'copyDown'
  | 'moveUp' | 'moveDown'
  | 'comment' | 'indent' | 'outdent'
  | 'selectNext' | 'trimTrailing' | 'deleteLine';

const OPS: Record<EditorOp, string> = {
  dupLine: 'editor.action.copyLinesDownAction',
  copyUp: 'editor.action.copyLinesUpAction',
  copyDown: 'editor.action.copyLinesDownAction',
  moveUp: 'editor.action.moveLinesUpAction',
  moveDown: 'editor.action.moveLinesDownAction',
  comment: 'editor.action.commentLine',
  indent: 'editor.action.indentLines',
  outdent: 'editor.action.outdentLines',
  selectNext: 'editor.action.addSelectionToNextFindMatch',
  trimTrailing: 'editor.action.trimTrailingWhitespace',
  deleteLine: 'editor.action.deleteLines',
};

const FOLDS: Record<FoldKind, string> = {
  all: 'editor.foldAll',
  none: 'editor.unfoldAll',
  recursively: 'editor.foldRecursively',
  level2: 'editor.foldLevel2',
  cursor: 'editor.fold',
  expand: 'editor.unfold',
};

/* ── caret + handle ───────────────────────────────────────────────────── */

export interface CaretInfo {
  offset: number;
  line: number;
  col: number;
  selStart: number;
  selEnd: number;
  selLen: number;
}

export interface EditorHandle {
  text(): string;
  selection(): { start: number; end: number };
  caret(): CaretInfo;
  /** Programmatic edit: replaces the whole buffer and parks the caret.
      Fires no `onChange`, so undo history stays owned by the caller. */
  apply(text: string, selStart: number, selEnd: number): void;
  focus(): void;
  blur(): void;
  revealOffset(offset: number): void;
  revealLine(line: number): void;
  /** Select a byte range and bring it into view, without moving focus. */
  selectAndReveal(start: number, end: number): void;
  fold(kind: FoldKind): void;
  setMinimap(on: boolean): void;
  runOp(kind: EditorOp): void;
}

export interface CodeEditorProps {
  body: string;
  lang: string;
  path: string;
  wrap?: boolean;
  /** Server-side problems, 1-based. Rendered as inline markers. */
  problems?: CodeProblem[];
  /** Lines the agent changed, so they read the way they do in Cursor:
   *  green for additions, a red bar at each deletion point. Ranges are
   *  1-based lines of the CURRENT text; the editor clamps them. */
  marks?: DiffMarks | null;
  ariaLabel: string;
  onChange?: (v: string, info: CaretInfo) => void;
  onCursor?: (info: CaretInfo) => void;
  onHandle?: (handle: EditorHandle | null) => void;
  /** Fires when folding changes; the count of collapsed regions, for the
     status bar. Monaco owns folding, so only it can report this. */
  onFoldChange?: (collapsed: number) => void;
}

/* ── theme ───────────────────────────────────────────────────────────── */

/* The editor once wore the dashboard's palette — navy #0a0c13, a violet
   cursor and pastel tokens — while `.ide` chrome had moved to graphite +
   editor blue. Inside the same window that read as two apps stacked. The
   editor now shares the app's surface-1/surface-2 tokens (#10131c /
   #171c28), so the Code view sits in the same window as every other
   section, while the cursor stays VS Code's own #aeafad, selection is
   #264f78, and the token colours are the real ones (blue keywords,
   orange-red strings) instead of imitations. The minimap shares the editor
   background, which also kills the odd dark rectangle it drew in the
   corner of an empty file. */
const ARIA_THEME = {
  base: 'vs-dark' as const,
  inherit: true,
  rules: [
    { token: 'comment', foreground: '6a9955', fontStyle: 'italic' },
    { token: 'keyword', foreground: '569cd6' },
    { token: 'number', foreground: 'b5cea8' },
    { token: 'string', foreground: 'ce9178' },
    { token: 'type', foreground: '4ec9b0' },
  ],
  colors: {
    'editor.background': '#10131c',
    'editor.foreground': '#cccccc',
    'editorLineNumber.foreground': '#6e7681',
    'editorLineNumber.activeForeground': '#cccccc',
    'editorCursor.foreground': '#aeafad',
    'editor.selectionBackground': '#264f78',
    'editor.inactiveSelectionBackground': '#3a3d41',
    'editor.lineHighlightBackground': '#ffffff0a',
    'editor.lineHighlightBorder': '#00000000',
    'editorGutter.background': '#10131c',
    'editorWidget.background': '#171c28',
    'editorWidget.foreground': '#cccccc',
    'editorWidget.border': '#2b3448',
    'editorSuggestWidget.background': '#171c28',
    'editorSuggestWidget.selectedBackground': '#1e3a5f',
    'editorHoverWidget.background': '#171c28',
    'editorStickyScroll.background': '#10131c',
    'editorStickyScrollHover.background': '#1e2435',
    'scrollbarSlider.background': '#4e5a7280',
    'scrollbarSlider.hoverBackground': '#64708ab3',
    'scrollbarSlider.activeBackground': '#bfc8da66',
    'minimap.background': '#10131c',
    'editorOverviewRuler.border': '#00000000',
    'editorIndentGuide.background': '#2a3448',
    'editorIndentGuide.background1': '#2a3448',
    'editorIndentGuide.activeBackground': '#55627a',
    'editorIndentGuide.activeBackground1': '#55627a',
    'editorBracketMatch.background': '#0064001a',
    'editorBracketMatch.border': '#888888',
  },
};

monaco.editor.defineTheme('aria-dark', ARIA_THEME);

/* ── component ──────────────────────────────────────────────────────── */

export default function MonacoEditor({
  body, lang, path, wrap = false, problems = [], marks = null, ariaLabel,
  onChange, onCursor, onHandle, onFoldChange,
}: CodeEditorProps) {
  const host = useRef<HTMLDivElement | null>(null);
  const edRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const modelRef = useRef<monaco.editor.ITextModel | null>(null);
  /* True while the component itself mutates the model, so its own change
     event does not round-trip as user input (which would double-commit undo
     snapshots and fight the caller's history). */
  const applying = useRef(false);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const onCursorRef = useRef(onCursor);
  onCursorRef.current = onCursor;
  const onFoldChangeRef = useRef(onFoldChange);
  onFoldChangeRef.current = onFoldChange;
  /* Monotonic token: a fold recount that resolves late must not
     overwrite the result of a newer one. */
  const foldSeq = useRef(0);
  /* One collection per editor for the agent's change marks; `set` replaces
     its contents wholesale, which is also how they are cleared. */
  const marksCol = useRef<monaco.editor.IEditorDecorationsCollection | null>(null);

  const caret = useCallback((): CaretInfo => {
    const ed = edRef.current;
    const model = modelRef.current;
    if (!ed || !model) {
      return { offset: 0, line: 1, col: 1, selStart: 0, selEnd: 0, selLen: 0 };
    }
    const sel = ed.getSelection();
    if (!sel) {
      return { offset: 0, line: 1, col: 1, selStart: 0, selEnd: 0, selLen: 0 };
    }
    const start = model.getOffsetAt(sel.getStartPosition());
    const end = model.getOffsetAt(sel.getEndPosition());
    const pos = sel.getStartPosition();
    return {
      offset: start, line: pos.lineNumber, col: pos.column,
      selStart: start, selEnd: end, selLen: end - start,
    };
  }, []);

  /* How many regions are collapsed. Read from the folding model itself.
     Counting gaps between visible ranges cannot see a fold whose hidden
     block runs to the last line: folding always keeps a region's first line
     visible, so that fold leaves no trailing gap to detect and the count
     comes out one short. The model knows; the heuristic guesses. */
  const reportFoldCount = useCallback((): void => {
    const ed = edRef.current;
    if (!ed) { onFoldChangeRef.current?.(0); return; }
    const seq = (foldSeq.current += 1);
    const fc = ed.getContribution('editor.contrib.folding') as unknown as
      { getFoldingModel: () => Promise<
          { regions: { length: number; isCollapsed: (i: number) => boolean } } | null> } | null;
    if (!fc) { onFoldChangeRef.current?.(0); return; }
    void fc.getFoldingModel().then((fm) => {
      if (seq !== foldSeq.current) return;
      let n = 0;
      if (fm) {
        const regions = fm.regions;
        for (let i = 0; i < regions.length; i += 1) {
          if (regions.isCollapsed(i)) n += 1;
        }
      }
      onFoldChangeRef.current?.(n);
    }).catch(() => { /* no folding model yet: keep the last count */ });
  }, []);

  /* Create the editor once, tear it down on unmount (and on a StrictMode
     remount, which would otherwise leave two live editors on one host). */
  useEffect(() => {
    if (!host.current) return undefined;
    const ed = monaco.editor.create(host.current, {
      theme: 'aria-dark',
      automaticLayout: true,
      readOnly: false,
      contextmenu: false,
      // VS Code's own editor behaviours: no re-implementation.
      autoClosingBrackets: 'always',
      autoClosingQuotes: 'always',
      autoSurround: 'languageDefined',
      bracketPairColorization: { enabled: true },
      guides: { bracketPairs: true, indentation: true },
      stickyScroll: { enabled: true, maxLineCount: 4 },
      folding: true,
      foldingHighlight: true,
      showFoldingControls: 'always',
      minimap: { enabled: true, size: 'proportional' },
      occurrencesHighlight: 'singleFile',
      selectionHighlight: true,
      renderWhitespace: 'selection',
      roundedSelection: true,
      smoothScrolling: true,
      cursorBlinking: 'smooth',
      cursorSmoothCaretAnimation: 'on',
      multiCursorModifier: 'ctrlCmd',
      wordWrap: 'off',
      scrollBeyondLastLine: false,
      renderLineHighlight: 'all',
      lineNumbersMinChars: 4,
      glyphMargin: false,
      lineDecorationsWidth: 6,
      padding: { top: 8, bottom: 40 },
      fontFamily: '"JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 12.5,
      lineHeight: 20,
      tabSize: 4,
      insertSpaces: true,
      detectIndentation: true,
      fixedOverflowWidgets: true,
      scrollbar: { alwaysConsumeMouseWheel: false, vertical: 'visible' },
      ariaLabel,
    });
    edRef.current = ed;

    const subs: monaco.IDisposable[] = [
      ed.onDidChangeModelContent(() => {
        if (applying.current) return;
        onChangeRef.current?.(ed.getValue(), caret());
      }),
      ed.onDidChangeCursorPosition(() => onCursorRef.current?.(caret())),
      ed.onDidChangeCursorSelection(() => onCursorRef.current?.(caret())),
      ed.onDidChangeHiddenAreas(() => reportFoldCount()),
    ];

    return () => {
      for (const s of subs) s.dispose();
      marksCol.current?.clear();
      marksCol.current = null;
      ed.dispose();
      edRef.current = null;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  /* One model per file: the model owns undo, folding, markers and the
     language, so reusing one across files would carry them all over. */
  useEffect(() => {
    const old = modelRef.current;
    if (old && old.uri.path === `/${path}`) return;
    const model = monaco.editor.createModel(
      body, editorLanguage(lang), monaco.Uri.parse(`inmemory://aria/${path}`));
    modelRef.current = model;
    edRef.current?.setModel(model);
    if (old) old.dispose();
    onFoldChangeRef.current?.(0);
  }, [path]); // eslint-disable-line react-hooks/exhaustive-deps

  /* Language follows the file; changing it re-tokenises in place. */
  useEffect(() => {
    if (modelRef.current) monaco.editor.setModelLanguage(modelRef.current, editorLanguage(lang));
  }, [lang]);

  useEffect(() => {
    edRef.current?.updateOptions({ wordWrap: wrap ? 'on' : 'off' });
  }, [wrap]);

  /* Server problems become inline markers. */
  useEffect(() => {
    const model = modelRef.current;
    if (!model) return;
    monaco.editor.setModelMarkers(model, 'aria', problems.map((p) => ({
      startLineNumber: Math.max(1, p.line),
      startColumn: Math.max(1, p.col),
      endLineNumber: Math.max(1, p.line),
      endColumn: Math.max(1, p.col) + 1,
      message: p.message,
      severity:
        p.severity === 'error' ? monaco.MarkerSeverity.Error
          : p.severity === 'warning' ? monaco.MarkerSeverity.Warning
            : monaco.MarkerSeverity.Info,
    })));
  }, [problems]);

  /* Push the authoritative buffer in when it changed behind the editor's
     back (the caller's programmatic edits). Typing is excluded because the
     caller's value is then a copy of ours, which keeps the caret alive.
     Declared BEFORE the marks effect on purpose: effects run in declaration
     order, and after an agent write both run in the same commit - if the
     marks were computed first they would be measured against the stale
     model, clamped away, and (the model having changed later) never
     recomputed. */
  useEffect(() => {
    const ed = edRef.current;
    const model = modelRef.current;
    if (!ed || !model || applying.current) return;
    if (model.getValue() === body) return;
    applying.current = true;
    model.setValue(body);
    applying.current = false;
  }, [body]);

  /* The agent's changes, Cursor-style: green for a line it added, yellow for
     one it edited (a replacement), red at a deletion point (the removed text
     itself is not in this file, so there is nothing else to colour). Runs on
     body too: after a reload the marks are recomputed against the new line
     numbering. */
  useEffect(() => {
    const ed = edRef.current;
    const model = modelRef.current;
    if (!ed) return;
    if (!marksCol.current) marksCol.current = ed.createDecorationsCollection([]);
    const max = model?.getLineCount() ?? 0;
    const decos: monaco.editor.IModelDeltaDecoration[] = [];
    for (const l of marks?.added ?? []) {
      if (l < 1 || l > max) continue;
      decos.push({
        range: new monaco.Range(l, 1, l, 1),
        options: {
          isWholeLine: true,
          className: 'aria-agent-add',
          linesDecorationsClassName: 'aria-agent-add-gutter',
          hoverMessage: { value: 'Added by the agent — review, then Keep or Revert' },
        },
      });
    }
    for (const l of marks?.modified ?? []) {
      if (l < 1 || l > max) continue;
      decos.push({
        range: new monaco.Range(l, 1, l, 1),
        options: {
          isWholeLine: true,
          className: 'aria-agent-mod',
          linesDecorationsClassName: 'aria-agent-mod-gutter',
          hoverMessage: { value: 'Edited by the agent — review, then Keep or Revert' },
        },
      });
    }
    for (const l of marks?.removedAt ?? []) {
      if (l < 1 || l > max) continue;
      decos.push({
        range: new monaco.Range(l, 1, l, 1),
        options: {
          linesDecorationsClassName: 'aria-agent-del-gutter',
          hoverMessage: { value: 'The agent removed lines here' },
        },
      });
    }
    marksCol.current.set(decos);
  }, [marks, path, body]);

  const handle: EditorHandle = useMemo(() => ({
    text: () => modelRef.current?.getValue() ?? '',
    selection: () => {
      const c = caret();
      return { start: c.selStart, end: c.selEnd };
    },
    caret,
    apply(text, selStart, selEnd) {
      const ed = edRef.current;
      const model = modelRef.current;
      if (!ed || !model) return;
      applying.current = true;
      model.setValue(text);
      applying.current = false;
      const p = model.getPositionAt(Math.min(selStart, text.length));
      const q = model.getPositionAt(Math.min(selEnd, text.length));
      ed.setSelection({
        startLineNumber: p.lineNumber, startColumn: p.column,
        endLineNumber: q.lineNumber, endColumn: q.column,
      });
      ed.focus();
      onCursorRef.current?.(caret());
    },
    focus: () => edRef.current?.focus(),
    blur: () => {
      const t = edRef.current?.getContainerDomNode()?.querySelector('textarea');
      if (t instanceof HTMLTextAreaElement) t.blur();
    },
    revealOffset(offset) {
      const ed = edRef.current;
      const model = modelRef.current;
      if (!ed || !model) return;
      const p = model.getPositionAt(Math.max(0, offset));
      ed.setPosition(p);
      ed.revealLineInCenter(p.lineNumber);
      ed.focus();
    },
    revealLine(line) {
      const ed = edRef.current;
      if (!ed) return;
      ed.revealLineInCenter(Math.max(1, line));
      ed.setPosition({ lineNumber: Math.max(1, line), column: 1 });
      ed.focus();
    },
    selectAndReveal(start, end) {
      const ed = edRef.current;
      const model = modelRef.current;
      if (!ed || !model) return;
      const p = model.getPositionAt(Math.max(0, start));
      const q = model.getPositionAt(Math.max(0, end));
      ed.setSelection({
        startLineNumber: p.lineNumber, startColumn: p.column,
        endLineNumber: q.lineNumber, endColumn: q.column,
      });
      ed.revealLineInCenter(p.lineNumber);
      onCursorRef.current?.(caret());
    },
    fold(kind) {
      const ed = edRef.current;
      if (!ed) return;
      ed.trigger('aria', FOLDS[kind], undefined);
    },
    setMinimap(on) {
      const ed = edRef.current;
      if (!ed) return;
      ed.updateOptions({ minimap: { enabled: on, size: 'proportional' } });
    },
    runOp(kind) {
      const ed = edRef.current;
      if (!ed) return;
      const id = OPS[kind];
      ed.focus();
      ed.trigger('aria', id, undefined);
    },
  }), [caret]);

  /* Publish the handle to the parent once. The object is stable (every
     method closes over refs), so the parent can hold it in a ref without
     re-rendering on each publish. */
  const publish = useRef(onHandle);
  publish.current = onHandle;
  useEffect(() => {
    publish.current?.(handle);
    return () => publish.current?.(null);
  }, [handle]);

  return (
    /* No role or aria-label here: Monaco renders its own textarea and takes
       `ariaLabel` from its construction options, so labelling this wrapper
       as well would put two textboxes in the accessibility tree. */
    /* The host div is the containment anchor the parent's window-level
       keydown handler uses to ask "is the focus in the editor?".
       `data-editor-host` matches Code.tsx's ED_HOST selector. */
    <div data-editor-host="" ref={host} className="absolute inset-0" />
  );
}
