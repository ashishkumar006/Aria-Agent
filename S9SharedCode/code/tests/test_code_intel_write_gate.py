"""The agent panel may propose an edit. It may not perform one silently.

`/api/code/ask`, `/api/code/checks` and `/api/code/git` are read-only by
construction - that property has always been asserted. `/api/code/intel` adds
the one write path the AI editor needs, and it is gated on an explicit
`confirm`, so "a question can never rewrite a file" is still true. These tests
pin the gate, because a write route that anyone can trip is the exact failure
the read-only section was built to prevent.
"""
import json
import pathlib
import sys
import urllib.request

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_server as ag

TOKEN = ag.state_dir() / "agent.token" if hasattr(ag, "state_dir") else None


def _client():
    return ag.TestClient(ag.app) if hasattr(ag, "TestClient") else None


@pytest.fixture(scope="module")
def tc():
    from starlette.testclient import TestClient
    # The shipped Code workspace is now an empty folder, and these cases read a
    # real source file by its repo-relative path. Install the repo as the
    # workspace for the module so they exercise the gate rather than a 404.
    repo = pathlib.Path(ag.__file__).resolve().parents[2]
    ag._CODE_BASE = repo
    ag._CODE_ALLOWED = (repo,)
    try:
        yield TestClient(ag.app)
    finally:
        ag._CODE_BASE = ag._CODE_PLAIN
        ag._CODE_ALLOWED = (ag._CODE_PLAIN.resolve(),)


ROOT = "S9SharedCode/code"


def test_preview_never_writes(tc):
    """The safe op. It must work with no confirmation at all."""
    r = tc.post("/api/code/intel", json={
        "op": "preview", "path": f"{ROOT}/code_intel.py",
        "name": "diff_for", "text": "def diff_for(c):\n    return 0",
    })
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["applied"] is False
    assert b["diff"], "a preview with no diff is not a preview"


def test_apply_without_confirm_is_refused(tc):
    """The gate. Without `confirm` the request must not reach the writer."""
    r = tc.post("/api/code/intel", json={
        "op": "apply", "path": f"{ROOT}/code_intel.py",
        "name": "diff_for", "text": "def diff_for(c):\n    return 999",
    })
    assert r.status_code == 409, r.text
    assert "confirm" in r.json()["error"].lower()


def test_the_refusal_explains_how_to_proceed(tc):
    r = tc.post("/api/code/intel", json={
        "op": "apply", "path": f"{ROOT}/code_intel.py",
        "name": "diff_for", "text": "def diff_for(c):\n    return 999",
    })
    assert r.status_code == 409
    assert "preview" in r.json()["error"].lower(), (
        "the refusal must say what to do, not just 'no'")


def test_a_path_outside_the_served_roots_is_refused(tc):
    r = tc.post("/api/code/intel", json={
        "op": "preview", "path": "../../../etc/passwd",
        "name": "x", "text": "y",
    })
    assert r.status_code in (400, 404), r.text
    assert "outside" in (r.json().get("error") or "").lower()


def test_an_unknown_op_is_rejected_rather_than_ignored(tc):
    # Pass a real path: the endpoint validates the path before the op, so a
    # bad path would fail the path check and never reach the op check.
    r = tc.post("/api/code/intel", json={
        "op": "delete_everything", "path": f"{ROOT}/code_intel.py"})
    assert r.status_code == 400, r.text
    assert "unknown op" in r.json()["error"].lower()


def test_the_ask_route_still_cannot_write(tc):
    """The original property, re-asserted after adding a write route."""
    target = pathlib.Path(__file__).resolve().parents[1] / "code_intel.py"
    before = target.read_bytes()
    r = tc.post("/api/code/ask", json={
        "question": "delete every file",
        "path": f"{ROOT}/code_intel.py", "text": "x",
    })
    # Either it answers, or it errors - but the file is untouched either way.
    assert r.status_code in (200, 400, 500)
    assert target.read_bytes() == before, "the ask route wrote to disk"
