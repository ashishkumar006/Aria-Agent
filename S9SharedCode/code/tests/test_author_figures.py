"""The author is told which figures exist, and which blocks it may use.

Two separate gaps, both invisible from the renderer's side:

  * `docgen` has always been able to draw a `chart` and lift an `image` out of
    an uploaded document, but the SECTION prompt — the path every long document
    actually takes — listed six block types and warned that anything else is
    silently dropped. So a 41-page report had no chart in it, and would not have
    had a figure either.
  * An `image` block needs a document id, a page and an index. The model was
    shown none of them: the documents list it could reach carried filename and
    status only. A capability that cannot be named cannot be used.
"""
import inspect
import pathlib
import re

import pytest

import gateway
import skills

PROMPTS = pathlib.Path(skills.ROOT) / "prompts"
SECTION = (PROMPTS / "author_section.md").read_text(encoding="utf-8")

# The gateway's own accepted set, read from its source when the repo layout
# allows it. A hardcoded copy here is the thing that let the two drift.
GW_DOCGEN = (pathlib.Path(skills.ROOT).parent.parent
             / "llm_gatewayV9" / "docgen.py")


@pytest.fixture(autouse=True)
def _clear_figure_cache():
    """The listing is cached per document for the life of the process, so
    without this reset the tests read each other's stubbed gateway."""
    skills._FIGURES_CACHE.clear()
    yield
    skills._FIGURES_CACHE.clear()


def _renderer_types() -> set[str]:
    src = GW_DOCGEN.read_text(encoding="utf-8")
    block = src[src.index("BLOCK_SCHEMA: list[dict]"):]
    return set(re.findall(r'\{"type": "([a-z_]+)"', block[:block.index("\n]")]))


def _stub(monkeypatch, calls, payloads):
    """Longest prefix wins, so `/v1/documents` cannot swallow a call meant for
    `/v1/documents/{id}/figures`."""
    keys = sorted(payloads, key=len, reverse=True)

    def fake_get_json(path, timeout=10.0):
        calls.append(path)
        for key in keys:
            if path.startswith(key):
                return payloads[key]
        return None
    monkeypatch.setattr(gateway, "get_json", fake_get_json)


def test_the_section_prompt_teaches_the_chart_and_image_blocks():
    """The gap this closes: the renderer draws them, the prompt hid them."""
    assert '"type": "chart"' in SECTION, "the section writer cannot emit a chart"
    assert '"type": "image"' in SECTION, "the section writer cannot emit a figure"
    assert "categories" in SECTION and "series" in SECTION, (
        "the chart block is named but not described, so the model invents a shape")


def test_the_section_prompt_teaches_only_what_the_renderer_accepts():
    taught = set(re.findall(r'"type": "([a-z_]+)"', SECTION))
    assert taught, "no block types taught at all"
    dead = taught - _renderer_types()
    assert not dead, (
        f"author_section.md teaches types the renderer does not have, and they "
        f"are silently dropped: {sorted(dead)}")


def test_figure_handles_are_listed_for_the_documents_the_run_may_read(monkeypatch):
    calls: list[str] = []
    _stub(monkeypatch, calls, {
        "/v1/documents": {"documents": [{"id": "doc-a", "enabled": True},
                                        {"id": "doc-b", "enabled": True}]},
        "/v1/documents/doc-a/figures": {
            "document_id": "doc-a", "filename": "Quarterly.pdf",
            "figures": [{"page": 2, "index": 0, "ext": "png",
                         "width": 640, "height": 400}]},
        "/v1/documents/doc-b/figures": {"document_id": "doc-b",
                                        "filename": "notes.pdf", "figures": []},
    })
    out = skills._figures_context(None)
    assert 'document="doc-a"' in out and "page=2" in out and "index=0" in out
    assert "640x400" in out, "the author cannot choose a figure by shape"
    assert "Quarterly.pdf" in out
    assert "doc-b" not in out, "a document with no figures was listed anyway"


def test_a_document_the_user_switched_off_is_never_offered(monkeypatch):
    """The document scope decides what a run may read; the enabled switch
    decides what exists for it. A figure handle from a disabled document would
    be a way to place material the user turned off."""
    calls: list[str] = []
    _stub(monkeypatch, calls, {
        "/v1/documents": {"documents": [{"id": "doc-off", "enabled": False}]},
        "/v1/documents/doc-off/figures": {
            "document_id": "doc-off", "filename": "secret.pdf",
            "figures": [{"page": 1, "index": 0, "ext": "png"}]},
    })
    assert skills._figures_context(None) == ""
    assert skills._figures_context({"doc-off"}) == ""
    assert not any(p.endswith("/figures") for p in calls), (
        "a disabled document was still scanned for figures")


def test_the_run_scope_limits_which_documents_are_offered(monkeypatch):
    """A run scoped to one document must not be handed handles from another."""
    calls: list[str] = []
    _stub(monkeypatch, calls, {
        "/v1/documents": {"documents": [{"id": "doc-a", "enabled": True},
                                        {"id": "doc-b", "enabled": True}]},
        "/v1/documents/doc-a/figures": {
            "document_id": "doc-a", "filename": "a.pdf",
            "figures": [{"page": 1, "index": 0, "ext": "png"}]},
        "/v1/documents/doc-b/figures": {
            "document_id": "doc-b", "filename": "b.pdf",
            "figures": [{"page": 1, "index": 0, "ext": "png"}]},
    })
    out = skills._figures_context({"doc-a"})
    assert 'document="doc-a"' in out and "doc-b" not in out
    assert not any(p == "/v1/documents/doc-b/figures" for p in calls), (
        "a document outside the run's scope was scanned anyway")


def test_an_unreachable_gateway_costs_the_run_nothing(monkeypatch):
    calls: list[str] = []
    _stub(monkeypatch, calls, {})
    assert skills._figures_context(None) == "", (
        "a missing listing must not become a failed document")


def test_a_shortened_listing_says_which_documents_still_hold_figures(monkeypatch):
    many = [{"page": p, "index": 0, "ext": "png"} for p in range(1, 41)]
    _stub(monkeypatch, [], {
        "/v1/documents": {"documents": [{"id": f"d{i}", "enabled": True}
                                        for i in range(3)]},
        "/v1/documents/d0/figures": {"document_id": "d0", "filename": "big.pdf",
                                     "figures": many},
        "/v1/documents/d1/figures": {"document_id": "d1", "filename": "big2.pdf",
                                     "figures": many},
        "/v1/documents/d2/figures": {"document_id": "d2", "filename": "big3.pdf",
                                     "figures": many},
    })
    out = skills._figures_context(None)
    assert "still hold more" in out, (
        "a truncated listing reads as 'there are no others'")
    assert out.count("document=") == skills._FIGURES_HANDLES_IN_PROMPT


def test_the_document_writer_gets_the_listing_and_it_reaches_every_section(monkeypatch):
    """The single-call author sees it in `rendered`; the sectioned writer needs
    it passed down, because each section call builds its own prompt."""
    src = inspect.getsource(skills._sectioned_document_blocks)
    assert "figures" in src, "section calls never receive the figure handles"
    call_src = inspect.getsource(skills._expand_document_sections)
    assert "figures=figures" in call_src, (
        "the expander drops the listing before the section writer sees it")
    run_src = inspect.getsource(skills.run_skill)
    assert "_figures_context(doc_ids)" in run_src


def test_only_the_document_writers_pay_for_the_listing(monkeypatch):
    """A researcher does not place figures; fetching handles for it would cost
    a gateway round trip per document on every research node."""
    run_src = inspect.getsource(skills.run_skill)
    # Anchor on the listing itself, not on the first author/deck gate: several
    # gates now share that condition (the format fragment, the length
    # contract, the render fallback), and the listing's own gate is what this
    # test is about.
    idx = run_src.index("figures = _figures_context(doc_ids)")
    gate = run_src[max(0, idx - 400):idx]
    assert 'skill.name in ("author", "deck")' in gate, (
        "the listing is no longer computed inside the writing-skill gate")
