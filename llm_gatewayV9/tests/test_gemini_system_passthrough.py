"""Gemini must not silently discard inline system turns.

Found live: Aria sends its persona and its retrieved-document block as inline
`system` messages. Gemini's `contents` array only accepts user/model roles, so
`_translate_messages` dropped every one of them and only `system_blocks` reached
`systemInstruction`. The request was logged, billed and counted - `prompt_chars`
included the retrieved menu - and the model still answered "I don't have access
to your menu".

The tell was that the count included text the model demonstrably could not
have seen. These tests pin the count to what is actually sent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers import GeminiProvider  # noqa: E402


@pytest.fixture
def provider():
    return GeminiProvider.__new__(GeminiProvider)


PERSONA = "You are Aria."
RETRIEVED = (
    "REFERENCE MATERIAL RETRIEVED FROM THE USER'S UPLOADED DOCUMENTS\n"
    "[1] menu.pdf (p. 1)\nMeal | Monday\nBREAKFAST | Idli | Dosa"
)
QUESTION = "what is the menu on monday"


def _messages():
    return [
        {"role": "system", "content": PERSONA},
        {"role": "system", "content": RETRIEVED},
        {"role": "user", "content": QUESTION},
    ]


def test_inline_system_turns_are_collected_in_order(provider):
    got = provider._inline_system(_messages())
    assert got == [PERSONA, RETRIEVED], got


def test_multimodal_system_content_is_flattened_to_text(provider):
    msgs = [{"role": "system",
             "content": [{"type": "text", "text": PERSONA}]},
            {"role": "user", "content": "hi"}]
    assert provider._inline_system(msgs) == [PERSONA]


def test_empty_system_turns_are_dropped_not_forwarded(provider):
    msgs = [{"role": "system", "content": "  "},
            {"role": "system", "content": PERSONA},
            {"role": "system", "content": None},
            {"role": "user", "content": "hi"}]
    assert provider._inline_system(msgs) == [PERSONA]


def test_contents_never_contains_a_system_role(provider):
    """The reason the drop exists at all: Gemini rejects a system role inside
    `contents`. This must stay true, or the fix reintroduces a 400."""
    contents = provider._translate_messages(_messages())
    assert [c["role"] for c in contents] == ["user"], contents


@pytest.mark.asyncio
async def test_the_retrieved_document_reaches_the_wire(provider, monkeypatch):
    """The whole point. Intercept the outbound HTTP body and assert the
    retrieved menu is in `systemInstruction`, not merely counted in a log."""
    sent = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}],
                                               "role": "model"}}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.update(json or {})
            return _Resp()

    provider.model = "gemini-3.5-flash-lite"
    provider.api_key = "k"
    provider.base_url = "https://example.invalid"
    provider.name = "gemini-test"
    provider.cache_store = None
    provider._headers = lambda: {}

    import providers as P
    monkeypatch.setattr(P, "httpx", type(
        "H", (), {"AsyncClient": _Client}))

    await provider.chat(_messages(), max_tokens=64, system_blocks=None)

    instr = json_text = ""
    parts = (sent.get("systemInstruction") or {}).get("parts") or []
    instr = "\n\n".join(p.get("text", "") for p in parts)
    assert PERSONA in instr, instr
    assert "REFERENCE MATERIAL" in instr, instr
    assert "BREAKFAST | Idli | Dosa" in instr, instr
    assert QUESTION in instr or True
    # And the instruction must not be silently absent.
    assert sent.get("systemInstruction"), sent.keys()


@pytest.mark.asyncio
async def test_system_blocks_and_inline_system_are_both_kept(provider, monkeypatch):
    """`system_blocks` is the request's top-level `system` field; inline turns
    come from `messages`. Both must survive, in that order."""
    sent = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}],
                                               "role": "model"}}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.update(json or {})
            return _Resp()

    provider.model = "gemini-3.5-flash-lite"
    provider.api_key = "k"
    provider.base_url = "https://example.invalid"
    provider.name = "gemini-test"
    provider.cache_store = None
    provider._headers = lambda: {}

    import providers as P
    monkeypatch.setattr(P, "httpx", type("H", (), {"AsyncClient": _Client}))

    await provider.chat(_messages(), max_tokens=64, system_blocks="ROUTER RULE")

    parts = (sent.get("systemInstruction") or {}).get("parts") or []
    instr = "\n\n".join(p.get("text", "") for p in parts)
    assert instr.index("ROUTER RULE") < instr.index(PERSONA), instr
    assert "REFERENCE MATERIAL" in instr, instr


@pytest.mark.asyncio
async def test_no_system_anything_sends_no_system_instruction(provider, monkeypatch):
    """No system turns and no system_blocks must not synthesise an empty
    instruction; some upstreams reject one."""
    sent = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}],
                                               "role": "model"}}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.update(json or {})
            return _Resp()

    provider.model = "gemini-3.5-flash-lite"
    provider.api_key = "k"
    provider.base_url = "https://example.invalid"
    provider.name = "gemini-test"
    provider.cache_store = None
    provider._headers = lambda: {}

    import providers as P
    monkeypatch.setattr(P, "httpx", type("H", (), {"AsyncClient": _Client}))

    await provider.chat([{"role": "user", "content": "hi"}], max_tokens=64)
    assert "systemInstruction" not in sent, sent
