"""Memory integration: latency, the kind/drawer split, and preference capture.

Every test here exists because something specific was wrong:

  * The Memory page took ~5s to load. Two compounding causes, both found by
    measurement: (a) the agent dialled "localhost", which resolves to ::1
    first while the gateway binds IPv4 only, so every call paid a refused
    IPv6 connect; (b) every call built a fresh httpx Client (~1s of SSL
    context + CA bundle setup on this machine). A shared pooled client plus an
    IPv4 literal took the endpoint from 5018ms to ~129ms.
  * A user preference was stored in the `fact` drawer, so the drawer list
    could not isolate it and the panel never showed one. `kinds` filtering
    now exists.
  * Nothing could write a preference at all: zero existed across every
    drawer, because no skill had a write path.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import gateway
import memory as mem
import mcp_server as mcp


# ── the latency fixes, pinned ───────────────────────────────────────────────
def test_gateway_url_is_ipv4_literal_not_localhost():
    """`localhost` resolves to ::1 first on this host and the gateway binds
    IPv4 only, so every request paid a refused IPv6 connect before falling
    back — 2591ms via httpx against 1034ms for the literal address."""
    assert "localhost" not in gateway.GATEWAY_URL, (
        f"GATEWAY_URL={gateway.GATEWAY_URL!r} reintroduces the IPv6-first "
        "connect penalty")
    assert "127.0.0.1" in gateway.GATEWAY_URL


def test_gateway_calls_share_one_pooled_client():
    """`httpx.get(...)` builds a Client per call — a fresh SSL context and CA
    bundle read every time (~1s here). One pooled keep-alive client is the
    fix; a per-call client would silently undo it."""
    a = gateway._client()
    b = gateway._client()
    assert a is b, "each call built a new httpx Client"
    # And memory.py must use it rather than module-level httpx.get.
    src = pathlib.Path(mem.__file__).read_text(encoding="utf-8")
    for bad in ("httpx.get(", "httpx.post(", "httpx.delete("):
        assert bad not in src, (
            f"memory.py still calls {bad} (new Client per request)")


@pytest.mark.parametrize("call", [
    lambda: mem._get("/v1/memory", {"limit": 5}),
    lambda: mem.list_recent(limit=5),
])
def test_memory_reads_stay_fast(call):
    """A generous ceiling: the measured path is ~60ms warm. The pre-fix
    number was 5018ms, so anything under a second proves the pooling and the
    IPv4 literal are both still in effect."""
    import time
    t0 = time.perf_counter()
    call()
    elapsed = (time.perf_counter() - t0) * 1000
    assert elapsed < 1000, f"memory read took {elapsed:.0f}ms"


# ── kind vs drawer: the two axes ────────────────────────────────────────────
def test_preferences_are_not_isolatable_by_drawer():
    """The structural reason the kinds filter exists. KIND_TO_DRAWER routes
    `preference` into `fact`, so drawer==cabinet and kind==vocabulary are
    genuinely different axes. This test fails loudly if someone ever 'fixes'
    the mapping by collapsing them, because the kinds filter would then be
    redundant and the panel's two filters would disagree."""
    from pathlib import Path as _P
    models = (_P(gateway.GATEWAY_V9_DIR) / "memory" / "models.py").read_text(
        encoding="utf-8")
    assert '"preference": "fact"' in models, (
        "preference no longer routes into the fact drawer — the Memory panel's "
        "kind/drawer split now needs revisiting")


def test_unknown_kind_is_rejected_by_the_gateway():
    """Fail closed. A typo'd kind must 400, not silently return everything."""
    import httpx
    try:
        r = httpx.get(f"{gateway.GATEWAY_URL}/v1/memory",
                      params={"kinds": "nonsense"}, timeout=20)
    except Exception:
        pytest.skip("gateway not reachable")
    assert r.status_code == 400, r.status_code
    body = r.text.lower()
    assert "kind" in body, body[:200]


def test_kinds_filter_is_advertised_on_the_agent_endpoint():
    """The panel sends ?kinds=; if the handler drops the param the preference
    view silently shows everything, which looks like a working filter but is
    not one."""
    src = pathlib.Path(mem.__file__).read_text(encoding="utf-8")
    assert "kinds" in src
    import agent_server
    srv = pathlib.Path(agent_server.__file__).read_text(encoding="utf-8")
    assert "kinds" in srv, "agent /api/memory does not accept a kinds filter"


# ── preference capture, end to end ─────────────────────────────────────────
def test_remember_preference_writes_kind_preference_without_a_classifier():
    """remember() runs an LLM classifier that can return `fact`, and a
    preference stored as a fact is invisible and does not read back as a
    standing instruction. This path names the kind and makes zero model
    calls."""
    import time
    marker = f"testkit-{int(time.time())}"
    # Scoped to a test session so the write can be cleaned up with the
    # existing scoped wipe; the store is not a scratch pad.
    try:
        item = mem.remember_preference(f"prefers {marker} for all answers",
                                       keywords=[marker],
                                       session_id="testkit-mem")
    except Exception as e:
        pytest.skip(f"gateway memory unavailable: {type(e).__name__}")
    try:
        assert item.kind == "preference"
        assert marker in item.descriptor
        assert getattr(item, "session_id", None) == "testkit-mem"
    finally:
        try:
            mem.clear(session_id="testkit-mem")
        except Exception:
            pass


def test_empty_preference_is_refused():
    with pytest.raises(ValueError):
        mem.remember_preference("   ")


# ── the detector: narrow on purpose ────────────────────────────────────────
# A loose detector would file half the user's requests as standing
# instructions, and a wrong standing instruction misinforms every later run
# — worse than missing some. Both directions are asserted.
@pytest.mark.parametrize("text", [
    "From now on always give me the publication date for every source, and never use emoji.",
    "I prefer metric units from now on.",
    "please always answer in bullet points",
    "Do not ever send emails without asking me first",
    "default to using TypeScript for new projects",
    "I always want the summary before the details",
    "stop using emojis in code comments",
    "going forward, keep answers under 200 words",
])
def test_detector_catches_standing_preferences(text):
    assert mem.detect_preference(text), f"missed a preference: {text!r}"


@pytest.mark.parametrize("text", [
    "what are my preferences?",
    "Do you know how I like my reports formatted?",
    "Write a Python function that parses CSV files and handles quoted fields",
    "What is the capital of France?",
    "Summarise the news in three bullets",
    "The user asked me to always use tabs, and this project uses spaces",
    "Explain how asyncio task groups work",
    "hi",
    "",
])
def test_detector_ignores_everything_else(text):
    assert mem.detect_preference(text) is None, f"false positive: {text!r}"


def test_detector_refuses_very_long_input():
    """A wall of text is not a standing instruction, and truncating one into
    a preference would store a half-sentence as a rule."""
    assert mem.detect_preference("always " * 400) is None


def test_both_chat_paths_call_the_capture_hook():
    """chat_simple and chat_simple_stream are separate handlers; wiring only
    one leaves the Chat page (which streams) uncaptured."""
    import agent_server
    import pathlib as _p
    src = _p.Path(agent_server.__file__).read_text(encoding="utf-8")
    assert src.count("_capture_preference_bg(query, conversation_id)") >= 2, (
        "only one chat path captures preferences")
    assert "_capture_preference_bg" in src
    # And it must be off the request path.
    assert "daemon=True" in src


def test_preference_tools_are_registered_and_schemad():
    """Both must exist on the catalog, or the model cannot call them."""
    import asyncio
    tools = asyncio.run(mcp.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"remember_preference", "recall_preferences"} <= names, (
        "preference tools missing from the MCP catalog")


def test_preference_tool_description_states_when_to_use_it():
    """A tool the model never calls is a tool that does not exist. The
    description has to carry the trigger, not just the signature."""
    import asyncio
    tools = {t.name: t for t in asyncio.run(mcp.mcp.list_tools())}
    desc = (tools["remember_preference"].description or "").lower()
    for hint in ("preference", "standing", "supersede"):
        assert hint in desc, f"description does not mention {hint!r}"


def test_skills_that_observe_preferences_are_granted_the_tool():
    """Least privilege still has to cover the skills that actually see the
    user expressing one: chat (inline system prompt), researcher and
    retriever."""
    from skills import SkillRegistry
    reg = SkillRegistry()
    for name in ("researcher", "retriever"):
        if name not in reg.names():
            continue
        allowed = reg.get(name).tools_allowed
        assert "remember_preference" in allowed, (
            f"{name} observes preferences but cannot record them")


def test_the_prompts_tell_the_agent_when_to_save_a_preference():
    """Instructions in the wrong place are the reason zero preferences
    existed: the gateway, the store and the model all agreed, and nobody
    asked for one."""
    from skills import ROOT
    import agent_server
    researcher = (ROOT / "prompts" / "researcher.md").read_text(encoding="utf-8")
    assert "remember_preference" in researcher
    assert "STANDING PREFERENCES" in researcher
    retriever = (ROOT / "prompts" / "retriever.md").read_text(encoding="utf-8")
    assert "recall_preferences" in retriever
    chat = pathlib.Path(agent_server.__file__).read_text(encoding="utf-8")
    assert "STANDING PREFERENCES" in chat, (
        "the chat system prompt has no preference instruction")


def test_recall_preferences_reads_back_what_was_written():
    import time
    marker = f"recallkit-{int(time.time())}"
    try:
        # remember_preference (the client) rather than the tool: the tool
        # takes no session_id, so this one can be scoped and cleaned up.
        mem.remember_preference(f"prefers {marker} in every reply",
                                keywords=[marker], session_id="testkit-recall")
        rows = mcp.recall_preferences(20)
    except Exception as e:
        pytest.skip(f"gateway memory unavailable: {type(e).__name__}")
    finally:
        try:
            mem.clear(session_id="testkit-recall")
        except Exception:
            pass
    assert any(marker in str(r.get("preference", "")) for r in rows), rows
