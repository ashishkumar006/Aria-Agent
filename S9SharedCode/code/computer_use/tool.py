"""In-process `computer_action` dispatch.

The MCP server runs in a child process, so an approval minted there lives in
that child's SafetyGates and never appears on the agent's Computer page. Chat
and the researcher call this module in the agent process instead, which keeps
the approval store they see the same one the UI resolves.
"""
from __future__ import annotations

from . import DaemonError, call, get_computer_use
from .safety import shared_gates

HIGH_LEVEL = frozenset({
    "drive_app", "run_command", "read_file", "write_file", "open_app",
})
SENSITIVE_LOW = frozenset({
    "kill_app", "click", "type_text", "press_key", "hotkey",
    "launch_app", "replay_trajectory", "start_recording",
})
READONLY_LOW = frozenset({
    "get_accessibility_tree", "get_window_state", "get_desktop_state",
    "list_apps", "stop_recording",
})

_DRY_RUN = ("No approval was created: nothing executes in dry-run. "
            "Restart the agent with -ComputerUseMode live to run it.")


def _judge_callbacks():
    """The gateway-backed L2b text and L3 vision judges.

    `drive_app` is a layered engine: L2b needs a cheap text judge and L3
    needs a vision judge. Constructing it with no callbacks left the a11y
    layer unable to choose an action, so every chat-initiated run fell
    through to "no screenshot or vision LLM" even on an app with a usable
    accessibility tree.

    Imported lazily because `skills` imports this package; a bare MCP child
    may not have the application module on its path, and then the engine
    still runs L1/L2a rather than failing to import at module load.
    """
    try:
        from skills import _v9_judge_chat, _v9_judge_vision
        return _v9_judge_chat, _v9_judge_vision
    except Exception:
        return None, None


def tool_dispatch(action: str, params: dict | None = None) -> dict:
    """Run one gated computer-use action in THIS process."""
    action = str(action or "").strip()
    params = dict(params) if isinstance(params, dict) else {}

    if action in HIGH_LEVEL:
        llm_chat, llm_vision = _judge_callbacks()
        return get_computer_use(llm_chat=llm_chat,
                                llm_vision=llm_vision).request(action, params)

    gates = shared_gates()
    if not gates.enabled:
        return {"status": "disabled",
                "message": "Computer-use is disabled. "
                           "Set COMPUTER_USE_ENABLED=true to opt in."}

    if action in SENSITIVE_LOW:
        if gates.mode == "dry-run":
            return {"status": "dry-run", "action": action, "params": params,
                    "message": f"[dry-run] would {action}. {_DRY_RUN}"}
        if (gates.needs_approval(action, params)
                and not gates.already_approved(action, params)):
            aid = gates.create_approval(action, params)
            return {"status": "pending", "approval_id": aid,
                    "message": "Action requires your approval in the "
                               "Computer page."}
    elif action not in READONLY_LOW:
        if gates.mode == "dry-run":
            return {"status": "dry-run", "action": action, "params": params,
                    "message": f"[dry-run] would {action}. {_DRY_RUN}"}
        # Unknown low-level actions stay fail-closed: an approval is minted
        # even if an identical one was approved before.
        aid = gates.create_approval(action, params)
        return {"status": "pending", "approval_id": aid,
                "message": "Unknown low-level action requires approval."}

    try:
        result = call(action, params, timeout=60)
        if isinstance(result, dict) and "status" not in result:
            return {"status": "done", "result": result}
        return result
    except DaemonError as e:
        return {"status": "error", "message": f"daemon error: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}
