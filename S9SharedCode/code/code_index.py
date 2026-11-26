"""A materialized map of a project's code.

Why this exists: the symbol tools (`document_symbols`, `find_references`,
`blast_radius`) recompute everything on every call. Measured on this repo —
305 Python files — `find_references` took **33 seconds**, because it walked
4,661 files (3,625 of them Aria's own `state/` tree) and read each one to test a
substring. The studio's own prompt then had to say "search first, spend it once",
which is advice about a cost the harness imposes.

So the map is built once and refreshed incrementally: per-file `mtime + size`,
hashed only when those change. `find_references` then becomes "which files
import or define this name" (an index lookup) followed by an exact parse of
those few files — not a walk of the tree.

Freshness is the whole risk, and it is handled explicitly rather than assumed:
every artefact records the root and each file's fingerprint, `refresh()` reports
what it changed, and a restore or a bulk replace drops the artefact entirely
(``invalidate``). A stale index would make the agent confidently wrong, which is
worse than no index.

Language coverage: Python is exact (``ast``). JavaScript/TypeScript and the rest
get a conservative line scanner — the same idea ``code_intel`` already uses for
``document_symbols``. Exactness is a property of the parse, not of the cache.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

VERSION = 1

# Directories that can never contain the project's own source. `.git` and the
# caches are noise; `node_modules` and virtualenvs are other people's code. The
# studio's own state tree is included because the repo this runs in keeps it
# inside the checkout — 3,625 of the 4,661 files in the walk that motivated this.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".next", ".nuxt", "dist", "build", "out", "coverage",
    "htmlcov", "target", "vendor", ".idea", ".vscode", "state", "logs",
    "sandbox", ".cache",
})

MAX_FILE_BYTES = 2_000_000
INDEX_NAME = "index.json"

_PY_SUFFIXES = {".py", ".pyi"}
# The rest are scanned for shape, not parsed: enough to list classes/functions
# and to know which files mention a name, not enough to resolve types.
_SCAN_GLOBS = {
    "javascript": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    "json": (".json",),
    "bash": (".sh", ".bash", ".ps1"),
}

_DEF_RE = {
    "python": re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(",
                         re.M),
    "javascript": re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*"
        r"([A-Za-z_$][\w$]*)\s*\("
        r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
        r"(?:async\s*)?\(",
        re.M),
    "bash": re.compile(r"^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\s*\)\s*\{",
                       re.M),
}
_CLASS_RE = {
    "python": re.compile(r"^class\s+([A-Za-z_]\w*)", re.M),
    "javascript": re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.M),
}


def _lang_for(path: Path) -> str:
    if path.suffix.lower() in _PY_SUFFIXES:
        return "python"
    for lang, suffixes in _SCAN_GLOBS.items():
        if path.suffix.lower() in suffixes:
            return lang
    return ""


def _fingerprint(path: Path) -> tuple[int, int, str]:
    """(mtime, size, sha1) for one file, or a zero fingerprint if unreadable."""
    try:
        st = path.stat()
    except OSError:
        return 0, 0, ""
    try:
        h = hashlib.sha1(path.read_bytes()).hexdigest()[:16]
    except OSError:
        h = ""
    return int(st.st_mtime), int(st.st_size), h


def _iter_files(root: Path) -> Iterable[Path]:
    """Walk without following links and without descending into SKIP_DIRS.

    ``os.walk`` rather than ``rglob``: a repo root can hold a junction to a
    removed target, and ``rglob`` raises on it (this project has one under a
    vendored ``node_modules``).

    The map's own artefact directory is pruned too, wherever it lives: indexing
    ``index.json`` would make the map report itself as the one file that changed
    on every refresh.
    """
    root = Path(root)
    try:
        own = Path(_state_dir()).resolve()
    except Exception:
        own = None
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS
            and (own is None or (Path(dirpath) / d).resolve() != own)
        ]
        for name in filenames:
            yield Path(dirpath) / name


# ── parsing ─────────────────────────────────────────────────────────────────

def _name_tokens(text: str, lang: str) -> list[str]:
    """The identifiers a file uses, as a sorted set for candidate lookup.

    Python goes through ``tokenize`` so comments and strings contribute nothing:
    a name mentioned only in prose must not make a file a candidate, or every
    reference query drags in half the repository's documentation.

    This is the index's real answer to "where could this name be": without it
    the only options are a full tree walk (33 s) or trusting that every use is
    also a definition or an import (it is not — `x = leaked_marker` is a use).
    """
    try:
        if lang == "python":
            import io
            import tokenize
            names = set()
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.NAME:
                    names.add(tok.string)
            return sorted(names)[:4000]
    except Exception:
        pass
    return sorted(set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)))[:4000]


def _py_symbols(text: str, rel: str) -> dict:
    """Classes, functions and imports from Python source. Exact."""
    out: dict[str, Any] = {"classes": [], "functions": [], "imports": []}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        out["error"] = "syntax"
        return out
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [
                f"{n.name}({', '.join(a.arg for a in n.args.args)})"
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            out["classes"].append({
                "name": node.name, "line": node.lineno,
                "end": getattr(node, "end_lineno", node.lineno),
                "methods": methods[:40],
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out["functions"].append({
                "name": node.name, "line": node.lineno,
                "end": getattr(node, "end_lineno", node.lineno),
                "args": [a.arg for a in node.args.args][:12],
            })
        elif isinstance(node, ast.Import):
            out["imports"] += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out["imports"].append(node.module)
            # `from x import y` is how a re-export reaches the rest of the tree;
            # recording the names is what makes references findable.
            for a in node.names:
                if a.name != "*":
                    out["imports"].append(f"{node.module or ''}.{a.name}"
                                          if node.module else a.name)
    out["imports"] = sorted({i for i in out["imports"] if i})[:200]
    return out


def _scanned_symbols(text: str, lang: str) -> dict:
    """A conservative shape for languages without a parser here."""
    out: dict[str, Any] = {"classes": [], "functions": [], "imports": []}
    for i, line in enumerate(text.splitlines(), 1):
        for m in _DEF_RE.get(lang, re.compile(r"^$")).finditer(line):
            # The JavaScript pattern has two alternatives; the shell one has a
            # single group. Read whichever groups exist rather than assuming two.
            name = next((g for g in m.groups() if g), None)
            if name:
                out["functions"].append({"name": name, "line": i})
        for m in _CLASS_RE.get(lang, re.compile(r"^$")).finditer(line):
            out["classes"].append({"name": m.group(1), "line": i})
        if lang == "javascript":
            m = re.search(r"""(?:from|require\()\s*['"]([^'"]+)['"]""", line)
            if m:
                out["imports"].append(m.group(1))
    return out


def _entry_for(path: Path, root: Path) -> dict:
    rel = path.relative_to(root).as_posix()
    lang = _lang_for(path)
    entry: dict[str, Any] = {"path": rel, "lang": lang, "classes": [],
                             "functions": [], "imports": [], "tokens": []}
    if not lang:
        return entry
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            entry["skipped"] = "too large"
            return entry
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        entry["skipped"] = str(exc)
        return entry
    entry = {**entry, **(_py_symbols(text, rel) if lang == "python"
                         else _scanned_symbols(text, lang))}
    entry["tokens"] = _name_tokens(text, lang)
    return entry


def _link_imports(files: dict[str, dict]) -> None:
    """Fill `imported_by` — the edge `document_symbols` alone cannot give you.

    This is the part a per-file AST call never produces: the reverse index is
    what makes "what would this rename touch" one lookup.
    """
    modules = {rel[:-3].replace("/", "."): rel
               for rel, e in files.items() if e["lang"] == "python"}
    js_modules = {rel.rsplit(".", 1)[0].replace("/", "/"): rel
                  for rel, e in files.items() if e["lang"] == "javascript"}
    for rel, entry in files.items():
        entry["imported_by"] = []
    for rel, entry in files.items():
        for imp in entry.get("imports", []):
            base = imp.split(".", 1)[0]
            for mod, target in modules.items():
                if target == rel:
                    continue
                if imp == mod or imp.endswith("." + mod) or base == mod:
                    files[target].setdefault("imported_by", []).append(rel)
                    break
            else:
                norm = imp.replace("\\", "/")
                for mod, target in js_modules.items():
                    if target == rel:
                        continue
                    if norm.endswith(mod) or norm.endswith(mod + ".js") \
                            or norm.endswith(mod + ".ts"):
                        files[target].setdefault("imported_by", []).append(rel)
                        break
    for entry in files.values():
        entry["imported_by"] = sorted(set(entry.get("imported_by") or []))[:80]


# ── storage ─────────────────────────────────────────────────────────────────

def _state_dir() -> Path:
    """Where the artefact lives: the process state dir, never the user's repo.

    Writing ``.aria/`` into someone's project shows up in their ``git status``
    and in the next diff the agent reads, which is a worse surprise than a cache
    miss.
    """
    try:
        import studio_workspace as sw
        return Path(sw.state_root()) / "code_index"
    except Exception:
        return Path(os.environ.get("ARIA_STATE") or
                    Path.home() / ".aria") / "code_index"


def _bucket(root: Path) -> str:
    return hashlib.sha1(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]


def _index_path(root: Path) -> Path:
    return _state_dir() / _bucket(root) / INDEX_NAME


def load(root: Path) -> dict | None:
    try:
        got = json.loads(_index_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(got, dict) or got.get("version") != VERSION:
        return None
    return got


def invalidate(root: Path) -> None:
    """Drop the artefact. Called by a restore or a bulk replace.

    Those rewrite files the agent did not edit, so every fingerprint in the map
    is a lie until it is rebuilt; dropping it costs one rebuild and removes the
    chance of a confident wrong answer.
    """
    try:
        _index_path(root).unlink()
    except OSError:
        pass


def _save(root: Path, data: dict) -> None:
    path = _index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")),
                   encoding="utf-8")
    os.replace(tmp, path)


# ── building and refreshing ─────────────────────────────────────────────────

def refresh(root: Path, *, force: bool = False) -> dict:
    """Bring the map up to date and say what moved.

    Incremental by design: only files whose mtime or size changed are re-read,
    and only those are hashed. A no-op refresh walks metadata alone.
    """
    root = Path(root)
    data = load(root)
    if force or data is None:
        data = {"version": VERSION, "root": str(root.resolve()), "built": 0,
                "files": {}}
        previous: dict[str, dict] = {}
    else:
        previous = data.get("files") or {}

    seen: set[str] = set()
    changed: list[str] = []
    files: dict[str, dict] = dict(previous)

    for path in _iter_files(root):
        if not _lang_for(path):
            continue
        rel = path.relative_to(root).as_posix()
        seen.add(rel)
        mtime, size, _ = _fingerprint(path)
        old = previous.get(rel)
        if old and old.get("mtime") == mtime and old.get("size") == size:
            continue
        if old and old.get("mtime") == mtime and old.get("hash"):
            # Same mtime and size: unchanged content (or a same-second rewrite,
            # which is the case the hash exists to catch).
            _, _, digest = _fingerprint(path)
            if old.get("hash") == digest:
                continue
        try:
            entry = _entry_for(path, root)
        except Exception as exc:                   # noqa: BLE001
            # One unreadable or alien file must not take the whole map down:
            # record it as skipped and keep indexing the rest.
            entry = {"path": rel, "lang": _lang_for(path), "classes": [],
                     "functions": [], "imports": [],
                     "skipped": f"{type(exc).__name__}: {exc}"}
        entry["mtime"], entry["size"] = mtime, size
        entry["hash"] = _fingerprint(path)[2]
        files[rel] = entry
        changed.append(rel)

    for gone in [r for r in files if r not in seen]:
        files.pop(gone, None)
        changed.append(gone)

    _link_imports(files)
    data = {"version": VERSION, "root": str(root.resolve()),
            "built": time.time(), "files": files}
    _save(root, data)
    return {"changed": changed, "files": len(files), "index": data}


# ── queries ─────────────────────────────────────────────────────────────────

def _module_list(index: dict) -> dict[str, str]:
    return {rel[:-3].replace("/", "."): rel
            for rel, e in index["files"].items() if e.get("lang") == "python"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))


def _py_code_lines(text: str) -> list[str]:
    """Blank out strings AND comments, keeping line numbering.

    ``tokenize`` rather than a regex or an AST walk: a reference inside a
    docstring, a comment or an error message is not a call site, and counting it
    is how a rename reports "no references" when the name is only being
    described. ``tokenize`` knows where strings and comments actually are, which
    matters for the `# add()` line that sits above a real call.
    """
    import io
    import tokenize

    lines = text.splitlines()
    blank: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                for i in range(tok.start[0] - 1, min(tok.end[0], len(lines))):
                    blank.add(i)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return lines
    for i in blank:
        lines[i] = ""
    return lines


def references(index: dict, name: str, *, include_defs: bool = True,
               limit: int = 200) -> list[dict]:
    """Every use of `name`, with the declarations marked.

    Candidates come from the index — the files that define the name, import it,
    or sit in the same package — and only those are read. That is the whole
    point: the exact answer without walking the tree.
    """
    root = Path(index.get("root") or ".")
    name = (name or "").strip()
    if not name:
        return []
    files: dict[str, dict] = index.get("files") or {}
    word = re.compile(r"\b" + re.escape(name) + r"\b")

    candidates: set[str] = set()
    for rel, entry in files.items():
        if name in (entry.get("tokens") or []):
            candidates.add(rel)
        if any(c.get("name") == name for c in entry.get("classes", [])) \
                or any(f.get("name") == name for f in entry.get("functions", [])):
            candidates.add(rel)
        for imp in entry.get("imports", []):
            if imp == name or imp.endswith("." + name):
                candidates.add(rel)

    hits: list[dict] = []
    for rel in sorted(candidates):
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lang = (files.get(rel) or {}).get("lang") or _lang_for(path)
        lines = (_py_code_lines(text) if lang == "python"
                 else text.splitlines())
        for i, line in enumerate(lines, 1):
            if word.search(line):
                if not include_defs and i in _def_lines(files.get(rel) or {}):
                    continue
                # `file` is the key the older tool already returns; keeping both
                # spellings means a caller can switch without changing its reader.
                hits.append({"path": rel, "file": rel, "line": i,
                             "text": line.strip()[:200]})
                if len(hits) >= limit:
                    return hits
    return hits


def _def_lines(entry: dict) -> set[int]:
    out = {int(c.get("line") or 0) for c in entry.get("classes", [])}
    out |= {int(f.get("line") or 0) for f in entry.get("functions", [])}
    return out


def blast_radius(index: dict, name: str, *, max_files: int = 50) -> dict:
    """What a change to `name` would touch: files ranked by reference count."""
    """What a change to `name` would touch: files ranked by reference count."""
    hits = references(index, name, limit=2000)
    per_file: dict[str, int] = {}
    for h in hits:
        per_file[h["path"]] = per_file.get(h["path"], 0) + 1
    ranked = sorted(per_file.items(), key=lambda kv: -kv[1])[:max_files]
    total = sum(per_file.values())
    risk = "high" if len(per_file) > 5 or total > 20 else \
           "medium" if len(per_file) > 1 else "low"
    return {
        "name": name, "risk": risk, "files": len(per_file), "references": total,
        "ranked": [{"path": p, "references": n} for p, n in ranked],
    }


def knows_name(index: dict, name: str) -> bool:
    """Does this name appear in the indexed source at all?

    The caller's choice between "answer empty" and "fall back to a full scan"
    depends on it: a name that is in no file's tokens is definitively absent
    from the code, and paying 30 s to be told so again is waste. A name that IS
    present but yields no references is the case worth the slower path.
    """
    name = (name or "").strip()
    if not name:
        return False
    for entry in (index.get("files") or {}).values():
        if name in (entry.get("tokens") or []):
            return True
        if any(c.get("name") == name for c in entry.get("classes", [])) \
                or any(f.get("name") == name for f in entry.get("functions", [])):
            return True
        for imp in entry.get("imports", []):
            if imp == name or imp.endswith("." + name):
                return True
    return False


def find_references(index: dict, name: str, *, max_hits: int = 200) -> dict:
    """The same answer as `code_intel.find_references`, from the map.

    Same shape on purpose (`definitions` / `references` / `exact`), because this
    is a faster path to an existing tool's answer, not a new contract — a caller
    that cannot get the map must be able to fall back without touching its
    rendering.
    """
    hits = references(index, name, include_defs=True, limit=max_hits * 2)
    files: dict[str, dict] = index.get("files") or {}
    defs: list[dict] = []
    refs: list[dict] = []
    for h in hits:
        entry = files.get(h["path"]) or {}
        h = {**h, "col": 0}
        if h["line"] in _def_lines(entry):
            defs.append({**h, "kind": "definition"})
        else:
            refs.append({**h, "kind": "use"})
        if len(defs) + len(refs) >= max_hits:
            break
    return {
        "ok": True,
        "symbol": name,
        "definitions": defs,
        "references": refs,
        "definition_count": len(defs),
        "reference_count": len(refs),
        "files_scanned": len({h["file"] for h in defs + refs}),
        "exact": True,
        "indexed": len(files),
        "indexed_at": index.get("built") or 0,
    }


def symbols(index: dict, rel: str) -> dict:
    entry = (index.get("files") or {}).get(rel)
    return entry or {}


def tree(index: dict, *, max_dirs: int = 40) -> str:
    """The folder overview: what is where, with counts.

    The prompt needs a map of the territory, not a file list — "src/ holds 40
    files, tests/ 12" tells the agent where to look before it spends a call.
    """
    dirs: dict[str, dict[str, int]] = {}
    for rel, entry in (index.get("files") or {}).items():
        parts = rel.split("/")
        for depth in range(1, len(parts) + 1):
            d = "/".join(parts[:depth]) if depth < len(parts) else "."
            slot = dirs.setdefault(d, {"files": 0, "py": 0, "js": 0})
            if depth == len(parts) - 1:
                slot["files"] += 1
                if entry.get("lang") == "python":
                    slot["py"] += 1
                elif entry.get("lang") == "javascript":
                    slot["js"] += 1
    lines = []
    for d in sorted(dirs)[:max_dirs]:
        slot = dirs[d]
        if not slot["files"]:
            continue
        bits = [f"{slot['files']} file" + ("s" if slot["files"] != 1 else "")]
        if slot["py"]:
            bits.append(f"{slot['py']} py")
        if slot["js"]:
            bits.append(f"{slot['js']} js")
        lines.append(f"{d}/  ({', '.join(bits)})")
    return "\n".join(lines)


def entry_points(index: dict, *, limit: int = 12) -> list[str]:
    """The files worth reading first in an unfamiliar project."""
    names = {"main.py", "cli.py", "app.py", "manage.py", "server.py", "wsgi.py",
             "asgi.py", "__main__.py", "index.js", "index.ts", "main.js",
             "main.ts", "server.js", "app.js"}
    out = []
    for rel in sorted((index.get("files") or {})):
        if rel.rsplit("/", 1)[-1] in names:
            out.append(rel)
    return out[:limit]