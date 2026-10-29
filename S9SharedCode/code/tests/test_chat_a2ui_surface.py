"""Chat A2UI surfaces must be renderable, not merely parsed.

The chat path used to accept any ```json object with `components` and hand it
to the console. The prompt advertised components the console does not have
(Button/TextField/Dropdown/...), so a model that followed it produced a
surface where only the Text nodes appeared - "A2UI does not render". These
tests pin the contract at the boundary: whatever survives `_extract_a2ui_surface`
is in the shape `a2ui_catalog.validate_surface` guarantees, and a surface that
cannot survive is turned into readable text instead of a raw JSON dump.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_server as A  # noqa: E402


def fenced(obj: dict) -> str:
    return "```json\n" + json.dumps(obj, ensure_ascii=False) + "\n```"


def test_a_canonical_surface_survives_and_carries_a_root():
    answer = fenced({
        "surfaceId": "s-1",
        "rootComponent": "root",
        "components": [
            {"id": "root", "component": "Column",
             "children": ["h", "t"], "gap": "normal"},
            {"id": "h", "component": "Text", "variant": "heading",
             "text": "Scheduled tasks"},
            {"id": "t", "component": "Table",
             "columns": [{"key": "query", "label": "Task"}],
             "rows": [{"query": "stretch"}]},
        ],
    })
    surface = A._extract_a2ui_surface(answer)
    assert surface is not None
    assert surface["rootComponent"] == "root"
    assert [c["component"] for c in surface["components"]] == [
        "Column", "Text", "Table"]
    # The renderer reads nested `props`; the catalog's flat shape is converted.
    table = surface["components"][2]
    assert table["props"]["columns"][0]["label"] == "Task"


def test_the_old_chat_shape_is_normalised_instead_of_refused():
    """type/props, Button+onPress, gap "small", Card.children and decorative
    properties are all shapes the old prompt taught. They must not cost the
    user the whole surface."""
    answer = fenced({
        "surfaceId": "s-2",
        "catalogId": "default",
        "rootComponent": "root",
        "components": [
            {"id": "root", "type": "Column",
             "props": {"goal": "ignored", "children": ["h", "btn", "card"],
                       "gap": "small"}},
            {"id": "h", "type": "Text",
             "props": {"variant": "heading", "text": "Panel"}},
            {"id": "btn", "type": "Button",
             "props": {"label": "Re-run",
                       "onPress": {"action": "rerun",
                                   "args": {"query": "q"}}}},
            {"id": "card", "type": "Card",
             "props": {"children": ["body"], "elevation": 1}},
            {"id": "body", "type": "Text", "props": {"text": "hello"}},
        ],
    })
    surface = A._extract_a2ui_surface(answer)
    assert surface is not None
    by_id = {c["id"]: c for c in surface["components"]}
    assert by_id["root"]["props"]["gap"] == "tight"
    assert "goal" not in by_id["root"]["props"]
    assert by_id["btn"]["component"] == "Action"
    assert by_id["btn"]["props"]["action"] == "rerun"
    assert by_id["btn"]["props"]["args"] == {"query": "q"}
    assert by_id["card"]["props"]["children"] == ["body"]
    assert "elevation" not in by_id["card"]["props"]


def test_table_title_is_folded_into_label():
    answer = fenced({
        "surfaceId": "s-3",
        "rootComponent": "root",
        "components": [
            {"id": "root", "component": "Column", "children": ["t"]},
            {"id": "t", "component": "Table",
             "columns": [{"key": "a", "title": "A", "width": "90px"}],
             "rows": [{"a": "1"}]},
        ],
    })
    surface = A._extract_a2ui_surface(answer)
    assert surface is not None
    col = surface["components"][1]["props"]["columns"][0]
    assert col["label"] == "A"
    assert "title" not in col
    assert "width" not in col


def test_a_non_root_id_is_renamed_when_the_payload_names_it():
    answer = fenced({
        "surfaceId": "s-4",
        "rootComponent": "main",
        "components": [
            {"id": "main", "component": "Column", "children": ["t"]},
            {"id": "t", "component": "Text", "text": "x"},
        ],
    })
    surface = A._extract_a2ui_surface(answer)
    assert surface is not None
    assert {c["id"] for c in surface["components"]} == {"root", "t"}


def test_an_unknown_component_falls_back_to_text_not_json():
    answer = ("Here is the panel:\n" + fenced({
        "surfaceId": "s-5",
        "rootComponent": "root",
        "components": [
            {"id": "root", "component": "Column", "children": ["tf", "h"]},
            {"id": "tf", "component": "TextField",
             "label": "Name", "value": ""},
            {"id": "h", "component": "Text", "text": "Readable heading"},
        ],
    }))
    assert A._extract_a2ui_surface(answer) is None
    cleaned = A._strip_a2ui_block(answer)
    assert "```" not in cleaned
    assert "Readable heading" in cleaned
    assert '"components"' not in cleaned


def test_an_ordinary_code_block_is_not_treated_as_a_surface():
    answer = 'Example:\n```json\n{"a": 1}\n```'
    assert A._extract_a2ui_surface(answer) is None
    assert A._strip_a2ui_block(answer) == answer


def test_the_chat_prompt_speaks_the_catalog_and_nothing_else():
    prompt = A._chat_a2ui_contract()
    for real in ("Column", "Row", "Card", "Text", "KeyValue", "Table",
                 "Action", "DataList"):
        assert real in prompt, real
    for phantom in ("TextField", "Dropdown", "Checkbox", "Slider",
                    "onPress", '"type"', "catalogId"):
        assert phantom not in prompt, phantom
    # The action payload is part of the contract the server validates.
    assert "args" in prompt
    assert "drive_app" in prompt


# ── near-miss payloads ─────────────────────────────────────────────────────
# A live dashboard came back as 5 KB of raw JSON because one component was
# closed with an extra `}`. A syntax slip, an enum synonym and a child
# reference holding prose each used to cost the ENTIRE surface.

def test_one_extra_closing_brace_does_not_cost_the_surface():
    """The exact shape from the live JEE dashboard: `...}}` on the component,
    then a stray `}` on the next line."""
    body = '''{
  "surfaceId": "jee-dashboard",
  "rootComponent": "root",
  "components": [
    {"id": "root", "component": "Column", "children": ["a", "b"]},
    {"id": "a", "component": "Action", "label": "Start", "action": "rerun",
     "args": {"query": "x"}}
    },
    {"id": "b", "component": "Text", "text": "still here"}
  ]
}'''
    answer = "Here you go:\n```json\n" + body + "\n```\nThat is the dashboard."
    surface = A._extract_a2ui_surface(answer)
    assert surface is not None
    ids = [c["id"] for c in surface["components"]]
    assert ids == ["root", "a", "b"]
    assert surface["components"][2]["props"]["text"] == "still here"
    # And the answer text no longer contains the payload.
    assert '"surfaceId"' not in A._strip_a2ui_block(answer)


def test_a_trailing_comma_and_a_literal_newline_are_repaired():
    answer = '```json\n{"surfaceId": "s", "rootComponent": "root",\n' \
             ' "components": [{"id": "root", "component": "Text",\n' \
             ' "text": "line one\nline two"},]}\n```'
    surface = A._extract_a2ui_surface(answer)
    assert surface is not None
    assert "line one\nline two" in surface["components"][0]["props"]["text"]


def test_presentational_synonyms_are_folded_not_refused():
    answer = fenced({
        "surfaceId": "s-tone",
        "rootComponent": "root",
        "components": [
            {"id": "root", "component": "Column", "children": ["h", "kv", "act"]},
            {"id": "h", "component": "Text", "variant": "title",
             "text": "Dashboard"},
            {"id": "kv", "component": "KeyValue", "label": "Progress",
             "value": "0%", "tone": "success"},
            {"id": "act", "component": "Action", "label": "Go",
             "variant": "secondary", "action": "rerun", "args": {}},
        ],
    })
    surface = A._extract_a2ui_surface(answer)
    assert surface is not None
    by_id = {c["id"]: c for c in surface["components"]}
    assert by_id["h"]["props"]["variant"] == "heading"
    assert by_id["kv"]["props"]["tone"] == "good"
    # An Action's own enum keeps its meaning: "secondary" is not a synonym for
    # a Text caption.
    assert by_id["act"]["props"]["variant"] == "secondary"


def test_a_child_holding_prose_becomes_text_and_a_typo_is_dropped():
    """`child: "150 questions"` is content; `children: ["heding"]` is a typo.
    The first must be shown, the second must not be rendered as a word."""
    answer = fenced({
        "surfaceId": "s-child",
        "rootComponent": "root",
        "components": [
            {"id": "root", "component": "Row",
             "children": ["easy-card", "heding"]},
            {"id": "easy-card", "component": "Card", "title": "Easy (30%)",
             "child": "150 questions"},
        ],
    })
    surface = A._extract_a2ui_surface(answer)
    assert surface is not None
    by_id = {c["id"]: c for c in surface["components"]}
    card = by_id["easy-card"]
    assert card["props"]["child"] == "easy-card-text"
    assert by_id["easy-card-text"]["props"]["text"] == "150 questions"
    # The typo'd id is gone, and no Text node was minted for it.
    assert by_id["root"]["props"]["children"] == ["easy-card"]
    assert not any("heding" in str(c["props"].get("text", ""))
                   for c in surface["components"])


def test_a_surface_block_that_will_not_parse_is_still_removed():
    """Last line of defence: never show the payload. Even an unparseable
    surface-shaped block is stripped rather than rendered verbatim."""
    answer = (
        "Here is the panel:\n```json\n"
        '{"surfaceId": "x", "components": [ {id root '
        '"component" Column children not json at all\n'
        "```\nTell me if you want changes."
    )
    assert A._extract_a2ui_surface(answer) is None
    cleaned = A._strip_a2ui_block(answer)
    assert "surfaceId" not in cleaned
    assert "components" not in cleaned
    assert "Tell me if you want changes." in cleaned


# ── the DAG engine ─────────────────────────────────────────────────────────
# The formatter prompt offers an `a2ui` envelope, and that formatter is the
# terminal node of a DAG run too. Before this, `final_answer: null` left the
# answer empty and the last-resort branch dumped the raw output dict into the
# transcript - the same payload leak the lightweight path no longer has.

_SURFACE_ENVELOPE = {
    "surfaceId": "chat-dag-1",
    "rootComponent": "root",
    "components": [
        {"id": "root", "component": "Column", "children": ["h"]},
        {"id": "h", "component": "Text", "variant": "heading",
         "text": "Scheduled tasks"},
    ],
}


class _FakeLLM:
    def __init__(self, script: dict):
        self._script = script
        self.calls: list[str] = []

    def chat(self, prompt=None, *, messages=None, system=None, agent=None,
             session=None, **kw):
        skill = agent or "formatter"
        self.calls.append(skill)
        payload = self._script.get(skill, {"final_answer": f"answer from {skill}"})
        return {"text": json.dumps(payload), "provider": "fake",
                "input_tokens": 10, "output_tokens": 10}


def _run_dag(script: dict, *, session_id: str, run_kind: str) -> str:
    import flow
    import mcp_runner

    fake = _FakeLLM(script)

    async def _fake_run_with_tools(*, prompt=None, tools_payload=None,
                                   agent=None, session_id=None, **kw):
        reply = fake.chat(prompt=prompt, agent=agent, session=session_id)
        return {**reply, "tool_calls": []}

    with mock.patch.object(flow, "ensure_gateway", lambda: None), \
         mock.patch("skills.LLM", lambda: fake), \
         mock.patch.object(mcp_runner, "run_with_tools", _fake_run_with_tools):
        return asyncio.run(flow.Executor().run(
            "show my scheduled tasks", session_id=session_id,
            run_kind=run_kind))


def test_a_dag_chat_formatter_surface_arrives_as_a_validatable_block():
    answer = _run_dag({
        "planner": {"nodes": [
            {"skill": "formatter", "inputs": ["USER_QUERY"], "metadata": {}}]},
        "formatter": {"final_answer": None, "a2ui": _SURFACE_ENVELOPE},
    }, session_id="t-a2ui-dag-chat", run_kind="chat")

    surface = A._extract_a2ui_surface(answer)
    assert surface is not None, answer[:400]
    assert surface["rootComponent"] == "root"
    assert [c["component"] for c in surface["components"]] == [
        "Column", "Text"]
    # And the answer is NOT a raw dict the user could be shown: stripping the
    # block leaves nothing behind, because the surface replaces the text.
    stripped = A._strip_a2ui_block(answer)
    assert "components" not in stripped


def test_a_dag_chat_formatter_still_answers_in_prose():
    answer = _run_dag({
        "planner": {"nodes": [
            {"skill": "formatter", "inputs": ["USER_QUERY"], "metadata": {}}]},
        "formatter": {"final_answer": "Three tasks are scheduled."},
    }, session_id="t-a2ui-dag-prose", run_kind="chat")
    assert answer.strip() == "Three tasks are scheduled."
    assert A._extract_a2ui_surface(answer) is None


# ── stored turns ──────────────────────────────────────────────────────────
# The reported turn was already on disk as raw JSON. Fixing the extractor
# only helps NEW turns unless reading a thread repairs the old ones, and the
# user re-opens exactly the message they complained about.

_STORED_BROKEN = '''I'll create an interactive JEE dashboard for you.

```json
{
  "surfaceId": "jee-dashboard",
  "rootComponent": "root",
  "components": [
    {"id": "root", "component": "Column", "children": ["h", "easy"]},
    {"id": "h", "component": "Text", "variant": "heading",
     "text": "JEE Preparation Dashboard"},
    {"id": "easy", "component": "Card", "title": "Easy (30%)",
     "child": "150 questions"}}
  ]
}
```

Would you like me to customize any section?'''


def test_a_stored_raw_payload_is_healed_into_a_surface_on_read():
    msg = {"role": "assistant", "content": _STORED_BROKEN}
    healed = A._heal_stored_surface(msg)
    assert healed["surface"] is not None
    assert healed["surface"]["surfaceId"] == "jee-dashboard"
    # The block is gone from the text; the prose the model wrote is kept.
    assert "surfaceId" not in healed["content"]
    assert "I'll create an interactive JEE dashboard" in healed["content"]
    assert "Would you like me to customize" in healed["content"]
    # The input dict is not mutated: healing is a read-time view.
    assert "surface" not in msg


def test_healing_leaves_ordinary_prose_and_surfaces_alone():
    plain = {"role": "assistant", "content": 'Here is a ```json\n{"a": 1}\n``` sample.'}
    assert A._heal_stored_surface(plain) is plain
    already = {"role": "assistant", "content": "x", "surface": {"surfaceId": "s"}}
    assert A._heal_stored_surface(already) is already
    user = {"role": "user", "content": _STORED_BROKEN}
    assert A._heal_stored_surface(user) is user


def test_get_chat_thread_returns_the_healed_surface():
    from starlette.testclient import TestClient

    cid = "ct-test-heal-a2ui"
    with A._CHAT_THREADS_LOCK:
        data = A._chat_threads_load()
        data[cid] = {"title": "heal", "updated": 0, "mode": "light",
                     "messages": [{"role": "user", "content": "dashboard please"},
                                  {"role": "assistant",
                                   "content": _STORED_BROKEN}]}
        A._chat_threads_save(data)
    try:
        client = TestClient(A.app)
        body = client.get(f"/api/chat/threads/{cid}").json()
        msg = body["messages"][-1]
        assert msg["surface"]["surfaceId"] == "jee-dashboard"
        assert "surfaceId" not in msg["content"]
        # And the stored file is untouched - this repairs the view, not data.
        with A._CHAT_THREADS_LOCK:
            raw = A._chat_threads_load()[cid]["messages"][-1]
        assert raw["content"] == _STORED_BROKEN
        assert not raw.get("surface")
    finally:
        TestClient(A.app).delete(f"/api/chat/threads/{cid}")


# ---------------------------------------------------------------------------
# History replay: a surface-only turn is stored with `content: ""`, and strict
# providers reject the whole request if that blank assistant row is replayed
# ("must have non-empty content or tool calls"). Because the same history is
# sent on every later turn, one such row made the thread fail forever with a
# 502. The model must instead receive the surface as text.
# ---------------------------------------------------------------------------

_SURFACE_ONLY = {
    "surfaceId": "tasks",
    "rootComponent": "root",
    "components": [
        {"id": "root", "component": "Column", "children": ["h", "t"]},
        {"id": "h", "component": "Text", "variant": "heading",
         "text": "Scheduled tasks"},
        {"id": "t", "component": "Table",
         "columns": [{"key": "title", "label": "Task"}],
         "rows": [{"title": "stretch"}]},
    ],
}


def test_a_surface_only_turn_reaches_the_model_as_text_not_an_empty_row():
    history = [{"role": "user", "content": "show my tasks"},
               {"role": "assistant", "content": "", "surface": _SURFACE_ONLY},
               {"role": "user", "content": "and the second one?"}]
    out = A._chat_history_for_model(history)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assistant = out[1]
    assert assistant["content"].strip(), "empty assistant turn would 400"
    assert "Scheduled tasks" in assistant["content"]
    assert "stretch" in assistant["content"]
    assert "surfaceId" not in assistant["content"]


def test_a_turn_that_is_empty_after_shaping_is_dropped_not_replayed():
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "   "},
               {"role": "user", "content": "still there?"}]
    out = A._chat_history_for_model(history)
    assert [m["role"] for m in out] == ["user", "user"]
    assert all(m["content"].strip() for m in out)


def test_prose_beside_a_surface_is_kept_without_the_digest():
    history = [{"role": "assistant", "content": "Here is your dashboard.",
                "surface": _SURFACE_ONLY}]
    out = A._chat_history_for_model(history)
    assert out == [{"role": "assistant", "content": "Here is your dashboard."}]

