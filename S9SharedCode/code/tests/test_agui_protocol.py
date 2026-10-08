"""The AG-UI endpoint must actually speak AG-UI.

Two classes of test here. The first is unit-level on the encoder: exact shapes,
and the spec's hard rules (a run starts with RUN_STARTED and ends with exactly
one terminal event; TEXT_MESSAGE_CONTENT carries a non-empty delta; tool calls
are correlated by toolCallId). The second is endpoint-level: that a real
request produces that event sequence, and that the flag can switch it off.

The ordering tests matter more than they look. AG-UI clients are strict — the
1.0 schema rejects unknown fields — so a stream that is merely "close enough"
fails validation and the client drops the whole run, not just the bad event.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agui  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import agent_server  # noqa: E402

client = TestClient(agent_server.app)


def frames(text: str) -> list[dict]:
    """Parse an SSE body into event dicts."""
    out = []
    for line in text.split("\n\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        out.append(json.loads(line[6:]))
    return out


# ── encoder shapes ───────────────────────────────────────────────────────────

def test_every_event_carries_a_type_and_a_timestamp():
    for e in (agui.run_started("t", "r"), agui.run_finished("t", "r"),
              agui.run_error("x"), agui.text_start("m"),
              agui.text_content("m", "hi"), agui.text_end("m"),
              agui.tool_call_start("c", "search"), agui.tool_call_args("c", "{}"),
              agui.tool_call_end("c", "search"),
              agui.tool_call_result("c", "m", "done"),
              agui.activity_snapshot("m", "thinking")):
        assert e.get("type"), e
        assert isinstance(e.get("timestamp"), int), e


def test_run_finished_omits_an_absent_result_rather_than_nulling_it():
    """An absent field and an explicit null are different to a strict schema."""
    assert "result" not in agui.run_finished("t", "r")
    assert "result" not in agui.run_finished("t", "r", result=None)
    assert agui.run_finished("t", "r", result={"a": 1})["result"] == {"a": 1}


def test_empty_delta_is_refused_not_emitted():
    """The spec requires a non-empty delta. Emitting one fails a client's
    validation and takes the whole stream down with it, so the guard lives in
    one place - `content()` - rather than at every call site."""
    assert agui.content(agui.text_content("m", "")) is None
    assert agui.content(agui.text_content("m", "x")) is not None
    # Non-text events are unaffected by the rule.
    assert agui.content(agui.text_end("m")) is not None


def test_sse_framing_matches_the_documented_encoder():
    frame = agui.sse(agui.text_content("m", "hi"))
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame[6:])["delta"] == "hi"


def test_new_ids_treats_a_missing_conversation_as_new_not_shared():
    """A constant default would make every anonymous run share one thread and
    their histories would interleave."""
    a_t, a_r, a_m = agui.new_ids(None)
    b_t, b_r, b_m = agui.new_ids(None)
    assert a_t != b_t
    assert a_r != b_r and a_m != b_m
    # A supplied conversation id becomes the thread id, so an AG-UI client and
    # the console address the same conversation.
    t, r, m = agui.new_ids("conv-7")
    assert t == "conv-7"
    assert r and m


def test_capabilities_only_claims_what_is_implemented():
    caps = agui.capabilities(streaming=True, tools=["web_search"])
    tr = caps["transport"]
    assert tr["streaming"] is True
    # Claiming a transport we do not implement sends a client down a path that
    # fails mid-run.
    for unsupported in ("websocket", "http_binary", "resumable",
                        "pushNotifications"):
        assert tr[unsupported] is False, unsupported
    assert caps["tools"]["toolCalling"] is True
    assert caps["tools"]["tools"] == ["web_search"]
    assert caps["reasoning"]["reasoning"] is False
    assert caps["multiAgent"]["subAgents"] is False
    assert caps["execution"]["codeExecution"] is False


# ── request parsing ──────────────────────────────────────────────────────────

def test_console_shaped_body_is_accepted():
    p = agui.parse_request({"query": "hello", "conversation_id": "c1"})
    assert p["query"] == "hello"
    assert p["thread_id"] == "c1"


def test_agui_shaped_body_is_accepted():
    p = agui.parse_request({
        "threadId": "th", "runId": "r1",
        "messages": [{"role": "user", "content": "hi there"}],
    })
    assert p["thread_id"] == "th"
    assert p["run_id"] == "r1"
    assert p["query"] == "hi there"


def test_tool_names_are_read_from_the_openai_shape_and_plain_strings():
    p = agui.parse_request({
        "query": "q",
        "tools": [{"function": {"name": "web_search"}}, "fetch_url",
                  {"name": "bare"}],
    })
    assert p["tool_names"] == ["web_search", "fetch_url", "bare"]


def test_a_malformed_body_is_rejected_with_a_reason():
    bad_bodies = [
        ([], "body is not an object"),
        ({"messages": "nope"}, "messages not a list"),
        ({"query": "q", "messages": [{"role": "wizard", "content": "x"}]},
         "bad role"),
        ({"query": "q", "messages": [{"role": "user", "content": 5}]},
         "non-string content is coerced"),
        ({"query": "q", "state": []}, "state not an object"),
        ({}, "no query anywhere"),
    ]
    for body, why in bad_bodies:
        if why == "non-string content is coerced":
            # Numeric content is coerced to a string rather than refused: it is
            # unambiguous and refusing it would be surprising. An explicit
            # `query` still wins over a user message, so this body yields "q".
            assert agui.parse_request(body)["query"] == "q"
            continue
        try:
            agui.parse_request(body)
        except agui.AguiRequestError as e:
            assert str(e), f"{why}: empty error"
        else:
            raise AssertionError(f"{body!r} ({why}) should have been rejected")


def test_non_text_content_parts_are_refused_loudly_not_dropped():
    """Silently discarding an image part would answer a question the user did
    not ask, with no indication anything was lost."""
    try:
        agui.parse_request({
            "query": "describe",
            "messages": [{"role": "user",
                          "content": [{"type": "image", "url": "x"}]}],
        })
    except agui.AguiRequestError as e:
        assert "content parts" in str(e)
    else:
        raise AssertionError("image-only content should be rejected")


def test_oversized_query_is_rejected_with_413():
    try:
        agui.parse_request({"query": "x" * (agui.MAX_INPUT_CHARS + 1)})
    except agui.AguiRequestError as e:
        assert e.status == 413
    else:
        raise AssertionError("oversized query should be rejected")


# ── endpoint behaviour ───────────────────────────────────────────────────────

def test_capabilities_endpoint_is_available_and_names_the_flag():
    r = client.get("/api/capabilities")
    assert r.status_code == 200
    caps = r.json()
    assert caps["transport"]["streaming"] is True
    assert "aguiEndpointEnabled" in caps["custom"]


def test_capabilities_lists_the_tools_chat_actually_has():
    """Aria's tool payload is `{name, input_schema}`, not OpenAI's
    `{"function": {"name"}}`. Reading the nested form produced an empty list,
    so the document told clients Aria has no tools while it was calling them on
    every chat request."""
    caps = client.get("/api/capabilities").json()
    assert caps["tools"]["toolCalling"] is True, caps["tools"]
    names = caps["tools"]["tools"]
    assert "web_search" in names, names
    assert "search_knowledge" in names, names
    # Sorted and deduplicated, so the document is stable between calls.
    assert names == sorted(names), names
    assert len(names) == len(set(names)), names


def test_agui_rejects_a_body_with_no_query():
    r = client.post("/api/agui", json={"messages": []})
    assert r.status_code == 400, r.text
    assert "query required" in r.json()["error"]


def test_agui_rejects_non_json():
    r = client.post("/api/agui", content=b"not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_agui_emits_a_well_formed_run(monkeypatch):
    """A real request, a stubbed model. Asserts the sequence the spec requires.

    `mcp_runner.LLM` is patched too, for the same reason as the failure test
    below: the name is bound at import time there.
    """
    import gateway as G
    import mcp_runner as MR

    class _LLM:
        def chat(self, **kw):
            return {"text": "the answer is four"}

    monkeypatch.setattr(G, "LLM", _LLM)
    monkeypatch.setattr(MR, "LLM", _LLM)
    monkeypatch.setattr(G, "ensure_gateway", lambda: None)
    monkeypatch.setattr(agent_server, "_session_cost_breakdown",
                        lambda _s: {})
    monkeypatch.setattr(agent_server, "_session_cost_delta",
                        lambda _s, _b: None)
    monkeypatch.setattr(agent_server, "_with_doc_context",
                        _async_none)

    r = client.post("/api/agui",
                    json={"query": "what is the answer", "conversation_id": "t-agui"})
    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers["content-type"]
    ev = frames(r.text)
    assert ev, r.text

    types = [e["type"] for e in ev]
    # Lifecycle: RUN_STARTED first, exactly one terminal event, last.
    assert types[0] == "RUN_STARTED", types
    assert types[-1] == "RUN_FINISHED", types
    assert types.count("RUN_STARTED") == 1
    assert types.count("RUN_FINISHED") == 1
    assert "RUN_ERROR" not in types

    # Message framing: start before any content, end after it.
    assert types.index("TEXT_MESSAGE_START") < types.index("TEXT_MESSAGE_CONTENT")
    assert types.index("TEXT_MESSAGE_CONTENT") < types.index("TEXT_MESSAGE_END")

    # Every content delta is non-empty and tagged with the same message.
    mids = {e["messageId"] for e in ev if e["type"].startswith("TEXT_MESSAGE")}
    assert len(mids) == 1, mids
    for e in ev:
        if e["type"] == "TEXT_MESSAGE_CONTENT":
            assert e["delta"], e

    started = ev[0]
    assert started["threadId"] == "t-agui"
    assert started["runId"]
    assert ev[-1]["threadId"] == "t-agui"
    assert ev[-1]["runId"] == started["runId"]
    assert "four" in ev[-1]["result"]["answer"]


def test_agui_reports_a_model_failure_as_run_error_not_a_silent_end(
        monkeypatch):
    """A stream that dies without a terminal event leaves the client waiting
    forever and renders a partial answer as a finished one.

    Patches `mcp_runner.LLM` as well as `gateway.LLM`. `mcp_runner` binds the
    name at import time (`from gateway import LLM`), so patching only the
    gateway attribute never reached the tool loop that AG-UI actually uses.

    This test was passing for the wrong reason until the tool loop's
    use-before-import of `os` was fixed: the loop raised `UnboundLocalError`,
    AG-UI correctly reported RUN_ERROR, and the assertion was satisfied by a
    crash rather than by the intended stubbed failure.
    """
    import gateway as G
    import mcp_runner as MR

    class _Boom:
        def chat(self, **kw):
            raise RuntimeError("provider exploded")

    monkeypatch.setattr(G, "LLM", _Boom)
    monkeypatch.setattr(MR, "LLM", _Boom)
    monkeypatch.setattr(G, "ensure_gateway", lambda: None)
    monkeypatch.setattr(agent_server, "_session_cost_breakdown",
                        lambda _s: {})
    monkeypatch.setattr(agent_server, "_session_cost_delta",
                        lambda _s, _b: None)
    monkeypatch.setattr(agent_server, "_with_doc_context", _async_none)

    r = client.post("/api/agui", json={"query": "q", "conversation_id": "t-boom"})
    assert r.status_code == 200, r.text
    types = [e["type"] for e in frames(r.text)]
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_ERROR", types
    assert "RUN_FINISHED" not in types
    err = frames(r.text)[-1]
    assert "provider exploded" in err["message"]
    assert err["code"]


def test_the_flag_can_switch_the_endpoint_off(monkeypatch):
    """Kill-switch. The capability document stays reachable either way, so a
    client can tell "off" from "does not exist"."""
    import flags
    monkeypatch.setattr(flags, "is_enabled", lambda name, default=None: False)
    r = client.post("/api/agui", json={"query": "q"})
    assert r.status_code == 404, r.text
    assert "chat.agui" in r.json()["error"]
    caps = client.get("/api/capabilities").json()
    assert caps["custom"]["aguiEndpointEnabled"] is False


def _async_none(messages, query, doc_ids):
    """Stand-in for _with_doc_context that injects nothing."""
    async def go():
        return messages, 0
    return go()
