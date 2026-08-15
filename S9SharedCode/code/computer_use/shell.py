"""L0 gated-shell fallback (charter §6, L0).

When the daemon is unavailable, expose the safe shell surface only
(run_command / read_file / write_file / open_app), all behind safety gates.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from computer_use.safety.gates import SafetyGates


class GatedShell:
    """Safe shell surface, gated by SafetyGates."""

    def __init__(self, safety: SafetyGates):
        self.safety = safety

    def run_command(self, command: str, *, force: bool = False) -> dict:
        if self.safety.cmd_blocked(command):
            return {"status": "blocked", "message": self.safety.cmd_blocked(command)}
        if not force and self.safety.needs_approval("run_command", {"command": command}):
            aid = self.safety.create_approval("run_command", {"command": command})
            return {"status": "pending", "approval_id": aid,
                    "message": "Action requires your approval."}
        if self.safety.mode == "dry-run":
            return {"status": "dry-run", "command": command,
                    "message": f"[dry-run] would run: {command}"}
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
        return {"status": "done", "returncode": proc.returncode,
                "stdout": proc.stdout[:8000], "stderr": proc.stderr[:4000]}

    def read_file(self, path: str, *, force: bool = False) -> dict:
        if self.safety.path_blocked(path):
            return {"status": "blocked", "message": f"path '{path}' is protected"}
        p = Path(os.path.expanduser(path))
        if not p.exists():
            return {"status": "error", "message": "file not found"}
        return {"status": "done", "path": str(p),
                "content": p.read_text(encoding="utf-8", errors="replace")[:8000]}

    def write_file(self, path: str, content: str, *, force: bool = False) -> dict:
        if self.safety.path_blocked(path):
            return {"status": "blocked", "message": f"path '{path}' is protected"}
        p = Path(os.path.expanduser(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"status": "done", "path": str(p), "bytes": len(content)}

    def open_app(self, app: str, *, force: bool = False) -> dict:
        if os.name == "nt":
            subprocess.Popen(["cmd", "/c", "start", "", app], shell=False)
        else:
            subprocess.Popen(["open", app] if os.name == "posix" else ["xdg-open", app])
        return {"status": "done", "opened": app}
