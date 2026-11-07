"""Slice 8 — file operations, project search with replace, and the git verbs.

Everything here is a PLANNER, not an executor: these functions decide whether
an operation is allowed and what it would do, and `agent_server.py` performs
it. That split is what makes the dangerous ones testable without touching a
repository or a disk.

Three rules the whole module is built around:

1. **A path means one thing.** Studio paths arrive in the explorer's
   vocabulary (`<root>/dir/file`), which is also what `_code_agent_abs` and
   `mcp_server._safe` accept. Search, rename and git all speak that form, so
   a result can be opened, mentioned with `@`, and reviewed without a
   translation step — and a translation step is where a silent wrong file
   comes from.

2. **Nothing destructive happens without the bytes being kept first.** A
   delete, a move and a project-wide replace all capture what they replace
   into a checkpoint, so "delete" stays a reviewable act rather than a loss.

3. **A regex can hang the process.** Python's `re` has no timeout and a nested
   quantifier on a 500 KB file runs for minutes, holding a worker with it. So
   a pattern that cannot be bounded is refused with its reason, rather than
   tried and blamed on the machine.
"""
from __future__ import annotations

import os
import re

MAX_PATTERN_CHARS = 400
MAX_MATCHES = 500
MAX_FILES = 4000
MAX_BYTES_PER_FILE = 512 * 1024
MAX_REPLACE_FILES = 200

# Directories that are never searched or replaced: build output, vendored
# trees, and the repository's own object store.
SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "test-results", "playwright-report", ".next", ".cache", "coverage",
    ".idea", ".vscode", ".tox",
})
SKIP_EXT = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".whl", ".pyc", ".pyo", ".exe", ".dll", ".so", ".dylib", ".woff",
    ".woff2", ".ttf", ".mp4", ".mov", ".sqlite", ".db",
})

# `(a+)+`, `(a*)*`, `([ab]+)*` — a quantifier applied to a group that already
# contains one. These are the shapes that go exponential on the long lines
# real files contain.
_TRAILING_DOUBLE = re.compile(r"[)\]]\s*[*+]\s*[*+]")


def _group_nests_quantifier(p: str) -> bool:
    """True when a quantified group encloses another quantified atom.

    A hand-written scanner, because the shapes that matter are few and a regex
    for them is unreadable: track whether the group currently open contains a
    `*`/`+`, and flag it when a `)` that did contain one is itself followed by
    a quantifier. `(a|b)+` and `(ab)+` stay allowed — no inner quantifier, no
    exponential backtracking.
    """
    stack: list = []
    i = 0
    n = len(p)
    while i < n:
        ch = p[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            j = i + 1
            if j < n and p[j] == "^":
                j += 1
            if j < n and p[j] == "]":
                j += 1
            while j < n and p[j] != "]":
                j += 2 if p[j] == "\\" else 1
            if j + 1 < n and p[j + 1] in ("*", "+") and stack:
                stack[-1] = True
            i = j + 2
            continue
        if ch == "(":
            stack.append(False)
            i += 1
            if i < n and p[i] == "?":
                i += 1
                while i < n and p[i] in "iLmsxaupn:<=!":
                    i += 1
                if i < n and p[i] == ":":
                    i += 1
            continue
        if ch in "*+":
            if stack:
                stack[-1] = True
            i += 1
            continue
        if ch == ")":
            inner = stack.pop() if stack else False
            nxt = p[i + 1:i + 2]
            # `nxt in "*+"` would be True for the EMPTY string — a group at the
            # very end of a pattern is the most common shape there is, so that
            # typo flags `OLD\s*=\s*(\d+)` as dangerous.
            quantified = nxt in ("*", "+")
            if inner and quantified:
                return True
            if quantified and stack:
                stack[-1] = True
            i += 2 if quantified else 1
            continue
        i += 1
    return False


def dangerous_pattern(pattern: str) -> str:
    """Why this pattern will not be compiled, or "" when it will.

    Refusing an unbounded expression is better than a search that eats a
    worker for ten minutes and returns nothing — and the message says what to
    write instead, because "invalid" without a reason sends the person to a
    search engine.

    This is a shape heuristic, not a proof: Python's `re` has no timeout and
    nothing in the standard library can bound a backtracking engine, so the
    documented dangerous families are refused and the file and byte budgets
    below bound everything else.
    """
    p = (pattern or "").strip()
    if not p:
        return "the pattern is empty"
    if len(p) > MAX_PATTERN_CHARS:
        return f"the pattern is longer than {MAX_PATTERN_CHARS} characters"
    if _group_nests_quantifier(p) or _TRAILING_DOUBLE.search(p):
        return ("this pattern nests one quantifier inside another "
                "((a+)+ and similar), which can run for minutes on a long "
                "line; write it without the outer repeat")
    try:
        re.compile(p)
    except re.error as e:
        return f"not a valid expression: {e}"
    return ""


def in_roots(roots, target) -> bool:
    """Is `target` inside one of `roots`? Compares RESOLVED paths, so a
    symlink that points out of the project does not count as inside it."""
    try:
        rp = os.path.realpath(str(target))
    except (OSError, TypeError):
        return False
    for r in roots:
        try:
            rr = os.path.realpath(str(r))
        except (OSError, TypeError):
            continue
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def _parts(p) -> list:
    return [x for x in str(p).replace("\\", "/").split("/") if x]


def _parent(p):
    try:
        return p.parent
    except Exception:
        return None


def _is_ancestor(a, b) -> bool:
    try:
        b.resolve().relative_to(a.resolve())
        return True
    except (ValueError, OSError):
        return False


def _is_root(roots, p) -> bool:
    try:
        rp = p.resolve()
    except (OSError, RuntimeError):
        return False
    for r in roots:
        try:
            if rp == r.resolve():
                return True
        except (OSError, RuntimeError):
            continue
    return False


# ── file operations ────────────────────────────────────────────────────
def plan_op(roots, display, op: str, frm: str = "", to: str = "") -> dict:
    """Decide one rename / move / delete / mkdir. Nothing is touched.

    `display` is the caller's path vocabulary: `resolve(client) -> Path |
    None` and `show(abs) -> client`. It is injected rather than reimplemented
    here because the naming belongs to the process that owns the roots — the
    studio has had three separate path dialects disagree before now.
    """
    op = (op or "").strip().lower()
    if op not in ("rename", "move", "delete", "mkdir"):
        return {"ok": False, "reason": f"unknown operation {op!r}"}
    src = display.resolve(frm) if frm else None
    if src is None:
        return {"ok": False, "reason": "no such path, or it is outside "
                                       "this session's folders"}
    if not in_roots(roots, src):
        return {"ok": False, "reason": "outside this session's folders"}
    if op in ("rename", "move"):
        dst = display.resolve(to) if to else None
        if dst is None or not in_roots(roots, dst):
            return {"ok": False, "reason": "the destination is outside "
                                          "this session's folders"}
        if dst.exists():
            return {"ok": False, "reason": f"something is already at {to}"}
        try:
            if src.is_dir() and _is_ancestor(src, dst):
                return {"ok": False, "reason": "a folder cannot be moved "
                                               "inside itself"}
        except OSError as e:
            return {"ok": False, "reason": f"the path could not be read: {e}"}
        if op == "rename" and _parent(src) != _parent(dst):
            return {"ok": False, "reason": "rename changes a name, not a "
                                          "folder — use move"}
        return {"ok": True, "kind": op, "abs_from": src, "abs_to": dst,
                "reason": ""}
    if op == "delete":
        if _is_root(roots, src):
            return {"ok": False, "reason": "the session's folder itself is not "
                                          "a file operation — remove it from "
                                          "the session instead"}
        if ".git" in _parts(src):
            return {"ok": False, "reason": "repository internals are not a "
                                          "file operation"}
        return {"ok": True, "kind": "delete", "abs_from": src, "abs_to": None,
                "reason": ""}
    if src.exists():
        return {"ok": False, "reason": "that path already exists"}
    parent = _parent(src)
    if parent is not None and not in_roots(roots, parent):
        return {"ok": False, "reason": "the parent is outside this session's "
                                      "folders"}
    return {"ok": True, "kind": "mkdir", "abs_from": None, "abs_to": src,
            "reason": ""}


# ── search ─────────────────────────────────────────────────────────────
def search(roots, display, pattern: str, *, regex: bool = True,
           case_sensitive: bool = True, glob: str = "",
           max_matches: int = MAX_MATCHES,
           max_files: int = MAX_FILES) -> dict:
    """Literal or regex search across every folder the session holds.

    `/api/code/search` is substring-only because it is wired to a browser text
    box; an IDE's find-in-files needs expressions, and this is that. The same
    bounds apply, and the same rule: a partial answer announces itself.
    """
    pattern = pattern or ""
    if not pattern:
        return {"matches": [], "count": 0, "truncated": False,
                "error": "nothing to search for"}
    rx = None
    if regex:
        why = dangerous_pattern(pattern)
        if why:
            return {"matches": [], "count": 0, "truncated": False, "error": why}
        try:
            rx = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
        except re.error as e:
            return {"matches": [], "count": 0, "truncated": False,
                    "error": f"not a valid expression: {e}"}
    cap = max(1, min(int(max_matches or MAX_MATCHES), MAX_MATCHES))
    file_cap = max(1, min(int(max_files or MAX_FILES), MAX_FILES))
    glob_rx = None
    if glob:
        glob_rx = re.compile("^" + re.escape(glob.lower())
                             .replace("\\*", ".*").replace("\\?", ".") + "$")
    matches: list = []
    scanned = 0
    skipped_binary = 0
    stopped = False
    truncated = False
    needle = pattern.lower()
    for root in roots:
        if stopped or truncated:
            break
        stack = [root]
        while stack and not (stopped or truncated):
            d = stack.pop()
            try:
                children = sorted(d.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    is_dir = child.is_dir()
                except OSError:
                    continue
                if is_dir:
                    if child.name not in SKIP_DIRS:
                        stack.append(child)
                    continue
                if glob_rx and not glob_rx.search(child.name.lower()):
                    continue
                if child.suffix.lower() in SKIP_EXT:
                    skipped_binary += 1
                    continue
                scanned += 1
                if scanned > file_cap:
                    stopped = True
                    break
                try:
                    if child.stat().st_size > MAX_BYTES_PER_FILE:
                        continue
                    text = child.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    skipped_binary += 1
                    continue
                for ln, line in enumerate(text.splitlines(), start=1):
                    if rx is not None:
                        cols = [m.start() for m in rx.finditer(line)][:20]
                    else:
                        cols = list(_find_all(line.lower(), needle))[:20]
                    for col in cols:
                        if len(matches) >= cap:
                            truncated = True
                            break
                        matches.append({"path": display.show(child),
                                        "line": ln, "col": col + 1,
                                        "text": line.strip()[:300]})
                    if truncated:
                        break
                if truncated:
                    break
    return {"matches": matches, "count": len(matches),
            "truncated": bool(truncated or stopped),
            "scanned": scanned, "binary_skipped": skipped_binary,
            "regex": bool(regex),
            "note": ("stopped at the file budget — this is not every match"
                     if stopped else "")}


def _find_all(hay: str, needle: str):
    start = 0
    while True:
        at = hay.find(needle, start)
        if at < 0:
            return
        yield at
        start = at + max(1, len(needle))


# ── replace (dry run first) ───────────────────────────────────────────
def plan_replace(roots, display, hits: list, pattern: str,
                 replacement: str, *, regex: bool = True,
                 case_sensitive: bool = True) -> dict:
    """The dry-run half of a project-wide replace.

    It takes the MATCHES the person was SHOWN rather than re-scanning, because
    "search then replace" quietly becomes a different operation the moment the
    second scan finds a different set — a file changed underneath, or a budget
    that cut in one pass and not the other.

    No file is written here. The route that applies it re-reads every file and
    skips any whose bytes have moved since this plan was made, returning them
    in `stale` — the verified files are still applied, because rolling back
    work that DID match the preview would punish the person for a race they
    cannot see. What is never allowed is a silent partial replace.
    """
    if regex:
        why = dangerous_pattern(pattern)
        if why:
            return {"ok": False, "error": why, "files": []}
    paths: list = []
    for h in hits or []:
        p = (h or {}).get("path")
        if p and p not in paths:
            paths.append(p)
    if not paths:
        return {"ok": True, "files": [], "count": 0, "refused": [],
                "error": "", "note": "no matches selected"}
    if len(paths) > MAX_REPLACE_FILES:
        return {"ok": False,
                "error": f"{len(paths)} files is over the {MAX_REPLACE_FILES} "
                         "per-operation limit; narrow it with a folder or a "
                         "file filter",
                "files": []}
    rx = None
    if regex:
        try:
            rx = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
        except re.error as e:
            return {"ok": False, "error": f"not a valid expression: {e}",
                    "files": []}
    files: list = []
    refused: list = []
    total = 0
    for client in paths:
        abs_path = display.resolve(client)
        if abs_path is None or not abs_path.is_file() \
                or not in_roots(roots, abs_path):
            refused.append(client)
            continue
        try:
            raw = abs_path.read_bytes()
            old = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            refused.append(client)
            continue
        if rx is not None:
            new, n = rx.subn(_safe_template(replacement, rx), old)
        else:
            new, n = _literal_replace(old, pattern, replacement, case_sensitive)
        if not n:
            continue
        files.append({"path": client, "count": n,
                      "bytes_before": len(raw), "bytes_after": len(new),
                      # The BYTES that were read, and the bytes to write back.
                      # Decoding and re-encoding on the apply step is where a
                      # CRLF file silently becomes LF, and the stale check then
                      # refuses a file nobody touched — the mismatch this
                      # project has already been bitten by once.
                      "before": raw, "after": new.encode("utf-8"),
                      "newline": "\r\n" if b"\r\n" in raw else "\n"})
        total += n
    return {"ok": True, "files": files, "count": total, "refused": refused,
            "regex": bool(regex),
            "note": (f"{len(refused)} path(s) could not be read or left the "
                     "session" if refused else "")}


def _safe_template(repl: str, rx) -> str:
    """A person-typed replacement as a `re.sub` template.

    `\\1` and `\\g<name>` stay back-references — that is the point of a regex
    replace — but an unpaired backslash crashes inside `sub`, so it is
    escaped rather than refused.
    """
    r = repl or ""
    try:
        rx.subn(r, "")
        return r
    except re.error:
        return r.replace("\\", "\\\\")


def _literal_replace(text: str, needle: str, repl: str,
                     case_sensitive: bool):
    if case_sensitive:
        return text.replace(needle, repl), text.count(needle)
    # subn, not sub: the caller unpacks (new, count). Returning the bare string
    # here made every case-insensitive literal replace raise "too many values
    # to unpack" and 500 the preview route.
    return re.subn(re.escape(needle), _escape_repl(repl), text,
                   flags=re.IGNORECASE)


def _escape_repl(repl: str) -> str:
    return repl.replace("\\", "\\\\")


# ── git verbs ─────────────────────────────────────────────────────────
# A deliberate WIDENING of what Aria may do to a repository, so the list is
# closed rather than open: anything not named here is refused, and the
# refusals say why. Reading is free; anything that moves HEAD or discards work
# needs an explicit confirm; a push is never offered.
_READ = {
    "status": (["status", "--porcelain=v1", "--branch"], False),
    "diff": (["diff", "--no-color"], False),
    "diff_staged": (["diff", "--cached", "--no-color"], False),
    "log": (["log", "--oneline", "-n", "30"], False),
    "branch": (["branch", "--show-current"], False),
    "branches": (["branch", "--list"], False),
}
GIT_ACTIONS = sorted(list(_READ) + [
    "stage", "unstage", "commit", "stash", "stash_pop", "branch_create",
    "branch_switch", "restore",
])
REFUSED = {
    "push": "pushing publishes to a remote other people read — do it yourself",
    "force_push": "force-pushing rewrites a shared branch",
    "reset": "reset moves HEAD and can discard commits",
    "clean": "clean deletes files git does not track, with no undo",
    "checkout": "use branch_switch; a bare checkout can overwrite work",
    "rebase": "rebasing rewrites history",
    "merge": "merging is a decision about two histories, not a button",
    "init": "making a repository a side effect of a button is wrong",
    "amend": "amending rewrites a commit that may already be shared",
}


def git_argv(action: str, args: dict | None = None) -> dict:
    """The argv for one git verb, or a refusal with its reason.

    No caller supplies a command line: the person chooses an action and a path
    list, and the argv is built here so a filename that begins with a dash
    cannot become an option (`--` precedes every path list, which is the only
    way a git UI stops `--force` being a flag someone typed).
    """
    a = (action or "").strip().lower()
    args = args or {}
    if a in REFUSED:
        return {"ok": False, "error": REFUSED[a], "refused": True}
    if a in _READ:
        argv, moves = _READ[a]
        return {"ok": True, "argv": list(argv), "moves_head": moves,
                "needs_confirm": False, "summary": " ".join(argv)}
    if a not in GIT_ACTIONS:
        return {"ok": False, "error": f"unknown git action {a!r}"}
    paths = _path_list(args.get("paths"), args.get("path"))
    if a == "stage":
        if not paths:
            return {"ok": False, "error": "no files given"}
        return {"ok": True, "argv": ["add", "--"] + paths, "moves_head": False,
                "needs_confirm": False, "summary": f"stage {len(paths)} file(s)"}
    if a == "unstage":
        if not paths:
            return {"ok": False, "error": "no files given"}
        return {"ok": True, "argv": ["restore", "--staged", "--"] + paths,
                "moves_head": False, "needs_confirm": False,
                "summary": f"unstage {len(paths)} file(s)"}
    if a == "commit":
        msg = str(args.get("message") or "").strip()
        if not msg:
            return {"ok": False, "error": "a commit needs a message"}
        if not paths:
            return {"ok": False, "error": "nothing was chosen to commit"}
        return {"ok": True, "argv": ["commit", "-m", msg, "--"] + paths,
                "moves_head": True, "needs_confirm": True,
                "summary": f"commit {len(paths)} file(s): "
                           f"{msg.splitlines()[0][:60]}"}
    if a == "stash":
        return {"ok": True, "argv": ["stash", "push", "-u"],
                "moves_head": False, "needs_confirm": True,
                "summary": "stash the work in progress (including untracked)"}
    if a == "stash_pop":
        return {"ok": True, "argv": ["stash", "pop"], "moves_head": True,
                "needs_confirm": True, "summary": "restore the stashed work"}
    if a == "branch_create":
        name = _branch_name(args.get("name"))
        if not name:
            return {"ok": False, "error": "a branch name cannot contain "
                                         "spaces, ~, ^, :, .. or start with "
                                         "- or ."}
        return {"ok": True, "argv": ["branch", name], "moves_head": False,
                "needs_confirm": False, "summary": f"create branch {name}"}
    if a == "branch_switch":
        name = _branch_name(args.get("name"))
        if not name:
            return {"ok": False, "error": "that is not a branch name"}
        return {"ok": True, "argv": ["switch", name], "moves_head": True,
                "needs_confirm": True, "summary": f"switch to {name}"}
    if a == "restore":
        if not paths:
            return {"ok": False, "error": "no files given"}
        return {"ok": True, "argv": ["restore", "--"] + paths,
                "moves_head": True, "needs_confirm": True,
                "summary": f"discard the changes in {len(paths)} file(s)"}
    return {"ok": False, "error": f"unhandled git action {a!r}"}


_BRANCH_BAD = re.compile(r"(^[-.~^:?*\[])|(\.\.)|(\s)|(/$)|(\\)")


def _branch_name(raw) -> str:
    name = str(raw or "").strip()
    if not name or len(name) > 120 or _BRANCH_BAD.search(name):
        return ""
    return name


def _path_list(raw, single=None) -> list:
    out: list = []
    if isinstance(raw, str):
        raw = [raw]
    if not raw and single:
        raw = [single]
    for p in (raw or []):
        s = str(p or "").strip().replace("\\", "/")
        if not s or s.startswith("-") or ".." in _parts(s):
            continue
        if s not in out:
            out.append(s)
    return out[:200]


def status_paths(porcelain: str) -> list:
    """`git status --porcelain` into rows a panel can offer to stage.

    Unmerged and deleted entries stay visible rather than being smoothed into
    the ordinary rows: the panel's job is to show what git said, including
    the states it cannot act on.
    """
    rows: list = []
    for line in (porcelain or "").splitlines():
        if len(line) < 4 or line.startswith("##"):
            continue
        x, y = line[0], line[1]
        raw = line[3:].strip()
        path = raw.split(" -> ")[-1] if " -> " in raw else raw
        rows.append({"path": path.strip('"'), "index": x, "worktree": y,
                    "staged": x not in " ?",
                    "untracked": x == "?",
                    "unmerged": "U" in (x + y) or (x + y) in ("AA", "DD", "AU", "UA"),
                    "deleted": "D" in (x + y)})
    return rows


# ── the journal ────────────────────────────────────────────────────────
# Human-initiated file and git operations are not model runs, so they are in
# no run transcript — and "what did Aria do to my repository" has to have an
# answer that does not depend on which tab was open when it happened. One
# append-only JSONL per session, newest last, never rewritten.
import json  # noqa: E402
import time  # noqa: E402

JOURNAL_MAX_LINES = 200


def journal_path(state_dir, sid: str):
    from pathlib import Path
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(sid or ""))[:60] or "default"
    return Path(state_dir) / "code-studio" / "journal" / f"{safe}.jsonl"


def journal_write(state_dir, sid: str, kind: str, detail: dict) -> dict:
    """Append one entry. Returns what was written, so the caller can show the
    same words it recorded rather than a second copy of them."""
    from pathlib import Path
    row = {"at": round(time.time(), 3), "kind": kind,
           "sid": str(sid or "")}
    row.update({k: v for k, v in (detail or {}).items()})
    p = journal_path(state_dir, sid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        row["error"] = f"the journal could not be written: {e}"
    return row


def journal_read(state_dir, sid: str, limit: int = JOURNAL_MAX_LINES) -> list:
    p = journal_path(state_dir, sid)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list = []
    for line in lines[-max(1, min(int(limit or 1), 5000)):]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))


