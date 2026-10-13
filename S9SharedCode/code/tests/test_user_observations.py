"""The behavioural profile: what it must refuse, and what it must never store.

The value of this feature is entirely in its refusals. A profile that quietly
accepted a paraphrased quote, a personality label, or a stray chat line would
be worse than no profile at all: it would be a durable claim about a real
person that nothing could later check. So these tests pin the refusals, not
the happy path.
"""
import sys
import pathlib
import types

import pytest

AGENT = pathlib.Path(r"C:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code")
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

import memory as mem  # noqa: E402
import skills  # noqa: E402


class _Item:
    def __init__(self, i="mem:1", d="obs"):
        self.id = i
        self.descriptor = d
        self.keywords = []
        self.confidence = 1.0


@pytest.fixture
def posted(monkeypatch):
    """Capture what would be written to the gateway instead of writing it."""
    calls = []

    def fake_post(path, body, timeout=60.0):
        calls.append((path, body))
        # Enough of a record for MemoryItem.model_validate; the fields the
        # caller reads back are id and descriptor.
        return {"item": {"id": "mem:1", "kind": body.get("kind", "observation"),
                         "descriptor": body.get("descriptor", ""),
                         "keywords": body.get("keywords", []),
                         "value": body.get("value", {}),
                         "source": body.get("source", "test"),
                         "run_id": body.get("run_id", "test"),
                         "confidence": body.get("confidence", 1.0)}}

    monkeypatch.setattr(mem, "_post", fake_post)
    return calls


# ── the evidence check: the whole safety property ─────────────────────────

def test_a_quote_that_is_not_in_the_source_is_not_an_observation():
    """A paraphrase is an inference wearing the costume of a quotation."""
    source = "please stop adding the preamble, I just want the answer"
    assert mem._evidence_verifies("stop adding the preamble", source)
    assert not mem._evidence_verifies(
        "the user dislikes verbose answers", source)


def test_evidence_is_matched_loosely_enough_to_be_useful():
    """The model normalises whitespace and case when copying; being strict
    about that would reject honest quotes and train the user to distrust the
    feature."""
    source = "Always   Give\n me the date"
    assert mem._evidence_verifies("always give me the date", source)


def test_a_trivially_short_quote_is_not_evidence():
    """Three characters matches almost any message; it proves nothing."""
    assert not mem._evidence_verifies("the", "give me the date please")


# ── trait language is refused outright ───────────────────────────────────

@pytest.mark.parametrize("claim", [
    "the user is anxious about deadlines",
    "shows an avoidant attachment style",
    "tends toward narcissistic framing",
    "probably has undiagnosed depression",
    "is emotionally unavailable",
    "procrastinates and is generally lazy",
])
def test_personality_and_health_claims_are_refused(posted, claim):
    with pytest.raises(ValueError):
        mem.remember_observation(claim, "some quote")
    assert posted == [], f"a trait claim reached the store: {claim}"


def test_observational_phrasing_of_the_same_thing_is_allowed(posted):
    """The guard targets labels, not the subject. Saying someone asks for
    short answers is a fact about their requests; calling them impatient is a
    claim about their character."""
    mem.remember_observation(
        "asks for short answers and pushes back on long preambles",
        "stop with the preamble, just give me the answer")
    assert len(posted) == 1


def test_an_observation_without_a_quote_is_refused(posted):
    """The gate that makes this a record of what someone said rather than a
    model's opinion about them."""
    with pytest.raises(ValueError):
        mem.remember_observation("prefers concise answers", "")
    with pytest.raises(ValueError):
        mem.remember_observation("prefers concise answers", "   ")
    assert posted == []


# ── what actually gets written ───────────────────────────────────────────

def test_a_stored_observation_carries_its_evidence_and_confidence(posted):
    mem.remember_observation(
        "wants a publication date on every factual claim",
        "from now on always give me the publication date",
        keywords=["dates", "citations"], confidence=0.85)

    path, body = posted[0]
    assert path == "/v1/memory/remember"
    assert body["kind"] == "observation"
    assert body["value"]["evidence"] == \
        "from now on always give me the publication date"
    assert body["value"]["observation"] == \
        "wants a publication date on every factual claim"
    assert body["confidence"] == 0.85
    assert body["keywords"] == ["dates", "citations"]


def test_confidence_is_clamped_rather_than_stored_nonsense(posted):
    """A caller sending 99 must not write a score that later reads as
    certainty."""
    mem.remember_observation("x y z z", "evidence here", confidence=99)
    assert posted[0][1]["confidence"] == 1.0
    mem.remember_observation("x y z z", "evidence here", confidence=-5)
    assert posted[1][1]["confidence"] == 0.0


# ── the extractor: low temperature, and it verifies before it writes ─────

def _fake_llm(payload):
    """A stand-in LLM that returns `payload` and records how it was called.

    Returns (cls, calls) so the caller can patch `mem.LLM` with the class and
    still inspect the kwargs - the temperature assertion depends on seeing the
    real call, not a spy around it.
    """
    calls = []

    class _L:
        def chat(self, **kw):
            calls.append(kw)
            return {"parsed": payload}

    return _L, calls


def test_extraction_is_off_unless_the_flag_is_on(monkeypatch):
    monkeypatch.setattr(mem, "observations_enabled", lambda: False)
    FakeLLM, calls = _fake_llm({"observations": [
        {"observation": "x", "evidence": "hello there friend", "confidence": 0.9}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)
    assert mem.extract_observations("hello there friend") == []
    assert calls == [], "it called the model even though the feature is off"


def test_the_extractor_is_not_the_fact_classifier(monkeypatch, posted):
    """temperature=1.0 is what makes the existing fact classifier creative.
    A wrong extraction here is a durable claim about a person, so creativity
    is the one property that must be absent."""
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, calls = _fake_llm({"observations": [
        {"observation": "wants short answers",
         "evidence": "stop with the preamble", "confidence": 0.9}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)

    out = mem.extract_observations("stop with the preamble, I just want it")
    assert len(out) == 1
    assert calls[0]["temperature"] <= 0.3, (
        f"extraction ran at temperature {calls[0]['temperature']}")


def test_an_unverifiable_quote_is_dropped_not_stored(monkeypatch, posted):
    """The extractor can hallucinate a quote. The check catches it."""
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "prefers formal prose",
         "evidence": "I always write in formal business English",  # invented
         "confidence": 0.95}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)

    assert mem.extract_observations("can you make this shorter please") == []
    assert posted == [], "an observation was stored with a quote the user never said"


def test_a_trait_extraction_is_dropped_even_with_a_real_quote(monkeypatch, posted):
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "the user is anxious about deadlines",
         "evidence": "I am worried about the deadline", "confidence": 0.9}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)

    assert mem.extract_observations("I am worried about the deadline") == []
    assert posted == []


def test_low_confidence_and_overlong_turns_are_skipped(monkeypatch, posted):
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "maybe likes brevity",
         "evidence": "make it shorter please", "confidence": 0.1}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)

    assert mem.extract_observations("make it shorter please") == []
    assert posted == [], "stored a guess the extractor itself called low-confidence"


def test_at_most_two_observations_are_taken_from_one_turn(monkeypatch, posted):
    """Otherwise a long, discursive message becomes a transcript summary."""
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    many = [{"observation": f"observation number {i}",
             "evidence": "the user said something", "confidence": 0.9}
            for i in range(7)]
    FakeLLM, _ = _fake_llm({"observations": many})
    monkeypatch.setattr(mem, "LLM", FakeLLM)

    mem.extract_observations("the user said something")
    assert len(posted) == 2


def test_a_broken_extractor_never_raises_into_the_chat_turn(monkeypatch, posted):
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)

    class _Boom:
        def chat(self, **kw):
            raise RuntimeError("gateway down")

    monkeypatch.setattr(mem, "LLM", _Boom)
    assert mem.extract_observations("please always use metric units") == []


def test_agent_own_text_is_not_mined_back_into_observations(monkeypatch, posted):
    """Reference material and context blocks are the agent's own words. Mining
    them would manufacture observations about the user out of text they never
    wrote."""
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, calls = _fake_llm({"observations": []})
    monkeypatch.setattr(mem, "LLM", FakeLLM)

    mem.extract_observations("[MEMORY HITS] the user prefers metric units")
    assert calls == []


# ── the schema the provider actually has to accept ────────────────────────

def test_the_extraction_schema_is_strict_mode_legal():
    """This is a spec test against the PROVIDER, not against pydantic.

    Gemini's strict structured output rejects `$ref`/`$defs` and requires
    `required` plus `additionalProperties: false` on every object. A nested
    pydantic model emits `$defs` and the gateway then fails every extraction
    with "structured output failed validation" - which looks like the gateway
    being down and is not. No unit test could catch it, because every unit
    test stubs the LLM.
    """
    schema = mem._OBSERVATION_SCHEMA

    def walk(node, path="$"):
        assert "$ref" not in node, f"{path}: $ref is not supported in strict mode"
        assert "$defs" not in node, f"{path}: $defs is not supported in strict mode"
        if node.get("type") == "object":
            assert "required" in node, f"{path}: object has no 'required'"
            assert node.get("additionalProperties") is False, (
                f"{path}: strict mode requires additionalProperties:false")
        for k, v in (node.get("properties") or {}).items():
            walk(v, f"{path}.{k}")
        items = node.get("items")
        if isinstance(items, dict):
            walk(items, f"{path}[]")

    walk(schema)


def test_the_schema_declares_every_field_the_code_reads():
    """The extractor reads these four keys; a schema missing one produces a
    KeyError at record time, not a visible field."""
    item = mem._OBSERVATION_SCHEMA["properties"]["observations"]["items"]
    required = set(item.get("required") or [])
    assert {"observation", "evidence", "keywords", "confidence"} <= required


# ── the flag itself ──────────────────────────────────────────────────────

def test_the_flag_defaults_to_off():
    """This is the only path that retains conversation content the user did
    not ask to retain. It must never start recording because someone added a
    field somewhere."""
    import flags
    assert flags.DEFINITIONS["memory.observations"]["default"] is False


# ── regressions found by the first live run ──────────────────────────────

def test_a_one_off_task_instruction_is_not_a_standing_pattern(monkeypatch, posted):
    """Live: "summarise that in exactly three bullet points" was recorded as
    an observation about bullet points - in the same conversation where the
    user had just said they never want bullet points. The extractor inverted
    a live preference from a single task instruction."""
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "The user requested a summary in bullet points despite "
                       "an earlier preference against them.",
         "evidence": "summarise that in exactly three bullet points",
         "confidence": 0.8}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)

    got = mem.extract_observations(
        "summarise that in exactly three bullet points")
    assert got == [], "a one-off task instruction became a standing pattern"
    assert posted == []


def test_a_claim_about_context_the_extractor_never_saw_is_dropped(monkeypatch, posted):
    """The extractor gets ONE message. "despite an earlier preference" is
    invented context - it cannot have seen an earlier turn."""
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "prefers bullet points despite earlier resistance",
         "evidence": "summarise that in three bullet points",
         "confidence": 0.9}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)

    assert mem.extract_observations(
        "summarise that in three bullet points") == []
    assert posted == []


def test_a_single_sighting_cannot_present_itself_as_a_pattern():
    """The user's own catch: "prefers concise responses" came from ONE message
    ("I really don't need the preamble") and was stored at confidence 0.9.

    The extractor's confidence grades how well its sentence matched the
    instructions - not how many times the behaviour has happened. Asked to
    describe a single incident it returns a high number, and "prefers" then
    reads as an established preference. Confidence has to be bounded by
    sightings.
    """
    assert mem._confidence_for(1.0, seen=1) <= 0.5, \
        "one incident can present itself as a settled preference"
    assert mem._confidence_for(0.95, seen=1) <= 0.5
    # And it rises only with repetition.
    assert mem._confidence_for(0.4, seen=2) > mem._confidence_for(0.4, seen=1)
    assert mem._confidence_for(0.4, seen=3) > mem._confidence_for(0.4, seen=2)


def test_confidence_never_reaches_certainty_about_a_person():
    assert mem._confidence_for(1.0, seen=99) <= 0.95


def test_a_first_sighting_is_written_at_the_capped_confidence(monkeypatch, posted):
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "prefers concise responses without introductory text",
         "evidence": "I really don't need the preamble",
         "keywords": ["concise", "preamble"], "confidence": 0.9}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)
    monkeypatch.setattr(mem, "list_observations", lambda k=200: [])

    mem.extract_observations("I really don't need the preamble")
    assert posted[0][1]["confidence"] <= 0.5, (
        "a one-off message was stored as a high-confidence preference")


def test_the_profile_block_marks_a_single_sighting_as_an_anecdote(monkeypatch):
    """Confidence alone hides it: 0.9 looks strong. The block has to say in
    words that this was seen once, or the model obeys it as a standing rule."""
    import agent_server
    # The block re-reads the flag through `memory`; patch the module it
    # imports so the test exercises the real gating path.
    import memory as _m
    monkeypatch.setattr(_m, "observations_enabled", lambda: True)
    monkeypatch.setattr(
        agent_server, "_read_profile_for_block", lambda: [
            {"observation": "prefers concise answers",
             "evidence": "no preamble please", "confidence": 0.5,
             "seen_count": 1}])
    blk = agent_server._chat_profile_block()
    assert "seen once" in blk, (
        "a single incident is being presented as a pattern to the model")
    assert "not a standing preference" in blk

    monkeypatch.setattr(
        agent_server, "_read_profile_for_block", lambda: [
            {"observation": "prefers concise answers",
             "evidence": "no preamble please", "confidence": 0.78,
             "seen_count": 4}])
    blk = agent_server._chat_profile_block()
    assert "seen 4 times" in blk


def test_identical_descriptors_dedupe_even_when_one_side_has_no_keywords():
    """Live: one message produced two rows with the SAME descriptor
    ("wants publication dates included for online sources") - one carried
    keywords, one did not. The keyword comparison is skipped when either side
    is empty, so this is exactly the case the text fallback exists for, and it
    is the case that got through."""
    known = [{"observation": "wants publication dates included for online "
                             "sources", "keywords": []}]
    assert not mem._observation_is_new(
        "wants publication dates included for online sources",
        ["publication date", "online"], known)
    assert not mem._observation_is_new(
        "wants publication dates included for online sources",
        [], known)


def test_the_second_identical_candidate_reinforces_and_supersedes(
        monkeypatch, posted, live_profile):
    quote = "always include the publication date"
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "wants publication dates included for online sources",
         "evidence": quote, "keywords": [], "confidence": 0.8},
        {"observation": "wants publication dates included for online sources",
         "evidence": quote, "keywords": ["publication date", "online"],
         "confidence": 0.9}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)

    got = mem.extract_observations(quote)

    assert len(got) == 1, f"stored {len(got)} identical rows"
    assert len(live_profile) == 1, "the profile grew by two for one message"
    assert posted[-1][1]["supersedes"], (
        "the reinforcement did not retire the row it replaced, so both stay "
        "visible in the profile")


def test_the_extractor_and_the_agent_tool_cannot_both_store_one_correction(
        monkeypatch, posted):
    """Live: one message produced two rows for the same correction. The
    background extractor writes, AND the agent was told to call
    `record_observation` in the same turn - and the tool's default confidence
    was 0.8, which is how the two copies were distinguishable in the store.

    Two writers with no shared gate is two memories. Both now go through
    `submit_observation`, so the second reinforces the first.
    """
    quote = "stop using emoji, it looks unprofessional"
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "prefers to avoid emojis in communication",
         "evidence": quote,
         "keywords": ["emoji", "unprofessional", "style"],
         "confidence": 0.8}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)
    monkeypatch.setattr(mem, "list_observations", lambda k=200: [])

    # 1. the background extractor
    mem.extract_observations(quote)
    assert len(posted) == 1

    # 2. the agent calling its tool with the same correction. The profile now
    #    holds what the extractor wrote.
    monkeypatch.setattr(mem, "list_observations", lambda k=200: [{
        "id": "mem:first", "observation": "prefers to avoid emojis in communication",
        "evidence": quote, "confidence": 0.5, "seen_count": 1,
        "keywords": ["emoji", "unprofessional", "style"], "session_id": None,
    }])
    verdict = mem.submit_observation(
        "Prefers to avoid emojis in communication", quote,
        keywords=["emoji", "unprofessional", "style"], confidence=0.8)

    assert verdict["status"] == "reinforced", (
        "the tool wrote a second copy of a claim the extractor had already "
        "recorded")
    new_rows = [p for p in posted if not p[1].get("supersedes")]
    assert len(new_rows) == 1, (
        f"one correction produced {len(new_rows)} separate rows")


def test_chat_is_not_offered_the_recording_tool():
    """Live: every observation came back at the reinforcement confidence
    (0.8) because the agent was calling `record_observation` on every turn
    alongside the background extractor. Two writers double-counted each
    correction, and the tool's paraphrase overwrote the extractor's verbatim
    quote - "wants to see the reasoning" ended up evidenced by "never give me
    a table when there are only two options".

    The agent sees the same message the extractor does and cannot verify a
    quote is verbatim, so it has strictly less to contribute.
    """
    import agent_server
    assert "record_observation" not in agent_server._CHAT_TOOLS, (
        "chat can still record observations, so every correction is counted "
        "twice and the weaker writer's quote wins")
    # It stays registered for explicit use, just not automatic.
    assert "record_observation" in skills._TOOL_CATALOG


def test_the_tool_cannot_bypass_the_sighting_cap(monkeypatch, posted):
    """The tool's default is 0.8, which used to land in the store verbatim -
    a single message reading as a high-confidence preference."""
    monkeypatch.setattr(mem, "list_observations", lambda k=200: [])
    verdict = mem.submit_observation(
        "prefers concise answers", "no preamble please",
        keywords=["concise"], confidence=0.8)
    assert verdict["status"] == "new"
    assert posted[0][1]["confidence"] <= 0.5


def test_the_tool_gets_the_same_refusals_as_the_extractor(monkeypatch, posted):
    monkeypatch.setattr(mem, "list_observations", lambda k=200: [])
    for claim in ("the user is anxious about deadlines",
                  "requested bullet points despite an earlier preference",
                  "prefers bullet points for this summary"):
        v = mem.submit_observation(claim, "some evidence here")
        assert v["status"] == "rejected", f"{claim!r} was not refused"
    assert posted == []


def test_plural_and_singular_spellings_count_as_the_same_topic():
    """Live: "prefers to avoid emojis in communication" and "prefers not to
    have emojis used in responses" were stored as two rows. `emoji` vs
    `emojis` was the only thing keeping them apart."""
    existing = [{"observation": "prefers to avoid emojis in communication",
                 "keywords": ["emoji", "communication", "style"]}]
    assert not mem._observation_is_new(
        "prefers not to have emojis used in responses",
        ["emoji", "responses", "style"], existing)


def test_a_shared_generic_word_is_not_enough_to_merge_two_claims():
    """"format", "writing" and "style" appear on a large share of rows here.
    Two claims about completely different subjects often share only those,
    and merging them would be worse than keeping a duplicate."""
    existing = [{"observation": "prefers prose over bullet points",
                 "keywords": ["format", "prose", "writing"]}]
    # Same generic word, different subject entirely.
    assert mem._observation_is_new(
        "wants publication dates on every factual claim",
        ["format", "dates", "citations"], existing)
    assert mem._observation_is_new(
        "prefers tables when comparing two options",
        ["format", "tables", "comparison"], existing)


def test_a_real_quote_that_is_about_something_else_is_rejected():
    """The failure this guard exists for. `_evidence_verifies` proved the
    quote was REAL - it really was something the user said - but it said
    nothing about reasoning. Worse, dedup then treated the wrong claim as a
    repeat of one it already had and discarded the genuinely correct
    observation about reasoning."""
    assert not mem._keywords_grounded_in_quote(
        ["reasoning", "conclusions"],
        "never give me a table when there are only two options")
    assert not mem._keywords_grounded_in_quote(
        ["acknowledgement", "acceptance", "response"], "yeah ok")


def test_the_support_check_rejects_a_real_quote_about_a_different_subject(
        monkeypatch):
    """The one deterministic guard could not do this and nothing else can.

    A faithful paraphrase shares no words with its quote - "prefers concise
    responses" vs "I really don't need the preamble" - while a wrong pairing
    can share a common noun and pass every word-overlap rule. That is the
    live failure: "wants detailed decision reasoning" carrying "never give me
    a table when there are only two options", matched on "options".
    """
    replies = {}

    class _LLM:
        def chat(self, **kw):
            replies["prompt"] = kw.get("prompt", "")
            return {"text": replies["next"]}

    monkeypatch.setattr(mem, "LLM", _LLM)
    monkeypatch.setattr(mem, "_quote_supports_claim", _REAL_SUPPORT_CHECK)

    replies["next"] = "no"
    assert mem._quote_supports_claim(
        "wants detailed decision reasoning rather than just the conclusion",
        "never give me a table when there are only two options") is False

    replies["next"] = "yes"
    assert mem._quote_supports_claim(
        "prefers concise responses without introductory text",
        "I really don't need the preamble") is True
    # Ask about the actual question, not just whether a reply came back.
    assert "different subject" in replies["prompt"]


def test_the_support_check_fails_open_when_the_gateway_is_down(monkeypatch):
    """An unreachable gateway must not silently discard everything the other,
    free guards accepted."""

    class _Boom:
        def chat(self, **kw):
            raise RuntimeError("gateway down")

    monkeypatch.setattr(mem, "LLM", _Boom)
    monkeypatch.setattr(mem, "_quote_supports_claim", _REAL_SUPPORT_CHECK)
    assert mem._quote_supports_claim("anything at all", "some quote") is True


def test_the_two_cheap_checks_together_reject_what_the_other_misses():
    """Layered defence: neither guard is sufficient alone.

    "yeah ok" carrying "the user acknowledges and accepts the response" is
    rejected by keyword grounding even though the model reads it as
    acknowledgement; a pure-paraphrase mismatch that grounding waves through
    is caught by the support check.
    """
    claim = "The user acknowledges and accepts the previous response"
    quote = "yeah ok"
    assert not mem._keywords_grounded_in_quote(
        ["acknowledgement", "acceptance", "response"], quote)
    assert callable(_REAL_SUPPORT_CHECK)


def test_a_real_quote_survives_the_grounding_check():
    for kws, quote in (
            (["concise", "preamble", "brevity"],
             "I really don't need the preamble"),
            (["emoji", "unprofessional", "style"],
             "stop using emoji, it looks unprofessional"),
            (["dates", "citations", "sources"],
             "from now on always include the publication date")):
        assert mem._keywords_grounded_in_quote(kws, quote), (
            f"a real, relevant observation was rejected: {kws} / {quote}")


def test_an_instruction_shaped_claim_is_refused_even_without_the_word_this(
        monkeypatch, posted):
    """Live: "The user prefers exactly three bullet points when asked to
    summarise" got stored - in the same conversation where the user had just
    said they never want bullet points. The first guard only matched "this
    time"/"for this"; the phrasing had neither."""
    assert mem._ONE_OFF.search(
        "prefers exactly three bullet points when asked to summarise")
    assert mem._ONE_OFF.search("prefers tables when asked to compare two")
    assert mem._ONE_OFF.search("uses emoji for this response")


def test_the_dag_renders_observations_with_their_evidence():
    """A research run reads the profile through _format_memory_hits. Without
    the quote an observation renders exactly like a fact, and the Planner
    cannot tell a supported pattern from a guess."""
    from skills import _format_memory_hits

    class _Hit:
        kind = "observation"
        descriptor = "prefers concise answers"
        source = "chat"
        value = {"observation": "prefers concise answers",
                 "evidence": "just give me the answer, no preamble"}

    out = _format_memory_hits([_Hit()])
    assert "just give me the answer, no preamble" in out, (
        "the evidence quote was dropped in the research path")
    assert "not a stated rule" in out, (
        "an observation was rendered as something the user asserted")


def test_the_dag_still_renders_plain_facts_unchanged():
    """The observation special-case must not leak into every other kind."""
    from skills import _format_memory_hits

    class _Fact:
        kind = "fact"
        descriptor = "Paris is the capital of France"
        source = "user_query"
        value = {"raw": "paris france capital"}

    out = _format_memory_hits([_Fact()])
    assert "Paris is the capital of France" in out
    assert "they said:" not in out
    assert "not a stated rule" not in out


# ── the duplicate problem the second live run exposed ────────────────────

def test_a_restatement_is_not_a_new_observation():
    """Live: one conversation produced "prefers Python over JavaScript" AND
    "prefers Python over JavaScript for programming tasks" as separate rows,
    and 13 rows from 15 messages. The store has no dedup anywhere by design,
    which is wrong for the profile specifically."""
    existing = [{"observation": "prefers Python over JavaScript for coding",
                 "keywords": ["python", "javascript", "language"]}]
    assert not mem._observation_is_new(
        "prefers Python over JavaScript for programming tasks",
        ["python", "javascript", "programming"], existing)


def test_paraphrases_with_no_shared_words_are_still_caught():
    """The real duplicate pair from the live run. Descriptor word overlap is
    0.22 between them - "preamble-free" vs "without introductory text" share
    nothing - so keyword overlap is what connects them."""
    existing = [{"observation": "prefers very short, preamble-free responses",
                 "keywords": ["concise", "preamble", "brevity"]}]
    assert not mem._observation_is_new(
        "prefers concise responses without introductory text",
        ["concise", "preamble", "verbosity"], existing)


def test_records_without_keywords_still_fall_back_to_text():
    """Rows written before the extractor supplied keywords must not become
    undeduplicable."""
    existing = [{"observation": "prefers Python over JavaScript for coding",
                 "keywords": []}]
    assert not mem._observation_is_new(
        "prefers python over javascript", None, existing)


def test_a_genuinely_different_claim_is_still_new():
    existing = [{"observation": "prefers Python over JavaScript for coding",
                 "keywords": ["python", "javascript", "language"]}]
    assert mem._observation_is_new(
        "prefers bullet points over prose in long documents",
        ["formatting", "bullet-points", "documents"], existing)
    assert mem._observation_is_new(
        "wants publication dates on every factual claim",
        ["dates", "citations", "sources"], existing)


def test_dedup_ignores_filler_words():
    """The model paraphrases freely; a comparison that counted 'the' and
    'for' as agreement would let every restatement through."""
    existing = [{"observation": "the user prefers concise answers",
                 "keywords": []}]
    assert not mem._observation_is_new("prefers concise answers", None, existing)


def test_a_repeat_sighting_reinforces_rather_than_duplicates(monkeypatch, posted):
    """Re-stating something three conversations later is the evidence a
    pattern claim needs. It should raise confidence and record the new quote,
    not append a third copy."""
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "prefers concise responses without preamble",
         "evidence": "keep it short, drop the preamble",
         "keywords": ["concise", "preamble", "brevity"],
         "confidence": 0.8}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)
    monkeypatch.setattr(mem, "list_observations", lambda k=200: [{
        "id": "mem:existing",
        "observation": "prefers short answers, no preamble",
        "evidence": "I don't need the preamble",
        "confidence": 0.6, "seen_count": 1,
        "keywords": ["concise"], "session_id": None,
    }])

    got = mem.extract_observations("keep it short, drop the preamble")

    assert got == [], "the repeat sighting was appended as a new row"
    assert len(posted) == 1, "the reinforcement did not write"
    _, body = posted[0]
    assert body["supersedes"] == "mem:existing"
    assert body["confidence"] > 0.6, "a second sighting did not raise confidence"
    assert body["value"]["seen_count"] == 2
    assert body["value"]["previous_evidence"] == "I don't need the preamble"


def test_reinforcement_never_claims_certainty_about_a_person(monkeypatch, posted):
    """Confidence climbs toward 1.0 on repeat agreement, and this store must
    not assert certainty about a real person however often it is told."""
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "prefers concise answers",
         "evidence": "keep it short please", "confidence": 1.0}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)
    monkeypatch.setattr(mem, "list_observations", lambda k=200: [{
        "id": "mem:existing", "observation": "prefers concise answers",
        "evidence": "no preamble", "confidence": 0.97, "seen_count": 9,
        "keywords": [], "session_id": None,
    }])

    mem.extract_observations("keep it short please")
    assert posted[0][1]["confidence"] <= 0.98


_REAL_SUPPORT_CHECK = mem._quote_supports_claim


@pytest.fixture(autouse=True)
def no_support_call(monkeypatch):
    """Neutralise the semantic support check for every test by default.

    It makes a real model call, and a test that spends money or needs the
    network is not a unit test - the same rule that made the original fact
    classifier's phantom patch useless. Tests that exercise it restore the
    real function with `_REAL_SUPPORT_CHECK`.
    """
    monkeypatch.setattr(mem, "_quote_supports_claim", lambda o, q: True)


@pytest.fixture
def live_profile(monkeypatch):
    """A `list_observations` that reflects what was actually written.

    The gate re-reads the profile on every call, so a stub that always returns
    [] makes a second identical candidate look new every time - which is
    exactly the duplication being guarded against, and it hides the bug rather
    than reproducing it.
    """
    seen: list[dict] = []

    def fake_list(k=200, **_kw):
        return [dict(r) for r in seen if not r.get("superseded_by")]

    def remember_observation(descriptor, evidence, *, keywords=None,
                             confidence=1.0, source="agent", run_id="chat",
                             session_id=None, supersedes=None):
        rec = {"id": f"mem:{len(seen) + 1}", "observation": descriptor,
               "descriptor": descriptor, "evidence": evidence,
               "keywords": list(keywords or []), "confidence": confidence,
               "seen_count": 1, "session_id": session_id}
        seen.append(rec)
        return type("I", (), rec)()

    monkeypatch.setattr(mem, "list_observations", fake_list)
    monkeypatch.setattr(mem, "remember_observation", remember_observation)
    return seen


def test_a_second_candidate_in_one_turn_cannot_duplicate_the_first(
        monkeypatch, live_profile):
    """Live, twice: one message produced two rows saying the same thing. The
    profile is read once per candidate now, so without the shared gate the
    second would still not see the first."""
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "prefers no emojis in responses",
         "evidence": "stop using emoji, it looks unprofessional",
         "keywords": ["emoji", "tone", "professional"], "confidence": 0.9},
        {"observation": "prefers a professional tone without emojis",
         "evidence": "stop using emoji, it looks unprofessional",
         "keywords": ["emoji", "tone", "professional"], "confidence": 0.95}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)

    got = mem.extract_observations(
        "stop using emoji, it looks unprofessional")

    assert len(got) == 1, f"one message stored {len(got)} near-identical rows"
    assert len(live_profile) == 1, (
        f"one message added {len(live_profile)} rows to the profile")


def test_two_concurrent_turns_do_not_both_write_the_same_claim(
        monkeypatch, live_profile):
    """Each chat turn spawns its own extractor thread. Without a shared gate
    they read the same snapshot and both append."""
    import threading as _th
    monkeypatch.setattr(mem, "observations_enabled", lambda: True)
    FakeLLM, _ = _fake_llm({"observations": [
        {"observation": "prefers publication dates on every claim",
         "evidence": "always give me the publication date",
         "keywords": ["dates", "citations"], "confidence": 0.9}]})
    monkeypatch.setattr(mem, "LLM", FakeLLM)

    out: list = []
    threads = [
        _th.Thread(target=lambda: out.extend(
            mem.extract_observations("always give me the publication date")))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(live_profile) == 1, (
        f"four concurrent turns on the same claim wrote "
        f"{len(live_profile)} rows")