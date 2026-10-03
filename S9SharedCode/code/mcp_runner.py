"""Tool-use loop wrapper around the gateway + MCP server.

When a skill declares `tools_allowed: [...]` in agent_config.yaml,
its dispatch goes through `run_with_tools` (below) rather than a single
chat call. The wrapper drives the conversation until the model stops
asking for tool_calls and emits text:

    1. chat(messages, tools=schemas)
    2. if reply.tool_calls is non-empty:
         for each tc: dispatch via MCP, append a `role="tool"` message
         append assistant message with tool_calls
         go to 1
       else:
         return reply.text

The MCP server is the same `mcp_server.py` carried over from S7. We open
one stdio session per skill invocation (the spawn cost is ~100ms and the
session lives only for the lifetime of one node — keeping it short means
no shared mutable state between skills).

This file is small on purpose. If the cost of a per-skill subprocess
becomes the bottleneck, the right fix is a shared session at the
Executor level, not a more clever client here.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from gateway import LLM

MCP_SERVER = Path(__file__).parent / "mcp_server.py"
MAX_TOOL_HOPS = 6  # hard cap so a model that loves tool-use can't cost a fortune
# Verifier-stall guard: the same tool with the same arguments more than
# this many times in one run means the model is stuck rewording a failed
# verification, not making progress. Global hop caps don't catch it
# because each call looks like forward motion.
MAX_SAME_CALL_REPEATS = 2
# Per-tool wall clock: a hung tool must fail the node, never the run.
TOOL_CALL_TIMEOUT_S = 120.0

# ── MCP subprocess circuit breaker ──────────────────────────────────────────
# Every skill invocation spawns a fresh MCP stdio subprocess. When that child
# is broken — dead on start, wedged, or emitting non-JSON-RPC on stdout, which
# is exactly the `Invalid JSON ... [COMPLETE]` failure seen in the log — each
# tool call fails individually and the node limps on returning "tool error"
# strings to the model. The model then usually retries variations, burning the
# hop cap and producing a confident answer built on nothing.
#
# A breaker turns that into one honest failure. It is per-process (the agent),
# not per-child, because the *condition* is systemic: if N consecutive session
# opens or handshakes failed, opening another one is not going to help.
BREAKER_THRESHOLD = 3
_breaker_lock = threading.Lock()
_breaker = {"consecutive": 0, "open": False, "opened_at": 0.0,
            "trips": 0, "last_error": ""}
BREAKER_COOLDOWN_S = 30.0


class McpUnavailable(RuntimeError):
    """Raised instead of spawning a child the breaker has given up on."""


def breaker_trip(err: str) -> None:
    with _breaker_lock:
        _breaker["consecutive"] += 1
        _breaker["last_error"] = err[:200]
        if _breaker["consecutive"] >= BREAKER_THRESHOLD and not _breaker["open"]:
            _breaker["open"] = True
            _breaker["opened_at"] = time.time()
            _breaker["trips"] += 1
            print(f"[mcp.breaker] OPEN after {BREAKER_THRESHOLD} consecutive "
                  f"failures: {err[:160]}")


def breaker_reset() -> None:
    with _breaker_lock:
        _breaker["consecutive"] = 0


def breaker_check() -> None:
    """Raise if the breaker is open. A half-open retry is allowed once the
    cooldown passes, so a transient failure does not disable tools forever."""
    with _breaker_lock:
        if not _breaker["open"]:
            return
        if (time.time() - _breaker["opened_at"]) < BREAKER_COOLDOWN_S:
            raise McpUnavailable(
                f"MCP tool subprocess is unavailable after "
                f"{BREAKER_THRESHOLD} consecutive failures "
                f"(last: {_breaker['last_error'][:120]}). Retrying in "
                f"{BREAKER_COOLDOWN_S - (time.time() - _breaker['opened_at']):.0f}s.")
        # Half-open: let exactly one attempt through.
        _breaker["open"] = False
        print("[mcp.breaker] half-open: allowing one retry")


def breaker_stats() -> dict:
    with _breaker_lock:
        return {
            "open": _breaker["open"],
            "consecutive_failures": _breaker["consecutive"],
            "trips": _breaker["trips"],
            "threshold": BREAKER_THRESHOLD,
            "cooldown_s": BREAKER_COOLDOWN_S,
            "last_error": _breaker["last_error"],
        }


def _call_key(name: str, args: dict) -> str:
    try:
        return name + ":" + json.dumps(args or {}, sort_keys=True,
                                       default=str)
    except Exception:
        return name + ":" + str(args)


async def _dispatch_tool(session: ClientSession, name: str, args: dict) -> str:
    """Run one MCP tool call and return its result as one text blob."""
    try:
        import asyncio as _a
        result = await _a.wait_for(
            session.call_tool(name, arguments=args),
            timeout=TOOL_CALL_TIMEOUT_S)
    except TimeoutError:
        return json.dumps(
            {"error": f"tool '{name}' timed out after "
                      f"{TOOL_CALL_TIMEOUT_S:.0f}s"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    parts: list[str] = []
    for c in (getattr(result, "content", None) or []):
        t = getattr(c, "text", None)
        parts.append(t if t is not None else str(c))
    return "\n".join(parts) if parts else ""


async def run_tool_loop(*, messages: list[dict], chat_fn, dispatch_fn,
                         max_hops: int = MAX_TOOL_HOPS,
                         on_event=None, on_outcome=None) -> dict:
    """The tool-use loop, factored for testability: `chat_fn(messages)`
    returns a gateway reply dict; `dispatch_fn(name, args)` returns result
    text. Returns the FINAL reply dict. Stops early with an explicit error
    reply on hop-cap or verifier-stall (same tool+args repeating).

    `on_event(kind, payload)` is an optional sync callback for live
    progress (chat streaming): kind is "tool_call" ({name}) or
    "tool_result" ({name, ok}). It must never raise — errors are swallowed
    so progress reporting can never break the loop.

    `on_outcome(name, arguments, ok, result_text, latency_s)` fires once per
    tool call with the measured latency. This is the single choke point every
    tool result passes through, which is what makes the tool-outcome memory
    write deterministic instead of depending on the model to remember to
    record it. Same contract: never raises, never blocks."""
    def _emit(kind: str, payload: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:
            pass
    last_reply: dict = {}
    repeats: dict[str, int] = {}
    for _ in range(max_hops + 1):
        reply = await chat_fn(messages)
        last_reply = reply
        tool_calls = reply.get("tool_calls") or []
        if not tool_calls:
            return reply
        for tc in tool_calls:
            key = _call_key(tc.get("name", ""), tc.get("arguments") or {})
            repeats[key] = repeats.get(key, 0) + 1
            if repeats[key] > MAX_SAME_CALL_REPEATS:
                return {"text": "", "tool_calls": [],
                        "provider": reply.get("provider", ""),
                        "error": f"verifier stall: tool '{tc.get('name')}' "
                                 f"called with identical arguments "
                                 f"{repeats[key]}x — aborting loop"}
        # Carry the assistant's tool-call turn back through.
        messages.append({
            "role": "assistant",
            "content": reply.get("text", "") or "",
            "tool_calls": tool_calls,
        })
        for tc in tool_calls:
            _emit("tool_call", {"name": tc.get("name", "")})
            _t0 = time.perf_counter()
            try:
                result_text = await dispatch_fn(tc.get("name", ""),
                                                tc.get("arguments") or {})
                ok = True
                _emit("tool_result", {"name": tc.get("name", ""), "ok": True})
            except Exception as e:
                result_text = f"tool error: {type(e).__name__}: {e}"
                ok = False
                _emit("tool_result", {"name": tc.get("name", ""), "ok": False})
            if on_outcome is not None:
                try:
                    on_outcome(tc.get("name", ""), tc.get("arguments") or {},
                               ok, result_text, time.perf_counter() - _t0)
                except Exception:
                    pass
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result_text[:8_000],  # cap per-tool reply
            })
    # Hit the hop cap. If the model never produced final text (only
    # tool_calls), returning last_reply would hand skills.py an empty
    # `text` that it parses as a successful-but-empty answer. Surface the
    # cap explicitly so the node fails loudly instead of silently.
    if last_reply.get("tool_calls"):
        return {"text": "", "tool_calls": last_reply.get("tool_calls"),
                "provider": last_reply.get("provider", ""),
                "error": f"tool-use hop cap ({max_hops}) reached without final text"}
    return last_reply


async def run_with_tools(*, prompt: str = "", tools_payload: list[dict],
                         agent: str, session_id: str,
                         provider_pin: str | None = None,
                         max_tokens: int = 2048,
                         temperature: float = 0.3,
                         messages: list[dict] | None = None,
                         on_event=None, on_outcome=None,
                         doc_ids: set[str] | None = None) -> dict:
    """Multi-turn chat: dispatch tool_calls via MCP, keep going until the
    model returns text. Returns the FINAL gateway reply dict (so callers
    can read `text`, `provider`, etc. the same way they would for a
    one-shot call). Pass `messages` for a full conversation (chat path);
    otherwise a single user `prompt` is used (skill path). `on_event` is
    forwarded to run_tool_loop for live progress, `on_outcome` for the
    tool-outcome memory write (see run_tool_loop).

    `doc_ids` is the set of uploaded documents this run may read. It reaches
    the MCP child through ARIA_DOC_IDS because the server is a separate
    process with no other way to learn which conversation invoked it. `None`
    means no document filtering; an empty set means no documents."""
    _messages: list[dict] = messages if messages is not None else [{"role": "user", "content": prompt}]
    _doc_env = _os.environ.copy()
    if doc_ids is not None:
        # An empty set is meaningful ("no documents"), so it must be sent as
        # an explicit marker rather than dropped as falsy.
        _doc_env["ARIA_DOC_IDS"] = ",".join(sorted(doc_ids)) or "-"
    last_reply: dict = {}

    # The child speaks JSON-RPC over stdio, and tool results routinely
    # contain non-ASCII (arrows, CJK, emoji — any fetched page). Without
    # these two settings the child inherits the Windows cp1252 locale, and
    # the FIRST such character crashes the tool call with
    # "UnicodeEncodeError: 'charmap' codec can't encode..." — which is how
    # entire researcher runs burned minutes on fetches that were doomed at
    # the encoding layer, not the network. PYTHONUTF8 makes the child use
    # UTF-8 for all stdio; `encoding` makes the client decode it as UTF-8
    # instead of the locale default.
    import os as _os
    # Fail fast and honestly if the tool subprocess is known-broken, instead of
    # spawning another child that will fail the same way.
    breaker_check()
    server_params = StdioServerParameters(
        command=sys.executable, args=[str(MCP_SERVER)],
        env={**_doc_env, "PYTHONUTF8": "1"},
        encoding="utf-8", encoding_error_handler="replace")
    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as mcp:
                await mcp.initialize()
                breaker_reset()   # a healthy handshake clears the counter

                async def _chat(messages: list[dict]) -> dict:
                    return await _gateway_chat(
                        messages=messages, tools=tools_payload,
                        agent=agent, session_id=session_id,
                        provider_pin=provider_pin,
                        max_tokens=max_tokens, temperature=temperature)

                async def _dispatch(name: str, args: dict) -> str:
                    return await _dispatch_tool(mcp, name, args)

                return await run_tool_loop(messages=_messages, chat_fn=_chat,
                                           dispatch_fn=_dispatch,
                                           on_event=on_event,
                                           on_outcome=on_outcome)
    except McpUnavailable:
        raise
    except BaseException as e:
        # A child that cannot start, or dies mid-handshake, is a systemic
        # failure — count it. The node still fails with this exception; the
        # breaker only changes what happens to the NEXT invocation.
        breaker_trip(f"{type(e).__name__}: {e}")
        raise


async def _gateway_chat(*, messages, tools, agent, session_id, provider_pin,
                        max_tokens, temperature) -> dict:
    import asyncio as _a
    # Python 3.8 compat: asyncio.to_thread was added in 3.9.
    if hasattr(_a, "to_thread"):
        return await _a.to_thread(
            LLM().chat,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            agent=agent,
            session=session_id,
            provider=provider_pin,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    loop = _a.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: LLM().chat(
            messages=messages,
            tools=tools,
            tool_choice="auto",
            agent=agent,
            session=session_id,
            provider=provider_pin,
            max_tokens=max_tokens,
            temperature=temperature,
        ),
    )
