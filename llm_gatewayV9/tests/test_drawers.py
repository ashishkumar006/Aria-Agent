"""Phase 1 drawer tests: routing, permissions, legacy compat, merge order.

New writes land in per-drawer stores under ``state/memory/<drawer>/``;
pre-drawer items stay byte-identical in the legacy ``state/memory.json``
(frozen read-mostly) and validate with ``drawer=None``.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))

from memory.plane import MemoryPlane  # noqa: E402
from memory.service import check_writer  # noqa: E402


# ── deterministic fake embedder ────────────────────────────────────────────

def _bow(text: str, dim: int = 64) -> list[float]:
    vec = [0.0] * dim
    for w in re.findall(r"\w+", text.lower()):
        if len(w) > 2:
            vec[zlib.crc32(w.encode()) % dim] += 1.0
    return vec


async def _fake_embed(text: str, task_type: str) -> list[float]:
    return _bow(text)


@pytest.fixture()
def plane(tmp_path):
    return MemoryPlane(tmp_path, embed_fn=_fake_embed)


def _legacy_seed(tmp_path, descriptors):
    """Write pre-drawer style items (no drawer field) to the legacy store."""
    items = [{
        "id": f"mem:legacy{i:04d}", "kind": "fact",
        "keywords": sorted(set(re.findall(r"\w+", d.lower()))),
        "descriptor": d, "value": {"raw": d},
        "source": "old", "run_id": "old",
    } for i, d in enumerate(descriptors)]
    (tmp_path / "memory.json").write_text(json.dumps(items, indent=2))


# ── write routing ──────────────────────────────────────────────────────────

class TestRouting:
    async def test_fact_lands_in_fact_drawer(self, plane, tmp_path):
        item = await plane.remember(kind="fact", descriptor="Mom birthday May",
                                    source="t", run_id="r1")
        assert item.drawer == "fact"
        assert (tmp_path / "memory" / "fact" / "memory.json").exists()

    async def test_preference_routes_to_fact(self, plane):
        item = await plane.remember(kind="preference", descriptor="likes tea",
                                    source="t", run_id="r1")
        assert item.drawer == "fact"

    async def test_scratchpad_routes_to_working(self, plane, tmp_path):
        item = await plane.remember(kind="scratchpad", descriptor="temp note",
                                    source="t", run_id="r1")
        assert item.drawer == "working"
        assert item.embedding is None  # scratchpad skips the vector path
        assert (tmp_path / "memory" / "working" / "memory.json").exists()

    async def test_record_outcome_routes_to_episode(self, plane, tmp_path):
        item = await plane.record_outcome(
            tool="get_time", arguments={}, result_text="12:00",
            artifact_id=None, run_id="r1", goal_id=None)
        assert item.drawer == "episode"
        assert item.kind == "tool_outcome"
        assert (tmp_path / "memory" / "episode" / "memory.json").exists()

    async def test_chunk_payload_routes_to_document(self, plane, tmp_path):
        item = await plane.remember(
            kind="fact", descriptor="[doc chunk 1/2] authentication flow",
            value={"chunk": "authentication flow body"},
            source="index", run_id="r1", principal_role="indexer")
        assert item.drawer == "document"
        assert (tmp_path / "memory" / "document" / "memory.json").exists()

    async def test_record_carries_scope_and_principal(self, plane):
        item = await plane.remember(
            kind="fact", descriptor="scoped fact", source="t", run_id="r1",
            principal_role="agent", principal_id="assistant")
        assert item.scope.tenant_id == "course"
        assert item.scope.project_id == "s9"
        assert item.principal.role == "agent"
        assert item.principal.id == "assistant"


# ── permissions (fail-closed) ──────────────────────────────────────────────

class TestPermissions:
    def test_matrix(self):
        check_writer("fact", "agent")
        check_writer("fact", "indexer")
        check_writer("episode", "runtime")
        check_writer("working", "runtime")
        check_writer("document", "indexer")
        check_writer("policy", "operator")
        check_writer("audit", "gateway")
        for drawer, role in [("fact", "runtime"), ("policy", "agent"),
                             ("policy", "runtime"), ("audit", "agent"),
                             ("audit", "operator"), ("playbook", "agent"),
                             ("document", "runtime"), ("episode", "indexer")]:
            with pytest.raises(PermissionError):
                check_writer(drawer, role)

    def test_unknown_drawer_raises_value_error(self):
        with pytest.raises(ValueError):
            check_writer("nope", "agent")

    async def test_denied_write_is_audited(self, plane):
        with pytest.raises(PermissionError):
            await plane.remember(kind="fact", descriptor="sneaky fact",
                                 source="t", run_id="r1",
                                 principal_role="runtime")
        audit = plane.drawers["audit"].store.load()
        assert any("write denied" in a.descriptor and "runtime" in a.descriptor
                   for a in audit)

    async def test_playbook_unwritable_via_agent_path(self, plane):
        # No kind routes to playbook in Phase 1 (consolidation path only);
        # direct service attempts with agent/runtime roles fail.
        from memory.service import MemoryService
        svc = plane.drawers["playbook"]
        with pytest.raises(PermissionError):
            check_writer("playbook", "agent")
        assert svc.store.load() == []


# ── legacy compatibility ───────────────────────────────────────────────────

class TestLegacy:
    async def test_legacy_items_readable_with_none_drawer(self, plane,
                                                          tmp_path):
        _legacy_seed(tmp_path, ["Seville is famous for tapas"])
        hits = await plane.search("Seville tapas")
        assert len(hits) >= 1
        assert hits[0].drawer is None

    async def test_legacy_bytes_untouched_by_reads_and_drawer_writes(
            self, plane, tmp_path):
        _legacy_seed(tmp_path, ["Seville is famous for tapas"])
        before = hashlib.sha256(
            (tmp_path / "memory.json").read_bytes()).hexdigest()
        await plane.search("Seville")
        await plane.remember(kind="fact", descriptor="brand new fact",
                             source="t", run_id="r1")
        after = hashlib.sha256(
            (tmp_path / "memory.json").read_bytes()).hexdigest()
        assert before == after

    async def test_drawer_hits_rank_before_legacy(self, plane, tmp_path):
        _legacy_seed(tmp_path, ["Seville tapas flamenco"])
        await plane.remember(kind="fact", descriptor="Seville oranges market",
                             source="t", run_id="r1")
        hits = await plane.search("Seville", top_k=5)
        assert len(hits) >= 2
        assert hits[0].drawer == "fact"
        assert hits[-1].drawer is None

    async def test_unknown_drawer_name_is_400_not_silent(self, plane):
        with pytest.raises(ValueError):
            await plane.search("x", drawers=["nope"])


# ── lifetime / sessions ────────────────────────────────────────────────────

class TestLifetime:
    async def test_expired_hidden_from_reads(self, plane):
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        await plane.remember(kind="scratchpad", descriptor="stale note",
                             source="t", run_id="r1", expires_at=past)
        hits = await plane.search("stale note", drawers=["working"])
        assert hits == []
        # ...but still on disk (sweeps are Phase 2; reads just hide them).
        assert len(plane.drawers["working"].store.load()) == 1

    async def test_session_write_visible_globally(self, plane):
        await plane.remember(kind="fact", descriptor="session fact alpha",
                             source="t", run_id="r1", session_id="s1")
        hits = await plane.search("session fact alpha")
        assert any(h.session_id == "s1" for h in hits)


# ── clear + audit ──────────────────────────────────────────────────────────

class TestClear:
    async def test_clear_spares_audit_and_records_wipe(self, plane):
        await plane.remember(kind="fact", descriptor="doomed fact",
                             source="t", run_id="r1")
        counts = await plane.clear()
        assert counts["fact"] >= 1
        assert plane.drawers["fact"].store.load() == []
        audit = plane.drawers["audit"].store.load()
        assert any("cleared" in a.descriptor for a in audit)

    async def test_session_clear_keeps_other_sessions(self, plane):
        await plane.remember(kind="fact", descriptor="s1 fact",
                             source="t", run_id="r1", session_id="s1")
        await plane.remember(kind="fact", descriptor="s2 fact",
                             source="t", run_id="r1", session_id="s2")
        counts = await plane.clear(session_id="s1")
        assert counts["fact"] == 1
        rest = plane.drawers["fact"].store.load()
        assert [r.session_id for r in rest] == ["s2"]


# ── Phase 2: TTL, routing, session set ───────────────────────────────────

class TestTTL:
    async def test_working_gets_default_ttl(self, plane):
        from memory.plane import WORKING_TTL_MINUTES
        item = await plane.remember(kind="scratchpad", descriptor="temp note",
                                    source="t", run_id="r1")
        assert item.expires_at is not None
        ttl = item.expires_at - item.created_at
        assert abs(ttl.total_seconds() - WORKING_TTL_MINUTES * 60) < 5

    async def test_fact_has_no_default_ttl(self, plane):
        item = await plane.remember(kind="fact", descriptor="stable fact",
                                    source="t", run_id="r1")
        assert item.expires_at is None

    async def test_sweep_deletes_expired_and_rebuilds_index(self, plane):
        from datetime import timedelta
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        svc = plane.drawers["fact"]
        # Write via the service (bypasses the plane's opportunistic sweep).
        # Facts embed, so the sweep must also rebuild the index.
        await svc.remember(kind="fact", descriptor="doomed fact alpha",
                           source="t", run_id="r1", drawer="fact",
                           expires_at=past)
        await svc.remember(kind="fact", descriptor="live fact beta",
                           source="t", run_id="r1", drawer="fact")
        assert svc.index is not None and svc.index.size == 2
        swept = plane.sweep()
        assert swept["fact"] == 1
        assert [r.descriptor for r in svc.store.load()] == ["live fact beta"]
        assert svc.index.size == 1
        hits = await svc.read("live fact beta")
        assert any("beta" in h.descriptor for h in hits)

    async def test_opportunistic_sweep_on_write(self, plane):
        from datetime import timedelta
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        svc = plane.drawers["working"]
        # Plant an expired record bypassing remember (no auto-sweep).
        from memory.models import MemoryRecord
        rec = MemoryRecord(id="mem:stale", kind="scratchpad",
                           drawer="working", descriptor="stale",
                           source="t", run_id="r1", expires_at=past)
        svc.store.append(rec)
        await plane.remember(kind="scratchpad", descriptor="fresh note",
                             source="t", run_id="r1")
        assert "mem:stale" not in {
            r.id for r in svc.store.load()}


class TestRouting:
    async def test_session_drawers_exclude_working_and_audit(self):
        from memory.plane import SESSION_DRAWERS
        assert "working" not in SESSION_DRAWERS
        assert "audit" not in SESSION_DRAWERS
        assert "legacy" in SESSION_DRAWERS
        assert "fact" in SESSION_DRAWERS and "policy" in SESSION_DRAWERS

    async def test_unwritten_drawers_cost_no_embed(self, tmp_path):
        calls: list[str] = []

        async def counting(text: str, task_type: str):
            calls.append(text)
            return _bow(text)

        from memory.plane import MemoryPlane
        p = MemoryPlane(tmp_path, embed_fn=counting)
        assert await p.search("anything") == []
        assert calls == []  # no store files anywhere → no embed attempted

    async def test_one_embed_across_drawers(self, tmp_path):
        calls: list[str] = []

        async def counting(text: str, task_type: str):
            calls.append(text)
            return _bow(text)

        from memory.plane import MemoryPlane
        p = MemoryPlane(tmp_path, embed_fn=counting)
        await p.remember(kind="fact", descriptor="shared token alpha",
                         source="t", run_id="r1")
        await p.remember(kind="scratchpad", descriptor="shared token beta",
                         source="t", run_id="r1")
        calls.clear()
        await p.search("shared token")
        assert len(calls) == 1  # one query embed for the whole cabinet


class TestPurge:
    async def test_older_than_scoping(self, plane):
        from datetime import timedelta
        await plane.remember(kind="scratchpad", descriptor="old note",
                             source="t", run_id="r1", session_id="s1")
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        counts = await plane.clear(session_id="s1", drawers=["working"],
                                   older_than=future)
        assert counts["working"] == 1
        await plane.remember(kind="scratchpad", descriptor="new note",
                             source="t", run_id="r1", session_id="s1")
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        counts = await plane.clear(session_id="s1", drawers=["working"],
                                   older_than=past)
        assert counts["working"] == 0  # younger than the cutoff: kept
        assert len(plane.drawers["working"].store.load()) == 1

    async def test_drawer_scoped_clear_leaves_others(self, plane):
        await plane.remember(kind="fact", descriptor="keep me",
                             source="t", run_id="r1")
        await plane.remember(kind="scratchpad", descriptor="purge me",
                             source="t", run_id="r1")
        counts = await plane.clear(drawers=["working"])
        assert counts.get("working") == 1
        assert "fact" not in counts  # untouched drawer not reported
        assert len(plane.drawers["fact"].store.load()) == 1


# ── Phase 3: supersede, drawer override, active policies ──────────────────

class TestSupersede:
    async def test_correction_hides_old_from_recall(self, plane):
        old = await plane.remember(kind="fact",
                                   descriptor="meeting on 15 May 2026",
                                   source="t", run_id="r1")
        new = await plane.remember(kind="fact",
                                   descriptor="meeting on 16 May 2026",
                                   source="t", run_id="r1",
                                   supersedes=old.id)
        assert new.supersedes == old.id
        stored = {r.id: r for r in plane.drawers["fact"].store.load()}
        assert stored[old.id].superseded_by == new.id
        hits = await plane.search("meeting May", drawers=["fact"])
        ids = {h.id for h in hits}
        assert new.id in ids and old.id not in ids
        # Review path still shows both.
        listed = {r.id for r in plane.list_recent(drawers=["fact"], limit=10)}
        assert {old.id, new.id} <= listed
        # The correction itself is audit-recorded.
        audit = plane.drawers["audit"].store.load()
        assert any(old.id in a.descriptor for a in audit)

    async def test_supersede_ghost_id_fails_loud(self, plane):
        # The new record is written first (valid claim preserved); linking
        # a ghost target raises instead of silently dangling.
        with pytest.raises(LookupError, match="not found"):
            await plane.remember(kind="fact", descriptor="orphan correction",
                                 source="t", run_id="r1",
                                 supersedes="mem:ghost0000")

    async def test_legacy_records_cannot_be_superseded(self, plane, tmp_path):
        _legacy_seed(tmp_path, ["legacy claim here"])
        legacy_id = plane.legacy.store.load()[0].id
        with pytest.raises(LookupError, match="frozen"):
            await plane.remember(kind="fact", descriptor="corrected claim",
                                 source="t", run_id="r1",
                                 supersedes=legacy_id)


class TestDrawerOverride:
    async def test_operator_writes_policy_via_override(self, plane):
        item = await plane.remember(kind="fact",
                                    descriptor="never email passwords",
                                    source="dashboard", run_id="op-1",
                                    drawer="policy", principal_role="operator")
        assert item.drawer == "policy"
        assert len(plane.drawers["policy"].store.load()) == 1

    async def test_agent_cannot_write_policy(self, plane):
        with pytest.raises(PermissionError):
            await plane.remember(kind="fact", descriptor="sneaky rule",
                                 source="t", run_id="r1",
                                 drawer="policy", principal_role="agent")

    async def test_chunk_rule_beats_override(self, plane):
        # Indexer spans must stay spans even if a caller names fact.
        item = await plane.remember(
            kind="fact", descriptor="span body",
            value={"chunk": "span body"}, source="index", run_id="r1",
            drawer="fact", principal_role="indexer")
        assert item.drawer == "document"

    async def test_unknown_drawer_override_rejected(self, plane):
        with pytest.raises(ValueError, match="unknown drawer"):
            await plane.remember(kind="fact", descriptor="x",
                                 source="t", run_id="r1",
                                 drawer="nope",  # type: ignore[arg-type]
                                 principal_role="operator")


class TestActivePolicies:
    async def test_revoked_policy_not_active(self, plane):
        v1 = await plane.remember(kind="fact", descriptor="old rule alpha",
                                  source="dashboard", run_id="op-1",
                                  drawer="policy", principal_role="operator")
        await plane.remember(kind="fact", descriptor="new rule alpha",
                             source="dashboard", run_id="op-1",
                             drawer="policy", principal_role="operator",
                             supersedes=v1.id)
        active = plane.active_policies()
        assert [p.descriptor for p in active] == ["new rule alpha"]


# ── Phase 4: episodes + playbook proposals ─────────────────────────────────

class TestEpisodes:
    async def test_episodes_session_filter_and_order(self, plane):
        await plane.record_outcome(tool="t1", arguments={}, result_text="r1",
                                   artifact_id=None, run_id="run-a",
                                   goal_id=None, session_id="s1")
        await plane.record_outcome(tool="t2", arguments={}, result_text="r2",
                                   artifact_id=None, run_id="run-b",
                                   goal_id=None, session_id="s2")
        await plane.record_outcome(tool="t3", arguments={}, result_text="r3",
                                   artifact_id=None, run_id="run-a2",
                                   goal_id=None, session_id="s1")
        all_eps = plane.episodes()
        assert len(all_eps) == 3
        assert all_eps[0].run_id == "run-a2"  # newest first, no vector work
        s1 = plane.episodes(session_id="s1")
        assert {e.run_id for e in s1} == {"run-a", "run-a2"}

    async def test_episodes_cost_no_embed(self, tmp_path):
        calls: list[str] = []

        async def counting(text: str, task_type: str):
            calls.append(text)
            return _bow(text)

        from memory.plane import MemoryPlane
        p = MemoryPlane(tmp_path, embed_fn=counting)
        await p.record_outcome(tool="t", arguments={}, result_text="r",
                               artifact_id=None, run_id="r1", goal_id=None)
        calls.clear()
        eps = p.episodes()
        assert len(eps) == 1 and calls == []


class TestPlaybook:
    async def _evidence(self, plane):
        ev = await plane.remember(kind="fact", descriptor="evidence fact",
                                  source="t", run_id="r1")
        return ev.id

    async def test_propose_creates_ttl_working_record(self, plane):
        eid = await self._evidence(plane)
        prop = await plane.propose_playbook(
            descriptor="retry once on 503 then fail over",
            procedure={"steps": ["retry", "failover"]},
            evidence_ids=[eid], source="agent", run_id="r1")
        assert prop.drawer == "working"
        assert prop.value["proposal"] is True
        assert prop.value["evidence_ids"] == [eid]
        assert prop.expires_at is not None

    async def test_propose_ghost_evidence_fails_before_write(self, plane):
        with pytest.raises(LookupError, match="not found"):
            await plane.propose_playbook(
                descriptor="bad proposal", evidence_ids=["mem:ghost0000"],
                source="agent", run_id="r1")
        assert plane.drawers["working"].store.load() == []

    async def test_approve_promotes_and_consumes(self, plane):
        eid = await self._evidence(plane)
        prop = await plane.propose_playbook(
            descriptor="retry once on 503", evidence_ids=[eid],
            source="agent", run_id="r1")
        book = await plane.approve_playbook(
            proposal_id=prop.id, run_id="op-9", principal_role="system",
            principal_id="operator")
        assert book.drawer == "playbook"
        assert book.value["promoted_from"] == prop.id
        assert book.value["evidence_ids"] == [eid]
        # Consumed proposal drops from recall, stays for review...
        hits = await plane.search("retry 503", drawers=["working"])
        assert prop.id not in {h.id for h in hits}
        listed = {r.id for r in plane.list_recent(drawers=["working"],
                                                  limit=10)}
        assert prop.id in listed
        # ...and the promotion is audit-recorded.
        audit = plane.drawers["audit"].store.load()
        assert any(prop.id in a.descriptor and "playbook promoted" in a.descriptor
                   for a in audit)

    async def test_approve_rejects_agent_role(self, plane):
        eid = await self._evidence(plane)
        prop = await plane.propose_playbook(
            descriptor="sneaky promotion", evidence_ids=[eid],
            source="agent", run_id="r1")
        with pytest.raises(PermissionError):
            await plane.approve_playbook(proposal_id=prop.id, run_id="r1",
                                         principal_role="agent")
        assert plane.drawers["playbook"].store.load() == []

    async def test_approve_non_proposal_rejected(self, plane):
        fact = await plane.remember(kind="fact", descriptor="plain fact",
                                    source="t", run_id="r1")
        with pytest.raises(ValueError, match="not an open playbook proposal"):
            await plane.approve_playbook(proposal_id=fact.id, run_id="op-1",
                                         principal_role="system")

    async def test_double_approve_rejected(self, plane):
        eid = await self._evidence(plane)
        prop = await plane.propose_playbook(
            descriptor="once only", evidence_ids=[eid],
            source="agent", run_id="r1")
        await plane.approve_playbook(proposal_id=prop.id, run_id="op-1",
                                     principal_role="system")
        with pytest.raises(ValueError, match="already consumed"):
            await plane.approve_playbook(proposal_id=prop.id, run_id="op-2",
                                         principal_role="system")


# ── Phase 5: document versions + neighbors ─────────────────────────────────

class TestDocVersions:
    async def _index(self, plane, doc_id, version, texts):
        for i, text in enumerate(texts):
            await plane.remember(
                kind="fact", descriptor=f"[{doc_id} v{version}] {text}",
                value={"chunk": text, "chunk_index": i,
                       "total_chunks": len(texts)},
                source="index", run_id="r1", principal_role="indexer",
                doc={"doc_id": doc_id, "version": version,
                     "chunk_index": i, "total_chunks": len(texts)})

    async def test_version_flip_hides_old_from_recall(self, plane):
        await self._index(plane, "spec.md", "1", ["alpha beta gamma"])
        await self._index(plane, "spec.md", "2", ["alpha beta gamma delta"])
        hits = await plane.search("alpha beta", drawers=["document"])
        versions = {(h.doc.version if h.doc else None) for h in hits}
        assert versions == {"2"}
        # Review path still lists both; stale opt-in recalls both.
        listed = plane.list_recent(drawers=["document"], limit=10)
        assert {(r.doc.version if r.doc else None) for r in listed} == {"1", "2"}
        stale = await plane.search("alpha beta", drawers=["document"],
                                   include_stale=True)
        assert {(h.doc.version if h.doc else None) for h in stale} == {"1", "2"}

    async def test_neighbors_attached(self, plane):
        await self._index(plane, "guide.md", "1",
                          ["first chunk words", "second chunk words",
                           "third chunk words"])
        hits = await plane.search("second chunk", drawers=["document"],
                                  top_k=1)
        assert len(hits) == 1
        previews = (hits[0].value or {}).get("neighbor_previews") or []
        idxs = sorted(p["chunk_index"] for p in previews)
        assert idxs == [0, 2]
        # Stored records are never mutated with previews.
        stored = plane.drawers["document"].store.load()
        assert all("neighbor_previews" not in (r.value or {})
                   for r in stored)

    async def test_unversioned_chunks_have_no_filter(self, plane):
        await plane.remember(kind="fact", descriptor="plain fact recall",
                             source="t", run_id="r1")
        hits = await plane.search("plain fact recall", drawers=["fact"])
        assert len(hits) >= 1


# ── HTTP boundary ──────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def api_client(tmp_path_factory):
    import os

    import memory_api as MA

    tmp = tmp_path_factory.mktemp("memdrawers")
    os.environ["GATEWAY_MEMORY_STATE"] = str(tmp)
    MA._reset_cache()
    import main as M
    transport = httpx.ASGITransport(app=M.app)
    async with M.app.router.lifespan_context(M.app):
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test",
                                     timeout=60) as c:
            yield c
    MA._reset_cache()
    os.environ.pop("GATEWAY_MEMORY_STATE", None)


@pytest.mark.asyncio
async def test_http_denied_role_is_403(api_client):
    r = await api_client.post("/v1/memory/remember", json={
        "kind": "fact", "descriptor": "runtime may not write facts",
        "source": "t", "run_id": "r1", "principal_role": "runtime"})
    assert r.status_code == 403, r.text
    assert "may not write" in r.text


@pytest.mark.asyncio
async def test_http_policy_override_and_supersede(api_client):
    r = await api_client.post("/v1/memory/remember", json={
        "kind": "fact", "descriptor": "operator rule one",
        "source": "dashboard", "run_id": "op-1",
        "drawer": "policy", "principal_role": "operator"})
    assert r.status_code == 200, r.text
    v1 = r.json()["item"]["id"]
    r = await api_client.post("/v1/memory/remember", json={
        "kind": "fact", "descriptor": "operator rule two",
        "source": "dashboard", "run_id": "op-1",
        "drawer": "policy", "principal_role": "operator",
        "supersedes": v1})
    assert r.status_code == 200, r.text
    # Revoked rule hidden from recall...
    r = await api_client.post("/v1/memory/search", json={
        "query": "operator rule", "drawers": ["policy"]})
    ids = {i["id"] for i in r.json()["items"]}
    assert v1 not in ids
    # ...visible for review, ghost targets 404.
    r = await api_client.get("/v1/memory",
                             params={"drawers": "policy", "limit": 10})
    assert v1 in {i["id"] for i in r.json()["items"]}
    r = await api_client.post("/v1/memory/remember", json={
        "kind": "fact", "descriptor": "ghost link",
        "source": "t", "run_id": "r1", "supersedes": "mem:ghost0000"})
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_http_scoped_search(api_client):
    r = await api_client.post("/v1/memory/remember", json={
        "kind": "scratchpad", "descriptor": "working note zebra",
        "source": "t", "run_id": "r1"})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["drawer"] == "working"
    r = await api_client.post("/v1/memory/search", json={
        "query": "zebra", "drawers": ["working"]})
    assert r.status_code == 200, r.text
    assert any(i["descriptor"] == "working note zebra"
               for i in r.json()["items"])
    r = await api_client.post("/v1/memory/search", json={
        "query": "zebra", "drawers": ["fact"]})
    assert r.status_code == 200, r.text
    assert all(i["descriptor"] != "working note zebra"
               for i in r.json()["items"])


@pytest.mark.asyncio
async def test_http_episodes_and_playbook_flow(api_client):
    r = await api_client.post("/v1/memory/record_outcome", json={
        "tool": "get_time", "arguments": {}, "result_text": "12:00",
        "run_id": "run-http-1", "session_id": "sess-http"})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["drawer"] == "episode"
    r = await api_client.get("/v1/memory/episodes",
                             params={"session_id": "sess-http"})
    assert r.status_code == 200, r.text
    assert any(i["run_id"] == "run-http-1" for i in r.json()["items"])

    # Evidence: seed a fact, propose with it, approve as system.
    r = await api_client.post("/v1/memory/remember", json={
        "kind": "fact", "descriptor": "http evidence fact",
        "source": "t", "run_id": "r1"})
    eid = r.json()["item"]["id"]
    r = await api_client.post("/v1/memory/playbook/propose", json={
        "descriptor": "http playbook candidate",
        "procedure": {"steps": ["x"]}, "evidence_ids": [eid],
        "source": "agent", "run_id": "r1"})
    assert r.status_code == 200, r.text
    pid = r.json()["item"]["id"]
    r = await api_client.post("/v1/memory/playbook/approve", json={
        "proposal_id": pid, "run_id": "op-1"})
    assert r.status_code == 403, r.text  # default agent role: denied
    r = await api_client.post("/v1/memory/playbook/approve", json={
        "proposal_id": pid, "run_id": "op-1",
        "principal_role": "system"})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["drawer"] == "playbook"
