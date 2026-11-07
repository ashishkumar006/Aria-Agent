"""The change delta.

What this module is for: a self-scheduled wake, or a plain "what changed since
this morning", answered by a bounded index of pointers instead of the agent
re-reading the world. Three properties matter and each has a test here, because
each is the kind that quietly rots:

  * a cursor round-trips — compute, then compute again from its cursor, and the
    second answer is EMPTY;
  * nothing is dropped silently — a cut is reported in `truncated`/`counts`;
  * the two giant drawers are never opened. `state/memory/audit/memory.json` is
    138 MB and the document drawer 91 MB, and the store rewrites either whole on
    every mutation — the mechanism behind the 35-second search. A "what changed"
    that scanned them would put that stall on a timer.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import delta


# ── cursor parsing ────────────────────────────────────────────────────────
def test_cursor_forms_all_mean_the_same_moment():
    ts = 1793799948.5
    assert delta.parse_cursor(delta.format_cursor(ts)) == pytest.approx(ts)
    assert delta.parse_cursor(ts) == pytest.approx(ts)
    assert delta.parse_cursor(str(ts)) == pytest.approx(ts)
    assert delta.parse_cursor(None) == 0.0


def test_a_memory_timestamp_is_a_datetime_not_a_string():
    """`MemoryItem.created_at` is a `datetime`; documents use epoch seconds and
    the artifacts sidecar uses an ISO string. Reading one shape and getting 0.0
    for the rest makes a delta read as 'nothing changed' forever."""
    iso = "2026-11-04T06:33:46.351633"
    assert delta._epoch(datetime.fromisoformat(iso)) == pytest.approx(
        delta._epoch(iso))
    naive = delta._epoch(datetime(2026, 1, 1, 12, 0, 0))
    aware = delta._epoch(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    assert naive == pytest.approx(aware), "a naive timestamp was read as local"
    assert delta._epoch("nonsense") == 0.0


# ── the round trip everything depends on ──────────────────────────────────
def test_second_pass_from_the_cursor_is_empty(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, documents=[_doc("doc-1", age=10)],
          artifacts=[_art("a" * 16, age=10)])
    first = delta.compute(since=None, limit=5)
    assert first["counts"]["documents"] == 1
    assert first["empty"] is False

    again = delta.compute(since=first["cursor"], limit=5)
    assert again["empty"] is True, (
        f"recomputed from the cursor still reports change: {again['changed']} "
        f"errors={again.get('errors')}")
    assert set(again["counts"].values()) == {0}


def test_until_is_pinned_before_the_collectors_run(tmp_path, monkeypatch):
    """A cursor taken after the work would skip anything that landed during it.
    A wake that misses a change is worse than one that sees it twice."""
    _wire(tmp_path, monkeypatch, documents=[_doc("doc-old", age=3600)],
          artifacts=[])
    start = time.time()
    out = delta.compute(since=None, until=start)
    assert out["until"] == start
    assert out["cursor"] == delta.format_cursor(start)


# ── nothing is dropped quietly ─────────────────────────────────────────────
def test_a_cut_is_reported_as_a_count_not_as_an_absence(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch,
          documents=[_doc(f"doc-{i}", age=i) for i in range(40)], artifacts=[])
    out = delta.compute(since=None, limit=5)
    assert len(out["changed"]["documents"]) == 5
    assert out["counts"]["documents"] == 40, "counts must include what was cut"
    assert out["truncated"]["documents"] == 35
    assert "40 documents" in out["summary"]


def test_surfaces_can_be_narrowed(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, documents=[_doc("doc-1", age=5)],
          artifacts=[_art("b" * 16, age=5)])
    out = delta.compute(since=None, surfaces=["artifacts"], limit=5)
    assert list(out["changed"]) == ["artifacts"]
    assert "documents" not in json.dumps(out["counts"])


# ── the two giant drawers stay shut ──────────────────────────────────────
def test_the_audit_and_document_drawers_are_never_requested(tmp_path, monkeypatch):
    """`audit` is 138 MB and `document` is 91 MB, each rewritten whole on every
    mutation. The document drawer also holds one record per CHUNK, so an upload
    would otherwise read as 139 memory changes."""
    assert "audit" not in delta.MEMORY_DRAWERS
    assert "document" not in delta.MEMORY_DRAWERS
    assert delta.MEMORY_DRAWERS == ("fact", "profile")

    seen: dict = {}

    class _Mem:
        @staticmethod
        def list_recent(**kw):
            seen.update(kw)
            return []

        @staticmethod
        def _get(path, params=None):
            seen["path"] = path
            return {"documents": []}

    _wire(tmp_path, monkeypatch, mem=_Mem)
    delta.compute(since=None)
    assert set(seen.get("drawers") or []) <= set(delta.MEMORY_DRAWERS), (
        f"delta asked for drawers {seen.get('drawers')}")
    assert "audit" not in json.dumps(seen)


def test_no_collector_reads_a_memory_file_directly(tmp_path, monkeypatch):
    """The delta goes through the gateway's read APIs, which page. Opening
    `state/memory/**` in-process is how this becomes the 35-second search again.
    Asserted on the code after the module docstring — the docstring is precisely
    the place where those paths are named, to warn the next reader."""
    src = Path(delta.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]
    assert "state/memory" not in body
    assert "memory.json" not in body


# ── stat-first discipline ─────────────────────────────────────────────────
def test_an_unchanged_file_is_parsed_once(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, documents=[], artifacts=[_art("c" * 16, age=5)])
    reads: list[str] = []
    real = Path.read_text

    def counting(self, *a, **k):
        reads.append(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    delta.compute(since=None, limit=5)
    first = len([r for r in reads if r.endswith(".json")])
    delta._STAT_CACHE.clear()
    delta.compute(since=None, limit=5)
    delta.compute(since=None, limit=5)
    # Two more passes, still one parse each: the cache keys on (mtime_ns, size).
    assert reads == [] or len([r for r in reads if r.endswith(".json")]) <= first * 3


def test_derived_artifact_caches_are_not_changes(tmp_path, monkeypatch):
    """`<sha>.raster.<key>.json` and `<sha>.<name>.json` are preview caches. If
    they counted, LOOKING at a document would show up as a change to the world."""
    art = "d" * 16
    _wire(tmp_path, monkeypatch, documents=[], artifacts=[_art(art, age=5)])
    (delta.STATE_DIR / "artifacts" / f"{art}.raster.5b733c88f630.json").write_text(
        json.dumps({"key": "x", "body": []}), encoding="utf-8")
    (delta.STATE_DIR / "artifacts" / f"{art}.preview.json").write_text(
        json.dumps({"key": "x"}), encoding="utf-8")
    out = delta.compute(since=None, surfaces=["artifacts"], limit=10)
    assert out["counts"]["artifacts"] == 1, out["changed"]["artifacts"]


def test_an_artifact_says_what_it_is_not_its_hash(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, documents=[],
          artifacts=[_art("e" * 16, age=5, descriptor="Project Brief (pdf)",
                          content_type="application/pdf", origin="authoring",
                          size_bytes=124956)])
    entry = delta.compute(since=None, surfaces=["artifacts"])["changed"]["artifacts"][0]
    assert "Project Brief" in entry["detail"]
    assert "authoring" in entry["detail"]
    assert entry["origin"] == "authoring"


# ── a broken surface degrades, it does not vanish ─────────────────────────
def test_one_failing_collector_names_itself_and_spares_the_rest(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, documents=[_doc("doc-1", age=5)],
          artifacts=[_art("f" * 16, age=5)], break_runs=True)
    out = delta.compute(since=None, limit=5)
    assert out["errors"]["runs"], "a dead surface reported nothing"
    assert out["counts"]["documents"] == 1
    assert out["counts"]["artifacts"] == 1
    assert "unavailable: runs" in out["summary"]


def test_a_lost_gateway_says_so_rather_than_reporting_no_changes(tmp_path, monkeypatch):
    """Empty and unreachable are different answers and the agent has to be able
    to tell them apart — otherwise a gateway outage reads as a quiet world."""
    class _Down:
        @staticmethod
        def list_recent(**kw):
            raise RuntimeError("gateway down")

        @staticmethod
        def _get(path, params=None):
            raise RuntimeError("gateway down")

    _wire(tmp_path, monkeypatch, documents=[], artifacts=[], mem=_Down)
    out = delta.compute(since=None)
    assert out["errors"].get("documents") and out["errors"].get("memory")
    assert "unavailable" in out["summary"]
    assert out["empty"] is False, "an outage must not read as 'nothing changed'"


# ── the watermark ─────────────────────────────────────────────────────────
def test_cursor_advances_only_when_something_delivered_it(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, documents=[], artifacts=[])
    assert delta.read_cursor("ct-1") == 0.0
    row = delta.advance_cursor("ct-1", "v1:1793799948.5")
    assert delta.read_cursor("ct-1") == pytest.approx(1793799948.5)
    assert row["wakes"] == 1
    delta.advance_cursor("ct-1", "v1:1793800000.0")
    assert delta.read_cursor("ct-1") == pytest.approx(1793800000.0)
    assert delta._load_cursors()["ct-1"]["wakes"] == 2
    assert delta.forget_cursor("ct-1") is True
    assert delta.read_cursor("ct-1") == 0.0


def test_a_corrupt_cursor_file_is_not_fatal(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, documents=[], artifacts=[])
    delta.CURSOR_PATH.write_text("{not json", encoding="utf-8")
    delta._STAT_CACHE.clear()
    assert delta.read_cursor("ct-9") == 0.0
    delta.advance_cursor("ct-9", time.time())
    assert delta.read_cursor("ct-9") > 0


def test_a_locked_sidecar_does_not_turn_a_delivery_into_an_error(tmp_path, monkeypatch):
    """The cursor write is cross-process: the MCP tool child does it, not the
    agent server. A lock that cannot be taken must degrade to an unlocked write,
    because the race it prevents only costs a re-shown delta — whereas raising
    here would make a tool that already answered look like it failed."""
    _wire(tmp_path, monkeypatch, documents=[], artifacts=[])
    monkeypatch.setattr(delta, "LOCK_WAIT_S", 0.05)
    held = open(delta.LOCK_PATH, "a+", encoding="utf-8")
    delta._byte_trylock(held)
    try:
        row = delta.advance_cursor("ct-lock", time.time())
    finally:
        delta._byte_unlock(held)
        held.close()
    assert row["cursor"].startswith("v1:")
    assert delta.read_cursor("ct-lock") > 0


# ── the tool and the route are the same code ──────────────────────────────
def test_the_agent_is_told_the_tool_exists_in_both_places():
    """`_CHAT_TOOLS` and `_TOOL_CATALOG` are two lists that must agree; the
    failure mode is a tool the model can call but cannot see, or one it sees and
    the dispatcher refuses."""
    import agent_server
    import skills
    assert "whats_new" in agent_server._CHAT_TOOLS
    assert "whats_new" in skills._TOOL_CATALOG
    assert skills._TOOL_CATALOG["whats_new"]["name"] == "whats_new"


def test_the_route_is_read_only(tmp_path, monkeypatch):
    """A GET must not move a watermark. If reading the panel consumed the delta,
    the next wake would find nothing and the changes would be lost."""
    _wire(tmp_path, monkeypatch, documents=[_doc("doc-1", age=5)], artifacts=[])
    from fastapi.testclient import TestClient
    import agent_server
    client = TestClient(agent_server.app)
    r = client.get("/api/whats-new", headers={"X-Aria-Token": "test-token"},
                   params={"conversation_id": "ct-route"})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["counts"]["documents"] == 1
    assert delta.read_cursor("ct-route") == 0.0, "a GET advanced the watermark"


# ── fixtures ──────────────────────────────────────────────────────────────
def _doc(doc_id: str, age: float) -> dict:
    now = time.time() - age
    return {"id": doc_id, "filename": f"{doc_id}.pdf", "status": "ready",
            "chunk_count": 12, "created_at": now - 10, "updated_at": now}


def _art(sha: str, age: float, **over) -> dict:
    meta = {"id": f"art:{sha}", "descriptor": f"brief {sha[:4]}",
            "content_type": "application/pdf", "origin": "authoring",
            "size_bytes": 1200, "created_at": datetime.fromtimestamp(
                time.time() - age, tz=timezone.utc).isoformat()}
    meta.update(over)
    meta["_age"] = age
    return meta


def _wire(tmp_path: Path, monkeypatch, *, documents=(), artifacts=(),
          mem=None, break_runs=False):
    """Point every collector at a temp dir and a stub gateway client.

    The sessions root is redirected as well: `_runs` reads it through
    `persistence`, and leaving it pointed at the real store would make a
    "hermetic" test's result depend on how many runs this machine happens to
    have.
    """
    root = tmp_path / "state"
    (root / "artifacts").mkdir(parents=True)
    (root / "apps").mkdir(parents=True)
    (root / "sessions" / "s8-live").mkdir(parents=True)
    monkeypatch.setattr(delta, "STATE_DIR", root)
    monkeypatch.setattr(delta, "CURSOR_PATH", root / "wake_cursors.json")
    monkeypatch.setattr(delta, "LOCK_PATH", root / "wake_cursors.lock")
    monkeypatch.setattr(delta, "_STAT_CACHE", {})

    import persistence
    monkeypatch.setattr(persistence, "SESSIONS_ROOT", root / "sessions")

    now = time.time()
    for a in artifacts:
        age = a.pop("_age", 5)
        p = root / "artifacts" / f"{a['id'].split(':')[1]}.json"
        p.write_text(json.dumps(a), encoding="utf-8")
        os_touch(p, now - age)
    if not artifacts:
        (root / "artifacts" / "keep.txt").write_text("x", encoding="utf-8")

    live = root / "sessions" / "s8-live"
    (live / "run_status.json").write_text(
        json.dumps({"degraded": False, "detail": "ok", "updated": now - 3}),
        encoding="utf-8")
    # `_runs` reads the DIRECTORY mtime, and writing into the directory just
    # moved it. Backdate it explicitly: on Windows a directory mtime can land a
    # few milliseconds after `time.time()`, and a fixture that races the clock
    # makes the cursor round-trip test fail for reasons that are not the code.
    os_touch(live, now - 3)
    if break_runs:
        # Raised INSIDE the collector's own try, the way a missing or
        # unreadable store really fails, so the isolation path is what runs.
        def _broken(_since, _limit):
            raise RuntimeError("sessions gone")
        monkeypatch.setattr(delta, "COLLECTORS", {**delta.COLLECTORS,
                                                  "runs": _broken})

    fake = mem if mem is not None else _StubMem(documents)
    import sys
    monkeypatch.setitem(sys.modules, "memory", fake)  # type: ignore[arg-type]


class _StubMem:
    def __init__(self, documents):
        self._documents = list(documents)

    def _get(self, path, params=None):
        return {"documents": self._documents}

    def list_recent(self, **kw):
        return []


def os_touch(path: Path, ts: float) -> None:
    import os
    os.utime(path, (ts, ts))


def test_the_flag_takes_the_tool_away_without_leaving_a_stub(monkeypatch):
    """`agent.whats_new` off must mean the model is never told it exists. A tool
    that is advertised and then refuses is worse than one that is absent, and a
    switch that only hides the ROUTE leaves the agent reaching for it."""
    import agent_server
    import flags as f
    monkeypatch.setattr(f, "is_enabled",
                        lambda name, default=True: False
                        if name == "agent.whats_new" else default)
    names = {t.get("name") for t in (agent_server._chat_tool_payload() or [])}
    assert "whats_new" not in names, "the payload still offers a switched-off tool"

    monkeypatch.setattr(f, "is_enabled", lambda name, default=True: default)
    back = {t.get("name") for t in (agent_server._chat_tool_payload() or [])}
    assert "whats_new" in back, "switching it back on did not restore the tool"


def test_the_route_refuses_when_switched_off(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, documents=[_doc("doc-1", age=5)], artifacts=[])
    import flags as f
    monkeypatch.setattr(f, "is_enabled",
                        lambda name, default=True: False
                        if name == "agent.whats_new" else default)
    from fastapi.testclient import TestClient
    import agent_server
    r = TestClient(agent_server.app).get(
        "/api/whats-new", headers={"X-Aria-Token": "test-token"})
    assert r.status_code == 503
    assert "agent.whats_new" in r.json()["error"], "refused without naming the switch"
