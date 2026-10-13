"""The document is written section by section, unconditionally.

Four runs produced 6,509 / 904 / 267 / 935+1,660 words on an identical
request. The one that worked did so because N section calls happened, not
because a threshold fired: every gated version (`_delivered < _floor * 0.6`
AND an outline could be derived) failed to fire once across four runs, so a
well-behaved author always shipped its single ~900-word call.

These pin the ungated design so the gate cannot quietly come back.
"""
import inspect

import pytest

import skills


def _code_only(fn) -> str:
    """The function's source with comments and docstrings stripped.

    The history of this gate is now written in the comment above it, so a naive
    substring check would fail on its own explanation.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            node.value.value = ""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)):
                node.body[0].value.value = ""
    return ast.unparse(tree)


def test_the_expansion_runs_for_author_unconditionally():
    src = _code_only(skills.run_skill)
    assert "_floor * 0.6" not in src, (
        "a length threshold is gating the expansion again - it never fired in "
        "four runs, so a well-behaved author ships one thin call")
    assert "_delivered < " not in src, "a conditional threshold is back"


def test_both_render_paths_expand():
    src = _code_only(skills.run_skill)
    assert "_expand_document_sections(" in src
    # Both the fallback record and the tool-path record must be routed through it.
    assert src.count("_expand_document_sections(") >= 3, (
        "both the spec-fallback and tool-render paths must expand")


def test_the_expansion_has_one_implementation():
    assert hasattr(skills, "_expand_document_sections")
    src = inspect.getsource(skills._expand_document_sections)
    assert "pre_rendered" in src, "it cannot serve both call shapes"


def test_it_always_derives_an_outline_and_reports_when_it_cannot():
    src = inspect.getsource(skills._expand_document_sections)
    assert "_derive_document_outline(" in src
    assert "only" in src and "derivable" in src, (
        "an underivable outline must be logged, not silently skipped - that "
        "silence is what hid the failure for four runs")


def test_a_failure_never_loses_the_document():
    """Every early return hands back the render we already have."""
    src = inspect.getsource(skills._expand_document_sections)
    assert src.count("return [current]") >= 3, (
        "no outline / no sections / render failed / render refused / not "
        "longer - each must fall back to the document already produced")


def test_it_keeps_the_longer_document():
    src = inspect.getsource(skills._expand_document_sections)
    assert "new_words <= cur_words" in src, (
        "a sectioned attempt that comes out SHORTER must not replace a better "
        "document")


def test_the_document_is_rendered_once_at_the_end():
    src = inspect.getsource(skills._expand_document_sections)
    assert src.count("render_document(") == 1, (
        "N sections must be merged and rendered ONCE, not one file per section")
    assert "merged[:400]" in src


def test_the_outline_can_come_from_the_request_alone():
    """72 blocks with no headings must still expand."""
    q = ("Write a treatise on aqueducts: materials and mortar, gradients and "
         "hydraulics, surveying instruments, maintenance routines, water "
         "quality and lead, decline and reuse")
    got = skills._derive_document_outline(
        [{"type": "paragraph", "text": "x"} for _ in range(72)],
        [], [], q)
    assert len(got) >= 5, got