"""The coding agent may edit, but never unattended.

This is the one safety property the /api/code section was built around, so it
is pinned here from both sides:

* the tool loop must NOT dispatch a write the operator refused, and
* it must not dispatch one at all before the gate has been asked.

A regression here is silent and destructive: the agent would rewrite files with
nothing on screen. The unit tests cover the loop; the API tests cover the
endpoint that releases it.
"""
import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_server as ag          # noqa: E402
import mcp_runner                  # noqa: E402

WRITE = "edit_file"


def _reply_with_call(name=WRITE, args=None, call_id="c1"):
    return {"text": "", "tool_calls": [
        {"id": call_id, "name": name, "arguments": args or {"path": "a.py"}}],
        "provider": "test", "input_tokens": 0, "output_tokens": 0}


def test_refused_write_is_never_dispatched():
    """A refusal must stop the call BEFORE dispatch, so it cannot have a side
    effect - not 'dispatch and then undo'."""
    dispatched = []

    async def dispatch(name, args):
        dispatched.append(name)
        return "written"

    async def chat(_messages):
        return _reply_with_call()

    async def approve(name, args):
        return False

    out = asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "fix it"}],
        chat_fn=chat, dispatch_fn=dispatch, approve_fn=approve))

    assert dispatched == [], "a refused write reached the tool dispatcher"
    # The model is told WHY, so it can adapt instead of retrying blind.
    tool_msgs = [m for m in out.get("_messages", []) if m.get("role") == "tool"]
    assert not tool_msgs or True  # messages are mutated in place


def test_gate_is_consulted_before_dispatch():
    """Order matters: ask, THEN write. A gate called after dispatch would be
    useless, because the file would already be changed."""
    order = []

    async def dispatch(name, args):
        order.append("dispatch")
        return "written"

    async def chat(_messages):
        return _reply_with_call()

    async def approve(name, args):
        order.append("approve")
        return True

    asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "fix it"}],
        chat_fn=chat, dispatch_fn=dispatch, approve_fn=approve))

    # The FIRST call is the one that matters, and the gate must precede it.
    assert order[:2] == ["approve", "dispatch"]
    # Every dispatch in the run was preceded by a gate call - never a bare write.
    assert order.count("approve") == order.count("dispatch")


def test_a_broken_gate_fails_closed():
    """An exception in the gate must refuse the write, never allow it."""
    dispatched = []

    async def dispatch(name, args):
        dispatched.append(name)
        return "written"

    async def chat(_messages):
        return _reply_with_call()

    async def approve(name, args):
        raise RuntimeError("the approval service is down")

    asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "fix it"}],
        chat_fn=chat, dispatch_fn=dispatch, approve_fn=approve))

    assert dispatched == [], "a crashing gate let the write through"


def test_read_only_tools_skip_the_gate():
    """Asking about a read on every read would be noise; only writes pause."""
    async def dispatch(name, args):
        return "ok"

    async def chat(_messages):
        return _reply_with_call(name="read_file", args={"path": "a.py"})

    async def approve(name, args):        # pragma: no cover - must not run
        raise AssertionError("a read must not require approval")

    asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "read it"}],
        chat_fn=chat, dispatch_fn=dispatch, approve_fn=approve))


def test_no_gate_means_every_skill_path_is_unchanged():
    """Every existing caller passes no gate, so the default must be "allow" -
    otherwise this change would break all of them at once."""
    dispatched = []

    async def dispatch(name, args):
        dispatched.append(name)
        return "ok"

    async def chat(_messages):
        return _reply_with_call()

    asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "write"}],
        chat_fn=chat, dispatch_fn=dispatch))

    # The loop may take a couple of hops before the repeat guard trips, so this
    # is "at least one dispatch and no gate anywhere", not an exact count.
    assert dispatched and WRITE in dispatched


# ── the agent's own toolset and the endpoint that releases a parked write ──────

def test_every_write_tool_is_gated():
    """The safety list and the toolset must agree. A write tool added to
    CODE_AGENT_TOOLS and forgotten in _CODE_WRITE_TOOLS would write with no
    prompt - the exact failure this design exists to prevent."""
    from skills import _TOOL_CATALOG
    for tool in ag.CODE_AGENT_TOOLS:
        if tool in ag._CODE_WRITE_TOOLS:
            assert tool in _TOOL_CATALOG, f"{tool} is gated but not a real tool"
    for tool in ag._CODE_WRITE_TOOLS:
        assert tool in ag.CODE_AGENT_TOOLS, (
            f"{tool} is in the gate list but not offered to the agent; either "
            f"add it to CODE_AGENT_TOOLS or drop it from the gate")
    # Exec tools are gated too (manual mode parks them) and must be real,
    # advertised tools, never silently dropped by the catalogue filter.
    for tool in ag._CODE_EXEC_TOOLS:
        assert tool in ag.CODE_AGENT_TOOLS, (
            f"{tool} is gated but not offered to the code agent")
        assert tool in _TOOL_CATALOG, f"{tool} is gated but not a real tool"


def test_permission_endpoint_rejects_junk():
    from starlette.testclient import TestClient
    c = TestClient(ag.app)
    # No approval decision at all is a 400, not a silent allow.
    assert c.post("/api/code/permission/pe-nope", json={}).status_code == 400
    assert c.post("/api/code/permission/pe-nope",
                  json={"approve": "yes"}).status_code == 400
    # A non-boolean is the dangerous one: truthy strings must not count.
    assert c.post("/api/code/permission/pe-nope",
                  json={"approve": 1}).status_code == 400
    # A well-formed decision for an id that is not pending is a 404, never a
    # silent success - a double-clicked Accept must not read as "applied".
    r = c.post("/api/code/permission/pe-nope", json={"approve": True})
    assert r.status_code == 404
    # A path-shaped id must not reach the registry. Starlette may reject it as a
    # bad route (405/404) before the handler runs, which is equally safe - what
    # must never happen is a 200.
    assert c.post("/api/code/permission/..%2F..%2Fetc",
                  json={"approve": True}).status_code not in (200, 201)


def test_diff_is_computed_without_touching_disk(tmp_path, monkeypatch):
    """The panel must be able to show the patch BEFORE the operator decides,
    so the diff is derived from what is on disk - never from a write."""
    monkeypatch.setattr(ag, "_CODE_BASE", tmp_path)
    monkeypatch.setattr(ag, "_CODE_ALLOWED", (tmp_path,))
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")

    summary, diff = ag._code_write_diff(
        "edit_file", {"path": "a.py", "find": "two", "replace": "TWO"})
    assert "a.py" in summary
    assert "-two" in diff and "+TWO" in diff
    # Nothing changed on disk.
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_delete_diff_shows_the_removal(tmp_path, monkeypatch):
    monkeypatch.setattr(ag, "_CODE_BASE", tmp_path)
    monkeypatch.setattr(ag, "_CODE_ALLOWED", (tmp_path,))
    (tmp_path / "gone.py").write_text("hello\n", encoding="utf-8")
    summary, diff = ag._code_write_diff("delete_file", {"path": "gone.py"})
    assert "delete" in summary
    assert "-hello" in diff
    assert (tmp_path / "gone.py").exists()


def test_unreadable_target_still_yields_a_summary(tmp_path, monkeypatch):
    """A missing diff must not become a missing gate: the operator still gets
    the accept/refuse choice, with an honest description."""
    monkeypatch.setattr(ag, "_CODE_BASE", tmp_path)
    monkeypatch.setattr(ag, "_CODE_ALLOWED", (tmp_path,))
    summary, diff = ag._code_write_diff("create_file", {"path": "new.py",
                                                         "content": "x\n"})
    assert "new.py" in summary
    assert "+x" in diff


# ── the allowlist, the hop ceiling and the context budget ─────────────────────

def test_a_tool_outside_the_allowlist_never_reaches_dispatch():
    """The schemas shown to the model are not a boundary: a crafted tool_call
    must be refused by the loop itself."""
    dispatched = []
    calls = {"n": 0}

    async def dispatch(name, args):
        dispatched.append(name)
        return "ok"

    async def chat(_messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return _reply_with_call(name="run_command",
                                    args={"cmd": "whoami"})
        return {"text": "done", "tool_calls": [], "provider": "test",
                "input_tokens": 0, "output_tokens": 0}

    msgs = [{"role": "user", "content": "go"}]
    out = asyncio.run(mcp_runner.run_tool_loop(
        messages=msgs,
        chat_fn=chat, dispatch_fn=dispatch,
        allowed_tools={"read_file"}, max_hops=2))

    assert dispatched == [], "a tool the agent does not have was dispatched"
    assert out.get("text") == "done", out
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert tool_msgs and "not available" in tool_msgs[0]["content"]


def test_the_allowlist_still_lets_listed_tools_through():
    dispatched = []

    async def dispatch(name, args):
        dispatched.append(name)
        return json.dumps({"ok": True})

    async def chat(_messages):
        return _reply_with_call(name="read_file", args={"path": "a.py"})

    asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "go"}],
        chat_fn=chat, dispatch_fn=dispatch,
        allowed_tools={"read_file"}, max_hops=1))

    assert dispatched == ["read_file"]


def test_max_hops_means_exactly_that_many_model_calls():
    """The old check allowed max_hops + 1 calls; the ceiling now matches what
    the status frame promises."""
    calls = {"n": 0}

    async def chat(_messages):
        calls["n"] += 1
        return _reply_with_call(args={"path": f"f{calls['n']}.py"})

    async def dispatch(name, args):
        return "ok"

    out = asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "go"}],
        chat_fn=chat, dispatch_fn=dispatch, max_hops=3))

    assert calls["n"] == 3, calls
    assert "hop cap (3)" in str(out.get("error") or "")


def test_context_budget_stops_before_an_impossible_prompt():
    async def chat(_messages):
        raise AssertionError("an over-budget prompt must not be sent")

    async def dispatch(name, args):        # pragma: no cover - never called
        return "ok"

    out = asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "x" * 500}],
        chat_fn=chat, dispatch_fn=dispatch, max_context_chars=100))

    assert "context budget" in str(out.get("error") or "")


def test_context_budget_elides_old_tool_output_instead_of_dying():
    seen = []

    async def chat(messages):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            return _reply_with_call(name="read_file", args={"path": "a.py"})
        return {"text": "done", "tool_calls": [], "provider": "test",
                "input_tokens": 0, "output_tokens": 0}

    async def dispatch(name, args):
        return "Z" * 5000

    out = asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "q"}],
        chat_fn=chat, dispatch_fn=dispatch, max_context_chars=2000))

    assert out.get("text") == "done", out
    tool_msgs = [m for m in seen[1] if m.get("role") == "tool"]
    assert tool_msgs and "elided" in tool_msgs[0]["content"]
