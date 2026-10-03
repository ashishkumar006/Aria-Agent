"""P1.2 — Memory thin-client tests (deterministic, no live gateway needed).

The store, FAISS index, embeddings and retrieval strategy live on the
gateway now (covered by llm_gatewayV9/tests/test_memory.py). These tests
cover what the agent still owns:

- ``remember`` — LLM-classified write (classifier mocked), correct
  ``POST /v1/memory/remember`` payload, response parsed to MemoryItem
- classifier failure → deterministic fact-write fallback (same payload path)
- empty classifier ``value`` → ``{"raw": ...}`` preserved
- ``read`` — search payload + parsing; fail-soft ``[]`` when the gateway
  is unreachable
- ``record_outcome`` / ``add_fact`` — payload shapes, zero LLM involvement
- ``clear`` / ``list_recent`` — endpoint + params
- ``_tokens`` — keyword extraction for the fallback path

HTTP is mocked at the ``memory._post`` / ``_get`` / ``_delete`` seam —
we test the *client* logic, not httpx.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import memory as memory_svc
from schemas import MemoryItem


def _item_dict(**kw):
    d = {"id": "mem:test0001", "kind": "fact", "keywords": ["seville"],
         "descriptor": "Seville is famous for tapas",
         "value": {"raw": "Seville is famous for tapas"},
         "artifact_id": None, "embedding": None,
         "source": "test", "run_id": "r1", "goal_id": None,
         "confidence": 1.0}
    d.update(kw)
    return d


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch):
    """Never touch the network: gateway health-check is a no-op."""
    monkeypatch.setattr(memory_svc, "ensure_gateway", lambda: None)


# ── remember() entry point (classifier mocked) ─────────────────────────────

class TestRemember:
    def test_classifier_unavailable_uses_fallback(self):
        """When the LLM classifier raises, remember still writes via fallback."""
        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            posted["_path"] = path
            return {"item": _item_dict(descriptor="The meeting is on 15 May 2026.")}

        with mock.patch.object(memory_svc, "_llm_classify",
                               side_effect=Exception("no gateway")), \
             mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            item = memory_svc.remember("The meeting is on 15 May 2026.",
                                       source="user", run_id="r1")
        assert item.kind == "fact"
        assert "15 May 2026" in item.descriptor
        assert posted["_path"] == "/v1/memory/remember"
        assert posted["value"] == {"raw": "The meeting is on 15 May 2026."}

    def test_classifier_result_parsed(self):
        """A successful classifier reply is parsed into the right kind/keywords."""
        fake_reply = {"parsed": {"kind": "preference", "descriptor": "I like tea",
                                 "keywords": ["tea", "preference"], "value": {}}}
        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            return {"item": _item_dict(kind="preference", descriptor="I like tea",
                                       keywords=["tea", "preference"],
                                       value={"raw": "I like tea."})}

        with mock.patch.object(memory_svc, "_llm_classify", return_value=fake_reply), \
             mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            item = memory_svc.remember("I like tea.", source="user", run_id="r1")
        assert item.kind == "preference"
        assert "tea" in item.keywords
        assert posted["kind"] == "preference"

    def test_empty_value_falls_back_to_raw(self):
        """Classifier returning empty value keeps the raw text retrievable."""
        fake_reply = {"parsed": {"kind": "fact", "descriptor": "x", "keywords": [],
                                 "value": {}}}
        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            return {"item": _item_dict(value={"raw": "important raw detail here."})}

        with mock.patch.object(memory_svc, "_llm_classify", return_value=fake_reply), \
             mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            item = memory_svc.remember("important raw detail here.",
                                       source="user", run_id="r1")
        assert item.value == {"raw": "important raw detail here."}
        assert posted["value"] == {"raw": "important raw detail here."}

    def test_transport_failure_raises(self):
        """Gateway down on write → raise so the caller decides (flow logs)."""
        with mock.patch.object(memory_svc, "_llm_classify",
                               return_value={"parsed": {"kind": "fact"}}), \
             mock.patch.object(memory_svc, "_post",
                               side_effect=Exception("conn refused")), \
             pytest.raises(Exception, match="conn refused"):
            memory_svc.remember("x", source="user", run_id="r1")


# ── read() public API ───────────────────────────────────────────────────────

class TestRead:
    def test_read_posts_search_and_parses(self):
        seen = {}

        def fake_post(path, body, timeout=60.0):
            seen.update(body)
            seen["_path"] = path
            return {"items": [_item_dict()]}

        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            hits = memory_svc.read("Seville food")
        assert len(hits) == 1
        assert isinstance(hits[0], MemoryItem)
        assert seen["_path"] == "/v1/memory/search"
        assert seen["query"] == "Seville food"
        assert seen["top_k"] == 8

    def test_read_forwards_kinds_topk_session(self):
        seen = {}

        def fake_post(path, body, timeout=60.0):
            seen.update(body)
            return {"items": []}

        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            assert memory_svc.read("x", kinds=["fact"], top_k=3,
                                   session_id="s1") == []
        assert seen["kinds"] == ["fact"]
        assert seen["top_k"] == 3
        assert seen["session_id"] == "s1"

    def test_read_fail_soft_on_gateway_down(self):
        with mock.patch.object(memory_svc, "_post",
                               side_effect=Exception("conn refused")):
            assert memory_svc.read("Seville food") == []


# ── direct writes / maintenance ─────────────────────────────────────────────

class TestWrites:
    def test_record_outcome_zero_llm(self):
        """record_outcome() posts without any LLM call."""
        from schemas import ToolCall

        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            posted["_path"] = path
            return {"item": _item_dict(
                kind="tool_outcome",
                descriptor="get_weather({\"location\": \"London\"}) -> 22C",
                value={"tool": "get_weather"})}

        with mock.patch.object(memory_svc, "_llm_classify",
                               side_effect=AssertionError("must not classify")), \
             mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            item = memory_svc.record_outcome(
                tool_call=ToolCall(name="get_weather",
                                   arguments={"location": "London"}),
                result_text="22C sunny", artifact_id=None,
                run_id="r1", goal_id="g1")
        assert item.kind == "tool_outcome"
        assert posted["_path"] == "/v1/memory/record_outcome"
        assert posted["tool"] == "get_weather"
        assert posted["arguments"] == {"location": "London"}

    def test_add_fact_direct_write(self):
        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            return {"item": _item_dict()}

        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            item = memory_svc.add_fact(
                "Python is a programming language",
                value={"category": "language"},
                keywords=["python", "programming"],
                source="index_document", run_id="r1")
        assert item.kind == "fact"
        assert posted["kind"] == "fact"
        assert posted["keywords"] == ["python", "programming"]

    def test_clear_hits_endpoint(self):
        seen = {}

        def fake_delete(path, params=None, timeout=30.0):
            seen["path"] = path
            seen["params"] = params
            return {"cleared": 2}

        with mock.patch.object(memory_svc, "_delete", side_effect=fake_delete):
            memory_svc.clear()
            assert seen["path"] == "/v1/memory"
            memory_svc.clear(session_id="s9")
            assert seen["params"] == {"session_id": "s9"}

    def test_list_recent_parses(self):
        def fake_get(path, params=None, timeout=30.0):
            assert path == "/v1/memory"
            return {"items": [_item_dict(), _item_dict(id="mem:test0002")]}

        with mock.patch.object(memory_svc, "_get", side_effect=fake_get):
            items = memory_svc.list_recent(limit=10)
        assert [i.id for i in items] == ["mem:test0001", "mem:test0002"]

    def test_list_recent_raises_on_backend_failure(self):
        """A failed read must NOT be indistinguishable from "no memories".

        This used to assert `list_recent() == []` on a transport error, which
        is exactly what made the dashboard render an empty Memory panel while
        the gateway was down -- a user would conclude their memory had been
        deleted and start re-entering it. `read` (recall) stays fail-soft
        because it runs at session start; `list_recent` must raise so the
        caller can show an error.
        """
        with mock.patch.object(memory_svc, "_get",
                               side_effect=Exception("down")):
            with pytest.raises(memory_svc.MemoryBackendError):
                memory_svc.list_recent()

    def test_list_recent_carries_the_gateway_status_code(self):
        """A gateway 400 (unknown kind) stays a 400, not a generic failure."""
        resp = mock.Mock(status_code=400)
        err = Exception("400 Bad Request")
        err.response = resp
        with mock.patch.object(memory_svc, "_get", side_effect=err):
            with pytest.raises(memory_svc.MemoryBackendError) as ei:
                memory_svc.list_recent()
        assert ei.value.status_code == 400


# ── Phase 1 drawers: principals + drawer scope ─────────────────────────────

class TestDrawerClient:
    def test_remember_sends_agent_principal(self):
        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            return {"item": _item_dict()}

        with mock.patch.object(memory_svc, "_llm_classify",
                               return_value={"parsed": {"kind": "fact"}}), \
             mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            memory_svc.remember("x", source="user", run_id="r1")
        assert posted["principal_role"] == "agent"

    def test_add_fact_sends_indexer_principal(self):
        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            return {"item": _item_dict()}

        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            memory_svc.add_fact("x", source="index", run_id="r1")
        assert posted["principal_role"] == "indexer"

    def test_read_forwards_drawers_scope(self):
        seen = {}

        def fake_post(path, body, timeout=60.0):
            seen.update(body)
            return {"items": []}

        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            assert memory_svc.read("x", drawers=["fact", "policy"]) == []
        assert seen["drawers"] == ["fact", "policy"]

    def test_record_with_drawer_fields_parses(self):
        """Gateway records (drawer/scope/principal/...) validate as the
        agent's MemoryItem — extras are ignored, prompts keep working."""
        full = _item_dict(drawer="fact",
                          scope={"tenant_id": "course", "project_id": "s9",
                                 "user_id": "local", "agent_id": "assistant"},
                          sources=[{"uri": "api://agent/runs",
                                    "author": "rohan"}],
                          principal={"id": "assistant", "role": "agent"},
                          expires_at=None, supersedes=None,
                          superseded_by=None, doc=None, session_id="s1")

        def fake_post(path, body, timeout=60.0):
            return {"items": [full]}

        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            hits = memory_svc.read("x")
        assert len(hits) == 1
        assert hits[0].descriptor == full["descriptor"]


# ── Phase 3: policy writes + active policies ───────────────────────────────

class TestPolicyClient:
    def test_write_policy_posts_operator_fact(self):
        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            posted["_path"] = path
            return {"item": _item_dict(kind="fact")}

        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            item = memory_svc.write_policy("never email passwords",
                                           run_id="op-1")
        assert posted["_path"] == "/v1/memory/remember"
        assert posted["kind"] == "fact"
        assert posted["drawer"] == "policy"
        assert posted["principal_role"] == "operator"
        assert posted["supersedes"] is None
        assert item.kind == "fact"

    def test_write_policy_forwards_supersedes(self):
        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            return {"item": _item_dict()}

        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            memory_svc.write_policy("new rule", run_id="op-2",
                                    supersedes="mem:old0001")
        assert posted["supersedes"] == "mem:old0001"

    def test_write_policy_skips_classifier(self):
        """Operator text is deliberate — zero LLM involvement."""
        with mock.patch.object(memory_svc, "_llm_classify",
                               side_effect=AssertionError("no LLM")), \
             mock.patch.object(memory_svc, "_post",
                               return_value={"item": _item_dict()}):
            memory_svc.write_policy("rule", run_id="op-1")

    def test_policies_hits_scoped_endpoint(self):
        seen = {}

        def fake_get(path, params=None, timeout=30.0):
            seen["path"] = path
            seen["params"] = params
            return {"items": []}

        with mock.patch.object(memory_svc, "_get", side_effect=fake_get):
            assert memory_svc.policies() == []
        assert seen["path"] == "/v1/memory"
        assert seen["params"]["drawers"] == "policy"
        assert seen["params"]["hide_superseded"] == "true"

    def test_format_policy_notes(self):
        from skills import _format_policy_notes

        assert _format_policy_notes([]) == ""
        hits = [MemoryItem.model_validate(_item_dict(
            descriptor="never email passwords", source="dashboard"))]
        out = _format_policy_notes(hits)
        assert "never email passwords" in out


    def test_add_fact_forwards_doc_span(self):
        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            return {"item": _item_dict()}

        doc = {"doc_id": "sandbox:notes.md", "version": "2",
               "chunk_index": 3, "total_chunks": 9}
        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            memory_svc.add_fact("chunk body", source="index", run_id="r1",
                                doc=doc)
        assert posted["doc"] == doc

    def test_read_forwards_include_stale(self):
        seen = {}

        def fake_post(path, body, timeout=60.0):
            seen.update(body)
            return {"items": []}

        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            assert memory_svc.read("x", include_stale=True) == []
        assert seen["include_stale"] is True


# ── Phase 4: episodes + playbook path ─────────────────────────────────────

class TestPhase4Client:
    def test_episodes_hits_endpoint(self):
        seen = {}

        def fake_get(path, params=None, timeout=30.0):
            seen["path"] = path
            seen["params"] = params
            return {"items": [_item_dict(kind="tool_outcome")]}

        with mock.patch.object(memory_svc, "_get", side_effect=fake_get):
            eps = memory_svc.episodes(session_id="s1", limit=10)
        assert seen["path"] == "/v1/memory/episodes"
        assert seen["params"] == {"limit": 10, "session_id": "s1"}
        assert len(eps) == 1 and eps[0].kind == "tool_outcome"

    def test_episodes_fail_soft(self):
        with mock.patch.object(memory_svc, "_get",
                               side_effect=Exception("down")):
            assert memory_svc.episodes() == []

    def test_propose_playbook_payload(self):
        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            posted["_path"] = path
            return {"item": _item_dict()}

        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            memory_svc.propose_playbook(
                "retry once", procedure={"steps": ["retry"]},
                evidence_ids=["mem:e1"], source="agent", run_id="r1")
        assert posted["_path"] == "/v1/memory/playbook/propose"
        assert posted["principal_role"] == "agent"
        assert posted["evidence_ids"] == ["mem:e1"]

    def test_approve_playbook_defaults_denied_role(self):
        posted = {}

        def fake_post(path, body, timeout=60.0):
            posted.update(body)
            posted["_path"] = path
            return {"item": _item_dict()}

        with mock.patch.object(memory_svc, "_post", side_effect=fake_post):
            memory_svc.approve_playbook("mem:p1", run_id="op-1")
        assert posted["_path"] == "/v1/memory/playbook/approve"
        # Fail-closed default: agent role goes out; gateway denies it.
        assert posted["principal_role"] == "agent"


# ── keyword extraction (fallback path) ──────────────────────────────────────

class TestTokens:
    def test_tokens_lowercase_and_stopwords(self):
        toks = memory_svc._tokens("Hello World, this is a TEST!")
        assert "hello" in toks and "test" in toks
        assert "is" not in toks and "a" not in toks and "this" not in toks

    def test_short_words_dropped(self):
        assert memory_svc._tokens("go to it") == set()
