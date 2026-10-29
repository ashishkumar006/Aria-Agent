"""The A2UI catalog is a security boundary, not just a component list.

An agent emits component definitions that a client renders. If the set of
acceptable definitions is open, the model can emit anything; if it is closed,
anything outside it is refused before render. So the rejection tests below are
the point of the module, and they are written as attacks rather than as
coverage:

  * markup / styles / handler names smuggled in as properties
  * a component invented out of thin air
  * nesting deep enough or wide enough to hang the renderer
  * a cycle, which is a denial of service dressed as a tree
  * a dangling child reference, which renders as an empty hole
  * an oversized surface that is individually valid but collectively a problem

Each must raise `CatalogError` naming the problem. A half-accepted surface is
worse than a rejected one, because it renders as though it were authoritative.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import a2ui_catalog as C  # noqa: E402


def ok(**over):
    """A minimal valid surface."""
    comps = [
        {"id": "root", "component": "Column", "children": ["title"]},
        {"id": "title", "component": "Text", "text": "hello"},
    ]
    comps = over.pop("components", comps)
    base = {"surfaceId": "s1", "components": comps}
    base.update(over)
    return base


def test_a_minimal_surface_validates():
    out = C.validate_surface(ok())
    assert out["surfaceId"] == "s1"
    assert out["catalogId"] == C.CATALOG_ID
    assert len(out["components"]) == 2
    assert "hello" in out["text"]


# ── the catalog document ─────────────────────────────────────────────────────

def test_catalog_document_is_self_describing():
    doc = C.catalog_document()
    assert doc["catalogId"] == C.CATALOG_ID
    assert doc["version"] == C.PROTOCOL_VERSION
    assert doc["rootComponent"] == "root"
    # Every entry must say what it maps to, or the client has nothing to render.
    for name, spec in doc["components"].items():
        assert spec["ariaWidget"], name
        assert spec["description"], name
        assert spec["properties"], name
    assert doc["limits"]["maxComponents"] == C.MAX_COMPONENTS


def test_catalog_ids_are_unique_and_sorted_deterministically():
    a = list(C.CATALOG_DOC_KEYS) if hasattr(C, "CATALOG_DOC_KEYS") else list(C.CATALOG)
    assert len(a) == len(set(a))
    doc1 = C.catalog_document()
    doc2 = C.catalog_document()
    assert list(doc1["components"]) == list(doc2["components"])


def test_prompt_mentions_every_component_and_the_key_rules():
    p = C.catalog_prompt()
    for name in C.CATALOG:
        assert name in p, f"{name} missing from the prompt"
    assert "root" in p
    assert "rejected" in p
    # The read-only guarantee must be stated, or a model will happily emit a
    # text field and a submit button.
    assert "no text input" in p
    assert "rejected" in p


def test_prompt_shows_enum_values_so_the_model_cannot_guess_them():
    """`gap: "small"` was generated and rejected ("not one of loose, normal,
    tight"), costing a retry on the first live generate. The catalog knows the
    values; the prompt that quotes the catalog must show them."""
    p = C.catalog_prompt()
    assert "gap[tight|normal|loose]" in p
    assert "variant[body|caption|heading|mono]" in p
    for name, spec in C.CATALOG.items():
        for prop, kind in spec["props"].items():
            if not kind.startswith("enum:"):
                continue
            for value in kind[5:].split("|"):
                assert value in p, f"{name}.{prop} value {value!r} not in prompt"


def test_catalog_has_no_interactive_text_input():
    """Deliberate. A generated form whose submit performs an action is an
    approval flow wearing a disguise."""
    for name, spec in C.CATALOG.items():
        props = " ".join(spec["props"]).lower()
        assert "submit" not in props, name
        assert "onclick" not in props, name
    assert "TextField" not in C.CATALOG
    assert "Form" not in C.CATALOG


# ── the happy path ───────────────────────────────────────────────────────────

def test_a_validated_surface_carries_no_internal_state():
    """`_children` is working state for the depth check, derived from props.
    Leaving it in the returned value shipped bookkeeping the client then had to
    know to ignore."""
    out = C.validate_surface(ok())
    assert "_children" not in json.dumps(out), out


def test_data_bindings_resolve_in_the_text_rendering():
    out = C.validate_surface(ok(components=[
        {"id": "root", "component": "Column", "children": ["a"]},
        {"id": "a", "component": "KeyValue", "label": "Runs",
         "value": {"path": "/stats/runs"}},
    ], data={"stats": {"runs": 42}}))
    assert "42" in out["text"], out["text"]


def test_all_catalog_components_are_accepted_at_the_shape_level():
    """Every advertised component must actually validate, or the catalog
    documents a promise the validator breaks.

    Components taking a single `child` get a real second component to point at.
    Pointing them at their own id would be a self-cycle - which the validator
    correctly rejects, so the fixture has to be a genuine tree.
    """
    for name, spec in C.CATALOG.items():
        # A real leaf for components that take a single `child`. Pointing a
        # component at its own id would be a self-cycle, which the validator
        # correctly rejects, so the fixture has to be a genuine tree.
        others = ([{"id": "leaf", "component": "Text", "text": "x"}]
                  if "child" in spec["props"] else [])
        props = {}
        for pname, ptype in spec["props"].items():
            if ptype == "ids":
                props[pname] = ["leaf"]
            elif ptype == "id":
                props[pname] = "leaf"
            elif ptype == "text|literal":
                props[pname] = "x"
            elif ptype == "number":
                props[pname] = 0.5
            elif ptype == "list<{key,label}>":
                # A Table with no columns is not a valid instance - it would
                # render an empty grid - so the shape fixture has to supply a
                # real column. `[]` is the right value for the other list props
                # (DataList rows, FilterChips options) and wrong for this one.
                props[pname] = [{"key": "a", "label": "A"}]
            elif ptype == "list<scalar>":
                props[pname] = [{"a": "1"}]
            elif ptype.startswith("list<"):
                props[pname] = []
            elif ptype == "bool":
                props[pname] = True
            elif ptype.startswith("enum:"):
                props[pname] = ptype.split(":", 1)[1].split("|")[0]
        root = {"id": "root", "component": name, **props}
        if "children" in spec["props"] and not others:
            # Nothing to point at; an empty child list is valid and is what a
            # container with no content should send.
            root["children"] = []
        comps = [root] + others
        C.validate_surface({"surfaceId": "s", "components": comps})


# ── attacks: unknown components ──────────────────────────────────────────────

def test_an_invented_component_is_rejected():
    try:
        C.validate_surface(ok(components=[
            {"id": "root", "component": "Column", "children": []},
            {"id": "x", "component": "Script", "source": "alert(1)"},
        ]))
    except C.CatalogError as e:
        assert "unknown component" in str(e)
        # The message must be actionable: tell it what IS available.
        assert "Column" in str(e)
    else:
        raise AssertionError("an invented component must be rejected")


def test_an_invented_property_is_rejected():
    try:
        C.validate_surface(ok(components=[
            {"id": "root", "component": "Text", "text": "x", "onclick": "steal()"},
        ]))
    except C.CatalogError as e:
        assert "onclick" in str(e)
    else:
        raise AssertionError("an unknown property must be rejected")


# ── attacks: markup, styles, handlers ────────────────────────────────────────

def test_markup_and_handler_properties_are_refused_outright():
    """These get a specific message, not the generic "unknown property" one,
    because they are the attacks rather than mistakes."""
    for prop, value in (
        ("html", "<img src=x onerror=alert(1)>"),
        ("innerHTML", "<script>alert(1)</script>"),
        ("dangerouslySetInnerHTML", {"__html": "<b>x</b>"}),
        ("style", {"position": "fixed"}),
        ("className", "fixed inset-0"),
        ("src", "https://evil.example/x.png"),
        ("href", "javascript:alert(1)"),
        ("onClick", "fetch('https://evil.example')"),
    ):
        try:
            C.validate_surface(ok(components=[
                {"id": "root", "component": "Text", "text": "x", prop: value},
            ]))
        except C.CatalogError as e:
            assert "not allowed" in str(e), f"{prop}: {e}"
        else:
            raise AssertionError(f"{prop!r} must be refused")


def test_a_url_shaped_value_in_an_allowed_property_is_not_fetched():
    """`Action` has an `action` event, and `Link` a console-local target.
    Neither may carry an arbitrary URL, so neither is given a url property -
    asserted here so a future "just add href" is caught."""
    for name in ("Action", "Link"):
        assert "href" not in C.CATALOG[name]["props"]
        assert "url" not in C.CATALOG[name]["props"]


# ── attacks: denial of service ───────────────────────────────────────────────

def test_too_many_components_is_rejected():
    comps = [{"id": "root", "component": "Column", "children": []}]
    comps += [{"id": f"t{i}", "component": "Text", "text": "x"}
              for i in range(C.MAX_COMPONENTS + 5)]
    try:
        C.validate_surface({"surfaceId": "s", "components": comps})
    except C.CatalogError as e:
        assert "exceeds the limit" in str(e)
    else:
        raise AssertionError("component flood must be rejected")


def test_too_many_children_under_one_parent_is_rejected():
    kids = [f"t{i}" for i in range(C.MAX_NODES_PER_PARENT + 1)]
    comps = [{"id": "root", "component": "Column", "children": kids}]
    comps += [{"id": k, "component": "Text", "text": "x"} for k in kids]
    try:
        C.validate_surface({"surfaceId": "s", "components": comps})
    except C.CatalogError as e:
        assert "children" in str(e)
    else:
        raise AssertionError("a single parent with thousands of children must "
                             "be rejected")


def test_deep_nesting_is_rejected():
    comps = []
    depth = C.MAX_DEPTH + 6
    for i in range(depth):
        nxt = [f"n{i + 1}"] if i + 1 < depth else []
        comps.append({"id": f"n{i}", "component": "Column", "children": nxt})
    comps[0]["id"] = "root"
    try:
        C.validate_surface({"surfaceId": "s", "components": comps})
    except C.CatalogError as e:
        assert "nesting" in str(e)
    else:
        raise AssertionError("deep nesting must be rejected")


def test_a_component_cycle_is_rejected_not_hung_on():
    """A cycle is a denial of service wearing a tree costume. The validator has
    to detect it with a visited set, not blow the stack."""
    comps = [
        {"id": "root", "component": "Column", "children": ["b"]},
        {"id": "b", "component": "Column", "children": ["c"]},
        {"id": "c", "component": "Column", "children": ["b"]},
    ]
    try:
        C.validate_surface({"surfaceId": "s", "components": comps})
    except C.CatalogError as e:
        assert "cycle" in str(e)
    else:
        raise AssertionError("a cycle must be rejected")


def test_a_cycle_through_a_non_first_child_is_rejected():
    """The depth walker used to follow only _children[0], so a cycle
    hiding behind a second child was invisible."""
    comps = [
        {"id": "root", "component": "Column", "children": ["a", "x"]},
        {"id": "a", "component": "Column", "children": ["b"]},
        {"id": "b", "component": "Column", "children": []},
        # x is root's SECOND child; the cycle runs through it.
        {"id": "x", "component": "Column", "children": ["y"]},
        {"id": "y", "component": "Column", "children": ["root"]},
    ]
    try:
        C.validate_surface({"surfaceId": "s", "components": comps})
    except C.CatalogError as e:
        assert "cycle" in str(e)
    else:
        raise AssertionError("a cycle through a non-first child must "
                             "be rejected")


def test_deep_nesting_behind_a_second_child_is_rejected():
    """Depth hiding behind a second child: the first-child-only walk
    never saw it, so a deep chain slipped past MAX_DEPTH."""
    depth = C.MAX_DEPTH + 6
    comps = [{"id": "root", "component": "Column",
              "children": ["decoy0", "n0"]}]
    for i in range(depth):
        kids = ([f"decoy{i + 1}", f"n{i + 1}"]
                if i + 1 < depth else [])
        comps.append({"id": f"n{i}", "component": "Column",
                      "children": kids})
        comps.append({"id": f"decoy{i}", "component": "Text",
                      "text": "x"})
    try:
        C.validate_surface({"surfaceId": "s", "components": comps})
    except C.CatalogError as e:
        assert "nesting" in str(e)
    else:
        raise AssertionError("deep nesting behind a second child must "
                             "be rejected")


def test_a_converging_dag_is_not_a_cycle():
    """Two branches meeting at one node is a legal DAG, not a cycle —
    the walker must not read re-visit as a loop."""
    comps = [
        {"id": "root", "component": "Column", "children": ["a", "b"]},
        {"id": "a", "component": "Column", "children": ["d"]},
        {"id": "b", "component": "Column", "children": ["d"]},
        {"id": "d", "component": "Text", "text": "shared"},
    ]
    C.validate_surface({"surfaceId": "s", "components": comps})


def test_oversized_text_is_rejected():
    try:
        C.validate_surface(ok(components=[
            {"id": "root", "component": "Text",
             "text": "x" * (C.MAX_TEXT_LEN + 1)},
        ]))
    except C.CatalogError as e:
        assert "limit" in str(e)
    else:
        raise AssertionError("oversized text must be rejected")


def test_text_budget_is_cumulative_not_just_per_field():
    """Every field under the per-field cap can still add up to a megabyte."""
    comps = [{"id": "root", "component": "Column", "children": []}]
    for i in range(40):
        comps.append({"id": f"t{i}", "component": "Text",
                      "text": "x" * C.MAX_TEXT_LEN})
    try:
        C.validate_surface({"surfaceId": "s", "components": comps})
    except C.CatalogError as e:
        assert "total" in str(e)
    else:
        raise AssertionError("the cumulative text budget must be enforced")


def test_an_individually_valid_but_huge_surface_is_rejected():
    """The component and text caps are not sufficient on their own."""
    comps = [{"id": "root", "component": "Column", "children": []}]
    for i in range(300):
        comps.append({"id": f"t{i}", "component": "CodeBlock",
                      "text": "y" * 200})
    payload = {"surfaceId": "s", "components": comps,
               "pad": "z" * 200_000}
    try:
        C.validate_surface(payload)
    except C.CatalogError as e:
        assert "bytes" in str(e) or "limit" in str(e)
    else:
        raise AssertionError("a 200KB-plus surface must be rejected")


# ── structural integrity ─────────────────────────────────────────────────────

def test_a_dangling_child_reference_is_rejected():
    """It would render as an invisible hole, so the surface looks complete but
    is missing content."""
    try:
        C.validate_surface(ok(components=[
            {"id": "root", "component": "Column", "children": ["nope"]},
        ]))
    except C.CatalogError as e:
        assert "unknown child" in str(e)
    else:
        raise AssertionError("a dangling child reference must be rejected")


def test_a_missing_root_is_rejected():
    try:
        C.validate_surface(ok(components=[
            {"id": "notroot", "component": "Text", "text": "x"},
        ]))
    except C.CatalogError as e:
        assert "root" in str(e)
    else:
        raise AssertionError("a surface with no root must be rejected")


def test_duplicate_ids_are_rejected():
    try:
        C.validate_surface(ok(components=[
            {"id": "root", "component": "Column", "children": []},
            {"id": "root", "component": "Text", "text": "x"},
        ]))
    except C.CatalogError as e:
        assert "duplicate" in str(e)
    else:
        raise AssertionError("duplicate ids must be rejected")


def test_an_overlong_or_hostile_surface_id_is_rejected():
    """surfaceId appears in message envelopes and DOM ids, so it is restricted
    to characters that cannot break either."""
    for bad in ("", "  ", "a" * 100, "has space", "has/slash", "<script>",
                "quote'd", "semi;colon"):
        try:
            C.validate_surface({"surfaceId": bad, "components": [
                {"id": "root", "component": "Text", "text": "x"}]})
        except C.CatalogError:
            pass
        else:
            raise AssertionError(f"surfaceId {bad!r} should be rejected")
    # A legitimate id still works.
    C.validate_surface({"surfaceId": "runs_filter-1", "components": [
        {"id": "root", "component": "Text", "text": "x"}]})


def test_a_foreign_catalog_id_is_rejected():
    """The agent must not be able to point us at somebody else's catalog."""
    try:
        C.validate_surface(ok(catalogId="google/basic"))
    except C.CatalogError as e:
        assert "unknown catalogId" in str(e)
    else:
        raise AssertionError("a foreign catalogId must be rejected")


def test_a_non_json_serialisable_surface_is_rejected():
    try:
        C.validate_surface({"surfaceId": "s", "components": [
            {"id": "root", "component": "Text", "text": object()}]})
    except C.CatalogError as e:
        assert "serialis" in str(e) or "text" in str(e)
    else:
        raise AssertionError("a non-serialisable payload must be rejected")


# ── the validated form must itself validate ────────────────────────────────
# `/api/a2ui/validate` and `/api/a2ui/generate` return the NORMALISED surface
# (`components[].props`), and the API tells clients to re-validate a surface
# they already hold. That round trip failed with `unknown property 'props'`
# because only the flat wire form was accepted on input.

def test_a_validated_surface_validates_again():
    surface = C.validate_surface(ok(components=[
        {"id": "root", "component": "Column", "children": ["head", "table"]},
        {"id": "head", "component": "Text", "text": "Runs", "variant": "heading"},
        {"id": "table", "component": "Table",
         "columns": [{"key": "run", "label": "Run"}],
         "rows": [{"run": "s8-1"}]},
    ]))
    again = C.validate_surface(surface)
    assert again["components"] == surface["components"], (
        "the surface a client received from the server does not validate")


def test_the_nested_props_form_is_flattened_not_shipped_back_nested():
    out = C.validate_components([
        {"id": "root", "component": "Column", "props": {"children": ["t"]}},
        {"id": "t", "component": "Text", "props": {"text": "hello"}},
    ])
    assert out[0]["props"] == {"children": ["t"]}
    assert out[1]["props"] == {"text": "hello"}
    assert "props" not in out[1]["props"]


def test_flat_keys_win_when_a_component_says_it_twice():
    out = C.validate_components([
        {"id": "root", "component": "Text", "props": {"text": "nested"},
         "text": "flat"},
    ])
    assert out[0]["props"]["text"] == "flat"


def test_a_non_object_props_is_rejected_by_name():
    try:
        C.validate_components([
            {"id": "root", "component": "Column", "props": ["children"]}])
    except C.CatalogError as e:
        assert "props must be an object" in str(e), str(e)
    else:
        raise AssertionError("a non-object props must be rejected")


# ── table row shapes ────────────────────────────────────────────────────────
# Generated surfaces produced rows as an object of column arrays and failed
# 2 of 2 with `rows must be an array`. Both column-oriented near-misses are
# unambiguous given `columns`, so they are transposed rather than refused.

def test_column_major_table_rows_are_transposed():
    out = C.validate_components([
        {"id": "root", "component": "Table",
         "columns": [{"key": "run", "label": "Run"},
                     {"key": "cost", "label": "Cost"}],
         "rows": {"run": ["s8-1", "s8-2"], "cost": [0.42, 0.31]}}])
    assert out[0]["props"]["rows"] == [
        {"run": "s8-1", "cost": 0.42},
        {"run": "s8-2", "cost": 0.31},
    ]


def test_positional_table_rows_are_transposed_in_column_order():
    out = C.validate_components([
        {"id": "root", "component": "Table",
         "columns": [{"key": "a", "label": "A"},
                     {"key": "b", "label": "B"}],
         "rows": [["1", "2"], ["3", "4"]]}])
    assert out[0]["props"]["rows"] == [{"a": "1", "b": "2"},
                                       {"a": "3", "b": "4"}]


def test_a_table_row_binding_is_refused_with_the_expected_shape():
    try:
        C.validate_components([
            {"id": "root", "component": "Table",
             "columns": [{"key": "run", "label": "Run"}],
             "rows": {"path": "/runs"}}])
    except C.CatalogError as e:
        assert "literal array of row objects" in str(e), str(e)
    else:
        raise AssertionError("a rows binding is not a documented shape")


def test_a_ragged_column_major_table_is_refused_rather_than_padded():
    try:
        C.validate_components([
            {"id": "root", "component": "Table",
             "columns": [{"key": "a", "label": "A"},
                         {"key": "b", "label": "B"}],
             "rows": {"a": ["1", "2"], "b": ["3"]}}])
    except C.CatalogError as e:
        assert "different lengths" in str(e), str(e)
    else:
        raise AssertionError("ragged rows must be refused, not padded")


def test_a_path_column_is_data_not_a_binding():
    """"path" is an ordinary column key (a table of files), and a
    column-major table that happens to use it must still transpose. The
    binding guard must key on a STRING value, not on the name alone."""
    out = C.validate_components([
        {"id": "root", "component": "Table",
         "columns": [{"key": "path", "label": "Path"},
                     {"key": "words", "label": "Words"}],
         "rows": {"path": ["/a.pdf", "/b.pdf"], "words": [10, 20]}}])
    assert out[0]["props"]["rows"] == [
        {"path": "/a.pdf", "words": 10},
        {"path": "/b.pdf", "words": 20},
    ]



def test_scalar_bindings_are_resolved_before_the_console_sees_them():
    """The generation prompt tells the model to BIND, but the renderer reads
    `props` literally - a bound KeyValue shipped as `[object Object]`."""
    out = C.validate_surface(ok(components=[
        {"id": "root", "component": "Column", "children": ["k"]},
        {"id": "k", "component": "KeyValue", "label": "Total",
         "value": {"path": "/stats/runs"}},
    ], data={"stats": {"runs": 42}}))
    assert out["components"][1]["props"]["value"] == 42
    assert "42" in out["text"]


def test_an_object_prop_is_never_rewritten_as_a_binding():
    """`Action.args` carries an object. `{"path": "C:\\reports\\q3.pdf"}` is
    a file argument there, not a JSON Pointer, and resolving it would break
    the action."""
    out = C.validate_surface(ok(components=[
        {"id": "root", "component": "Action", "label": "Open",
         "action": "read_file", "args": {"path": "/home/u/report.txt"}},
    ], data={}))
    assert out["components"][0]["props"]["args"] == {
        "path": "/home/u/report.txt"}


def test_a_binding_to_a_missing_key_is_rejected():
    try:
        C.validate_surface(ok(components=[
            {"id": "root", "component": "KeyValue", "label": "Total",
             "value": {"path": "/nope"}},
        ], data={"total": 1}))
    except C.CatalogError as e:
        assert "not in the surface's data" in str(e), str(e)
    else:
        raise AssertionError("an unresolved binding must not reach the client")


# ── pointer resolution ──────────────────────────────────────────────────────

def test_pointer_resolution_is_bounded_and_safe():
    assert C._resolve({"a": {"b": 1}}, "/a/b") == 1
    assert C._resolve({"a": [10, 20]}, "/a/1") == 20
    assert C._resolve({"a": 1}, "/nope") is None
    assert C._resolve({"a": 1}, "no-slash") is None
    # Escaping must not walk out of the root.
    assert C._resolve({"a": {"b": {"c": 1}}}, "/a/b/../../secret") is None
    # A very long path must terminate rather than grind.
    assert C._resolve({"a": 1}, "/" + "/".join(["a"] * 200)) is None


# ── enum enforcement ──────────────────────────────────────────────────────

def test_enum_properties_are_enforced_not_merely_described():
    """`enum:` type strings were only ever prose. `Link.target` claimed to be
    validated to `enum:console`, so a surface could name any URL and pass
    validation — harmless only while the client renders a <span> rather than a
    real anchor, which is exactly the trap the comment invited."""
    def _surface(**props):
        return [{"id": "root", "component": "Column", "children": ["l"]},
                {"id": "l", "component": "Link", **props}]

    try:
        C.validate_components(
            _surface(label="x", target="https://evil.example"))
    except C.CatalogError as e:
        assert "not one of" in str(e), str(e)
    else:
        raise AssertionError(
            "Link.target accepted an out-of-range value; the enum is decoration")

    C.validate_components(_surface(label="x", target="console"))


def test_every_declared_enum_is_actually_enforced():
    """Guards the guard: if a property is declared `enum:...`, an out-of-range
    value must be refused. Otherwise the declaration is decoration."""
    checked = 0
    for name, spec in C.CATALOG.items():
        for prop, declared in spec["props"].items():
            if not declared.startswith("enum:"):
                continue
            checked += 1
            comp = {"id": "x", "component": name, prop: "zzz-not-a-member"}
            try:
                C.validate_components(
                    [{"id": "root", "component": "Column", "children": ["x"]},
                     comp])
            except C.CatalogError:
                continue
            raise AssertionError(
                f"{name}.{prop} is declared {declared} but accepted "
                f"'zzz-not-a-member'")
    assert checked >= 5, f"only {checked} enum properties found — too few to be sure"


def test_the_enum_rule_does_not_constrain_free_text():
    def _text(value):
        return [{"id": "root", "component": "Column", "children": ["t"]},
                {"id": "t", "component": "Text", "text": value}]

    C.validate_components(_text("tight normal loose — whatever else"))
    C.validate_components(_text(""))
