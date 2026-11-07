"""Turn console-shaped agent output into something a person can read on a phone.

Why this exists
---------------
The answer a run produces is written for the console: markdown headings,
`[source](https://…)` links, a `Sources:` list, bold spans. That text is also
what gets delivered to Telegram, where none of it renders, and the delivery
composition made it worse. The message a user actually received today was:

    check_weather_and_notify_if_temp_exceeds_15_c_in_vijayawada

    The current temperature in Vijayawada is 26.5°C. This temperature exceeds
    the 15°C threshold.

    Sources:
    - [indianexpress.com: vijayawada weather forecast today](https://…)
    …

So the first line is the job's internal identifier used as a headline, and the
rest is a console page pasted into a chat. Neither is what anyone asked to read.

The rule this module enforces: **a channel message is written for a reader, not
a log.** The raw query is never a title; markdown becomes plain text with the
link after the words it describes; the source list is dropped rather than
dumped; and a cut lands on a sentence boundary, not at character 600 with an
ellipsis in the middle of a clause.
"""
from __future__ import annotations

import re

# A channel message that has to be truncated should fit on one phone screen.
MAX_MESSAGE_CHARS = 900

_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)")
_MD_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_MD_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", re.S)
_MD_CODE = re.compile(r"`([^`]*)`")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)
_NUM_BULLET = re.compile(r"^\s*\d+[.)]\s+", re.M)
_HRULE = re.compile(r"^\s*[-=*_]{3,}\s*$", re.M)
_SOURCES_BLOCK = re.compile(
    r"\n*\s*(?:\*\*)?Sources?(?:\*\*)?\s*:.*\Z", re.S | re.I)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_TWO_NEWLINES = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+\n")


def humanise_identifier(text: str) -> str:
    """`check_weather_and_notify_if_temp_exceeds_15_c` → a sentence.

    Scheduled jobs carry the query as written, and models write those in
    snake_case. That string is a good identifier and a bad headline, so when it
    has to be shown it is at least shown as English.
    """
    s = str(text or "").strip()
    if not s:
        return ""
    if "_" in s or "-" in s and " " not in s and len(s.split()) == 1:
        s = s.replace("_", " ").replace("-", " ")
        s = s[0].upper() + s[1:] if s else s
    return s


def strip_markdown(text: str) -> str:
    """Console markdown to readable plain text, links kept where they help."""
    s = str(text or "")
    if not s.strip():
        return ""
    # Links first, while the surrounding text is still intact: the label is the
    # description, so keep it and put the address after it. Bare domains in the
    # label are noise ("[indianexpress.com: …](url)") and go with the bracket.
    def _link(m: re.Match) -> str:
        label, url = m.group(1).strip(), m.group(2).strip()
        if not label or label == url:
            return url
        # Drop a label that is only a domain, e.g. "indianexpress.com".
        if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", label, re.I):
            return url
        return f"{label}: {url}"

    s = _MD_LINK.sub(_link, s)
    s = _MD_BOLD.sub(r"\2", s)
    s = _MD_ITALIC.sub(r"\1", s)
    s = _MD_CODE.sub(r"\1", s)
    s = _HEADING.sub("", s)
    # A markdown table in a chat is unreadable; keep the cells as a line.
    s = _TABLE_ROW.sub(lambda m: " · ".join(
        c.strip() for c in m.group(0).strip().strip("|").split("|") if c.strip()), s)
    s = _HRULE.sub("", s)
    s = _BULLET.sub("• ", s)
    s = _NUM_BULLET.sub(lambda m: "", s)
    s = _TRAILING_WS.sub("\n", s)
    s = _TWO_NEWLINES.sub("\n\n", s)
    return s.strip()


def drop_sources(text: str) -> str:
    """Remove a trailing Sources block.

    A notification that says "the temperature is 26.5°C" does not need eleven
    lines of page titles and URLs under it. The full sourcing stays in the app,
    where the run's own transcript holds it and the reader can actually click.
    """
    return _SOURCES_BLOCK.sub("", str(text or "")).rstrip()


def cut_at_sentence(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Truncate on a sentence boundary, never mid-clause with an ellipsis."""
    s = str(text or "")
    if len(s) <= limit:
        return s
    head = s[:limit]
    # Prefer the last full stop that leaves a decent amount of text standing.
    for punct in (". ", "! ", "? ", "\n", ".", "!", "?"):
        i = head.rfind(punct)
        if i >= limit * 0.5:
            return head[:i + 1].rstrip()
    return head.rstrip() + "…"


_STOPWORDS = frozenset("""a an the to of and or if in on at for with is are be
this that it its you your we our they their as by from into about when then
than so such not no yes do does did done will would can could shall should""".split())


def _words(text: str) -> set:
    """Meaningful lowercase words, for overlap tests only."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())
            if w not in _STOPWORDS and len(w) > 1}


def _shares_a_word(lead: str, body: str) -> bool:
    """True when the body already speaks about what the lead would name.

    One shared content word is enough, because the case being decided is
    "does this answer already say what this job was about" — a reminder to
    stretch and an answer about an hour of focus share nothing, and an answer
    about Vijayawada's temperature and a job about Vijayawada's weather do.
    """
    return bool(_words(lead) & _words(body))


def _sentence(text: str) -> str:
    """One clean sentence: no trailing punctuation doubled, first letter up."""
    t = text.strip().rstrip(".!").strip()
    if not t:
        return ""
    return t[0].upper() + t[1:] + "."


def channel_text(*, answer: str = "", query: str = "", message: str = "",
                 lead: str = "", subject: str = "",
                 limit: int = MAX_MESSAGE_CHARS) -> str:
    """Compose one readable channel message from what the run produced.

    `message` is the agent's own words and wins — but the work's result is
    appended when there is one, because a promise alone is not an outcome.

    Two different front lines, deliberately two parameters:

    - `lead` is a sentence a human or the agent wrote FOR the reader. It is
      honoured even when the answer repeats it.
    - `subject` is the job's own name, humanised. It is there so a second
      reminder is distinguishable from the first, and it is dropped the moment
      the answer already names the same thing — because "Check weather and
      notify if temp exceeds 15 c in vijayawada." above "The current
      temperature in Vijayawada is 26.5°C." is the same fact written twice, and
      the second one is what the user asked for.
    """
    authored = strip_markdown(message)
    body = strip_markdown(drop_sources(answer))
    if authored and body:
        out = f"{authored}\n\n{body}"
    elif authored or body:
        out = authored or body
    else:
        out = ""
    if not out.strip():
        # Nothing said. The identifier is the last resort, and even then as
        # English, in a sentence, with its full stop — a bare "daily standup
        # note" on a phone reads as a fragment that got cut off.
        fallback = lead or subject or humanise_identifier(query)
        fallback = fallback.strip().rstrip(".!")
        return cut_at_sentence(f"{fallback}." if fallback else "", limit)
    # An authored lead stands, because whoever wrote it wrote it for a reader.
    if lead.strip():
        out = f"{_sentence(strip_markdown(lead))}\n\n{out}"
    # A subject is only worth its line if the answer does not already say it.
    # "Stands alone" is decided by shared WORDS, not by whether the subject is
    # a substring of the body: a substring test could only be true for a
    # headline the answer had repeated verbatim, which never happens, so it
    # pasted the weather job's identifier back on top of an answer that
    # already named Vijayawada and 15.
    elif subject.strip() and not _shares_a_word(subject, out):
        out = f"{_sentence(strip_markdown(subject))}\n\n{out}"
    return cut_at_sentence(out.strip(), limit)
