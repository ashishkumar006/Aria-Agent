"""Phase 1 (L0, deterministic) — E2E production-readiness scenarios.

These run with NO network, NO LLM, NO Playwright, NO desktop daemon.
``run_skill`` is replaced by a scenario-driven fake (see e2e_harness); every
node the orchestrator executes is recorded in ``CALL_LOG``.

Covers: EA-01 (short-circuit path), EA-02 (full DAG), EA-05 (malformed
planner), EA-06 (MAX_NODES cap), EA-07 (short-circuit toggle OFF parity),
EB-01/02/03 (browser cascade paths), EE-01 (MCP tool catalogue).
"""
from e2e_harness import deterministic, CALL_LOG, run_executor, skills_called, last_output

import pytest


# ── A. Conversational core ──────────────────────────────────────────────────
def test_ea01_simple_query_shortcircuits(deterministic):
    deterministic["planner"] = {"answer": "The capital of France is Paris."}
    ans = run_executor("What is the capital of France?", "L0-ea01")
    assert "Paris" in ans
    assert skills_called() == ["planner"], skills_called()


def test_ea02_complex_query_full_dag(deterministic):
    deterministic["planner"] = {
        "nodes": [
            {"skill": "researcher", "inputs": ["USER_QUERY"],
             "metadata": {"label": "r1"}},
            {"skill": "formatter", "inputs": ["n:r1"],
             "metadata": {"label": "f1"}},
        ]
    }
    deterministic["researcher"] = {"text": "Paris is the capital of France."}
    deterministic["formatter"] = {"text": "Paris is the capital of France."}
    ans = run_executor("Tell me about France's capital.", "L0-ea02")
    assert "Paris" in ans
    assert skills_called() == ["planner", "researcher", "formatter"], skills_called()


def test_ea05_malformed_planner_json_graceful(deterministic):
    deterministic["planner"] = {"rejected": ["{'skill': 'formatter' BAD SPEC}"]}
    ans = run_executor("do something weird", "L0-ea05")
    # Must terminate cleanly (no crash / no hang) and yield a string answer.
    assert isinstance(ans, str)
    assert skills_called()[0] == "planner"


def test_ea06_node_cap_no_infinite_loop(deterministic):
    # A planner that emits more workers than MAX_NODES must be capped at
    # MAX_NODES, not at whatever the number used to be. The cap is a runaway
    # LOOP guard (MAX_NODES 60 -> 400 so a book-length plan's research fleet
    # fits); it is not a cap on plan quality, which is now uncapped.
    import flow
    nodes = [
        {"skill": "researcher", "inputs": ["USER_QUERY"],
         "metadata": {"label": f"w{i}"}}
        for i in range(flow.MAX_NODES + 20)
    ]
    deterministic["planner"] = {"nodes": nodes}
    deterministic["researcher"] = {"text": "x"}
    ans = run_executor("spawn many workers", "L0-ea06")
    assert isinstance(ans, str)
    # Planner + capped workers; never more than MAX_NODES executed nodes.
    assert len(skills_called()) <= flow.MAX_NODES, len(skills_called())
    assert skills_called()[0] == "planner"


def test_ea07_toggle_off_runs_full_dag(deterministic):
    # The old PLANNER_SHORTCIRCUIT module flag is gone; short-circuiting is
    # now data-driven (planner emits {"answer": ...} with no nodes, see
    # ea01). The toggle-off behaviour is exercised by planning a real DAG
    # for a trivial query: it must go through the formatter (no shortcut).
    deterministic["planner"] = {
        "nodes": [{"skill": "formatter", "inputs": ["USER_QUERY"]}]
    }
    deterministic["formatter"] = {"text": "Paris is the capital."}
    ans = run_executor("capital of France?", "L0-ea07")
    assert "Paris" in ans
    assert skills_called() == ["planner", "formatter"], skills_called()


# ── B. Browser cascade paths (mocked, no Playwright) ────────────────────────
def test_eb01_browser_extract_path(deterministic):
    deterministic["planner"] = {
        "nodes": [
            {"skill": "browser", "inputs": ["USER_QUERY"],
             "metadata": {"label": "b1", "url": "https://example.com"}},
            {"skill": "formatter", "inputs": ["n:b1"]},
        ]
    }
    deterministic["browser"] = {"path": "extract", "text": "Example Domain"}
    deterministic["formatter"] = {"text": "Example Domain"}
    ans = run_executor("summarise example.com", "L0-eb01")
    assert last_output("browser")["path"] == "extract"
    assert "browser" in skills_called() and "formatter" in skills_called()


def test_eb02_browser_a11y_path(deterministic):
    deterministic["planner"] = {
        "nodes": [
            {"skill": "browser", "inputs": ["USER_QUERY"],
             "metadata": {"label": "b1", "url": "https://hf.co/models"}},
            {"skill": "formatter", "inputs": ["n:b1"]},
        ]
    }
    deterministic["browser"] = {"path": "a11y", "text": "driven the UI"}
    deterministic["formatter"] = {"text": "driven the UI"}
    run_executor("use HF filters", "L0-eb02")
    assert last_output("browser")["path"] == "a11y"


def test_eb03_browser_vision_path(deterministic):
    deterministic["planner"] = {
        "nodes": [
            {"skill": "browser", "inputs": ["USER_QUERY"],
             "metadata": {"label": "b1", "force_path": "vision"}},
            {"skill": "formatter", "inputs": ["n:b1"]},
        ]
    }
    deterministic["browser"] = {"path": "vision", "text": "clicked the circle"}
    deterministic["formatter"] = {"text": "clicked the circle"}
    run_executor("click the red circle", "L0-eb03")
    assert last_output("browser")["path"] == "vision"


# ── E. MCP tool catalogue (EE-01) ────────────────────────────────────────────
def test_ee01_mcp_tool_catalogue():
    import asyncio
    import mcp_server as mcp_mod
    tools = asyncio.run(mcp_mod.mcp.list_tools())
    names = {t.name for t in tools}
    # 46-tool catalogue (matches tests/comprehensive/test_mcp_tools.py).
    # Was 40: render_document was counted as present while never having been
    # registered, so the model could not call it. +2 for list_channels and
    # check_channel, which tell the agent where a reminder can be delivered
    # and whether that channel's credential still works. +1 for
    # enable_scheduled: "re-enable that reminder" was answered by scheduling a
    # SECOND copy and leaving the original greyed out.
    # +3 for the coding-agent loop: run_checks (the project's own
    # lint/typecheck/tests - without it a file with a syntax error reports
    # success), git_inspect (review your own diff) and find_symbol
    # (definitions vs usages, which regex cannot separate).
    #
    # The count is deliberately an equality, not a floor: every addition has to
    # be a decision. `tests/test_mcp_tool_reachability.py` separately proves
    # all of them are also ADVERTISED, which registration alone does not.
    # 54 = 49 + 5 code-intelligence tools (document_symbols,
    # find_references, blast_radius, preview_edit, apply_symbol_edit).
    # 56 = + make_dir (BUG-035: folders were impossible) and + run_command,
    # the PowerShell escape hatch for a specific command the checker does not
    # cover (bounded + approval-gated).
    # Keeps this file in step with tests/comprehensive/test_mcp_tools.py.
    assert len(tools) == 56, f"expected 56 tools, got {len(tools)}"
    for expected in ("run_checks", "git_inspect", "find_symbol",
                     "list_scheduled", "web_search", "read_file", "list_dir",
                     "search_knowledge", "github_query", "read_artifact",
                     "schedule_task", "list_channels", "check_channel",
                     "enable_scheduled", "save_as_document", "record_observation", "recall_observations"):
        assert expected in names, f"missing MCP tool: {expected}"


if __name__ == "__main__":
    import pytest as _p
    _p.main([__file__, "-q"])
