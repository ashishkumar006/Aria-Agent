"""Wiring self-test: imports, catalogs, shapes, signatures. No network."""
import sys

fails = []


def check(name, fn):
    try:
        fn()
        print(f"OK   {name}")
    except Exception as e:
        fails.append(name)
        print(f"FAIL {name}: {type(e).__name__}: {e}")


def t_imports():
    import skills, flow, mcp_server, mcp_runner, scheduler, turnlog
    import agent_server, gateway, persistence
    import computer_use, computer_use.engine, computer_use.shell
    import computer_use.safety.gates
    from computer_use import daemon as top_daemon
    from computer_use.core import daemon as core_daemon
    assert top_daemon.call is core_daemon.call, "daemon shim diverged"


def t_catalog_covers_yaml():
    import yaml
    from pathlib import Path
    from skills import _TOOL_CATALOG, tool_payload
    cfg = yaml.safe_load(open(Path("agent_config.yaml")))
    allowed = set()
    for _name, spec in cfg.items():
        if isinstance(spec, dict):
            allowed.update(spec.get("tools_allowed") or [])
    missing = allowed - set(_TOOL_CATALOG)
    assert not missing, f"tools_allowed missing from catalog: {missing}"
    assert tool_payload(sorted(allowed)) is not None
    assert tool_payload([]) is None
    assert "read_file" in _TOOL_CATALOG and "index_document" in _TOOL_CATALOG


def t_mcp_tools_have_catalog():
    import re
    src = open("mcp_server.py").read()
    names = re.findall(r"@mcp\.tool\(\)\s*\ndef (\w+)", src)
    from skills import _TOOL_CATALOG
    missing = [n for n in names if n not in _TOOL_CATALOG]
    assert not missing, f"mcp tools without catalog entry: {missing}"


def t_drive_app_shape():
    import inspect
    from computer_use import ComputerUse
    src = inspect.getsource(ComputerUse.request)
    assert 'kind == "drive_app"' in src and '"layer"' in src and '"status"' in src


def t_cost_tally_keys():
    from skills import _bump_cost
    from skills import _computer_cost_snapshot, _computer_cost_reset
    import inspect
    src = inspect.getsource(_bump_cost)
    assert "input_tokens" in src and "output_tokens" in src
    _computer_cost_reset()
    _bump_cost({"input_tokens": 10, "output_tokens": 5}, l2b=True)
    snap = _computer_cost_snapshot()
    assert snap["total_tokens"] >= 15, snap
    assert snap["l2b_calls"] == 1, snap
    _computer_cost_reset()
    assert _computer_cost_snapshot()["total_tokens"] == 0


def t_notify_exists():
    from agent_server import notify_task_done, list_notifications
    import inspect
    sig = inspect.signature(notify_task_done)
    assert list(sig.parameters) == ["query", "answer", "cost", "session_id"]
    import scheduler
    src = inspect.getsource(scheduler._fire)
    assert "notify_task_done" in src


def t_turnlog_sig():
    import inspect
    from turnlog import append, recent
    assert list(inspect.signature(append).parameters) == ["session_id", "query", "answer"]
    assert "session_id" in inspect.signature(recent).parameters


def t_server_routes():
    from agent_server import app
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    for p in ["/", "/app.js", "/style.css", "/api/health", "/api/chat",
              "/api/tts", "/api/tts/voices", "/api/stt",
              "/api/sessions", "/api/sessions/{session_id}/graph",
              "/api/cost", "/api/tools", "/api/config", "/api/notifications",
              "/api/memory", "/api/schedule", "/api/templates",
              "/api/audit", "/api/artifacts/{session_id}/{path}",
              "/api/computer/approvals",
              "/api/sessions/{session_id}/browser-shots",
              "/api/computer/runs", "/api/computer/runs/{run_id}"]:
        assert p in paths, f"missing route {p}"


def t_thread_mapping():
    # conversation_id -> stable persisted session_id; no id -> fresh sid.
    from agent_server import resolve_session
    assert resolve_session("conv-x") == resolve_session("conv-x")
    assert resolve_session(None) != resolve_session(None)
    assert resolve_session(None).startswith("s8-")


def t_export_audit():
    from computer_use.safety.gates import SafetyGates
    assert SafetyGates.export_audit(redact=True) == [] or isinstance(
        SafetyGates.export_audit(redact=True), list)


def t_stream_queue():
    # Regression guard: the chat stream must use asyncio.Queue (a blocking
    # threading.Queue.get() freezes the whole event loop mid-chat).
    import inspect
    from agent_server import _stream_run
    src = inspect.getsource(_stream_run)
    assert "asyncio.Queue" in src
    assert "log_q.get()" not in src


def t_gateway_contract():
    # Load by file path: plain `import schemas` would hit the already-loaded
    # LOCAL schemas module (same module name, different file).
    import importlib.util
    from pathlib import Path
    p = Path("../../llm_gatewayV9/schemas.py").resolve()
    spec = importlib.util.spec_from_file_location("gw_schemas_check", p)
    gs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gs)
    f = gs.ChatResponse.model_fields
    assert "input_tokens" in f and "output_tokens" in f and "usage" not in f


for name, fn in sorted({k: v for k, v in globals().items() if k.startswith("t_")}.items()):
    check(name, fn)

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
