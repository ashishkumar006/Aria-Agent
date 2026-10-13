"""Chat's tool surface: one list, prompt and payload in agreement.

Chat used to have THREE independent descriptions of its own capability: a
literal list in each of two endpoints, and a third, shorter list spelled out in
the system prompt. They disagreed, so the model answered "I have 11 tools"
from the prompt while the payload carried a different set - and it could list
reminders but had no tool to create one, so "remind me at six" could not work
no matter how the user phrased it.
"""
import agent_server
from skills import tool_payload


def test_the_whole_chat_toolset_actually_resolves():
    """Every name chat advertises has to be a real tool.

    A name that does not resolve is silently dropped by tool_payload, so a
    typo would quietly remove a capability and nothing would fail.
    """
    got = {t["name"] for t in (tool_payload(agent_server._CHAT_TOOLS) or [])}
    missing = set(agent_server._CHAT_TOOLS) - got
    assert not missing, f"named but not a real tool: {sorted(missing)}"


def test_no_refused_tool_is_reachable_from_chat():
    """Sending mail, creating events and touching the machine stay on the
    Research path: they act on other people and are not undoable from chat."""
    got = {t["name"] for t in (tool_payload(agent_server._CHAT_TOOLS) or [])}
    leaked = got & set(agent_server._CHAT_TOOLS_REFUSED)
    assert not leaked, f"refused tools are reachable: {sorted(leaked)}"


def test_the_system_prompt_names_exactly_the_tools_chat_has():
    """The prompt is how the model answers "what can you do".

    A tool in the payload but absent from the prompt is invisible to the
    user; a tool in the prompt but absent from the payload is a promise the
    model cannot keep, which is worse.
    """
    prompt = agent_server._CHAT_SYSTEM
    unnamed = [n for n in agent_server._CHAT_TOOLS if n not in prompt]
    assert not unnamed, (
        f"chat can call these but will not say so: {unnamed}")
    invented = [n for n in agent_server._CHAT_TOOLS_REFUSED if n in prompt]
    assert not invented, (
        f"prompt promises tools chat does not have: {invented}")


def test_chat_cannot_reach_a_write_through_a_multi_call_tool():
    """A refused capability reachable through a general wrapper is not refused.

    `_CHAT_TOOLS_REFUSED` listed `github_create_issue` and `notion_create_page`
    to keep chat from writing. Neither name is a real MCP tool — the write
    lives inside `github_query` / `notion_query`, whose `api_method` enum
    contains `create_issue` / `create_page` / `append_text`. Chat was handed
    that enum verbatim, so the model could open issues and edit Notion pages,
    and the name-based guard above passed because it cannot see through a
    wrapper. The test it should have had asserts on the SCHEMA, not the names.
    """
    specs = agent_server._chat_tool_payload() or []
    by_name = {s.get("name"): s for s in specs}

    for tool, denied in agent_server._CHAT_WRITE_METHODS.items():
        spec = by_name.get(tool)
        assert spec is not None, f"{tool} not offered to chat at all"
        enum = (((spec.get("input_schema") or {}).get("properties") or {})
                .get("api_method", {}).get("enum") or [])
        leaked = denied & set(enum)
        assert not leaked, (
            f"chat can reach {sorted(leaked)} through {tool}(api_method=...) "
            f"even though it is listed as refused")

    # And the read methods must survive - this is not "remove the tool".
    for tool, _ in agent_server._CHAT_WRITE_METHODS.items():
        spec = by_name.get(tool)
        enum = (((spec.get("input_schema") or {}).get("properties") or {})
                .get("api_method", {}).get("enum") or [])
        assert enum, f"{tool} was left with no methods at all"


def test_the_research_path_keeps_the_write_methods():
    """The refusal is chat-only. Research is the confirmation-gated path and
    must still be able to file an issue."""
    import yaml
    from skills import tool_payload as _tp

    cfg = yaml.safe_load(
        open(agent_server.__file__.replace("agent_server.py", "agent_config.yaml"),
             encoding="utf-8")) or {}
    skills_cfg = cfg.get("skills") or cfg
    allowed: list[str] = []
    for entry in (skills_cfg.values() if isinstance(skills_cfg, dict) else []):
        if isinstance(entry, dict) and isinstance(entry.get("tools_allowed"), list):
            allowed.extend(str(t) for t in entry["tools_allowed"])
    specs = {s.get("name"): s for s in (_tp(sorted(set(allowed))) or [])}
    gh = specs.get("github_query")
    if gh is None:
        return  # no skill grants it; nothing to preserve
    enum = (((gh.get("input_schema") or {}).get("properties") or {})
            .get("api_method", {}).get("enum") or [])
    assert "create_issue" in enum, (
        "the chat guard leaked into the research path and took away its "
        "ability to file issues")


def test_chat_can_actually_schedule():
    """The complaint that started this: chat could list reminders but not
    create them, so every attempt to schedule from chat failed."""
    assert "schedule_task" in agent_server._CHAT_TOOLS
    assert "cancel_scheduled" in agent_server._CHAT_TOOLS
    assert "list_scheduled" in agent_server._CHAT_TOOLS
    assert "remind" in agent_server._CHAT_SYSTEM.lower(), (
        "the prompt must tell the model how to handle a reminder request")


def test_chat_can_write_files_and_render_documents():
    """Asking chat for a document used to return pasted markdown. Reading
    before editing matters: edit_file on a path the model invented destroys
    whatever was there."""
    for name in ("read_file", "create_file", "update_file", "edit_file",
                 "render_document"):
        assert name in agent_server._CHAT_TOOLS, name
    assert "delete_file" not in agent_server._CHAT_TOOLS, (
        "chat must not delete; there is no undo and no confirmation")


def test_every_chat_entry_point_uses_the_same_list():
    """The one-shot and streaming twins once held separate literals, so the two
    gave the model different capabilities."""
    src = open(agent_server.__file__, encoding="utf-8").read()
    # Two endpoints build a payload, one lists capabilities, one documents the
    # refusals. Every one of them must read the shared name, and the literals
    # may only appear inside the definition itself.
    assert src.count("_CHAT_TOOLS") >= 4, (
        "a chat endpoint is still hardcoding its own tool list")
    assert src.count('"gmail_query", "github_query", "slack_history"') == 1, (
        "the chat tool list is spelled out somewhere other than the definition")
