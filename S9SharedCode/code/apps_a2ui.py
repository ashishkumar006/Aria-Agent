"""A2UI surfaces for the Apps board.

Why this exists
---------------
The Apps view renders tracker apps - a metric row, a sortable/searchable data
table and a history sparkline - through `prefab_ui` (`prefab_views.py`). That
works, and it is offline-capable. This module renders the same apps as A2UI so
the board can speak the same declarative protocol as everything else the agent
can produce, and so an app view and an agent-generated view are one system
rather than two.

Deterministic, not generated
----------------------------
`a2ui_surfaces.generate_surface()` asks a model to produce a surface. That is
the wrong tool here: an app's columns, rows and history are already known
exactly, so asking a model to reproduce them would add latency, cost and a
class of failure (a hallucinated row) to a computation with no uncertainty in
it. The only judgement a model could add - which columns matter - is already
recorded in the app's own spec. So this builder is a pure function of
`get_app(app_id)`, and it is the same for every client.

Bounded by construction
-----------------------
Row count, column count, cell length and series length are capped here *and*
re-checked by `a2ui_catalog.validate_components()`. A tracker app is a third
party feed (a GitHub issue list, an RSS feed, a sheet), so its shape is not
ours to assume - the caps are a trust boundary, not a formality.
"""
from __future__ import annotations

from typing import Any

import a2ui_catalog as _cat

MAX_TABLE_ROWS = 200          # below the catalog's 500; app boards are read-at-a-glance
MAX_CELL_CHARS = 200
MAX_SERIES_POINTS = 120
MAX_PREVIEW_ROWS = 500


def _text(value: Any, limit: int = MAX_CELL_CHARS) -> str:
    """One cell, as a bounded string.

    Everything becomes text on purpose. A feed item can carry dicts and lists
    (an API record's `labels`, a Slack attachment); the catalog rejects nested
    values, so they are rendered rather than dropped. A dict is JSON, because
    that is at least readable; anything else is `str`.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        s = value
    elif isinstance(value, (dict, list)):
        import json as _json
        try:
            s = _json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            s = str(value)
    else:
        s = str(value)
    s = " ".join(s.split())
    return s[:limit]


def _columns(spec: dict[str, Any]) -> list[dict[str, str]]:
    """The app's declared columns, or a fallback.

    Never empty: an app with no `ui.columns` falls back to its `fields`, and
    one with neither falls back to the id column. A table with no columns
    renders an empty grid and tells the user nothing, so there is always at
    least one column to look at.
    """
    ui = spec.get("ui") or {}
    declared = ui.get("columns") or []
    cols: list[dict[str, str]] = []
    for c in declared:
        if isinstance(c, dict) and c.get("field"):
            cols.append({"key": str(c["field"]),
                         "label": _text(c.get("label") or c["field"], 60)})
    if not cols:
        for f in (spec.get("fields") or ["id"]):
            cols.append({"key": str(f), "label": _text(f, 60)})
    return cols[:_cat.MAX_TABLE_COLUMNS]


def surface_for(app: dict[str, Any]) -> dict[str, Any]:
    """Build the A2UI surface for one app detail payload.

    Returns `{"surface": {...}, "notes": [...]}`. Raises `ValueError` for an
    unknown app, mirroring `prefab_views._detail`.
    """
    app_id = app.get("id") or app.get("app_id")
    if not app_id:
        raise ValueError("unknown app")
    spec = app.get("spec") or {}
    id_field = spec.get("id_field") or "id"
    items = [i for i in (app.get("items") or []) if isinstance(i, dict)]
    # get_app() spreads app_status(), whose "added"/"removed" are
    # INT counts; the row lists live under added_items/removed_ids.
    # Iterating the ints used to raise TypeError, which the route
    # turned into a 502 on every app. Prefer the list keys, and
    # coerce anything else to [] so a mismatched payload degrades
    # to "no new items" instead of failing the whole surface.
    raw_added = app.get("added_items")
    if raw_added is None:
        raw_added = app.get("added")
    if not isinstance(raw_added, list):
        raw_added = []
    added_ids = {
        str(a.get(id_field) or a.get("id"))
        for a in raw_added if isinstance(a, dict)
    }

    cols = _columns(spec)
    rows: list[dict[str, str]] = []
    for it in items[:MAX_TABLE_ROWS]:
        row = {c["key"]: _text(it.get(c["key"])) for c in cols}
        row["_new"] = "yes" if str(it.get(id_field) or it.get("id")) in added_ids else ""
        rows.append(row)
    # `"_new" not in cols[0]` tested the first column's dict KEYS ("key",
    # "label"), not its key value, so it was always True and the marker column
    # was always appended. For an app whose own fields include one literally
    # named `_new`, the validator then rejected the duplicate key and the whole
    # board failed with a 502 "A2UI surface unavailable".
    if cols and "_new" not in [c.get("key") for c in cols]:
        cols = cols + [{"key": "_new", "label": "new"}]

    series = [float(h.get("count", 0) or 0)
              for h in (app.get("history") or [])
              if isinstance(h, dict)][-MAX_SERIES_POINTS:]

    title = _text(spec.get("title") or app.get("title") or app_id, 120)
    added_n = len(added_ids)
    removed = app.get("removed_ids")
    if removed is None:
        removed = app.get("removed")
    if not isinstance(removed, list):
        removed = []
    error = _text(app.get("error") or "", 200)

    c: list[dict[str, Any]] = []
    add = c.append
    # Built from what is actually created: badge, spark and err are conditional,
    # so a hard-coded child list referenced components that were never emitted
    # and the surface failed validation on every app.
    children: list[str] = ["title", "sub"]

    # The stack this replaced was one flat Column of five siblings, which is
    # why every app looked the same and none of them looked designed: the
    # app's own name at body-text size (no `variant`), three metric cards
    # touching a data table touching a sparkline, with nothing grouping them.
    # The catalog already has Section, Grid and Text's `variant`; the surface
    # just never used them. So: a heading, a caption that carries the count
    # instead of gluing it into the title with a hyphen, an Overview section
    # holding the metrics and the trend, and the items table in its own
    # section under a subtitle that says how big it is.
    add({"id": "title", "component": "Text",
         "text": title, "variant": "heading"})
    add({"id": "sub", "component": "Text",
         "text": (f"{len(items)} item{'s' if len(items) != 1 else ''}"
                  + (f" · {added_n} added since last refresh" if added_n else "")
                  + ("" if error else " · up to date")),
         "variant": "caption"})
    if error:
        # The source failed. Say so IN the surface, and above the data rather
        # than under it — a dead feed that renders an empty table below looks
        # exactly like an app with nothing in it.
        add({"id": "err", "component": "Callout", "tone": "bad",
             "title": "Last refresh failed", "text": error})
        children.append("err")

    add({"id": "m", "component": "MetricRow",
         "children": ["m_items", "m_added", "m_removed"]})
    add({"id": "m_items", "component": "KeyValue",
         "label": "items", "value": str(len(items))})
    add({"id": "m_added", "component": "KeyValue",
         "label": "added", "value": str(added_n)})
    add({"id": "m_removed", "component": "KeyValue",
         "label": "removed", "value": str(len(removed))})

    overview: list[str] = ["m"]
    if added_n:
        add({"id": "badge", "component": "Badge", "text": f"{added_n} new"})
        overview.append("badge")
    if len(series) > 1:
        add({"id": "spark", "component": "Sparkline",
             "series": series, "label": "items per refresh"})
        overview.append("spark")
    add({"id": "s_overview", "component": "Section", "title": "Overview",
         "children": overview, "gap": "normal"})
    children.append("s_overview")

    add({"id": "table", "component": "Table",
         "columns": cols, "rows": rows,
         "rowKey": str(id_field), "searchable": True, "paginated": True,
         "emptyText": "No items. Refresh to fetch."})
    add({"id": "s_items", "component": "Section", "title": "Items",
         "subtitle": (f"{min(len(items), MAX_TABLE_ROWS)} of {len(items)} rows"
                      if len(items) > MAX_TABLE_ROWS else
                      f"{len(items)} row{'s' if len(items) != 1 else ''}"),
         "children": ["table"], "gap": "tight"})
    children.append("s_items")

    # `root` is appended last so it can close over the real child list.
    add({"id": "root", "component": "Column", "children": children,
         "gap": "normal"})

    surface = {
        "surfaceId": f"app:{app_id}",
        "catalogId": _cat.CATALOG_ID,
        "rootComponent": "root",
        "components": c,
    }
    notes: list[str] = []
    if len(items) > MAX_TABLE_ROWS:
        notes.append(f"{len(items)} items; showing the first {MAX_TABLE_ROWS}.")
    return {"surface": surface, "notes": notes}


def validated_surface(app: dict[str, Any]) -> dict[str, Any]:
    """The surface to show for one app.

    An `a2ui` app was DESIGNED: the builder asked a model for a layout, and what
    it returned was validated and stored on the spec (`get_app` hands it up as
    `app["surface"]`). This function used to ignore that field completely and
    rebuild the deterministic tracker board from the app's spec fields, so a
    "Reading Goals Dashboard" built as three sections, three gauges and a weekly
    callout reached the console as `items / added / removed` over an empty
    two-column table. Every layout the agent made was discarded at read time —
    which is the real reason the app layouts looked bad: the one you were shown
    was not the one that was built.

    So the app's own surface wins. The tracker board stays what a tracker or
    JSON-feed app renders, and the fallback for an a2ui app whose stored surface
    no longer validates — and that substitution is said in `notes`, not hidden.
    """
    own = app.get("surface")
    fallback_note = ""
    if (isinstance(own, dict) and isinstance(own.get("components"), list)
            and own["components"]):
        try:
            comps = _cat.validate_components(own["components"])
        except _cat.CatalogError as e:
            fallback_note = (
                "the app's own surface no longer validates "
                f"({str(e)[:120]}); showing the tracker view instead")
        else:
            return {
                "surface": {
                    "surfaceId": str(own.get("surfaceId") or f"app:{app.get('id')}"),
                    "catalogId": own.get("catalogId") or _cat.CATALOG_ID,
                    "rootComponent": own.get("rootComponent") or "root",
                    "data": own.get("data") or {},
                    "components": comps,
                },
                "notes": [],
            }
    out = surface_for(app)
    out["surface"]["components"] = _cat.validate_components(
        out["surface"]["components"])
    if fallback_note:
        out["notes"] = list(out.get("notes") or []) + [fallback_note]
    return out