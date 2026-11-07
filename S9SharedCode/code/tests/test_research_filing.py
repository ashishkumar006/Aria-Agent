"""A researcher's write-up has to be retrievable after the run ends.

Measured on two live runs: four researcher nodes produced 3.6k, 7.3k, 17.8k and
19.1k characters of findings, and none of it was stored anywhere. The pages they
read were filed under "Web sources", so the raw material survived and the
judgement about it did not — the next question about the same topic had to
re-research the web, which is the exact waste the artifact-store work exists to
stop.

These tests pin the filing: what the document contains, that its question rides
with the first block instead of becoming a header chunk of its own, and that it
lands in the Research folder with the session that made it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import artifacts  # noqa: E402
import flow  # noqa: E402
import gateway  # noqa: E402


@pytest.fixture(autouse=True)
def _private_store(tmp_path, monkeypatch):
    real = artifacts.STORE
    artifacts.STORE = tmp_path / "artifacts"
    artifacts.STORE.mkdir(parents=True, exist_ok=True)
    yield artifacts.STORE
    artifacts.STORE = real


OUT = {
    "question": "What changed in module prices in 2026?",
    "findings": "Module prices rose 26% since January.\n\nCells followed wafers.",
    "sources": [{"title": "PV price index", "url": "https://p.dev/index"},
                {"title": "Market note", "url": "https://m.dev/note"}],
    "caveats": ["Prices are spot, not contract."],
    "conflicts": [{"claim": "One source says 22%"}],
}


def test_the_document_carries_the_write_up_and_its_provenance():
    text = flow._research_findings_text(OUT)
    assert "Module prices rose 26%" in text
    assert "PV price index — https://p.dev/index" in text
    assert "Caveats:" in text and "Conflicts:" in text
    # The question is the first line, joined to the findings: a blank-separated
    # header would be its own chunk, and a chunk of nothing but a question
    # matches every search and answers none.
    assert text.startswith("What changed in module prices in 2026?\n"
                           "Module prices rose 26%")


def test_no_findings_is_not_filed():
    assert flow._research_findings_text({"question": "q", "findings": "  "}) == ""
    assert flow._research_findings_text({}) == ""


def test_structured_findings_are_serialised_not_dropped():
    """A researcher that returns a list of finding objects must not lose them to
    a `str()` of a Python repr."""
    text = flow._research_findings_text(
        {"question": "q", "findings": [{"claim": "43.1 GW installed"}]})
    assert "43.1 GW installed" in text
    assert "{'claim'" not in text


def test_filing_uses_the_research_folder_the_session_and_a_readable_name(
        monkeypatch):
    posts: list[dict] = []
    monkeypatch.setattr(gateway, "post_multipart",
                        lambda path, *, files, data=None, timeout=120.0:
                        posts.append({"path": path, "files": files,
                                      "data": data or {}}) or
                        {"document": {"id": "doc-research001"}})
    did = flow.file_research_findings("n_002", OUT, "s8-abc")
    assert did == "doc-research001"
    sent = posts[0]
    assert sent["data"]["origin"] == "research"
    assert sent["data"]["folder_name"] == "Research"
    assert sent["data"]["session_id"] == "s8-abc"
    assert sent["files"]["file"][0] == \
        "research-What-changed-in-module-prices-in-2026.txt"
    assert sent["data"]["origin_ref"].startswith("art:")
    # and the artifact keeps the link, so the run can point at the row
    art = sent["data"]["origin_ref"]
    assert artifacts.get_meta(art).document_id == "doc-research001"


def test_a_gateway_that_is_away_still_keeps_the_bytes(monkeypatch, capsys):
    """Filing is a convenience. The node already succeeded; losing the index
    must not look like losing the research."""
    monkeypatch.setattr(gateway, "post_multipart", lambda *a, **k: None)
    assert flow.file_research_findings("n_003", OUT, "s8-abc") == ""
    out = capsys.readouterr().out
    assert "not indexed" in out


def test_a_crash_in_filing_never_reaches_the_node(monkeypatch):
    monkeypatch.setattr(gateway, "post_multipart",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert flow.file_research_findings("n_004", OUT, "s8-abc") == ""


def test_the_same_findings_are_not_filed_twice(monkeypatch):
    posts: list = []
    monkeypatch.setattr(gateway, "post_multipart",
                        lambda path, *, files, data=None, timeout=120.0:
                        posts.append(files) or
                        {"document": {"id": "doc-research001"}})
    flow.file_research_findings("n_005", OUT, "s8-abc")
    flow.file_research_findings("n_006", OUT, "s8-abc")
    assert len(posts) == 1, "identical findings were uploaded twice"
