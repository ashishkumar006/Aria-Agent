"""API contract tests for the Aria agent server (agent_server.py).

Uses FastAPI's TestClient against the in-process `app`. Only the non-LLM
endpoints are exercised (health, cost, UI route, session resolution) so the
suite stays fast and does not burn tokens or hit the orchestrator.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server
import flags
import gateway
from agent_server import app, resolve_session


def test_health_contract():
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body.get("agent") == "ready"
        assert "gateway_up" in body


def test_index_route_does_not_500():
    with TestClient(app) as client:
        r = client.get("/")        # 200 if the UI bundle is present, 404 if not — must never 500
        assert r.status_code in (200, 404)


def test_cost_endpoint_empty_session():
    with TestClient(app) as client:
        r = client.get("/api/cost")
        assert r.status_code == 200
        body = r.json()
        assert "rows" in body
        assert "totals" in body


def test_resolve_session_stable_per_conversation():
    a = resolve_session("test-conv-aria")
    b = resolve_session("test-conv-aria")
    assert a == b
    assert a.startswith("s8-")


def test_resolve_session_fresh_when_unscoped():
    a = resolve_session(None)
    b = resolve_session(None)
    assert a != b
    assert a.startswith("s8-")


def test_sessions_counts_nodes_that_never_executed():
    """A run stopped before its first dispatch has a `skipped` node in
    graph.json but ZERO n_*.json state files (those are only written when a
    node actually runs). The sidebar counted only state files, so a stopped
    run showed "0 nodes" while the canvas showed one."""
    import networkx as nx

    from persistence import SessionStore

    sid = "t-sess-skipcount"
    store = SessionStore(sid)
    store.write_query("stopped before dispatch")
    g = nx.DiGraph()
    g.add_node("n:1", status="skipped", skill="planner", label="planner")
    store.write_graph(g)

    with TestClient(app) as client:
        rows = [s for s in client.get("/api/sessions?limit=500").json()["sessions"]
                if s["session_id"] == sid]
    assert rows, "session missing from the sidebar list"
    assert rows[0]["nodes"] == 1
    assert rows[0]["status_counts"] == {"skipped": 1}


# ── chat streaming (SSE) ────────────────────────────────────────────────────
def _frames(body: str) -> list[dict]:
    """Parse an SSE body into the JSON frames it carries, in order."""
    import json
    out = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def test_chat_stream_emits_started_deltas_done(monkeypatch):
    """The lightweight chat stream must open with `started`, deliver the
    answer as `delta` chunks, and close with `done` — the order the Chat UI
    relies on to paint progressively."""
    monkeypatch.setattr(flags, "is_enabled", lambda *a, **k: False)

    class _LLM:
        def chat(self, **kw):
            return {"text": "streamed answer here", "provider": "fake"}

    monkeypatch.setattr(gateway, "LLM", _LLM)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)

    with TestClient(app) as client:
        r = client.post("/api/chat/simple/stream", json={"query": "hi"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = _frames(r.text)
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "started" and kinds[-1] == "done", kinds
    assert "status" in kinds
    assert "".join(f.get("text", "") for f in frames if f["type"] == "delta"
                   ).strip() == "streamed answer here"
    done = frames[-1]
    assert done["answer"] == "streamed answer here"
    assert str(done["conversation_id"]).startswith("ct-")


def test_chat_stream_persists_thread_and_reuses_id(monkeypatch):
    """Streaming must persist to the same thread store as /api/chat/simple,
    and a second turn on the same conversation_id must keep that history."""
    import flags
    monkeypatch.setattr(flags, "is_enabled", lambda *a, **k: False)
    monkeypatch.setattr(agent_server, "_CHAT_MAX_THREADS", 5)

    class _LLM:
        def chat(self, **kw):
            return {"text": "ok", "provider": "fake"}

    monkeypatch.setattr(gateway, "LLM", _LLM)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)

    with TestClient(app) as client:
        first = _frames(client.post("/api/chat/simple/stream",
                                    json={"query": "one"}).text)
        cid = first[-1]["conversation_id"]
        client.post("/api/chat/simple/stream",
                    json={"query": "two", "conversation_id": cid})
        thread = client.get(f"/api/chat/threads/{cid}").json()
    roles = [m["role"] for m in thread["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"], roles
    assert thread["messages"][-1]["content"] == "ok"


def test_chat_stream_rejects_empty_query():
    with TestClient(app) as client:
        r = client.post("/api/chat/simple/stream", json={"query": "   "})
    assert r.status_code == 400


def test_chat_stream_rejects_oversized_query():
    import agent_server as a
    with TestClient(app) as client:
        r = client.post("/api/chat/simple/stream",
                        json={"query": "x" * (a._CHAT_MAX_QUERY + 1)})
    assert r.status_code == 400


def test_chat_chunks_reassembles_to_the_original_text():
    """Chunking is a transport detail — the joined text must be identical
    to the answer, or streamed answers would silently lose characters."""
    for text in ["one", "a b c", "word " * 200, "line1\nline2",
                 "punctuation, everywhere! (really)", ""]:
        assert " ".join(agent_server._chat_chunks(text)).split() == \
            text.split()


def test_chat_stream_heartbeats_during_a_slow_call(monkeypatch):
    """A slow provider must not leave the client on a stale 'thinking…'.
    The stream emits a periodic elapsed-time status frame while the model
    call is still in flight."""
    monkeypatch.setattr(flags, "is_enabled", lambda *a, **k: False)
    monkeypatch.setattr(agent_server, "_CHAT_STREAM_HEARTBEAT_S", 0.05)

    import asyncio as _a

    class _SlowLLM:
        def chat(self, **kw):
            import time
            time.sleep(0.4)  # blocking on purpose: models are sync
            return {"text": "slow but sure", "provider": "fake"}

    monkeypatch.setattr(gateway, "LLM", _SlowLLM)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)

    with TestClient(app) as client:
        r = client.post("/api/chat/simple/stream", json={"query": "slow"})
    beats = [f for f in _frames(r.text)
             if f["type"] == "status" and "still working" in f.get("text", "")]
    assert beats, "no heartbeat while the model call was in flight"
    assert "elapsed" in beats[0]["text"]


def test_chat_stream_reports_error_when_persistence_fails(monkeypatch):
    """A corrupt thread store or a full disk used to let the exception
    escape the async generator: the response truncated, so the client saw
    deltas but NO terminal frame, rendered a partial answer as finished, and
    the turn was silently not stored. It must now be a clean `error`."""
    monkeypatch.setattr(flags, "is_enabled", lambda *a, **k: False)

    class _LLM:
        def chat(self, **kw):
            return {"text": "a perfectly good answer", "provider": "fake"}

    monkeypatch.setattr(gateway, "LLM", _LLM)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)

    real_load = agent_server._chat_threads_load
    calls = {"n": 0}

    def _load():
        # 1: pre-flight (outside the generator), 2: building the message
        # list, 3: persisting the reply. Only the last one is wrapped.
        calls["n"] += 1
        if calls["n"] >= 3:
            raise OSError("No space left on device")
        return real_load()

    monkeypatch.setattr(agent_server, "_chat_threads_load", _load)

    with TestClient(app) as client:
        r = client.post("/api/chat/simple/stream", json={"query": "hi"})
    frames = _frames(r.text)
    assert frames[-1]["type"] == "error", frames[-1]
    assert "could not be saved" in frames[-1]["text"]


def test_sse_responses_disable_proxy_buffering():
    """Behind nginx (the default `proxy_buffering on`) these headers are
    what stop a whole research run being held in the proxy's buffer. Every
    text/event-stream response must carry them."""
    import re
    src = (ROOT / "agent_server.py").read_text(encoding="utf-8")
    assert "X-Accel-Buffering" in src
    sse = [m for m in re.finditer(r"StreamingResponse\(", src)
           if "text/event-stream" in src[m.end():m.end() + 60]
           or "_SSE_HEADERS" in src[m.end():m.end() + 60]]
    assert sse, "no text/event-stream responses found"
    for m in sse:
        tail = src[m.end():m.end() + 60]
        assert "_SSE_HEADERS" in tail, \
            f"an SSE response bypasses the shared header set: {tail!r}"
        # `**_SSE_HEADERS` (unpacked), never `headers=_SSE_HEADERS`: the
        # bundle is {"media_type":..., "headers":{...}}, so passing it as a
        # value makes StreamingResponse try to latin-1-encode the nested dict
        # and every such response 500s. That bug shipped once already.
        window = src[max(0, m.start() - 120):m.end() + 60]
        assert "headers=_SSE_HEADERS" not in window, \
            "SSE response passed the kwargs bundle as a headers value"


def test_root_serves_the_spa_not_the_legacy_ui():
    """`/` used to hard-code the legacy vanilla UI, so the documented root
    URL opened a completely different application from the console."""
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    if agent_server._SPA_INDEX.exists():
        assert "assets/" in r.text or "<div id=" in r.text or "<script" in r.text
        # the React shell, not the legacy page
        assert "Aria — General AI Agent" not in r.text


def test_every_console_route_is_registered():
    """All ten console pages must be served, and `/` must be the SPA.

    A decorator typo silently unregistered a whole page: `/console` returned
    404 with `{"detail":"Not Found"}` — a page that had been the default
    landing view. Route registration is cheap to assert and impossible to
    notice by eye, so it is asserted."""
    routes = {r.path for r in app.routes}
    for p in ("/", "/console", "/research", "/runs", "/memory", "/scheduler",
              "/skills", "/apps", "/ledger", "/mission", "/settings"):
        assert p in routes, f"console route not registered: {p}"
    with TestClient(app) as client:
        for p in ("/console", "/research", "/runs", "/memory", "/scheduler",
                  "/skills", "/apps", "/ledger", "/mission", "/settings", "/"):
            r = client.get(p, timeout=30)
            assert r.status_code == 200, f"{p} -> HTTP {r.status_code}"
            if agent_server._SPA_INDEX.exists():
                assert "Not Found" not in r.text, f"{p} served a 404 body"


def test_health_reports_whether_the_spa_is_built():
    """A machine with no dist/ silently serves the legacy UI at every
    route; health must be able to assert which build is live."""
    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert body["spa_built"] == agent_server._SPA_INDEX.exists()


def test_sessions_and_runs_summary_clamp_their_limit():
    """Each row parses a whole graph.json, so an unclamped limit was a
    one-request way to make the server parse every session ever recorded."""
    with TestClient(app) as client:
        for path in ("/api/sessions?limit=100000",
                     "/api/runs/summary?limit=100000"):
            r = client.get(path, timeout=60)
            assert r.status_code == 200, path
            key = "sessions" if "sessions?" in path else "runs"
            assert len(r.json()[key]) <= 500, path
        assert client.get("/api/sessions?limit=0").status_code == 200


def test_tool_catalog_exposes_its_real_parameter_schema():
    """The catalog keys each tool's schema as `input_schema`; the endpoint
    looked for `params`/`parameters`, so all 26 tools reported {}."""
    with TestClient(app) as client:
        tools = client.get("/api/tools").json()["tools"]
    with_schema = [t for t in tools if t.get("params")]
    assert tools, "no tools in catalog"
    assert with_schema, "every tool reports an empty parameter schema"


def test_delete_unknown_session_is_404_not_200_ok():
    """Deleting something that never existed returned 200 ok, so a typo'd
    id was indistinguishable from a real deletion."""
    with TestClient(app) as client:
        r = client.delete("/api/sessions/s8-does-not-exist-12345")
        assert r.status_code == 404, r.status_code
        r = client.delete("/api/sessions/!!!not-a-valid-id!!!")
        assert r.status_code == 400, r.status_code


def test_light_graph_omits_node_states_key():
    """light=true exists so the 2s poll never downloads node states — but
    the key was still emitted (empty), so the client couldn't tell light
    mode from 'no states' and every poll carried dead weight."""
    with TestClient(app) as client:
        sessions = client.get("/api/sessions?limit=50").json()["sessions"]
    assert sessions, "need at least one session for this test"
    with TestClient(app) as client:
        g = client.get(
            f"/api/sessions/{sessions[0]['session_id']}/graph?light=true").json()
    assert "node_states" not in g, "light graph must omit node_states"
    assert isinstance(g.get("nodes"), list)


def test_sessions_list_is_newest_first():
    """The listing used reverse-lexicographic id order over random-hex
    ids, so limit=N could omit a run that finished seconds ago."""
    with TestClient(app) as client:
        a = client.get("/api/sessions?limit=500").json()["sessions"]
    assert a, "need sessions for this test"
    # Order check via the public field: each row's updated_ago grows
    # monotonically (smaller = more recent first).
    agos = [s.get("updated_ago") for s in a]
    agos = [x for x in agos if isinstance(x, (int, float))]
    assert agos, "no updated_ago fields to order by"
    assert agos == sorted(agos), "sessions not newest-first"


def test_schedule_is_idempotent_and_errors_name_formats():
    """Posting the identical enabled job twice must return the SAME id
    (double-click/retry safety), and a garbage `when` must name the valid
    formats instead of leaking 'could not convert string to float'."""
    import uuid as _uuid
    tag = f"qa-idem-{_uuid.uuid4().hex[:6]}"
    with TestClient(app) as client:
        r1 = client.post("/api/schedule", json={"query": tag, "when": "in 30m"})
        assert r1.status_code == 200, r1.text
        r2 = client.post("/api/schedule", json={"query": tag, "when": "in 30m"})
        assert r2.status_code == 200, r2.text
        assert r1.json()["id"] == r2.json()["id"], "duplicate job created"
        client.delete(f"/api/schedule/{r1.json()['id']}?hard=true")
        bad = client.post("/api/schedule",
                          json={"query": tag, "when": "not-a-time"})
        assert bad.status_code == 400, bad.status_code
        assert "in 30m" in bad.json()["error"], bad.json()


def test_topic_is_the_users_question_not_the_skill_prompt():
    """A research run stores the *researcher system prompt* in query.txt with
    the real question appended under a "Topic:" line, so every Runs row was
    titled with the first sentence of the prompt ("You are a research
    agent...") and only the browser recovered the topic, by regex."""
    assert agent_server._topic_of(
        "You are a research agent. Research this topic and deliver a "
        "final report as Markdown.\n\nTopic: tallest building in the world?"
    ) == "tallest building in the world?"
    # No Topic: line -> the prompt's own first line is all we have.
    assert agent_server._topic_of("Research Paris history in depth.") == \
        "Research Paris history in depth."
    assert agent_server._topic_of("") == ""
    assert agent_server._topic_of("\n\n  \n") == ""
    # The payload must carry it, not just the helper.
    with TestClient(app) as client:
        rows = client.get("/api/sessions?limit=50").json()["sessions"]
        runs = client.get("/api/runs/summary?limit=50").json()["runs"]
    assert rows, "need sessions for this test"
    for row in rows + runs:
        assert "topic" in row, f"{row['session_id']} has no topic field"
        if not row["topic"]:
            continue
        assert not row["topic"].lower().startswith("you are a"), \
            f"topic still leaks the system prompt: {row['topic']!r}"


def test_stale_running_nodes_are_reported_as_interrupted():
    """A node keeps status 'running' in graph.json until the executor marks
    it terminal. If the process dies first, nothing rewrites it and the UI
    showed that run as 'live' forever — a 13h-old run still carried a blue
    'live' pill. Runs older than STALE_RUN_S must read as interrupted."""
    now = 1_000_000.0
    fresh = {"running": 2, "pending": 1}
    assert agent_server._reconcile_status(fresh, now - 10, now) == fresh, \
        "a run that was just written must keep its live status"
    stale = agent_server._reconcile_status(dict(fresh), now - 10_000, now)
    assert stale.get("running", 0) == 0, stale
    assert stale["interrupted"] == 2, stale
    assert "pending" in stale, "untouched statuses must survive"
    # Single-node form used by the light graph endpoint.
    assert agent_server._stale_node_status("running", now - 10, now) == "running"
    assert agent_server._stale_node_status("running", now - 10_000, now) == \
        "interrupted"
    assert agent_server._stale_node_status("done", now - 10_000, now) == "done"
    # Terminal runs are never relabelled, however old.
    done = {"done": 3}
    assert agent_server._reconcile_status(done, now - 10_000, now) == done


def test_log_bursts_fold_into_one_feed_row():
    """One mishap writes 15-25 log lines: the frame list, the echoed source,
    caret markers, the exception, a docs URL. The Console feed tailed the file
    verbatim, so a single bad request filled the view with red and the ERRORS
    counter read 73 for six real incidents.

    The samples below are verbatim shapes from logs/agent.out and agent.err."""
    # scheduler.py traceback, including a mid-line "Traceback" header
    sched = [
        "[scheduler] [sch-abf344ce] run failed: Traceback (most recent call last):",
        '  File "C:\\...\\code\\scheduler.py", line 285, in _fire',
        "    answer = asyncio.run(",
        "            ^^^^^^^^^^^^",
        '  File "C:\\...\\asyncio\\runners.py", line 615, in run',
        "    return runner.run(main)",
        "          ^^^^^^^^^^^^^^^^",
        "UnicodeEncodeError: 'charmap' codec can't encode character '\\U0001f4c5'",
        "",
        'INFO:     127.0.0.1:57745 - "GET /api/schedule HTTP/1.1" 200 OK',
    ]
    folded = agent_server._fold_log_block(sched)
    assert len(folded) == 2, folded          # the burst + the access-log line
    text, absorbed = folded[0]
    assert absorbed == 7, folded             # 7 of the 8 burst lines absorbed
    assert "UnicodeEncodeError" in text and "charmap" in text
    assert "sch-abf344ce" in text, "the row must keep its origin tag"
    assert "+7 lines in the log" in text

    # pydantic JSON-RPC burst: detail lines at column 0, not just indentation
    pyd = [
        "For further information visit https://errors.pydantic.dev/2.13/v/json_invalid",
        "Invalid JSON: expected value at line 1 column 1 [type=json_invalid, input_value=' / ']",
        "pydantic_core._pydantic_core.ValidationError: 1 validation error for JSONRPCMessage",
        "==================================================",
        "return cls.__pydantic_validator__.validate_json(",
        'File "C:\\...\\pydantic\\main.py", line 782, in model_validate_json',
        "Traceback (most recent call last):",
        '  File "C:\\...\\mcp\\client\\stdio\\__init__.py", line 155, in stdout_reader',
        "Failed to parse JSONRPC message from server",
    ]
    # pydantic JSON-RPC burst: detail lines at column 0, not just indentation.
    # A second Traceback header splits the run, so this is 2 rows — the
    # exception block, then the traceback (named after its frame).
    folded_pyd = agent_server._fold_log_block(pyd)
    assert len(folded_pyd) == 2, folded_pyd
    assert "JSONRPCMessage" in folded_pyd[0][0], folded_pyd[0]
    assert "stdout_reader" in folded_pyd[1][0], \
        f"a frameless traceback row must name its frame: {folded_pyd[1][0]}"

    # Chained bursts must never produce a mega-row: whatever the split, no
    # output row may still contain frame noise, carets, or a path dump.
    chained = agent_server._fold_log_block(pyd + pyd)
    assert len(chained) <= 4, chained
    for text, _absorbed in chained:
        assert "^^^^" not in text, text
        assert "return cls." not in text, text
        assert ".py\", line" not in text, text

    # Ordinary output must never be swallowed or decorated.
    plain = [
        'INFO:     127.0.0.1:57745 - "GET /api/schedule HTTP/1.1" 200 OK',
        "[memory.read] 8 hit(s) visible to every skill this run",
        "[n:1] planner            complete (5.4s)[n:2] researcher         failed   (0.0s)  err=exception: AttributeError",
        "[warmup] ollama embedding model loaded",
    ]
    assert agent_server._fold_log_block(plain) == [(l, 0) for l in plain]
    # A lone exception line is a real message, not a burst to be summarised.
    solo = agent_server._fold_log_block(["[scheduler] job failed: ValueError: bad when"])
    assert solo == [("[scheduler] job failed: ValueError: bad when", 0)], solo


def test_tools_config_writes_the_file_the_guard_reads():
    """POST /api/config/tools must persist under $S9_STATE_DIR —
    the same file skills._disabled_tools live-reads. A hardcoded
    ROOT/state path made every toggle a silent no-op whenever
    S9_STATE_DIR was set, and a non-atomic write could leave a
    truncated JSON that the guard's fail-open path turned into
    "nothing is withheld"."""
    import json
    import skills

    state = Path(agent_server.STATE_DIR)
    guard = state / "tools_disabled.json"
    if guard.exists():
        guard.unlink()
    try:
        with TestClient(app) as client:
            r = client.post("/api/config/tools",
                            json={"tool": "web_search", "enabled": False})
            assert r.status_code == 200, r.text
            assert r.json() == {"tool": "web_search",
                                "enabled": False,
                                "disabled": ["web_search"]}
            # The file the endpoint wrote is the file the guard reads.
            assert json.loads(
                guard.read_text(encoding="utf-8")) == {
                    "tools": ["web_search"]}
            assert "web_search" in skills._disabled_tools()
            # Re-enabling removes it again.
            r = client.post("/api/config/tools",
                            json={"tool": "web_search", "enabled": True})
            assert r.status_code == 200
            assert "web_search" not in skills._disabled_tools()
            # Unknown tool names are refused, not persisted.
            r = client.post("/api/config/tools",
                            json={"tool": "not_a_real_tool",
                                  "enabled": False})
            assert r.status_code == 400
    finally:
        if guard.exists():
            guard.unlink()


def test_tool_guard_fails_closed_when_unreadable():
    """A corrupt guard file must withhold EVERY tool, not
    none. The old fail-open returned set() on a read
    failure, so a tool the operator disabled ran anyway —
    and the API answered 200 {"disabled": []}, an
    authoritative all-clear derived from a read that never
    happened."""
    import skills

    state = Path(agent_server.STATE_DIR)
    guard = state / "tools_disabled.json"
    guard.write_text("{corrupt json", encoding="utf-8")
    try:
        assert skills._disabled_tools() is None
        # tool_payload withholds everything when the guard
        # state is unknown, and says so.
        assert skills.tool_payload(["web_search"]) is None
        # And the API reports the unknown state instead of
        # an empty list.
        with TestClient(app) as client:
            r = client.get("/api/config/tools")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["disabled"] is None
            assert body.get("error")
    finally:
        if guard.exists():
            guard.unlink()
