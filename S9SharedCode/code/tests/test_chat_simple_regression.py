"""Regression: the one-shot chat endpoint must not raise UnboundLocalError.

The bug this pins down
----------------------
`chat_simple` computed its tool payload *inside* the `except` branch of the
`chat.tools` flag import:

    try:
        from flags import is_enabled as _flag_on
        _chat_tools_on = _flag_on("chat.tools", True)
    except Exception:
        _chat_tools_on = True
        _tools = _tool_payload(_CHAT_TOOLS) if _chat_tools_on else None
    if _tools:            # <-- ran with _tools never assigned

When the flags import *succeeds* - which it does on every normal launch - the
assignment never executed, so `if _tools` raised UnboundLocalError. The
handler caught it and returned 502 on every single one-shot chat request.
Streaming chat was unaffected because its copy of the block sits at the `try`
level, so the two endpoints disagreed.

Why the suite missed it: the chat tests all exercise the streaming endpoint.
Nothing called the one-shot path, so a total outage of a public endpoint was
invisible to CI.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

import agent_server

HDR = {"X-Aria-Token": "test-token"}


@pytest.fixture()
def client():
    return TestClient(agent_server.app)


@pytest.fixture()
def no_llm(monkeypatch):
    """Keep every model call out of this file.

    The handlers import `LLM` and `run_with_tools` *inside* the function, so a
    module-level patch on `agent_server` is a phantom — `raising=False` creates
    a dead attribute, the real gateway is called, and the test passes only
    because a live LLM happens to be up. That is what this fixture exists to
    prevent: a unit test that spends money and fails on a machine with no
    gateway is not a regression test.

    Document retrieval and memory injection are stubbed too: they are separate
    features with their own tests, and left live they put four-to-six seconds of
    real network per call into a test about tool flags.
    """
    import gateway
    import mcp_runner

    calls: dict = {"llm": 0, "tools": 0, "payloads": []}

    class _LLM:
        def chat(self, **kwargs):
            calls["llm"] += 1
            return {"text": "PONG"}

    async def _run_with_tools(**kwargs):
        calls["tools"] += 1
        calls["payloads"].append(kwargs.get("tools_payload"))
        # The handler does `reply.get("text")`, so the shape is a mapping.
        return {"text": "PONG"}

    async def _passthrough_msgs(messages, query, *a, **k):
        return messages, []

    async def _passthrough(messages, query, *a, **k):
        return messages

    monkeypatch.setattr(gateway, "LLM", _LLM)
    monkeypatch.setattr(mcp_runner, "run_with_tools", _run_with_tools)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)
    monkeypatch.setattr(agent_server, "_with_doc_context", _passthrough_msgs)
    monkeypatch.setattr(agent_server, "_with_chat_memory", _passthrough)
    monkeypatch.setattr(agent_server, "_capture_preference_bg",
                        lambda *a, **k: None)
    # Cost accounting is a separate concern with its own tests, and it is two
    # blocking gateway round-trips per call — the reason this file took 5s.
    monkeypatch.setattr(agent_server, "_session_cost_breakdown",
                        lambda *a, **k: None)
    return calls


def test_chat_simple_succeeds_with_flags_available(client, no_llm):
    """The default path: flags import works, payload assigned, no 502."""
    r = client.post("/api/chat/simple", headers=HDR, json={
        "session_id": "regress-chat-simple-1",
        "query": "Reply with exactly: PONG",
    })
    assert r.status_code == 200, r.text
    assert r.json().get("answer") == "PONG"
    assert no_llm["tools"] + no_llm["llm"] == 1, (
        "the stub was bypassed, so this test called a real model")


def test_chat_simple_tools_assignment_is_at_try_level():
    """Structural pin: `_tools` must not be nested under the except clause.

    Deliberately structural. The behavioural test above covers the happy path
    but cannot tell "fixed" from "the flag import happens to fail in this
    environment", and this pin catches the re-indentation directly.
    """
    src = inspect.getsource(agent_server.chat_simple)
    lines = src.splitlines()

    try_idx = next(i for i, l in enumerate(lines)
                   if l.strip() == "try:" and "flag" in "\n".join(lines[i:i + 4]))
    except_idx = next(i for i, l in enumerate(lines) if i > try_idx
                      and l.strip().startswith("except"))
    assign_idx = next(i for i, l in enumerate(lines)
                      if i > except_idx and "_tools = _chat_tool_payload(" in l)

    assign_indent = len(lines[assign_idx]) - len(lines[assign_idx].lstrip())
    except_indent = len(lines[except_idx]) - len(lines[except_idx].lstrip())

    # The assignment must sit at the same level as the `try`/`except`
    # statements themselves, not one level deeper (which would be "inside
    # the except body"), and not deeper still.
    assert assign_indent == except_indent, (
        f"_tools assignment at indent {assign_indent} but except clause is at "
        f"{except_indent}; if it is deeper it only runs when the flag import "
        f"fails and UnboundLocalError follows on the happy path"
    )


def test_both_chat_endpoints_share_one_tool_list():
    """The one-shot and streaming twins must advertise identical tools.

    They are two copies of the same block; a fix applied to one and not the
    other is exactly how they drifted apart in the first place.
    """
    from skills import tool_payload

    expected = {t["name"] if isinstance(t, dict) else t
                for t in tool_payload(agent_server._CHAT_TOOLS)}

    assert "render_document" in expected
    assert "schedule_task" in expected

    # Both handlers reference the same module-level constant, so there is no
    # second list to drift. They now go through `_chat_tool_payload()`, which
    # reads `_CHAT_TOOLS` and additionally strips the write api_methods chat
    # is not allowed to use - so the pin is on the shared BUILDER, which is
    # the thing that actually has to stay single.
    assert isinstance(agent_server._CHAT_TOOLS, list)
    for handler in (agent_server.chat_simple, agent_server.chat_simple_stream):
        src = inspect.getsource(handler)
        assert "_chat_tool_payload(" in src, (
            "a chat endpoint stopped going through the shared tool builder")
        # A local redefinition would reintroduce the split-brain bug.
        assert "_CHAT_TOOLS =" not in src
        assert "_chat_tool_payload =" not in src

    # And the builder itself must read the shared constant, not a copy.
    bsrc = inspect.getsource(agent_server._chat_tool_payload)
    assert "tool_payload as _tp" in bsrc and "_tp(_CHAT_TOOLS)" in bsrc, (
        "the chat tool builder stopped reading the shared constant")


def test_chat_simple_tools_disabled_flag_yields_no_tools(client, monkeypatch,
                                                         no_llm):
    """With chat.tools off the model must be told it has no tools.

    This asserted only on the status code, so it passed identically with the
    flag on or off — the test name was the specification and the body checked
    nothing. It now asserts that the tool loop was not entered.
    """
    import flags

    monkeypatch.setattr(flags, "is_enabled",
                        lambda key, default=True: False, raising=False)

    r = client.post("/api/chat/simple", headers=HDR, json={
        "session_id": "regress-chat-simple-2",
        "query": "Reply with exactly: PONG",
    })
    assert r.status_code == 200, r.text

    assert no_llm["tools"] == 0, (
        "the tool loop ran with chat.tools disabled")
    assert no_llm["payloads"] == [], (
        "a tool payload was still sent to the model with the flag off")
    assert no_llm["llm"] == 1, "expected the plain-text path instead"


def test_chat_simple_with_tools_on_advertises_the_catalog(client, no_llm):
    """The counterpart: with the flag on, the model IS offered the tools."""
    r = client.post("/api/chat/simple", headers=HDR, json={
        "session_id": "regress-chat-simple-3",
        "query": "Reply with exactly: PONG",
    })
    assert r.status_code == 200, r.text

    assert no_llm["tools"] == 1
    names = {t["name"] if isinstance(t, dict) else t
             for t in (no_llm["payloads"][0] or [])}
    assert "render_document" in names
    assert "schedule_task" in names
