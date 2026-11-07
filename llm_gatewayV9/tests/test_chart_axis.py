"""Value-axis selection for charts.

Why this file exists
--------------------
`_chart` used to size the two ends of the axis from different things: the
MINIMUM from the data (`min(0.0, lo)`) and the STEP from the MAXIMUM ONLY
(`_nice_step(hi/4)`). Two measurable consequences, both confirmed by computing
the old formula directly:

* `[-500, 10, 20]` → step 5 across a span of 520 = **104 gridlines**. `[-900,
  -10]` → **3,604**. Either the renderer stalls or the chart prints as a solid
  block of lines. Any negative value at all triggered this, and a report about
  "change since last year" has negative values constantly.
* Positive data was squashed. Rounding the step UP then multiplying by a fixed 4
  overshoots: `[1, 5, 500]` got an axis to 800, so the tallest bar filled 62.5 %
  of the plot and the top third was empty. `[10, 20, 30]` got 40 for a max of 30.

These tests assert the axis, not the pixels, because a gridline count of 3,604
is a fact about the numbers and is cheaper to prove than a screenshot.
"""
from __future__ import annotations

import sys
from pathlib import Path

GW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GW))

import pytest

from docgen import value_axis


def _ticks(axis):
    bot, step, top = axis
    return round((top - bot) / step)


@pytest.mark.parametrize("values", [
    [10, 20, 30],
    [1, 5, 500],
    [-500, 10, 20],     # the 104-gridline case
    [-900, -10],        # the 3,604-gridline case
    [0.001, 0.002],
    [94, 91, 82],
    [0, 0],
    [42],
    [7, 7, 7],
    [-1, -2],
])
def test_the_axis_never_explodes_into_hundreds_of_gridlines(values):
    """The single invariant that was actually broken."""
    ax = value_axis(values)
    assert ax is not None
    bot, step, top = ax
    assert step > 0
    assert top >= max(values) and bot <= min(values), (
        f"axis {ax} does not cover {values}")
    assert _ticks(ax) <= 8, (
        f"{values} produced {_ticks(ax)} gridlines; a readable chart has ~3-6")
    assert _ticks(ax) >= 2


@pytest.mark.parametrize("values", [
    [1, 5, 500], [10, 20, 30], [94, 91, 82], [0.5, 1.5], [100, 404],
])
def test_a_positive_series_is_not_squashed_into_the_lower_half(values):
    """The defect a reader sees on ordinary data.

    62.5 % was measured for [1, 5, 500] under the old formula: the axis ran to
    800 so a third of the plot above the tallest bar was blank, and bar height
    is the thing the eye is asked to compare.
    """
    bot, step, top = value_axis(values)
    fill = (max(values) - bot) / (top - bot)
    assert fill >= 0.80, (
        f"{values}: tallest bar fills only {fill:.1%} of the plot height")


def test_bar_axes_keep_a_zero_baseline():
    """A bar chart that does not start at zero is a lie about ratios — the bar
    length is the encoding, so the baseline is not a stylistic choice."""
    for values in ([10, 20, 30], [900, 910, 920], [1, 5, 500]):
        bot, _step, _top = value_axis(values, want_zero=True)
        assert bot <= 0 <= _top


def test_a_series_entirely_below_zero_still_gets_a_sane_axis():
    """The old code set `top = hi if hi > 0 else 1.0`, so an all-negative series
    got an axis topping out above zero while every bar hung from it — and the
    step, sized from that fake 1.0, produced thousands of lines below it."""
    bot, step, top = value_axis([-900, -10])
    assert top >= 0
    assert bot <= -900
    assert _ticks((bot, step, top)) <= 8


def test_a_flat_series_does_not_degenerate():
    """span == 0 divides to nothing; the axis must still have width."""
    for values in ([7, 7, 7], [0, 0]):
        bot, step, top = value_axis(values)
        assert top > bot, (bot, step, top)
        assert step > 0


def test_empty_and_junk_input_returns_none_rather_than_raising():
    assert value_axis([]) is None
    assert value_axis([None, "x", float("nan")]) is None


def test_the_rendered_pdf_actually_draws_bars():
    """End-to-end guard: the helper being right means nothing if `_chart` does
    not use it. Count bar rectangles in the produced PDF."""
    import io
    import re

    import docgen
    from pypdf import PdfReader

    data, _name, _ctype = docgen.generate("pdf", {
        "title": "axis check",
        "blocks": [
            {"type": "chart", "kind": "bar", "title": "t",
             "categories": ["a", "b", "c"],
             "series": [{"name": "s", "data": [1, 5, 500]}]},
        ]})
    reader = PdfReader(io.BytesIO(data))
    page = reader.pages[0]
    content = page.get_contents()
    raw = content.get_data() if content is not None else b""
    heights = [float(m.group(1)) for m in
               re.finditer(rb"[\d.]+ [\d.]+ [\d.]+ ([\d.]+)\s+re", raw)
               if float(m.group(1)) > 2.0]
    # 3 bars, and under the old axis the 500-bar would sit at 62 % of the plot.
    assert heights, "no bar rectangles were drawn at all"
    assert max(heights) > 100.0, (
        f"tallest bar is {max(heights):.1f}pt of a ~142pt plot; the axis is "
        f"still inflating. heights={sorted(set(heights), reverse=True)[:6]}")
