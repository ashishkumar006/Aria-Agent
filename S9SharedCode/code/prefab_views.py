"""PrefectHQ/prefab render layer for tracker apps (SPIKE).

Converts a stored tracker snapshot (`apps.get_app`) into a Prefect-Prefab
component tree: Metric row (items/added/removed) + searchable DataTable
(spec columns) + history Sparkline. Data is baked in at build time;
refreshing stays on the existing Refresh-now path (no MCP host bridge in
the embedded case, so CallTool actions are intentionally absent here).

Served side-by-side at GET /api/apps/{id}/prefab as the bundled
single-file HTML (offline-capable) — the existing table/cards views are
untouched. The raw protocol dict is available via tracker_prefab_tree()
for validation and future MCP-Apps exposure.

prefab_ui is imported lazily so a missing/broken package can never break
agent boot or the other views.
"""
from __future__ import annotations


def _detail(app_id: str) -> dict:
    import apps as _apps
    det = _apps.get_app(app_id)
    if det is None:
        raise ValueError(f"unknown app '{app_id}'")
    return det


def tracker_prefab_tree(app_id: str) -> dict:
    """Component-tree protocol JSON for one tracker app. Raises ValueError
    for unknown apps; any prefab_ui failure propagates (caller maps it)."""
    return _compose(app_id).to_json()


def tracker_prefab_html(app_id: str) -> str:
    """Bundled single-file HTML (offline) for iframe embedding."""
    return _compose(app_id).html(renderer_mode="bundled")


def _compose(app_id: str):
    """Build the live PrefabApp object (shared by tree + html paths).

    NOTE (0.20.2): the `with`-block auto-attach does not populate, and
    `model_dump()` drops children — compose with explicit `parent=` links
    and serialize with `to_json()` (the documented wire format).
    """
    det = _detail(app_id)
    from prefab_ui.app import PrefabApp
    from prefab_ui.components import (
        Column, Grid, Card, Metric, DataTable, DataTableColumn,
        Heading, Text, Badge,
    )
    from prefab_ui.components.charts import Sparkline
    spec = det.get("spec") or {}
    ui = spec.get("ui") or {}
    columns_cfg = ui.get("columns") or [
        {"field": f, "label": f} for f in (spec.get("fields") or ["id"])]
    id_field = spec.get("id_field") or "id"
    items = det.get("items") or []
    added_ids = {str(a.get(id_field) or a.get("id")) for a in (det.get("added_items") or [])
                 if isinstance(a, dict)}
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        row = {c["field"]: it.get(c["field"]) for c in columns_cfg}
        row["_new"] = str(it.get(id_field) or it.get("id")) in added_ids
        rows.append(row)
    table_cols = [DataTableColumn(key=c["field"], header=c["label"], sortable=True)
                  for c in columns_cfg]
    hist = [h.get("count", 0) for h in (det.get("history") or [])
            if isinstance(h, dict)]
    root = Column(gap=4)
    Heading(det.get("name") or app_id, parent=root)
    Text(f"{det.get('count', 0)} items · {spec.get('schedule', 'manual')}", parent=root)
    grid = Grid(columns=3, gap=4, parent=root)
    for label, val in (("Items", str(det.get("count", 0))),
                       ("Added", f"+{det.get('added', 0)}"),
                       ("Removed", f"-{det.get('removed', 0)}")):
        card = Card(css_class="p-6", parent=grid)
        Metric(label=label, value=val, parent=card)
    if det.get("error"):
        Text(f"Last refresh failed: {det['error']}", parent=root)
    if added_ids:
        Badge(f"{len(added_ids)} new", variant="success", parent=root)
    DataTable(columns=table_cols, rows=rows, search=True, paginated=True, parent=root)
    if len(hist) > 1:
        Heading("History", level=3, parent=root)
        Sparkline(data=hist, parent=root)
    return PrefabApp(view=root)
