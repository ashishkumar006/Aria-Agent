import json

import pytest

import artifacts as arts
import mcp_server
import skills


def _big(n_words=3000):
    return "Findings with figures. " * n_words


def _upstream_entry(findings: str) -> dict:
    """An entry in the shape `resolve_inputs` actually produces.

    The old test built a FLAT dict — `{"question":..., "findings":...}` —
    which is not what any node receives. `resolve_inputs` nests an upstream
    node's whole result under the dict-valued `output` key
    (`{"id","kind","skill","output": {...}}`), so a flat entry tested a
    fiction: the spill loop walked top-level keys, never saw `output` because
    it was a dict rather than a string, and the entire spill subsystem was
    dead code in production while this test passed. Every entry here goes
    through `resolve_inputs` so the shape cannot drift again.
    """
    from schemas import AgentResult
    nodes = {"n:1": {"result": AgentResult(
        success=True, agent_name="researcher",
        output={"question": "indexing trade-offs",
                "findings": findings,
                "sources": [{"url": "http://s1", "title": "S1"}]})}}
    resolved = skills.resolve_inputs(["n:1"], nodes, "indexing trade-offs")
    assert resolved, "resolve_inputs returned nothing"
    assert isinstance(resolved[0].get("output"), dict), \
        "the fixture is not in the nested upstream shape"
    return resolved[0]


def test_a_real_upstream_node_spills_to_an_artifact(tmp_path, monkeypatch):
    """A large upstream result must be paid for once, not by every node that
    reads it: over the spill threshold it moves into the artifact store and is
    replaced by a handle + preview.

    This is the test that should have existed. The previous one hand-built a
    flat dict, so it exercised a code path production never takes — and the
    spill, the reason the content-addressed store exists, had never fired once
    in a real run.
    """
    monkeypatch.setattr(arts, "STORE", tmp_path / "artifacts")
    monkeypatch.setattr(skills.artifacts_svc, "STORE", tmp_path / "artifacts")
    big = _big()
    block = skills._inputs_block([_upstream_entry(big)], 400_000,
                                 skill_name="distiller")
    assert '"artifact"' in block, (
        "a real upstream node's long findings did not spill")
    assert "call read_artifact" in block
    field = json.loads(block)[0]["output"]["findings"]
    assert field["artifact"].startswith("art:")
    assert field["title"].endswith("findings")
    # The real byte count, which for UTF-8 is not the character count.
    assert field["bytes"] == len(big.encode("utf-8"))
    assert len(field["preview"]) < len(big) / 10


def test_the_spill_threshold_is_the_documented_one(tmp_path, monkeypatch):
    """Over ~24 KB is what the model is told (`read_artifact`'s docstring).
    The loop tested `len(val) > _FIELD_BUDGET` (60,000) before it reached
    the spill check, so the effective gate was 60k and a 30k field was hard
    -sliced instead of spilled."""
    monkeypatch.setattr(arts, "STORE", tmp_path / "artifacts")
    monkeypatch.setattr(skills.artifacts_svc, "STORE", tmp_path / "artifacts")
    mid = _big(n_words=1100)  # ~25k chars, above 24k, below 60k
    assert 24_000 < len(mid) < 60_000
    block = skills._inputs_block([_upstream_entry(mid)], 400_000,
                                 skill_name="distiller")
    assert '"artifact"' in block, (
        "a field between 24k and 60k was truncated instead of spilled - the "
        "documented threshold is not the real one")
    assert "chars of this field omitted" not in block


def test_large_upstream_field_spills_to_an_artifact(tmp_path, monkeypatch):
    """A large upstream result must be paid for once, not by every node
    that reads it: over ~24KB it moves into the artifact store and is
    replaced by a handle + preview."""
    monkeypatch.setattr(arts, "STORE", tmp_path / "artifacts")
    monkeypatch.setattr(skills.artifacts_svc, "STORE", tmp_path / "artifacts")
    entry = {"question": "indexing trade-offs", "findings": _big(),
             "sources": [{"url": "http://s1", "title": "S1"}]}
    block = skills._inputs_block([entry], 400_000, skill_name="distiller")
    assert '"artifact"' in block
    assert "call read_artifact" in block
    field = json.loads(block)[0]["findings"]
    assert field["artifact"].startswith("art:")
    assert field["title"] == "indexing trade-offs — findings"
    assert field["bytes"] == len(entry["findings"].encode("utf-8"))
    # Only the preview is inlined, so the saving is real.
    assert len(field["preview"]) < len(entry["findings"]) / 10


def test_a_small_result_stays_inline():
    """Below the threshold nothing is spilled - the text is right there.

    This test used to assert the opposite at 5.6KB, pinning the old 24KB
    threshold. The threshold is now 4KB, so 5.6KB legitimately spills: the
    text is preserved intact as an artifact with a readable preview, and
    `read_artifact` is in the consumer's tool surface. What must never happen
    is losing the text, which is what the pins below check.
    """
    entry = {"question": "q", "findings": "Short finding. " * 100}   # ~1.4KB
    block = skills._inputs_block([entry], 120_000, skill_name="distiller")
    assert '"artifact"' not in block
    assert "Short finding." in block


def test_spilling_never_discards_the_text():
    """Whatever crosses the threshold, the full field is recoverable."""
    entry = {"question": "q", "findings": "Short finding. " * 400}   # ~5.6KB
    block = skills._inputs_block([entry], 120_000, skill_name="distiller")
    field = json.loads(block)[0]["findings"]
    assert field["artifact"].startswith("art:")
    import artifacts as _arts
    stored = _arts.get_bytes(field["artifact"])
    assert stored.decode("utf-8") == entry["findings"]


def test_the_terminal_consumer_is_exempt_from_spilling():
    """The Formatter writes the report from this material, so handing it
    handles would degrade exactly the output the run exists to produce."""
    entry = {"question": "q", "findings": _big()}
    block = skills._inputs_block([entry], 120_000, skill_name="formatter")
    assert '"artifact"' not in block
    assert entry["findings"][:200] in block


def test_read_artifact_round_trips_and_pages(tmp_path, monkeypatch):
    # Own store: the shared one is repointed by other tests.
    monkeypatch.setattr(arts, "STORE", tmp_path / "artifacts")
    # Under the 20k default limit, so a single read returns all of it.
    payload = _big(200)
    handle = arts.put(payload.encode("utf-8"), content_type="text/plain",
                      source="test", descriptor="spill fixture")
    out = mcp_server.read_artifact(handle)
    assert out["ok"] is True
    assert out["size_bytes"] == len(payload)
    assert out["content"] == payload
    assert out["truncated"] is False
    assert out["next_offset"] is None

    # Paging: a big artifact is read in slices, and the slices reassemble.
    big = _big(3000)
    bh = arts.put(big.encode("utf-8"), content_type="text/plain",
                  source="test", descriptor="big fixture")
    page = mcp_server.read_artifact(bh, offset=0, limit=500)
    assert page["returned_chars"] == 500
    assert page["truncated"] is True
    assert page["next_offset"] == 500
    rest = mcp_server.read_artifact(bh, offset=500, limit=100_000)
    assert page["content"] + rest["content"] == big


def test_paging_never_splits_a_multibyte_character(tmp_path, monkeypatch):
    """Research text is rarely pure ASCII.

    `read_artifact` sliced the raw BYTES and then decoded with
    errors="replace", so any multi-byte character straddling a page boundary
    became U+FFFD permanently. The page count above was asserted on `_big()`,
    which is pure ASCII - so the corruption passed every test while serving
    the model mangled text on every real artifact over one page.
    """
    monkeypatch.setattr(arts, "STORE", tmp_path / "artifacts")
    text = ("f Findings — with em-dashes, naïve café, 日本語 "
            "and emoji 🎙 — repeated. ") * 120
    handle = arts.put(text.encode("utf-8"), content_type="text/plain",
                      source="test", descriptor="unicode fixture")
    assert len(text.encode("utf-8")) != len(text), (
        "fixture must be multi-byte to be a real test")

    offset, out, pages = 0, [], 0
    while pages < 50:
        page = mcp_server.read_artifact(handle, offset=offset, limit=200)
        out.append(page["content"])
        pages += 1
        if not page["truncated"]:
            break
        offset = page["next_offset"]

    joined = "".join(out)
    assert pages > 1, "fixture was small enough to fit in one page"
    assert "�" not in joined, "a multi-byte character was split by paging"
    assert joined == text


def test_read_artifact_rejects_bad_handles():
    assert mcp_server.read_artifact("nope")["ok"] is False
    assert mcp_server.read_artifact("art:zzz")["ok"] is False
    assert mcp_server.read_artifact("art:0000000000000000")["ok"] is False


def test_read_artifact_is_offered_to_nodes_that_consume_inputs():
    reg = skills.SkillRegistry()
    assert "read_artifact" in skills._TOOL_CATALOG
    # Nodes that read upstream results can expand a handle.
    for name in ("distiller", "summariser"):
        assert "read_artifact" in reg.get(name).tools_allowed, name
    payload = skills.tool_payload(["read_artifact"])
    assert payload and payload[0]["name"] == "read_artifact"
