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

import asyncio
import json
import re
import sys
import threading
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from gateway import LLM, cancel_requested

MCP_SERVER = Path(__file__).parent / "mcp_server.py"
MAX_TOOL_HOPS = 6  # hard cap so a model that loves tool-use can't cost a fortune
# Verifier-stall guard: the same tool with the same arguments more than
# this many times in one run means the model is stuck rewording a failed
# verification, not making progress. Global hop caps don't catch it
# because each call looks like forward motion.
MAX_SAME_CALL_REPEATS = 2
# How many tool calls from ONE assistant turn are dispatched at the same time.
# A coding agent asked to "read these files" emits several read_file calls in a
# single turn; running them serially makes the wall-clock cost the sum of the
# reads, which is the difference between a snappy loop and one that looks hung.
# Bounded so a model that emits fifty calls cannot fork fifty subprocess reads.
MAX_PARALLEL_TOOLS = 8
# With no hop cap, this is the backstop: a run of consecutive hops that produce
# no NEW successful tool call is not progress. Varying arguments defeat the
# repeat guard (a model re-reading a file at a fresh offset looks like
# pagination), so a run of them has to stop the loop. Generous enough for a real
# multi-file investigation.
MAX_NO_PROGRESS_HOPS = 8
# How many completed tool calls ride back into the model's context as a progress
# ledger. Without it the loop looks amnesiac: each hop is handed the user
# question plus the tool results, so a model re-derives its plan and re-reads the
# same file (observed live: four identical README reads in one turn, each
# preceded by the same "The user is asking..." reasoning).
#
# The ledger is NAMES AND ARGUMENTS, deliberately not the reasoning itself.
# Echoing the free-form reasoning was tried first and made it worse: the model
# copied the echoed prose straight back out, so every hop's thinking block was a
# superset of the previous one (252 -> 1138 -> 1272 -> 4320 chars, each opening
# with the identical sentence). Facts about what already ran are not restated as
# prose, and they answer the question that matters - "what have I already done?"
PROGRESS_LEDGER_CALLS = 12
# How much of a tool result rides back into the model's context. This used to
# be 8_000, which is smaller than an ordinary source file: read_file returns
# the whole file, the loop clipped it to the first 8 KB, and the model went
# looking for the part it had been shown but could not see - re-reading the
# same file until it hit the hop cap and answered nothing. 32 KB fits typical
# files whole while still bounding a runaway tool. Distinct from the TRUNCATED
# copy shown in stream events (~400 chars), which is display only.
MAX_TOOL_RESULT_MESSAGE_CHARS = 32_000
# ── consume-then-fold ───────────────────────────────────────────────────────
# The problem this measures. A node's tool results are appended to `messages`
# and re-sent on EVERY later hop, so a researcher that batch-fetches 20 pages
# carries all 20 forward for the rest of its life. The researcher also sets
# `max_tool_result_chars: 0` — no harness clip — because clipping mid-page is
# what made a model re-read the same file until it hit the hop cap. The two
# together produced the calls in the ledger: 330,468 to 429,333 characters of
# prompt, and median researcher OUTPUT collapsing from ~1,070 tokens (under
# 150k) to 266 (over 150k). The instruction that makes a document long tells
# each researcher to fetch exhaustively, so the feature is the trigger.
#
# Folding is what the 4 KB artifact design was for, applied to the path that
# actually holds the bytes. The node has already read the page — what it has
# not done is stop sending it. So a consumed result keeps its full text in the
# store and leaves the prompt as a handle plus a readable opening.
#
# Threshold below which folding is a bad trade: a handle, preview and note cost
# a few hundred characters themselves, so folding something small saves nothing
# and costs the model a tool call.
FOLD_MIN_CHARS = 6_000
# How many of the newest tool-result rounds stay whole. 1 means the model always
# has the material it JUST asked for in full, and folding only ever touches
# what it has already had a turn to use. Folding the newest would take back the
# page the node is in the middle of writing from.
FOLD_KEEP_ROUNDS = 1
FOLD_PREVIEW_CHARS = 1_200
# Only tools whose payload IS the document/answer text. Everything else keeps
# travelling whole, because for those the result is not prose to read once but
# facts that stay load-bearing: an artifact handle, a schedule id, a delivery
# receipt. Folding `schedule_task` would hide the very id the user types back
# to cancel the job.
FOLD_CONTENT_TOOLS = frozenset({
    "fetch_url", "fetch_pdf", "wayback_fetch", "extract_tables",
    "read_file", "read_document", "search_files", "list_dir", "lookup",
    "web_search", "wikipedia_search", "arxiv_search", "openalex_search",
    "news_search", "marginalia_search", "slack_history", "gmail_query",
    "github_query", "notion_query", "search_knowledge",
})
# `read_artifact` is the un-folding step. Folding what it returned would put the
# text back behind a handle and the node would re-fetch it, which is a loop.
FOLD_NEVER_TOOLS = frozenset({"read_artifact"})

_fold_notes = {"n": 0, "chars": 0}


def _fold_enabled() -> bool:
    try:
        from flags import is_enabled
        return bool(is_enabled("agent.fold_tool_results", True))
    except Exception as e:
        # Folding is an optimisation, so the safe default on a broken flag read
        # is the behaviour that has always worked. But a silent off-switch here
        # is how a feature can be dead for weeks while looking configured: say
        # once that the switch could not be read.
        if not _fold_notes.get("warned"):
            _fold_notes["warned"] = True
            print(f"[mcp] could not read agent.fold_tool_results ({e!r}); "
                  f"NOT folding this process — check flags.py/state/flags.json")
        return False


def _fold_consumed(messages: list, names_by_id: dict[str, str]) -> tuple[int, int]:
    """Replace already-consumed content results with a handle + preview.

    Returns (rounds_folded, characters_removed). Mutates `messages` in place.

    The switch is checked HERE, not only at the call site: a gate that only one
    caller honours is not a kill switch, it is a default.
    """
    import artifacts as _arts
    import json as _json

    if not _fold_enabled():
        return 0, 0

    # Find the tool rounds, newest first, and leave the newest few alone.
    rounds: list[list[int]] = []   # indices of tool messages sharing one assistant turn
    current: list[int] = []
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "tool":
            current.append(i)
        elif isinstance(m, dict) and m.get("role") == "assistant" and current:
            rounds.append(current)
            current = []
    if current:
        rounds.append(current)
    if len(rounds) <= FOLD_KEEP_ROUNDS:
        return 0, 0

    folded = 0
    saved = 0
    for rnd in rounds[:-FOLD_KEEP_ROUNDS]:
        for i in rnd:
            m = messages[i]
            body = m.get("content")
            if not isinstance(body, str) or len(body) < FOLD_MIN_CHARS:
                continue
            if "folded" in body[:80]:
                continue                      # already a stand-in
            name = names_by_id.get(str(m.get("tool_call_id") or ""), "")
            if (name in FOLD_NEVER_TOOLS
                    or (name and name not in FOLD_CONTENT_TOOLS)):
                # Unnamed is possible when ids do not survive a replay; leave
                # anything we cannot classify alone rather than hide it.
                continue
            try:
                art_id = _arts.put(body.encode("utf-8"),
                                   content_type="text/plain",
                                   source=f"folded:{name or 'tool'}",
                                   descriptor=(name or "tool result")[:200])
            except Exception:
                continue                      # never lose text to a store hiccup
            cut = len(body) - FOLD_PREVIEW_CHARS
            m["content"] = _json.dumps({
                "folded": True,
                "tool": name,
                "artifact": art_id,
                "bytes": len(body.encode("utf-8")),
                "chars": len(body),
                "preview": body[:FOLD_PREVIEW_CHARS]
                           + ("\n…" if cut > 0 else ""),
                "note": ("You already read this on an earlier turn. It is folded "
                         "to keep this request bounded, not deleted — the full "
                         f"text is at read_artifact(\"{art_id}\"). Do NOT re-run "
                         "the original call to get it back."),
            }, ensure_ascii=False)
            folded += 1
            saved += max(0, len(body) - len(m["content"]))
    return folded, saved


# Per-tool wall clock: a hung tool must fail the node, never the run.
TOOL_CALL_TIMEOUT_S = 120.0
# `run_checks` is the exception: a real test suite routinely takes minutes, and
# killing it at 120s reported "timed out" for a run that was still working. The
# child clamps its own timeout to 900s; match it here so the client waits at
# least as long as the child is allowed to run.
RUN_CHECKS_TIMEOUT_S = 900.0

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


_TEXT_TOOL_MARK = "[tool call] "
_TEXT_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")


def _text_tool_calls(text: str) -> list[dict]:
    """Recover calls that a provider replayed as prose instead of a call.

    Gemini 3 requires a thoughtSignature on a replayed `functionCall`, and the
    gateway substitutes `[tool call] name({...})` prose when it has none to
    send. Smaller Gemini models then copy that prose back as their own answer
    instead of calling the tool, so the loop ended with a literal
    "[tool call] computer_action(...)" string. A real answer never consists
    solely of these marker lines: require every non-empty line to be one, and
    parse the whole line, so ordinary prose that merely mentions the marker is
    left alone.
    """
    s = (text or "").strip()
    if not s.startswith(_TEXT_TOOL_MARK):
        return []
    calls: list[dict] = []
    for line in s.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith(_TEXT_TOOL_MARK):
            return []
        rest = line[len(_TEXT_TOOL_MARK):].lstrip()
        op = rest.find("(")
        if op <= 0:
            return []
        name = rest[:op].strip()
        if not _TEXT_TOOL_NAME.match(name):
            return []
        payload = rest[op + 1:].lstrip()
        try:
            args, end = json.JSONDecoder().raw_decode(payload)
        except ValueError:
            return []
        if payload[end:].strip() != ")" or not isinstance(args, dict):
            return []
        calls.append({"id": f"txt_{len(calls) + 1}", "name": name,
                      "arguments": args})
    return calls


def _brief_args(args: dict) -> str:
    """One short identifying string for a tool call, for the progress ledger
    that rides back into the model's context (PROGRESS_LEDGER_CALLS)."""
    try:
        vals = [str(v) for v in (args or {}).values()
                if isinstance(v, (str, int, float, bool))]
        return ", ".join(vals)[:70]
    except Exception:
        return ""


def _outcome_brief(text: str) -> str:
    """One short line on what a tool result CONTAINED, for the ledger.

    The bare name of a call is not enough: a model that has already read a file
    but cannot see that the read was cut off re-issues the identical call hoping
    for the rest (observed live: seven identical README reads in one turn). The
    pagination it needs is in the result, so put the fact that matters there.
    """
    try:
        d = json.loads(text)
    except Exception:
        return ""
    if not isinstance(d, dict):
        return ""
    if d.get("ok") is False:
        return "failed"
    bits: list[str] = []
    if d.get("truncated") or d.get("next_offset"):
        nxt = d.get("next_offset")
        end = d.get("end_line")
        if nxt:
            bits.append(f"truncated, continue at offset={nxt}")
        elif end:
            bits.append(f"truncated at line {end}")
    for k in ("line_count", "count", "n_hits"):
        v = d.get(k)
        if isinstance(v, int):
            bits.append(f"{k}={v}")
    return ", ".join(bits)[:90]


def _context_chars(messages: list[dict]) -> int:
    """Approximate prompt size in characters, tool calls included."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        tcs = m.get("tool_calls")
        if tcs:
            try:
                total += len(json.dumps(tcs, default=str))
            except Exception:
                pass
    return total


def _elide_oldest_tool_output(messages: list[dict]) -> bool:
    """Replace the oldest substantive tool result with a short marker.

    Returns False when nothing is left to elide. The message (and its
    tool_call id) stays, so the assistant/tool pairing remains valid; only the
    bulk goes. The model can re-ask with a narrower call - which is cheaper
    than the run dying with no answer at all.
    """
    for m in messages:
        if m.get("role") != "tool":
            continue
        c = str(m.get("content") or "")
        if len(c) <= 600 or c.startswith("[earlier tool output elided"):
            continue
        # A folded message already gave up its bulk and kept a handle. Eliding
        # it would replace JSON containing that handle with a marker telling the
        # model to re-run the call — losing the pointer to text that is still
        # intact in the store, and paying for the network again. Never trade a
        # handle for a "call it again".
        if '"folded"' in c[:80]:
            continue
        m["content"] = ("[earlier tool output elided to stay within the "
                        "context budget - call the tool again with a narrower "
                        "request if you still need it]")
        return True
    return False


async def _dispatch_tool(session: ClientSession, name: str, args: dict) -> str:
    """Run one MCP tool call and return its result as one text blob."""
    timeout = (RUN_CHECKS_TIMEOUT_S if name == "run_checks"
               else TOOL_CALL_TIMEOUT_S)
    try:
        import asyncio as _a
        result = await _a.wait_for(
            session.call_tool(name, arguments=args),
            timeout=timeout)
    except TimeoutError:
        return json.dumps(
            {"error": f"tool '{name}' timed out after "
                      f"{timeout:.0f}s"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    parts: list[str] = []
    for c in (getattr(result, "content", None) or []):
        t = getattr(c, "text", None)
        parts.append(t if t is not None else str(c))
    return "\n".join(parts) if parts else ""


async def run_tool_loop(*, messages: list[dict], chat_fn, dispatch_fn,
                         chat_stream_fn=None,
                         max_hops: int = MAX_TOOL_HOPS,
                         on_event=None, on_outcome=None,
                         approve_fn=None,
                         prepare_fn=None,
                         allowed_tools: set[str] | None = None,
                         max_context_chars: int = 0,
                         max_tool_result_chars: int | None = None,
                         capture_transcript: bool = False,
                         max_seconds: float = 0,
                         max_total_tokens: int = 0) -> dict:
    """The tool-use loop, factored for testability: `chat_fn(messages)`
    returns a gateway reply dict; `dispatch_fn(name, args)` returns result
    text. Returns the FINAL reply dict. Stops early with an explicit error
    reply on hop-cap or verifier-stall (same tool+args repeating).

    `on_event(kind, payload)` is an optional sync callback for live
    progress (chat streaming): kind is "tool_call" ({name,
    arguments}) or "tool_result" ({name, ok, result | error}).
    The arguments and a bounded result ride the events so an
    operator watching a stream sees what was actually dispatched
    and what came back — the name alone cannot answer either.
    It must never raise — errors are swallowed
    so progress reporting can never break the loop.

    `on_outcome(name, arguments, ok, result_text, latency_s)` fires once per
    tool call with the measured latency. This is the single choke point every
    tool result passes through, which is what makes the tool-outcome memory
    write deterministic instead of depending on the model to remember to
    record it. Same contract: never raises, never blocks.

    `prepare_fn(name, arguments)` runs BEFORE the call is dispatched and may
    return rewritten arguments. Anything that has to change what a tool
    receives must happen here, not in on_outcome - by then the call is
    already out over the wire. Also never raises.

    `approve_fn(name, arguments)` is the human gate for destructive tools. It
    is awaited IMMEDIATELY BEFORE dispatch and returns True to let the call
    through, False to refuse it. A refusal never reaches `dispatch_fn`, so a
    denied write cannot have a side effect - the model is told it was refused
    and continues. Callers that gate nothing pass None (every skill path),
    which keeps the gate opt-in rather than a change in behaviour for every
    existing caller. Also never raises: an exception is treated as a refusal,
    so a broken gate fails closed.

    `allowed_tools`, when given, is the set of names that may dispatch; a name
    outside it is answered with an error and never reaches `dispatch_fn` or
    the gate. `max_context_chars` bounds the accumulating prompt: the oldest
    tool outputs are elided first, and a prompt still over budget stops the
    loop with a stated reason rather than a gateway rejection.
    `max_tool_result_chars` overrides how much of a single tool result is fed
    back to the model; None uses the 32k default, and 0 or less means no
    harness clip (the tool's own guard still applies).

    `capture_transcript` attaches every dispatched call and its FULL result to
    the reply under `transcript` ([{name, arguments, ok, result}]). The
    researcher uses it to write its findings up from the pages it actually
    fetched instead of from a single summarising pass (see
    skills._sectioned_findings). Off by default: the transcript can hold
    megabytes of page text, and replies that travel to a client must not
    carry it."""
    def _emit(kind: str, payload: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:
            pass
    result_cap = (MAX_TOOL_RESULT_MESSAGE_CHARS
                  if max_tool_result_chars is None
                  else int(max_tool_result_chars))
    last_reply: dict = {}
    repeats: dict[str, int] = {}
    succeeded: set[str] = set()
    # What this run has already done, as a short factual ledger handed back to
    # the model on the next hop so it does not repeat work it cannot see.
    ledger: list[str] = []
    last_reply: dict = {}
    # Per-NODE token accounting. A tool-using node makes several LLM calls and
    # only the LAST one's usage survives in `last_reply`, so a researcher that
    # searched and fetched across 5 turns reported turn 5's tokens alone. The
    # Runs page needs the node's true total, so accumulate across turns.
    usage_in = 0
    usage_out = 0

    def _absorb(rep: dict) -> None:
        nonlocal usage_in, usage_out
        try:
            usage_in += int(rep.get("input_tokens") or 0)
            usage_out += int(rep.get("output_tokens") or 0)
        except Exception:
            pass

    # Optional evidence trail. Only populated when the caller asked for it, so
    # the normal path pays nothing and the reply stays small.
    transcript: list[dict] = []

    def _finish(rep: dict) -> dict:
        if capture_transcript:
            rep = dict(rep)
            rep["transcript"] = transcript
        return rep

    def _stopped() -> dict:
        # The operator pressed Stop. `error` rather than a bare empty reply
        # because every caller reads the two differently: an empty `text` with
        # no reason is treated as a model that produced nothing, and the author
        # and deck paths RESPOND to that by re-asking (skills.py's "empty
        # reply" retry), which spends a call on a run that has been called off.
        # `cancelled` is the honest label for a receipt — a stop is not a
        # fault — and the caller can already confirm it from its own flag.
        #
        # What this loop cannot do is interrupt the call it is awaiting: one
        # model round trip always finishes first.
        return _finish({"text": "", "tool_calls": [],
                "provider": last_reply.get("provider", ""),
                "input_tokens": usage_in, "output_tokens": usage_out,
                "cancelled": True,
                "error": "stopped by the operator"})

    # Unlimited when `max_hops <= 0`. A coding agent that has to read a dozen
    # files must not be cut off mid-investigation and then answer nothing —
    # which is exactly what an old fixed cap produced live. What stops a
    # runaway run is the pair of guards below, not a hop count: identical calls
    # abort at once, and a long run of hops that make no successful progress
    # does too. Callers that want a ceiling pass a positive number, which now
    # means exactly that many model calls (the old check allowed one extra).
    no_progress = 0
    empty_retries = 0
    # Consecutive hops on which the streamed call raised. One is a bad moment;
    # two is a backend that cannot stream this payload. See the except below.
    stream_failures = 0
    # Set on the hop AFTER an empty streamed reply, so that retry goes out
    # unstreamed — a different transport, and the empty completion is usually a
    # streaming-path event. It clears itself: the answer for the rest of the run
    # still streams. See the empty-reply branch below.
    _batched_once = False
    # tool_call_id -> tool name, so a later hop can tell WHAT KIND of bulk it is
    # looking at. The `tool` message itself only carries the id.
    tool_names_by_id: dict[str, str] = {}
    hop = 0
    run_started = time.time()
    while True:
        # The boundary a Stop can actually reach. A node is one model call away
        # from here, and until Stop was honoured at this line a long document
        # (ONE author node, 160 section calls) and a deep researcher (thirty
        # hops) both kept billing after the console said "stopped" — while
        # holding their idempotency claim, which is what made re-asking the
        # same question answer "an identical run is already in progress".
        if cancel_requested():
            return _stopped()
        if max_seconds > 0 and time.time() - run_started >= max_seconds:
            return _finish({
                "text": "", "tool_calls": [],
                "provider": last_reply.get("provider", ""),
                "input_tokens": usage_in, "output_tokens": usage_out,
                "error": (f"run time ceiling ({max_seconds:.0f}s) reached "
                          "without final text")})
        if max_total_tokens > 0 and (usage_in + usage_out) >= max_total_tokens:
            return _finish({
                "text": "", "tool_calls": [],
                "provider": last_reply.get("provider", ""),
                "input_tokens": usage_in, "output_tokens": usage_out,
                "error": (f"run token ceiling ({max_total_tokens}) reached "
                          "without final text")})
        if max_hops > 0 and hop >= max_hops:
            break
        hop += 1
        # Consume-then-fold, BEFORE any elision: this keeps the text and asks
        # nothing of the caller's budget. See the constants above for the
        # measurement that motivated it. The switch is honoured inside
        # `_fold_consumed`, so there is exactly one place that decides.
        _n, _saved = _fold_consumed(messages, tool_names_by_id)
        if _n:
            _fold_notes["n"] += _n
            _fold_notes["chars"] += _saved
            print(f"[mcp] folded {_n} consumed tool result(s) into artifact "
                  f"handles, -{_saved:,} chars this hop "
                  f"(process total: {_fold_notes['n']} folded / "
                  f"{_fold_notes['chars']:,} chars)")
        # Context budget: elide the oldest tool output until the prompt fits.
        # Without this an uncapped run feeds every prior result back on every
        # hop until the gateway rejects the request outright, losing the run.
        if max_context_chars > 0:
            while (_context_chars(messages) > max_context_chars
                   and _elide_oldest_tool_output(messages)):
                pass
            if _context_chars(messages) > max_context_chars:
                return _finish({"text": "", "tool_calls": [],
                        "provider": last_reply.get("provider", ""),
                        "input_tokens": usage_in,
                        "output_tokens": usage_out,
                        "error": f"context budget exceeded "
                                 f"({_context_chars(messages)} chars) — "
                                 "answer from what you already have"})
        if chat_stream_fn is not None and not _batched_once:
            try:
                # The streamed path forwards each text delta through on_event as
                # it arrives, so the answer appears token by token instead of in
                # one lump after the model has finished.
                reply = await chat_stream_fn(
                    messages, lambda d: _emit("text", {"delta": d}))
            except Exception:
                # A backend that cannot stream with tools (or a mid-stream drop)
                # must not fail the run: fall back to the batched call for THIS
                # hop. It used to also switch streaming off for every remaining
                # hop, which is what made a Discuss turn feel dead: one empty
                # or dropped stream at hop 1 meant the answer to the user's
                # actual question arrived as one lump at the end, even though
                # the next three hops were perfectly streamable. Two
                # consecutive failures is a capability problem; one is a bad
                # moment, and the retry on the next hop is free.
                stream_failures += 1
                if stream_failures >= 2:
                    chat_stream_fn = None
                reply = await chat_fn(messages)
            else:
                stream_failures = 0
        else:
            reply = await chat_fn(messages)
        _batched_once = False
        last_reply = reply
        _absorb(reply)
        tool_calls = reply.get("tool_calls") or []
        if not tool_calls:
            _from_text = _text_tool_calls(reply.get("text") or "")
            if _from_text:
                # The call is real even though the provider could only replay
                # it as prose. Treat it as the tool call it is, and do not also
                # keep the marker line as the model's answer.
                tool_calls = _from_text
                reply = dict(reply)
                reply["text"] = ""
                last_reply = reply
        if not tool_calls and not str(reply.get("text") or "").strip():
            # An empty reply is not an answer. On the streaming path the
            # upstream occasionally completes with zero text and zero calls
            # (observed with Gemini: 55s, output_tokens=0), which surfaced as
            # "ran out of steps" even though no tool had run. Retry once on the
            # batched path, which is also what happens if the stream raises.
            if empty_retries < 1:
                empty_retries += 1
                hop -= 1
                # The retry goes out on the batched path — a different request
                # shape is the only thing that makes a retry worth spending —
                # but streaming is NOT switched off. Disabling it here is what
                # turned one bad completion into a silent console for the rest
                # of the turn, and the user's own question is usually several
                # hops later.
                _batched_once = True
                continue
            return _finish({"text": "", "tool_calls": [],
                    "provider": reply.get("provider", ""),
                    "input_tokens": usage_in,
                    "output_tokens": usage_out,
                    "error": "the model returned an empty reply twice "
                             "(no text and no tool call)"})
        if not tool_calls:
            # No tool calls = the run is over. Replace the single turn's
            # usage with the loop's accumulated total so the node reports
            # everything it spent, not just this turn.
            reply = dict(reply)
            reply["input_tokens"] = usage_in
            reply["output_tokens"] = usage_out
            return _finish(reply)

        # Prepare EVERY call before dispatching any: the repeat guard is cheap
        # and must judge the whole batch, and `prepare_fn` is the only place a
        # rewrite can still change what a tool receives (once a call is out
        # over the wire it is too late).
        prepared: list[dict] = []
        for tc in tool_calls:
            _name = tc.get("name", "")
            _raw = tc.get("arguments") or {}
            key = _call_key(_name, _raw)
            repeats[key] = repeats.get(key, 0) + 1
            if repeats[key] > MAX_SAME_CALL_REPEATS:
                # A repeat of a call that already SUCCEEDED is not a stall, it
                # is a model that has what it needs and is looping anyway.
                # Observed live: render_document returned 200 and stored the
                # PDF, the model called it again unchanged, and this guard
                # aborted the loop - so the node died holding a real document
                # and the receipt reported nothing. Break out and keep it.
                if key in succeeded:
                    # A repeat of a call that already SUCCEEDED is not a stall, it
                    # is a model that has what it needs and is looping anyway.
                    # Observed live: render_document returned 200 and stored the
                    # PDF, the model called it again unchanged, and this guard
                    # aborted the loop - so the node died holding a real document
                    # and the receipt reported nothing. Break out and keep it.
                    return _finish({"text": reply.get("text", "") or "",
                            "tool_calls": [],
                            "provider": reply.get("provider", ""),
                            "input_tokens": usage_in,
                            "output_tokens": usage_out,
                            "note": ("repeated an already-successful call; "
                                     "stopping the loop")})
                return _finish({"text": "", "tool_calls": [],
                        "provider": reply.get("provider", ""),
                        "input_tokens": usage_in,
                        "output_tokens": usage_out,
                        "error": f"verifier stall: tool '{tc.get('name')}' "
                                 f"called with identical arguments "
                                 f"{repeats[key]}x — aborting loop"})
            _args = _raw
            if prepare_fn is not None:
                try:
                    _args = prepare_fn(_name, _args) or _args
                except Exception:
                    pass
            prepared.append({"tc": tc, "name": _name, "args": _args,
                             "key": key, "skey": _call_key(_name, _args),
                             "id": (tc.get("id") or
                                    f"t{hop}_{len(prepared)}")})

        # A Stop that lands after the model has asked for tools must not fire
        # them: these are side-effecting calls — file writes, renders,
        # third-party API posts — on a run the operator has already called off.
        # Checked BEFORE the assistant turn is appended, so a cancelled loop
        # never leaves a tool call with no result behind it; the chat path hands
        # the same `messages` list back on the next turn, and an unanswered
        # tool_call there is a malformed request.
        if cancel_requested():
            return _stopped()

        # Carry the assistant's tool-call turn back through, then announce every
        # call BEFORE any runs, so a parallel batch reads as a batch in the
        # transcript rather than trickling in.
        said = str(reply.get("text") or "").strip()
        # A ledger of what this run has ALREADY done. This is the part the model
        # cannot see: the results are in the tool messages, but nothing says
        # "you already read README.md", so it reads it again. Facts, not
        # reasoning - see PROGRESS_LEDGER_CALLS for why the reasoning was
        # removed.
        note = ""
        if ledger:
            note = "[already done this run: " + "; ".join(
                ledger[-PROGRESS_LEDGER_CALLS:]) + "]"
        messages.append({
            "role": "assistant",
            "content": f"{said}\n\n{note}".strip() if said else note,
            "tool_calls": tool_calls,
        })
        for p in prepared:
            _emit("tool_call", {"name": p["name"], "arguments": p["args"],
                                "id": p["id"]})

        # Dispatch the batch concurrently. Results keep the assistant's call
        # order (gather preserves input order) so the `role: tool` messages line
        # up with the tool_call ids the model sent.
        succeeded_before = len(succeeded)
        gate = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

        async def _run_one(p: dict) -> tuple:
            async with gate:
                _t0 = time.perf_counter()
                # Enforce the caller's allowlist HERE: a tool_call is
                # model-controlled input, and a name outside the set must not
                # reach the child even if the model was shown the schema by
                # some other path. Refused before the gate - there is nothing
                # to approve about a tool this agent does not have.
                if allowed_tools is not None and p["name"] not in allowed_tools:
                    return (p, False,
                            f"tool '{p['name']}' is not available to this "
                            "agent; nothing ran. Use one of: "
                            + ", ".join(sorted(allowed_tools)),
                            time.perf_counter() - _t0)
                # The gate runs before dispatch, inside the semaphore, so a
                # batch of writes queues one approval at a time instead of
                # firing a burst of prompts at the operator. Fail CLOSED: an
                # exception from approve_fn refuses the call.
                if approve_fn is not None:
                    try:
                        decision = await approve_fn(p["name"], p["args"])
                        if decision is not True:
                            # An approval callback may return a string to state
                            # WHY the call was refused (for example, "read the
                            # file before editing it"). A plain False keeps the
                            # old user-declined message.
                            if isinstance(decision, str) and decision.strip():
                                reason = decision.strip()
                            else:
                                reason = ("the user declined this change, so "
                                          "nothing was written. Do not retry it "
                                          "unchanged - ask what they want "
                                          "instead, or propose a different edit.")
                            return (p, False, f"refused: {reason}",
                                    time.perf_counter() - _t0)
                    except Exception as exc:           # noqa: BLE001
                        return (p, False,
                                f"refused: approval check failed "
                                f"({type(exc).__name__}: {exc}); nothing was "
                                f"written.",
                                time.perf_counter() - _t0)
                try:
                    text = await dispatch_fn(p["name"], p["args"])
                    return p, True, text, time.perf_counter() - _t0
                except Exception as exc:  # noqa: BLE001 — fed back as tool text
                    return (p, False,
                            f"tool error: {type(exc).__name__}: {exc}",
                            time.perf_counter() - _t0)

        if len(prepared) == 1:
            results = [await _run_one(prepared[0])]
        else:
            results = await asyncio.gather(
                *(_run_one(p) for p in prepared))

        for p, ok, result_text, latency in results:
            if ok:
                succeeded.add(p["skey"])
                # name(args) -> what it actually returned. The outcome is the
                # part the model cannot recover from the result alone: it tells a
                # truncated read apart from a complete one, which is what stops
                # the identical re-read.
                brief = f"{p['name']}({_brief_args(p['args'])})"
                got = _outcome_brief(result_text)
                ledger.append(f"{brief} -> {got}" if got else brief)
                _emit("tool_result", {"name": p["name"], "ok": True,
                                      "result": result_text[:1500],
                                      "id": p["id"]})
            else:
                ledger.append(f"{p['name']}({_brief_args(p['args'])}) -> failed")
                _emit("tool_result", {"name": p["name"], "ok": False,
                                      "error": result_text[:1500],
                                      "id": p["id"]})
            if on_outcome is not None:
                try:
                    on_outcome(p["name"], p["args"], ok, result_text, latency)
                except Exception:
                    pass
            if capture_transcript:
                # The FULL result, not the model-facing clip: this is evidence
                # for a second writing pass that never sees the tool messages.
                transcript.append({
                    "name": p["name"],
                    "arguments": p["args"] if isinstance(p["args"], dict) else {},
                    "ok": bool(ok),
                    "result": result_text if isinstance(result_text, str)
                    else str(result_text),
                })
            body = result_text
            if result_cap > 0 and len(body) > result_cap:
                # Never clip silently. A model handed a cut-off result with no
                # marker assumes it received everything, then burns its budget
                # trying to find the part that was withheld. Name the loss and
                # the way to a smaller request instead.
                dropped = len(body) - result_cap
                body = (body[:result_cap]
                        + f"\n\n[...truncated {dropped} more characters. "
                        "Ask for less: search_files for the exact text, or "
                        "read_file(path, offset, limit) for a line range. Do "
                        "not repeat the same call.]")
            # `tc` is read with .get() everywhere else because it is a plain dict
            # in production and a .get()-only object in the outcome tests. The
            # first version of this line used `p["tc"]["id"]` and raised
            # TypeError for four of them.
            _tc_id = str(p["tc"].get("id") or "")
            messages.append({
                "role": "tool",
                "tool_call_id": _tc_id,
                "content": body,
            })
            # Which tool this result came from. Folding needs it: the decision
            # is about the KIND of payload, and a `tool` message carries only an
            # id. Recorded at append so it survives a replay of the transcript.
            if _tc_id:
                tool_names_by_id[_tc_id] = p["name"]

        # Progress guard for the uncapped loop. `succeeded` only grows on a NEW
        # successful call, so a hop that merely re-reads (even at a fresh
        # offset, which slips past the repeat guard) counts as no progress.
        if len(succeeded) > succeeded_before:
            no_progress = 0
        else:
            no_progress += 1
            if no_progress >= MAX_NO_PROGRESS_HOPS:
                return _finish({"text": "", "tool_calls": [],
                        "provider": reply.get("provider", ""),
                        "input_tokens": usage_in,
                        "output_tokens": usage_out,
                        "error": f"stopped after {no_progress} consecutive "
                                 "steps with no successful tool result — the "
                                 "agent is stuck"})

    # Hit the hop cap. If the model never produced final text (only
    # tool_calls), returning last_reply would hand skills.py an empty
    # `text` that it parses as a successful-but-empty answer. Surface the
    # cap explicitly so the node fails loudly instead of silently.
    if last_reply.get("tool_calls"):
        return _finish({"text": "", "tool_calls": last_reply.get("tool_calls"),
                "provider": last_reply.get("provider", ""),
                "input_tokens": usage_in,
                "output_tokens": usage_out,
                "error": f"tool-use hop cap ({max_hops}) reached without final text"})
    last_reply = dict(last_reply)
    last_reply["input_tokens"] = usage_in
    last_reply["output_tokens"] = usage_out
    return _finish(last_reply)


def _context_node_affinity():
    """The affinity the running DAG node bound, if any.

    Imported lazily: `gateway` pulls in the client and the auto-start path, and
    mcp_runner is imported by the MCP child process, where that work is neither
    wanted nor cheap. A failure to read it is not fatal — it only means this
    loop falls back to its own `agent:session` group.
    """
    try:
        from gateway import current_node_affinity
        return current_node_affinity()
    except Exception:
        return None


async def run_with_tools(*, prompt: str = "", tools_payload: list[dict],
                         agent: str, session_id: str,
                         provider_pin: str | None = None,
                         model: str | None = None,
                         max_hops: int = MAX_TOOL_HOPS,
                         max_tokens: int = 2048,
                         temperature: float = 0.3,
                         messages: list[dict] | None = None,
                         route_affinity: str | None = None,
                         cache_system: bool | None = None,
                         on_event=None, on_outcome=None,
                         prepare_fn=None, approve_fn=None,
                         extra_dispatch=None,
                         doc_ids: set[str] | None = None,
                         sandbox_root: str | None = None,
                         extra_roots: list[str] | None = None,
                         browser_sid: str = "",
                         allowed_tools: set[str] | None = None,
                         max_context_chars: int = 0,
                         max_tool_result_chars: int | None = None,
                         capture_transcript: bool = False,
                         stream: bool = False,
                         max_seconds: float = 0,
                         max_total_tokens: int = 0) -> dict:
    """Multi-turn chat: dispatch tool_calls via MCP, keep going until the
    model returns text. Returns the FINAL gateway reply dict (so callers
    can read `text`, `provider`, etc. the same way they would for a
    one-shot call). Pass `messages` for a full conversation (chat path);
    otherwise a single user `prompt` is used (skill path). `on_event` is
    forwarded to run_tool_loop for live progress, `on_outcome` for the
    tool-outcome memory write (see run_tool_loop).

    `extra_dispatch(name, args)` is consulted FIRST for every tool call and
    returns a result string to answer it, or None to fall through to the MCP
    child. It lets the caller own a tool without that tool having to live in
    the child process - used by chat to hand off to the research and authoring
    agents, which run in the agent process.

    `doc_ids` is the set of uploaded documents this run may read. It reaches
    the MCP child through ARIA_DOC_IDS because the server is a separate
    process with no other way to learn which conversation invoked it. `None`
    means no document filtering; an empty set means no documents.

    `allowed_tools`, when given, is enforced on EVERY dispatch: a name outside
    the set is answered with an error instead of reaching the MCP child. The
    tool schemas the model is shown are not a security boundary - a model can
    emit any name - so the Code agent passes its allowlist here. `None` keeps
    the     old behaviour for callers that rely on the full server surface.
    `max_context_chars` bounds the accumulating prompt; see run_tool_loop.
    `max_tool_result_chars` overrides the per-result clip; 0 disables it.
    `capture_transcript` returns every tool result verbatim under `transcript`;
    see run_tool_loop."""
    _messages: list[dict] = messages if messages is not None else [{"role": "user", "content": prompt}]
    # `_os` was first imported sixteen lines below its use, so Python bound it as
    # a local and every entry raised `UnboundLocalError: cannot access local
    # variable '_os'`. It was invisible because the caller caught it and fell
    # back to a plain-text call, so a broken tool loop surfaced as a merely worse
    # answer rather than an error - and when the fallback produced nothing, as
    # the literal string "(empty answer)", it surfaced as success.
    import os as _os
    _doc_env = _os.environ.copy()
    if doc_ids is not None:
        # An empty set is meaningful ("no documents"), so it must be sent as
        # an explicit marker rather than dropped as falsy.
        _doc_env["ARIA_DOC_IDS"] = ",".join(sorted(doc_ids)) or "-"
    if sandbox_root:
        # The coding agent's file tools are sandbox-relative (mcp_server's
        # SANDBOX); point that sandbox at the user's project so read_file /
        # list_dir / search_files see the repo rather than a scratch dir.
        _doc_env["ARIA_SANDBOX"] = str(sandbox_root)
    if extra_roots:
        # A studio session may hold several folders. `ARIA_SANDBOX` is only ever
        # one of them — it decides where a BARE relative path lands — and this is
        # the rest of the allow-list. Without it the human's routes confine a
        # session to two projects and the agent is quietly confined to one, and
        # the asymmetry shows up as a tool write outside the folder the user
        # opened. Delimited by the path separator rather than a comma because
        # Windows paths contain commas legally.
        _doc_env["ARIA_ROOTS"] = ";".join(str(p) for p in extra_roots if p)
    # Which run this child belongs to. A fetched page is filed into Documents
    # with its provenance, and without the session id the library can say a row
    # came from the web but not which run pulled it — so a later "what did we
    # find for this question" has nothing to group by.
    if session_id:
        _doc_env["ARIA_SESSION"] = str(session_id)[:80]
    # Which STUDIO SESSION this child's browser belongs to — not which run.
    # A browser that dies with the run cannot hold a login, so the persistent
    # context in `browser_session.py` is keyed on this instead of on
    # `ARIA_SESSION`, which is now per-run so that one run's cost is readable.
    if browser_sid:
        _doc_env["ARIA_BROWSER_SID"] = str(browser_sid)[:80]
    last_reply: dict = {}
    # Affinity, decided here so the chat and code-agent paths get it without
    # each having to remember to ask. Inside a DAG node the executor already
    # bound the more specific "<session>:<node>" id, and that wins — deriving
    # `agent:session` there would collapse a run's nodes into one cache group
    # and pin them all to a single provider, which is the opposite of the
    # spread-across-the-key-pool behaviour the authoring batch cap relied on.
    # Outside a node there is no finer scope than the conversation, so
    # `agent:session` is the right group: a chat thread's history is a growing
    # prefix over the same system turn, which is the best cache case here.
    route_affinity = route_affinity or _context_node_affinity() or (
        f"{agent}:{session_id}" if session_id else None)
    # Explicit system-prompt caching, decided here rather than by the caller.
    # `cache_system` asks the gateway to mint a cached-content resource for this
    # node's system turn. Minting costs a call and storage, so it only pays when
    # the SAME prompt is sent again — and a tool loop is by construction that
    # case: the skill contract plus the tool grants are re-sent on every hop
    # while only the tail grows. `max_hops=1` is the one shape where it cannot
    # pay back, so it is excluded. An explicit True/False from the caller wins.
    if cache_system is None:
        cache_system = bool(tools_payload) and max_hops != 1
    # The tool loop's answer, kept OUTSIDE the client's async-with blocks.
    # Closing an anyio task group (the stdio session) can raise after the loop
    # has already produced its result; see the except clause below.
    loop_result: dict | None = None

    # The child speaks JSON-RPC over stdio, and tool results routinely
    # contain non-ASCII (arrows, CJK, emoji — any fetched page). Without
    # these two settings the child inherits the Windows cp1252 locale, and
    # the FIRST such character crashes the tool call with
    # "UnicodeEncodeError: 'charmap' codec can't encode..." — which is how
    # entire researcher runs burned minutes on fetches that were doomed at
    # the encoding layer, not the network. PYTHONUTF8 makes the child use
    # UTF-8 for all stdio; `encoding` makes the client decode it as UTF-8
    # instead of the locale default.
    # Fail fast and honestly if the tool subprocess is known-broken, instead of
    # spawning another child that will fail the same way.
    breaker_check()
    # Keep the pre-loop value out here so the except clause can tell a broken
    # MCP child from a model/gateway failure.
    handshake_ok = False
    server_params = StdioServerParameters(
        command=sys.executable, args=[str(MCP_SERVER)],
        env={**_doc_env, "PYTHONUTF8": "1"},
        encoding="utf-8", encoding_error_handler="replace")
    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as mcp:
                await mcp.initialize()
                handshake_ok = True
                breaker_reset()   # a healthy handshake clears the counter

                async def _chat(messages: list[dict]) -> dict:
                    return await _gateway_chat(
                        messages=messages, tools=tools_payload,
                        agent=agent, session_id=session_id,
                        provider_pin=provider_pin, model=model,
                        max_tokens=max_tokens, temperature=temperature,
                        route_affinity=route_affinity,
                        cache_system=cache_system)

                async def _dispatch(name: str, args: dict) -> str:
                    # Some tools are handled by the CALLER rather than the MCP
                    # child - chat's hand-off to the research/authoring
                    # agents, which run in this process and must not be
                    # shipped across the tool boundary to be called back.
                    # `extra_dispatch` returns None for "not mine", which keeps
                    # every other tool on the unchanged path.
                    if extra_dispatch is not None:
                        mine = await extra_dispatch(name, args)
                        if mine is not None:
                            return mine
                    return await _dispatch_tool(mcp, name, args)

                async def _chat_stream(messages: list[dict],
                                       on_text=None) -> dict:
                    # Same reply shape as _chat, but assembled from the stream.
                    # Usage is absent from a streamed response, so output tokens
                    # are estimated; the loop's own accounting overwrites them
                    # with the sum across hops either way.
                    parts: list[str] = []
                    reasoning: list[str] = []
                    tool_calls: list[dict] = []
                    provider = ""
                    # Which model actually answered (distinct from the `model`
                    # this closure was called with: rebinding that would pin the
                    # pinned value for every later hop).
                    answered = ""
                    async for ev in LLM().stream_chat(
                            messages=messages, tools=tools_payload,
                            agent=agent, session=session_id,
                            provider=provider_pin, model=model,
                            max_tokens=max_tokens, temperature=temperature,
                            # The streamed branch is the same loop's hops, so it
                            # must carry the same affinity. Without it the code
                            # agent — the longest, most repetitive prompt in the
                            # system, and the one path that always streams —
                            # would keep bouncing across providers and cache on
                            # none of them.
                            route_affinity=route_affinity):
                        kind = ev.get("type")
                        if kind == "text":
                            chunk = ev.get("text") or ""
                            parts.append(chunk)
                            if on_text and chunk:
                                on_text(chunk)
                        elif kind == "thinking":
                            # The model's reasoning. Forwarded live on its own
                            # event (the panel shows it collapsed) and kept so the
                            # final reply can report it too.
                            piece = ev.get("text") or ""
                            if piece:
                                reasoning.append(piece)
                                if on_event is not None:
                                    try:
                                        on_event("thinking", {"delta": piece})
                                    except Exception:
                                        pass
                        elif kind == "tool_calls":
                            tool_calls = ev.get("tool_calls") or []
                        elif kind == "done":
                            provider = ev.get("provider") or provider
                            # Which model actually answered. With "Auto" the
                            # caller pinned none, so this is the only way the
                            # panel can say what replied.
                            answered = ev.get("model") or answered
                    text = "".join(parts)
                    return {"text": text, "tool_calls": tool_calls,
                            "provider": provider, "model": answered,
                            "reasoning": "".join(reasoning),
                            "input_tokens": 0,
                            "output_tokens": len(text) // 4}

                loop_result = await run_tool_loop(
                    messages=_messages, chat_fn=_chat,
                    dispatch_fn=_dispatch,
                    chat_stream_fn=(_chat_stream if stream else None),
                    max_hops=max_hops,
                    on_event=on_event,
                    on_outcome=on_outcome,
                    approve_fn=approve_fn,
                    prepare_fn=prepare_fn,
                    allowed_tools=allowed_tools,
                    max_context_chars=max_context_chars,
                    max_tool_result_chars=max_tool_result_chars,
                    capture_transcript=capture_transcript,
                    max_seconds=max_seconds,
                    max_total_tokens=max_total_tokens)
    except McpUnavailable:
        raise
    except BaseException as e:
        # The MCP session's anyio task group is torn down AFTER the loop has
        # already produced its answer, and that teardown can raise
        # `ExceptionGroup: unhandled errors in a TaskGroup` (a cancelled stdio
        # reader, a child that exits as we close the pipes). That used to fail a
        # node whose tool work had SUCCEEDED - observed live as a retriever
        # failing at elapsed_s 0.0 alongside a completed answer. An answer in
        # hand outranks a teardown fault: keep it. Only a failure with no
        # result at all is a real tool failure.
        if loop_result is not None and not isinstance(e, asyncio.CancelledError):
            print(f"[mcp] ignoring teardown fault after a completed loop: "
                  f"{type(e).__name__}: {e}")
            return loop_result
        # A teardown fault can MASK the loop's own failure: when `__aexit__`
        # raises while unwinding, Python sets the group as the new exception
        # and keeps the real cause in __context__. Report and re-raise the
        # cause, or the node blames "transport closed" for a gateway error.
        cause: BaseException = e
        if isinstance(e, BaseExceptionGroup) and e.__context__ is not None:
            cause = e.__context__
        # A child that cannot start, or dies mid-handshake, is a systemic
        # failure — count it. The node still fails with this exception; the
        # breaker only changes what happens to the NEXT invocation. A gateway
        # error or a model rejection is NOT an MCP fault: counting those opened
        # the breaker against a perfectly healthy child and turned one bad
        # model response into 30s of "tools unavailable" for every run.
        if not handshake_ok:
            breaker_trip(f"{type(cause).__name__}: {cause}")
        raise cause
    return loop_result or {}


async def _gateway_chat(*, messages, tools, agent, session_id, provider_pin,
                        model=None, max_tokens, temperature,
                        route_affinity=None, cache_system=None) -> dict:
    import asyncio as _a
    # Every hop of one node carries the same `route_affinity`, so the gateway
    # keeps the node on the provider it first chose instead of re-picking by
    # load. Provider caches are per provider, so a node that bounced between
    # providers paid its full (large, tool-heavy) prompt again on every hop.
    # `cache_system` is the caller's call, not this function's: explicit
    # caching costs a mint on the first call, so only a caller that KNOWS it
    # will make several hops should ask for it.
    _kw = dict(messages=messages, tools=tools, tool_choice="auto",
               agent=agent, session=session_id, provider=provider_pin,
               model=model, max_tokens=max_tokens, temperature=temperature,
               route_affinity=route_affinity)
    if cache_system:
        _kw["cache_system"] = True

    def _go():
        return LLM().chat(**_kw)

    # Python 3.8 compat: asyncio.to_thread was added in 3.9.
    if hasattr(_a, "to_thread"):
        return await _a.to_thread(_go)
    loop = _a.get_event_loop()
    return await loop.run_in_executor(None, _go)
