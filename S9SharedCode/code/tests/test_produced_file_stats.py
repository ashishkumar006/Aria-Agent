"""The done frame must carry what the renderer actually reported.

`take_produced_files` read the sidecar and dropped `bytes` and `stats`, so
every file in the SSE `done` frame arrived with no size and no word count.
That is the signal both the model and the console use to tell a real document
from a stub.
"""
import json
import pathlib

import pytest

import artifacts as arts
import flow


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A session whose sidecar names two real artifacts."""
    monkeypatch.setattr(flow, "_SROOT", tmp_path)
    sid = "s8-testbytes"
    d = tmp_path / "state" / "sessions" / sid
    d.mkdir(parents=True)
    full = arts.put(b"%PDF-1.4 " + b"x" * 4000, content_type="application/pdf",
                    source="author:pdf", descriptor="Full (pdf)")
    stub = arts.put(b"%PDF-1.4 " + b"y" * 200, content_type="application/pdf",
                    source="author:pdf", descriptor="Stub (pdf)")
    (d / "produced_files.json").write_text(json.dumps([
        {"artifact": full, "filename": "Full.pdf", "format": "pdf",
         "skill": "author", "bytes": 4012, "blocks": 16,
         "stats": {"words": 620, "sections": 2, "format": "pdf"}},
        {"artifact": stub, "filename": "Stub.pdf", "format": "pdf",
         "skill": "author", "bytes": 208, "blocks": 1,
         "stats": {"words": 9, "sections": 0, "format": "pdf"}},
    ]), encoding="utf-8")
    return sid, full, stub


def test_bytes_and_stats_survive_the_read(session):
    sid, full, stub = session
    files = flow.take_produced_files(sid)
    assert len(files) == 2, files
    by_art = {f["artifact"]: f for f in files}
    full_f = by_art[full]
    assert full_f["bytes"] == 4012, full_f
    assert full_f["stats"]["words"] == 620, full_f
    assert full_f["blocks"] == 16, full_f


def test_the_stub_is_visibly_a_stub_in_the_frame(session):
    """The 9-word shell has to be recognisable, not just downloadable."""
    sid, _full, stub = session
    files = {f["artifact"]: f for f in flow.take_produced_files(sid)}
    s = files[stub]
    assert s["stats"]["words"] == 9, s
    assert s["stats"]["sections"] == 0, s
    assert s["bytes"] == 208, s


def test_legacy_entries_without_stats_do_not_crash(monkeypatch, tmp_path):
    """Sidecars written before these fields existed must still load."""
    monkeypatch.setattr(flow, "_SROOT", tmp_path)
    sid = "s8-legacy"
    d = tmp_path / "state" / "sessions" / sid
    d.mkdir(parents=True)
    art = arts.put(b"%PDF-1.4 legacy", content_type="application/pdf",
                   source="author:pdf", descriptor="Legacy (pdf)")
    (d / "produced_files.json").write_text(json.dumps([
        {"artifact": art, "filename": "L.pdf", "format": "pdf", "skill": "author"},
    ]), encoding="utf-8")
    files = flow.take_produced_files(sid)
    assert len(files) == 1, files
    assert files[0]["bytes"] is None
    assert files[0]["stats"] == {}


def test_unresolvable_handles_are_still_dropped(session, monkeypatch):
    """The verification guard must survive the added fields."""
    sid, _f, _s = session
    d = pathlib.Path(flow._SROOT) / "state" / "sessions" / sid
    data = json.loads((d / "produced_files.json").read_text(encoding="utf-8"))
    data.append({"artifact": "art:deadbeefdeadbeef", "filename": "Fake.pdf",
                 "format": "pdf", "skill": "author", "bytes": 1,
                 "stats": {"words": 1}})
    (d / "produced_files.json").write_text(json.dumps(data), encoding="utf-8")
    arts_ids = {f["artifact"] for f in flow.take_produced_files(sid)}
    assert "art:deadbeefdeadbeef" not in arts_ids