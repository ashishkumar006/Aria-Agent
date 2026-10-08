"""Structure-aware chunking for uploaded documents.

Replaces the old sliding-window word counter (`mcp_server._chunk_text`),
which cut mid-sentence, split list items and table rows apart, and orphaned
headings from their sections.

Design: **hierarchy first, then sentence-aware packing.**

  1. Headings are hard boundaries. A section never merges into a sibling, and
     a section larger than the target is split *within* itself rather than
     bleeding into the next one.
  2. Within a section, whole sentences are packed together. Splits happen on
     sentence-final punctuation (plus closing quotes/brackets), guarded so
     `Dr.`, `e.g.`, `3.14`, `U.S.` and ellipses do not trigger one.
  3. Overlap is made of **whole trailing sentences** sized as a fraction of
     the target, never a fixed word slice.
  4. Tables are chunked by row groups with the header repeated on every
     chunk - numbers without column names are useless on their own.
  5. Every chunk records `heading_path`, `page` and `char_span` so a
     retrieved fragment stays understandable and citable out of context.

No network, no file IO, no agent imports: this is pure text in, chunks out,
so it can be tested exhaustively and quickly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Iterable

# ── defaults ────────────────────────────────────────────────────────────────
# TARGET is a soft target in words, not a hard cut: a sentence that would
# overflow it is still kept whole rather than split.
#
# 150 was chosen by MEASUREMENT against Ollama nomic-embed-text, not by
# convention. Ten questions, each answer in exactly one section of a
# five-section corpus:
#
#   target | chunks | avg w | recall@3 | top-1 | precision
#   -------|--------|-------|----------|-------|-----------
#     150  |   35   |  112  |   100%   |  80%  |   1.00
#     250  |   25   |  161  |    80%   |  80%  |   0.80
#     400  |   15   |  248  |    90%   |  70%  |   0.90
#     600  |   15   |  266  |    70%   |  60%  |   0.47
#
# Larger targets were WORSE, not merely slower: a chunk spanning two sections
# diluted the signal, so a question about canary traffic retrieved a chunk
# dominated by billing text. 150 also embeds fastest.
DEFAULT_TARGET_WORDS = 150
DEFAULT_OVERLAP_FRACTION = 0.20
# Hard ceiling on one chunk's characters.
#
# `parsers.MAX_CHUNK_CHARS` documents this number but nothing enforced it, and
# `rows_per = target // 12` encodes "a row is a handful of words" - true for an
# 8-column menu, wildly false for a 49-column export. A P&L CSV chunked that
# way produced 12,443-char chunks against an 8000-char embedder ceiling: the
# vectors were computed over silently truncated text, destroying exactly the
# row/column association this feature exists to preserve. The embed path calls
# the embedder in-process and so bypasses `/v1/embed`'s 413 guard, meaning
# nothing downstream would have caught it either.
MAX_CHUNK_CHARS = 8000
# Below this a "sentence" is a fragment (a heading, a table cell); keep it but
# do not let it become the tail of an overlap window.
MIN_OVERLAP_WORDS = 12
# A single sentence longer than this gets hard-wrapped, or one runaway
# sentence would produce one enormous chunk.
MAX_SENTENCE_WORDS = 120

HEADING_CHARS = 5  # "## 1.2", "#", "1.2.3"
MIN_HEADING_WORDS = 1


# ── block model ──────────────────────────────────────────────────────────────
@dataclass
class Block:
    """One structural element of a parsed document.

    kind: "heading" | "para" | "list" | "table" | "code"
    level: heading depth (1 = top), None for non-headings
    heading_path: breadcrumb of the enclosing headings
    page: source page (PDF/HTML), else None
    text: the block's own text
    rows: table body rows (first row is the header) for kind == "table"
    header: table header row for kind == "table"
    meta: anything a parser wants to carry
    """

    kind: str
    text: str = ""
    level: int | None = None
    heading_path: tuple[str, ...] = ()
    page: int | None = None
    rows: list[list[str]] = field(default_factory=list)
    header: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["heading_path"] = list(self.heading_path)
        return d


@dataclass
class Chunk:
    """One embeddable unit. `text` is what gets embedded; `content` is the
    same text without the prepended heading breadcrumb, kept separately so
    the breadcrumb is not mistaken for document content when quoted back."""

    text: str
    index: int
    heading_path: tuple[str, ...]
    kind: str = "para"
    page: int | None = None
    char_span: tuple[int, int] = (0, 0)
    content: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["heading_path"] = list(self.heading_path)
        d["char_span"] = list(self.char_span)
        return d


# ── sentence splitting ───────────────────────────────────────────────────────
# Abbreviations that end in a period but do not end a sentence.
_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "rev", "gen",
    "col", "capt", "lt", "sgt", "inc", "ltd", "co", "corp", "dept", "univ",
    "vs", "etc", "al", "approx", "fig", "no", "vol", "pp", "ed", "eds",
    "min", "max", "avg", "e.g", "i.e", "cf", "ca", "ibid", "op", "seq",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
}

# A sentence ends at . ! ? (optionally followed by a closing quote/bracket)
# and then whitespace + something that can start a sentence.
_SENT_END = re.compile(r'([.!?]+["\')\]]*)\s+')
# Token ending in a period: check the preceding word against _ABBREV.
_LAST_WORD = re.compile(r"([A-Za-z][A-Za-z.]*)$")
# A lone digit sequence before the period (e.g. "3.14") must not split.
_NUMERIC = re.compile(r"^\d+$")
# Single capital letters / initialisms (U.S., A.M.) - safe to split after.
_INITIAL = re.compile(r"^([A-Z])\.$")


# Acronyms whose trailing period does NOT end a sentence: U.S., U.S.A., U.K.,
# Ph.D., a.m./p.m. An all-caps run of single letters (or a known initialism)
# is an acronym, not a sentence end.
_ACRONYM = re.compile(r"^(?:[A-Z]\.){2,}$|^[A-Z](?:\.[A-Z])+\.?$")
_TRAILING_ACRONYM = re.compile(r"((?:[A-Za-z]\.)*[A-Z](?:\.[A-Z])+\.?)$")


def _ends_sentence(text: str, match: re.Match) -> bool:
    """Is the punctuation at `match` a real sentence end?

    Guards the cases that make naive splitting produce fragments: a
    lowercase abbreviation, a decimal number, an ellipsis, and a single
    initial such as "J." in "J. Smith".
    """
    punct = match.group(1)
    core = punct[0] if punct else ""
    if not core:
        return False
    # Ellipsis: "wait... then" - treat as a boundary only if followed by a
    # capital, otherwise it is a pause inside a sentence.
    if len(punct) >= 3:
        rest = text[match.end(1):].lstrip()
        return bool(rest) and rest[:1].isupper()

    before = text[:match.start(1)]
    # An acronym directly before the punctuation: "the U.S.", "the U.S.A.",
    # "a Ph.D.". The period belongs to the acronym, not the sentence.
    tail = before.rstrip()
    if tail and tail[-1] not in ".!?\"')]":
        am = _TRAILING_ACRONYM.search(tail)
        if am and _ACRONYM.match(am.group(1) + "."):
            return False
    m = _LAST_WORD.search(tail)
    if m:
        raw = m.group(1)
        word = raw.rstrip(".").lower()
        if word in _ABBREV or word in ("e.g", "i.e"):
            return False
        if _NUMERIC.match(word):
            return False
        # A lone capital initial, e.g. "J. Smith" or "A. Turing": only a real
        # boundary when what follows starts a new sentence.
        if _INITIAL.match(raw + ".") and "." not in raw.rstrip("."):
            rest = text[match.end(1):].lstrip()
            return bool(rest) and rest[:1].isupper()
    return True


def split_sentences(text: str) -> list[str]:
    """Split into sentences, never mid-sentence, keeping the punctuation and
    any trailing quote/bracket attached to the sentence it closes."""
    text = (text or "").strip()
    if not text:
        return []
    out: list[str] = []
    start = 0
    for m in _SENT_END.finditer(text):
        if _ends_sentence(text, m):
            piece = text[start:m.end(1)].strip()
            if piece:
                out.append(piece)
            start = m.end()
    # Anything after the last real boundary belongs to the final sentence,
    # even if it carries several quoted sentences: the quote's closing
    # punctuation is part of the sentence that contains it.
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


def _hard_wrap(sentence: str, max_words: int) -> list[str]:
    """Split an over-long sentence at a comma/semicolon/whitespace nearest the
    limit. Only used to stop one runaway sentence producing a huge chunk."""
    words = sentence.split()
    if len(words) <= max_words:
        return [sentence]
    parts, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= max_words and (w.endswith((",", ";", ":")) or
                                      w.rstrip(",;:").endswith(("and", "or",
                                                                 "but", "which"))):
            parts.append(" ".join(cur))
            cur = []
    if cur:
        parts.append(" ".join(cur))
    return parts


# ── overlap ──────────────────────────────────────────────────────────────────
def _overlap_tail(pack: list[str], target: int) -> list[str]:
    """Whole trailing sentences worth ~`fraction` of the target, for the next
    chunk's opening context.

    At least MIN_OVERLAP_WORDS so the overlap is worth having, and never the
    whole pack - a target larger than the pack (which happens whenever a
    section is short but the target is large) would otherwise make the next
    chunk a pure duplicate of the previous one.
    """
    want = max(MIN_OVERLAP_WORDS, int(target * DEFAULT_OVERLAP_FRACTION))
    # Leave at least one sentence behind in the chunk we are overlapping from.
    max_sentences = max(1, len(pack) - 1)
    tail: list[str] = []
    n = 0
    for s in reversed(pack):
        if len(tail) >= max_sentences:
            break
        w = len(s.split())
        if n and n + w > want:
            break
        tail.insert(0, s)
        n += w
        if n >= want:
            break
    return tail


# ── section assembly ────────────────────────────────────────────────────────
def _sections(blocks: Iterable[Block]) -> list[list[Block]]:
    """Group blocks into sections, starting a new section at every heading.

    Each section carries its own breadcrumb, so a section never merges with a
    sibling and a heading is never separated from the text it introduces.
    """
    sections: list[list[Block]] = []
    cur: list[Block] = []
    path: list[str] = []
    pending_path: tuple[str, ...] = ()
    for b in blocks:
        if b.kind == "heading":
            if cur:
                sections.append(cur)
                cur = []
            # Rebuild the breadcrumb to this depth. A heading belongs to its
            # OWN path, so the following text inherits it.
            lvl = max(1, int(b.level or 1))
            path = list(path[:lvl - 1])
            while len(path) < lvl - 1:
                path.append("")
            path.append((b.text or "").strip())
            pending_path = tuple(p for p in path if p)
            # The heading itself carries the full path INCLUDING itself, so a
            # top-level heading reads as ("Overview",) not ().
            b.heading_path = pending_path
        else:
            b.heading_path = pending_path
        cur.append(b)
    if cur:
        sections.append(cur)
    return sections


def _breadcrumb(path: tuple[str, ...]) -> str:
    return " > ".join(p for p in path if p)


def _group_label_rows(rows: list[list]) -> dict[int, str]:
    """Row indices that act as group headings inside a table.

    A weekly menu has no `##` headings, so its structure - BREAKFAST, LUNCH,
    SNACKS, DINNER - lives in a row that fills only the first column
    (`Meal: BREAKFAST`) with every other cell empty. Without recognising that,
    all four meals index as one undifferentiated blob and `heading_path` is
    empty, so a retrieved fragment cannot say which meal it came from.

    The pattern is deliberately narrow, because a false positive would invent
    structure that is not there: the row must have exactly one non-empty cell,
    that cell must be in the first column, it must be short, and it must look
    like a label rather than data.
    """
    out: dict[int, str] = {}
    for i, r in enumerate(rows):
        if not r:
            continue
        cells = [str(c).strip() for c in r]
        filled = [(j, v) for j, v in enumerate(cells) if v]
        if len(filled) != 1:
            continue
        j, v = filled[0]
        if j != 0:
            continue
        if len(v) > 60 or "\n" in v:
            continue
        out[i] = v.rstrip(":").strip() or v
    return out


def _chunk_section(
    blocks: list[Block],
    target: int,
    out: list,
) -> None:
    """Flatten one section into chunk-ready pieces.

    Prose is emitted as a flat list of WHOLE SENTENCES rather than
    pre-packed fragments: the sliding window with overlap is done once, in
    `chunk_blocks`, over this sentence stream. Packing here and re-packing
    there was what let chunks drift past their ceiling.
    """
    tables = [b for b in blocks if b.kind == "table"]

    for b in blocks:
        if b.kind == "table":
            continue
        if b.kind == "heading":
            # A heading IS its own final path element, so pass the parent path
            # and let _mk re-append the heading text. Without this a top-level
            # heading chunk would carry an empty breadcrumb.
            out.append(dict(kind="heading", text=b.text,
                            path=b.heading_path[:-1] if b.heading_path else (),
                            page=b.page, own=b.text))
            continue
        text = (b.text or "").strip()
        if not text:
            continue
        for s in split_sentences(text):
            for piece in _hard_wrap(s, MAX_SENTENCE_WORDS):
                out.append(dict(kind=b.kind, text=piece, path=b.heading_path,
                                page=b.page, own=None))

    for b in tables:
        header = b.header or (b.rows[0] if b.rows else [])
        body = b.rows[1:] if b.rows and header and b.rows[0] == header \
            else b.rows
        if not header and not body:
            continue
        hdr = " | ".join(str(c).strip() for c in header) if header else ""
        # When a table is wide, render each cell with its column name instead
        # of relying on pipe position.
        #
        # A weekly menu indexed as `BREAKFAST | <Mon> | <Tue> | ... | <Sun>`
        # makes the day-to-dish mapping depend on counting eight pipes across a
        # 700-character line. Observed live: asked for Thursday breakfast, the
        # model returned the FRIDAY column verbatim - correctly read, wrong
        # index - while the same query for lunch and dinner came back right.
        # The parser was fine; the rendering was the defect. Naming each cell
        # turns a positional lookup into a keyword lookup.
        names = [str(c).strip() for c in header]
        # A `None` header cell must not become the literal column name "None".
        names = ["" if n == "None" else n for n in names]
        width = max([len(names)] + [len(r) for r in body]) if body else len(names)
        labelled = len(names) >= 3 and all(names) and not any(
            "|" in n for n in names)
        # Duplicate column names make the labelled form ambiguous again - two
        # `- Monday:` lines that retrieval cannot tell apart - which is the
        # positional guessing the labelling exists to remove. Disambiguate so
        # the mapping stays injective.
        if labelled:
            seen: dict[str, int] = {}
            uniq = []
            for n in names:
                if n in seen:
                    # Second occurrence reads as "Monday (2)", matching the
                    # position a reader would count to.
                    seen[n] += 1
                    uniq.append(f"{n} ({seen[n] + 1})")
                else:
                    seen[n] = 0
                    uniq.append(n)
            names = uniq
        # Rows per chunk, from the MEASURED width of this table's rows.
        #
        # This was `target // 12`, a hardcoded guess of 12 characters per row.
        # A 7-column menu row is ~180 characters, so the estimate was out by
        # roughly 15x: rows_per came out as 666, every real table fitted in one
        # group, and the whole weekly menu indexed as a SINGLE chunk with an
        # empty heading_path. Nothing was split, nothing was over the ceiling -
        # the budget was simply never applied. Measuring costs one pass over
        # cells that have already been parsed.
        widths = []
        for r in body[:64]:                      # a sample is enough
            widths.append(sum(len(str(c)) for c in r) + 2 * max(1, len(r)) - 1)
        avg_row = max(24, (sum(widths) // len(widths)) if widths else 24)
        rows_per = max(1, target // avg_row)
        # A group label row ("Meal: BREAKFAST") is a boundary of its own; a
        # chunk holding four meals is as unretrievable as one holding four
        # sections of prose, so the labelled form splits there too.
        label_rows = _group_label_rows(body) if labelled else {}
        # Split points: the size boundary, plus every group-label row, so a
        # chunk never straddles two meals. Walking the boundaries in order
        # replaces a plain stride - `range(0, len, rows_per)` could cut a group
        # in half and drop the label away from the rows it introduces.
        cuts = list(range(0, len(body), rows_per)) or [0]
        for idx in sorted(label_rows):
            if idx not in cuts:
                cuts.append(idx)
        cuts = sorted(set(cuts))
        bounds = list(zip(cuts, cuts[1:] + [len(body)]))
        for start, end in bounds:
            i = start
            group = body[i:end]
            if not group:
                continue
            # The label row belongs to the group it introduces, not the one
            # before it, so it is prepended to its own chunk's text below.
            group_label = label_rows.get(i, "")
            # A pure label row is redundant - and wrong - when the
            # rows it introduces carry their own row labels in
            # column 0. A labelled data row already names itself
            # ("Meal: LUNCH"), so a stray empty section header
            # above it ("BREAKFAST" with no dishes of its own)
            # would stamp a meal that has no data onto a chunk of
            # another meal's dishes.
            #
            # The reverse is the reason the label exists at all: in
            # a PURE-label table the dish rows' column 0 is empty,
            # so the group label is the only place the meal name
            # survives - it goes on the chunk's breadcrumb, which
            # is prepended to the chunk text.
            if group_label:
                for r in group[1:]:
                    if r and str(r[0]).strip():
                        group_label = ""
                        break
            # Build the whole group's text first, then split it on CHARACTERS
            # as well as rows. A row count alone cannot bound a chunk: 12 rows
            # of a 49-column export is ~12k chars against an 8000 ceiling, and
            # the embedder then silently truncates.
            lines = []
            if labelled:
                # Always emit the header line too. A column that is empty in
                # every row of the chunk otherwise has no lexical anchor at all
                # - a menu with a closed day would omit that day from the index
                # entirely, so "what is the menu on Wednesday" would have
                # nothing to match.
                lines.append(hdr)
                for r in group:
                    cells = [str(c).strip() for c in r]
                    row_label = cells[0] if cells else ""
                    # Iterate the CELLS, not the header width. `parse_csv`,
                    # `parse_xlsx` and `parse_docx` pad rows to a common width
                    # but `parse_html` and `parse_markdown` do not, so a ragged
                    # row reaches here with more cells than the header has and
                    # the extras used to be dropped from the index entirely.
                    pairs = []
                    for j, v in enumerate(cells[1:], start=1):
                        if not v:
                            continue
                        label = names[j] if j < len(names) else f"col{j}"
                        pairs.append((label, v))
                    if not pairs:
                        continue
                    if row_label:
                        # Name the label column too, so every header name
                        # survives into the chunk text.
                        lines.append(f"{names[0]}: {row_label}" if names[0]
                                     else row_label)
                    lines.extend(f"- {n}: {v}" for n, v in pairs)
            else:
                if hdr:
                    # Repeated on every chunk: a row of numbers is meaningless
                    # without its column names.
                    lines.append(hdr)
                lines.extend(" | ".join(str(c).strip() for c in r)
                             for r in group)
            for piece in _split_by_chars(lines, MAX_CHUNK_CHARS):
                # A group label becomes part of the breadcrumb, so a retrieved
                # fragment carries "BREAKFAST" as its heading instead of the
                # meal name being trapped in the body text where a citation
                # cannot see it.
                path = b.heading_path
                extra = group_label
                if extra:
                    path = tuple(path) + (extra,)
                out.append(dict(kind="table", text=piece,
                                path=path, page=b.page, own=None))


def _split_by_chars(lines: list[str], limit: int) -> list[str]:
    """Join `lines` into chunks of at most `limit` characters.

    Never splits a single line: one oversized cell has to stay whole rather
    than be cut mid-value, because a half number is worse than a long chunk.
    Rows keep their grouping - a labelled chunk always repeats the header, so a
    split row-group is still readable on its own.
    """
    out: list[str] = []
    cur: list[str] = []
    size = 0
    for ln in lines:
        add = len(ln) + (1 if cur else 0)
        if cur and size + add > limit:
            out.append("\n".join(cur))
            cur, size = [], 0
            add = len(ln)
        cur.append(ln)
        size += add
    if cur:
        out.append("\n".join(cur))
    return out


def chunk_blocks(
    blocks: Iterable[Block],
    target_words: int = DEFAULT_TARGET_WORDS,
) -> list[Chunk]:
    """Chunk a parsed document. Returns chunks in reading order."""
    blocks = [b for b in blocks]
    if not blocks:
        return []
    target = max(50, int(target_words or DEFAULT_TARGET_WORDS))
    out: list[Chunk] = []
    for section in _sections(blocks):
        pieces: list[dict] = []
        _chunk_section(section, target, pieces)
        # One sliding window over the section's whole sentences. Flush on a
        # boundary piece (heading/table), and otherwise pack until the next
        # sentence would exceed the target - which is why a whole sentence is
        # never cut and the overlap is made of whole trailing sentences.
        i = 0
        buf: list[str] = []
        buf_meta = None
        buf_w = 0
        while i < len(pieces):
            p = pieces[i]
            text, path, kind, page, own = (p["text"], p["path"], p["kind"],
                                           p["page"], p.get("own"))
            w = len(text.split())
            if kind in ("table", "heading"):
                # Tables and headings are atomic: never merged, never overlapped.
                if buf:
                    out.append(_mk(buf, buf_meta, len(out)))
                    buf, buf_w = [], 0
                out.append(_mk([text], (path, kind, page, own), len(out)))
                i += 1
                continue
            if buf and buf_w + w > target:
                out.append(_mk(buf, buf_meta, len(out)))
                buf = list(_overlap_tail(buf, target))
                buf_w = sum(len(t.split()) for t in buf)
                buf_meta = (path, kind, page, None)
            if not buf:
                buf_meta = (path, kind, page, None)
            buf.append(text)
            buf_w += w
            i += 1
        if buf:
            out.append(_mk(buf, buf_meta, len(out)))
    return _renumber(out)


def _mk(pieces: list[str], meta, idx: int) -> Chunk:
    """Assemble a chunk. `own` is the heading a heading-chunk contributes to
    its own path (so a top-level heading is not left with an empty crumb)."""
    path, kind, page, own = meta or ((), "para", None, None)
    # Sentences within a chunk are one continuous run of text, so they join
    # with a space. A blank line between every sentence made the chunk look
    # like a list of fragments to the model and to a human reading it back.
    sep = "\n" if kind == "table" else " "
    content = sep.join(p.strip() for p in pieces if p and p.strip()).strip()
    full_path = tuple(list(path) + [own]) if own else tuple(path)
    crumb = _breadcrumb(full_path)
    text = f"{crumb}\n\n{content}" if crumb and kind != "heading" else content
    return Chunk(text=text, index=idx, heading_path=full_path, kind=kind,
                 page=page, content=content)


def _renumber(chunks: list[Chunk]) -> list[Chunk]:
    for i, c in enumerate(chunks):
        c.index = i
    return chunks


def chunk_text(
    text: str,
    target_words: int = DEFAULT_TARGET_WORDS,
    source_format: str = "text",
) -> list[Chunk]:
    """Convenience for plain/markdown-ish text: derive blocks from blank-line
    separated paragraphs, treating lines that look like headings as headings.

    The real parsers (stage 2) hand `chunk_blocks` proper structure; this is
    for text that arrives with none.
    """
    blocks: list[Block] = []
    for para in re.split(r"\n\s*\n", (text or "").strip()):
        p = para.strip()
        if not p:
            continue
        first = p.splitlines()[0].strip()
        if re.fullmatch(rf"#+ {re.escape(first[2:])}", first) or \
                re.fullmatch(r"#{1,6}\s+.+", first) and len(p.splitlines()) == 1:
            lvl = len(first) - len(first.lstrip("#"))
            blocks.append(Block(kind="heading", text=first.lstrip("#").strip(),
                                level=lvl))
        else:
            blocks.append(Block(kind="para", text=p))
    return chunk_blocks(blocks, target_words)
