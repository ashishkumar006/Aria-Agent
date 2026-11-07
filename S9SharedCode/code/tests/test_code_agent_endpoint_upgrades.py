"""Endpoint-level proof for approval modes, read gate, plan and verification."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server  # noqa: E402
import mcp_runner  # noqa: E402
import studio_review  # noqa: E402
from agent_server import app  # noqa: E402


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(agent_server, "_CODE_BASE", tmp_path)
    monkeypatch.setattr(agent_server, "_CODE_ALLOWED", (root.resolve(),))
    # "" is the bucket of a caller that names no session — every route here.
    studio_review.clear("")
    yield root
    studio_review.clear("")


def _frames(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except ValueError:
                pass
    return out


def _post(client, root, **extra):
    with client.stream("POST", "/api/code/agent", json={
        "question": "do the thing",
        "path": "",
        "text": "",
        "max_hops": 5,
        **extra,
    }) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    return _frames(body)


def test_read_only_filters_tools_and_never_writes(workspace, monkeypatch):
    seen = {}

    async def fake_run(**kwargs):
        seen["allowed"] = set(kwargs.get("allowed_tools") or ())
        return {"text": "I can only read.", "tool_calls": [],
                "provider": "test", "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    with TestClient(app) as client:
        frames = _post(client, workspace, read_only=True)
    assert "edit_file" not in seen["allowed"]
    assert "run_command" not in seen["allowed"]
    assert "update_plan" in seen["allowed"]
    types = [f.get("type") for f in frames]
    assert "permission" not in types and "write" not in types
    done = next(f for f in frames if f.get("type") == "done")
    assert done["mode"] == "read-only"


def test_ask_mode_requires_a_read_before_editing(workspace, monkeypatch):
    decisions = []

    async def fake_run(**kwargs):
        approve = kwargs["approve_fn"]
        decisions.append(await approve("edit_file", {"path": "a.py"}))
        return {"text": "read it first", "tool_calls": [],
                "provider": "test", "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    with TestClient(app) as client:
        _post(client, workspace)
    assert decisions and isinstance(decisions[0], str)
    assert "read_file" in decisions[0]
    assert not (workspace / "a.py").exists()


def test_auto_edit_verifies_writes_and_retries_a_broken_file(workspace,
                                                            monkeypatch):
    calls = {"n": 0}

    async def fake_run(**kwargs):
        calls["n"] += 1
        approve = kwargs["approve_fn"]
        outcome = kwargs["on_outcome"]
        extra = kwargs["extra_dispatch"]
        await extra("update_plan", {"steps": [
            {"title": "Read a.py", "status": "done"},
            {"title": "Edit a.py", "status": "doing"},
            {"title": "Verify", "status": "pending"},
        ]})
        # Simulate the model reading the file first, then editing it.
        outcome("read_file", {"path": "a.py"}, True, "old", 0.01)
        decision = await approve("edit_file", {"path": "a.py"})
        assert decision is True, decision
        broken = calls["n"] == 1
        (workspace / "a.py").write_text(
            "def broken(:\n" if broken else "value = 1\n", encoding="utf-8")
        outcome("edit_file", {"path": "a.py"}, True, "written", 0.01)
        return {"text": f"attempt {calls['n']}", "tool_calls": [],
                "provider": "test", "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    with TestClient(app) as client:
        frames = _post(client, workspace, auto_approve=True)

    assert calls["n"] == 2
    plans = [f for f in frames if f.get("type") == "plan"]
    verifies = [f for f in frames if f.get("type") == "verify"]
    assert plans and plans[0]["steps"][1]["status"] == "doing"
    assert len(verifies) == 2
    assert verifies[0]["ok"] is False and verifies[0]["retry"] is True
    assert verifies[1]["ok"] is True and verifies[1]["retry"] is False
    done = next(f for f in frames if f.get("type") == "done")
    assert done["verification"]["ok"] is True
    assert (workspace / "a.py").read_text(encoding="utf-8") == "value = 1\n"


def test_auto_all_runs_a_command_without_parking(workspace, monkeypatch):
    """`auto_approve_exec` was parsed and honoured but never sent or tested.

    The console's "Auto" only covered writes, so every `run_command` parked on
    a permission frame and the user clicked Approve for each one — which is
    what ended a build-fix loop after two or three hops."""
    seen = {}

    async def fake_run(**kwargs):
        approve = kwargs["approve_fn"]
        seen["command"] = await approve("run_command",
                                        {"command": "pytest -q tests/test_x.py"})
        seen["checks"] = await approve("run_checks", {"path": "."})
        return {"text": "ran it", "tool_calls": [], "provider": "test",
                "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    with TestClient(app) as client:
        frames = _post(client, workspace, auto_approve=True,
                       auto_approve_exec=True)

    assert seen["command"] is True, seen
    assert seen["checks"] is True, seen
    parked = [f for f in frames if f.get("type") == "permission"]
    assert parked == [], f"auto-all still asked for: {[p['tool'] for p in parked]}"
    assert not agent_server._CODE_PERM, "a command approval was left parked"


def test_auto_edit_still_asks_before_running_a_command(workspace, monkeypatch):
    """The separate opt-in is the point: files auto, commands ask.

    Running the project's checks executes repository code, so `auto-edit` must
    not quietly become full auto."""
    seen = {}

    async def fake_run(**kwargs):
        approve = kwargs["approve_fn"]
        # Decline it the way the console would, so the run does not sit on the
        # 300s permission timeout.
        async def _resolve():
            import asyncio
            for _ in range(200):
                pid = next(iter(agent_server._CODE_PERM), None)
                if pid:
                    agent_server._code_perm_resolve(pid, False)
                    return True
                await asyncio.sleep(0.01)
            return False

        import asyncio
        got = asyncio.create_task(_resolve())
        seen["decision"] = await approve("run_command", {"command": "pytest -q"})
        seen["parked"] = await got
        return {"text": "skipped", "tool_calls": [], "provider": "test",
                "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    with TestClient(app) as client:
        _post(client, workspace, auto_approve=True)

    assert seen["parked"] is True, "no command approval was ever parked"
    assert seen["decision"] is False, "auto-edit ran a command without asking"


def _drain_perm(remember=False):
    """Resolve whatever is parked, so a test never waits on the 300s timeout."""
    import asyncio

    async def _wait_and_resolve(expect_parked=True):
        for _ in range(200):
            live = [pid for pid, e in agent_server._CODE_PERM.items()
                    if e.get("sig")]
            if live:
                agent_server._code_perm_resolve(live[0], True, remember)
                return live[0]
            await asyncio.sleep(0.01)
        return None
    return _wait_and_resolve


def _reset_perms():
    agent_server._CODE_PERM.clear()
    agent_server._CODE_APPROVED.clear()


def test_always_allow_stands_one_command_up_for_the_session(workspace,
                                                            monkeypatch):
    """The whole point of the button: ask once, then stop asking.

    The gate used to be per-call with no memory, so a build-fix loop parked the
    same `pytest -q` on every hop."""
    _reset_perms()
    seen = {}

    async def fake_run(**kwargs):
        import asyncio
        approve = kwargs["approve_fn"]
        resolve = _drain_perm(remember=True)

        first = asyncio.create_task(approve("run_command",
                                            {"command": "pytest -q tests/x"}))
        pid = await resolve()
        assert pid, "the first call never parked"
        seen["first"] = await first
        # Same command again: must not park at all.
        seen["second"] = await asyncio.wait_for(
            approve("run_command", {"command": "pytest -q tests/x"}), 5)
        # A different command must still ask.
        other = asyncio.create_task(approve("run_command",
                                            {"command": "npm run lint"}))
        seen["other_parked"] = await _drain_perm(remember=False)()
        seen["other"] = await other
        return {"text": "done", "tool_calls": [], "provider": "test",
                "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    with TestClient(app) as client:
        frames = _post(client, workspace, session="cs-endpoint-1")

    assert seen["first"] is True
    assert seen["second"] is True, "the stood-up command still asked"
    assert seen["other_parked"], "a different command was auto-approved"
    assert seen["other"] is True
    asks = [f for f in frames if f.get("type") == "permission"]
    assert len(asks) == 2, f"expected two prompts, got {len(asks)}"
    assert all(a.get("rememberable") is True for a in asks), [sorted(a.keys()) for a in asks]
    assert all(a.get("tool") == "run_command" for a in asks), asks
    _reset_perms()


def test_no_session_id_means_no_always_allow_offer(workspace, monkeypatch):
    """Without an owner for the approval, the button would be a global 'yes'."""
    _reset_perms()
    seen = {}

    async def fake_run(**kwargs):
        approve = kwargs["approve_fn"]
        t = asyncio_task = None
        import asyncio
        t = asyncio.create_task(approve("run_command", {"command": "make"}))
        pid = await _drain_perm(remember=True)()
        seen["parked"] = pid
        seen["decision"] = await t
        return {"text": "ok", "tool_calls": [], "provider": "test",
                "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    with TestClient(app) as client:
        frames = _post(client, workspace)   # no session

    asks = [f for f in frames if f.get("type") == "permission"]
    assert len(asks) == 1
    assert asks[0].get("rememberable") is not True
    assert agent_server._CODE_APPROVED == {},         "an ownerless approval was recorded"
    _reset_perms()
