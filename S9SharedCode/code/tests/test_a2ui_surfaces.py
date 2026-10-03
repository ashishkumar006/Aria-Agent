"""Generated surfaces must be validated before anyone sees them, and rejected
loudly when they are not.

The two failure modes that matter:

  * A surface that reaches the client unvalidated. Everything in
    `test_a2ui_catalog.py` is an attack, and the catalog is only a defence if
    it is applied on the way OUT, not only when the catalog is read.
  * A surface that renders as though it were authoritative when it is wrong. So
    rejection must carry a specific, actionable message, and the model gets one
    retry with that message - not a generic 500.

Also covered: the model is fed a bounded, flattened context (it is model input,
so it is both a prompt-injection surface and a cost problem), and the
generation call has no tools, so producing a UI cannot reach anything.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import a2ui_catalog as C  # noqa: E402
import a2ui_surfaces as S  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import agent_server  # noqa: E402


GOOD = {
    "surfaceId": "runs_by_skill",
    "components": [
        {"id": "root", "component": "Column", "children": ["h", "s"]},
        {"id": "h", "component": "Text", "text": "Runs by skill",
         "variant": "heading"},
        {"id": "s", "component": "KeyValue", "label": "Total",
         "value": {"path": "/total"}},
    ],
    "data": {"total": 42},
}


def model_returning(payload):
    async def _call(system, user, max_tokens):
        return payload if isinstance(payload, str) else json.dumps(payload)
    return _call


# ── extracting JSON from a chatty model ──────────────────────────────────────

def test_plain_json_is_extracted():
    obj = S.extract_json_object(json.dumps(GOOD))
    assert obj and obj["surfaceId"] == "runs_by_skill"


def test_json_wrapped_in_a_fence_is_extracted():
    text = "Here you go:\n```json\n" + json.dumps(GOOD) + "\n```\nHope that helps!"
    obj = S.extract_json_object(text)
    assert obj and obj["surfaceId"] == "runs_by_skill"


def test_json_wrapped_in_prose_is_extracted():
    text = "Sure! " + json.dumps(GOOD) + " Let me know if you want changes."
    obj = S.extract_json_object(text)
    assert obj and obj["surfaceId"] == "runs_by_skill"


def test_braces_inside_strings_do_not_confuse_the_scanner():
    payload = {"surfaceId": "s", "components": [
        {"id": "root", "component": "Text", "text": 'a } b { c " d'}],
        "data": {}}
    obj = S.extract_json_object("noise " + json.dumps(payload) + " trailing")
    assert obj is not None
    assert obj["components"][0]["text"] == 'a } b { c " d'


def test_the_largest_object_wins_when_there_are_several():
    """A model that echoes the schema example and then its answer must not
    yield the example."""
    small = {"surfaceId": "a"}
    text = json.dumps(small) + "\n" + json.dumps(GOOD)
    obj = S.extract_json_object(text)
    assert obj["surfaceId"] == "runs_by_skill", obj


def test_prose_with_no_object_yields_none_rather_than_guessing():
    assert S.extract_json_object("I cannot generate that view.") is None
    assert S.extract_json_object("") is None
    assert S.extract_json_object(None) is None
    assert S.extract_json_object("{not json at all") is None


# ── the generation prompt ────────────────────────────────────────────────────

def test_the_prompt_carries_the_catalog_so_the_model_is_not_left_guessing():
    system, user = S.generation_prompt("show runs by skill")
    for name in C.CATALOG:
        assert name in system, f"{name} missing from the system prompt"
    assert C.CATALOG_ID in system + user
    # The request must be in the USER turn. With everything in the system
    # message and a placeholder user turn, the model read the exchange as
    # "a persona was assigned, nothing was asked" and answered
    # "I'm ready! What are we doing?" — observed live.
    assert "show runs by skill" in user


def test_the_prompt_states_the_read_only_constraint():
    system, _user = S.generation_prompt("x")
    assert "no text input" in system.lower()
    assert "rejected" in system.lower()


def test_the_prompt_asks_for_the_wire_shape():
    system, _user = S.generation_prompt("x")
    assert '"catalogId"' in system
    assert '"components"' in system
    # Explicit about the delimiters: several models will otherwise prepend a
    # sentence of preamble.
    assert "Start with `{`" in system


# ── generation ───────────────────────────────────────────────────────────────

def test_a_good_surface_is_returned_with_its_wire_messages():
    import asyncio
    out = asyncio.run(S.generate_surface(
        "show runs by skill", {"summary": "total: 42"},
        call_model=model_returning(GOOD)))
    assert out["ok"] is True
    assert out["attempts"] == 1
    # The A2UI message order is mandatory: a surface must exist before it is
    # updated.
    kinds = [next(iter(m)) for m in out["messages"]]
    assert kinds[0] == "version"
    inner = [k for m in out["messages"] for k in m if k != "version"]
    assert inner[0] == "createSurface"
    assert inner[1] == "updateComponents"
    assert inner[2] == "updateDataModel"


def test_a_surface_with_an_invented_component_is_retried_then_rejected():
    """One retry with the specific error, then a refusal that names it."""
    import asyncio
    bad = {"surfaceId": "s", "components": [
        {"id": "root", "component": "Script", "source": "x"}]}
    seen: list[str] = []

    async def _call(system, user, max_tokens):
        seen.append(system + "||" + user)
        return json.dumps(bad)

    try:
        asyncio.run(S.generate_surface("x", call_model=_call))
    except S.GenerationRejected as e:
        assert "usable surface" in str(e)
        assert "unknown component" in (e.detail or "")
    else:
        raise AssertionError("an invalid surface must be rejected")
    # Two calls: the original plus one retry.
    assert len(seen) == S.MAX_RETRIES + 1
    # The retry prompt carries the diagnosis, so a retry can actually succeed.
    assert "unknown component" in seen[-1]


def test_a_retry_that_produces_a_valid_surface_succeeds():
    import asyncio
    bad = {"surfaceId": "s", "components": [
        {"id": "root", "component": "Nope"}]}
    calls = {"n": 0}

    async def _call(system, user, max_tokens):
        calls["n"] += 1
        return json.dumps(bad if calls["n"] == 1 else GOOD)

    out = asyncio.run(S.generate_surface("x", call_model=_call))
    assert out["ok"] is True
    assert out["attempts"] == 2


def test_prose_instead_of_json_is_reported_as_such():
    import asyncio
    try:
        asyncio.run(S.generate_surface(
            "x", call_model=model_returning("I'd rather not.")))
    except S.GenerationRejected as e:
        assert "no JSON object" in (e.detail or "")
    else:
        raise AssertionError("a prose reply must be rejected")


def test_an_empty_request_is_refused_before_any_model_call():
    import asyncio
    called = {"n": 0}

    async def _call(system, user, max_tokens):
        called["n"] += 1
        return "{}"

    try:
        asyncio.run(S.generate_surface("   ", call_model=_call))
    except S.GenerationRejected as e:
        assert "describe the view" in str(e)
    else:
        raise AssertionError("an empty request must be refused")
    assert called["n"] == 0, "no tokens should be spent on an empty request"


def test_an_oversized_request_is_refused_before_any_model_call():
    import asyncio
    called = {"n": 0}

    async def _call(system, user, max_tokens):
        called["n"] += 1
        return "{}"

    try:
        asyncio.run(S.generate_surface("x" * (S.MAX_QUERY + 1), call_model=_call))
    except S.GenerationRejected:
        pass
    else:
        raise AssertionError("an oversized request must be refused")
    assert called["n"] == 0


def test_no_model_configured_is_an_error_not_a_crash():
    import asyncio
    try:
        asyncio.run(S.generate_surface("x"))
    except S.GenerationRejected as e:
        assert "no model configured" in str(e)
    else:
        raise AssertionError("expected a rejection")


# ── the reviewable summary ───────────────────────────────────────────────────

def test_a_surface_has_a_textual_form_so_it_can_be_reviewed():
    """A generated UI that can only be checked by rendering it is one nobody
    checks."""
    surface = C.validate_surface(GOOD)
    out = S.surface_summary(surface)
    assert out["componentCount"] == 3
    assert out["components"]["Column"] == 1
    assert out["components"]["Text"] == 1
    assert "Runs by skill" in out["text"]
    assert "dataKeys" in out


def test_the_summary_does_not_leak_internal_bookkeeping():
    surface = C.validate_surface(GOOD)
    out = S.surface_summary(surface)
    assert "_children" not in json.dumps(out)


# ── context handling ─────────────────────────────────────────────────────────

def test_caller_context_is_flattened_and_bounded():
    """Context is model input: an unbounded blob is both an injection surface and
    a cost problem, and a nested object must not be inlined wholesale."""
    big = {"k": "v" * 10_000, "arr": list(range(5000)),
           "nested": {"a": {"b": {"c": 1}}}}
    out = agent_server._a2ui_context_summary(big)
    assert len(out) <= 2000, len(out)
    assert "5000 items" in out
    assert "1 keys" in out, out
    # A single huge value must not consume the whole budget. It is truncated to
    # a short prefix - enough to be useful, not enough to crowd out every other
    # field - and the ellipsis says it was cut.
    assert "vvvv" in out, "a truncated prefix should still be shown"
    assert "…" in out
    assert "v" * 300 not in out, "a value must not be inlined whole"


def test_empty_context_is_stated_rather_than_silently_blank():
    assert agent_server._a2ui_context_summary(None) == "(no data)"
    assert agent_server._a2ui_context_summary({}) == "(no data)"


# ── endpoints ────────────────────────────────────────────────────────────────

def test_catalog_endpoint_serves_the_document():
    r = TestClient(agent_server.app).get("/api/a2ui/catalog")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["catalogId"] == C.CATALOG_ID
    assert "Column" in doc["components"]
    assert doc["limits"]["maxComponents"] == C.MAX_COMPONENTS


def test_validate_endpoint_accepts_a_good_surface():
    r = TestClient(agent_server.app).post("/api/a2ui/validate", json=GOOD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["summary"]["componentCount"] == 3


def test_validate_endpoint_rejects_a_bad_surface_with_422_and_a_reason():
    """422 not 400: the JSON was well-formed, the content was not. A client
    needs to tell "retry" from "give up"."""
    r = TestClient(agent_server.app).post("/api/a2ui/validate", json={
        "surfaceId": "s",
        "components": [{"id": "root", "component": "Script"}]})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["ok"] is False
    assert "unknown component" in body["error"]


def test_generate_endpoint_rejects_a_non_object_context():
    r = TestClient(agent_server.app).post(
        "/api/a2ui/generate", json={"request": "x", "context": []})
    assert r.status_code == 400, r.text


def test_generate_endpoint_requires_a_request():
    r = TestClient(agent_server.app).post("/api/a2ui/generate", json={})
    assert r.status_code in (400, 422), r.text
