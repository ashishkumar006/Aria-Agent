"""The gateway's own calls tables must show the token rate, and stay aligned.

A `tk/s` column was asked for alongside In and Out on the ledger. Two tables
carry that ledger — the dashboard's "Recent calls" and the Ledger page's
filtered list — plus a CSV export, and the failure mode of adding a column is
always the same one: the header, the row template and the `colspan` of the
empty-state row drift apart, and every row after the change renders shifted by
a cell while looking superficially fine.

The formatter lives in `static/gateway.js` so the two tables cannot disagree
about what counts as slow, and a missing rate must read as a dash rather than
`0.0`: rows written before the column existed have no rate, and "no rate
recorded" is a different fact from "the model generated nothing".
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_the_shared_formatter_exists_and_distinguishes_absent_from_zero():
    js = _read("gateway.js")
    assert "window.tpsCell" in js and "function tpsCell" in js
    # A missing/zero rate returns the bare dash, never a formatted number.
    body = js[js.index("function tpsCell"):js.index("function tpsText")]
    assert "return '—'" in body, (
        "tpsCell no longer renders a missing rate as a dash; a pre-column row "
        "would display as 0.0 tk/s and read like a stalled model")
    assert "thinking_tokens" in body, (
        "the rate is generated-tokens-per-second, so thinking has to be shown "
        "with it or a fast thinking call still looks wrong")


def test_dashboard_header_row_and_empty_state_agree_on_the_column_count():
    html = _read("dashboard.html")
    head = re.search(r"<tr><th>Time</th>.*?</tr>", html, re.S)
    assert head, "the calls table header was not found"
    cols = head.group(0).count("<th>")
    assert cols == 13, f"dashboard header now has {cols} columns"
    tmpl = re.search(r"tr\.innerHTML = `([\s\S]*?)</td>`;", html)
    assert tmpl, "the dashboard row template was not found"
    cells = len(re.findall(r"<td[ >]", tmpl.group(1)))
    assert cells == cols, (
        f"dashboard renders {cells} cells against {cols} headers — every row "
        f"after the change is shifted one column")
    assert "tpsCell" in tmpl.group(1), "the Tk/s column is not rendered"


def test_ledger_page_carries_the_column_and_fixed_its_colspan():
    html = _read("ledger.html")
    head = re.search(r"<thead><tr><th>Time</th>.*?</tr></thead>", html, re.S)
    assert head, "the ledger calls header was not found"
    cols = head.group(0).count("<th>")
    assert cols == 10, f"ledger header now has {cols} columns"
    # The row template is a chain of four template literals concatenated with
    # `+`, so it has to be taken as one statement rather than by line. The page
    # has other `<tr>` templates, so the search is scoped to this function.
    # Anchor on the row's opening `<tr>` and its closing `</tr>`;`; the
    # empty-state cell is a separate string and must not count as a data cell.
    region = html[html.index("function renderLedgerCalls"):
                  html.index("function exportLedgerCSV")]
    tmpl = re.search(r"return `(<tr>[\s\S]*?)</tr>`;", region)
    assert tmpl, "the ledger row template was not found"
    cells = len(re.findall(r"<td[ >]", tmpl.group(1)))
    assert cells == cols, f"ledger renders {cells} cells against {cols} headers"
    assert "tpsCell" in tmpl.group(1)
    span = re.search(r'colspan="(\d+)"',
                     html[html.index("function renderLedgerCalls"):
                          html.index("function exportLedgerCSV")])
    assert span and int(span.group(1)) == cols, (
        f"the empty-state row spans {span and span.group(1)} of {cols} columns")


def test_the_csv_export_gained_the_field_without_breaking_alignment():
    """The header string and the row array are two lists that must match by
    position; adding to one and not the other silently mislabels every column
    in the spreadsheet."""
    html = _read("ledger.html")
    head = re.search(r"\['(ts,time,[^']*)'\]", html)
    assert head, "the CSV header line changed shape"
    names = head.group(1).split(",")
    body = html[html.index("const csv ="):html.index("URL.createObjectURL")]
    # Position of each header name must line up with the emitted values.
    assert "tk_s" in names and "thinking_tokens" in names
    assert names.index("tk_s") == names.index("out") + 1
    assert body.count("c.input_tokens || 0") == 1
    assert "Number(c.output_tps)" in body and "c.thinking_tokens" in body
    assert len(names) == 13, (
        f"CSV header has {len(names)} names; the row array must be the same length")
