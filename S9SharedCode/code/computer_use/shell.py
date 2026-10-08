"""L0 gated-shell fallback (charter §6, L0).

When the daemon is unavailable, expose the safe shell surface only
(run_command / read_file / write_file / open_app), all behind safety gates.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from computer_use.safety.gates import SafetyGates


class GatedShell:
    """Safe shell surface, gated by SafetyGates."""

    def __init__(self, safety: SafetyGates):
        self.safety = safety

    def run_command(self, command: str, *, force: bool = False) -> dict:
        blocked = self.safety.cmd_blocked(command)
        if blocked:
            self.safety.record_action("run_command", {"command": command},
                                      f"blocked: {blocked}")
            return {"status": "blocked", "message": blocked}
        if (not force and self.safety.needs_approval(
                "run_command", {"command": command})
                and not self.safety.already_approved(
                    "run_command", {"command": command})):
            aid = self.safety.create_approval("run_command", {"command": command})
            return {"status": "pending", "approval_id": aid,
                    "message": "Action requires your approval."}
        if self.safety.mode == "dry-run":
            self.safety.record_action("run_command", {"command": command},
                                      "dry-run")
            return {"status": "dry-run", "command": command,
                    "message": f"[dry-run] would run: {command}"}
        try:
            proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self.safety.record_action("run_command", {"command": command},
                                      "error: timeout")
            return {"status": "error", "message": "command timed out after 60s"}
        except Exception as e:
            self.safety.record_action("run_command", {"command": command},
                                      f"error: {type(e).__name__}")
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}
        self.safety.record_action("run_command", {"command": command},
                                  f"done rc={proc.returncode}")
        return {"status": "done", "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[:8000], "stderr": (proc.stderr or "")[:4000]}

    def read_file(self, path: str, *, force: bool = False) -> dict:
        if self.safety.path_blocked(path):
            self.safety.record_action("read_file", {"path": path},
                                      "blocked: protected path")
            return {"status": "blocked", "message": f"path '{path}' is protected"}
        # Gated like everything else: approval patterns may target sensitive
        # paths, and dry-run describes instead of returning real content.
        if (not force and self.safety.needs_approval("read_file", {"path": path})
                and not self.safety.already_approved("read_file", {"path": path})):
            aid = self.safety.create_approval("read_file", {"path": path})
            return {"status": "pending", "approval_id": aid,
                    "message": "Action requires your approval."}
        if self.safety.mode == "dry-run":
            self.safety.record_action("read_file", {"path": path}, "dry-run")
            return {"status": "dry-run", "path": path,
                    "message": f"[dry-run] would read: {path}"}
        p = Path(os.path.expanduser(path))
        if not p.exists():
            self.safety.record_action("read_file", {"path": path},
                                      "error: not found")
            return {"status": "error", "message": "file not found"}
        self.safety.record_action("read_file", {"path": path}, "done")
        return {"status": "done", "path": str(p),
                "content": p.read_text(encoding="utf-8", errors="replace")[:8000]}

    def write_file(self, path: str, content: str, *, force: bool = False) -> dict:
        if self.safety.path_blocked(path):
            self.safety.record_action("write_file", {"path": path},
                                      "blocked: protected path")
            return {"status": "blocked", "message": f"path '{path}' is protected"}
        # A file write is a mutation: it must go through the same approval
        # and dry-run gates as run_command/open_app, not around them.
        if (not force and self.safety.needs_approval("write_file", {"path": path})
                and not self.safety.already_approved("write_file", {"path": path})):
            aid = self.safety.create_approval("write_file", {"path": path})
            return {"status": "pending", "approval_id": aid,
                    "message": "Action requires your approval."}
        if self.safety.mode == "dry-run":
            self.safety.record_action("write_file", {"path": path}, "dry-run")
            return {"status": "dry-run", "path": path,
                    "message": f"[dry-run] would write {len(content)} bytes: {path}"}
        p = Path(os.path.expanduser(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self.safety.record_action("write_file", {"path": path},
                                  f"done bytes={len(content)}")
        return {"status": "done", "path": str(p), "bytes": len(content)}

    def open_app(self, app: str, *, force: bool = False) -> dict:
        # open_app is gated like everything else (previously ungated).
        if self.safety.path_blocked(app):
            self.safety.record_action("open_app", {"app": app},
                                      "blocked: protected path")
            return {"status": "blocked", "message": f"path '{app}' is protected"}
        if (not force and self.safety.needs_approval("open_app", {"app": app})
                and not self.safety.already_approved("open_app", {"app": app})):
            aid = self.safety.create_approval("open_app", {"app": app})
            return {"status": "pending", "approval_id": aid,
                    "message": "Action requires your approval."}
        if self.safety.mode == "dry-run":
            self.safety.record_action("open_app", {"app": app}, "dry-run")
            return {"status": "dry-run", "app": app,
                    "message": f"[dry-run] would open: {app}"}
        try:
            if os.name == "nt":
                subprocess.Popen(["cmd", "/c", "start", "", app], shell=False)
            else:
                subprocess.Popen(["open", app] if sys.platform == "darwin" else ["xdg-open", app])
        except Exception as e:
            self.safety.record_action("open_app", {"app": app},
                                      f"error: {type(e).__name__}")
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}
        self.safety.record_action("open_app", {"app": app}, "done")
        return {"status": "done", "opened": app}
