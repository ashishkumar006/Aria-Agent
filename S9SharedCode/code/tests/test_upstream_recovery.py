"""A dead node must not starve its consumer of results that already exist.

Live: a standard research run planned three researchers -> author -> formatter.
The gateway returned 502 to one researcher and to the author, so both were
marked `skipped`. The formatter's only input was the author, so
`resolve_inputs` handed it a single `upstream-missing` entry and it answered
"No information or input data was provided by the upstream nodes" - while two
complete researcher findings sat in the same graph. These tests pin the
recovery that walks a skipped node's inputs and supplies the completed ancestors.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from schemas import AgentResult
from skills import resolve_inputs


def _node(skill, inputs, result=None):
    return {"skill": skill, "inputs": list(inputs), "result": result}


def test_skipped_synthesis_node_yields_its_completed_ancestors():
    nodes = {
        "n:1": _node("planner", ["USER_QUERY"]),
        "n:2": _node("researcher", ["USER_QUERY"], AgentResult(
            success=True, agent_name="researcher",
            output={"findings": "SQLite is row-oriented."})),
        "n:3": _node("researcher", ["USER_QUERY"], AgentResult(
            success=True, agent_name="researcher",
            output={"findings": "DuckDB is columnar."})),
        "n:4": _node("researcher", ["USER_QUERY"]),      # skipped by the run
        "n:5": _node("author", ["n:2", "n:3", "n:4"]),   # skipped by the run
        "n:6": _node("formatter", ["n:5"]),
    }
    resolved = resolve_inputs(nodes["n:6"]["inputs"], nodes, "compare")
    ids = [r.get("id") for r in resolved]
    kinds = [r.get("kind") for r in resolved]
    assert "n:5" in ids and kinds[ids.index("n:5")] == "upstream-missing"
    assert ids.count("n:2") == 1 and ids.count("n:3") == 1
    assert all(r.get("kind") == "upstream" for r in resolved
               if r.get("id") in ("n:2", "n:3"))
    # The provenance is explicit, so a prompt can explain the gap.
    assert {r.get("recovered_via") for r in resolved
            if r.get("id") in ("n:2", "n:3")} == {"n:5"}


def test_failed_result_object_is_not_mistaken_for_data():
    """The live shape: the node RAN and failed, so `result` is a real
    AgentResult with success=False and output={}. It must be treated as
    missing, not serialised as an empty upstream payload."""
    nodes = {
        "n:2": _node("researcher", ["USER_QUERY"], AgentResult(
            success=True, agent_name="researcher",
            output={"findings": "DuckDB is columnar."})),
        "n:3": _node("author", ["n:2"], AgentResult(
            success=False, agent_name="author", output={},
            error="exception: HTTPStatusError: 502 Bad Gateway")),
        "n:4": _node("formatter", ["n:3"]),
    }
    resolved = resolve_inputs(nodes["n:4"]["inputs"], nodes, "q")
    by_id = {r.get("id"): r for r in resolved}
    assert by_id["n:3"]["kind"] == "upstream-missing"
    assert "502" in by_id["n:3"]["error"]
    assert by_id["n:2"]["kind"] == "upstream"
    assert by_id["n:2"]["output"]["findings"].startswith("DuckDB")


def test_success_with_empty_output_is_treated_as_missing():
    nodes = {
        "n:2": _node("researcher", ["USER_QUERY"], AgentResult(
            success=True, agent_name="researcher", output={})),
        "n:3": _node("formatter", ["n:2"]),
    }
    resolved = resolve_inputs(["n:2"], nodes, "q")
    assert resolved[0]["kind"] == "upstream-missing"


def test_missing_input_still_reports_the_gap_when_nothing_was_recoverable():
    nodes = {
        "n:2": _node("researcher", ["USER_QUERY"]),
        "n:3": _node("formatter", ["n:2"]),
    }
    resolved = resolve_inputs(["n:2"], nodes, "q")
    assert [r["kind"] for r in resolved] == ["upstream-missing"]
    assert resolved[0]["note"]


def test_recovery_does_not_duplicate_user_query_or_loop():
    nodes = {
        "n:1": _node("planner", ["USER_QUERY"]),
        "n:2": _node("researcher", ["USER_QUERY"], AgentResult(
            success=True, agent_name="researcher", output={"findings": "a"})),
        # A malformed graph that points back at itself must terminate.
        "n:3": _node("author", ["n:2", "n:3", "USER_QUERY"]),
        "n:4": _node("formatter", ["n:3"]),
    }
    resolved = resolve_inputs(["n:4"], nodes, "q")
    assert [r.get("id") for r in resolved if r.get("kind") == "upstream"] == ["n:2"]
    assert not any(r.get("kind") == "query" for r in resolved)
