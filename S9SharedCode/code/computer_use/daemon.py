"""cua-driver daemon manager + JSON-RPC-over-pipe wrapper.

cua-driver speaks JSON over a transport that is a **Unix socket on macOS/
Linux** and a **Windows named pipe** on Windows (`\\.\pipe\cua-driver`).
The CLI exposes the same surface cross-platform, so we drive it through the
`cua-driver call <tool> '<json>'` subprocess interface rather than talking
to the socket directly — simpler, and the daemon owns the element-index
cache for us.

Key facts learned from the installed binary (0.19.3, Windows):
  - `cua-driver serve`            → starts the daemon (blocking; run detached)
  - `cua-driver status`           → "daemon is running" / "not running"
  - `cua-driver call TOOL '{...}'`→ returns a JSON result document
  - `cua-driver list-tools`       → one-line tool catalogue
  - `cua-driver describe TOOL`    → full input_schema for a tool
  - `cua-driver stop`             → stops the daemon
  - `cua-driver recording ...`    → start_recording / stop_recording / replay
  - Windows needs NO special permissions for native apps (unlike TCC on macOS).

The result document shape (observed):
  {"content":[{"type":"text","text":"...json or text..."}], "isError":false}
or for some tools a top-level structured payload. We normalise both.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── errors ──────────────────────────────────────────────────────────────────
class DaemonError(Exception):
    """Base class for daemon-facing failures."""


class PreconditionError(DaemonError):
    """Raised when the AX tree is empty / driver not usable (trap guard)."""


class PermissionsError(DaemonError):
    """Raised when required OS permissions are missing."""


# ── binary resolution ───────────────────────────────────────────────────────
def _binary() -> str | None:
    """Locate the cua-driver executable in the active uv venv."""
    # Prefer the venv next to this file's package root.
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / ".venv" / "Scripts" / "cua-driver.EXE",
        here.parent / ".venv" / "bin" / "cua-driver",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # Fall back to PATH.
    return shutil.which("cua-driver")


# ── daemon lifecycle ─────────────────────────────────────────────────────────
def ensure_daemon(timeout: float = 12.0) -> bool:
    """Start the cua-driver daemon if it is not already running AND responsive.

    On this Windows build the daemon self-terminates ~30s after a UIA window
    enumeration timeout, leaving a zombie process whose pipe is dead. So we
    don't just check `status` — we probe the pipe with a real (cheap) call and
    restart the daemon if the probe fails.

    Returns True if a daemon is reachable (already up or just started).
    Returns False if the binary is missing or the daemon cannot start.
    """
    bin_ = _binary()
    if bin_ is None:
        return False
    # If a daemon claims to be up, verify the pipe actually responds.
    if _is_running(bin_) and _pipe_responsive(bin_):
        return True
    # Either down or a zombie — kill any stale process, then start fresh.
    try:
        subprocess.run([bin_, "stop"], capture_output=True, text=True, timeout=5)
    except Exception:
        pass
    try:
        # Redirect to a log file (NOT a PIPE) and fully detach the process so
        # the daemon survives even when the spawning process exits. A PIPE
        # closes when the parent dies, which kills the daemon on short-lived
        # callers; a file handle does not.
        log_path = ROOT / "computer_use" / "daemon.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(log_path, "ab")
        subprocess.Popen(
            [bin_, "serve"],
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception:
        return False
    # Poll for readiness via a real pipe probe (not just process existence).
    for _ in range(int(timeout * 4)):
        time.sleep(0.25)
        if _pipe_responsive(bin_):
            return True
    return False


def _pipe_responsive(bin_: str) -> bool:
    """True only if the daemon's pipe answers a cheap call with real output
    (not the 'daemon is not running' error)."""
    try:
        out = subprocess.run(
            [bin_, "call", "get_desktop_state", "{}"],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
        )
        blob = ((out.stdout or "") + (out.stderr or "")).lower()
        # Dead/zombie if it reports not running, or returns no output at all.
        if "not running" in blob:
            return False
        if not (out.stdout or "").strip():
            return False
        return True
    except Exception:
        return False


def _is_running(bin_: str) -> bool:
    try:
        out = subprocess.run([bin_, "status"], capture_output=True, text=True,
                             timeout=5, encoding="utf-8", errors="replace")
        return "not running" not in out.stdout.lower()
    except Exception:
        return False


def stop_daemon() -> None:
    bin_ = _binary()
    if bin_:
        try:
            subprocess.run([bin_, "stop"], capture_output=True, text=True, timeout=5)
        except Exception:
            pass


# ── the JSON-RPC-style call wrapper ──────────────────────────────────────────
def call(tool: str, args: dict | None = None, *, timeout: float = 60.0) -> dict:
    """Invoke a cua-driver tool and return the normalised result dict.

    Raises:
      DaemonError        — binary missing / daemon not running
      PreconditionError  — driver returned isError with an empty-AX symptom
      RuntimeError       — tool returned isError (generic)
    """
    bin_ = _binary()
    if bin_ is None:
        raise DaemonError("cua-driver binary not found in venv or PATH")
    if not _is_running(bin_):
        raise DaemonError("cua-driver daemon is not running (call ensure_daemon())")

    payload = json.dumps(args or {})
    try:
        proc = subprocess.run(
            [bin_, "call", tool, payload],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise DaemonError(f"cua-driver call '{tool}' timed out after {timeout}s") from e

    raw = (proc.stdout or "").strip()
    if not raw:
        raise DaemonError(f"cua-driver call '{tool}' returned no output: {proc.stderr[:200]}")

    # Normalise: the CLI wraps results as {"content":[{"type":"text","text":...}]}
    # but some tools emit a bare JSON object. Handle both.
    data = _normalise(raw)

    is_error = bool(data.get("isError"))
    if is_error:
        msg = _extract_text(data) or json.dumps(data)[:300]
        if "empty" in msg.lower() or "element_count" in msg.lower() or "no elements" in msg.lower():
            raise PreconditionError(msg)
        raise RuntimeError(f"cua-driver tool '{tool}' error: {msg}")

    # Unwrap content[].text if present, else return structured payload.
    structured = data.get("structuredContent") or data.get("structured_content")
    if structured is not None:
        return structured
    text = _extract_text(data)
    if text:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {"text": text}
    return data


def _normalise(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Sometimes the CLI prints a leading log line; find the first '{'.
        idx = raw.find("{")
        if idx >= 0:
            try:
                return json.loads(raw[idx:])
            except json.JSONDecodeError:
                pass
    return {"content": [{"type": "text", "text": raw}]}


def _extract_text(data: dict) -> str | None:
    content = data.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or "")
        return "\n".join(p for p in parts if p) or None
    if isinstance(content, str):
        return content
    return None


# ── capability probe (permission pre-flight) ─────────────────────────────────
def capabilities() -> dict:
    """Structured report the Planner can read to pick a viable target.

    Front-loads the trap table: tells us AX ok?, screenshot ok?, daemon up?,
    elevated?, and lists running apps.
    """
    bin_ = _binary()
    report = {
        "binary_present": bin_ is not None,
        "daemon_running": False,
        "ax_ok": False,
        "screenshot_ok": False,
        "elevated": False,
        "platform": "windows" if hasattr(__import__("os"), "name") and __import__("os").name == "nt" else "posix",
        "apps": [],
        "note": "",
    }
    if bin_ is None:
        report["note"] = "cua-driver not installed"
        return report
    report["daemon_running"] = _is_running(bin_)
    if not report["daemon_running"]:
        report["note"] = "daemon not running"
        return report
    try:
        desktop = call("get_accessibility_tree", {}, timeout=15)
        report["ax_ok"] = bool(desktop.get("windows") or desktop.get("processes"))
    except Exception as e:
        report["note"] = f"AX probe failed: {e}"
    try:
        scr = call("get_desktop_state", {}, timeout=15)
        report["screenshot_ok"] = bool(scr.get("screenshot") or scr.get("image"))
    except Exception:
        pass
    try:
        apps = call("list_apps", {}, timeout=15)
        report["apps"] = [
            {"name": a.get("name"), "pid": a.get("pid"), "running": a.get("running")}
            for a in (apps.get("apps") or [])[:30]
        ]
    except Exception:
        pass
    return report
