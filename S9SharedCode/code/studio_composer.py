"""Composer support for the studio: `@` mentions, `/` commands, commit messages.

Three small pure helpers, deliberately kept out of `agent_server.py` so they
can be tested without a FastAPI app:

`mentions()`     — what `@<fragment>` can complete to, in the SAME path
                   vocabulary the explorer and the review surface use, across
                   every folder the session holds.
`commands()`     — the slash-command registry. One list, because a command the
                   frontend invents and a command the server knows about are
                   two different products the moment they drift.
`diff_prompt()`  — the git status+diff text a commit message is written from,
                   bounded honestly: every changed file is always named, and
                   the response says which hunk bodies were left out instead of
                   quietly sending half a changeset.

The vocabulary rule matters here more than anywhere else in the studio. A
mention that resolves to a path the agent's `read_file` cannot use is worse
than no completion at all: the model reads "<root>/app.py", the tool joins it
onto the root, and it hunts for `proj/proj/app.py`.
"""
from __future__ import annotations

import posixpath
import re

MENTION_LIMIT = 40
MAX_WALK_ENTRIES = 40_000

# Kept short on purpose: the description is what the popup shows, and a
# paragraph per command is a popup nobody reads.
_COMMANDS = (
    {"name": "new", "args": "", "kind": "session",
     "desc": "start another session in this workspace"},
    {"name": "stop", "args": "", "kind": "run",
     "desc": "stop the running agent at its next step"},
    {"name": "undo", "args": "", "kind": "review",
     "desc": "restore the last checkpoint of the open file"},
    {"name": "checks", "args": "", "kind": "review",
     "desc": "run syntax and compile checks on the workspace"},
    {"name": "commit-msg", "args": "", "kind": "git",
     "desc": "draft a commit message from the staged diff"},
    {"name": "roots", "args": "", "kind": "session",
     "desc": "list the folders this session holds"},
    {"name": "review", "args": "", "kind": "review",
     "desc": "open the review editor for the file with changes"},
    {"name": "terminal", "args": "", "kind": "dock",
     "desc": "show the dock's terminal"},
)


def commands() -> list[dict]:
    """The slash commands the studio understands, in the order the popup lists them."""
    return [dict(c) for c in _COMMANDS]


def command_names() -> set[str]:
    return {c["name"] for c in _COMMANDS}


# A mention starts a word: `@app.py` is a file, `me@example.com` is an email
# and must not become one. The token itself is greedy and excludes the
# punctuation that ends a sentence, because a lazy pattern that allows `.`
# inside stops at "alpha/src/app" when the writer typed a comma after ".py".
# A path with a space in it is the one case that needs quoting.
_MENTION_RE = re.compile(
    r'(?:^|(?<=\s))@(?:"([^"\n]{1,200})"|([^\s,;:!?)\]}]{1,201}))')
_TRAILING = ".,;:?'\"`"


def extract_mentions(text: str) -> list[str]:
    """The `@path` tokens in a question, in order, de-duplicated.

    The composer writes these in the explorer's vocabulary (`<root>/dir/file`)
    because that is what the person can see and recognise. The agent's tools
    speak paths relative to a root, so the caller maps them before handing
    them over — and the mapping has to happen on the server, because the same
    token typed by hand in a terminal must mean the same file.
    """
    out: list[str] = []
    for m in _MENTION_RE.finditer(text or ""):
        tok = (m.group(1) or m.group(2) or "").strip()
        tok = tok.rstrip(_TRAILING).rstrip("/")
        if tok and tok not in out:
            out.append(tok)
    return out


def _display_root(base, root) -> str:
    """The first path segment the explorer shows for a root."""
    try:
        rel = root.relative_to(base)
        name = rel.as_posix()
    except (ValueError, OSError):
        name = ""
    name = name or (getattr(root, "name", "") or str(root))
    return "" if name == "." else name


def _score(frag: str, path: str, name: str) -> int | None:
    """Lower is better; None means the fragment does not match at all."""
    if not frag:
        return 3
    p = path.lower()
    n = name.lower()
    f = frag.lower()
    if n.startswith(f):
        return 0
    if f in n:
        return 1
    if p.endswith("/" + f) or p == f:
        return 1
    if f in p:
        return 2
    # Subsequence: "smte" should still find "state/migrations/telemetry.py".
    # Not for a short fragment — three letters scattered across a path are a
    # coincidence, and a completion list full of coincidences is worse than a
    # short one ("app" otherwise matches "alpha" for its a, p, p).
    if len(f) < 4:
        return None
    it = iter(f)
    head = next(it, None)
    if head is None:
        return 3
    for ch in p:
        if ch == head:
            head = next(it, None)
            if head is None:
                return 4
    return None


def mentions(base, roots, frag: str, limit: int = MENTION_LIMIT, *,
             skip_dirs=(), skip_files=(), visible=None,
             max_entries: int = MAX_WALK_ENTRIES) -> dict:
    """Paths that complete `@<frag>` — files AND folders, across every root.

    `roots` is the session's own allow-list, so a completion can never offer a
    path the session cannot read. Blocking directories are not walked, which
    also means they are never suggested.
    """
    frag = (frag or "").strip().replace("\\", "/").lstrip("/")
    # A fragment with a directory in it completes inside that directory only;
    # bare words match the whole workspace.
    want_dir, _, want_name = frag.rpartition("/")
    cap = max(1, min(int(limit or MENTION_LIMIT), MENTION_LIMIT))
    skip = {str(s).lower().rstrip("/\\") for s in skip_dirs}
    skipf = {str(s).lower() for s in skip_files}
    out: list[dict] = []
    seen: set[str] = set()
    dirs: set[str] = set()
    walked = 0
    stopped = False
    for root in roots:
        dr = _display_root(base, root)
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        stack = [(root, dr)]
        while stack:
            d, prefix = stack.pop()
            try:
                children = list(d.iterdir())
            except OSError:
                continue
            children.sort(key=lambda c: c.name.lower())
            for child in children:
                walked += 1
                if walked > max_entries:
                    stopped = True
                    break
                nm = child.name
                # Hidden dirs and the build/vendor trees are not walked, so
                # they are never suggested either — a completion that offers
                # `.git/config` is a leak, not a convenience.
                if nm.startswith(".") or nm.lower() in skip:
                    continue
                try:
                    is_dir = child.is_dir()
                except OSError:
                    continue
                path = posixpath.join(prefix, nm) if prefix else nm
                if is_dir:
                    if path.lower() not in dirs:
                        dirs.add(path.lower())
                        out.append({"path": path, "name": nm, "dir": True})
                    stack.append((child, path))
                    continue
                if nm.lower() in skipf or (visible and not visible(nm)):
                    continue
                out.append({"path": path, "name": nm, "dir": False})
            if stopped:
                break
        if stopped:
            break
    # `want_dir` narrows to one folder: "@src/|", and it must survive even when
    # the folder itself would score badly. Both spellings have to work — the
    # explorer's full form (`<root>/src/`) and the folder-relative form a
    # person types when they are already thinking inside the project (`src/`).
    # Matching only the prefix made "@src/" return nothing at all in a session
    # whose root is named something else, which reads as a broken completion.
    if want_dir:
        wd = want_dir.lower().rstrip("/")
        segs = [s for s in wd.split("/") if s]

        def _inside(row: dict) -> bool:
            here = posixpath.dirname(row["path"]).lower()
            if here.startswith(wd) or row["path"].lower().startswith(wd + "/"):
                return True
            parts = [s for s in here.split("/") if s]
            return bool(segs) and len(parts) >= len(segs) \
                and parts[-len(segs):] == segs

        out = [row for row in out if _inside(row)]
    scored: list[tuple[int, int, str, dict]] = []
    for row in out:
        s = _score(want_name, row["path"], row["name"])
        if s is None:
            continue
        if row["path"].lower() in seen:
            continue
        seen.add(row["path"].lower())
        scored.append((s, len(row["path"]), row["path"], row))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    rows = [t[3] for t in scored[:cap]]
    return {"matches": rows, "count": len(rows),
            "truncated": len(scored) > cap,
            "walked": walked, "stopped": stopped,
            "note": ("the folder tree is bigger than the walk budget; "
                     "early results only"
                     if stopped else "")}


# --- commit messages ---------------------------------------------------------

_DIFF_BUDGET = 60_000
_PER_FILE_BUDGET = 6_000

_SUBJECT_MAX = 72


def diff_prompt(status: str, diff: str, *, budget: int = _DIFF_BUDGET,
                per_file: int = _PER_FILE_BUDGET) -> dict:
    """Bind a status+diff blob into the text a model writes a commit from.

    Every changed FILE is always present (git status is never trimmed) and
    `omitted` lists the files whose hunks were cut, so the UI can say "4 of 31
    files sent without their diff" instead of implying the model saw
    everything. That distinction is the whole function: a silently shortened
    changeset produces a confident, wrong message.
    """
    status = (status or "").strip()
    budget = max(2_000, int(budget))
    per_file = max(500, int(per_file))
    files = _status_files(status)
    chunks = _split_diff(diff or "")
    body: list[str] = []
    used = 0
    omitted: list[str] = []
    for path, text in chunks:
        blob = f"diff --git a/{path} b/{path}\n{text}"
        if len(blob) > per_file:
            omitted.append(path)
            blob = blob[:per_file] + "\n… (hunks cut)"
        if used + len(blob) > budget:
            omitted.append(path)
            continue
        used += len(blob)
        body.append(blob)
    kept = [p for p, _t in chunks if p not in omitted]
    full = "CHANGED FILES (git status):\n" + (status or "(clean)") + \
        "\n\nDIFFS:\n" + "\n\n".join(body)
    if omitted:
        full += ("\n\n(Hunks for these files were left out of what you were "
                 "shown — mention them from their status line only, do not "
                 "describe their contents: " + ", ".join(omitted) + ")")
    return {"prompt": full, "chars": len(full), "files": files,
            "files_with_diffs": kept, "omitted": omitted,
            "truncated": bool(omitted), "status": status}


def _status_files(status: str) -> list[str]:
    out: list[str] = []
    for line in status.splitlines():
        row = line.rstrip()
        if len(row) < 4:
            continue
        path = row[3:].strip()
        if "->" in path:
            path = path.split("->")[-1].strip()
        path = path.strip('"')
        if path and path not in out:
            out.append(path)
    return out


def _split_diff(diff: str) -> list[tuple[str, str]]:
    """`(path, hunk text)` per file section of a unified diff."""
    chunks: list[tuple[str, str]] = []
    cur_path = ""
    cur: list[str] = []
    for line in (diff or "").splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur_path and cur:
                chunks.append((cur_path, "".join(cur)))
            m = re.search(r" b/(\S+)", line)
            cur_path = (m.group(1) if m else "")
            cur = []
            continue
        cur.append(line)
    if cur_path and cur:
        chunks.append((cur_path, "".join(cur)))
    return chunks


def parse_commit_reply(text: str) -> dict:
    """Split a model's reply into subject + body, without inventing either.

    Models wrap the answer in quotes, code fences and "Here is a commit
    message:" preambles. Those are stripped; the wording is not. A subject the
    model did not write is better than one this function wrote.
    """
    raw = (text or "").strip()
    raw = re.sub(r"^here (is|are) (a|the) (commit )?message:?\s*", "", raw,
                 flags=re.I)
    raw = re.sub(r"^```[a-z]*\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return {"subject": "", "body": "", "text": ""}
    subject = lines[0].strip('"').strip("'")
    if len(subject) > _SUBJECT_MAX * 2:
        cut = subject[:_SUBJECT_MAX].rsplit(" ", 1)
        subject = cut[0] if len(cut) > 1 and len(cut[0]) > 20 else subject[:_SUBJECT_MAX]
    body = "\n\n".join(ln for ln in lines[1:] if ln)
    return {"subject": subject, "body": body,
            "text": (subject + ("\n\n" + body if body else ""))}
