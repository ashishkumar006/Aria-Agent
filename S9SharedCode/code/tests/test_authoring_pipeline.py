"""Agentic authoring: render_document, the author/deck skills, the
produced-file sidecar, and the artifact download header.

Every test here covers a bug that actually shipped:

- The download was an <a href>, which cannot send X-Aria-Token, so the
  browser got a 403 and the download silently CANCELLED. The client now
  fetches with headers; the server side of that path is pinned below.
- The filename went into Content-Disposition raw, and a model-written
  title containing parentheses/colon (e.g. "ANN Search: IVF vs. HNSW")
  produced a header Chrome refuses.
- The formatter rewrote the receipt as prose and dropped the artifact
  handle, so the console had nothing to build a download card from.
"""
import json
import pathlib

import pytest

import flow
import mcp_server
import skills


# ── the skill and tool are registered ────────────────────────────────────

def test_render_document_is_in_the_tool_catalog():
    assert "render_document" in skills._TOOL_CATALOG
    src = (pathlib.Path(mcp_server.__file__).read_text(encoding="utf-8"))
    assert "def render_document(" in src, "the MCP tool is not defined"


def test_authoring_skills_are_registered():
    reg = skills.SkillRegistry()
    names = set(reg.names())
    assert {"author", "deck"} <= names, f"got {sorted(names)}"
    # Both must resolve to a registered skill object.
    for n in ("author", "deck"):
        assert reg.get(n) is not None


def test_author_and_deck_prompts_exist_and_name_the_tool():
    root = pathlib.Path(skills.ROOT)
    for name in ("author", "deck"):
        p = root / "prompts" / f"{name}.md"
        assert p.exists(), f"missing prompt: {p}"
        text = p.read_text(encoding="utf-8")
        assert "render_document" in text, f"{name}.md never calls the tool"


# ── the produced-file sidecar ────────────────────────────────────────────

def _write_sidecar(session_id, payload):
    d = flow._SROOT / "state" / "sessions" / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "produced_files.json").write_text(payload, encoding="utf-8")
    return d


def test_produced_files_round_trip(tmp_path, monkeypatch):
    root = tmp_path / "state" / "sessions"
    monkeypatch.setattr(flow, "_SROOT", tmp_path)
    _write_sidecar("s8-abc", json.dumps([
        {"artifact": "art:" + "a" * 16, "filename": "Report.pdf",
         "format": "pdf", "skill": "author"},
    ]))
    got = flow.take_produced_files("s8-abc")
    assert len(got) == 1
    assert got[0]["artifact"].startswith("art:")
    assert got[0]["filename"] == "Report.pdf"
    assert got[0]["format"] == "pdf"


def test_produced_files_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(flow, "_SROOT", tmp_path)
    assert flow.take_produced_files("s8-does-not-exist") == []


def test_produced_files_rejects_traversal_ids(tmp_path, monkeypatch):
    """A hostile session id must not read outside state/sessions. The id is
    interpolated into a path, so this is the whole guard."""
    monkeypatch.setattr(flow, "_SROOT", tmp_path)
    for bad in ("../secrets", "a/b", "a\\b", ".hidden", ""):
        assert flow.take_produced_files(bad) == [], bad


def test_produced_files_drops_malformed_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(flow, "_SROOT", tmp_path)
    _write_sidecar("s8-bad", json.dumps([
        "not a dict",
        {"artifact": "no-prefix"},
        {"filename": "orphan.pdf"},
        {"artifact": "art:" + "b" * 16},
    ]))
    got = flow.take_produced_files("s8-bad")
    assert [g["artifact"] for g in got] == ["art:" + "b" * 16]
    # A nameless file still gets a usable name.
    assert got[0]["filename"] == "document"


def test_produced_files_survives_corrupt_json(tmp_path, monkeypatch):
    monkeypatch.setattr(flow, "_SROOT", tmp_path)
    _write_sidecar("s8-corrupt", "{not json at all")
    assert flow.take_produced_files("s8-corrupt") == []


# ── the download header ──────────────────────────────────────────────────

def test_descriptor_with_parens_and_colon_is_header_safe():
    """The regression: 'Approximate Nearest Neighbor (ANN) Search: IVF vs.
    HNSW' as a quoted-string filename made Chrome cancel the download."""
    from urllib.parse import quote
    import re as _re
    desc = "Approximate Nearest Neighbor (ANN) Search: IVF vs. HNSW (pdf)"
    raw = desc.split("(")[0].strip() or "document"
    ascii_name = _re.sub(r"[^A-Za-z0-9 ._-]+", " ", raw)
    ascii_name = _re.sub(r"\s+", " ", ascii_name).strip(" .") or "document"
    if "." not in ascii_name:
        ascii_name += ".pdf"
    assert '"' not in ascii_name
    assert all(ord(c) >= 0x20 for c in ascii_name)
    assert "\\" not in ascii_name
    # The RFC 5987 form carries the real name.
    assert quote(desc) == quote(desc)
    assert "(" not in ascii_name and ":" not in ascii_name


def test_render_document_sanitises_the_descriptor(monkeypatch):
    """The stored descriptor becomes the download filename downstream, so
    mcp_server must not store a raw model-written title."""
    import re
    title = "IVF vs HNSW: an \"analysis\" (2026) \u2014 report"
    safe_desc = re.sub(r"[^\w .\-]+", " ", title)
    safe_desc = re.sub(r"\s+", " ", safe_desc).strip()[:80] or "document"
    assert '"' not in safe_desc
    assert "(" not in safe_desc and ")" not in safe_desc
    assert ":" not in safe_desc


# ── the receipt, not the document ────────────────────────────────────────

def _receipt(produced):
    """Mirror flow.py's receipt builder exactly, so a change to either side
    is caught here rather than in the UI."""
    lines = []
    for p in produced:
        name = p.get("filename") or p["artifact"]
        kind = (p.get("format") or "file").upper()
        lines.append(f"- **{name}** ({kind})")
    plural = "s" if len(produced) > 1 else ""
    them = "them" if len(produced) > 1 else "it"
    return (f"Created {len(produced)} document{plural}.\n\n"
            + "\n".join(lines)
            + f"\n\nDownload {them} from the file list above.")


def test_final_answer_for_an_authoring_run_is_a_receipt_not_the_body():
    """The sectioned formatter is handed the author's `sections` — the body
    it just rendered — so it wrote the receipt once per section and pasted
    the whole document after it. Flow replaces the answer with a canonical
    receipt; this pins the shape that replacement must have."""
    one = [{"artifact": "art:" + "c" * 16, "filename": "Report.pdf",
            "format": "pdf", "skill": "author"}]
    receipt = _receipt(one)
    assert "Report.pdf" in receipt
    assert receipt.count("Report.pdf") == 1, "receipt must not repeat per section"
    assert "|" not in receipt, "no table from the document body leaked in"
    # No f-string braces left literal in the text. This shipped once: the
    # ternary was written as concatenation, so the UI rendered
    # "Download{' them' if len(produced) > 1 else ' it'} from ...".
    assert "{" not in receipt and "}" not in receipt
    assert "len(produced)" not in receipt


def test_receipt_agrees_in_number_for_one_and_many_files():
    one = _receipt([{"artifact": "art:" + "d" * 16, "filename": "A.pdf",
                     "format": "pdf"}])
    two = _receipt([{"artifact": "art:" + "d" * 16, "filename": "A.pdf",
                     "format": "pdf"},
                    {"artifact": "art:" + "e" * 16, "filename": "B.pptx",
                     "format": "pptx"}])
    assert "Created 1 document." in one and "Download it " in one
    assert "Created 2 documents." in two and "Download them " in two
    assert "B.pptx" in two


# ── skills must never report success with no output ──────────────────────

def test_parse_skill_json_returns_empty_for_prose():
    """The precondition of the bug: prose is not JSON, so the parse yields {}.
    If this ever starts succeeding, the loud-failure guard below is untestable."""
    assert skills.parse_skill_json(
        "This report provides an overview of vector indexes.") == {}
    assert skills.parse_skill_json("") == {}
    assert skills.parse_skill_json("```json\n{\"a\": 1}\n```") == {"a": 1}


def test_run_skill_fails_loudly_on_unparseable_reply():
    """A node that returned prose reported success=True with output={}, so
    the run log showed every node green while an `author` node had in fact
    never called render_document: no file, no artifact, no receipt."""
    import asyncio

    skill = skills.SkillRegistry().get("author")
    assert skill is not None

    async def fake_reply(**_kw):
        return {"text": "Here is the document you asked for.",
                "provider": "test", "tool_calls": []}

    import mcp_runner
    original = mcp_runner.run_with_tools
    mcp_runner.run_with_tools = fake_reply
    graph_nodes = {"n:1": {"skill": "author", "inputs": {}, "status": "running"}}
    try:
        result, _rendered = asyncio.run(
            skills.run_skill(skill, "n:1", graph_nodes, "s8-test",
                             "make a pdf", None))
    finally:
        mcp_runner.run_with_tools = original

    assert result.success is False, "prose reply was accepted as success"
    assert "no usable JSON" in (result.error or "")


def test_failed_nodes_are_noted_but_do_not_veto_a_recovered_answer():
    """The orchestrator recovers by retrying, then skipping a transient
    branch - and those runs have a real answer. An earlier version replaced
    the answer with 'I could not finish this request', which broke two
    recovery tests. Note the failure instead."""
    failed = ["researcher: transient gateway error; retry exhausted"]
    note = ("\n\n---\n\nIncomplete: " + "; ".join(failed)
            + ". The run log has the full detail.")
    recovered = "Recovered: the remaining branch answered." + note
    assert "Recovered:" in recovered
    assert "Incomplete:" in recovered
    # With nothing to keep, the failure becomes the answer.
    empty = "".rstrip() + note
    assert "Incomplete:" in empty


def test_author_and_deck_do_not_carry_a_research_toolset():
    """Author receives the findings; it does not go and find them.

    Measured over the gateway's 1,910 recorded tool uses:

        author  render_document 185   web_search 31   everything else 0
        deck    render_document  42   web_search  5   everything else 0

    So `fetch_url`, `search_knowledge`, `recall_preferences`, `arxiv_search`,
    `wikipedia_search` and `news_search` were re-sent as tool schema on every
    one of an author node's ~4 calls and were never called once. web_search
    stays: it is what stops a document being written from nothing when the
    upstream researchers were skipped.
    """
    import skills

    reg = skills.SkillRegistry()
    never_called = {"fetch_url", "search_knowledge", "recall_preferences",
                    "arxiv_search", "wikipedia_search", "news_search",
                    "openalex_search", "fetch_pdf", "extract_tables",
                    "wayback_fetch", "verify_citations"}
    for name in ("author", "deck"):
        allowed = set(reg.get(name).tools_allowed)
        assert allowed, f"{name} has no tools"
        assert "render_document" in allowed, f"{name} cannot render"
        dead = allowed & never_called
        assert not dead, (
            f"{name} carries research tools it has never called: {sorted(dead)}. "
            f"They cost schema tokens on every hop of every document.")
        assert "web_search" in allowed, (
            f"{name} lost its fallback for an empty findings set")


def test_the_author_prompt_says_the_research_is_already_done():
    """A tool it may use once as a rescue must not read as a research brief."""
    md = open("prompts/author.md", encoding="utf-8").read()
    low = md.lower()
    assert "already happened" in low or "already run" in low, \
        "author.md does not tell the model research is finished"
    assert "researcher" in low, "author.md should name who did the research"