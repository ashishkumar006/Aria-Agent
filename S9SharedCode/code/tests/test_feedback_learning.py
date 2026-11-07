"""Feedback → memory → persona loop.

These tests keep the LLM seam and the memory seam injected: no gateway, no
network, no live state. They pin the parts that made the old loop incomplete:
the rated answer is reconstructed, a note is interpreted in context, a model
failure cannot turn a raw complaint into a "fact", the same note is never
applied twice, and an applied note reaches the persisted persona documents.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server  # noqa: E402
from agent_server import app  # noqa: E402
from feedback_learning import FeedbackLearner, chat_signature  # noqa: E402
from schemas import AgentResult, NodeState  # noqa: E402


class FakeMemory:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.writes: list[tuple] = []

    def list_recent(self, **kwargs):
        return list(self.existing)

    def remember_preference(self, descriptor, **kwargs):
        self.writes.append(("preference", descriptor, kwargs))
        item = types.SimpleNamespace(
            id=f"mem:p{len(self.writes)}", kind="preference",
            descriptor=descriptor, value=kwargs.get("value") or {})
        self.existing.append(item)
        return item

    def remember_fact(self, descriptor, **kwargs):
        self.writes.append(("fact", descriptor, kwargs))
        item = types.SimpleNamespace(
            id=f"mem:f{len(self.writes)}", kind="fact",
            descriptor=descriptor, value=kwargs.get("value") or {})
        self.existing.append(item)
        return item

    def detect_preference(self, text):
        low = str(text or "").lower()
        return text if low.startswith(("i prefer", "i always", "i never")) else None


def _write_feedback(tmp_path: Path, **overrides) -> dict:
    entry = {
        "id": "fb-test0001",
        "t": 1.0,
        "session_id": "ct-1",
        "node_id": "",
        "msg_id": "sig",
        "vote": -1,
        "skill": "chat",
        "note": "I prefer short answers with no preamble",
    }
    entry.update(overrides)
    path = tmp_path / "feedback.jsonl"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return entry


def _learner(tmp_path: Path, *, chat_fn=None, memory=None):
    return FeedbackLearner(
        state_dir=tmp_path,
        feedback_path=tmp_path / "feedback.jsonl",
        chat_threads_path=tmp_path / "chat_threads.json",
        sessions_root=tmp_path / "sessions",
        chat_fn=chat_fn,
        memory_api=memory or FakeMemory(),
    )


def _model_reply(**overrides):
    value = {
        "action": "remember",
        "kind": "preference",
        "statement": "prefers concise answers with no preamble",
        "evidence": "I prefer short answers with no preamble",
        "keywords": ["concise", "preamble"],
        "confidence": 0.9,
        "supersedes": "",
        "reason": "the note states a standing style preference",
    }
    value.update(overrides)
    return value


def test_chat_signature_matches_the_console_shape():
    assert chat_signature({"role": "assistant", "content": "Hello"}) == (
        '["assistant","Hello",null,null,false,null]')
    assert chat_signature({
        "role": "assistant", "content": "A", "kind": "report",
        "title": "T", "surface": {"type": "card"},
    }) == '["assistant","A","report","T",false,{"type":"card"}]'


def test_chat_context_finds_the_rated_turn(tmp_path):
    assistant = {"role": "assistant", "content": "A long answer."}
    (tmp_path / "chat_threads.json").write_text(json.dumps({
        "ct-1": {"title": "t", "messages": [
            {"role": "user", "content": "Explain X"},
            assistant,
        ]},
    }), encoding="utf-8")
    entry = _write_feedback(
        tmp_path, msg_id=chat_signature(assistant),
        note="too long, keep it short")
    learner = _learner(tmp_path)
    context = learner.build_context(entry)
    assert context["source"] == "chat"
    assert context["matched"] is True
    assert context["rated_response"] == "A long answer."
    assert context["question"] == "Explain X"


def test_chat_context_falls_back_to_latest_settled_turn(tmp_path):
    (tmp_path / "chat_threads.json").write_text(json.dumps({
        "ct-1": {"title": "t", "messages": [
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "stale"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "latest"},
            {"role": "assistant", "content": "", "pending": True},
        ]},
    }), encoding="utf-8")
    entry = _write_feedback(tmp_path, msg_id="not-a-real-signature")
    context = _learner(tmp_path).build_context(entry)
    assert context["matched"] is False
    assert context["rated_response"] == "latest"
    assert context["question"] == "Q2"


def test_node_context_reads_the_rated_node_output(tmp_path):
    sessions = tmp_path / "sessions" / "s8-test"
    (sessions / "nodes").mkdir(parents=True)
    state = NodeState(
        node_id="n:1", skill="researcher", status="complete", inputs=[],
        result=AgentResult(
            success=True, agent_name="researcher",
            output={"findings": "The measured answer was 42."}),
        prompt_sent="research this", started_at=1.0, completed_at=2.0)
    (sessions / "nodes" / "n_001.json").write_text(
        state.model_dump_json(indent=2), encoding="utf-8")
    (sessions / "query.txt").write_text("What is the answer?", encoding="utf-8")
    entry = _write_feedback(
        tmp_path, session_id="s8-test", node_id="n:1", msg_id="",
        note="the answer was correct")
    context = _learner(tmp_path).build_context(entry)
    assert context["source"] == "dag-node"
    assert context["skill"] == "researcher"
    assert "measured answer" in context["rated_response"]
    assert context["question"] == "What is the answer?"


def test_interpretation_receives_the_rated_answer_and_note(tmp_path):
    prompts: list[str] = []

    def chat(prompt, schema):
        prompts.append(prompt)
        return _model_reply()

    learner = _learner(tmp_path, chat_fn=chat)
    entry = _write_feedback(tmp_path)
    context = {
        "rated_response": "A very long preface followed by the answer.",
        "question": "Explain the protocol",
        "prior": "USER: earlier question",
        "skill": "chat",
        "source": "chat",
        "matched": True,
    }
    proposal = learner.interpret(entry, context, [])
    assert proposal["action"] == "remember"
    assert proposal["kind"] == "preference"
    assert proposal["value"]["source"] == "feedback_note"
    assert proposal["value"]["feedback_id"] == "fb-test0001"
    assert "A very long preface" in prompts[0]
    assert "I prefer short answers" in prompts[0]
    assert "Explain the protocol" in prompts[0]


def test_apply_preference_writes_provenance_and_never_reapplies(tmp_path):
    mem = FakeMemory()
    learner = _learner(tmp_path, chat_fn=lambda p, s: _model_reply(),
                       memory=mem)
    _write_feedback(tmp_path)

    first = learner.process_pending(dry_run=False)
    assert first["applied"] and len(first["applied"]) == 1
    kind, statement, kwargs = mem.writes[0]
    assert kind == "preference"
    assert statement == "prefers concise answers with no preamble"
    assert kwargs["value"]["source"] == "feedback_note"
    assert kwargs["value"]["feedback_id"] == "fb-test0001"
    assert kwargs["value"]["vote"] == -1
    assert kwargs["value"]["session_id"] == "ct-1"
    assert kwargs["confidence"] == 0.9

    second = learner.process_pending(dry_run=False)
    assert second["applied"] == []
    assert second["pending"] == 0
    assert len(mem.writes) == 1

    state = json.loads((tmp_path / "feedback_learning.json").read_text())
    event = state["events"]["fb-test0001"]
    assert event["status"] == "applied"
    assert event["memory_id"] == "mem:p1"


def test_fact_correction_uses_the_fact_writer(tmp_path):
    mem = FakeMemory()
    learner = _learner(
        tmp_path,
        chat_fn=lambda p, s: _model_reply(
            kind="fact", statement="the project targets Python 3.11",
            evidence="Actually this project targets Python 3.11",
            confidence=0.85),
        memory=mem)
    _write_feedback(tmp_path, note="Actually this project targets Python 3.11",
                    msg_id="")
    result = learner.process_pending(dry_run=False)
    assert result["applied"]
    assert mem.writes[0][0] == "fact"
    assert mem.writes[0][1] == "the project targets Python 3.11"


def test_praise_cannot_become_an_unrelated_preference(tmp_path):
    """The rated answer being short must not turn "good report" into
    "likes short answers" — the note is the only evidence."""
    mem = FakeMemory()
    learner = _learner(
        tmp_path,
        chat_fn=lambda p, s: _model_reply(
            statement="likes short answers",
            evidence="this is a good report",
            confidence=0.9),
        memory=mem)
    _write_feedback(tmp_path, note="hmm, this is a good report")
    result = learner.process_pending(dry_run=False)
    assert result["applied"] == []
    assert result["skipped"]
    assert mem.writes == []


def test_evidence_must_be_quoted_from_the_note(tmp_path):
    mem = FakeMemory()
    learner = _learner(
        tmp_path,
        chat_fn=lambda p, s: _model_reply(
            statement="prefers concise answers",
            evidence="the answer was short"),
        memory=mem)
    _write_feedback(tmp_path, note="this is a good report")
    result = learner.process_pending(dry_run=False)
    assert result["applied"] == []
    assert mem.writes == []


def test_a_correction_cue_lets_a_general_statement_through(tmp_path):
    """'too verbose' supports 'prefers concise answers' even without shared
    vocabulary; the note is still asking for a change."""
    mem = FakeMemory()
    learner = _learner(
        tmp_path,
        chat_fn=lambda p, s: _model_reply(
            statement="prefers concise answers",
            evidence="too verbose",
            confidence=0.8),
        memory=mem)
    _write_feedback(tmp_path, note="too verbose, keep it short")
    result = learner.process_pending(dry_run=False)
    assert result["applied"]
    assert mem.writes[0][0] == "preference"


def test_low_confidence_and_trait_labels_are_skipped(tmp_path):
    mem = FakeMemory()
    learner = _learner(
        tmp_path,
        chat_fn=lambda p, s: _model_reply(confidence=0.2),
        memory=mem)
    _write_feedback(tmp_path)
    result = learner.process_pending(dry_run=False)
    assert result["skipped"]
    assert mem.writes == []

    mem2 = FakeMemory()
    learner2 = _learner(
        tmp_path,
        chat_fn=lambda p, s: _model_reply(
            statement="the user is anxious about deadlines"),
        memory=mem2)
    (tmp_path / "feedback_learning.json").unlink(missing_ok=True)
    result2 = learner2.process_pending(dry_run=False)
    assert result2["skipped"]
    assert mem2.writes == []


def test_fallback_learns_clear_preferences_but_never_invents_facts(tmp_path):
    mem = FakeMemory()
    learner = _learner(tmp_path, chat_fn=lambda p, s: None, memory=mem)
    _write_feedback(tmp_path, note="I prefer metric units")
    result = learner.process_pending(dry_run=False)
    assert result["applied"]
    assert mem.writes[0][0] == "preference"
    assert "metric" in mem.writes[0][1].lower()

    other = tmp_path / "other"
    other.mkdir()
    mem2 = FakeMemory()
    learner2 = _learner(other, chat_fn=lambda p, s: None, memory=mem2)
    _write_feedback(other, note="this answer was wrong")
    result2 = learner2.process_pending(dry_run=False)
    assert result2["applied"] == []
    assert result2["skipped"]
    assert mem2.writes == []


def test_existing_equivalent_is_a_duplicate_not_a_second_write(tmp_path):
    existing = types.SimpleNamespace(
        id="mem:old", kind="preference",
        descriptor="prefers concise answers with no preamble",
        value={"raw": "I prefer short answers with no preamble"})
    mem = FakeMemory([existing])
    learner = _learner(tmp_path, chat_fn=lambda p, s: _model_reply(),
                       memory=mem)
    _write_feedback(tmp_path)
    result = learner.process_pending(dry_run=False)
    assert result["applied"] == []
    assert result["skipped"]
    assert mem.writes == []
    state = json.loads((tmp_path / "feedback_learning.json").read_text())
    assert state["events"]["fb-test0001"]["status"] == "duplicate"


def test_supersede_is_only_accepted_for_an_existing_preference(tmp_path):
    existing = types.SimpleNamespace(
        id="mem:old", kind="preference",
        descriptor="prefers imperial units", value={})
    mem = FakeMemory([existing])
    learner = _learner(
        tmp_path,
        chat_fn=lambda p, s: _model_reply(
            statement="prefers metric units", supersedes="mem:old",
            evidence="I prefer metric units now"),
        memory=mem)
    _write_feedback(tmp_path, note="I prefer metric units now")
    result = learner.process_pending(dry_run=False)
    assert result["applied"]
    assert mem.writes[0][2]["supersedes"] == "mem:old"

    # A hallucinated id is ignored rather than sent to the gateway.
    mem2 = FakeMemory([existing])
    learner2 = _learner(
        tmp_path,
        chat_fn=lambda p, s: _model_reply(
            supersedes="mem:invented", evidence="I prefer metric units now"),
        memory=mem2)
    (tmp_path / "feedback_learning.json").unlink(missing_ok=True)
    learner2.process_pending(dry_run=False)
    assert mem2.writes[0][2]["supersedes"] is None


def test_dry_run_never_writes_or_marks_processed(tmp_path):
    mem = FakeMemory()
    learner = _learner(tmp_path, chat_fn=lambda p, s: _model_reply(),
                       memory=mem)
    _write_feedback(tmp_path)
    result = learner.process_pending(dry_run=True)
    assert result["dry_run"] is True
    assert result["proposed"]
    assert result["applied"] == []
    assert mem.writes == []
    assert not (tmp_path / "feedback_learning.json").exists()
    assert learner.status()["pending"] == 1


def test_post_feedback_assigns_an_id_and_queues_learning(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_server, "STATE_DIR", tmp_path)
    queued: list[bool] = []
    monkeypatch.setattr(agent_server, "_queue_feedback_learning",
                        lambda: queued.append(True) or True)
    with TestClient(app) as client:
        r = client.post("/api/feedback", json={
            "session_id": "ct-1", "msg_id": "sig", "vote": 1,
            "skill": "chat", "note": "I prefer metric units"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feedback_id"].startswith("fb-")
    assert body["learning"] == "queued"
    assert queued == [True]
    lines = (tmp_path / "feedback.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["id"] == body["feedback_id"]


def test_get_feedback_restores_the_latest_note(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_server, "STATE_DIR", tmp_path)
    rows = [
        {"id": "fb-vote", "t": 1.0, "session_id": "ct-1", "node_id": "",
         "msg_id": "sig", "vote": -1, "skill": "chat", "note": ""},
        {"id": "fb-note1", "t": 2.0, "session_id": "ct-1", "node_id": "",
         "msg_id": "sig", "vote": -1, "skill": "chat",
         "note": "I prefer metric units"},
        # A later thumb-only row must not erase the note text.
        {"id": "fb-vote2", "t": 3.0, "session_id": "ct-1", "node_id": "",
         "msg_id": "sig", "vote": 1, "skill": "chat", "note": ""},
    ]
    (tmp_path / "feedback.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    with TestClient(app) as client:
        r = client.get("/api/feedback", params={
            "msg_id": "sig", "session_id": "ct-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vote"] == 1
    assert body["note"] == "I prefer metric units"
    assert body["feedback_id"] == "fb-note1"


def test_feedback_note_survives_live_signature_null_mismatch(tmp_path,
                                                             monkeypatch):
    """A live turn's msg_id had empty kind/title; a reloaded row has nulls.
    The note must still be found, and new posts must store the canonical form."""
    monkeypatch.setattr(agent_server, "STATE_DIR", tmp_path)
    live_sig = json.dumps(
        ["assistant", "Hello!", "", "", False, None],
        ensure_ascii=False, separators=(",", ":"))
    stored_sig = json.dumps(
        ["assistant", "Hello!", None, None, False, None],
        ensure_ascii=False, separators=(",", ":"))
    (tmp_path / "feedback.jsonl").write_text(json.dumps({
        "id": "fb-legacy", "t": 1.0, "session_id": "ct-1", "node_id": "",
        "msg_id": live_sig, "vote": 1, "skill": "chat",
        "note": "i do a lot of research",
    }) + "\n", encoding="utf-8")

    with TestClient(app) as client:
        r = client.get("/api/feedback", params={
            "msg_id": stored_sig, "session_id": "ct-1"})
        assert r.status_code == 200, r.text
        assert r.json()["note"] == "i do a lot of research"

        posted = client.post("/api/feedback", json={
            "session_id": "ct-1", "msg_id": live_sig, "vote": 1,
            "note": "another note"})
        assert posted.status_code == 200, posted.text

    rows = [json.loads(line) for line in
            (tmp_path / "feedback.jsonl").read_text().splitlines() if line]
    assert json.loads(rows[-1]["msg_id"])[2] is None
    assert json.loads(rows[-1]["msg_id"])[3] is None


def test_post_feedback_reports_when_automatic_learning_is_off(tmp_path,
                                                              monkeypatch):
    monkeypatch.setattr(agent_server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(agent_server, "_queue_feedback_learning",
                        lambda: False)
    with TestClient(app) as client:
        r = client.post("/api/feedback", json={
            "session_id": "ct-1", "msg_id": "sig", "vote": 1,
            "note": "I prefer metric units"})
    assert r.status_code == 200, r.text
    assert r.json()["learning"] == "disabled"


def test_post_feedback_without_a_note_does_not_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_server, "STATE_DIR", tmp_path)
    queued: list[bool] = []
    monkeypatch.setattr(agent_server, "_queue_feedback_learning",
                        lambda: queued.append(True) or True)
    with TestClient(app) as client:
        r = client.post("/api/feedback", json={
            "session_id": "ct-1", "msg_id": "sig", "vote": 1})
    assert r.status_code == 200, r.text
    assert r.json()["learning"] == "no_note"
    assert queued == []


def test_persona_sync_body_can_turn_a_dry_run_into_a_write(monkeypatch):
    seen: list[dict] = []

    def fake_sync(dry_run=True, limit=50):
        seen.append({"dry_run": dry_run, "limit": limit})
        return {"dry_run": dry_run, "applied": [], "proposed": [],
                "skipped": [], "errors": [], "pending": 0,
                "pending_before": 0, "selected": 0}

    monkeypatch.setattr(agent_server, "_sync_feedback_to_persona", fake_sync)
    with TestClient(app) as client:
        r = client.post("/api/profile/persona/sync",
                        json={"dry_run": False})
    assert r.status_code == 200, r.text
    assert seen and seen[0]["dry_run"] is False


def test_worker_regenerates_persona_after_an_applied_note(monkeypatch):
    applied = []

    class Learner:
        def process_pending(self, limit=20, dry_run=False):
            applied.append((limit, dry_run))
            return {"applied": [{"text": "prefers metric units"}],
                    "pending": 0}

    monkeypatch.setattr(agent_server, "_feedback_learner", lambda: Learner())
    refreshed: list[str] = []
    monkeypatch.setattr(
        agent_server, "_refresh_persona",
        lambda reason="manual": refreshed.append(reason) or {"ok": True})
    monkeypatch.setattr(agent_server, "_FEEDBACK_WORKER_RUNNING", False)
    agent_server._feedback_learning_worker()
    assert applied == [(20, False)]
    assert refreshed == ["feedback"]


def test_queue_respects_the_learning_kill_switch(monkeypatch):
    import flags
    monkeypatch.setattr(flags, "is_enabled",
                        lambda name, default=False: (
                            False if name == "feedback.learning" else default))
    assert agent_server._queue_feedback_learning() is False


def test_feedback_llm_chat_uses_the_schema_and_parses_fenced_json(monkeypatch):
    import gateway as gateway_mod
    seen: dict = {}

    class FakeLLM:
        def chat(self, **kwargs):
            seen.update(kwargs)
            return {"text": "```json\n" + json.dumps({
                "action": "skip", "kind": "preference", "statement": "",
                "evidence": "", "keywords": [], "confidence": 0.0,
                "supersedes": "", "reason": "nothing durable",
            }) + "\n```"}

    monkeypatch.setattr(gateway_mod, "LLM", lambda: FakeLLM())
    result = agent_server._feedback_llm_chat("prompt", {"type": "object"})
    assert result and result["action"] == "skip"
    assert seen["auto_route"] == "memory"
    assert seen["response_format"]["name"] == "FeedbackLearning"
    assert seen["response_format"]["schema"] == {"type": "object"}


def test_remember_fact_declares_agent_provenance(monkeypatch):
    import memory as memory_mod
    captured: dict = {}

    def fake_post(path, body, timeout=60):
        captured.update(path=path, body=body)
        return {"item": {
            "id": "mem:fact1", "kind": "fact", "keywords": body["keywords"],
            "descriptor": body["descriptor"], "value": body["value"],
            "source": body["source"], "run_id": body["run_id"],
            "confidence": body["confidence"],
        }}

    monkeypatch.setattr(memory_mod, "_post", fake_post)
    item = memory_mod.remember_fact(
        "the project targets Python 3.11",
        keywords=["python"],
        value={"raw": "we use 3.11"},
        source="feedback_note",
        run_id="feedback-sync",
        confidence=0.8,
    )
    assert captured["path"] == "/v1/memory/remember"
    assert captured["body"]["principal_role"] == "agent"
    assert captured["body"]["confidence"] == 0.8
    assert captured["body"]["kind"] == "fact"
    assert item.id == "mem:fact1"


def test_learning_status_endpoint_reports_flags(monkeypatch):
    class Learner:
        def status(self):
            return {"pending": 2, "last_run": 1.0,
                    "counts": {"applied": 3}, "processed": []}

    monkeypatch.setattr(agent_server, "_feedback_learner", lambda: Learner())
    with TestClient(app) as client:
        r = client.get("/api/feedback/learning")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending"] == 2
    assert body["counts"]["applied"] == 3
    assert "enabled" in body and "persona_autosave" in body
