"""Request-validation regressions found by the dynamic route sweep.

Two gateway endpoints answered requests they should have rejected:

* ``POST /v1/memory/sweep`` ignored its body entirely, so an unknown drawer
  returned 200 plus a full per-drawer report and looked like a clean sweep.
* ``POST /v1/chat`` with an empty ``messages`` array reached the provider and
  billed a real LLM call, which then invented an answer to a question nobody
  asked.

``main.py`` probes a live gateway at import, so ``_has_real_turn`` is pulled
out by AST rather than imported -- the same approach as
``test_credential_skip_rule.py``.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))


# --------------------------------------------------------------------------
# POST /v1/chat - empty conversations must never reach a provider
# --------------------------------------------------------------------------

def _load_has_real_turn():
    src = (HERE / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_has_real_turn":
            ns: dict = {}
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         "main.py", "exec"), ns)
            return ns["_has_real_turn"]
    raise AssertionError("_has_real_turn not found in main.py")


@pytest.fixture(scope="module")
def has_real_turn():
    return _load_has_real_turn()


@pytest.mark.parametrize("messages, expected", [
    ([{"role": "user", "content": "hello"}], True),
    ([{"role": "user", "content": "   "}], False),
    ([{"role": "user", "content": ""}], False),
    ([{"role": "user", "content": None}], False),
    ([], False),
    (None, False),
    # A lone system turn is instruction, not something to answer.
    ([{"role": "system", "content": "be helpful"}], False),
    ([{"role": "user", "content": "hi"},
      {"role": "system", "content": "be brief"}], True),
    # Structured content blocks.
    ([{"role": "user", "content": [{"type": "text", "text": "hi"}]}], True),
    ([{"role": "user", "content": [{"type": "text", "text": "  "}]}], False),
    ([{"role": "user", "content": []}], False),
    ([{"role": "user", "content": [{"type": "image", "source": {}}]}], False),
])
def test_has_real_turn(has_real_turn, messages, expected):
    assert has_real_turn(messages) is expected, messages


def test_chat_refuses_empty_conversation():
    """The guard must be wired into /v1/chat, not merely defined."""
    src = (HERE / "main.py").read_text(encoding="utf-8")
    assert "_has_real_turn(messages)" in src
    tree = ast.parse(src)
    chat_fn = next(n for n in tree.body
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat")
    assert "_has_real_turn" in ast.unparse(chat_fn)


def test_chat_batch_inherits_the_guard():
    """chat_batch delegates per item to chat(), so it must inherit the 400."""
    src = (HERE / "main.py").read_text(encoding="utf-8")
    assert "await chat(call)" in src


# --------------------------------------------------------------------------
# POST /v1/memory/sweep - drawer scoping must be validated
# --------------------------------------------------------------------------

class _FakeSvc:
    def __init__(self):
        self.calls = 0

    def sweep_expired(self):
        self.calls += 1
        return 7

    def delete_one(self, memory_id):
        return None


class _FakePlane:
    def __init__(self, names=("fact", "working", "audit")):
        self.drawers = {n: _FakeSvc() for n in names}
        self.legacy = _FakeSvc()
        self.sweeped_all = False

    def sweep(self):
        self.sweeped_all = True
        return {n: s.sweep_expired() for n, s in self.drawers.items()}


@pytest.fixture()
def sweep_client(monkeypatch):
    """A minimal app holding only the memory router, with a fake plane."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import memory_api

    plane = _FakePlane()
    monkeypatch.setattr(memory_api, "_plane", lambda request: plane)
    app = FastAPI()
    app.include_router(memory_api.router)
    monkeypatch.setattr(memory_api, "_log_mem", lambda *a, **k: None)
    with TestClient(app) as c:
        yield c, plane


def test_sweep_all_drawers_still_works(sweep_client):
    client, plane = sweep_client
    r = client.post("/v1/memory/sweep")
    assert r.status_code == 200, r.text
    assert len(r.json()["swept"]) == 3
    assert plane.sweeped_all is True


def test_sweep_empty_body_sweeps_all(sweep_client):
    client, plane = sweep_client
    assert client.post("/v1/memory/sweep").json()["swept"].keys() == {
        "fact", "working", "audit"}
    assert plane.sweeped_all is True


@pytest.mark.parametrize("body, want", [
    ({"drawer": "working"}, ["working"]),
    ({"drawers": "fact"}, ["fact"]),
    ({"drawers": ["fact", "audit"]}, ["fact", "audit"]),
])
def test_sweep_known_drawer_is_scoped(sweep_client, body, want):
    client, plane = sweep_client
    r = client.post("/v1/memory/sweep", json=body)
    assert r.status_code == 200, r.text
    assert sorted(r.json()["swept"]) == sorted(want)
    assert plane.sweeped_all is False
    for name in want:
        assert plane.drawers[name].calls == 1
    untouched = set(plane.drawers) - set(want)
    for name in untouched:
        assert plane.drawers[name].calls == 0


@pytest.mark.parametrize("body", [
    {"drawer": "not_a_drawer"},
    {"drawers": "nope"},
    {"drawers": ["fact", "nope"]},
    {"drawers": []},
])
def test_sweep_unknown_drawer_is_400(sweep_client, body):
    client, plane = sweep_client
    r = client.post("/v1/memory/sweep", json=body)
    assert r.status_code == 400, f"{body} -> {r.status_code} {r.text}"
    # Critically: it must NOT have swept everything on the way to failing.
    assert plane.sweeped_all is False
    for svc in plane.drawers.values():
        assert svc.calls == 0


def test_sweep_malformed_json_is_400(sweep_client):
    client, plane = sweep_client
    r = client.post("/v1/memory/sweep", content=b"{bad",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert plane.sweeped_all is False


def test_sweep_non_object_body_is_400(sweep_client):
    client, _ = sweep_client
    r = client.post("/v1/memory/sweep", json=["fact"])
    assert r.status_code == 400


# --------------------------------------------------------------------------
# Deep-nesting poison must not brick a drawer
# --------------------------------------------------------------------------
# pydantic's serialiser raises "Circular reference detected (depth exceeded)"
# past ~100 levels. That ValueError was reported as embedding-dim drift, and
# because MemoryStore.append mutated the shared cached list before the save
# failed, one such record stayed in the cache forever -- every later read and
# write in that drawer failed until the process restarted.

def _deep(depth: int) -> dict:
    root: dict = {}
    cur = root
    for _ in range(depth):
        cur["n"] = {}
        cur = cur["n"]
    return root


def test_value_depth_counter():
    from memory_api import _value_depth, MAX_VALUE_DEPTH
    assert _value_depth({}) == 1
    assert _value_depth({"a": 1}) == 2
    assert _value_depth({"a": {"b": {"c": 1}}}) == 4
    assert _value_depth([1, 2, 3]) == 2
    assert _value_depth(_deep(5)) == 6
    # Bounded: it stops counting past the limit instead of walking forever.
    assert _value_depth(_deep(MAX_VALUE_DEPTH + 50)) > MAX_VALUE_DEPTH


def test_deeply_nested_remember_is_rejected_by_the_api(tmp_path):
    """The endpoint must refuse the poison payload before it reaches the store.

    Without this check the record lands, and because every later save in that
    drawer then raises while serialising it, the drawer becomes permanently
    unreadable and unwritable.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import memory_api
    from memory.service import MemoryService

    plane_svc = MemoryService(tmp_path / "fact", embed_fn=None)

    class _Plane:
        drawers = {"fact": plane_svc}
        legacy = None

    # Both are module globals and MUST be restored: leaving `_log_mem`
    # stubbed silently disabled the memory ledger for every later test in the
    # session (test_observability asserts on those rows).
    real_plane, real_log = memory_api._plane, memory_api._log_mem
    memory_api._plane = lambda request: _Plane()
    memory_api._log_mem = lambda *a, **k: None
    try:
        app = FastAPI()
        app.include_router(memory_api.router)
        with TestClient(app) as c:
            r = c.post("/v1/memory/remember",
                       json={"kind": "fact", "descriptor": "zz poison probe",
                             "value": _deep(400), "source": "test",
                             "run_id": "r1"})
            assert r.status_code == 400, r.text
            assert "deep" in r.json()["detail"].lower()
        # Nothing unusable was stored.
        assert plane_svc.store.load() == []
        # And the store still works for a normal write afterwards.
        import asyncio
        asyncio.new_event_loop().run_until_complete(
            plane_svc.remember(kind="fact", descriptor="fine", value={},
                               source="test", run_id="r2"))
        assert len(plane_svc.store.load()) == 1
    finally:
        memory_api._plane = real_plane
        memory_api._log_mem = real_log


def test_store_append_does_not_mutate_cache_on_failed_save(tmp_path):
    """A failed save must roll the cache back, not poison it.

    This is the root cause of the brick: append() mutated the list returned
    by _load_locked() -- which IS the cached list -- so a record that could not
    be serialised remained visible in memory and unremovable.
    """
    from memory.models import MemoryRecord
    from memory.store import MemoryStore

    store = MemoryStore(tmp_path / "memory.json")
    good = MemoryRecord(id="mem:good", kind="fact", descriptor="good",
                        value={}, source="t", run_id="r1")
    store.append(good)

    bad = MemoryRecord(id="mem:bad", kind="fact", descriptor="bad",
                       value=_deep(400), source="t", run_id="r1")
    with pytest.raises(ValueError):
        store.append(bad)

    # The bad record is not in memory and not on disk.
    ids = {i.id for i in store.load()}
    assert "mem:bad" not in ids, f"cache poisoned: {ids}"
    # The good record survived, and the store still accepts new writes --
    # i.e. the drawer is not wedged.
    store.append(MemoryRecord(id="mem:after", kind="fact", descriptor="after",
                              value={}, source="t", run_id="r2"))
    assert {i.id for i in store.load()} == {"mem:good", "mem:after"}


def test_store_save_failure_leaves_no_temp_file(tmp_path):
    from memory.models import MemoryRecord
    from memory.store import MemoryStore

    store = MemoryStore(tmp_path / "memory.json")
    store.append(MemoryRecord(id="mem:a", kind="fact", descriptor="a",
                              value={}, source="t", run_id="r1"))
    with pytest.raises(ValueError):
        store.append(MemoryRecord(id="mem:bad", kind="fact", descriptor="bad",
                                  value=_deep(400), source="t", run_id="r1"))
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name]
    assert not leftovers, f"temp files left behind: {leftovers}"


# --------------------------------------------------------------------------
# The unscoped wipe must be explicit
# --------------------------------------------------------------------------
# `DELETE /api/memory/` (trailing slash) drew a 307 that preserved the method
# and landed on the wipe handler, so a request meant to touch nothing erased
# every drawer and still answered 200.

def test_unscoped_wipe_requires_confirm():
    from memory_api import router
    fn = next(r.endpoint for r in router.routes
              if getattr(r, "path", "") == "/v1/memory"
              and "DELETE" in getattr(r, "methods", set()))
    assert "confirm" in fn.__code__.co_varnames[:fn.__code__.co_argcount], \
        "the wipe endpoint must accept an explicit confirm parameter"


def test_redirect_slashes_disabled_on_both_services():
    """A 307 preserves the HTTP method, so it can silently turn a narrow
    request into the destructive one."""
    import re
    from pathlib import Path
    root = Path(__file__).parent.parent
    gw = (root / "main.py").read_text(encoding="utf-8")
    assert "redirect_slashes=False" in gw, \
        "gateway must disable redirect_slashes (a 307 preserves DELETE)"
    agent_root = Path(r"C:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code")
    ag = (agent_root / "agent_server.py").read_text(encoding="utf-8")
    assert "redirect_slashes=False" in ag


def test_sweep_error_message_lists_valid_drawers(sweep_client):
    client, _ = sweep_client
    body = client.post("/v1/memory/sweep", json={"drawer": "nope"}).json()
    assert "not_a_drawer" in body["error"] or "nope" in body["error"]
    assert "fact" in body["error"]        # tells the caller what is valid


# --------------------------------------------------------------------------
# DELETE /v1/memory/{id} - per-item delete
# --------------------------------------------------------------------------
# The only removal path used to be DELETE /v1/memory, which wipes every
# drawer, so a single mis-captured preference could not be corrected.

def test_delete_route_exists():
    from memory_api import router
    paths = {r.path for r in router.routes if "DELETE" in getattr(r, "methods", set())}
    assert "/v1/memory/{memory_id}" in paths, sorted(paths)


def test_service_delete_one_removes_and_returns_record(tmp_path):
    from memory.models import MemoryRecord
    from memory.service import MemoryService

    def _rec(i, desc):
        return MemoryRecord(id=i, kind="fact", descriptor=desc,
                            value={}, source="test", run_id="r1")

    svc = MemoryService(tmp_path, embed_fn=None)
    svc.store.append(_rec("mem:keep", "keep me"))
    svc.store.append(_rec("mem:drop", "drop me"))
    rec = svc.delete_one("mem:drop")
    assert rec is not None and rec.id == "mem:drop"
    assert {i.id for i in svc.store.load()} == {"mem:keep"}


def test_service_delete_one_unknown_is_none(tmp_path):
    from memory.service import MemoryService
    svc = MemoryService(tmp_path, embed_fn=None)
    assert svc.delete_one("mem:nope") is None


def test_service_delete_one_rebuilds_index_when_vector_present(tmp_path, monkeypatch):
    """A deleted memory must stop coming back from search, so the vector
    index has to be rebuilt exactly as sweep_expired does."""
    import asyncio

    from memory.service import MemoryService

    async def _bow(*args, **kwargs):
        """The service awaits embed_fn(text, task_type=...), so this must be a
        coroutine function accepting anything."""
        import re
        import zlib
        text = args[0] if args else kwargs.get("text", "")
        dim = 16
        v = [0.0] * dim
        for w in re.findall(r"\w+", str(text or "").lower()):
            if len(w) > 2:
                v[zlib.crc32(w.encode()) % dim] += 1.0
        return v

    svc = MemoryService(tmp_path, embed_fn=_bow)
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        svc.remember(kind="fact", descriptor="the capital of France is Paris",
                     source="test", run_id="r1"))
    rebuilt = {"n": 0}
    real = svc._rebuild_locked

    def _counting():
        rebuilt["n"] += 1
        return real()

    monkeypatch.setattr(svc, "_rebuild_locked", _counting)
    target = svc.store.load()[0]
    assert target.embedding, "test needs an embedded record"
    svc.delete_one(target.id)
    assert rebuilt["n"] == 1, "index not rebuilt after deleting an embedded row"
    # And the record is really gone from the store.
    assert all(i.id != target.id for i in svc.store.load())


def test_delete_one_endpoint_404s_unknown_id(sweep_client):
    client, _ = sweep_client
    r = client.delete("/v1/memory/mem:nope")
    assert r.status_code == 404


def test_delete_one_endpoint_deletes_from_a_drawer(sweep_client):
    client, plane = sweep_client

    class _Rec:
        id = "mem:xyz"
        kind = "fact"

    for svc in plane.drawers.values():
        svc.delete_one = lambda mid: (_Rec() if mid == "mem:xyz" else None)
    plane.legacy.delete_one = lambda mid: None
    r = client.delete("/v1/memory/mem:xyz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "mem:xyz" and body["deleted"] == 1
    assert body["drawer"] in plane.drawers


def test_delete_one_falls_through_to_legacy(sweep_client):
    client, plane = sweep_client

    class _Rec:
        id = "mem:old"
        kind = "fact"

    for svc in plane.drawers.values():
        svc.delete_one = lambda mid: None
    plane.legacy.delete_one = lambda mid: (_Rec() if mid == "mem:old" else None)
    r = client.delete("/v1/memory/mem:old")
    assert r.status_code == 200
    assert r.json()["drawer"] == "legacy"


def test_wipe_route_is_separate_from_delete():
    """Guards the distinction: the path param route must not shadow the wipe."""
    from memory_api import router
    by_path = {}
    for r in router.routes:
        if "DELETE" in getattr(r, "methods", set()):
            by_path.setdefault(r.path, []).append(r)
    assert "/v1/memory" in by_path and "/v1/memory/{memory_id}" in by_path
    assert len(by_path["/v1/memory"]) == 1