"""Standing command approvals: decided once, honoured for the session, no further.

The gate is per-call by design, which is what made a build-fix loop ask for
`npm run build` again on every hop. "Always allow" answers that, but only if a
standing approval is bound to the session that granted it and to the exact
command line — otherwise one Yes becomes a credential every later run can spend.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server as srv


@pytest.fixture(autouse=True)
def _clean():
    srv._CODE_PERM.clear()
    srv._CODE_APPROVED.clear()
    yield
    srv._CODE_PERM.clear()
    srv._CODE_APPROVED.clear()


def test_the_signature_is_the_tool_and_the_exact_command_line():
    a = srv._code_sig("run_command", {"command": "npm run build"})
    b = srv._code_sig("run_command", {"command": "npm run build "})
    assert a == b, "trailing whitespace must not re-ask"
    assert a != srv._code_sig("run_command", {"command": "npm test"})
    assert srv._code_sig("run_checks", {"path": "sub"}) != \
        srv._code_sig("run_checks", {"path": "."})


def test_one_target_spelled_four_ways_is_one_standing_approval():
    """Live 2026-11-02: the agent asked for `run_checks` twice in one turn
    after "Always allow", because the model wrote the path differently on the
    second hop. The approval is about the target, not the spelling."""
    root = srv._code_agent_root()
    name = root.name
    same = srv._code_sig("run_checks", {"path": ""})
    for spelling in (".", name, f"{name}/", str(root), f"{root}\\"):
        assert srv._code_sig("run_checks", {"path": spelling}) == same, spelling
    # A real subdirectory is still a different target.
    assert srv._code_sig("run_checks", {"path": "tests"}) != same


def test_a_standing_approval_is_scoped_to_the_session_that_gave_it():
    sig = srv._code_sig("run_command", {"command": "pytest -q"})
    srv._code_approved_add("cs-one", sig)
    assert srv._code_approved_has("cs-one", sig)
    assert not srv._code_approved_has("cs-two", sig), \
        "a standing approval leaked across sessions"


def test_an_empty_session_records_nothing():
    """No session id means no owner, and an ownerless approval is a global one."""
    srv._code_approved_add("", "run_command\x1fls")
    assert not srv._code_approved_has("", "run_command\x1fls")
    assert srv._CODE_APPROVED == {}


@pytest.mark.asyncio
async def test_resolve_with_remember_stands_the_command_up():
    entry = srv._code_perm_put("pe-1", sig="run_command\x1fmake",
                               session="cs-a")
    assert srv._code_perm_resolve("pe-1", True, True) is True
    assert entry["decision"] is True
    assert srv._code_approved_has("cs-a", "run_command\x1fmake")


@pytest.mark.asyncio
async def test_declining_never_stands_anything_up():
    srv._code_perm_put("pe-2", sig="run_command\x1frm -rf build",
                       session="cs-b")
    srv._code_perm_resolve("pe-2", False, True)
    assert not srv._code_approved_has("cs-b", "run_command\x1frm -rf build"), \
        "a refusal cannot be a standing approval"


@pytest.mark.asyncio
async def test_remember_without_a_signature_is_inert():
    """A write frame has no command signature; remembering it would mean
    'allow any future write', which is not what the button says."""
    srv._code_perm_put("pe-3")
    srv._code_perm_resolve("pe-3", True, True)
    assert srv._CODE_APPROVED == {}


def test_the_store_is_bounded():
    for i in range(srv._CODE_APPROVED_SESSIONS + 40):
        srv._code_approved_add(f"cs-{i}", f"run_command\x1fcmd{i}")
    assert len(srv._CODE_APPROVED) <= srv._CODE_APPROVED_SESSIONS + 1


def test_per_session_command_cap_does_not_grow_without_end():
    for i in range(srv._CODE_APPROVED_MAX + 25):
        srv._code_approved_add("cs-x", f"run_command\x1fcmd{i}")
    assert len(srv._CODE_APPROVED["cs-x"]) <= srv._CODE_APPROVED_MAX


def test_route_rejects_a_non_boolean_remember():
    """`remember` is opt-in and must not accept a truthy string as consent."""
    import asyncio
    from fastapi.testclient import TestClient
    from agent_server import app
    srv._code_perm_put("pe-4", sig="run_command\x1fls", session="cs-c")
    with TestClient(app) as client:
        r = client.post("/api/code/permission/pe-4",
                        json={"approve": True, "remember": "yes"})
        assert r.status_code == 200, r.text
    # a string is not True, so nothing was stood up
    assert not srv._code_approved_has("cs-c", "run_command\x1fls")
