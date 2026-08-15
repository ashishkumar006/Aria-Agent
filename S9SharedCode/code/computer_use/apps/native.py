"""apps/native.py — native (AX-tree) app verticals (charter §5).

Standard platform-UI apps expose a full accessibility tree. This module
holds per-app helpers that build on the generic daemon primitives: launch,
find an element by label, click it, read a value. These are the "driveable"
targets on the native side of the charter's table.

Each helper returns structured data so the engine / tests can verify.
"""
from __future__ import annotations

import time
from pathlib import Path
from computer_use.core.daemon import call, DaemonError

ROOT = Path(__file__).resolve().parent.parent.parent


def launch(app_name: str, timeout: float = 20.0) -> dict:
    """Launch an app by name; return {pid, window_id} or raise.

    On Windows, some apps restore the previous session/file instead of
    opening a fresh blank instance. To avoid stale state, we first try to
    close any existing windows for the same app, then launch.
    """
    try:
        tree = call("get_accessibility_tree", {}, timeout=timeout)
        for w in (tree.get("windows") or []):
            title = (w.get("title") or "").lower()
            if app_name.lower() in title:
                try:
                    call("kill_app", {"pid": w["pid"]}, timeout=10)
                except Exception:
                    pass
                time.sleep(1)
    except Exception:
        pass

    r = call("launch_app", {"name": app_name}, timeout=timeout)
    pid = r.get("pid")
    wid = (r.get("windows") or [{}])[0].get("window_id")
    if pid is None or wid is None:
        raise DaemonError(f"launch_app('{app_name}') returned no window")
    return {"pid": pid, "window_id": wid}


def fresh_document_path(sandbox_root: Path | None = None, ext: str = "txt",
                       name: str | None = None) -> str:
    """Create a brand-new EMPTY file in the sandbox and return its path.

    Modern Notepad (and similar Store apps) restore the previous session /
    file on launch, so a blank 'Untitled' instance still shows old content.
    Opening a specific fresh file sidesteps that entirely.

    If `name` is given (e.g. the user asked for 'best_cars.txt'), the file is
    created with THAT name so the agent never has to rename it later — a
    rename via Save As is exactly what used to make the cascade loop forever.
    """
    root = sandbox_root or (ROOT / "state" / "computer_use_sandbox")
    root.mkdir(parents=True, exist_ok=True)
    if name:
        safe = name.strip().strip("'\"")
        if not safe.lower().endswith(".txt"):
            safe += f".{ext}"
        # Keep only filesystem-safe characters.
        safe = "".join(c for c in safe if c.isalnum() or c in "._- ")
        path = root / safe
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + str(int(time.time() * 1000) % 1000)
        path = root / f"note-{stamp}.{ext}"
    path.write_text("", encoding="utf-8")
    return str(path)


def launch_fresh(app_name: str, sandbox_root: Path | None = None,
                 ext: str = "txt", timeout: float = 20.0,
                 name: str | None = None) -> dict:
    """Launch an app, opening a NEW empty file in the sandbox when needed.

    Only apps that restore a stale session/file on launch (Notepad, WordPad)
    receive a fresh file path. All other apps launch normally.

    If `name` is supplied the fresh file is created with that exact name, so
    the agent writes straight into the correctly-named document and only has
    to Ctrl+S — no Save As / rename step that it cannot perform via the AX tree.
    """
    path = fresh_document_path(sandbox_root, ext, name=name)
    # Apps that restore previous session/file — must open a specific fresh file.
    file_apps = {"notepad", "wordpad", "paint"}
    if app_name.lower() in file_apps:
        r = call("launch_app", {"name": app_name, "additional_arguments": [path]},
                 timeout=timeout)
    else:
        r = call("launch_app", {"name": app_name}, timeout=timeout)
    pid = r.get("pid")
    wid = (r.get("windows") or [{}])[0].get("window_id")
    if pid is None or wid is None:
        raise DaemonError(f"launch_app('{app_name}') returned no window")
    return {"pid": pid, "window_id": wid, "path": path}


def find_element(tree_markdown: str, label_substr: str) -> int | None:
    """Return the element_index whose label contains label_substr.

    The driver markdown is indented and prefixed like `  - [N] Role "Label"`;
    we strip the leading `- ` and `[N]` to parse robustly.
    """
    for line in tree_markdown.splitlines():
        s = line.strip().lstrip("-").strip()
        if s.startswith("[") and "]" in s:
            idx = s[1:s.index("]")]
            if label_substr.lower() in s.lower():
                try:
                    return int(idx)
                except ValueError:
                    pass
    return None


def get_state(pid: int, window_id, *, include_screenshot: bool = True,
              timeout: float = 30.0) -> dict:
    """Fetch window state (AX tree + optional screenshot)."""
    return call("get_window_state",
                {"pid": pid, "window_id": window_id,
                 "include_screenshot": include_screenshot}, timeout=timeout)


def click_element(pid: int, window_id, element_index: int, snapshot_id: str | None,
                  timeout: float = 15.0) -> dict:
    """Click an element by index (requires snapshot_id per Cua Driver 0.17)."""
    args = {"pid": pid, "window_id": window_id, "element_index": element_index}
    if snapshot_id:
        args["snapshot_id"] = snapshot_id
    return call("click", args, timeout=timeout)


def type_text(pid: int, window_id, text: str, snapshot_id: str | None,
              element_index: int | None = None, timeout: float = 15.0) -> dict:
    """Type text into the focused element (or a specific element)."""
    args = {"pid": pid, "window_id": window_id, "text": text}
    if snapshot_id:
        args["snapshot_id"] = snapshot_id
    if element_index is not None:
        args["element_index"] = element_index
    return call("type_text", args, timeout=timeout)


# ── Calculator vertical ──────────────────────────────────
def calculator_compute(expression: str) -> dict:
    """Open Calculator, type an expression, press Enter, read the result.

    `expression` is a string like '2+2' or '5*3'. Returns the display value.
    """
    t = launch("Calculator")
    pid, wid = t["pid"], t["window_id"]
    time.sleep(2)
    st = get_state(pid, wid)
    snap = st.get("snapshot_id")
    # Map simple operators to button labels.
    label_map = {
        "0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four",
        "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine",
        "+": "Plus", "-": "Minus", "*": "Multiply by", "/": "Divide by",
        "=": "Equals",
    }
    for ch in expression:
        lbl = label_map.get(ch, ch)
        idx = find_element(st.get("tree_markdown") or "", f'"{lbl}"')
        if idx is not None:
            click_element(pid, wid, idx, snap)
            time.sleep(0.3)
    time.sleep(0.8)
    st2 = get_state(pid, wid)
    display = ""
    for line in (st2.get("tree_markdown") or "").splitlines():
        if "Display is" in line:
            display = line.strip()
    call("kill_app", {"pid": pid}, timeout=15)
    return {"expression": expression, "display": display}


# ── Settings vertical (brightness / night light) ─────────
def settings_open_display() -> dict | None:
    """Open Settings → Display, return the window handle + state, or None."""
    t = launch("Settings")
    pid, wid = t["pid"], t["window_id"]
    time.sleep(2)
    st = get_state(pid, wid)
    snap = st.get("snapshot_id")
    disp = find_element(st.get("tree_markdown") or "", "ListItem")
    # find the Display list item specifically
    for line in (st.get("tree_markdown") or "").splitlines():
        s = line.strip().lstrip("-").strip()
        if s.startswith("[") and "Display" in s and "ListItem" in s:
            disp = int(s[1:s.index("]")])
            break
    if disp is None:
        return None
    click_element(pid, wid, disp, snap)
    time.sleep(1.5)
    return {"pid": pid, "window_id": wid, "state": get_state(pid, wid)}
