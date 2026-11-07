"""Sentence-aligned speech chunks.

Kokoro synthesizes in one blocking call. Handing it a 3,000-word answer meant
10-50 seconds of silence before the first word; the old client paid that whole
cost up front. Small chunks let playback start after the first sentence while
the next is still being synthesized.

Cuts happen ONLY at sentence endings (terminal punctuation, optionally
followed by closing quotes) or at a hard paragraph/newline boundary. Those are
semantic boundaries, not character counts: a chunk never ends mid-sentence.
Text with no boundary is one chunk - there is nowhere legitimate to cut it.
"""
from __future__ import annotations

import re

_TERMINAL = ".!?\u2026"
_CLOSERS = "\"')]\u2019\u201d"
# Titles and abbreviations whose period is not a sentence end.
_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g",
    "i.e", "fig", "figs", "no", "nos", "inc", "ltd", "co", "corp", "dept",
    "approx", "gen", "gov", "sen", "rep", "col", "capt", "lt", "sgt",
    "u.s", "u.k", "u.n", "a.m", "p.m",
}


def _is_abbreviation(text: str, dot: int) -> bool:
    """Is the period at `dot` part of a known abbreviation or an initial?"""
    before = text[:dot]
    m = re.search(r"([A-Za-z][A-Za-z.]*)$", before)
    if not m:
        return False
    word = m.group(1).lower().rstrip(".")
    if word in _ABBREV:
        return True
    # Single-letter initial ("J. Smith").
    if len(word) == 1 and word.isalpha():
        return True
    # Initials with periods ("U.S", "J.R.R").
    if re.fullmatch(r"(?:[a-z]\.)+[a-z]", word):
        return True
    return False


def split_sentences(text: str) -> list[str]:
    """Split at sentence endings, never inside one.

    A period followed immediately by a non-space is a decimal, domain or
    ellipsis fragment, not a boundary. A period after a known abbreviation
    (Dr., e.g.) is not a boundary either. Newlines are hard boundaries because
    a list item or paragraph is a complete spoken unit even without a period.
    """
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return []
    out: list[str] = []
    start = 0
    i = 0
    while i < len(s):
        ch = s[i]
        end = None
        if ch == "\n":
            end = i + 1
        elif ch in _TERMINAL:
            j = i + 1
            while j < len(s) and s[j] in _CLOSERS:
                j += 1
            if ch == "." and _is_abbreviation(s, i):
                pass
            elif j < len(s) and s[j] not in " \n\t":
                # "3.14", "example.com", "a..." — not a finished sentence.
                pass
            else:
                end = j
        if end is not None:
            piece = s[start:end].strip()
            if piece:
                out.append(piece)
            start = end
            i = end
            continue
        i += 1
    rest = s[start:].strip()
    if rest:
        out.append(rest)
    return out


def speech_chunks(text: str, first_chars: int = 180,
                  target_chars: int = 360) -> list[str]:
    """One chunk per sentence.

    Grouping several sentences into a ~360-character chunk made the second
    chunk 18 seconds of audio, which takes longer to synthesize than the
    first chunk takes to play - the exact gap this pipeline exists to remove.
    One sentence keeps each synthesis short and each wait small. The
    parameters are kept for API compatibility; they no longer merge sentences,
    because merging is what created the long silent gap.

    A production sentence is never split, even when it is long: cutting
    inside it would be the arbitrary chunking the user explicitly rejected.
    """
    return split_sentences(text)
