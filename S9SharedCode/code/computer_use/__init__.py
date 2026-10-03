"""computer_use — layered computer-use desktop agent (charter §1-§13).

Public surface (stable for skills.py / agent_server.py):
  ComputerUseSkill, ComputerResult        — engine cascade
  ComputerUse, get_computer_use           — high-level request/approval API
  ensure_daemon, call, capabilities       — daemon
  DaemonError, PreconditionError, PermissionsError
  safety                                  — SafetyGates + shared_gates()

Internal packages (by function/vertical):
  core/    daemon + recording
  layers/  goal / perception / sequencing / recovery / vision
  engine/  cascade / actions / shell
  safety/  gates
  apps/    native / electron
  prompts/ prompt constants
"""
from __future__ import annotations

from computer_use.core.daemon import (
    DaemonError, PreconditionError, PermissionsError,
    ensure_daemon, stop_daemon, call, capabilities,
)
from computer_use.engine import ComputerUseSkill, ComputerResult
from computer_use.safety.gates import SafetyGates, shared_gates, reset_shared_gates
from computer_use.shell import GatedShell

__all__ = [
    "ComputerUseSkill", "ComputerResult",
    "ComputerUse", "get_computer_use",
    "DaemonError", "PreconditionError", "PermissionsError",
    "ensure_daemon", "stop_daemon", "call", "capabilities",
    "SafetyGates", "shared_gates", "reset_shared_gates",
    "GatedShell",
]


class ComputerUse:
    """High-level request/approval API used by the agent server."""

    def __init__(self, session_id: str = "s8-default",
                 llm_chat=None, llm_vision=None):
        self.safety = shared_gates()
        self.skill = ComputerUseSkill(
            session_id=session_id,
            llm_chat=llm_chat, llm_vision=llm_vision,
            safety=self.safety,
        )

    def request(self, kind: str, payload: dict):
        if not self.safety.enabled:
            return {"status": "disabled",
                    "message": "Computer-use is disabled. "
                               "Set COMPUTER_USE_ENABLED=true to opt in."}
        if kind == "drive_app":
            # skill.run returns a ComputerResult dataclass — normalise it to
            # the status-dict shape every other action returns so MCP/JSON
            # callers get a serialisable result instead of a raw object.
            try:
                res = self.skill.run(payload.get("goal", ""),
                                     app_hint=payload.get("app"),
                                     max_turns=int(payload.get("max_turns", 12) or 12))
            except Exception as e:
                return {"status": "error",
                        "message": f"{type(e).__name__}: {e}"}
            return {"status": "done" if res.success else "error",
                    "layer": res.layer,
                    "output": res.output,
                    "trace": res.trace[-20:],
                    "error": res.error,
                    "message": res.error or f"drive_app finished via {res.layer}"}
        shell = GatedShell(self.safety)
        if kind == "run_command":
            return shell.run_command(payload.get("command", ""))
        if kind == "read_file":
            return shell.read_file(payload.get("path", ""))
        if kind == "write_file":
            return shell.write_file(payload.get("path", ""),
                                    payload.get("content", ""))
        if kind == "open_app":
            return shell.open_app(payload.get("app", ""))
        return {"status": "error", "message": f"unknown request kind: {kind}"}

    def list_approvals(self):
        return self.safety.list_approvals()

    def resolve(self, approval_id: str, decision: str | None = None, *,
                 approve: bool | None = None):
        # Accept either `decision="approved"`/`"rejected"` or the boolean
        # `approve=True/False` kwarg (test/UI convenience).
        if approve is None:
            approve = (decision or "rejected").lower() != "rejected"
        result = self.safety.resolve(approval_id, approve)
        if result is None:
            return {"status": "not_found"}
        # Surface the resolution status for the caller (UI/test convenience).
        result = dict(result)
        result["status"] = "approved" if result.get("approve") else "rejected"
        return result


def get_computer_use(session_id: str = "s8-default", llm_chat=None, llm_vision=None):
    return ComputerUse(session_id=session_id, llm_chat=llm_chat, llm_vision=llm_vision)
