"""Every tool schema must be accepted by the providers we actually route to.

Measured failure: the `typeset` parameter was declared as
`enum: ["", "latex"]`. Gemini rejects the whole request with

    GenerateContentRequest.tools[0].function_declarations[0].parameters
    .properties[typeset].enum[0]: cannot be empty

which surfaced as a 502 from the gateway and killed an `author` node before it
had written a word — a schema mistake in one optional argument made an entire
document run fail, and the error text pointed at `/v1/chat`, not at the schema.

An empty string is never a meaningful enum member: "omit the parameter" is how
a caller says "use the default", and both providers enforce that. This test
walks the whole catalog so the next such enum cannot ship.
"""
from __future__ import annotations

import pytest

import skills


def _walk(node, path=""):
    """Yield (json_path, value) for every enum list anywhere in a schema."""
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if k == "enum" and isinstance(v, list):
                yield here, v
            else:
                yield from _walk(v, here)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


@pytest.fixture(scope="module")
def catalog():
    return skills._TOOL_CATALOG


def test_no_enum_contains_an_empty_string(catalog):
    bad = []
    for name, spec in catalog.items():
        schema = spec.get("input_schema") or {}
        for where, values in _walk(schema):
            if any(isinstance(v, str) and v == "" for v in values):
                bad.append(f"{name}{where} = {values!r}")
    assert not bad, ("an empty enum member is rejected by Gemini and fails "
                     "the whole tool-calling request:\n  " + "\n  ".join(bad))


def test_every_enum_is_a_list_of_strings(catalog):
    bad = []
    for name, spec in catalog.items():
        for where, values in _walk(spec.get("input_schema") or {}):
            if not values or not all(isinstance(v, str) for v in values):
                bad.append(f"{name}{where} = {values!r}")
    assert not bad, "\n  ".join(bad)


def test_typeset_declares_only_the_value_it_actually_handles(catalog):
    """Regression pin for the exact field that broke the run."""
    props = (catalog["render_document"]["input_schema"]["properties"])
    assert props["typeset"]["enum"] == ["latex"]
    assert "omit" in props["typeset"]["description"].lower()
