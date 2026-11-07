"""Sentence-only speech chunking and the NDJSON TTS stream."""
import base64
import json

import pytest

from voice_chunking import split_sentences, speech_chunks


def test_titles_are_not_sentence_ends():
    s = split_sentences("Dr. Smith went home. He slept well.")
    assert s == ["Dr. Smith went home.", "He slept well."]


def test_initials_and_abbreviations_are_not_split():
    s = split_sentences("The U.S. market grew. J. Smith agreed at 5 p.m. yesterday.")
    assert len(s) == 2, s
    assert "U.S." in s[0] and "J. Smith" in s[1] and "p.m." in s[1]


def test_decimals_domains_and_ellipses():
    assert split_sentences("Pi is 3.14. Visit example.com now.") == [
        "Pi is 3.14.", "Visit example.com now."]
    assert split_sentences("Wait... what? Yes!") == ["Wait...", "what?", "Yes!"]


def test_quotes_stay_with_their_sentence():
    s = split_sentences('He said "go." Then left.')
    assert s == ['He said "go."', "Then left."]


def test_a_paragraph_break_is_a_boundary():
    assert split_sentences("First item\nSecond item") == ["First item", "Second item"]


def test_text_with_no_boundary_is_one_chunk():
    s = "no punctuation at all just words"
    assert split_sentences(s) == [s]
    assert speech_chunks(s) == [s]


def test_chunks_end_only_at_sentence_ends():
    text = ("One short sentence. " * 3 + "A much longer second sentence that "
            "keeps going for a while. " * 2 + "Done.")
    chunks = speech_chunks(text, first_chars=80, target_chars=120)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk.endswith((".", "!", "?", "\u2026", '"', "'")), chunk
    # No sentence is torn between two chunks.
    sentences = split_sentences(text)
    joined = " ".join(chunks)
    assert joined == " ".join(sentences)


def test_first_chunk_is_shorter_than_later_targets():
    text = " ".join(["This is a sentence of moderate length."] * 20)
    chunks = speech_chunks(text, first_chars=100, target_chars=400)
    assert len(chunks[0]) <= 100 + len("This is a sentence of moderate length.")
    assert len(chunks[-1]) >= len(chunks[0]) or len(chunks) < 3


def test_the_stream_emits_one_wav_per_sentence_chunk(monkeypatch):
    from starlette.testclient import TestClient
    import numpy as np
    import main as M
    import voice_api

    class FakeK:
        def create(self, text, voice, speed):
            return np.zeros(1200, dtype="float32"), 24000

    monkeypatch.setattr(voice_api, "_get_kokoro", lambda: FakeK())
    text = ("First sentence that is quite long indeed. "
            "Second sentence that is quite long too. "
            "Third sentence that is also quite long.")
    with TestClient(M.app) as c:
        r = c.post("/v1/tts/stream", json={
            "text": text, "voice": "af_heart", "first_chars": 40,
            "chunk_chars": 40})
        assert r.status_code == 200, r.text
        events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    chunks = [e for e in events if e.get("type") == "chunk"]
    assert len(chunks) == 3, events
    assert [e["text"] for e in chunks] == [
        "First sentence that is quite long indeed.",
        "Second sentence that is quite long too.",
        "Third sentence that is also quite long."]
    for chunk in chunks:
        assert chunk["text"].endswith(".")
    for e in chunks:
        blob = base64.b64decode(e["audio"])
        assert blob[:4] == b"RIFF"
        assert e["sample_rate"] == 24000
    assert events[-1]["type"] == "done"
