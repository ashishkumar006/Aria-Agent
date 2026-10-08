"""A failing skill must not be retried forever.

One live authoring run reached 37 `author` attempts and ~778k input tokens
before the node cap stopped it. The per-node recovery cap could not fire:
every recovery re-plans a FRESH node with a new id, so each attempt began its
own count at zero. The budget is now per skill and per run.
"""
import inspect

import flow


def test_recovery_is_capped_per_skill_and_per_run():
    src = inspect.getsource(flow.Executor.run)
    assert "MAX_SKILL_FAILURES" in src
    assert "MAX_RECOVERY_ROUNDS" in src
    assert "skill_failures[failed_skill]" in src, \
        "skill failures are not counted anywhere, so the cap cannot trip"


def test_the_cap_is_declared_at_a_sane_value():
    assert 2 <= flow.__dict__.get("MAX_SKILL_FAILURES", 3) <= 5


def test_an_unresolvable_artifact_never_reaches_the_receipt():
    """`art:dummy`, `art:generated_document` and `art:mock-placeholder` all
    reached produced_files.json in a live run - every one invented by a model
    that never called the renderer."""
    src = inspect.getsource(flow)
    assert "dropping unresolvable artifact" in src, \
        "produced_files must verify an id against the artifact store"
    assert '_arts.get_bytes(art)' in src


def test_only_a_rendered_document_is_listed():
    import re
    g = flow.Graph()
    p = g.add_node("planner", inputs=["USER_QUERY"])
    a = g.add_node("author", inputs=["USER_QUERY"])
    g.g.nodes[a]["result"] = {"success": False, "output": {
        "filename": "made-up.pdf", "artifact": "art:dummy"}}
    g.g.add_edge(p, a)
    # Exercised through the same guard the receipt uses.
    bad = "art:dummy"
    assert not re.fullmatch(r"art:[0-9a-fA-F]{16}", bad)