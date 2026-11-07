"""The persona is Aria's evolving, AI-written portrait, not a memory render."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server  # noqa: E402
from agent_server import app  # noqa: E402
from persona import PersonaStore, SCAFFOLD  # noqa: E402


class FakeMemory:
    def __init__(self, items=None):
        self.items = list(items or [])

    def list_recent(self, **kwargs):
        return list(self.items)


def _record(kind, descriptor, *, drawer="", source="chat", value=None):
    return SimpleNamespace(kind=kind, descriptor=descriptor, drawer=drawer,
                           source=source, value=value or {})


def _store(tmp_path, chat_fn=None, memory=None):
    return PersonaStore(
        state_dir=tmp_path,
        chat_threads_path=tmp_path / "chat_threads.json",
        turn_logs_path=tmp_path / "turn_logs.json",
        feedback_path=tmp_path / "feedback.jsonl",
        memory_api=memory or FakeMemory(),
        chat_fn=chat_fn,
    )


def _write_interactions(tmp_path, *, include_probe=True):
    threads = {
        "ct-real": {"title": "Planning a migration", "updated": 1.0,
                    "messages": [
                        {"role": "user", "content": "I need to move a service without downtime."},
                        {"role": "assistant", "content": "Start with a shadow deployment."},
                    ]},
    }
    if include_probe:
        threads["ct-probe"] = {
            "title": "Playwright probe", "updated": 2.0,
            "messages": [{"role": "user", "content": "Reply with exactly: TEST"}],
        }
    (tmp_path / "chat_threads.json").write_text(json.dumps(threads),
                                                encoding="utf-8")
    (tmp_path / "turn_logs.json").write_text(json.dumps({
        "s8-real": [{"q": "Compare two migration strategies",
                     "a": "A canary is safer than a big bang.",
                     "ts": "2026-10-30T01:00:00"}],
        "s8-probe": [{"q": "What is the QX-7 vault code?",
                      "a": "NONE", "ts": "2026-10-30T01:01:00"}],
    }), encoding="utf-8")
    (tmp_path / "feedback.jsonl").write_text(json.dumps({
        "id": "fb-1", "t": 1.0, "session_id": "ct-real", "node_id": "",
        "msg_id": "m", "vote": -1, "skill": "chat",
        "note": "Too much preamble; I prefer the answer first.",
    }) + "\n", encoding="utf-8")


def test_scaffold_until_a_portrait_is_generated(tmp_path):
    store = _store(tmp_path)
    assert store.has_persona() is False
    assert "not enough interactions yet" in store.current_markdown()
    assert store.current_markdown() == SCAFFOLD


def test_generation_uses_previous_portrait_and_real_interactions(tmp_path):
    _write_interactions(tmp_path)
    prompts: list[str] = []

    def chat(prompt):
        prompts.append(prompt)
        return ("# Aria's read on you — 30 October 2026\n\n"
                "## How you come across\n\nYou seem to plan before acting.\n\n"
                "## How you think and work\n\nYou prefer a canary over a big bang.\n\n"
                "## How you like to be helped\n\nYou want the answer first.\n\n"
                "## What seems to matter to you\n\nContinuity.\n\n"
                "## How my read on you is changing\n\nEarly, but warming.\n\n"
                "## What I'm still unsure about\n\nHow much detail you want.\n\n"
                "This is a working impression; correct me if it is wrong.")

    memory = FakeMemory([
        _record("preference", "prefers the answer first"),
        _record("observation", "plans migrations carefully",
                value={"evidence": "I need to move a service without downtime"}),
        _record("fact", "works on service migrations"),
        _record("fact", "a document chunk that must not enter the portrait",
                drawer="document", source="document:doc-1"),
    ])
    store = _store(tmp_path, chat_fn=chat, memory=memory)
    result = store.generate("manual")

    assert result["ok"] is True
    assert result["version"] == 1
    assert store.has_persona() is True
    assert "plan before acting" in store.current_markdown()
    assert "Version 1" in store.evolution_markdown()
    assert store.meta()["version"] == 1
    assert store.meta()["based_on"]["interactions"] >= 2

    prompt = prompts[0]
    assert "no previous portrait" in prompt
    assert "Planning a migration" in prompt
    assert "canary" in prompt
    assert "I prefer the answer first" in prompt
    assert "a document chunk" not in prompt
    assert "QX-7" not in prompt
    assert "Reply with exactly" not in prompt

    # A second generation must see the first portrait and append version 2.
    second = store.generate("chat")
    assert second["ok"] is True
    assert second["version"] == 2
    assert "## How you come across" in prompts[1]
    assert "plan before acting" in prompts[1]  # previous portrait fed back
    evolution = store.evolution_markdown()
    assert "Version 1" in evolution and "Version 2" in evolution
    assert store.meta()["history"][-1]["version"] == 2


def test_grounding_pass_removes_unsupported_claims(tmp_path):
    _write_interactions(tmp_path)
    draft = ("# Aria's read on you — 30 October 2026\n\n"
             "## How you come across\n\nYou seem practical.\n\n"
             "## How you like to be helped\n\n"
             "You requested no emojis and every claim must carry a "
             "publication date.\n\n"
             "## What seems to matter to you\n\nClarity.\n\n"
             "This is a working impression.")
    cleaned = ("# Aria's read on you — 30 October 2026\n\n"
               "## How you come across\n\nYou seem practical.\n\n"
               "## How you like to be helped\n\n"
               "You seem to want the answer first.\n\n"
               "## What seems to matter to you\n\nClarity.\n\n"
               "This is a working impression; correct me if it is wrong.")
    calls: list[str] = []

    def chat(prompt):
        calls.append(prompt)
        return draft if len(calls) == 1 else cleaned

    store = _store(tmp_path, chat_fn=chat)
    store.critic_fn = chat
    result = store.generate("manual")
    assert result["ok"] is True
    assert len(calls) == 2
    assert "no emojis" not in store.current_markdown()
    assert "publication date" not in store.current_markdown()
    assert "answer first" in store.current_markdown()
    assert "SOURCES:" in calls[1]


def test_portrait_uses_friend_voice_not_the_user(tmp_path):
    from persona import PERSONA_PROMPT
    assert "second person" in PERSONA_PROMPT
    assert "you" in PERSONA_PROMPT and "your" in PERSONA_PROMPT

    # A third-person draft must be rejected rather than shipped, and the
    # previous portrait must survive.
    store = _store(
        tmp_path,
        chat_fn=lambda p: "# Aria's read on you — today\n\n"
                          "The user seems calm. " + ("You seem thoughtful. " * 12))
    result = store.generate("manual")
    assert result["ok"] is False
    assert "the user" in result["error"]
    assert store.has_persona() is False


def test_failed_generation_keeps_the_previous_portrait(tmp_path):
    store = _store(
        tmp_path,
        chat_fn=lambda p: "# Persona\n\nFirst version.\n\n"
                          + ("You seem thoughtful. " * 12))
    first = store.generate("manual")
    assert first["ok"]
    before = store.current_markdown()

    store.chat_fn = lambda p: (_ for _ in ()).throw(RuntimeError("gateway down"))
    failed = store.generate("chat")
    assert failed["ok"] is False
    assert store.current_markdown() == before
    assert store.meta()["version"] == 1

    store.chat_fn = lambda p: ""
    empty = store.generate("chat")
    assert empty["ok"] is False
    assert store.current_markdown() == before


def test_note_turn_counts_and_generation_resets_the_counter(tmp_path):
    store = _store(tmp_path, chat_fn=lambda p: "# Persona\n\n" + "x" * 200)
    assert store.note_turn() == (1, 1)
    assert store.note_turn() == (2, 2)
    store.generate("chat")
    assert store.meta()["turns_since_update"] == 0
    assert store.meta()["total_turns"] == 2


def test_regenerate_endpoint_reports_failure_and_success(monkeypatch):
    calls: list[str] = []

    def ok(reason="manual"):
        calls.append(reason)
        return {"ok": True, "version": 3, "markdown": "# Persona\n\nYou seem calm."}

    monkeypatch.setattr(agent_server, "_refresh_persona", ok)
    with TestClient(app) as client:
        r = client.post("/api/profile/persona/regenerate")
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 3
    assert calls == ["manual"]

    monkeypatch.setattr(agent_server, "_refresh_persona",
                        lambda reason="manual": {"ok": False, "error": "no model"})
    with TestClient(app) as client:
        r = client.post("/api/profile/persona/regenerate")
    assert r.status_code == 502
    assert "no model" in r.json()["error"]
