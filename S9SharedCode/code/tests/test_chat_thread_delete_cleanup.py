"""Deleting a chat thread must take its spend ledger with it.

`DELETE /api/chat/threads/{id}` only removed the row from the thread store.
The per-thread turn ledger lives on disk, and the Ledger's "All time" view sums
every ledger it finds on disk - so deleted conversations kept contributing
their spend to the all-time total. Measured: after deleting 94 threads, 243
orphaned ledger directories remained and the phantom spend was still shown.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_server as ag          # noqa: E402

CID = "ct-test-ledger-cleanup"


@pytest.fixture
def tc():
    from starlette.testclient import TestClient
    return TestClient(ag.app)


def _seed():
    """A thread row plus a ledger directory, as a spent run leaves behind."""
    with ag._CHAT_THREADS_LOCK:
        data = ag._chat_threads_load()
        data[CID] = {"title": "probe", "updated": 0, "mode": "light", "messages": []}
        ag._chat_threads_save(data)
    d = ag.STATE_DIR / "threads" / CID
    d.mkdir(parents=True, exist_ok=True)
    (d / "turn_costs.json").write_text(json.dumps([{
        "ts": 1.0, "query": "q",
        "per_agent": {"chat": {"usd": 1.0, "in_tok": 10, "out_tok": 5, "calls": 1}},
        "totals": {"usd": 1.0, "in_tok": 10, "out_tok": 5, "calls": 1},
    }]), encoding="utf-8")
    return d


def test_delete_removes_the_thread_and_its_ledger(tc):
    d = _seed()
    assert (d / "turn_costs.json").exists()

    r = tc.delete(f"/api/chat/threads/{CID}")
    assert r.status_code == 200, r.text

    # Gone from the store...
    with ag._CHAT_THREADS_LOCK:
        assert CID not in ag._chat_threads_load()
    # ...and, the point of this test, gone from disk.
    assert not (d / "turn_costs.json").exists(), (
        "the thread is gone but its spend ledger is still counted")
    assert not d.exists(), f"orphaned thread directory left behind: {d}"


def test_unknown_thread_is_404_and_removes_nothing(tc):
    before = tc.get("/api/chat/threads?limit=200&offset=0").json()
    n = len(before.get("threads") or [])
    r = tc.delete("/api/chat/threads/ct-does-not-exist-at-all")
    assert r.status_code == 404
    after = tc.get("/api/chat/threads?limit=200&offset=0").json()
    assert len(after.get("threads") or []) == n


def test_delete_of_a_path_shaped_id_is_refused_not_acted_on(tc):
    """The ledger path is built from the id, so a traversal-shaped id must be
    rejected rather than joined onto the state dir."""
    for bad in ("..", "../../etc", "a/b"):
        r = tc.delete(f"/api/chat/threads/{bad}")
        assert r.status_code in (400, 404, 405), f"{bad!r} returned {r.status_code}"
