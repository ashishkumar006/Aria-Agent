"""Chat's tool surface: one list, prompt and payload in agreement.

Chat used to have THREE independent descriptions of its own capability: a
literal list in each of two endpoints, and a third, shorter list spelled out in
the system prompt. They disagreed, so the model answered "I have 11 tools"
from the prompt while the payload carried a different set - and it could list
reminders but had no tool to create one, so "remind me at six" could not work
no matter how the user phrased it.
"""
import asyncio
import json

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
    """Sending mail, creating events and deleting files stay off chat's
    surface: they act on other people or have no undo. Desktop control is
    NOT in this set - it is offered as the gated `computer_action` instead,
    where the approval queue decides whether anything runs."""
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


def test_chat_reaches_the_machine_only_through_the_gated_tool():
    """Desktop access is offered to chat, but as ONE approval-gated tool.

    The model has to be told what `pending` means or it will report a click
    that never ran, and it has to be told the host gate exists or it will
    promise to open Notepad on a build where computer use is off.
    """
    names = {t["name"] for t in agent_server._chat_tool_payload() or []}
    assert "computer_action" in names
    assert "computer_action" not in agent_server._CHAT_TOOLS_REFUSED
    assert "computer_action" in agent_server._CHAT_SYSTEM
    assert "pending" in agent_server._CHAT_SYSTEM.lower()
    assert "dry-run" in agent_server._CHAT_SYSTEM.lower()


def test_chat_is_offered_goal_level_computer_actions_only():
    """A small chat model handed the daemon primitives improvises against the
    focused window: observed live as press_key LWin, a URL typed into a page and
    an unrelated click instead of one drive_app call. Low-level control stays
    on the Computer page and in skills; chat gets goals."""
    specs = {t["name"]: t for t in agent_server._chat_tool_payload() or []}
    spec = specs["computer_action"]
    enum = (((spec.get("input_schema") or {}).get("properties") or {})
            .get("action", {}).get("enum") or [])
    assert set(enum) == set(agent_server._CHAT_COMPUTER_ACTIONS)
    assert "drive_app" in enum
    for low in ("click", "press_key", "replace_text", "get_window_state"):
        assert low not in enum


def test_chat_drive_app_without_a_named_app_is_refused(monkeypatch):
    """The driver targets the foreground window when app is missing.

    Live, a chat "play a song" call without params.app clicked a Chrome page
    and still reported that Spotify was playing. Refuse before dispatch so no
    unrelated window can be touched.
    """
    import computer_use.safety as safety
    monkeypatch.setenv("COMPUTER_USE_ENABLED", "true")
    monkeypatch.setenv("COMPUTER_USE_MODE", "live")
    safety.reset_shared_gates()

    dispatch = agent_server._chat_handoff_dispatch("cu-test")
    raw = asyncio.run(dispatch("computer_action", {
        "action": "drive_app", "params": {"goal": "play a song"}}))
    out = json.loads(raw)

    assert out["status"] == "bad_request"
    assert "params.app" in out["message"]
    assert safety.shared_gates().list_approvals() == []


def test_chat_computer_action_runs_in_the_agent_process(monkeypatch):
    """Chat must not dispatch computer_action over MCP: the child's approval
    store is not the one `/api/computer/approvals` reads, so a pending action
    there never appears in the Computer page."""
    import computer_use.safety as safety
    monkeypatch.setenv("COMPUTER_USE_ENABLED", "true")
    monkeypatch.setenv("COMPUTER_USE_MODE", "dry-run")
    safety.reset_shared_gates()

    dispatch = agent_server._chat_handoff_dispatch("cu-test")
    raw = asyncio.run(dispatch("computer_action", {
        "action": "open_app", "params": {"app": "Spotify"}}))
    out = json.loads(raw)

    assert out["status"] == "dry-run"
    assert safety.shared_gates().list_approvals() == []


def test_chat_pending_approval_is_listed_by_the_computer_page(monkeypatch):
    import computer_use.safety as safety
    monkeypatch.setenv("COMPUTER_USE_ENABLED", "true")
    monkeypatch.setenv("COMPUTER_USE_MODE", "live")
    safety.reset_shared_gates()

    dispatch = agent_server._chat_handoff_dispatch("cu-test")
    raw = asyncio.run(dispatch("computer_action", {
        "action": "run_command",
        "params": {"command": "taskkill /F /PID 1234"}}))
    out = json.loads(raw)

    assert out["status"] == "pending"
    assert out["approval_id"] in {
        a["id"] for a in safety.shared_gates().list_approvals()}


def test_the_researcher_gets_the_same_gated_machine_access():
    """`computer` is a DAG skill; the researcher is a tool-calling node, so
    the capability has to be granted in `tools_allowed` and explained in its
    prompt or the model can never call it. The planner already carries the
    skill itself."""
    import yaml

    cfg = yaml.safe_load(
        open(agent_server.__file__.replace("agent_server.py", "agent_config.yaml"),
             encoding="utf-8")) or {}
    skills_cfg = cfg.get("skills") or cfg
    researcher = skills_cfg.get("researcher") or {}
    assert "computer_action" in (researcher.get("tools_allowed") or [])

    prompt_path = agent_server.__file__.replace(
        "agent_server.py", "prompts/researcher.md")
    prompt = open(prompt_path, encoding="utf-8").read()
    assert "computer_action" in prompt
    assert "pending" in prompt.lower(), (
        "the researcher must be told that pending means it did not run")

    planner = open(agent_server.__file__.replace(
        "agent_server.py", "prompts/planner.md"), encoding="utf-8").read()
    assert "computer" in planner


def test_chat_hands_documents_and_files_to_the_authoring_agent():
    """Asking chat for a document used to return pasted markdown, and the fix
    was to give chat render_document - so chat started doing the authoring
    agent's job, one chat-side tool call at a time. That is what made chat
    heavy: a request for a document could burn the hop cap and answer
    "(empty answer)". One delegate_authoring call now does the whole job and
    returns a real downloadable file, so the specialist tools must be GONE
    from chat's surface - that is the property this asserts."""
    for gone in ("render_document", "save_as_document", "read_artifact",
                 "create_file", "update_file", "edit_file"):
        assert gone not in agent_server._CHAT_TOOLS, (
            f"chat still carries {gone}; it should hand off instead")
    assert "delete_file" not in agent_server._CHAT_TOOLS, (
        "chat must not delete; there is no undo and no confirmation")
    # read-only file access stays: peeking at the workspace is cheap.
    for keep in ("read_file", "list_dir", "search_files"):
        assert keep in agent_server._CHAT_TOOLS, keep


def test_the_handoff_tools_reach_the_chat_payload():
    """A tool that is in the list but not in the schema the model actually
    receives is a tool it can never call."""
    names = [t["name"] for t in agent_server._chat_tool_payload() or []]
    assert "delegate_research" in names
    assert "delegate_authoring" in names


def test_chat_keeps_the_conversational_tools():
    """Handing off the big jobs must not take the conversational ones too: the
    user's own accounts, reminders and memory are what chat is FOR."""
    names = {t["name"] for t in agent_server._chat_tool_payload() or []}
    for keep in ("calendar_query", "gmail_query", "slack_history",
                 "notion_query", "github_query", "schedule_task",
                 "cancel_scheduled", "remember_preference", "web_search"):
        assert keep in names, f"chat lost {keep}"


def test_the_handoff_is_described_in_the_prompt():
    """A tool the model has never heard of is a tool it will not call."""
    assert "delegate_authoring" in agent_server._CHAT_SYSTEM
    assert "delegate_research" in agent_server._CHAT_SYSTEM


class _FakeStream:
    """The minimum `_run_handoff` consumes: a body_iterator of SSE bytes."""

    def __init__(self, frames):
        self._frames = frames

    @property
    def body_iterator(self):
        async def _it():
            for f in self._frames:
                yield ("data: " + json.dumps(f) + "\n\n").encode("utf-8")
        return _it()


_FILE_FRAME = {"type": "done", "answer": "The report body.",
               "files": [{"artifact": "art:" + "a" * 16,
                          "filename": "report.pdf", "format": "pdf",
                          "skill": "author"}]}


def test_a_delegated_run_reports_its_files_and_forbids_invented_links(monkeypatch):
    """Observed live: a delegated authoring run finished, and chat told the
    user the file was at `sandbox:/mnt/data/report.pdf` - a path that does not
    exist here, and a dead link in the console. The tool result now names the
    file and says not to write any URL, and the `done` frame carries the real
    artifact so the transcript can offer the download."""
    import asyncio

    monkeypatch.setattr(agent_server, "_stream_run",
                        lambda *a, **k: _FakeStream([_FILE_FRAME]))
    got = asyncio.run(agent_server._run_handoff("a topic", "cid", "key"))
    assert got["ok"] is True and got["status"] == "done"
    assert got["files"][0]["filename"] == "report.pdf"
    assert "report.pdf" in got["say"]
    assert "Do NOT write any URL" in got["say"]


def test_the_dispatcher_collects_handoff_files_for_the_done_frame(monkeypatch):
    """Chat's `done` frame used to scrape `art:` handles out of the answer,
    which only works if the model volunteers the handle. The delegated file has
    to reach the frame regardless."""
    import asyncio

    monkeypatch.setattr(agent_server, "_stream_run",
                        lambda *a, **k: _FakeStream([_FILE_FRAME]))
    sink: list = []
    d = agent_server._chat_handoff_dispatch("cid", sink)
    out = asyncio.run(d("delegate_research", {"topic": "x"}))
    assert json.loads(out)["ok"] is True
    assert sink and sink[0]["filename"] == "report.pdf"


def test_chat_is_told_tools_run_in_parallel():
    """It kept answering "I can only call one tool at a time" - a falsehood it
    invented because the prompt never said otherwise, while the runner had
    always dispatched a batch with asyncio.gather."""
    assert "PARALLEL" in agent_server._CHAT_SYSTEM.upper()


def test_the_handoff_dispatcher_ignores_every_other_tool():
    """It must return None for anything that is not its own, so every other
    tool still goes down the unchanged MCP path."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        d = agent_server._chat_handoff_dispatch("ct-test")
        for name in ("web_search", "gmail_query", "schedule_task", "read_file"):
            assert loop.run_until_complete(d(name, {})) is None, (
                f"{name} must not be intercepted by the hand-off")
    finally:
        loop.close()


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
