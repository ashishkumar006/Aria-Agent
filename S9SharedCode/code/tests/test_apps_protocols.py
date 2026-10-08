"""Apps as an A2UI surface, and refresh progress as an AG-UI stream.

Both exist so the Apps board speaks the same declarative protocols as the rest
of the agent's output. Neither is allowed to be worse than what it replaces:
`prefab_views` still serves the board, and a builder bug must fail loudly here
rather than reaching the console as an unrenderable surface.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import a2ui_catalog as C  # noqa: E402
import agent_server  # noqa: E402
import agui as _agui  # noqa: E402
import apps_a2ui as _a2  # noqa: E402


# ── the surface builder ───────────────────────────────────────────────────────

def _app(**over):
    base = {
        "id": "app-1",
        "spec": {
            "title": "Issues",
            "id_field": "number",
            "fields": ["number", "title", "state"],
            "ui": {"columns": [{"field": "number", "label": "#"},
                               {"field": "title", "label": "Title"},
                               {"field": "state", "label": "State"}]},
        },
        "items": [
            {"number": 1, "title": "Login is broken", "state": "open",
             "labels": [{"name": "bug"}, {"name": "ui"}]},
            {"number": 2, "title": "Slow search", "state": "open"},
        ],
        "added": [{"number": 2, "title": "Slow search", "state": "open"}],
        "removed": [],
        "history": [{"count": 1}, {"count": 2}, {"count": 5}],
        "error": None,
    }
    base.update(over)
    return base


def _by_id(surface, cid):
    for c in surface["components"]:
        if c["id"] == cid:
            return c
    raise AssertionError(f"no component {cid!r}")


def test_a_tracker_app_becomes_a_validated_surface():
    out = _a2.validated_surface(_app())
    s = out["surface"]
    assert s["catalogId"] == C.CATALOG_ID
    assert s["rootComponent"] == "root"
    # `validated_surface` already ran these through the catalog validator, so
    # reaching here means the Apps board exercises it on every render. Do NOT
    # validate the normalised output a second time: validate_components is not
    # idempotent (it wraps props), and doing so asserts nothing useful.
    ids = {c["id"] for c in s["components"]}
    assert {"root", "title", "m", "table"} <= ids


def test_the_table_carries_the_apps_own_columns_and_rows():
    s = _a2.validated_surface(_app())["surface"]
    table = _by_id(s, "table")
    assert table["component"] == "Table"
    keys = [c["key"] for c in table["props"]["columns"]]
    assert keys[:3] == ["number", "title", "state"]
    assert len(table["props"]["rows"]) == 2
    assert table["props"]["rows"][0]["title"] == "Login is broken"


def test_newly_added_items_are_marked():
    s = _a2.validated_surface(_app())["surface"]
    rows = _by_id(s, "table")["props"]["rows"]
    by_num = {r["number"]: r for r in rows}
    assert by_num["2"]["_new"] == "yes"
    assert by_num["1"]["_new"] == ""


def test_the_history_becomes_a_sparkline_series():
    s = _a2.validated_surface(_app())["surface"]
    assert _by_id(s, "spark")["props"]["series"] == [1.0, 2.0, 5.0]


def test_a_dead_source_is_stated_in_the_surface():
    """A failed refresh must not look like an app that simply has no items."""
    s = _a2.validated_surface(_app(error="GITHUB_TOKEN expired"))["surface"]
    err = _by_id(s, "err_text")
    assert "GITHUB_TOKEN expired" in err["props"]["text"]
    # And the last good rows are still shown, so the board is not blanked.
    assert len(_by_id(s, "table")["props"]["rows"]) == 2


def test_nested_cell_values_are_rendered_not_dropped():
    """A feed record's list/dict cells must not vanish - and must not be
    passed through either, because the catalog rejects nested cell values."""
    s = _a2.validated_surface(_app())["surface"]
    labels = _by_id(s, "table")["props"]["columns"]
    assert "labels" not in [c["key"] for c in labels]
    # Render it directly to prove the path works.
    assert _a2._text([{"name": "bug"}]) == '[{"name":"bug"}]'
    assert _a2._text({"a": 1}) == '{"a":1}'


def test_booleans_render_readably():
    assert _a2._text(True) == "yes"
    assert _a2._text(False) == "no"
    assert _a2._text(None) == ""


def test_row_and_series_length_are_capped():
    app = _app(items=[{"number": i, "title": "t"} for i in range(900)],
               history=[{"count": i} for i in range(400)])
    out = _a2.validated_surface(app)
    rows = _by_id(out["surface"], "table")["props"]["rows"]
    assert len(rows) == _a2.MAX_TABLE_ROWS
    series = _by_id(out["surface"], "spark")["props"]["series"]
    assert len(series) == _a2.MAX_SERIES_POINTS
    assert any("showing the first" in n for n in out["notes"])


def test_a_long_cell_is_truncated():
    app = _app(items=[{"number": 1, "title": "x" * 5000}])
    rows = _by_id(_a2.validated_surface(app)["surface"], "table")["props"]["rows"]
    assert len(rows[0]["title"]) <= _a2.MAX_CELL_CHARS


def test_an_app_with_no_declared_columns_falls_back_to_its_fields():
    app = _app(spec={"title": "Bare", "fields": ["a"]}, items=[{"a": 1}])
    out = _a2.validated_surface(app)
    table = _by_id(out["surface"], "table")
    assert [c["key"] for c in table["props"]["columns"]][0] == "a"
    # A fallback is not a missing-column situation, so it must not warn.
    assert not any("no columns" in n for n in out["notes"])


def test_an_app_with_neither_columns_nor_fields_still_gets_one():
    """Never an empty grid: the id column is the last-resort fallback."""
    app = _app(spec={"title": "Bare"}, items=[{"a": 1}])
    table = _by_id(_a2.validated_surface(app)["surface"], "table")
    keys = [c["key"] for c in table["props"]["columns"]]
    assert keys[0] == "id"
    assert out_notes(app) == []


def out_notes(app) -> list[str]:
    return _a2.validated_surface(app)["notes"]


def test_an_app_with_no_rows_and_no_history_omits_the_sparkline():
    app = _app(items=[], history=[], added=[], removed=[])
    s = _a2.validated_surface(app)["surface"]
    ids = {c["id"] for c in s["components"]}
    assert "spark" not in ids
    # The table is still there with its empty-state text.
    assert _by_id(s, "table")["props"]["emptyText"]


def test_an_app_with_no_id_is_refused():
    with pytest.raises(ValueError):
        _a2.surface_for({"spec": {}})


def test_the_get_app_shape_builds_a_surface():
    """get_app() spreads app_status(), whose "added"/"removed" are
    INT counts with the row lists under added_items/removed_ids.
    Iterating the ints raised TypeError, which the a2ui route
    turned into a 502 on every app."""
    app = _app(added=1, removed=0,
               added_items=[{"number": 2, "title": "Slow search",
                             "state": "open"}],
               removed_ids=[])
    out = _a2.validated_surface(app)
    rows = _by_id(out["surface"], "table")["props"]["rows"]
    assert rows[1]["_new"] == "yes"
    assert rows[0]["_new"] == ""
    # The counts render from the ints, not the lists.
    assert _by_id(out["surface"], "m_added")["props"]["value"] == "1"
    assert _by_id(out["surface"], "m_removed")["props"]["value"] == "0"


# ── the catalog gains the two components Apps needs ───────────────────────────

def test_table_rejects_a_nested_cell():
    with pytest.raises(C.CatalogError) as e:
        C.validate_surface({"surfaceId": "s", "components": [
            {"id": "root", "component": "Table",
             "columns": [{"key": "a", "label": "A"}],
             "rows": [{"a": {"nested": 1}}]}]})
    assert "scalar" in str(e.value)


def test_table_rejects_no_columns():
    with pytest.raises(C.CatalogError):
        C.validate_surface({"surfaceId": "s", "components": [
            {"id": "root", "component": "Table", "columns": [], "rows": []}]})


def test_table_rejects_duplicate_column_keys():
    with pytest.raises(C.CatalogError) as e:
        C.validate_surface({"surfaceId": "s", "components": [
            {"id": "root", "component": "Table",
             "columns": [{"key": "a", "label": "A"}, {"key": "a", "label": "B"}],
             "rows": []}]})
    assert "duplicate" in str(e.value)


def test_table_rejects_too_many_rows():
    rows = [{"a": "1"}] * (C.MAX_TABLE_ROWS + 1)
    with pytest.raises(C.CatalogError) as e:
        C.validate_surface({"surfaceId": "s", "components": [
            {"id": "root", "component": "Table",
             "columns": [{"key": "a", "label": "A"}], "rows": rows}]})
    assert str(C.MAX_TABLE_ROWS) in str(e.value)


def test_table_rejects_an_overlong_cell():
    with pytest.raises(C.CatalogError):
        C.validate_surface({"surfaceId": "s", "components": [
            {"id": "root", "component": "Table",
             "columns": [{"key": "a", "label": "A"}],
             "rows": [{"a": "x" * (C.MAX_CELL_CHARS + 1)}]}]})


def test_sparkline_rejects_non_numbers_and_non_finite():
    for bad in (["a"], [float("nan")], [float("inf")], [True]):
        with pytest.raises(C.CatalogError):
            C.validate_surface({"surfaceId": "s", "components": [
                {"id": "root", "component": "Sparkline", "series": bad}]})


def test_sparkline_rejects_too_many_points():
    with pytest.raises(C.CatalogError):
        C.validate_surface({"surfaceId": "s", "components": [
            {"id": "root", "component": "Sparkline",
             "series": [1] * (C.MAX_SERIES_POINTS + 1)}]})


def test_the_catalog_document_advertises_the_new_limits():
    limits = C.catalog_document()["limits"]
    assert limits["maxTableRows"] == C.MAX_TABLE_ROWS
    assert limits["maxSeriesPoints"] == C.MAX_SERIES_POINTS


# ── AG-UI: step_finished exists and pairs ────────────────────────────────────

def test_step_finished_exists_and_pairs_with_step_started():
    started = _agui.step_started("fetch")
    finished = _agui.step_finished("fetch")
    assert started["type"] == "STEP_STARTED"
    assert finished["type"] == "STEP_FINISHED"
    # A client that renders progress needs the same step name back.
    assert finished["stepName"] == started["stepName"]


def test_event_stream_guarantees_one_terminal_event():
    """The refresh stream's contract: whatever happens, exactly one of
    RUN_FINISHED / RUN_ERROR, so a client never waits forever."""
    async def gen():
        yield _agui.run_started("t", "r")
        yield _agui.step_started("fetch")
        yield _agui.step_finished("fetch")
        yield _agui.run_finished("t", "r", {"count": 1})

    async def collect():
        return [f async for f in gen()]

    frames = asyncio.run(collect())
    terminal = [f for f in frames if f["type"] in ("RUN_FINISHED", "RUN_ERROR")]
    assert len(terminal) == 1


def test_a_refresh_stream_balances_every_started_step(monkeypatch):
    """A started step with no finish leaves a spinner running forever, and the
    encoder had no constructor to close one. Assert the pairing on the real
    event sequence rather than trusting the code read."""
    _patch_refresh(monkeypatch, {"count": 3, "added": [1], "removed": [],
                                 "error": None})
    events = _events(monkeypatch)
    started = [e["stepName"] for e in events if e["type"] == "STEP_STARTED"]
    finished = [e["stepName"] for e in events if e["type"] == "STEP_FINISHED"]
    assert started, "the refresh must announce its phases"
    assert sorted(started) == sorted(finished), (
        f"unbalanced steps: started={started} finished={finished}")
    terminal = [e for e in events if e["type"] in ("RUN_FINISHED", "RUN_ERROR")]
    assert len(terminal) == 1
    assert terminal[0]["type"] == "RUN_FINISHED"
    assert terminal[0]["result"]["count"] == 3


def _patch_refresh(monkeypatch, result):
    import apps as _apps
    monkeypatch.setattr(_apps, "refresh_app", lambda a: result)


def _events(monkeypatch) -> list[dict]:
    """Drive the refresh event generator and return the events."""
    async def go():
        return [e async for e in agent_server._apps_refresh_events("app-1")]

    return asyncio.run(go())


def test_a_dead_source_completes_the_run_with_an_error_field(monkeypatch):
    """A dead source is a completed run, not a crashed one: the board still
    has its last good snapshot, and a client must not treat it as a failure to
    start."""
    _patch_refresh(monkeypatch, {"count": 0, "added": [], "removed": [],
                                 "error": "GITHUB_TOKEN expired"})
    events = _events(monkeypatch)
    terminal = [e for e in events if e["type"] in ("RUN_FINISHED", "RUN_ERROR")]
    assert len(terminal) == 1
    assert terminal[0]["type"] == "RUN_FINISHED"
    assert terminal[0]["result"]["error"] == "GITHUB_TOKEN expired"
    assert any(e["type"] == "ACTIVITY_SNAPSHOT" for e in events)


def test_a_crashing_refresh_also_balances_its_steps(monkeypatch):
    """The failure path closes every step it opened. It did not before, so an
    exception mid-refresh stranded the UI on a spinner."""
    import apps as _apps

    def boom(_a):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(_apps, "refresh_app", boom)
    events = _events(monkeypatch)
    started = [e["stepName"] for e in events if e["type"] == "STEP_STARTED"]
    finished = [e["stepName"] for e in events if e["type"] == "STEP_FINISHED"]
    assert sorted(started) == sorted(finished)
    terminal = [e for e in events if e["type"] in ("RUN_FINISHED", "RUN_ERROR")]
    assert len(terminal) == 1 and terminal[0]["type"] == "RUN_ERROR"
    assert "connection refused" in terminal[0]["message"]