"""The render tool has to be a real MCP tool, and the user's setup has to be
applied before the call leaves.

`render_document` was a bare function in mcp_server.py: not decorated with
`@mcp.tool()`, so absent from the advertised tool list, so every model call
for it returned "Unknown tool: render_document". A run only produced a file
when the model happened to also write the whole spec as JSON data for the
in-process fallback - observed live at roughly one run in two.

Separately, the setup rewrite lived in `on_outcome`, which the tool loop
calls AFTER dispatch. The call had already gone out over the wire, so the
arguments the tool received were the model's own and the chosen format, page
and style were discarded - a rewrite in the only place that looked right was
in the one place that was too late.
"""
import ast
from pathlib import Path

import pytest

import mcp_server
import mcp_runner

MCP_SERVER = Path(mcp_server.__file__)


def test_render_document_is_registered_as_an_mcp_tool():
    """A bare function is invisible to the model. This is the whole bug."""
    tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "render_document")
    decorators = []
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        parts = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        decorators.append(".".join(reversed(parts)))
    assert any(d.endswith("tool") for d in decorators), (
        "render_document is not decorated with @mcp.tool(), so the model "
        f"cannot call it. decorators={decorators}")


def test_render_document_is_advertised_and_callable_over_mcp():
    """End to end over the real stdio transport, not a mock.

    Asserting the decorator exists is not enough: the schema has to build and
    the call has to succeed, which is what a run actually depends on.
    """
    import asyncio
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    sp = StdioServerParameters(command=sys.executable, args=[str(MCP_SERVER)],
                               env={"PYTHONUTF8": "1"}, encoding="utf-8")

    async def _go():
        async with stdio_client(sp) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                names = {t.name for t in tools.tools}
                assert "render_document" in names, (
                    f"not advertised; {len(names)} tools listed")
                res = await s.call_tool("render_document", arguments={
                    "format": "pdf", "title": "Registration Probe",
                    "blocks": [{"type": "heading", "level": 1, "text": "H"},
                               {"type": "paragraph", "text": "Body."}]})
                text = "\n".join(getattr(c, "text", str(c))
                                 for c in res.content)
                return getattr(res, "isError", False), text

    is_error, text = asyncio.run(_go())
    assert not is_error, f"call failed: {text[:300]}"
    assert "artifact" in text, text[:300]


def test_the_setup_rewrite_runs_before_dispatch_not_after():
    """`prepare_fn` is the only hook that can still change what a tool sees.

    Assert this by wiring the real loop: prepare_fn must fire while
    dispatch_fn has not yet been called.
    """
    import asyncio

    seen = {}

    async def _chat(messages):
        if not any(m.get("role") == "assistant" for m in messages):
            return {"text": "", "provider": "t", "tool_calls": [{
                "id": "c1", "name": "render_document",
                "arguments": {"format": "pdf", "title": "X", "blocks": []}}]}
        return {"text": "done", "provider": "t", "tool_calls": []}

    def _prepare(name, args):
        seen["prepared_before_dispatch"] = "dispatched" not in seen
        args = dict(args)
        args["format"] = "docx"
        return args

    async def _dispatch(name, args):
        seen["dispatched"] = True
        seen["format_seen_by_tool"] = args.get("format")
        return "ok"

    asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "go"}],
        chat_fn=_chat, dispatch_fn=_dispatch,
        prepare_fn=_prepare))

    assert seen.get("prepared_before_dispatch") is True, (
        "prepare_fn ran after the tool was already called")
    assert seen.get("format_seen_by_tool") == "docx", seen