"""The action skill uses progressive tool disclosure via channel bundles.

The node gets the core tools + `load_tool_bundle`, and pulls a channel's schemas
mid-run by calling `load_tool_bundle("github")`. This file asserts the base
payload does NOT contain channel tools, the bundles are complete, and the prompt
header names exactly what is offered.
"""
from __future__ import annotations

from pathlib import Path

import yaml

import skills

CODE = Path(__file__).resolve().parents[1]


def _action() -> skills.Skill:
    cfg = yaml.safe_load((CODE / "agent_config.yaml").read_text(encoding="utf-8"))
    return skills.Skill("action", cfg["action"])


def test_base_payload_is_core_plus_bundle_loader():
    sk = _action()
    got = skills.effective_tools(sk, "post the summary to #general on Slack", {})
    assert "load_tool_bundle" in got
    # Channel tools are NOT in the base payload — they are loaded on demand.
    assert "slack_message" not in got
    assert "github_get_user" not in got
    assert "telegram_get_me" not in got
    # Core tools survive.
    assert "schedule_task" in got and "search_knowledge" in got


def test_every_bundle_has_schemas():
    bundles = skills.build_tool_bundles()
    assert set(bundles) == set(skills.CHANNEL_TOOLS)
    for name, schemas in bundles.items():
        assert schemas, f"bundle {name} has no schemas"
        for s in schemas:
            assert s.get("name"), f"bundle {name} has an unnamed schema"


def test_load_tool_bundle_is_in_grant_and_catalog():
    sk = _action()
    assert "load_tool_bundle" in sk.tools_allowed
    assert "load_tool_bundle" in skills._TOOL_CATALOG


def test_prompt_header_names_the_base_payload():
    sk = _action()
    granted = skills.effective_tools(sk, "file a GitHub issue", {})
    rendered = skills.render_prompt(sk, "file a GitHub issue", [], tools=granted)
    assert "{{ACTION_TOOLS}}" not in rendered
    assert "GRANTED THIS RUN" in rendered
    assert "load_tool_bundle" in rendered


def test_every_channel_tool_exists_in_the_catalogue():
    import asyncio
    import mcp_server
    tools = asyncio.run(mcp_server.mcp.list_tools())
    catalog = {t.name for t in tools}
    for channel, names in skills.CHANNEL_TOOLS.items():
        for n in names:
            assert n in catalog, f"{channel} lists {n}, which is not a real tool"


def test_every_channel_tool_is_in_the_action_grant():
    sk = _action()
    for names in skills.CHANNEL_TOOLS.values():
        for n in names:
            assert n in sk.tools_allowed, f"{n} is not granted to action"


def test_dynamic_channels_is_on_for_action():
    assert _action().dynamic_channels is True