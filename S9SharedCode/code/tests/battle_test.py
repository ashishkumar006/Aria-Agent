"""Standalone E2E battle-test of the Aria agent without the gateway.

Drives the real flow.Executor with the deterministic fake_run_skill from
e2e_harness. No network, no LLM, no Playwright, no desktop daemon.
"""
from __future__ import annotations

import asyncio
import sys
import time as _time

ROOT = r"C:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code"
sys.path.insert(0, ROOT)
sys.path.insert(0, f"{ROOT}\\tests")

import e2e_harness as h
import skills, flow, gateway, memory as _mem, persistence as _persist

CALL_LOG = h.CALL_LOG
SCENARIO = h.SCENARIO
CALL_COUNTS = h.CALL_COUNTS


def fake_run_skill(skill, node_id, graph_nodes, session_id, query, fr,
                   memory_hits=None, **kwargs):
    return h.fake_run_skill(skill, node_id, graph_nodes, session_id, query, fr,
                            memory_hits)


# Capture the real _atomic_write BEFORE any patching so the retry wrapper
# doesn't call itself (the original bug caused infinite recursion because
# `orig = _persist._atomic_write` was evaluated at call time, after the patch).
_ORIG_ATOMIC_WRITE = _persist._atomic_write

# TEST-POLLUTION FIX: setup() below replaces module-level symbols on the REAL
# skills/flow/gateway/memory/persistence modules. Because Python caches modules,
# every test file imported AFTER this one in the same pytest process inherited
# these fakes — 51 downstream failures that all vanished when this file ran
# alone. Snapshot the originals at import time and restore them after each
# test via the teardown fixture so this file is self-contained.
_ORIGINALS = {
    "skills.run_skill": skills.run_skill,
    "flow.run_skill": flow.run_skill,
    "gateway.ensure_gateway": gateway.ensure_gateway,
    "flow.ensure_gateway": flow.ensure_gateway,
    "memory.read": _mem.read,
    "flow._safe_remember": flow._safe_remember,
    "flow._safe_purge_working": flow._safe_purge_working,
    "persistence._atomic_write": _persist._atomic_write,
}


def _restore_originals():
    for dotted, val in _ORIGINALS.items():
        mod_name, attr = dotted.split(".")
        import importlib as _il
        setattr(_il.import_module(mod_name), attr, val)


def _atomic_write_retry(path, data):
    for _attempt in range(8):
        try:
            return _ORIG_ATOMIC_WRITE(path, data)
        except PermissionError:
            if _attempt == 7:
                raise
            _time.sleep(0.25)


def setup():
    CALL_LOG.clear()
    SCENARIO.clear()
    CALL_COUNTS.clear()
    skills.run_skill = fake_run_skill
    flow.run_skill = fake_run_skill
    gateway.ensure_gateway = lambda *a, **k: None
    flow.ensure_gateway = lambda *a, **k: None
    _mem.read = lambda q, top_k=5, **kw: []
    flow._safe_remember = lambda q, s: None
    flow._safe_purge_working = lambda s, t: None
    _persist._atomic_write = _atomic_write_retry


def run(query, session_id):
    return asyncio.run(flow.Executor().run(query, session_id=session_id))


# NOTE: pytest does NOT auto-load hook functions (pytest_runtest_teardown etc.)
# from test modules — only from conftest.py. An earlier attempt used a hook
# here and it silently never ran, so the pollution persisted. An autouse
# fixture defined in THIS module does apply to this module's tests.
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _restore_modules_after_each_test():
    yield
    _restore_originals()


def skills_called():
    return [c["skill"] for c in CALL_LOG]


def last_output(name):
    for rec in reversed(CALL_LOG):
        if rec["skill"] == name:
            return rec["output"]
    return None


# ── Scenarios ───────────────────────────────────────────────────────────────

def test_shortcircuit():
    setup()
    SCENARIO["planner"] = {"answer": "The capital of France is Paris."}
    ans = run("What is the capital of France?", "L0-ea01")
    assert "Paris" in ans, ans
    assert skills_called() == ["planner"], skills_called()
    print("PASS  short-circuit")


def test_full_dag():
    setup()
    SCENARIO["planner"] = {
        "nodes": [
            {"skill": "researcher", "inputs": ["USER_QUERY"],
             "metadata": {"label": "r1"}},
            {"skill": "formatter", "inputs": ["n:r1"],
             "metadata": {"label": "f1"}},
        ]
    }
    SCENARIO["researcher"] = {"text": "Paris is the capital of France."}
    SCENARIO["formatter"] = {"text": "Paris is the capital of France."}
    ans = run("Tell me about France's capital.", "L0-ea02")
    assert "Paris" in ans, ans
    assert skills_called() == ["planner", "researcher", "formatter"], skills_called()
    print("PASS  full DAG")


def test_malformed_planner():
    setup()
    SCENARIO["planner"] = {"rejected": ["{'skill': 'formatter' BAD SPEC}"]}
    ans = run("do something weird", "L0-ea05")
    # Planner failure is terminal — answer is empty string, but the run must
    # not crash or hang.
    assert isinstance(ans, str), ans
    assert skills_called()[0] == "planner"
    print("PASS  malformed planner (terminal failure)")


def test_node_cap():
    setup()
    nodes = [
        {"skill": "researcher", "inputs": ["USER_QUERY"],
         "metadata": {"label": f"w{i}"}}
        for i in range(70)
    ]
    SCENARIO["planner"] = {"nodes": nodes}
    SCENARIO["researcher"] = {"text": "x"}
    ans = run("spawn many workers", "L0-ea06")
    assert isinstance(ans, str) and ans, ans
    assert len(skills_called()) <= 60, skills_called()
    assert skills_called()[0] == "planner"
    print("PASS  node cap (MAX_NODES=60)")


def test_browser_extract():
    setup()
    SCENARIO["planner"] = {
        "nodes": [
            {"skill": "browser", "inputs": ["USER_QUERY"],
             "metadata": {"label": "b1", "url": "https://example.com"}},
            {"skill": "formatter", "inputs": ["n:b1"]},
        ]
    }
    SCENARIO["browser"] = {"path": "extract", "text": "Example Domain"}
    SCENARIO["formatter"] = {"text": "Example Domain"}
    run("summarise example.com", "L0-eb01")
    assert last_output("browser")["path"] == "extract"
    assert "browser" in skills_called() and "formatter" in skills_called()
    print("PASS  browser extract")


def test_browser_vision():
    setup()
    SCENARIO["planner"] = {
        "nodes": [
            {"skill": "browser", "inputs": ["USER_QUERY"],
             "metadata": {"label": "b1", "force_path": "vision"}},
            {"skill": "formatter", "inputs": ["n:b1"]},
        ]
    }
    SCENARIO["browser"] = {"path": "vision", "text": "clicked"}
    SCENARIO["formatter"] = {"text": "clicked"}
    run("click the red circle", "L0-eb03")
    assert last_output("browser")["path"] == "vision"
    print("PASS  browser vision")


def test_fan_out():
    setup()
    SCENARIO["planner"] = {
        "nodes": [
            {"skill": "researcher", "inputs": ["USER_QUERY"],
             "metadata": {"label": "r1", "question": "Q1"}},
            {"skill": "researcher", "inputs": ["USER_QUERY"],
             "metadata": {"label": "r2", "question": "Q2"}},
            {"skill": "formatter", "inputs": ["n:r1", "n:r2"],
             "metadata": {"label": "f1"}},
        ]
    }
    SCENARIO["researcher"] = {"text": "r1-result"}
    SCENARIO["formatter"] = {"text": "f1-result"}
    ans = run("compare London and Berlin populations", "L0-fan1")
    called = skills_called()
    assert called.count("researcher") == 2, called
    assert called[-1] == "formatter", called
    print("PASS  fan-out (parallel siblings)")


def test_critic_autoinsert():
    setup()
    SCENARIO["planner"] = {
        "nodes": [
            {"skill": "distiller", "inputs": ["USER_QUERY"],
             "metadata": {"label": "d1"}},
            {"skill": "formatter", "inputs": ["n:d1"],
             "metadata": {"label": "f1"}},
        ]
    }
    # distiller is `critic: true`, so after it succeeds the orchestrator
    # auto-inserts a Critic between distiller and formatter.
    SCENARIO["distiller"] = [
        {"text": "draft", "success": True},
        {"text": "final", "success": True},
    ]
    SCENARIO["critic"] = {"text": "pass"}
    ans = run("Write a haiku about cats.", "L0-critic1")
    called = skills_called()
    assert "critic" in called, called
    assert called.count("formatter") >= 1, called
    print("PASS  critic auto-insert")


def test_recovery_after_failure():
    setup()
    SCENARIO["planner"] = {
        "nodes": [
            {"skill": "researcher", "inputs": ["USER_QUERY"],
             "metadata": {"label": "r1"}},
            {"skill": "formatter", "inputs": ["n:r1"],
             "metadata": {"label": "f1"}},
        ]
    }
    SCENARIO["researcher"] = [
        {"text": "", "success": False, "error": "timeout"},
        {"text": "recovered", "success": True},
    ]
    SCENARIO["formatter"] = {"text": "recovered answer"}
    ans = run("research something flaky", "L0-rec1")
    assert "recovered" in ans, ans
    called = skills_called()
    assert called.count("researcher") == 2, called
    print("PASS  recovery retry")


def test_computer_skill_dispatched():
    setup()
    SCENARIO["planner"] = {
        "nodes": [
            {"skill": "computer", "inputs": ["USER_QUERY"],
             "metadata": {"label": "c1"}},
            {"skill": "formatter", "inputs": ["n:c1"],
             "metadata": {"label": "f1"}},
        ]
    }
    SCENARIO["computer"] = {"layer": "l2a", "text": "42"}
    SCENARIO["formatter"] = {"text": "42"}
    run("compute 2+2", "L0-comp1")
    called = skills_called()
    assert "computer" in called, called
    assert last_output("computer")["layer"] == "l2a"
    print("PASS  computer skill dispatch")


def test_action_skill():
    setup()
    SCENARIO["planner"] = {
        "nodes": [
            {"skill": "action", "inputs": ["USER_QUERY"],
             "metadata": {"label": "a1"}},
            {"skill": "formatter", "inputs": ["n:a1"],
             "metadata": {"label": "f1"}},
        ]
    }
    SCENARIO["action"] = {"text": "ok", "ok": True}
    SCENARIO["formatter"] = {"text": "done"}
    run("send telegram hello", "L0-act1")
    assert "action" in skills_called()
    print("PASS  action skill dispatch")


def test_mcp_tool_catalogue():
    setup()
    import asyncio
    import mcp_server as mcp_mod
    tools = asyncio.run(mcp_mod.mcp.list_tools())
    names = {t.name for t in tools}
    # 21 original + 3 scheduler CRUD + slack_refresh_token +
    # calendar_refresh_token + discord_message + index_document - weather = 26,
    # + 11 expansion tools (arxiv/wikipedia/openalex/news/fetch_pdf/
    # extract_tables/wayback/calendar_query/slack_history/delete_file/
    # search_files) = 37, + read_artifact (expand a spilled upstream
# result) = 38, + render_document = 39, and _doc_stats removed again = 40.
# render_document was always counted here as if it existed, but it was a bare
# function with no @mcp.tool(), so it was not in the list at all and every
# model call for it returned "Unknown tool: render_document". _doc_stats is a
# private helper for render_document and was likewise counted as a tool, which
# let a model ask it for a word count instead of rendering a document.
    assert len(tools) == 40, f"expected 40 tools, got {len(tools)}"
    assert "render_document" in names, \
        "the deliverable tool must be a real, callable MCP tool"
    # get_time was dropped here: the model's own clock covers it, and the tool
    # was not even reachable from the research DAG.
    assert "get_time" not in names, "get_time should have been removed"
    for expected in ("web_search", "read_file", "list_dir",
                     "search_knowledge", "github_query", "read_artifact",
                     "schedule_task", "list_scheduled", "cancel_scheduled"):
        assert expected in names, f"missing MCP tool: {expected}"
    print("PASS  MCP tool catalogue (40 tools)")


def test_toggle_off_runs_full_dag():
    setup()
    # The old PLANNER_SHORTCIRCUIT module flag is gone; short-circuiting is
    # now data-driven (planner emits {"answer": ...} with no nodes). The
    # toggle-off behaviour is exercised by simply planning a real DAG.
    SCENARIO["planner"] = {
        "nodes": [{"skill": "formatter", "inputs": ["USER_QUERY"]}]
    }
    SCENARIO["formatter"] = {"text": "Paris is the capital."}
    ans = run("capital of France?", "L0-ea07")
    assert "Paris" in ans, ans
    assert skills_called() == ["planner", "formatter"], skills_called()
    print("PASS  planned DAG (no short-circuit)")


if __name__ == "__main__":
    tests = [
        test_shortcircuit,
        test_full_dag,
        test_malformed_planner,
        test_node_cap,
        test_browser_extract,
        test_browser_vision,
        test_fan_out,
        test_critic_autoinsert,
        test_recovery_after_failure,
        test_computer_skill_dispatched,
        test_action_skill,
        test_mcp_tool_catalogue,
        test_toggle_off_runs_full_dag,
    ]
    failed = []
    for t in tests:
        try:
            t()
        except Exception as e:
            failed.append((t.__name__, e))
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests)-len(failed)}/{len(tests)} passed")
    if failed:
        print("FAILURES:")
        for name, err in failed:
            print(f"  {name}: {err}")
        sys.exit(1)
