"""File parsers for uploaded documents.

Each parser returns a list of `documents.chunker.Block` — structure preserved,
not flattened to a wall of text. The chunker needs to know where a heading
ends and a paragraph begins; a PDF's raw text stream gives it no way to tell.

Supported: PDF, DOCX, MD, TXT, HTML, CSV, XLSX.

Every parser is best-effort but never silent: parse problems (an unreadable
page, a skipped sheet) are collected into `warnings` on the result so the
console can show them, rather than producing a document that silently lost
half its content.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from documents.chunker import Block

SUPPORTED = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
    ".text": "txt",
    ".log": "txt",
    ".html": "html",
    ".htm": "html",
    ".csv": "csv",
    ".tsv": "csv",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
}

SUPPORTED_HUMAN = ("PDF", "DOCX", "Markdown", "plain text", "HTML",
                   "CSV/TSV", "Excel (xlsx)")

# The gateway's embed endpoint rejects anything over this per input.
MAX_CHUNK_CHARS = 8000


@dataclass
class ParseResult:
    blocks: list[Block] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    doc_type: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def text_length(self) -> int:
        return sum(len(b.text or "") for b in self.blocks)


class UnsupportedDocument(ValueError):
    def __init__(self, ext: str, supported=SUPPORTED_HUMAN):
        self.ext = ext
        self.supported = supported
        super().__init__(
            f"{ext or 'that file type'} is not supported. "
            f"Supported: {', '.join(supported)}")


def detect_type(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    kind = SUPPORTED.get(ext)
    if not kind:
        raise UnsupportedDocument(ext or "(none)")
    return kind


def parse(filename: str, data: bytes) -> ParseResult:
    """Dispatch on the file extension. Raises UnsupportedDocument for
    anything not in SUPPORTED rather than guessing."""
    kind = detect_type(filename)
    fn = {
        "pdf": parse_pdf, "docx": parse_docx, "md": parse_markdown,
        "txt": parse_text, "html": parse_html, "csv": parse_csv,
        "xlsx": parse_xlsx,
    }[kind]
    res = fn(data)
    res.doc_type = kind
    if not res.blocks:
        res.warnings.append("no readable text found in this file")
    return res


# ── helpers ──────────────────────────────────────────────────────────────────
def _dec(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _clean(text: str) -> str:
    """Collapse the whitespace damage documents carry without touching the
    text itself: soft hyphens, zero-width chars, repeated blank lines, and
    non-breaking spaces (which otherwise read as words glued together)."""
    text = text.replace("\u00ad", "")            # soft hyphen
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)   # zero-width
    text = text.replace("\xa0", " ")            # non-breaking space
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── PDF table recovery ──────────────────────────────────────────────────────
# Why this exists, concretely: a weekly menu PDF arrived as one flattened
# paragraph. The day names ("Monday Tuesday ... Sunday") appeared once, in the
# header, and ~35 dish names followed as an unstructured run. A question like
# "what is the menu on monday" was unanswerable from it - not because retrieval
# failed (all chunks came back) but because the association between a day and
# its dishes had been destroyed before chunking. The model, told to say so
# rather than guess, correctly refused.
#
# `page.extract_text()` cannot recover that: a borderless table has no ruling
# lines to align to. So we ask pdfplumber, which can align on text position
# instead, and then REBUILD the logical grid - because text alignment alone
# splits a wrapped cell across two physical columns ("Rajma Dal (" | "100gm)").

_PDF_TABLE_SETTINGS = {
    # Borderless tables: align to the words themselves, not to ruling lines.
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "intersection_tolerance": 5,
    "text_tolerance": 2,
}

# Ruled tables: trust the ruling, never the text alignment.
_PDF_TABLE_SETTINGS_RULED = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
}
# A page must carry at least this many ruling lines/rects before
# the ruled-grid path runs. Prose pages carry a handful of stray
# rules (underlines, a header rule, bullet leaders) and
# lines-based extraction turns three of them into a 1-3 row
# non-table; a real grid has one line per row and column
# boundary (measured on a placement bluebook: prose pages carry
# 3-5, its degree tables carry 8-25).
_MIN_RULING_LINES = 8

# Below these, a "header" row is just text and guessing a grid would invent
# structure that is not there.
_MIN_TABLE_COLS = 3
_MIN_TABLE_ROWS = 3
# How many distinct lines below the header must start at (or within a
# couple points of) EACH column start before the grid is believed.
# Real table cells are left-aligned under their headers, so every wrapped
# line in a column starts at the column's x. Multi-column PROSE has no
# such regularity - words start wherever the justification lands them -
# so without this check a brochure page's heading words pass every shape
# test and the whole page is shredded into a fake table (observed live:
# a 6 MB placement bluebook parsed as 64 table blocks and zero prose,
# with justified words split mid-word).
_MIN_COLUMN_ANCHORS = 3
# Fraction of ALL words below the header that start at (or within a
# couple points of) SOME column start. Measured on the shipped menu
# (a real borderless grid): 0.36 - cells are left-aligned under their
# headers, so a third of every word sits at a column start. Measured on
# the bluebook's prose pages (false positives): 0.04-0.20 - justified
# text starts words wherever it lands. Anything below the threshold is
# prose and is declined here rather than shredded into a fake grid.
_MIN_COLUMN_ALIGN_FRACTION = 0.25


def _cell(row: list, i: int) -> str:
    if i >= len(row):
        return ""
    v = row[i]
    return _clean(v.replace("\n", " ")) if isinstance(v, str) else ""


def _looks_like_row_label(text: str) -> bool:
    """A row-label cell: one or two short words, no quantity, no punctuation
    that would make it a sentence.

    Deliberately conservative. A loose version of this matched dish names -
    "Raita", "Phulka" are short, digit-free and punctuation-free - and every
    one of them split a logical row-group in half. Restricting WHERE the label
    may appear (see `_rebuild_grid`) is what actually fixes that; this only
    filters what is left.
    """
    if not text or len(text) > 28:
        return False
    if re.search(r"\d", text):
        return False
    if re.search(r"[.;!?]", text):
        return False
    return len(text.split()) <= 3


def _rebuild_grid(grid: list[list]) -> tuple[list[str], list[list]] | None:
    """Turn a physically-aligned grid back into logical (header, rows).

    Returns None when the grid does not look like a header + body table, so
    the caller can fall back to flat text rather than invent structure.

    The physical grid from text alignment has one column per text cluster, so a
    logical cell whose text wrapped across a column gap arrives as two or more
    adjacent fragments. The header tells us where each logical column STARTS;
    each therefore runs up to the next one's start.
    """
    if not grid or len(grid) < _MIN_TABLE_ROWS:
        return None
    width = max(len(r) for r in grid)
    if width < _MIN_TABLE_COLS:
        return None

    # The header is the row in the first few with the most short, non-empty
    # cells - column names, which are short by nature. Only the first three
    # rows are considered: searching further finds data rows that happen to be
    # all-short and mistakes one for the header.
    best_idx, best_score = -1, 0
    for i, row in enumerate(grid[:3]):
        filled = [_cell(row, j) for j in range(width)]
        score = sum(1 for c in filled if c and len(c) <= 28)
        if sum(1 for c in filled if c) >= 2 and score > best_score:
            best_idx, best_score = i, score
    if best_idx < 0:
        return None

    header_row = grid[best_idx]
    starts: list[tuple[int, str]] = []
    for j in range(width):
        label = _cell(header_row, j)
        if label and len(label) <= 28:
            starts.append((j, label))
    if len(starts) < 2:
        return None

    # Logical column j spans from starts[j] to starts[j+1]-1 (the last runs to
    # the end). This is what re-joins "Rajma Dal (" + "100gm)".
    ranges: list[tuple[int, int, str]] = []
    for n, (col, label) in enumerate(starts):
        end = (starts[n + 1][0] - 1) if n + 1 < len(starts) else width - 1
        ranges.append((col, max(end, col), label))

    data_start = min(c for c, _e, _l in ranges)
    # Row labels live in the gutter to the LEFT of the first data column. Only
    # there - see `_looks_like_row_label` for why this is not enough alone.
    label_cols = range(0, data_start)

    def find_label(row: list) -> str:
        for j in label_cols:
            t = _cell(row, j)
            if _looks_like_row_label(t):
                return t
        return ""

    # Group body rows by the row-label gutter.
    #
    # Rows are assigned to the NEAREST label, not to "the next label below".
    # A row label is usually vertically CENTRED in its group, so it can sit
    # above its own cells (BREAKFAST at row 10, cells from row 2) or below them
    # (DINNER at the last row, cells from rows 36-46). Using "next label below"
    # put every DINNER cell into SNACKS and then produced a DINNER row with no
    # content at all, silently losing the last meal.
    body = grid[best_idx + 1:]
    label_at: dict[int, str] = {}
    for i, row in enumerate(body):
        for j in label_cols:
            t = _cell(row, j)
            if _looks_like_row_label(t):
                label_at[i] = t
                break

    groups: list[list] = []          # [label, rows]
    if label_at:
        positions = sorted(label_at)
        buckets: dict[str, list[list]] = {}
        order: list[str] = []
        for i, row in enumerate(body):
            if i in label_at:
                key = label_at[i]
                if key not in buckets:
                    buckets[key] = []
                    order.append(key)
                # Keep the label's own row. It often also carries the group's
                # first data cells - in the real menu, BREAKFAST's label row
                # held "Sambar / Orange (1)" - and discarding it lost them.
                # Its label cell sits in the gutter, so it is not read as data.
                buckets[key].append(row)
                continue
            # Nearest label by physical distance; ties go to the earlier
            # one so a leading partial group is not stolen by the next.
            nearest = min(positions, key=lambda p: (abs(i - p), p))
            key = label_at[nearest]
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(row)
        for key in order:
            if buckets[key]:
                groups.append([key, buckets[key]])
    else:
        groups = [["", list(body)]]
    if not groups:
        return None

    header = ["Meal"] + [label for _c, _e, label in ranges]
    rows: list[list[str]] = []
    for label, group in groups:
        cells: list[str] = []
        for col, end, _l in ranges:
            parts: list[str] = []
            for row in group:
                for j in range(col, end + 1):
                    t = _cell(row, j)
                    if t:
                        parts.append(t)
            cells.append(_clean(" ".join(parts)))
        if any(cells):
            rows.append([label] + cells)

    if len(rows) < 2:
        return None
    return header, rows


_WORD_X_TOL = 3.5
_WORD_Y_TOL = 2.0
# How far a data line's leftmost word may sit from a column start and still
# count as "that column has content here". Cells are left-aligned under their
# header; a couple of points of slack covers rounding and font side-bearings.
_COLUMN_ALIGN_TOL = 4.0
# A band separator must beat the next widest gap in its window by this factor.
# Measured on the shipped menu the tightest real separator is 16% wider than
# its runner-up, so the factor has to sit below that or the document stops
# parsing at all.
_BAND_GAP_MARGIN = 1.15


def _is_header_token(t: str) -> bool:
    """A column name: one alphabetic word, short. `Monday` yes; `Brown Bread,`
    and `Rice,Curd` no."""
    return bool(t) and t.isalpha() and 2 <= len(t) <= 20


def _is_row_label(t: str) -> bool:
    """A meal name in the left gutter: one ALL-CAPS word."""
    return bool(t) and t.isalpha() and t.isupper() and 2 <= len(t) <= 20


def _layout_table(words: list[dict]) -> tuple[list[str], list[list]] | None:
    """Rebuild a borderless grid from word coordinates.

    This exists because the physical-grid path (`extract_tables`) was wrong for
    a real weekly-menu PDF in two independent ways, and the second was
    invisible: the day-to-dish mapping looked plausible while breakfast cells
    had absorbed the top of the lunch band. Verified against the page's own
    coordinates:

    * **Row labels are not vertically centred.** BREAKFAST sits mid-band, but
      LUNCH, SNACKS and DINNER all sit at the *bottom* of theirs. Assigning
      content to the nearest label therefore put every DINNER cell into SNACKS,
      and pulled the first line of each band into the band above.
    * **`extract_tables` invents columns.** Text alignment turned the page into
      a 48x12 grid whose "columns" are text clusters, not days, so a value
      wrapping a gap became two columns and the day boundaries drifted.

    What is actually reliable on the page:

    * one header line of short single-word names, well separated in x;
    * those names' `x0` values, which are the column starts. Cells are
      left-aligned text, so a word belongs to the last header at or before its
      own `x0` - which is why an overlong Monday entry may reach x=244 while
      Tuesday's column starts at x=259;
    * the whitespace *between* bands, which is wider than the leading within
      them. The three largest gaps between consecutive text lines are exactly
      the three meal boundaries on this page (18.8, 16.2 and 17.5 points,
      against a ~6-10 point leading inside a band).

    So: name the bands from the gutter labels, in y order, and cut them at the
    most prominent gaps. Nothing here infers a structure it cannot see.
    """
    if len(words) < 40:
        return None

    # ── lines ────────────────────────────────────────────────────────────────
    buckets: dict[int, list[dict]] = {}
    for w in words:
        ymid = (w["top"] + w["bottom"]) / 2
        buckets.setdefault(int(ymid / _WORD_Y_TOL), []).append(w)
    line_ys: list[float] = []
    line_words: list[list[dict]] = []
    for key in sorted(buckets):
        ws = sorted(buckets[key], key=lambda w: w["x0"])
        line_ys.append(sum((w["top"] + w["bottom"]) / 2 for w in ws) / len(ws))
        line_words.append(ws)

    # ── header line ──────────────────────────────────────────────────────────
    #
    # A candidate is only the column header if the DATA agrees with it, and the
    # test is deliberately about the LEFTMOST column. Without it a centred report
    # title satisfies every shape test - three single alpha words, well
    # separated - and becomes the header, which drags `label_gutter_max` to the
    # title's left edge and then discards every real column to its left. That
    # silently deleted 40% of the menu's cells in a reproduction where the title
    # had no punctuation, and because `parse` then sets `table_only`, the
    # dropped text was indexed nowhere.
    #
    # The leftmost column is the reliable witness: cells are left-aligned under
    # their header, so some data line below must begin within a couple of points
    # of the header's leftmost x. A centred title has real columns to its left
    # and nothing of its own beneath it. Matching ANY column is not enough - a
    # title can easily share an x with a word further down the page.
    header_idx, columns = -1, []
    for i, ws in enumerate(line_words):
        toks = [w["text"] for w in ws]
        if len(toks) < 3 or not all(_is_header_token(t) for t in toks):
            continue
        xs = [w["x0"] for w in ws]
        if min(b - a for a, b in zip(xs, xs[1:])) < 15:
            continue                      # not a row of column names
        leftmost = min(xs)
        anchored = any(
            any(abs(w["x0"] - leftmost) <= _COLUMN_ALIGN_TOL for w in lower)
            for lower in line_words[i + 1:]
        )
        if not anchored:
            continue
        header_idx, columns = i, [(w["x0"], w["text"]) for w in ws]
        break
    if header_idx < 0 or len(columns) < 3:
        return None
    columns.sort()
    col_x = [c[0] for c in columns]
    col_name = [c[1] for c in columns]
    # Column-start regularity: count the distinct lines below the header
    # that BEGIN at each column start. A real grid anchors many lines per
    # column (one per wrapped cell line); prose anchors almost none, so a
    # multi-column brochure page is declined here instead of shredded.
    anchors = [0] * len(columns)
    for ws in line_words[header_idx + 1:]:
        starts = [w["x0"] for w in ws]
        for n, cx in enumerate(col_x):
            if any(abs(x - cx) <= _COLUMN_ALIGN_TOL for x in starts):
                anchors[n] += 1
    if min(anchors) < _MIN_COLUMN_ANCHORS:
        return None
    # Alignment density: in a real grid, cells are left-aligned under
    # their headers, so a substantial share of EVERY word below the
    # header starts at a column start. Justified prose starts words
    # wherever the line lands them, so its share stays near zero.
    below = [w for ws in line_words[header_idx + 1:] for w in ws]
    aligned = sum(1 for w in below
                  if any(abs(w["x0"] - cx) <= _COLUMN_ALIGN_TOL
                         for cx in col_x))
    if below and aligned / len(below) < _MIN_COLUMN_ALIGN_FRACTION:
        return None
    label_gutter_max = min(col_x) - 15

    # ── row labels in the left gutter ────────────────────────────────────────
    labels: list[tuple[float, str]] = []
    for y, ws in zip(line_ys, line_words):
        for w in ws:
            if w["x0"] < label_gutter_max and _is_row_label(w["text"]):
                labels.append((y, w["text"]))
                break
    labels.sort()
    # De-duplicate a label repeated on adjacent lines.
    deduped: list[tuple[float, str]] = []
    for y, t in labels:
        if deduped and deduped[-1][1] == t:
            continue
        deduped.append((y, t))
    labels = deduped
    if len(labels) < 2:
        return None

    # ── band boundaries ───────────────────────────────────────────────────────
    # One cut per gap between consecutive labels, each chosen independently
    # inside its own window.
    #
    # Taking the globally widest gaps and keeping the top N is not safe: a band
    # with wide internal whitespace contributes gaps as large as the real
    # separators, and the count then runs out mid-table. Searching each window
    # separately means a cut is only ever chosen from the region it actually
    # separates, and each boundary is decided on its own evidence.
    cuts: list[float] = []
    for (ya, _ta), (yb, _tb) in zip(labels, labels[1:]):
        window = [(line_ys[i - 1], line_ys[i])
                  for i in range(1, len(line_ys))
                  if line_ys[i - 1] >= ya and line_ys[i] <= yb]
        if not window:
            return None
        # The widest gap wins only if it is a clear outlier. A blank line inside
        # a band can be as wide as the real separator, and the widest gap in the
        # window is then the wrong one - measured on the shipped menu, the
        # LUNCH->SNACKS separator beats its runner-up by only 16%, so an
        # ordinary reflow would have cut a band in half and served lunch dishes
        # as snacks. Without a margin there is no way to tell a separator from a
        # paragraph break, so decline instead of guessing.
        ranked = sorted(window, key=lambda p: p[1] - p[0], reverse=True)
        best = ranked[0]
        best_gap = best[1] - best[0]
        if len(ranked) > 1:
            runner_up = ranked[1][1] - ranked[1][0]
            if runner_up > 0 and best_gap < runner_up * _BAND_GAP_MARGIN:
                return None
        cuts.append((best[0] + best[1]) / 2)
    if len(cuts) != len(labels) - 1:
        return None

    # ── assign every word to a band and a column ─────────────────────────────
    def band_of(y: float) -> int:
        for i, cut in enumerate(cuts):
            if y < cut:
                return i
        return len(cuts)

    def col_of(x0: float) -> int:
        # Last column whose start is at or before this word.
        c = 0
        for i, start in enumerate(col_x):
            if x0 >= start:
                c = i
            else:
                break
        return c

    cells: dict[tuple[int, int], list[str]] = {}
    # Column assignment: a word belongs to the last header at or before its own
    # x0. Cells are left-aligned under their header, and this is what lets an
    # overlong Monday entry reach x=244 while Tuesday starts at 259.
    #
    # KNOWN LIMITATION, measured on the shipped menu: one text run spans two
    # columns and gets split between them. At y=238.58 the line reads
    # `Plain@416 Rice,Curd@446 with@501 sugar@527 and@559 salt,@581`, so
    # "with sugar and salt," lands in Thursday's cell while "Plain Rice,Curd"
    # starts Wednesday's.
    #
    # It is not fixable from geometry. On the same page the gap between two
    # different days' items sharing a line is ~9pt, while the gap inside that
    # split run is ~5pt - not separable. Every rule tried made a different dish
    # wrong: a horizontal-gap-run rule collapses an entire snack row into
    # Monday's column, and rewinding to the nearest already-filled column pulls
    # Wednesday's own text back into Tuesday. Splitting one ambiguous line is
    # the least-wrong outcome, so it is recorded here rather than hidden.
    for i in range(header_idx + 1, len(line_words)):
        b = band_of(line_ys[i])
        if b >= len(labels):
            continue
        for w in line_words[i]:
            # The gutter holds the band's own name, which is already the row
            # label. Letting it fall through to a cell appended a literal
            # "BREAKFAST" to Monday's breakfast.
            if w["x0"] < label_gutter_max:
                continue
            cells.setdefault((b, col_of(w["x0"])), []).append(w["text"])

    rows: list[list[str]] = []
    for b, (_y, name) in enumerate(labels):
        row = [name]
        for c in range(len(col_name)):
            row.append(_clean(" ".join(cells.get((b, c), []))))
        if any(row[1:]):
            rows.append(row)
    # A two-row "table" is indistinguishable from a page of
    # columnar prose with a couple of ALL-CAPS section words in
    # the gutter (measured on a placement bluebook: every false
    # positive was a 2-row grid built from one "DUAL DEGREE"
    # heading). The grid path requires `_MIN_TABLE_ROWS` already;
    # hold the coordinate path to the same bar. The real menu
    # yields four (one per meal).
    if len(rows) < _MIN_TABLE_ROWS:
        return None
    return ["Meal"] + col_name, rows


def _pdf_table_blocks(data: bytes) -> tuple[list[Block], set[int], list[str]]:
    """Table blocks recovered from a PDF, the pages they came
    from, plus any warnings.

    Empty when pdfplumber is unavailable or finds nothing usable, in which
    case the caller falls back to flat text extraction. Degrading rather
    than failing matters: a PDF with no tables must still be searchable.
    """
    try:
        import pdfplumber
    except ImportError:
        return [], set(), ["pdfplumber unavailable; PDF tables cannot be recovered "
                           "and a table document will be indexed as flat text"]
    warnings: list[str] = []
    blocks: list[Block] = []
    table_pages: set[int] = set()
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for pno, page in enumerate(pdf.pages, start=1):
                # Coordinate reconstruction first: it is strictly more
                # faithful than the physical grid on a borderless table, and
                # keeps words whole.
                try:
                    words = page.extract_words(
                        x_tolerance=_WORD_X_TOL, y_tolerance=_WORD_Y_TOL)
                except Exception as e:
                    warnings.append(f"page {pno} word scan failed: {e}")
                    words = []
                if words:
                    laid = _layout_table(words)
                    if laid:
                        header, rows = laid
                        blocks.append(Block(
                            kind="table", header=header, rows=rows, page=pno,
                            text="\n".join(" | ".join(r) for r in rows)[:20000]))
                        table_pages.add(pno)
                        continue
                # Ruled tables only, and only where the ruling is
                # dense enough to be a grid. The text-strategy
                # extraction that used to run here treated ANY text
                # alignment as a grid, so every prose page of a
                # brochure was shredded into a fake table (observed
                # live: 46 invented grids, cells split mid-word, on
                # a document with no borderless table at all). The
                # coordinate path above already covers borderless
                # grids; a page without ruling has nothing for THIS
                # path to find, so it is skipped and its prose is
                # extracted flat below.
                if len(page.lines) + len(page.rects) < _MIN_RULING_LINES:
                    continue
                try:
                    found = page.extract_tables(
                        _PDF_TABLE_SETTINGS_RULED)
                except Exception as e:
                    warnings.append(f"page {pno} table scan failed: {e}")
                    continue
                if not found:
                    continue
                for grid in found:
                    if not grid or len(grid) < _MIN_TABLE_ROWS:
                        continue
                    width = max(len(r) for r in grid)
                    if width < _MIN_TABLE_COLS:
                        continue
                    # Ruling already defines the logical cells, so the
                    # grid is emitted as-is (the column re-merging in
                    # `_rebuild_grid` exists for the borderless path,
                    # where one cell wraps across a physical gap).
                    rows = [[_cell(r, j) for j in range(width)]
                            for r in grid]
                    rows = [r for r in rows if any(r)]
                    if len(rows) < _MIN_TABLE_ROWS:
                        continue
                    header: list[str] = []
                    body = rows
                    # A leading row of short cells is the header row.
                    if (len(rows) > _MIN_TABLE_ROWS and rows[0]
                            and all(c and len(c) <= 28
                                    for c in rows[0] if c)):
                        header = rows[0]
                        body = rows[1:]
                        if len(body) < 2:
                            continue
                    blocks.append(Block(kind="table", header=header,
                                        rows=body, page=pno,
                                        text="\n".join(
                                            " | ".join(r) for r in body)[:20000]))
                    table_pages.add(pno)
    except Exception as e:
        warnings.append(f"table extraction failed: {e}")
    return blocks, table_pages, warnings


def _is_heading_text(text: str) -> int | None:
    """Heading level from plain-text conventions: markdown hashes, ALL CAPS
    short lines, or a numbered section. Returns None when it is prose."""
    t = text.strip()
    if not t or len(t) > 120:
        return None
    m = re.match(r"^(#{1,6})\s+\S", t)
    if m:
        return len(m.group(1))
    if re.match(r"^(?:chapter|section|part|appendix)\s+[0-9ivxIVX]+\b", t,
                re.I):
        return 1
    if re.match(r"^\d+(?:\.\d+){0,3}[.)]?\s+\S.{0,90}$", t) and \
            not t.rstrip().endswith((".", ",", ";")):
        return 2
    if re.match(r"^(?:abstract|introduction|conclusion|references|appendix|"
                r"contents|summary|overview)$", t, re.I):
        return 1
    # Short, no terminal punctuation, title-ish.
    if len(t.split()) <= 9 and not t.endswith((".", ",", ";", ":", "!", "?")) \
            and t[0].isupper():
        words = t.split()
        if sum(w.isupper() for w in words if w.isalpha()) >= max(2, len(words) - 1):
            return 2
    return None


def _blocks_from_text(text: str, page: int | None = None) -> list[Block]:
    """Generic paragraph/heading extraction shared by txt/pdf fallbacks."""
    out: list[Block] = []
    for para in re.split(r"\n\s*\n", _clean(text)):
        p = para.strip()
        if not p:
            continue
        first = p.splitlines()[0].strip()
        single = len(p.splitlines()) == 1
        lvl = _is_heading_text(first) if single else None
        if lvl:
            out.append(Block(kind="heading", text=first, level=lvl, page=page))
            rest = p.splitlines()[1:]
            if rest and " ".join(rest).strip():
                out.append(Block(kind="para",
                                text=" ".join(r.strip() for r in rest),
                                page=page))
            continue
        # A bulleted run: keep together, mark as a list so it is never split.
        lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
        if lines and all(re.match(r"^(?:[-*+•]|\d+[.)])\s+", ln) for ln in lines):
            out.append(Block(kind="list", text="\n".join(lines), page=page))
        else:
            out.append(Block(kind="para",
                            text=" ".join(ln.strip() for ln in lines),
                            page=page))
    return out


# ── plain text / markdown ────────────────────────────────────────────────────

def parse_text(data: bytes) -> ParseResult:
    return ParseResult(blocks=_blocks_from_text(_dec(data)))


_MD_LIST = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|>\s+)")
# A markdown table delimiter row: |---|---| or |:--:|---| (at least one rule).
_MD_TABLE_RULE = re.compile(r"^\|[\s:\-|]*[-][\s:\-|]*\|?$")


def _is_table_rule(line: str) -> bool:
    s = line.strip()
    return bool(_MD_TABLE_RULE.match(s)) and "-" in s


def _split_md_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_markdown(data: bytes) -> ParseResult:
    """Honour the markdown grammar the file actually uses: ATX headings,
    fenced code (kept whole, never split), lists, tables, paragraphs."""
    res = ParseResult()
    lines = _clean(_dec(data)).splitlines()
    i, n = 0, len(lines)
    para: list[str] = []
    lst: list[str] = []
    fence: list[str] = []
    fence_tag: str | None = None

    def flush_para():
        if para:
            t = " ".join(x.strip() for x in para).strip()
            if t:
                res.blocks.append(Block(kind="para", text=t))
            para.clear()

    def flush_list():
        if lst:
            res.blocks.append(Block(kind="list", text="\n".join(lst)))
            lst.clear()

    def flush_fence():
        if fence:
            res.blocks.append(Block(kind="code", text="\n".join(fence),
                                    meta={"lang": fence_tag or ""}))
            fence.clear()

    while i < n:
        line = lines[i]
        s = line.strip()

        if fence_tag is not None:
            if s.startswith("```"):
                flush_fence()
                fence_tag = None
            else:
                fence.append(line)
            i += 1
            continue
        if s.startswith("```") or s.startswith("~~~"):
            flush_para(); flush_list()
            fence_tag = s.strip("`~")
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush_para(); flush_list()
            res.blocks.append(Block(kind="heading",
                                    text=m.group(2).strip().rstrip("#").strip(),
                                    level=len(m.group(1))))
            i += 1
            continue

        # Setext headings: text underlined by === or ---
        if s and i + 1 < n and re.fullmatch(r"={3,}", lines[i + 1].strip()):
            flush_para(); flush_list()
            res.blocks.append(Block(kind="heading", text=s, level=1))
            i += 2
            continue
        if s and i + 1 < n and re.fullmatch(r"-{3,}", lines[i + 1].strip()) \
                and not _MD_LIST.match(line):
            flush_para(); flush_list()
            res.blocks.append(Block(kind="heading", text=s, level=2))
            i += 2
            continue

        # Table: a header row followed by a |---|---| separator. The
        # separator row is the DELIMITER, not a data row - including it made
        # the header "------ | ------:" and pushed real data down a row.
        if s.startswith("|") and i + 1 < n and \
                _is_table_rule(lines[i + 1]):
            flush_para(); flush_list()
            rows = [_split_md_row(s)]
            i += 2                       # header + rule
            while i < n and lines[i].strip().startswith("|"):
                if _split_md_row(lines[i]):
                    rows.append(_split_md_row(lines[i]))
                i += 1
            if rows:
                res.blocks.append(Block(kind="table", header=rows[0],
                                        rows=rows))
            continue

        if not s:
            flush_para(); flush_list()
            i += 1
            continue

        if _MD_LIST.match(line):
            flush_para()
            lst.append(re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", line))
            i += 1
            continue

        flush_list()
        para.append(line)
        i += 1

    flush_para(); flush_list(); flush_fence()
    return res


# ── HTML ─────────────────────────────────────────────────────────────────────

_DROP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "noscript",
              "svg", "form", "iframe")
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def parse_html(data: bytes) -> ParseResult:
    res = ParseResult()
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ParseResult(blocks=_blocks_from_text(_dec(data)),
                           warnings=["beautifulsoup unavailable; "
                                     "parsed as plain text"])
    soup = BeautifulSoup(_dec(data), "html.parser")
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    if main is None:
        # A fragment with no <body> (or a fully malformed file): fall back to
        # the soup itself rather than emitting nothing.
        main = soup

    for el in main.find_all(
            ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "table"]):
        if el.name in _HEADING_TAGS:
            t = _clean(el.get_text(" ", strip=True))
            if t:
                res.blocks.append(Block(kind="heading", text=t,
                                        level=_HEADING_TAGS[el.name]))
        elif el.name == "table":
            rows = []
            for tr in el.find_all("tr"):
                cells = [_clean(td.get_text(" ", strip=True))
                         for td in tr.find_all(["td", "th"])]
                if any(cells):
                    rows.append(cells)
            if rows:
                res.blocks.append(Block(kind="table", header=rows[0], rows=rows))
        elif el.name == "pre":
            t = el.get_text()
            if t.strip():
                res.blocks.append(Block(kind="code", text=_clean(t)))
        else:
            t = _clean(el.get_text(" ", strip=True))
            if t:
                res.blocks.append(
                    Block(kind="list" if el.name == "li" else "para", text=t))
    if not res.blocks:
        res.blocks = _blocks_from_text(soup.get_text("\n"))
        res.warnings.append("no structured elements found; "
                            "fell back to the document's plain text")
    return res


def _blocks_from_raw_lines(
    raw: str,
    line_sizes: dict[str, float],
    body_size: float | None,
    page: int | None = None,
) -> list[Block]:
    """Build blocks from PDF text, using each line's FONT SIZE as the
    structural signal.

    Going line by line (rather than re-deriving structure from the joined
    text) is what makes this work: a heading is a large-font line, and the
    generic paragraph splitter had been gluing it to the body text that
    followed, so the heading disappeared entirely.
    """
    threshold = (body_size or 0.0) * 1.3
    out: list[Block] = []
    buf: list[str] = []
    buf_big = False
    in_list = False

    def flush():
        nonlocal buf, buf_big, in_list
        text = " ".join(x.strip() for x in buf if x.strip()).strip()
        if text:
            if buf_big and len(buf) == 1 and len(text) <= 120:
                out.append(Block(kind="heading", text=text,
                                 level=_is_heading_text(text) or 2, page=page))
            elif in_list:
                out.append(Block(kind="list", text="\n".join(buf), page=page))
            else:
                out.append(Block(kind="para", text=text, page=page))
        buf, buf_big, in_list = [], False, False

    for raw_line in (raw or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        size = line_sizes.get(line)
        big = bool(size and threshold and size >= threshold)
        if re.match(r"^(?:[-*+•]|\d+[.)])\s+", line):
            buf.append(line)
            in_list = True
            continue
        if in_list:
            flush()
        buf.append(line)
        if big:
            buf_big = True
            # A large-font line stands alone: it is a title, not a lead-in to
            # a paragraph.
            flush()
    flush()
    if not out:
        out = _blocks_from_text(raw, page=page)
    return out


# ── PDF ──────────────────────────────────────────────────────────────────────

def parse_pdf(data: bytes) -> ParseResult:
    res = ParseResult()
    # Tables first. Flat extraction is lossy for them in a way that is not
    # recoverable downstream: a weekly-menu grid arrives as the day names once
    # in a header followed by an unstructured run of dishes, and no chunking or
    # retrieval strategy can put the association back.
    #
    # The flat pass is skipped only for the PAGES that produced a table, not
    # the whole document: a mixed document (a brochure with one summary grid
    # and prose elsewhere) used to lose every prose page the moment one page
    # yielded a table, because `extract_text()` also flattens the non-table
    # prose on the same page and indexing both would double-count the table.
    table_blocks, table_pages, twarnings = _pdf_table_blocks(data)
    res.warnings.extend(twarnings)
    if table_blocks:
        res.blocks.extend(table_blocks)
        res.meta["tables"] = len(table_blocks)
        res.meta["table_pages"] = sorted(table_pages)

    try:
        import pypdf
    except ImportError:
        if not table_blocks:
            res = ParseResult(blocks=_blocks_from_text(_dec(data, "latin-1")),
                              warnings=["pypdf unavailable; "
                                        "parsed as plain text"])
        else:
            res.warnings.append("pypdf unavailable; prose on non-table "
                                "pages was not extracted")
        res.meta["pages"] = res.meta.get("pages") or None
        return res
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as e:
        res.warnings.append(f"could not read the PDF: {e}")
        return res
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            res.warnings.append("this PDF is password-protected")
            return res
    for pno, page in enumerate(reader.pages, start=1):
        if pno in table_pages:
            continue  # already indexed as a table
        try:
            raw = page.extract_text() or ""
        except Exception as e:
            res.warnings.append(f"page {pno} could not be read: {e}")
            continue
        if not raw.strip():
            res.warnings.append(f"page {pno} has no extractable text "
                                f"(a scanned page needs OCR)")
            continue
        # Font-size clustering, using the per-line sizes pypdf reports.
        body_size, line_sizes = _pdf_font_sizes(page, raw)
        for block in _blocks_from_raw_lines(raw, line_sizes, body_size,
                                            page=pno):
            res.blocks.append(block)
    res.meta["pages"] = len(reader.pages)
    return res


def _pdf_font_sizes(page, raw: str) -> tuple[float | None, dict[str, float]]:
    """Median body font size for a page, plus the size of each extracted line.

    Uses the text-showing operator so the size is read at the point each line
    is drawn, rather than guessed from the text afterwards.
    """
    sizes: list[float] = []
    per_line: dict[str, float] = {}
    try:
        def visitor(text, cm, tm, font_dict, font_size):
            if not text or not text.strip():
                return
            for piece in text.strip().splitlines():
                key = piece.strip()
                if not key:
                    continue
                if font_size:
                    per_line.setdefault(key, float(font_size))
                    sizes.append(float(font_size))
        page.extract_text(visitor_text=visitor)
    except Exception:
        return None, {}
    if not sizes:
        return None, per_line
    sizes.sort()
    # The MEDIAN is wrong here: a document is mostly body text, but a
    # one-line page (or a fixture with a single heading and one line of text)
    # puts the heading in the middle of the distribution, so the median became
    # the heading's own size and nothing ever read as "large". Take the
    # MODE instead - the size that most lines share is the body size.
    counts: dict[float, int] = {}
    for s in sizes:
        counts[s] = counts.get(s, 0) + 1
    body = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    return body, per_line


# ── DOCX ─────────────────────────────────────────────────────────────────────

def parse_docx(data: bytes) -> ParseResult:
    res = ParseResult()
    try:
        import docx  # python-docx
    except ImportError:
        return ParseResult(blocks=_blocks_from_text(_dec(data)),
                           warnings=["python-docx unavailable; "
                                     "parsed as plain text"])
    try:
        d = docx.Document(io.BytesIO(data))
    except Exception as e:
        res.warnings.append(f"could not read the DOCX: {e}")
        return res
    style_map = {}
    for s in getattr(d, "styles", []):
        try:
            style_map[s.name] = s.style_id
        except Exception:
            continue
    # Walk the body in DOCUMENT ORDER. Matching elements by identity against
    # d.paragraphs / d.tables separately lost every paragraph whenever the
    # document contained a table, because the two collections interleave but
    # are searched independently.
    def _para_text(el) -> str:
        parts = []
        for node in el.iter():
            tag = node.tag.split("}")[-1]
            if tag == "t" and node.text:
                parts.append(node.text)
            elif tag in ("tab",):
                parts.append(" ")
            elif tag in ("br", "cr"):
                parts.append("\n")
        return _clean("".join(parts))

    def _style_of(el) -> str:
        for node in el.iter():
            if node.tag.split("}")[-1] == "pStyle":
                return node.get(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val"
                ) or ""
        return ""

    def _heading_level(style: str) -> int | None:
        # The value is the styleId, which in real documents is often
        # "Heading1"/"Heading2" or a localised alias; match either shape.
        m = re.match(r"^heading\s*([1-9])$", (style or "").replace(" ", ""),
                     re.I)
        if m:
            return int(m.group(1))
        if re.match(r"^(title|subtitle)$", style or "", re.I):
            return 1
        return None

    for child in d.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            txt = _para_text(child)
            if not txt:
                continue
            lvl = _heading_level(_style_of(child))
            if lvl:
                res.blocks.append(Block(kind="heading", text=txt, level=lvl))
            elif "list" in (_style_of(child) or "").lower():
                res.blocks.append(Block(kind="list", text=txt))
            else:
                res.blocks.append(Block(kind="para", text=txt))
        elif tag == "tbl":
            # findall on the qualified name works, but a real document also
            # nests rows inside sdt/tblGrid wrappers, so descend for anything
            # that is a row, wherever it sits.
            WNS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            rows = []
            for tr in child.iter(f"{WNS}tr"):
                cells = [_para_text(tc) for tc in tr.iter(f"{WNS}tc")]
                # A row's cells must be direct children, not nested inside a
                # sibling cell - collecting every descendant cell duplicated
                # the columns of any table with a nested structure.
                direct = [tc for tc in tr if tc.tag == f"{WNS}tc"]
                if direct:
                    cells = [_para_text(tc) for tc in direct]
                else:
                    cells = [_para_text(c) for c in
                             tr.findall(f"{WNS}tc")] or cells
                if any(cells):
                    rows.append(cells)
            if rows:
                width = max(len(r) for r in rows)
                rows = [(r + [""] * width)[:width] for r in rows]
                res.blocks.append(Block(kind="table", header=rows[0], rows=rows))
    if not res.blocks:
        res.blocks = _blocks_from_text("\n".join(p.text for p in d.paragraphs))
        res.warnings.append("no styled paragraphs found; used plain text")
    return res


# ── CSV / TSV ────────────────────────────────────────────────────────────────

def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except Exception:
        counts = {",": sample.count(","), "\t": sample.count("\t"),
                  ";": sample.count(";")}
        best = max(counts, key=counts.get)
        return best if counts[best] else ","


def parse_csv(data: bytes) -> ParseResult:
    res = ParseResult()
    text = _dec(data)
    if not text.strip():
        return res
    delim = _sniff_delimiter(text[:4096])
    try:
        rows = [list(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]
    except Exception as e:
        res.warnings.append(f"could not parse the table: {e}")
        return res
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return res
    # Pad to the widest row rather than truncating: a short row was being cut
    # at the width of the FIRST row, so trailing cells were silently lost.
    width = max(len(r) for r in rows)
    rows = [(list(r) + [""] * width)[:width] for r in rows]
    if len(rows) == 1:
        res.blocks.append(Block(kind="table", header=rows[0], rows=rows))
        return res
    # First row is the header. If a "header" is just the first data row the
    # table is still usable - the header is repeated on every chunk either
    # way, which is what makes the numbers meaningful.
    res.blocks.append(Block(kind="table", header=rows[0], rows=rows,
                            meta={"rows": len(rows) - 1, "cols": width}))
    res.meta["rows"] = len(rows) - 1
    res.meta["cols"] = width
    return res


# ── XLSX ─────────────────────────────────────────────────────────────────────

def parse_xlsx(data: bytes) -> ParseResult:
    res = ParseResult()
    try:
        import openpyxl
    except ImportError:
        # No openpyxl: xlsx is a zip of XML. Reading the shared strings and
        # the first sheet inline is better than refusing the file outright,
        # but it is lossy, so it says so.
        return _parse_xlsx_fallback(data)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True,
                                    data_only=True)
    except Exception as e:
        res.warnings.append(f"could not read the workbook: {e}")
        return res
    for name in wb.sheetnames:
        try:
            ws = wb[name]
            rows = []
            for r in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in r]
                if any(c.strip() for c in cells):
                    rows.append(cells)
        except Exception as e:
            res.warnings.append(f"sheet {name!r} could not be read: {e}")
            continue
        if not rows:
            res.warnings.append(f"sheet {name!r} is empty")
            continue
        width = max(len(r) for r in rows)
        rows = [(r + [""] * width)[:width] for r in rows]
        res.blocks.append(Block(
            kind="table", header=rows[0], rows=rows,
            meta={"sheet": name, "rows": len(rows) - 1, "cols": width}))
    try:
        wb.close()
    except Exception:
        pass
    res.meta["sheets"] = len(res.blocks)
    return res


def _parse_xlsx_fallback(data: bytes) -> ParseResult:
    import zipfile
    res = ParseResult()
    try:
        import xml.etree.ElementTree as ET
    except ImportError:
        return res
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        res.warnings.append(f"not a readable workbook: {e}")
        return res
    res.warnings.append("openpyxl not installed: read the first sheet only, "
                        "without formatting or formulas")
    shared: list[str] = []
    try:
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            for si in root.findall(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter()
                                      if t.tag.endswith("}t")))
    except Exception:
        pass
    sheets = [n for n in z.namelist()
              if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
    if not sheets:
        res.warnings.append("no worksheets found in the workbook")
        return res
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        root = ET.fromstring(z.read(sorted(sheets)[0]))
    except Exception as e:
        res.warnings.append(f"sheet XML unreadable: {e}")
        return res
    rows = []
    for row in root.iter(f"{ns}row"):
        cells = []
        for c in row.findall(f"{ns}c"):
            ctype = c.get("t") or ""
            if ctype == "s":
                v = c.find(f"{ns}v")
                val = v.text if v is not None else ""
                try:
                    cells.append(shared[int(val)] if val is not None else "")
                except Exception:
                    cells.append(val or "")
                continue
            if ctype == "inlineStr":
                # openpyxl writes inline strings, NOT sharedStrings, so a
                # fallback that only reads <v> saw empty cells and returned
                # nothing at all. The text lives in <is><t>.
                node = c.find(f"{ns}is")
                cells.append("".join(t.text or "" for t in node.iter()
                                     if t.tag.endswith("}t")) if node is not None
                             else "")
                continue
            v = c.find(f"{ns}v")
            cells.append((v.text or "") if v is not None else "")
        if any(str(c).strip() for c in cells):
            rows.append(cells)
    if rows:
        width = max(len(r) for r in rows)
        rows = [(r + [""] * width)[:width] for r in rows]
        res.blocks.append(Block(kind="table", header=rows[0], rows=rows))
    return res
