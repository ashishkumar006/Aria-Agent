"""The prompts must describe the agent that exists.

Why this file exists
--------------------
The user's complaint was that the agent does not know what it can do, and the
cause was exactly here: `prompts/action.md` documented
`schedule_task(query, when, conversation_id?)` while the tool actually accepts
eleven parameters including `message` and `channel`. The model therefore
guessed a `message` with no idea that a message *replaces* the work at fire
time, and a reminder was delivered that had checked nothing. A prompt that
describes half an interface is not documentation, it is a lie the model acts
on — and it is the kind of drift nothing catches, because the tool works and
the prompt still reads well.

So: every tool a prompt names must exist, and every parameter a prompt names
for that tool must be a parameter the tool accepts. The reverse direction is
checked too — a tool the action skill can reach that the prompt never mentions
is a capability the model will not know to use.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import yaml

import pytest

CODE = Path(__file__).resolve().parents[1]
PROMPTS = CODE / "prompts"

# `- tool_name(a, b, c?)` or "`tool_name(a, b)`" — the shape the action prompt
# uses to introduce a tool. Only names that resolve to a real tool are kept, so
# prose that happens to contain parentheses does not fail the suite.
SIG = re.compile(r"\b([a-z][a-z0-9_]{2,})\(([^)]*)\)")

# Names in prompts that look like signatures but are not tools.
NOT_A_TOOL = {
    "label", "position", "name", "text", "string", "object", "array", "number",
    "boolean", "null", "json", "utf", "http", "https", "e.g", "i.e", "etc",
    "run", "read", "write", "send", "list", "get", "set", "node", "skill",
    "inputs", "output", "result", "answer", "query", "prompt", "model",
    "format", "depth", "goal", "facets", "topic", "sources", "limit",
}


@pytest.fixture(scope="module")
def registry():
    import mcp_server
    tools = asyncio.run(mcp_server.mcp.list_tools())
    return {t.name: set(((t.inputSchema or {}).get("properties") or {}).keys())
            for t in tools}


def _action_prompt_tools():
    """Yield (tool, [params named for it]) from the action skill's prompt."""
    text = (PROMPTS / "action.md").read_text(encoding="utf-8")
    for m in SIG.finditer(text):
        name, arglist = m.group(1), m.group(2)
        if name in NOT_A_TOOL:
            continue
        params = [p.strip().split(":")[0].strip().rstrip("?*")
                  for p in arglist.split(",")]
        params = [p for p in params if p and re.fullmatch(r"[a-z][a-z0-9_]*", p)]
        yield name, params


def test_every_tool_the_action_prompt_names_exists(registry):
    reg = registry
    missing = sorted({n for n, _ in _action_prompt_tools() if n not in reg})
    assert not missing, (
        f"prompts/action.md tells the model it has tools that do not exist: "
        f"{missing}. It will call them, get an error, and explain the error to "
        f"the user instead of doing the work.")


def test_every_parameter_the_action_prompt_names_is_real(registry):
    """The exact defect that produced the silent reminder."""
    reg = registry
    bad = []
    for name, params in _action_prompt_tools():
        if name not in reg or not params:
            continue
        unknown = [p for p in params if p not in reg[name]]
        if unknown:
            bad.append(f"{name}({', '.join(unknown)})")
    assert not bad, f"the prompt documents parameters that do not exist: {bad}"


def test_the_action_prompt_covers_the_scheduling_surface_it_needs(registry):
    """`message` and `channel` are the two parameters whose absence caused the
    bug: the model set a message without being told the message is what gets
    delivered, and never learned the query is what gets run."""
    reg = registry
    # Accumulate with a union. `set(p)` over the inner loop would split the
    # FIRST PARAMETER into characters and every assertion would then compare
    # against a set of letters — which is how this test briefly "proved" that
    # `query` was undocumented.
    named: dict = {}
    for name, params in _action_prompt_tools():
        named.setdefault(name, set()).update(params)
    assert "schedule_task" in named, "the action prompt no longer documents schedule_task"
    for param in ("query", "when", "message", "channel", "conversation_id"):
        assert param in named["schedule_task"], (
            f"schedule_task's `{param}` is undocumented — a model that guesses "
            f"it cannot know what it means")
        assert param in reg["schedule_task"], f"{param} is not a real parameter"


def test_every_tool_the_action_prompt_names_is_granted_to_the_action_skill():
    """The direction the test above does NOT check, and the one I fell into.

    `test_no_reachable_tool_is_invisible_to_the_action_skill` asserts a tool is
    MENTIONED, and its docstring says "a tool the skill can call" — but nothing
    verified the skill can call it. `tools_allowed` in agent_config.yaml is the
    grant: tool_payload() intersects it, so a tool absent from that list is not
    offered to the model at all. I documented `whats_new` and `schedule_wake` in
    action.md while neither was granted, which is a false capability claim of
    exactly the kind this file exists to catch — the prompt says you have hands,
    the schema list says you do not.
    """
    cfg = yaml.safe_load((CODE / "agent_config.yaml").read_text(encoding="utf-8"))
    granted = set((cfg.get("action") or {}).get("tools_allowed") or [])
    assert granted, "action has no tools_allowed at all — parse broke, not a finding"
    named = {n for n, _ in _action_prompt_tools()}
    missing = sorted(n for n in named if n not in granted)
    assert not missing, (
        f"prompts/action.md documents tools the action skill is never given: "
        f"{missing}. The model is told it can and cannot call them.")


def test_no_reachable_tool_is_invisible_to_the_action_skill():
    """A tool the skill can call but the prompt never mentions is a capability
    the model will not think to use. Scoped deliberately: this checks the
    side-effect and delivery tools, not the whole catalogue, which the action
    skill is not meant to know. Both directions now hold: this list is also
    cross-checked against `tools_allowed` by the test above, so a name here
    cannot quietly become prose about a tool that was never granted."""
    text = (PROMPTS / "action.md").read_text(encoding="utf-8")
    expected = ["schedule_task", "list_scheduled", "cancel_scheduled",
                "send_telegram", "gmail_query", "calendar_query",
                "github_query", "notion_query", "whats_new", "schedule_wake"]
    absent = [n for n in expected if not re.search(rf"\b{n}\b", text)]
    assert not absent, f"tools the action skill can reach but is never told about: {absent}"


def test_the_planner_describes_depth_the_way_the_code_handles_it():
    """`depth` is recorded and displayed; it does not steer a worker.

    Measured: `NodeSpec.merged()` folds the node's `depth` into metadata, and no
    runtime module ever reads `metadata["depth"]`. The only depth that changes
    behaviour is the one the CALLER supplies (the Research page, `delegate_research`),
    which becomes a planner-only run note. The prompt used to say "It is read by
    the executor, so what you emit here is what actually executes" — which let a
    planner believe it had made the fleet exhaustive by writing the word `deep`,
    and it is the kind of claim that quietly makes a prompt wrong forever, because
    nothing ever contradicts it.

    If you are here because this test went red, someone WIRED per-node depth.
    Good — then delete the caveat in `prompts/planner.md` instead of this test.
    """
    src = [(CODE / name).read_text(encoding="utf-8")
           for name in ("flow.py", "skills.py", "mcp_runner.py")]
    readers = [s for s in src if re.search(r"""(metadata|meta|node_meta)\s*(\[\s*["']depth["']\s*\]|\.get\(\s*["']depth["'])""", s)]
    assert not readers, (
        "per-node depth is now read at runtime — the planner prompt's account of "
        "what a worker receives is out of date")
    text = (PROMPTS / "planner.md").read_text(encoding="utf-8")
    assert "no worker reads it" in text, (
        "planner.md must say plainly that depth is a label, not a lever")


def test_the_planner_promises_only_tools_the_action_node_has(registry):
    """The planner now makes capability claims about another skill.

    `prompts/planner.md` gained a "What `action` can reach" section that names
    tools, because a planner that does not know the action surface cannot write
    a goal that uses it. That makes the planner a second place a false
    capability can be asserted — and it is the more damaging place, since the
    claim is what generates the node. Every tool-shaped name it attributes to
    `action` must be registered AND in `action.tools_allowed`.
    """
    cfg = yaml.safe_load((CODE / "agent_config.yaml").read_text(encoding="utf-8"))
    granted = set((cfg.get("action") or {}).get("tools_allowed") or [])
    text = (PROMPTS / "planner.md").read_text(encoding="utf-8")
    section = text.split("### What `action` can reach", 1)
    assert len(section) == 2, (
        "the planner no longer documents the action surface — if this was "
        "deliberate, delete this test as well as the section")
    tail = section[1].split("\n### ", 1)[0]
    # Both shapes: `tool_name` backticked, and tool_name(args) as a signature.
    named = set(re.findall(r"`([a-z][a-z0-9_]{2,})`", tail))
    named |= {m.group(1) for m in SIG.finditer(tail)}
    bad = []
    # A backticked name that is a registered SKILL is not a tool claim, so the
    # prose that routes work away from `action` ("that is `coder`, not this")
    # must not be read as a tool. Derived from the config rather than a
    # hand-kept list, which goes stale the first time a skill is added.
    skills_named = set(cfg)
    # Words that are neither, and appear in backticks as prose.
    not_tools = {"schedule", "gmail", "telegram", "notion", "slack", "github",
                 "check_weather_and_notify_if_temp_exceeds_15_c_in_vijayawada"}
    for name in sorted(named):
        if name in NOT_A_TOOL or name in skills_named or name in not_tools:
            continue
        if name not in registry:
            bad.append(f"{name} (no such tool at all)")
        elif name not in granted:
            bad.append(f"{name} (exists, but the action node is not given it)")
    assert not bad, f"the planner attributes unreachable tools to `action`: {bad}"


def test_all_four_sources_agree_that_a_message_cancels_the_work():
    """The nudge contract is stated in four places, and they must not diverge.

    `scheduler._is_nudge_only`, `mcp_server.schedule_task`'s docstring,
    `prompts/action.md`, `prompts/planner.md` and
    `agent_server._platform_capabilities()` all describe what happens when a
    scheduled job carries both a query and a message. Three of them said, or
    implied, that the query still runs — and `_platform_capabilities` said it
    outright ("the query still runs"), which is how it reached a user: asked on
    the live server what a job with both does, the agent answered "the query
    executes as an agent turn, delivered alongside the message". Confidently
    wrong, about its own scheduler, because that is the sentence it was given.

    Checked as a prohibition rather than a required phrasing, because the five
    texts are written for different readers and cannot share a sentence.
    """
    banned = ("query still runs", "message is what gets said, not a substitute")
    import agent_server
    sources = {
        "prompts/action.md": (PROMPTS / "action.md").read_text(encoding="utf-8"),
        "prompts/planner.md": (PROMPTS / "planner.md").read_text(encoding="utf-8"),
        "mcp_server.schedule_task": __import__("mcp_server").schedule_task.__doc__ or "",
        # Called, not grepped. Scanning agent_server.py's source also reads the
        # comments, and the comment above this sentence QUOTES the old wrong
        # wording to explain why it changed — which is exactly what a whole-file
        # scan turns into a false failure. What the model sees is the return
        # value, so that is what must be clean.
        "_platform_capabilities": agent_server._platform_capabilities(),
    }
    offenders = {name: [b for b in banned if b in text]
                 for name, text in sources.items()}
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, (
        f"these describe a message-bearing job as still running its query: "
        f"{offenders}. It does not — `_is_nudge_only` sends the words and runs "
        f"nothing, and that IS the documented contract.")


def test_the_nudge_contract_matches_the_code_that_decides_it():
    """The prose is only right if the discriminator is what the prose says."""
    import scheduler
    # A message is the whole decision, even over an instruction-shaped query.
    assert scheduler._is_nudge_only(
        {"query": "whats_new surfaces=documents",
         "notify_policy": {"message": "Checking..."}}) is True
    assert scheduler._is_nudge_only(
        {"query": "check the weather", "notify_policy": {}}) is False


def test_every_skill_in_the_planner_table_is_registered_and_has_a_prompt():
    """The planner is the only place the model learns what shapes it can build.
    A skill in its table that the registry rejects is a KeyError mid-run; a
    registered skill it never mentions is work it will not plan for."""
    import yaml
    cfg = yaml.safe_load((CODE / "agent_config.yaml").read_text(encoding="utf-8"))
    table = set(re.findall(r"^\| `([a-z_]+)`",
                           (PROMPTS / "planner.md").read_text(encoding="utf-8"),
                           re.M))
    assert table, "the planner's skill table could not be parsed — the scan broke"
    unknown = sorted(table - set(cfg))
    assert not unknown, f"the planner offers skills that are not registered: {unknown}"
    for name in sorted(table):
        path = (cfg.get(name) or {}).get("prompt")
        assert path, f"skill {name} is advertised but has no prompt path configured"
        assert (CODE / path).is_file(), (
            f"skill {name} points at {path}, which does not exist. Its "
            f"prompt_template() silently returns 'You are the {name} skill. "
            f"(Prompt file missing.)' and the node runs on that.")


def test_the_planner_table_is_not_missing_registered_skills():
    cfg = yaml.safe_load((CODE / "agent_config.yaml").read_text(encoding="utf-8"))
    table = set(re.findall(r"^\| `([a-z_]+)`",
                           (PROMPTS / "planner.md").read_text(encoding="utf-8"),
                           re.M))
    # Internal skills the planner must never emit, and so is right to omit.
    internal = {"planner", "app_builder", "vision_file"}
    hidden = sorted(set(cfg) - table - internal)
    assert not hidden, (
        f"registered skills the planner is not told it can use: {hidden}")
