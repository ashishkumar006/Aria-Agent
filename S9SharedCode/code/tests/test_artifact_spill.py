import json

import pytest

import artifacts as arts
import mcp_server
import skills


def _big(n_words=3000):
    return "Findings with figures. " * n_words


def test_large_upstream_field_spills_to_an_artifact(tmp_path, monkeypatch):
    """A large upstream result must be paid for once, not by every node
    that reads it: over ~24KB it moves into the artifact store and is
    replaced by a handle + preview."""
    monkeypatch.setattr(arts, "STORE", tmp_path / "artifacts")
    monkeypatch.setattr(skills.artifacts_svc, "STORE", tmp_path / "artifacts")
    entry = {"question": "indexing trade-offs", "findings": _big(),
             "sources": [{"url": "http://s1", "title": "S1"}]}
    block = skills._inputs_block([entry], 120_000, skill_name="distiller")
    assert '"artifact"' in block
    assert "call read_artifact" in block
    field = json.loads(block)[0]["findings"]
    assert field["artifact"].startswith("art:")
    assert field["title"] == "indexing trade-offs — findings"
    assert field["bytes"] == len(entry["findings"])
    # Only the preview is inlined, so the saving is real.
    assert len(field["preview"]) < len(entry["findings"]) / 10


def test_a_normal_sized_result_is_never_spilled():
    """Spilling at 4KB (the first proposal) would turn almost every node
    output into a handle; only genuine outliers should spill."""
    entry = {"question": "q", "findings": "Short finding. " * 400}
    block = skills._inputs_block([entry], 120_000, skill_name="distiller")
    assert '"artifact"' not in block
    assert "Short finding." in block


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
