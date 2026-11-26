"""Per-session code workspaces — Studio slice 1.

Why a session owns its roots
----------------------------
`_CODE_BASE` / `_CODE_ALLOWED` in `agent_server.py` are process globals, and that
is a defect, not a simplification. Two console tabs re-root each other: opening a
folder in one silently changes what the other's explorer lists and what its save
writes into. Worse, `POST /api/code/workspace` re-roots an agent that is ALREADY
RUNNING — its next `edit_file` lands in a different project than the one the user
asked it to change. The Code section got away with this because it held one
workspace at a time; an IDE that keeps several sessions and several folders open
cannot.

So the workspace becomes a property of the session, and the process global
survives only as the fallback for requests that name no session — which is every
existing caller, so `/code` behaves exactly as it did.

Confinement is deliberately not reimplemented here. This module answers "which
folders does this session hold"; `agent_server._code_abs` and `mcp_server._safe`
stay the only two places that decide whether a path may be touched. Two
confinement mechanisms is how one of them gets forgotten.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import time
from pathlib import Path

MAX_ROOTS = 8
MAX_PATH_CHARS = 400
# A studio session id is minted by the client and becomes part of a filename, so
# it is constrained rather than sanitised. A `..` or a separator here would walk
# the roots store outside its own directory.
SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

# The session the CURRENT request belongs to. A ContextVar rather than a
# parameter because the answer is needed ~30 frames deep, inside
# `_code_abs`/`_code_listing`/`_code_agent_root`, and threading `sid` through
# every one of them invites the bug this file's own sibling modules document:
# a value read where it was never bound. It is set once per request and never
# reset — each request runs in its own task context, so a set cannot leak into
# the next request the way it would leak between threads.
_SID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "studio_session_sid", default="")


def state_root() -> Path:
    """The agent's state directory, derived the same way every other module
    derives it: `S9_STATE_DIR`, else `state/` beside this file."""
    root = os.environ.get("S9_STATE_DIR")
    return Path(root) if root else (Path(__file__).resolve().parent / "state")


def state_dir() -> Path:
    """Where this module's files live, read per call.

    Derived from `S9_STATE_DIR` at call time rather than captured at import,
    because `auth.state_dir()` is bound at import and a test that redirects the
    state dir after importing would silently write session roots into the LIVE
    state — the isolation hole this repo has already been bitten by once.
    """
    d = state_root() / "code-studio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _roots_dir() -> Path:
    d = state_dir() / "roots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _roots_path(sid: str) -> Path:
    return _roots_dir() / f"{sid}.json"


def _sessions_file() -> Path:
    """The index the studio's left column reads: which sessions exist, what
    they are called, and whether they are archived.

    Per-session root files already answer "what does this session hold"; they
    cannot answer "what sessions are there", because that would mean listing a
    directory the user's own ids name. An index is the honest shape, and it is
    written from `save()` so it cannot drift out of step with the roots."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "sessions.json"


def sessions() -> list[dict]:
    """Every known session, newest activity first.

    The index is reconciled against `roots/*.json` on read, because the index
    is newer than the roots store: sessions created before it existed have a
    folder list and no row, and a left column that shows "no sessions" while the
    user is working in one is worse than no column at all. Rows are never
    deleted here — a session whose folders were all closed is still a session
    the user made, and it stays listed with `roots: 0` to say so."""
    rows = [r for r in _read_json(_sessions_file()).get("sessions") or []
            if isinstance(r, dict) and r.get("sid")]
    known = {str(r.get("sid")) for r in rows}
    added = []
    for path in sorted(_roots_dir().glob("*.json")):
        sid = path.stem
        if not sid or sid in known or not valid_sid(sid):
            continue
        state = load(sid)
        if state is None:
            continue
        added.append({
            "sid": sid, "label": str(state.get("label") or ""),
            "created": state.get("created") or 0,
            "updated": state.get("updated") or 0,
            "roots": len(state.get("roots") or []),
            "paths": [str(r.get("path") or r.get("name") or "") for r in (state.get("roots") or [])][:8],
            "archived": False,
        })
        known.add(sid)
    if added:
        rows = rows + added
        _sessions_write(rows)
    return sorted(rows, key=lambda r: float(r.get("updated") or 0), reverse=True)


def _sessions_write(rows: list[dict]) -> None:
    path = _sessions_file()
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"sessions": rows, "updated": time.time()},
                              indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def touch_session(sid: str, label: str = "", roots: list[dict] | None = None,
                  archived: bool | None = None) -> dict:
    """Create or update one index row. Returns the row."""
    rows = [r for r in _read_json(_sessions_file()).get("sessions") or []
            if isinstance(r, dict) and r.get("sid")]
    found = next((r for r in rows if r.get("sid") == sid), None)
    if found is None:
        found = {"sid": sid, "label": "", "created": time.time(),
                 "roots": 0, "archived": False}
        rows.append(found)
    if label:
        found["label"] = label[:120]
    if roots is not None:
        found["roots"] = len(roots)
        found["paths"] = [str(r.get("name") or "") for r in roots][:8]
    if archived is not None:
        found["archived"] = bool(archived)
    found["updated"] = time.time()
    _sessions_write(rows)
    return found


def rename_session(sid: str, label: str) -> dict | None:
    return touch_session(sid, label=label) if valid_sid(sid) else None


def set_archived(sid: str, archived: bool) -> bool:
    if not valid_sid(sid):
        return False
    rows = _read_json(_sessions_file()).get("sessions") or []
    hit = any(isinstance(r, dict) and r.get("sid") == sid for r in rows)
    touch_session(sid, archived=bool(archived))
    return hit


def forget_session(sid: str) -> None:
    """Drop a session's index row. Its roots file is left alone: `clear` already
    removed the folders, and an id the index no longer names is unreachable."""
    rows = [r for r in (_read_json(_sessions_file()).get("sessions") or [])
            if isinstance(r, dict) and r.get("sid") != sid]
    _sessions_write(rows)


def valid_sid(sid: str) -> bool:
    return bool(sid) and bool(SID_RE.match(sid or ""))


def bind(sid: str) -> str:
    """Record the sid for the rest of this request; return the usable value.

    An unusable id binds to "" rather than raising: the honest reading of a
    malformed `?sid=` is "no session", which falls back to the legacy global
    workspace, and every caller already has to handle that case.
    """
    clean = (sid or "").strip()
    if not valid_sid(clean):
        clean = ""
    _SID.set(clean)
    return clean


def bound_sid() -> str:
    return _SID.get() or ""


def _expand(raw: str) -> Path | None:
    """Absolute, variable/user-expanded, symlink-resolved, or None."""
    raw = (raw or "").strip()
    if not raw or len(raw) > MAX_PATH_CHARS:
        return None
    try:
        p = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
        return p
    except (OSError, RuntimeError, ValueError):
        return None


# The sanctioned scratch areas inside the state dir. Declaring one of these is
# the normal case — the shipped default workspace and a studio playground both
# live under it. Nothing else inside the state dir is a legal workspace.
SCRATCH_SUBTREES = ("code_workspace", os.path.join("code-studio", "playgrounds"))


def forbidden_root(p: Path) -> str:
    """Why this folder must not be a workspace root, or "" when it may.

    The direction of this rule is easy to write backwards, and the failure mode
    is severe either way. A root that IS the state dir, or HOLDS it, exposes
    `sessions/`, `threads/` and `agent.token` to whoever drives the editor — the
    same hazard `_code_agent_root` records from the other side ("one folder in,
    one folder out"). But a root that lives INSIDE the state dir is the default
    playground, and rejecting descendants would refuse the one workspace the app
    ships with. So: ancestors and the state dir itself are refused, descendants
    are refused too EXCEPT under the two sanctioned scratch subtrees.

    A descendant is refused because `state/sessions` or `state/threads` would be
    an even better target for a confused "open folder" than `state` itself: it
    holds every other conversation's transcripts.
    """
    try:
        sd = state_root().resolve()
    except (OSError, RuntimeError):
        sd = None
    if sd is not None:
        if p == sd:
            return ("this folder is the agent's state directory: sessions, "
                    "threads and the per-launch token live here")
        if p in sd.parents:
            return "this folder holds the agent's state directory"
        inside_state = sd in p.parents
        if inside_state:
            scratch = [sd / sub for sub in SCRATCH_SUBTREES]
            if not any(_is_within(s, p) for s in scratch):
                return ("this folder is inside the agent's state directory; "
                        "only a playground or the default workspace may be")
    if str(p) in ("", "/", "\\"):
        return "pick a folder inside a drive, not the drive root"
    try:
        if p.parent == p:
            return "pick a folder inside a drive, not the drive root"
    except (OSError, ValueError):
        pass
    return ""


def _is_within(ancestor: Path, node: Path) -> bool:
    return node == ancestor or ancestor in node.parents


def validate_root(raw: str) -> tuple[Path | None, str]:
    """(resolved folder, "") or (None, reason). Never raises."""
    p = _expand(raw)
    if p is None:
        return None, "not a readable path"
    if not p.exists():
        return None, f"does not exist: {p.name or p}"
    if not p.is_dir():
        return None, f"not a folder: {p.name or p}"
    try:
        # Read-probe rather than trusting the stat: a folder can exist and be
        # listed and still refuse to open.
        next(iter(p.iterdir()), None)
    except (PermissionError, OSError) as e:
        return None, f"cannot be read ({type(e).__name__})"
    why = forbidden_root(p)
    if why:
        return None, why
    return p, ""


def load(sid: str) -> dict | None:
    if not valid_sid(sid):
        return None
    try:
        data = json.loads(_roots_path(sid).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    roots = [r for r in (data.get("roots") or []) if isinstance(r, dict)]
    return {"sid": sid, "roots": roots,
            "created": data.get("created") or 0,
            "updated": data.get("updated") or 0,
            "label": str(data.get("label") or "")[:120]}


def save(sid: str, roots: list[dict], label: str = "") -> dict:
    now = time.time()
    prior = load(sid) or {}
    data = {"sid": sid, "roots": roots, "label": (label or "")[:120],
            "created": prior.get("created") or now, "updated": now}
    path = _roots_path(sid)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    # The index is written from here rather than from each route so that a
    # session can be in the left column and out of date at the same time only
    # if this function itself failed.
    touch_session(sid, label=label or prior.get("label") or "", roots=roots)
    return data


def add_root(sid: str, raw: str) -> tuple[dict | None, str]:
    """Declare one more folder. Returns (state, "") or (None, reason)."""
    if not valid_sid(sid):
        return None, "sid is required and must be a plain id"
    folder, why = validate_root(raw)
    if folder is None:
        return None, why
    state = load(sid) or {"roots": []}
    have = [str(r.get("path") or "") for r in state["roots"]]
    if str(folder) in have:
        return public(sid), f"already a root: {folder.name}"
    if len(state["roots"]) >= MAX_ROOTS:
        return public(sid), f"at most {MAX_ROOTS} folders per session"
    state["roots"].append({"path": str(folder), "name": folder.name or str(folder),
                           "added": time.time()})
    save(sid, state["roots"], state.get("label") or "")
    return public(sid), ""


def remove_root(sid: str, raw: str = "") -> tuple[dict | None, str]:
    if not valid_sid(sid):
        return None, "sid is required and must be a plain id"
    state = load(sid)
    if not state:
        return None, "this session has no declared folders"
    folder, _why = _expand(raw), None
    keep = [r for r in state["roots"] if str(r.get("path") or "") != str(folder)]
    if len(keep) == len(state["roots"]):
        return public(sid), f"not a root of this session: {raw}"
    save(sid, keep, state.get("label") or "")
    return public(sid), ""


def clear(sid: str) -> None:
    if not valid_sid(sid):
        return
    try:
        _roots_path(sid).unlink()
    except OSError:
        pass
    # A session with no folders is still a session the user made; it stays in
    # the list so the workspace picker can show it as empty rather than
    # pretending it never existed.
    touch_session(sid, roots=[])


def public(sid: str) -> dict:
    """The response shape every roots route returns."""
    state = load(sid) or {"roots": []}
    return {"sid": sid,
            "roots": [{"path": str(r.get("path") or ""),
                       "name": str(r.get("name") or Path(str(r.get("path") or "")).name),
                       } for r in state["roots"]],
            "label": state.get("label") or "",
            "count": len(state["roots"]),
            "max": MAX_ROOTS}


def live_roots(sid: str) -> tuple[Path, ...]:
    """The session's folders that still resolve to a directory.

    A declared root that has since been deleted, moved, or turned into a file
    drops out here rather than being trusted: an allow-list that keeps naming a
    path which no longer exists is how a root becomes something else entirely
    with the same name. If NOTHING survives, the caller gets an empty tuple and
    the consumer fails closed — the absence of a workspace is not a licence to
    fall back to the process default, because that default is a DIFFERENT
    project and writing into it would be the bug this slice exists to fix.
    """
    out: list[Path] = []
    for r in (load(sid) or {}).get("roots") or []:
        p = _expand(str(r.get("path") or ""))
        if p is None:
            continue
        if forbidden_root(p):
            continue
        if p.is_dir():
            out.append(p)
    return tuple(out)


def base_for(roots: tuple[Path, ...]) -> Path | None:
    """The path client lookups are relative to.

    Existing convention: the base is the PARENT of a root so a client path reads
    "<folder>/<rel>" and the explorer's first level is folder names, not the
    whole filesystem. With several roots that only works when they share the
    same parent; otherwise the base is the common ancestor, and a path under a
    sibling of that ancestor is refused by the containment check rather than by
    luck. Returning the roots themselves is never needed: every allowed path is
    checked against `allowed`, not against `base`.
    """
    if not roots:
        return None
    first = roots[0]
    base = first.parent if first.parent != first else first
    if all(str(r).startswith(str(base) + os.sep) or r == base for r in roots):
        return base
    parts = [Path(r).parts for r in roots]
    common = parts[0]
    for p in parts[1:]:
        i = 0
        while i < len(common) and i < len(p) and common[i] == p[i]:
            i += 1
        common = common[:i]
    if not common:
        # No shared ancestor (different drives): there is no honest base, and
        # the containment test alone still confines, so anchor at the first
        # root's own directory and let `allowed` do the work.
        return first
    return Path(*common)


def resolve(sid: str = "") -> tuple[Path, tuple[Path, ...]] | None:
    """(base, allowed_roots) for one session, or None for "no session at all".

    `sid` empty means "whatever this request bound", which is how
    `agent_server._code_workspace()` asks the question without threading a
    parameter through thirty functions.

    Returning None ONLY when no session is bound is the whole safety of this
    function. A session that declares nothing — just created, or just reset, or
    whose folders were all moved — gets `(missing_base, ())`, and the empty
    allow-list refuses every path. Falling through to the process default there
    would mean a workspace reset silently re-pointed the editor at the shipped
    playground, and the first save would write into someone else's project. That
    is the bug this function exists to prevent, so it is spelled out rather than
    left to the caller.
    """
    want = sid or bound_sid()
    if not want:
        return None
    roots = live_roots(want)
    if not roots:
        return _missing_base(), ()
    base = base_for(roots)
    if base is None:
        return _missing_base(), ()
    return base, roots


def _missing_base() -> Path:
    """A base that contains nothing, for a session whose roots all vanished.

    It has to be a real path (the routes do `base / rel`) but it must not be a
    writable playground or the state dir: every lookup then resolves under a
    directory that does not exist and fails the containment test anyway.
    """
    return (state_dir() / "unbound")


def playground_dir(sid: str) -> Path:
    d = state_dir() / "playgrounds"
    d.mkdir(parents=True, exist_ok=True)
    return d / sid


def new_playground(sid: str) -> Path | None:
    """Create a fresh empty folder owned by this session and declare it."""
    if not valid_sid(sid):
        return None
    folder = playground_dir(sid)
    folder.mkdir(parents=True, exist_ok=True)
    _, why = add_root(sid, str(folder))
    if why and "already a root" not in why:
        return None
    return folder


def roots_env(sid: str = "") -> list[str]:
    """Absolute root paths for the MCP child's ARIA_ROOTS.

    The child is a separate process with no other way to learn which folders the
    session holds. Without this, a multi-root session confines the human routes
    to two folders and the AGENT to one — the asymmetry is what makes a tool
    write outside the project the user opened.
    """
    got = resolve(sid)
    return [str(p) for p in (got[1] if got else ())]
