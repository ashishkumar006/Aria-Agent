"""Studio-coder DELEGATION is wired end-to-end (planner -> skill -> tool).

The DAG can hand a whole coding task to the Studio coding agent (the full code
agent that edits the session's REAL project roots, not the throwaway sandbox).
That path has four links and each can rot independently:

  1. the skill is registered in agent_config.yaml with `studio_coder` granted;
  2. its prompt file exists and tells the model to make exactly one call;
  3. `studio_coder` is a real MCP tool (not a name that resolves to nothing);
  4. the planner is actually told the skill exists (prompts/planner.md), or it
     can never emit the node.

A break at any link means "change my actual codebase" silently falls back to
the sandbox `coder` or to an apology. These tests pin all four without a live
agent, a Studio session, or a network.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

import skills

CODE = Path(__file__).resolve().parents[1]


def _studio() -> skills.Skill:
    cfg = yaml.safe_load((CODE / "agent_config.yaml").read_text(encoding="utf-8"))
    return skills.Skill("studio_coder", cfg["studio_coder"])


# ── link 1: the skill is registered and granted the tool ─────────────────────

def test_skill_is_registered_with_the_tool_granted():
    sk = _studio()
    assert sk.name == "studio_coder"
    assert "studio_coder" in sk.tools_allowed, sk.tools_allowed


def test_effective_tools_include_studio_coder():
    sk = _studio()
    got = skills.effective_tools(sk, "add a --version flag to the CLI", {})
    assert "studio_coder" in got


# ── link 2: the prompt exists and says "one call, then report" ───────────────

def test_prompt_file_exists_and_is_nonempty():
    sk = _studio()
    assert sk.prompt_path.exists(), sk.prompt_path
    text = sk.prompt_path.read_text(encoding="utf-8").strip()
    assert text, "studio_coder delegate prompt is empty"
    # It must name the tool it calls, or the model has nothing to call.
    assert "studio_coder(" in text


def test_prompt_forbids_inventing_a_receipt():
    """The delegation's whole safety property: report the receipt, never fake
    it. If a future edit drops this, the model can claim a change that never
    happened."""
    text = _studio().prompt_path.read_text(encoding="utf-8").lower()
    assert "never invent" in text or "do not claim" in text


def test_rendered_prompt_names_the_tool_and_grants_only_it():
    """studio_coder's prompt has no `{{ACTION_TOOLS}}` marker (it names the one
    call inline), so the contract is: the grant is exactly that one tool and the
    rendered prompt names it. A regression that widened the grant would let the
    delegate call unrelated tools; one that dropped it would leave the model with
    nothing callable."""
    sk = _studio()
    granted = skills.effective_tools(sk, "refactor the parser", {})
    assert set(granted) == {"studio_coder"}, granted
    rendered = skills.render_prompt(sk, "refactor the parser", [], tools=granted)
    assert "studio_coder(" in rendered


# ── link 3: studio_coder is a real MCP tool ──────────────────────────────────

def test_studio_coder_is_a_real_mcp_tool():
    import mcp_server
    tools = asyncio.run(mcp_server.mcp.list_tools())
    catalog = {t.name for t in tools}
    assert "studio_coder" in catalog, "studio_coder skill calls a tool that does not exist"


def test_studio_coder_tool_schema_requires_a_task():
    import mcp_server
    tools = asyncio.run(mcp_server.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    schema = by_name["studio_coder"].inputSchema
    assert "task" in (schema.get("properties") or {})
    assert "task" in (schema.get("required") or [])


# ── link 4: the planner is told the skill exists ─────────────────────────────

def test_planner_prompt_lists_studio_coder():
    text = (CODE / "prompts" / "planner.md").read_text(encoding="utf-8")
    assert "studio_coder" in text, "planner cannot emit a skill it is not told about"


# ── the skill is LLM-backed, not a bypass ────────────────────────────────────

def test_studio_coder_is_not_a_dispatch_bypass():
    """sandbox_executor and browser bypass the LLM dispatch. studio_coder must
    NOT — it is an ordinary tool-loop skill, so it belongs on the gateway path
    with the rest. Guard against someone 'optimising' it into a bypass and
    losing the tool grant."""
    import inspect
    src = inspect.getsource(skills.run_skill)
    # The two real bypasses are guarded by these exact comparisons.
    assert 'skill.name == "sandbox_executor"' in src
    assert 'skill.name == "browser"' in src
    # studio_coder has no dedicated branch: it is handled by the generic path.
    assert 'skill.name == "studio_coder"' not in src