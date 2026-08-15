"""safety/gates.py — safety gates for computer-use (charter §13).

These gates wrap the *destructive / sensitive* surface so the agent can
never silently do something irreversible. They apply to BOTH the
gated-shell L0 path and any desktop action the layered engine wants to gate.

Gates (in order):
  1. ENABLED  — computer-use is OFF unless COMPUTER_USE_ENABLED=true.
  2. MODE     — dry-run (default) describes only; live actually executes.
  3. ALLOW/DENY — command substrings / paths can be pinned to lists.
  4. APPROVAL — sensitive patterns return a pending token; the web UI
                resolves it (approve/reject) before execution.
  5. AUDIT    — every request + outcome appended to state/computer_use.log.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
AUDIT_PATH = ROOT / "state" / "computer_use.log"
AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

# Patterns that ALWAYS require human approval, even in live mode.
DEFAULT_APPROVAL_PATTERNS = (
    "rm ", "del ", "format", "mkfs", "shutdown", "reboot", "restart",
    "sudo", "runas", "reg ", "sc delete", "taskkill", "net user",
    "curl ", "wget ", "powershell -enc", "Invoke-WebRequest",
    "kill_app", "rmdir", "rd ", "format ", "diskpart",
)
# Paths that are NEVER allowed to be touched (defence in depth).
DEFAULT_DENY_PATHS = (
    "C:\\Windows", "C:\\Program Files", "/etc", "/System",
    os.path.expanduser("~/.ssh"), os.path.expanduser("~/.aws"),
)


@dataclass
class Approval:
    id: str
    action: str
    params: dict
    created: float = field(default_factory=time.time)
    status: str = "pending"  # pending | approved | rejected


class SafetyGates:
    def __init__(self):
        self.enabled = os.environ.get("COMPUTER_USE_ENABLED", "false").lower() == "true"
        self.mode = os.environ.get("COMPUTER_USE_MODE", "dry-run").lower()
        self.allow_cmds = [c for c in os.environ.get("COMPUTER_USE_ALLOW", "").split(";") if c]
        self.deny_cmds = [c for c in os.environ.get("COMPUTER_USE_DENY", "").split(";") if c]
        self.approval_patterns = DEFAULT_APPROVAL_PATTERNS
        self.deny_paths = DEFAULT_DENY_PATHS
        self._approvals: dict[str, Approval] = {}
        self._lock = threading.Lock()
        self._seq = 0

    # ── audit ────────────────────────────────────────────
    def _audit(self, action: str, params: dict, outcome: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {action} {outcome} {params}\n"
        try:
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    # ── gate checks ──────────────────────────────────────
    def needs_approval(self, action: str, params: dict) -> bool:
        blob = f"{action} {params}".lower()
        return any(p in blob for p in self.approval_patterns)

    def path_blocked(self, path: str) -> bool:
        p = os.path.abspath(os.path.expanduser(path))
        return any(p.startswith(os.path.abspath(d)) for d in self.deny_paths)

    def cmd_blocked(self, cmd: str) -> str | None:
        c = cmd.lower()
        for d in self.deny_cmds:
            if d and d.lower() in c:
                return f"matches denylist pattern '{d}'"
        for d in DEFAULT_DENY_PATHS:
            if d.lower() in c:
                return f"references protected path '{d}'"
        if self.allow_cmds and not any(a.lower() in c for a in self.allow_cmds):
            return "not on the command allow-list"
        return None

    # ── approval store ────────────────────────────────────
    def create_approval(self, action: str, params: dict) -> str:
        with self._lock:
            self._seq += 1
            aid = f"cu-{int(time.time())}-{self._seq}"
            self._approvals[aid] = Approval(id=aid, action=action, params=params)
        self._audit(action, params, "pending-approval")
        return aid

    def list_approvals(self) -> list[dict]:
        with self._lock:
            return [{"id": a.id, "action": a.action, "params": a.params,
                     "status": a.status, "created": a.created}
                    for a in self._approvals.values() if a.status == "pending"]

    def resolve(self, approval_id: str, approve: bool) -> dict | None:
        with self._lock:
            a = self._approvals.get(approval_id)
            if not a:
                return None
            a.status = "approved" if approve else "rejected"
        if not approve:
            self._audit(a.action, a.params, "rejected")
        return {"action": a.action, "params": a.params, "approve": approve}


# ── shared singleton ─────────────────────────────────────
_shared_gates: SafetyGates | None = None


def shared_gates() -> SafetyGates:
    global _shared_gates
    if _shared_gates is None:
        _shared_gates = SafetyGates()
    return _shared_gates


def reset_shared_gates() -> None:
    """Drop the cached singleton so the next call re-reads env vars."""
    global _shared_gates
    _shared_gates = None
