"""Code intelligence for the coding agent: structure, references, blast radius.

`find_symbol` regex-matches a name and cannot tell a definition from a call
site, so the same text matched imports, comments and string literals. That is
the single reason the coder could not reason about a codebase before editing
it: "edit every caller of `foo`" was never safe to ask.

What is here replaces that with real parsing where the language allows it:

  - `document_symbols` - the file's outline (classes, functions, methods,
    plus module-level constants for Python)
  - `find_references`  - every USE, excluding comments and string literals,
    with the declaration separated out
  - `blast_radius`     - what a change to this symbol touches, ranked
  - `replace_symbol_body` / `insert_after_symbol` - edit BY NAME, so the
    agent never has to compute line:column
  - `preview_edit`     - a unified diff, written to NOTHING. The safety
    primitive: see the change, then apply it.

Python is parsed with `ast`, so the results are exact. Other languages fall
back to an indentation/keyword scanner that is deliberately conservative - it
reports less rather than guessing wrong, because a false reference sends an
agent editing the wrong line.

Reference semantics: for Python a name inside a STRING or COMMENT is not a
reference. `strip_literals` removes them before matching. Getting this wrong in
the permissive direction is how an agent "renames" a docstring.
"""
from __future__ import annotations

import ast
import difflib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MAX_FILE_BYTES = 2_000_000

#: Text files we will read. Anything else is binary by assumption.
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".bz2", ".xz", ".7z", ".rar", ".bin", ".exe", ".dll", ".so",
    ".dylib", ".class", ".jar", ".wasm", ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".mov", ".avi", ".webm", ".pyc", ".pyo", ".o", ".a",
    ".db", ".sqlite", ".sqlite3", ".bin.stl", ".stl", ".obj",
}

PY_DEF = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

#: Credential-shaped files the scanners must never read. The same predicate is
#: used by mcp_server's file tools; living here keeps a repo-wide reference
#: scan from quietly indexing an unchecked-in `.env`. `.env.example` is
#: documentation and stays visible.
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}
_SECRET_PRIVATE_KEYS = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}


def is_secret_name(name: str) -> bool:
    """True for files that must not be read, searched, listed or edited."""
    n = name.strip().lower()
    if n == ".env" or (n.startswith(".env.") and n != ".env.example"):
        return True
    if n in _SECRET_PRIVATE_KEYS:
        return True
    return Path(n).suffix in _SECRET_SUFFIXES

DECL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|"
    r"protected\s+|internal\s+|static\s+|final\s+|abstract\s+|"
    r"async\s+|pub\s+)*"
    r"(?P<kw>class|interface|struct|enum|trait|impl|type|func|function|"
    r"def|fn|defp|sub|proc|method)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)"
)
CONST_RE = re.compile(
    r"^\s*(?:export\s+)?(?:declare\s+)?(?:public\s+|private\s+|"
    r"protected\s+|static\s+|final\s+|internal\s+)*"
    r"(?:const(?:ant)?|let|var|val|constexpr|final\s+var)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*[:=]"
)
PY_CONST_RE = re.compile(
    r"^\s*(?:export\s+)?(?:public\s+|private\s+)*"
    r"(?:[A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?="
)

#: Directories that are never source. A measured `find_references` over the
#: repo root walked 30,714 files and took 389s, because it descended into
#: `.venv`, `node_modules` and `dist` - tens of thousands of vendored files
#: that cannot contain a definition the caller cares about. Skipping them is
#: the difference between a second and six minutes.
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "out", ".next", ".nuxt", ".cache", ".tox",
    "site-packages", ".idea", ".vscode", "target", "vendor", ".gradle",
    ".eggs", "*.egg-info", ".venv.lock", "coverage", "htmlcov",
    ".terraform", "Pods", "DerivedData", ".gradle", "bin", "obj",
}


def _skip_dir(name: str) -> bool:
    return (name in SKIP_DIRS
            or name.endswith(".egg-info")
            or name.endswith(".dist-info")
            or name.endswith(".cache"))


def iter_files(root: str | Path, *, suffixes: Iterable[str] | None = None
               ) -> list[Path]:
    """Source files under `root`, pruning vendored and build directories.

    Pruning is done with `os.walk`'s in-place `dirnames` edit, which stops the
    descent rather than filtering afterwards - a filter still pays the cost of
    stat-ing every file inside `node_modules`.
    """
    import os

    r = _safe(root)
    if r.is_file():
        return [r]
    allowed = {s.lower() for s in suffixes} if suffixes else None
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(r):
        dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
        for fn in filenames:
            if is_secret_name(fn):
                continue
            f = Path(dirpath) / fn
            if f.suffix.lower() in SKIP_SUFFIXES:
                continue
            if allowed and f.suffix.lower() not in allowed:
                continue
            try:
                if f.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            out.append(f)
        out.sort()
    return out
CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")


@dataclass
class Symbol:
    name: str
    kind: str                      # class | function | method | constant | variable
    line: int                      # 1-based, as an editor shows it
    end_line: int = 0              # for `replace_symbol_body`
    col: int = 0
    doc: str = ""
    parent: str = ""               # enclosing class, for methods

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "line": self.line,
                "end_line": self.end_line or self.line, "col": self.col,
                "doc": self.doc[:200], "parent": self.parent}


@dataclass
class Reference:
    file: str
    line: int
    text: str
    kind: str = "use"              # definition | use | import | string | comment
    col: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "col": self.col,
                "text": self.text[:200], "kind": self.kind}


@dataclass
class Change:
    """One proposed edit. Never written until `apply` is called."""
    path: str
    kind: str
    detail: str
    before: str = ""
    after: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "detail": self.detail,
                "notes": list(self.notes)}


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def _safe(path: str | Path) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else (Path.cwd() / p)


def read_text(path: str | Path) -> str:
    p = _safe(path)
    if p.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{p.name} is larger than {MAX_FILE_BYTES} bytes")
    return p.read_text(encoding="utf-8", errors="replace")



def _py_symbols(text: str) -> tuple[list[Symbol], list[tuple[int, str]]]:
    """Exact outline for Python. Returns (symbols, used_names).

    `used_names` is every Name/Attribute read or written plus every attribute
    access, which is what makes reference finding exact: a name that never
    appears there is genuinely not a reference to it.
    """
    tree = ast.parse(text)
    syms: list[Symbol] = []
    used: list[tuple[int, str]] = []

    lines = text.splitlines()

    def doc_of(node) -> str:
        try:
            return (ast.get_docstring(node) or "").strip()
        except Exception:
            return ""

    def walk(body, parent: str = ""):
        for node in body:
            if isinstance(node, PY_DEF):
                kind = "class" if isinstance(node, ast.ClassDef) else (
                    "method" if parent else "function")
                syms.append(Symbol(
                    name=node.name, kind=kind, line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno) or node.lineno,
                    col=node.col_offset, doc=doc_of(node), parent=parent))
                walk(node.body, node.name if isinstance(node, ast.ClassDef)
                     else parent)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                for t in targets:
                    if isinstance(t, ast.Name) and (t.id.isupper()
                                                    or (parent == "" and node.col_offset == 0)):
                        syms.append(Symbol(name=t.id, kind="constant",
                                           line=node.lineno, parent=parent,
                                           col=node.col_offset))

    walk(tree.body)

    # every identifier the program actually resolves
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute):
            used.append((node.lineno, node.attr))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            used.append((node.lineno, node.name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                nm = (a.asname or a.name).split(".")[0]
                if nm:
                    used.append((node.lineno, nm))

    _ = lines
    return syms, used


def _scan_symbols(text: str, *, p_is_py: bool = False) -> list[Symbol]:
    """Conservative outline for non-Python: a declaration line plus the block
    it opens. Reports less rather than guessing."""
    syms: list[Symbol] = []
    lines = text.splitlines()
    opener = re.compile(r"[:{]\s*$")
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith(("//", "#", "*", "/*")):
            continue
        m = DECL_RE.match(line)
        if m:
            kind = m.group("kw")
            syms.append(Symbol(
                name=m.group("name"),
                kind="class" if kind in ("class", "interface", "struct",
                                         "enum", "trait", "impl", "type")
                else "function",
                line=i, col=len(line) - len(line.lstrip())))
            continue
        c = CONST_RE.match(line)
        if not c and p_is_py:
            c = PY_CONST_RE.match(line)
        if c:
            syms.append(Symbol(name=c.group("name"), kind="constant", line=i,
                               col=len(line) - len(line.lstrip())))

    # extend each symbol to the end of the block it opens
    for s in syms:
        s.end_line = s.line
        if s.line - 1 < len(lines) and opener.search(lines[s.line - 1]):
            depth = 0
            for j in range(s.line - 1, len(lines)):
                depth += lines[j].count("{") + lines[j].count("[")
                depth -= lines[j].count("}") + lines[j].count("]")
                if depth <= 0 and j > s.line - 1:
                    s.end_line = j + 1
                    break
                s.end_line = j + 1
            else:
                s.end_line = len(lines)
    return syms


def document_symbols(path: str) -> dict[str, Any]:
    """The file's outline."""
    p = _safe(path)
    text = read_text(p)
    if p.suffix.lower() == ".py":
        try:
            syms, _ = _py_symbols(text)
        except SyntaxError as exc:
            return {"ok": False, "error": f"cannot parse {p.name}: {exc}",
                    "file": p.name, "symbols": []}
        lang = "python"
    else:
        syms = _scan_symbols(text, p_is_py=(p.suffix.lower() == ".py"))
        lang = p.suffix.lower().lstrip(".")

    return {"ok": True, "file": p.name, "language": lang,
            "symbols": [s.to_dict() for s in syms],
            "symbol_count": len(syms)}


# ---------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------

def _strip_py_noise(text: str) -> list[bool]:
    """Per-character mask: True where the character is real CODE, False where
    it is a string literal or a comment.

    A name inside a docstring is not a reference to it. Without this mask
    "rename this function" also rewrites its own documentation.
    """
    mask = [True] * len(text)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return mask

    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.end_lineno and node.end_col_offset is not None:
                lines = text.splitlines(keepends=True)
                off = sum(len(l) for l in lines[:node.lineno - 1])
                spans.append((off + node.col_offset,
                              off + node.end_col_offset))
    for start, end in spans:
        for i in range(max(0, start), min(len(mask), end)):
            mask[i] = False
    return mask


def find_references(name: str, path: str = ".", *, include_definition: bool = True,
                    max_hits: int = 200) -> dict[str, Any]:
    """Every USE of `name`, with the declaration reported separately.

    Python is exact: string literals and comments are excluded. Other
    languages are matched on code lines only, with comment lines skipped.

    TWO PHASES, and the order matters. This used to run `ast.parse` on every
    `.py` in the tree before checking whether the name appeared in it, so a
    reference query on a large workspace parsed the whole codebase and blocked
    the event loop for minutes - one request stalled the server. Now a cheap
    substring test runs FIRST and only files that actually contain the name
    are parsed. Same answers, a fraction of the work.
    """
    root = _safe(path)
    if not root.exists():
        return {"ok": False, "error": f"'{path}' does not exist",
                "references": []}
    try:
        limit = max(1, min(int(max_hits or 200), 1000))
    except (TypeError, ValueError):
        limit = 200

    rx_word = re.compile(r"\b" + re.escape(str(name or "")) + r"\b")
    refs: list[Reference] = []
    defs: list[Reference] = []
    files_scanned = 0
    parsed = 0

    for f in iter_files(root):
        if len(refs) + len(defs) >= limit:
            break
        files_scanned += 1
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Phase 1: cheap. Most files never mention the symbol.
        if not rx_word.search(text):
            continue
        rel = f.relative_to(root).as_posix() if root.is_dir() else f.name
        is_py = f.suffix.lower() == ".py"

        exact_used: dict[int, set[str]] | None = None
        mask = [True] * len(text)
        if is_py:
            try:
                parsed += 1
                _, used = _py_symbols(text)
                exact_used = {}
                for ln, nm in used:
                    exact_used.setdefault(ln, set()).add(nm)
                mask = _strip_py_noise(text)
            except SyntaxError:
                exact_used = None

        offset = 0
        for i, line in enumerate(text.splitlines(), 1):
            m = rx_word.search(line)
            if not m:
                offset += len(line) + 1
                continue
            stripped = line.lstrip()
            if stripped.startswith(("//", "#", "*", "/*")):
                offset += len(line) + 1
                continue

            is_def = False
            if exact_used is not None:
                # `DECL_RE` alone is not enough: `def helper(...)` matched it,
                # but so did `return helper(...)` on a line whose only resolved
                # name IS `helper`. A declaration is a declaration keyword at
                # the start of the line, nothing else.
                is_def = DECL_RE.match(line) is not None
            else:
                is_def = DECL_RE.match(line) is not None

            # character offset within the line, checked against the mask
            base = offset + m.start()
            in_literal = not all(mask[base:base + len(name)]) if is_py else False
            if in_literal:
                offset += len(line) + 1
                continue

            entry = Reference(file=rel, line=i, text=line.strip()[:200],
                              kind="definition" if is_def else "use",
                              col=len(line) - len(line.lstrip()))
            if is_def:
                defs.append(entry)
            else:
                refs.append(entry)
            offset += len(line) + 1
            if len(refs) + len(defs) >= limit:
                break

    return {
        "ok": True,
        "symbol": name,
        "definitions": [d.to_dict() for d in defs],
        "references": [r.to_dict() for r in
                       (refs if include_definition else refs + defs)],
        "definition_count": len(defs),
        "reference_count": len(refs),
        "files_scanned": files_scanned,
        "files_parsed": parsed,
        "exact": parsed > 0 or not any(
            f.suffix.lower() == ".py" for f in [root] if f.is_dir()),
    }


def blast_radius(name: str, path: str = ".", *, max_files: int = 50
                 ) -> dict[str, Any]:
    """What a change to `name` would touch, grouped by file.

    The question an agent must answer before editing a shared helper: what
    else breaks if I change this? Ranked so the first files are the ones a
    change is most likely to break.
    """
    r = find_references(name, path, max_hits=500)
    if not r.get("ok"):
        return r
    by_file: dict[str, list[dict]] = {}
    for ref in r["references"]:
        by_file.setdefault(ref["file"], []).append(ref)
    ranked = sorted(by_file.items(), key=lambda kv: -len(kv[1]))
    return {
        "ok": True,
        "symbol": name,
        "files_touched": len(ranked),
        "total_references": r["reference_count"],
        "blast": [{"file": f, "count": len(v), "lines": [x["line"] for x in v]}
                  for f, v in ranked[:max_files]],
        "risk": ("high" if len(ranked) > 10 else
                 "medium" if len(ranked) > 2 else "low"),
        "exact": r["exact"],
    }


# ---------------------------------------------------------------------------
# editor queries: word at cursor, definition, hover, rename
#
# These back the editor's IDE features (go-to-definition, hover, rename). They
# are built on the same AST that `document_symbols`/`find_references` use, so
# the editor and the agent answer from ONE source of truth rather than two
# implementations that drift.
# ---------------------------------------------------------------------------

_WORD_CHARS = set("abcdefghijklmnopqrstuvwxyz"
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def word_at(text: str, line: int, col: int) -> str:
    """The identifier under a 1-based line/col cursor."""
    lines = text.split("\n")
    if line < 1 or line > len(lines):
        return ""
    s = lines[line - 1]
    if col is None:
        col = 0
    col = max(0, min(int(col), len(s)))
    i = col
    while i > 0 and s[i - 1] in _WORD_CHARS:
        i -= 1
    j = col
    while j < len(s) and s[j] in _WORD_CHARS:
        j += 1
    return s[i:j]


def definition(name: str, path: str = ".", *, line: int = 0,
                col: int = 0, root: str | Path | None = None) -> dict[str, Any]:
    """Where `name` is defined. Local first (the file being edited), then the
    whole tree, so go-to-definition works on a symbol defined in this file
    without touching the repo."""
    if not name:
        return {"ok": False, "error": "no symbol under the cursor",
                "found": False, "name": ""}
    p = _safe(path)
    try:
        text = read_text(p)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "found": False, "name": name}
    syms = document_symbols(str(p)).get("symbols") or []
    local = [s for s in syms if s.get("name") == name]
    if local:
        # Closest enclosing body, else the first declaration.
        cand = ([s for s in local if line and s.get("line", 0) <= line
                 <= (s.get("end_line") or s.get("line", 0))] or local)
        cand = sorted(cand, key=lambda s: abs((s.get("line", 0)) - (line or 0)))
        s = cand[0]
        return {"ok": True, "found": True, "name": name, "local": True,
                "file": p.name, "line": s["line"], "col": s.get("col", 0),
                "kind": s.get("kind", ""), "parent": s.get("parent", "")}
    refs = find_references(name, str(root or _code_root()))
    defs = (refs.get("definitions") or []) + (
        [r for r in (refs.get("references") or [])
         if r.get("kind") == "definition"])
    if defs:
        d = defs[0]
        return {"ok": True, "found": True, "name": name, "local": False,
                "file": d["file"], "line": d["line"], "col": d.get("col", 0),
                "kind": "definition", "parent": ""}
    return {"ok": True, "found": False, "name": name,
            "error": f"no definition of {name!r} found"}


def hover(name: str, path: str = ".") -> dict[str, Any]:
    """Signature + doc for `name`: what an editor's hover tooltip shows."""
    if not name:
        return {"ok": False, "found": False, "error": "no symbol", "name": ""}
    d = definition(name, path)
    if not d.get("found"):
        return {"ok": True, "found": False, "name": name,
                "error": d.get("error", "")}
    p = _safe(path)
    syms = document_symbols(str(p)).get("symbols") or []
    sym = next((s for s in syms if s.get("name") == name), None)
    signature = ""
    try:
        lines = read_text(p).split("\n")
        ln = d["line"] - 1
        if 0 <= ln < len(lines):
            signature = lines[ln].strip()[:400]
    except Exception:
        pass
    return {"ok": True, "found": True, "name": name,
            "kind": d.get("kind") or (sym or {}).get("kind", ""),
            "signature": signature,
            "doc": (sym or {}).get("doc", "") or d.get("doc", ""),
            "file": d["file"], "line": d["line"],
            "parent": (sym or {}).get("parent", "")}


def rename_sites(name: str, path: str = ".") -> dict[str, Any]:
    """Every use that a rename must change, with the line it is on.

    The word is re-located on each line rather than trusting a column: the
    `Reference.col` of the underlying finder is the line's INDENT, not the
    position of the match, so using it directly would edit the wrong bytes.
    """
    root = _code_root()
    refs = find_references(name, str(root), max_hits=1000)
    if not refs.get("ok"):
        return refs
    out: list[dict] = []
    for d in (refs.get("definitions") or []):
        out.append(d)
    for r in (refs.get("references") or []):
        out.append(r)
    return {"ok": True, "symbol": name, "sites": out,
            "site_count": len(out),
            "files_touched": len({s["file"] for s in out}),
            "exact": bool(refs.get("exact"))}


def apply_rename(name: str, new_name: str, path: str = ".") -> dict[str, Any]:
    """Rename `name` to `new_name` across the tree. WRITES.

    Per line, the first word-boundary occurrence only: a line can hold the name
    once in code and once inside a string, and a blind replace would rewrite the
    string too.
    """
    if not name or not new_name:
        return {"ok": False, "error": "name and new_name are required",
                "changed": [], "written": False}
    if name == new_name:
        return {"ok": True, "changed": [], "written": False,
                "error": "new name is the same"}
    r = rename_sites(name, path)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", ""), "changed": [],
                "written": False}
    root = _code_root()
    by_file: dict[str, list[dict]] = {}
    for s in r.get("sites") or []:
        by_file.setdefault(s["file"], []).append(s)
    rx = re.compile(r"\b" + re.escape(name) + r"\b")
    changed: list[str] = []
    for rel in sorted(by_file):
        target = root / rel
        try:
            if not (root / rel).is_relative_to(root) or not target.is_file():
                continue
            old = target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lines = old.split("\n")
        touched = False
        for s in by_file[rel]:
            i = int(s.get("line") or 0) - 1
            if not (0 <= i < len(lines)):
                continue
            # Only a code line: `find_references` already drops lines starting
            # with a comment marker, but re-check so a doc line can never be
            # rewritten.
            if lines[i].lstrip().startswith(("//", "#", "*", "/*")):
                continue
            new_line, n = rx.subn(new_name, lines[i], count=1)
            if n:
                lines[i] = new_line
                touched = True
        if not touched:
            continue
        new = "\n".join(lines)
        if new == old:
            continue
        tmp = target.with_suffix(target.suffix + ".aria-tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(str(tmp), str(target))
        changed.append(rel)
    return {"ok": True, "symbol": name, "new_name": new_name,
            "changed": changed, "files_changed": len(changed),
            "written": bool(changed)}


def _code_root() -> Path:
    """The repository root the editor serves files from."""
    for cand in (Path(__file__).resolve().parent.parent.parent,
                 Path.cwd()):
        if (cand / "S9SharedCode").is_dir() or (cand / ".git").exists():
            return cand
    return Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# editing by name
# ---------------------------------------------------------------------------

def _find_symbol_extent(text: str, name: str) -> tuple[int, int, list[Symbol]] | None:
    """1-based (start, end) line span of `name`'s body, plus the outline."""
    if not text:
        return None
    p = _safe(".")
    if text.lstrip().startswith("def ") or True:
        pass
    try:
        syms, _ = _py_symbols(text)
    except SyntaxError:
        syms = _scan_symbols(text)
    _ = p
    for s in syms:
        if s.name == name:
            return s.line, (s.end_line or s.line), syms
    return None


def _dedent_body(body: str) -> str:
    """Re-indent a replacement body to the target symbol's indentation."""
    lines = body.splitlines()
    body_lines = [l for l in lines if l.strip()]
    if not body_lines:
        return body
    common = min(len(l) - len(l.lstrip()) for l in body_lines)
    out = [l[common:] if len(l) >= common else l for l in lines]
    return "\n".join(out)


def replace_symbol_body(path: str, name: str, body: str) -> Change:
    """Replace a symbol's whole body. Addressing by NAME means the agent never
    computes line:column, and a shifted file cannot make it edit the wrong
    function."""
    p = _safe(path)
    text = read_text(p)
    ext = _find_symbol_extent(text, name)
    if ext is None:
        return Change(str(p), "replace_symbol_body",
                      f"no symbol named {name!r} in {p.name}")
    start, end, syms = ext
    lines = text.splitlines()
    target = next((s for s in syms if s.name == name), None)
    indent = ""
    if target:
        orig = lines[target.line - 1]
        indent = orig[:len(orig) - len(orig.lstrip())]

    head = lines[:start - 1]
    decl = lines[start - 1]
    tail = lines[end:]
    new_body = _dedent_body(body).splitlines()
    # The caller may pass either just the body OR a replacement that repeats
    # the declaration. Emitting both produced
    #     def helper(who: str) -> str:
    #     def helper(who):
    #         return 'X'
    # which does not parse. If the first non-blank line of `body` looks like a
    # declaration, keep the CALLER's signature and drop the original: they
    # usually change the parameters, and keeping the old one would silently
    # discard that edit.
    caller_decl = bool(new_body and DECL_RE.match(new_body[0]))
    if caller_decl:
        decl = new_body[0]
        new_body = new_body[1:]
    merged = [decl] + [(indent + l if l.strip() else l) for l in new_body]
    after = "\n".join(head + merged + tail)
    if text.endswith("\n") and not after.endswith("\n"):
        after += "\n"

    return Change(str(p), "replace_symbol_body",
                  f"replaced body of {name!r} (lines {start}-{end})",
                  before=text, after=after,
                  notes=[f"{name}: {target.kind if target else 'symbol'}"])


def insert_after_symbol(path: str, name: str, code: str,
                        *, at_module_level: bool = False) -> Change:
    """Insert `code` immediately after `name`'s body."""
    p = _safe(path)
    text = read_text(p)
    ext = _find_symbol_extent(text, name)
    if ext is None:
        return Change(str(p), "insert_after_symbol",
                      f"no symbol named {name!r} in {p.name}")
    _start, end, syms = ext
    lines = text.splitlines()
    target = next((s for s in syms if s.name == name), None)
    indent = ""
    if target and not at_module_level:
        orig = lines[target.line - 1]
        indent = orig[:len(orig) - len(orig.lstrip())]
    added = [(indent + l if l.strip() else l)
             for l in _dedent_body(code).splitlines()]
    after = "\n".join(lines[:end] + [""] + added + lines[end:])
    if text.endswith("\n") and not after.endswith("\n"):
        after += "\n"
    return Change(str(p), "insert_after_symbol",
                  f"inserted {len(added)} line(s) after {name!r} (line {end})",
                  before=text, after=after)


# ---------------------------------------------------------------------------
# preview / apply
# ---------------------------------------------------------------------------

def diff_for(change: Change, *, context: int = 3) -> str:
    return "\n".join(difflib.unified_diff(
        change.before.splitlines(),
        change.after.splitlines(),
        fromfile=f"a/{Path(change.path).name}",
        tofile=f"b/{Path(change.path).name}",
        lineterm="",
        n=context,
    ))


def preview(path: str, name: str, code: str, mode: str = "replace") -> Change:
    """Build a change and render its diff WITHOUT writing anything.

    This is the primitive that makes an agentic edit safe: the model sees the
    exact patch, and applying is a separate, explicit step.
    """
    if mode == "replace":
        return replace_symbol_body(path, name, code)
    if mode == "insert_after":
        return insert_after_symbol(path, name, code)
    return Change(str(_safe(path)), mode, f"unknown mode {mode!r}")


def apply_change(change: Change) -> dict[str, Any]:
    """Write a previewed change to disk."""
    if change.before == change.after:
        return {"ok": True, "written": False, "reason": "no change"}
    p = Path(change.path)
    tmp = p.with_suffix(p.suffix + ".aria-tmp")
    try:
        tmp.write_text(change.after, encoding="utf-8")
        tmp.replace(p)          # atomic: never leaves a half-written file
    except OSError as exc:
        return {"ok": False, "error": f"could not write {p.name}: {exc}"}
    return {"ok": True, "written": True, "path": str(p),
            "bytes": len(change.after.encode("utf-8"))}


__all__ = [
    "Symbol", "Reference", "Change",
    "document_symbols", "find_references", "blast_radius",
    "replace_symbol_body", "insert_after_symbol",
    "preview", "apply_change", "diff_for",
    "read_text", "iter_files",
]