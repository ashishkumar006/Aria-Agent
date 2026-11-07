"""The Apps board's own surface has to LOOK composed, not just validate.

Every layout primitive the console needs has been in the catalog for a while —
`Section`, `Grid`, `MetricRow`, `Text variant:"heading"`. The deterministic
tracker surface used none of them: root was a flat `Column` of
`Text, MetricRow, Badge, Table, Sparkline, Card`, and the app's own name was a
plain body `Text`, so a title rendered at exactly the size of its body copy and
the whole board read as one undifferentiated stack. That is the "the layouts
they are making are bad" report, and the same absence of composition guidance
is why model-generated apps come out the same way.

These assertions are about structure, because structure is what makes a view
read as built: a heading that is a heading, the metrics and the trend grouped
apart from the table, and a failure stated ABOVE the empty table rather than
under it.
"""

from __future__ import annotations

import json

import pytest

import a2ui_catalog as cat
import apps_a2ui as a2


def _app(**over) -> dict:
    base = {
        "id": "demo-app",
        "spec": {"title": "OpenRouter free models", "fields": ["id", "notes"]},
        "items": [{"id": f"m{i}", "notes": "n"} for i in range(4)],
        "added_items": [{"id": "m3"}],
        "removed_ids": [],
        "history": [{"count": 1}, {"count": 3}, {"count": 4}],
        "error": "",
    }
    base.update(over)
    return base


def _comps(app: dict) -> dict[str, dict]:
    out = a2.validated_surface(app)               # raises CatalogError if invalid
    return {c["id"]: c for c in out["surface"]["components"]}


def test_the_title_is_a_heading_and_the_count_is_a_caption():
    by = _comps(_app())
    assert by["title"]["component"] == "Text"
    assert by["title"]["props"].get("variant") == "heading", (
        "a title rendered as body text is the same size as the copy under it — "
        "the board has no visual entry point")
    assert by["sub"]["props"].get("variant") == "caption"
    # The count moved out of the title rather than being hyphenated onto it.
    assert "item" in by["sub"]["props"]["text"]
    assert "item" not in by["title"]["props"]["text"]
    assert not by["title"]["props"]["text"].endswith(")")


def test_plurals_read_as_english():
    for n, want in ((0, "0 items"), (1, "1 item"), (4, "4 items")):
        by = _comps(_app(items=[{"id": str(i)} for i in range(n)], added_items=[]))
        assert want in by["sub"]["props"]["text"], by["sub"]["props"]["text"]
        assert by["s_items"]["props"]["subtitle"].startswith(
            f"{n} row" + ("" if n == 1 else "s"))


def test_the_view_is_grouped_into_sections_not_a_flat_stack():
    by = _comps(_app())
    root = by["root"]
    assert root["component"] == "Column"
    # The root's children are groupings, not leaves.
    kids = [by[k] for k in root["props"]["children"]]
    kinds = [k["component"] for k in kids]
    assert kinds.count("Section") == 2, f"root holds {kinds}"
    titles = [k["props"].get("title") for k in kids if k["component"] == "Section"]
    assert titles == ["Overview", "Items"]
    # The metrics and the trend belong to the overview, the table to items.
    assert "m" in by["s_overview"]["props"]["children"]
    assert "spark" in by["s_overview"]["props"]["children"]
    assert by["s_items"]["props"]["children"] == ["table"]


def test_a_failed_refresh_is_stated_above_the_data():
    by = _comps(_app(items=[], added_items=[], error="feed returned 500"))
    order = by["root"]["props"]["children"]
    assert "err" in order
    assert order.index("err") < order.index("s_overview"), (
        "a dead feed printed below an empty table looks exactly like an app "
        "with nothing in it")
    assert by["err"]["component"] == "Callout"
    assert by["err"]["props"].get("tone") == "bad"


def test_a_single_item_app_does_not_invent_components():
    by = _comps(_app(items=[{"id": "only"}], added_items=[], history=[]))
    for c in by.values():
        for kid in (c["props"].get("children") or []):
            assert kid in by, f"{c['id']} references {kid}, which was never built"
    assert "spark" not in by and "badge" not in by
    assert by["s_overview"]["props"]["children"] == ["m"]


def test_every_generated_surface_survives_the_validator():
    # The builder is the trusted path, but it still goes through the same
    # validator a model's output does, so a builder bug fails here loudly.
    with pytest.raises(cat.CatalogError):
        cat.validate_components([{"id": "x", "component": "NotReal"}])
    _comps(_app(items=[{"id": str(i)} for i in range(300)],
                history=[{"count": i} for i in range(200)]))


# ── the guidance both prompt paths share ──────────────────────────────────
def test_the_catalog_teaches_composition_not_just_parts():
    p = cat.catalog_prompt()
    assert "LAYOUT" in p, (
        "the catalog lists components and says nothing about assembling them, "
        "which is how a model with Section and Grid available still emits a "
        "flat Column of body-size Text")
    for needle in ('variant:"heading"', "Section", "MetricRow", "Grid",
                   "DataList", "reach"):
        assert needle in p, f"LAYOUT block is missing {needle!r}"


def test_both_surface_prompt_paths_carry_the_layout_block():
    import app_builder
    import a2ui_surfaces
    base = app_builder.PROMPT_PATH.read_text(encoding="utf-8")
    assembled = base.rstrip() + "\n\n" + cat.catalog_prompt()
    assert "LAYOUT" in assembled
    system, _ = a2ui_surfaces.generation_prompt("a board of open issues")
    assert "LAYOUT" in system, (
        "the app builder is told how to compose a view and the chat surface "
        "generator is not — the same failure in the path nobody thinks to check")


def test_the_app_builder_prompt_does_not_duplicate_the_catalog_rules():
    """One source of truth. app_builder.md is concatenated WITH catalog_prompt,
    so a second copy of the layout rules would be paid for on every build."""
    import app_builder
    base = app_builder.PROMPT_PATH.read_text(encoding="utf-8")
    assert base.count("LAYOUT") <= 1
    assert 'columns:"auto"' not in base, (
        "grid guidance belongs in catalog_prompt, which both paths append")


# ── the app's own design must be what gets shown ───────────────────────────
"""`validated_surface` was rebuilding the tracker board for every app and
throwing the builder's layout away.

The builder asks a model for a surface, validates it, and stores it on the
spec; `get_app()` returns it as `app["surface"]`. The read path then ignored
that field and composed `items / added / removed` over a table of the spec's
own fields. Live, a "Reading Goals Dashboard" designed as three Sections with
three Gauges and a weekly Callout reached the console as an empty metric strip.
"""

DESIGNED = {
    "surfaceId": "reading-goals-dashboard",
    "catalogId": cat.CATALOG_ID,
    "rootComponent": "root",
    "components": [
        {"id": "root", "component": "Column",
         "children": ["t", "goals", "notes"]},
        {"id": "t", "component": "Text", "text": "Reading goals",
         "variant": "heading"},
        {"id": "goals", "component": "Section", "title": "Goals",
         "children": ["g1"]},
        {"id": "g1", "component": "Gauge", "label": "Finish 12 books",
         "value": 5, "max": 12},
        {"id": "notes", "component": "Callout", "title": "This week",
         "text": "Two finished.", "tone": "info"},
    ],
}


def _a2ui_app(**over) -> dict:
    base = {"id": "reading-goals-dashboard", "kind": "a2ui",
            "spec": {"title": "Reading Goals Dashboard", "fields": ["id"]},
            "surface": DESIGNED, "items": [], "added_items": [],
            "removed_ids": [], "history": [], "error": None}
    base.update(over)
    return base


def test_an_a2ui_app_is_shown_the_surface_it_was_given():
    out = a2.validated_surface(_a2ui_app())
    ids = [c["id"] for c in out["surface"]["components"]]
    assert "goals" in ids and "notes" in ids, (
        f"served {ids} — the app's own design was replaced by the tracker board")
    assert out["surface"]["surfaceId"] == "reading-goals-dashboard"
    assert not out["notes"]


def test_the_served_design_is_still_validated():
    """Not a passthrough: an a2ui app that somehow holds a bad surface must not
    push unvalidated components at the client."""
    bad = json.loads(json.dumps(DESIGNED))
    bad["components"].append({"id": "x", "component": "NotReal"})
    out = a2.validated_surface(_a2ui_app(surface=bad))
    ids = [c["id"] for c in out["surface"]["components"]]
    assert "s_items" in ids and "m_items" in ids, (
        "an unvalidatable design fell through to a partial surface instead of "
        "the tracker board")
    assert "goals" not in ids, "half of a rejected surface was served anyway"
    assert any("no longer validates" in n for n in out["notes"]), (
        "the substitution is silent — the user cannot tell their design from "
        "the generated tracker view")


def test_a_tracker_app_still_gets_the_deterministic_board():
    """The board was the point for feeds: nothing was designed, the shape is
    the data, and a model would only add cost and a hallucinated row."""
    out = a2.validated_surface(_app(kind="tracker", surface=None))
    ids = [c["id"] for c in out["surface"]["components"]]
    assert {"m_items", "m_added", "table", "s_overview"} <= set(ids)


def test_an_a2ui_app_with_no_stored_surface_falls_back_rather_than_showing_nothing():
    out = a2.validated_surface(_a2ui_app(surface=None))
    ids = [c["id"] for c in out["surface"]["components"]]
    assert "root" in ids and "s_items" in ids
