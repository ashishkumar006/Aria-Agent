"""Channel text must read like a message, not like a log line.

The user's complaint is quoted verbatim in the expected values below: this is
the message Telegram actually delivered today, from a real fired job
(`sch-ccfaad23`, the Vijayawada weather watch). The tests assert the shape of
what replaces it, because every one of those defects is the kind that comes
back the moment someone adds a new delivery path.
"""
from __future__ import annotations

import channelformat as cf

# Exactly what the agent's run produced for the weather job, and exactly what
# the user received. Kept whole on purpose: a fixture that paraphrases the bug
# stops proving it.
ANSWER = (
    "The current temperature in Vijayawada is 26.5°C. This temperature exceeds "
    "the 15°C threshold.\n\n"
    "Sources:\n"
    "- [indianexpress.com: vijayawada weather forecast today]"
    "(https://indianexpress.com/section/weather/vijayawada-weather-forecast-today/)\n"
    "- [en.climate-data.org: november 11](https://en.climate-data.org/asia/india/"
    "andhra-pradesh/vijayawada-715084/t/november-11/)\n\n"
    "The Indian Express (https://indianexpress.com/section/weather/"
    "vijayawada-weather-forecast-today)\n"
    "Vijayawada Weather Today: Vijayawada Temperature, Hourly Forecast "
    "(Thursday, Nov 05, 2026) | The Indian Express\n"
    "Vijayawada Weather Forecast Today, (Thursday): Check Vijayawada weather "
    "today with live temperature, humidity, wind speed, rain forecast and "
    "real-time updates. Get today's forecast and hourly trends here."
)
QUERY = "check_weather_and_notify_if_temp_exceeds_15_c_in_vijayawada"


def test_the_raw_job_identifier_is_never_the_headline():
    out = cf.channel_text(answer=ANSWER, query=QUERY)
    assert "check_weather_and_notify" not in out, (
        f"the snake_case job id is still being used as a title:\n{out[:200]}")


def test_the_sources_dump_is_dropped():
    out = cf.channel_text(answer=ANSWER, query=QUERY)
    assert "Sources:" not in out
    assert out.count("http") <= 1, f"links still pasted:\n{out[:300]}"


def test_the_answer_survives_and_reads_as_a_sentence():
    out = cf.channel_text(answer=ANSWER, query=QUERY)
    assert "26.5" in out, out
    assert "](" not in out, "markdown link syntax reached the channel"


def test_a_snake_case_identifier_becomes_english_when_it_must_be_shown():
    s = cf.humanise_identifier("check_weather_and_notify_if_temp_exceeds_15_c")
    assert "_" not in s
    assert s.startswith("Check weather")


def test_markdown_becomes_plain_text_with_the_link_after_the_words():
    s = cf.strip_markdown("See **[the report](https://x.dev/a)** for details.")
    assert "**" not in s and "[" not in s
    assert "the report: https://x.dev/a" in s, s
    # A label that is only a domain carries no information; the URL is enough.
    s2 = cf.strip_markdown("- [indianexpress.com: weather](https://a.dev/b)")
    assert "http" in s2 and "](" not in s2


def test_a_cut_lands_on_a_sentence_not_mid_clause():
    long_text = "Alpha one. " * 200
    out = cf.cut_at_sentence(long_text, limit=120)
    assert out.endswith("."), repr(out[-12:])
    assert "…" not in out            # a clean boundary needs no ellipsis
    out2 = cf.cut_at_sentence("abcdefghij" * 40, limit=50)
    assert out2.endswith("…")


def test_an_authored_message_is_kept_and_the_result_appended():
    """A promise with no outcome is the bug this whole file exists to fix."""
    out = cf.channel_text(answer="Two new documents arrived: solar.pdf and wind.pdf.",
                          query="whats_new surfaces=documents",
                          message="Checking for new documents.")
    assert "Checking for new documents." in out
    assert "solar.pdf" in out, f"the work's result was dropped:\n{out}"
    assert "whats_new" not in out


def test_nothing_invented_when_there_is_nothing_to_say():
    out = cf.channel_text(answer="", query="daily_standup_note", message="")
    assert out == "Daily standup note.", repr(out)


def test_a_standalone_answer_is_not_prefixed_with_the_job_identifier():
    """"Check weather and notify if temp exceeds 15 c…" above "The current
    temperature in Vijayawada is 26.5°C" says the same thing twice, and the
    second is the sentence the user asked for. An explicit lead is still
    honoured, because a human wrote that."""
    out = cf.channel_text(answer=ANSWER, query=QUERY)
    assert out.startswith("The current temperature"), repr(out[:60])
    assert "Check weather" not in out
    led = cf.channel_text(answer=ANSWER, query=QUERY,
                          lead="Vijayawada weather check")
    assert led.startswith("Vijayawada weather check."), repr(led[:60])


def test_the_weather_message_before_and_after_are_actually_different():
    """The regression guard for the user-visible complaint: the delivered text
    must be shorter than the console page it came from and must not contain the
    internal identifier or the source list."""
    old = f"{QUERY[:200]}\n\n{ANSWER[:600]}"
    new = cf.channel_text(answer=ANSWER, query=QUERY)
    assert len(new) < len(old)
    assert "Vijayawada" in new
    assert "indianexpress" not in new.lower()


def test_a_subject_is_dropped_when_the_answer_already_names_it():
    """`lead` and `subject` are two parameters because they are two decisions.

    A lead was written for the reader and stays. A subject is the job's own
    name, offered only so several reminders are distinguishable, so it is
    redundant the moment the answer says the same thing — and an identifier
    humanised is still an identifier: `check_weather_and_notify...` becomes
    "Check weather and notify if temp exceeds 15 c in vijayawada", which above
    an answer about Vijayawada's temperature is the fact twice.

    This is the shape the whole formatter was written for, and it was broken
    by a substring test that could never fire against a reworded headline.
    """
    subject = cf.humanise_identifier(QUERY)
    named = cf.channel_text(answer=ANSWER, query=QUERY, subject=subject)
    assert named == cf.channel_text(answer=ANSWER, query=QUERY), (
        f"a subject the answer already covers was prepended anyway: {named[:80]!r}")

    # A job whose answer does NOT name its subject keeps it, so a second
    # reminder can be told apart from the first.
    unlinked = cf.channel_text(answer="you have been at it an hour",
                               query="stretch", subject="stretch")
    assert unlinked.lower().startswith("stretch."), repr(unlinked[:60])

    # And an authored lead survives even when it overlaps the answer, because
    # the agent wrote it to be read.
    overlap = cf.channel_text(answer=ANSWER, query=QUERY,
                              lead="Vijayawada weather check")
    assert overlap.startswith("Vijayawada weather check."), repr(overlap[:60])

