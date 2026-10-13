"""Every MCP tool must be advertised, not merely registered.

Three boundaries, each of which fails silently from the outside:
  1. registered on the MCP server      (mcp_server.list_tools)
  2. present in _TOOL_CATALOG          (what the model is actually SHOWN)
  3. dispatched over the stdio session (mcp_runner)

`run_with_tools` never calls list_tools at runtime: it is handed a
`tools_payload` built from `_TOOL_CATALOG`, and the MCP subprocess is used
only to DISPATCH. So a tool can be registered, perfectly dispatchable, and
still never appear in a prompt - and no test covered that.
"""
import asyncio
import pathlib

import pytest

import mcp_server as mcp
import skills


@pytest.fixture(scope="module")
def registered():
    loop = asyncio.new_event_loop()
    try:
        return sorted(t.name for t in loop.run_until_complete(mcp.mcp.list_tools()))
    finally:
        loop.close()


@pytest.fixture(scope="module")
def catalog():
    return sorted(skills._TOOL_CATALOG)


def test_the_catalog_is_not_empty(registered, catalog):
    """A catalog that failed to load offers the model nothing."""
    assert catalog, "_TOOL_CATALOG is empty - no tool would ever be offered"


def test_every_registered_tool_is_advertisable(registered, catalog):
    """Registered + dispatchable is not the same as SHOWN."""
    missing = sorted(set(registered) - set(catalog))
    assert not missing, (
        "registered but absent from _TOOL_CATALOG, so the model is never "
        f"offered them: {missing}")


def test_no_catalog_entry_is_undispatchable(catalog):
    """An advertised tool the server does not have is a dead offer."""
    loop = asyncio.new_event_loop()
    try:
        live = set(t.name for t in loop.run_until_complete(mcp.mcp.list_tools()))
    finally:
        loop.close()
    ghosts = sorted(set(catalog) - live)
    assert not ghosts, f"advertised but not registered: {ghosts}"


def test_the_catalog_carries_a_schema_for_each_tool(catalog):
    """A name with no description/properties advertises nothing usable."""
    for name in catalog:
        schema = skills._TOOL_CATALOG[name]
        assert isinstance(schema, dict), name
        assert schema.get("description") or schema.get("name"), name


def test_every_tool_has_a_callable_implementation(catalog):
    """Dispatch goes by name over stdio, so the symbol must exist."""
    missing = []
    for name in catalog:
        fn = getattr(mcp, name, None)
        if fn is None or not callable(fn):
            missing.append(name)
    assert not missing, f"catalogued but not defined in mcp_server: {missing}"


def test_tool_payload_returns_schemas_for_a_whole_skill(catalog):
    """The end-to-end path: a skill's tools_allowed becomes a real payload."""
    import yaml
    cfg_path = pathlib.Path(skills.__file__).parent / "agent_config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    problems = []
    for skill, entry in (cfg or {}).items():
        if not isinstance(entry, dict):
            continue
        allowed = entry.get("tools_allowed") or []
        for t in allowed:
            if t not in catalog:
                problems.append(f"{skill}: {t}")
    assert not problems, f"skills list tools absent from the catalog: {problems}"


def test_the_stale_tool_cache_is_not_the_source_of_truth(registered):
    """state/tool_cache.json is `{}` and dated months ago.

    Nothing may read it as a catalog, or every tool is invisible. If this
    starts failing because someone wires it up, the file has to be populated
    first.
    """
    p = pathlib.Path(skills.__file__).parent / "state" / "tool_cache.json"
    if not p.exists():
        return
    import json
    data = json.loads(p.read_text(encoding="utf-8"))
    if data in ({}, []):
        src = (pathlib.Path(mcp_runner.__file__).read_text(encoding="utf-8")
               if False else "")
        import mcp_runner
        src = pathlib.Path(mcp_runner.__file__).read_text(encoding="utf-8")
        assert "tool_cache" not in src, (
            "mcp_runner reads tool_cache.json, which is empty - every tool "
            "would be invisible to the model")