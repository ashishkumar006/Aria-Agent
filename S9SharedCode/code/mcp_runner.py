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
# Tools that change the workspace. A successful one invalidates the repeat
# counters: `edit_file` then `run_checks` then `edit_file` then `run_checks` is
# three identical CHECK calls, and all three are meaningful — the second and
# third are measuring different code. Without this, a normal
# fix-then-rerun cycle trips the loop guard and the run ends early with
# "repeated an already-successful call".
_WRITE_TOOLS = frozenset({
    "create_file", "edit_file", "update_file", "write_file", "apply_symbol_edit",
    "delete_file", "make_dir", "apply_rename",
})
# When the model repeats an already-successful call but the last test/check run
# still failed, don't dead-end: hand it the failing output plus a corrective
# instruction and let it change strategy, up to this many times. Observed live:
# a coding agent looped re-editing source while its flawed tests could never
# pass, and the old guard aborted the run instead of letting it fix the tests.
MAX_STALL_RECOVERIES = 2
# Idempotent reads are EXEMPT from the repeat guard. Re-listing a directory or
# re-reading a file is normal orientation, not a stall — and because the guard
# judges the whole batch, aborting on a repeated read also discarded the
# productive create_file/edit_file calls sitting beside it in the same turn
# (observed live: a good code model re-called list_dir and the loop died before
# its writes ever ran). Reads are cheap and bounded by the hop cap; only
# side-effecting repeats are treated as a stall.
_REPEAT_EXEMPT_READS = frozenset({
    "list_dir", "read_file", "search_files", "read_artifact", "git_inspect",
    "document_symbols", "find_references", "blast_radius",
})
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
# ── context compaction ───────────────────────────────────────────────────────
# Folding handles the bytes that are SIX THOUSAND CHARACTERS OR MORE. These
# knobs handle the long tail: results of 900 chars to 6 kB, and the long
# assistant-authored "said" text that re-enters every prompt. Neither was
# touched by the old elision — elision replaced a 900-char README preview with
# a marker telling the model to call the tool again, which was a worse answer
# than a truncated one.
#
# The newest tool round is never compacted: that is the material the model is
# writing from right now. Older rounds get:
#   * tool results → head + tail + a byte count, so the model keeps the
#     load-bearing first lines (file header, JSON keys) and the end
#     (the interesting value of a check run, the verdict line of a tool
#     result), and loses only the middle.
#   * assistant "said" text → head + tail + a count, so the task framing and
#     the verdict survive, and the middle goes.
# Keys, ids and tool-pair structure are always preserved: the MCP pairing
# remains valid after the rewrite.
COMPACT_WHEN_CHARS = 400_000        # ~100k tokens; comfortably under the elision point
COMPACT_TOOL_MIN = 900              # don't bother with snippets
COMPACT_TOOL_KEEP_HEAD = 700
COMPACT_TOOL_KEEP_TAIL = 300
COMPACT_SAID_MIN = 400
COMPACT_SAID_KEEP_HEAD = 250
COMPACT_SAID_KEEP_TAIL = 150
# Only kick in when the context is actually large — a small prompt spends
# nothing, so compaction is free of cost there.
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


# A model call that fails on the SERVER's side is worth retrying; one that fails
# because the request itself is wrong is not. The distinction is made from the
# error text because the gateway seam raises RuntimeError with the HTTP status
# in it, and a dedicated exception type would have to be threaded through every
# provider adapter to say the same thing.
_LLM_MAX_ATTEMPTS = 5
_LLM_RETRY_BASE_S = 1.0
# A rate limit is a wait, not a verdict: a free tier usually admits the same
# request seconds later, and the alternative is throwing away a run that is
# already deep into its work.
_LLM_RATE_LIMIT_ATTEMPTS = 6
_LLM_RATE_LIMIT_BASE_S = 5.0
# Ceiling on a single rate-limit wait. Long enough to cross a free-tier window,
# short enough that the console still looks alive.
_LLM_RATE_LIMIT_MAX_S = 120.0
# Empty completions (no text, no tool call) are their own retry: a different
# request shape is tried, not the same one.
_EMPTY_REPLY_RETRIES = 2
# Times a run will re-ask when the model sends its thinking as the answer.
_MAX_REASONING_LEAKS = 3
# Openers that read as deliberation rather than as a reply. Deliberately few
# and specific: a false positive costs one extra model call, a false negative
# shows the person a wall of thinking where the answer should be.
_REASONING_OPENERS = (
    "we need to", "we need build", "we need create", "we need add",
    "we need implement", "we need to build", "let me ", "i need to ",
    "i should ", "first, i", "first i", "okay, i", "so i need",
    "the user wants me to", "the user asked me to",
    "so ruff", "so the", "so i", "so we", "i'll ", "i will ",
    "let's ", "we should ", "need to ", "need build",
)
# A wall of text with no code fence and several of these is deliberation, not an
# answer — live: 32,783 characters of planning returned as the reply.
_REASONING_MARKERS = ("let me", "i need", "we need", "i should", "we should",
                      "probably", "maybe", "let's", "i'll", "need to")
# A coding agent's answers are summaries. Past this many characters the reply is
# the model thinking on the page — live: 32,783 characters of planning with one
# code fence in it, returned as the answer to a build task.
_LONG_CODING_ANSWER_CHARS = 8000


def _looks_like_reasoning(text: str, short_answer_guard: bool = False) -> bool:
    """True when a reply reads as the model's deliberation, not its answer.

    Heuristic by construction, and only ever used to *retry* — never to
    rewrite or truncate what the person is shown. Long, opener-led, no code
    fence: an answer that cites files or shows a diff is left alone.
    """
    body = (text or "").strip()
    if len(body) < 200:
        return False
    # A coding agent's answer is a summary: paths, names, what changed. A reply
    # of many thousands of characters is the model thinking on the page, even
    # when it happens to include a snippet. Opt-in, because the authoring path
    # legitimately returns documents.
    if short_answer_guard and len(body) > _LONG_CODING_ANSWER_CHARS:
        return True
    # A reply that carries a code fence is showing work, not deliberating —
    # naming a file is NOT such a signal, because thinking out loud names files
    # constantly ("the caller in cli.py uses two of them").
    if "```" in body:
        return False
    head = body[:200].lower()
    if any(head.startswith(op) or op in head[:120] for op in _REASONING_OPENERS):
        return True
    # No recognisable opener, but a wall of text with no code that keeps
    # deliberating ("probably", "maybe", "let me") is the same thing wearing a
    # different first sentence — live: 32k characters starting "So ruff is a
    # separate executable, not a module…".
    if len(body) > 2000:
        low = body.lower()
        hits = sum(1 for m in _REASONING_MARKERS if m in low[:4000])
        if hits >= 3:
            return True
    return False
_TRANSIENT_LLM_MARKS = (
    "http 429", "http 500", "http 502", "http 503", "http 504", "http 408",
    "rate limit", "ratelimit", "too many requests", "overloaded",
    "temporarily unavailable", "service unavailable", "bad gateway",
    "gateway time-out", "gateway timeout", "timed out", "timeout",
    "connection reset", "connection aborted", "connection refused",
    "connection error", "remote end closed", "incomplete chunked",
    "getaddrinfo", "nodename nor servname", "temporary failure in name",
    # The provider client wraps every upstream fault as "kilo failed: <exc>" and
    # sometimes masks the cause (a 429 body rendered as KeyError 'name'). No
    # message with that prefix is the request's fault, so all of them are worth
    # another attempt.
    "kilo failed",
)


def _retry_after_seconds(e: BaseException) -> int:
    """Seconds the provider asked us to wait, when it said so.

    A 429 that names a delay is not a guess: waiting what it asked for is both
    kinder and more likely to work than exponential backoff. Accepts the common
    spellings ("retry in 45s", "try again in 30 seconds", `"retry_after": 60`)
    and answers 0 when it said nothing.
    """
    text = str(e or "")
    m = re.search(r"retry[-_ ]?after\D{0,12}(\d{1,5})", text, re.I)
    if not m:
        m = re.search(r"(?:retry|try again|wait)\D{0,24}?(\d{1,5})\s*(?:s|sec|secs|seconds)\b",
                      text, re.I)
    if not m:
        return 0
    try:
        return max(1, min(int(m.group(1)), 300))
    except ValueError:
        return 0


def _is_rate_limit_error(e: BaseException) -> bool:
    """True when the provider refused for rate reasons rather than availability.

    "Quota exceeded" is deliberately NOT here: an exhausted quota does not clear
    in three seconds, and retrying it only delays the honest error.
    """
    text = str(e or "").lower()
    return any(mark in text for mark in (
        "429", "rate limit", "rate_limit", "too many requests"))


def _is_transient_llm_error(e: BaseException) -> bool:
    """True when a failed model call is worth another attempt.

    Conservative on purpose: a 400/401/403/404 in the text means the request was
    wrong, and retrying it only spends the user's time. Anything that looks like
    an availability problem — a 5xx, a 429, a timeout, a dropped connection — is
    retried.
    """
    text = str(e or "").lower()
    if not text:
        return False
    m = re.search(r"\bhttp (\d{3})\b", text)
    if m:
        code = int(m.group(1))
        return code >= 500 or code in (408, 409, 425, 429)
    return any(mark in text for mark in _TRANSIENT_LLM_MARKS)


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


def _tool_round_spans(messages: list[dict]) -> list[list[int]]:
    """Indices of `tool` messages that belong to one assistant tool round.

    A round is the contiguous run of `tool` messages that follows one
    assistant message carrying `tool_calls`. Newest last. Grouped so the
    most recent batch can stay whole while older batches get compressed.
    """
    rounds: list[list[int]] = []
    current: list[int] = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            if current:
                rounds.append(current)
                current = []
            continue
        if m.get("role") == "assistant" and "tool_calls" in m:
            if current:
                rounds.append(current)
            current = []
        elif m.get("role") == "tool":
            current.append(i)
        else:
            if current:
                rounds.append(current)
                current = []
    if current:
        rounds.append(current)
    return rounds


def _clamp_head_tail(body: str, head: int, tail: int, note: str) -> str:
    keep = head + tail + len(note) + 4
    if len(body) <= keep:
        return body
    return body[:head] + f"\n[…{note}: {len(body) - head - tail} chars elided]\n" + body[-tail:]


async def _llm_compact(messages: list[dict], compaction_fn) -> bool:
    """Replace all but the newest tool round with one instruction-driven
    summary (prompts/compaction.md). Whole old rounds are dropped together so
    assistant tool_call / tool-result pairing stays valid; the summary is
    inserted as a single user message of established facts. Returns True when
    the history was rewritten. Any failure leaves messages untouched so the
    mechanical tiers remain the safety net."""
    rounds = _tool_round_spans(messages)
    if len(rounds) < 2:
        return False
    last_start = rounds[-1][0]
    old = messages[1:last_start]
    if not old:
        return False
    try:
        summary = await compaction_fn(old)
    except Exception:
        return False
    if not summary or not str(summary).strip():
        return False
    messages[1:last_start] = [{
        "role": "user",
        "content": ("[CONTEXT SUMMARY of earlier turns - treat as established "
                    "facts; re-read a narrow range if you need exact text]\n"
                    + str(summary).strip()),
    }]
    return True


def _compact_context(messages: list[dict], budget_chars: int) -> bool:
    """Compress old tool results and old assistant tool-call narration when
    the prompt is carrying too much. The newest tool round and the most
    recent assistant tool-call turn are always left whole — that is the
    working set the model is writing from right now. Everything ahead of
    the boundary is history; losing detail there is the whole point of
    compaction.
    Returns True when at least one message changed.
    """
    changed = False
    if _context_chars(messages) <= budget_chars:
        return False
    rounds = _tool_round_spans(messages)
    if not rounds:
        return False

    # Indices the model needs in full: the messages sitting inside the
    # newest round of tool calls, plus the newest assistant turn that
    # carries tool_calls (the turn that opened that round).
    keep_assistants = set()
    last_round = rounds[-1]
    for i in range(len(messages)):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "assistant" \
                and isinstance(m.get("tool_calls"), list) and m["tool_calls"]:
            keep_assistants.add(i)

    def _inside_newest_round(idx: int) -> bool:
        start = 0
        for r in rounds:
            if r[0] <= idx <= r[-1]:
                return r is last_round
            start = r[-1] + 1
        return False

    for idx, m in enumerate(messages):
        if not isinstance(m, dict):
            continue

        # Assistant narration leading to a tool call: compress it. The
        # progress ledger carries the operative facts; the prose was never
        # load-bearing, just noisy.
        body = m.get("content")
        if m.get("role") == "assistant" \
                and isinstance(m.get("tool_calls"), list) and m["tool_calls"]:
            if isinstance(body, str) and len(body) > COMPACT_SAID_MIN \
                    and idx != max(k for k in keep_assistants):
                new = _clamp_head_tail(body, COMPACT_SAID_KEEP_HEAD,
                                       COMPACT_SAID_KEEP_TAIL,
                                       "context compacted")
                if new != body:
                    m["content"] = new
                    changed = True
            # Old tool-call arguments carry full file bodies (create_file /
            # update_file / edit_file content). Left whole they accumulate and
            # inflate the prompt with text that is already on disk and
            # re-readable. Clamp them in old rounds; the newest round stays whole.
            if idx != max(k for k in keep_assistants):
                for tc in m["tool_calls"]:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    raw_args = fn.get("arguments", tc.get("arguments"))
                    was_str = isinstance(raw_args, str)
                    args = raw_args
                    if was_str:
                        try:
                            args = json.loads(raw_args)
                        except Exception:
                            args = None
                    if isinstance(args, dict):
                        for k in ("content", "replace", "code", "new_str",
                                  "find", "old_code"):
                            v = args.get(k)
                            if isinstance(v, str) and len(v) > 400:
                                args[k] = v[:200] + f"…[{len(v) - 200} chars compacted]"
                                changed = True
                        if was_str:
                            fn["arguments"] = json.dumps(args)
                        else:
                            tc["arguments"] = args
            continue

        if m.get("role") != "tool" \
                or not isinstance(body, str) \
                or len(body) < COMPACT_TOOL_MIN \
                or _inside_newest_round(idx):
            continue

        new = _clamp_head_tail(body, COMPACT_TOOL_KEEP_HEAD,
                               COMPACT_TOOL_KEEP_TAIL,
                               "context compacted")
        if new != body:
            m["content"] = new
            changed = True
    return changed

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
                         max_context_tokens: int = 0,
                         max_tool_result_chars: int | None = None,
                         capture_transcript: bool = False,
                         max_seconds: float = 0,
                         max_total_tokens: int = 0,
                         compaction_fn=None,
                          short_answers: bool = False) -> dict:
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
    stall_recoveries = 0
    last_verify_failure = ""
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
    # Live prompt size in TOKENS, anchored to the gateway's real input_tokens
    # after each model call; between calls we add ~len/4 for appended tool text.
    # Compaction keys off this (real) rather than a pure char guess.
    ctx_tokens = 0

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
    # Times the model has answered with its own thinking instead of an answer.
    reasoning_leaks = 0
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
        # Context budget. Two tiers before the hard ceiling:
        #   1) Compaction — approaching the limit, compress older tool results
        #      and the assistant's own narration (the progress ledger keeps the
        #      facts, so prose is the thing losing bytes).
        #   2) Elision — still over, replace the oldest remaining result with a
        #      "call again narrower" marker.
        # Without both, an uncapped run feeds every prior result back on every
        # hop until the gateway rejects the request outright, losing the run.
        if max_context_chars > 0 or max_context_tokens > 0:
            # The compaction limit IS the model's max context size. Prefer the
            # REAL token count (gateway input_tokens) when the caller supplies the
            # model window in tokens; fall back to the char budget otherwise.
            compact_target = max_context_chars or max_context_tokens * 4
            _over = _context_chars(messages) > compact_target or (
                max_context_tokens > 0
                and ctx_tokens > max_context_tokens * 4 // 5)
            # Instruction-driven compaction first (prompts/compaction.md): an
            # LLM summarizes the old rounds into one structured state message,
            # preserving goals/decisions/files/tests instead of blind clipping.
            # Falls back to the mechanical tiers below if absent or it fails.
            if compaction_fn is not None and _over:
                await _llm_compact(messages, compaction_fn)
            _compact_context(messages, compact_target)
            while (max_context_chars > 0
                   and _context_chars(messages) > max_context_chars
                   and _elide_oldest_tool_output(messages)):
                pass
            ctx_tokens = _context_chars(messages) // 4
            if max_context_chars > 0 and _context_chars(messages) > max_context_chars:
                return _finish({"text": "", "tool_calls": [],
                        "provider": last_reply.get("provider", ""),
                        "input_tokens": usage_in,
                        "output_tokens": usage_out,
                        "error": f"context budget exceeded "
                                 f"({_context_chars(messages)} chars) — "
                                 "answer from what you already have"})
        # A model call that fails for a reason on the SERVER's side (a 502 from a
        # free provider, a rate limit, a dropped connection) must not throw away
        # a run that is already 40 tool calls deep. Live 2026-11-22: a
        # `gateway HTTP 502: {"detail":"kilo failed: "}` at hop 9 of a large
        # build killed the run after 191s and left the user with nothing.
        # Retried with backoff; anything else (a bad request, a refusal) is
        # raised at once, because retrying it changes nothing.
        attempts = 0
        while True:
            attempts += 1
            try:
                if chat_stream_fn is not None and not _batched_once:
                    try:
                        # The streamed path forwards each text delta through
                        # on_event as it arrives, so the answer appears token by
                        # token instead of in one lump after the model has
                        # finished.
                        reply = await chat_stream_fn(
                            messages, lambda d: _emit("text", {"delta": d}))
                    except Exception:
                        # A backend that cannot stream with tools (or a
                        # mid-stream drop) must not fail the run: fall back to
                        # the batched call for THIS hop. It used to also switch
                        # streaming off for every remaining hop, which is what
                        # made a Discuss turn feel dead: one empty or dropped
                        # stream at hop 1 meant the answer to the user's actual
                        # question arrived as one lump at the end, even though
                        # the next three hops were perfectly streamable. Two
                        # consecutive failures is a capability problem; one is a
                        # bad moment, and the retry on the next hop is free.
                        stream_failures += 1
                        if stream_failures >= 2:
                            chat_stream_fn = None
                        reply = await chat_fn(messages)
                    else:
                        stream_failures = 0
                else:
                    reply = await chat_fn(messages)
                break
            except Exception as e:
                # A free tier's rate limit is a WAIT, not a verdict: the same
                # request usually succeeds a few seconds later. Five attempts
                # with a longer base is what keeps a 40-call run alive through
                # one — live 2026-11-22, a 429 at call 12 of a large build ended
                # the run with an empty answer.
                rate_limited = _is_rate_limit_error(e)
                limit = _LLM_RATE_LIMIT_ATTEMPTS if rate_limited else _LLM_MAX_ATTEMPTS
                if attempts >= limit or cancel_requested() \
                        or not _is_transient_llm_error(e):
                    raise
                base = _LLM_RATE_LIMIT_BASE_S if rate_limited else _LLM_RETRY_BASE_S
                delay = base * (2 ** (attempts - 1))
                if rate_limited:
                    # Wait what it asked for when it said, else back off — and
                    # never wait so long that the person thinks it died.
                    delay = _retry_after_seconds(e) or min(delay, _LLM_RATE_LIMIT_MAX_S)
                _emit("status", {"text": (
                    f"model call failed ({str(e)[:120]}) — retrying in "
                    f"{delay:.0f}s ({attempts}/{limit - 1})")})
                await asyncio.sleep(delay)
        _batched_once = False
        last_reply = reply
        _absorb(reply)
        # Anchor the live context size to the gateway's REAL prompt tokens for
        # this call (corrects the between-call char estimate).
        if int(reply.get("input_tokens") or 0) > 0:
            ctx_tokens = int(reply["input_tokens"])
        tool_calls = reply.get("tool_calls") or []
        # A tool call with no usable name is malformed, and letting it through
        # poisons the rest of the run: the assistant turn carries a call the
    # provider then refuses ("function.name must be a string"), so every later
    # request in the same conversation 400s. Drop it here and say so, which
    # keeps the history valid and tells the model what to fix.
        dropped_calls = [tc for tc in tool_calls
                         if not str((tc or {}).get("name") or "").strip()]
        if dropped_calls:
            tool_calls = [tc for tc in tool_calls if tc not in dropped_calls]
            reply = dict(reply)
            reply["tool_calls"] = tool_calls
            last_reply = reply
            _emit("status", {"text": (
                f"ignored {len(dropped_calls)} tool call(s) with no name — "
                "call the tool by its name")})
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
        # Reasoning arriving as the ANSWER. Some providers stream their thinking
        # in `content` instead of a reasoning field, so the run ends with a
        # paragraph of deliberation where the answer should be — and the parser
        # did nothing wrong, it was handed prose. Live: "We need build project.
        # Need inspect environment…" became the reply to a build task.
        #
        # This is a parser-level repair, not a prompt rule: the text is moved to
            # the thinking channel and the hop is retried, so the model gets to
            # answer. Bounded, because a model that always does this is a model
            # that cannot finish, and one honest attempt is worth spending.
        if not tool_calls and _looks_like_reasoning(reply.get("text") or "",
                                                  short_answers):
            if reasoning_leaks < _MAX_REASONING_LEAKS:
                reasoning_leaks += 1
                # Keep going rather than ending: the model is mid-thought, often
                # right after a failing check, and a fresh turn with its own
                # words in the history is what lets it act. Ending here is how a
                # run stopped with 72 failing tests it had just written.
                _emit("thinking", {"text": str(reply.get("text") or "")[:4000]})
                messages.append({"role": "assistant",
                                 "content": str(reply.get("text") or "")})
                _emit("status", {"text": "the model sent its thinking as the "
                                         "answer — continuing"})
                await asyncio.sleep(_LLM_RETRY_BASE_S)
                continue
            # Out of retries: say so. Passing the deliberation through as the
            # answer is the one outcome that looks like success while saying
            # nothing — the person reads a wall of thinking and believes the run
            # finished.
            return _finish({
                "text": "", "tool_calls": [],
                "provider": last_reply.get("provider", ""),
                "input_tokens": usage_in, "output_tokens": usage_out,
                "error": "the model answered with its own reasoning "
                         f"{reasoning_leaks + 1} times and produced no answer"})

        if not tool_calls and not str(reply.get("text") or "").strip():
            # upstream occasionally completes with zero text and zero calls
            # (observed with Gemini: 55s, output_tokens=0), which surfaced as
            # "ran out of steps" even though no tool had run. Retry once on the
            # batched path, which is also what happens if the stream raises.
            if empty_retries < _EMPTY_REPLY_RETRIES:
                empty_retries += 1
                hop -= 1
                # The retry goes out on the batched path — a different request
                # shape is the only thing that makes a retry worth spending —
                # but streaming is NOT switched off. Disabling it here is what
                # turned one bad completion into a silent console for the rest
                # of the turn, and the user's own question is usually several
                # hops later. Two empty completions in a row is a provider bad
                # moment, not a verdict: the second retry is cheap and the run
                # it saves is 40 tool calls deep.
                _batched_once = True
                await asyncio.sleep(_LLM_RETRY_BASE_S * (empty_retries + 1))
                continue
            return _finish({"text": "", "tool_calls": [],
                    "provider": reply.get("provider", ""),
                    "input_tokens": usage_in,
                    "output_tokens": usage_out,
                    "error": f"the model returned an empty reply "
                             f"{empty_retries + 1} times "
                             "(no text and no tool call)"})
        if not tool_calls:
            # No tool calls = the run is over. Replace the single turn's
            # usage with the loop's accumulated total so the node reports
            # everything it spent, not just this turn.
            reply = dict(reply)
            reply["input_tokens"] = usage_in
            reply["output_tokens"] = usage_out
            reply["context_chars"] = _context_chars(messages)
            return _finish(reply)

        # Prepare EVERY call before dispatching any: the repeat guard is cheap
        # and must judge the whole batch, and `prepare_fn` is the only place a
        # rewrite can still change what a tool receives (once a call is out
        # over the wire it is too late).
        prepared: list[dict] = []
        _recover = False
        for tc in tool_calls:
            _name = tc.get("name", "")
            _raw = tc.get("arguments") or {}
            key = _call_key(_name, _raw)
            repeats[key] = repeats.get(key, 0) + 1
            if repeats[key] > MAX_SAME_CALL_REPEATS and _name not in _REPEAT_EXEMPT_READS:
                # A repeat of a call that already SUCCEEDED is not a stall, it
                # is a model that has what it needs and is looping anyway.
                # Observed live: render_document returned 200 and stored the
                # PDF, the model called it again unchanged, and this guard
                # aborted the loop - so the node died holding a real document
                # and the receipt reported nothing. Break out and keep it.
                if key in succeeded:
                    if stall_recoveries < MAX_STALL_RECOVERIES and last_verify_failure:
                        # The edit already applied but the model is re-issuing
                        # it - usually because tests still fail and it is fixing
                        # the wrong thing. Redirect instead of aborting.
                        stall_recoveries += 1
                        repeats[key] = 0
                        _recover = True
                        break
                    # A repeat of a call that already SUCCEEDED is not a stall, it
                    # is a model that has what it needs and is looping anyway.
                    # Observed live: render_document returned 200 and stored the
                    # PDF, the model called it again unchanged, and this guard
                    # aborted the loop - so the node died holding a real document
                    # and the receipt reported nothing. Break out and keep it —
                    # but FIRST ask the caller for one final tool-less pass so the
                    # run ends with an answer rather than a cut-off reply.
                    return _finish({"text": "", "tool_calls": [],
                            "provider": last_reply.get("provider", ""),
                            "input_tokens": usage_in,
                            "output_tokens": usage_out,
                            "note": ("repeated an already-successful call; "
                                     "final pass requested"),
                            "needs_wrap_up": True,
                            "wrap_up_messages": list(messages)})
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

        # A bounded recovery: the model re-issued an already-successful call
        # while verification still fails. Skip dispatching the duplicate (and
        # its assistant turn, to keep tool_call/tool pairing valid) and hand the
        # model the failing output plus a directive to fix the root cause.
        if _recover:
            note = ("[harness] You re-issued a tool call that already succeeded, "
                    "but the task is not finished. ")
            if last_verify_failure:
                note += ("Your last test/check run still fails:\n"
                         + last_verify_failure + "\n")
            note += ("Change strategy: fix the ROOT CAUSE (for example tests "
                     "sharing one storage file, or actions that cannot be "
                     "serialized), not the same edit again. Give tests an "
                     "isolated storage path (tmp_path). Do not repeat the "
                     "identical call.")
            messages.append({"role": "user", "content": note})
            continue

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
            if p["name"] in ("run_command", "run_checks") and (
                    not ok or '"ok": false' in result_text
                    or "failed" in result_text.lower()):
                last_verify_failure = result_text[:1500]
            if ok:
                succeeded.add(p["skey"])
                if p["name"] in _WRITE_TOOLS:
                    # The tree moved, so a later identical check is a new
                    # measurement rather than the same call twice. The write's
                    # OWN counter is kept: create_file called three times with
                    # the same arguments is a loop, and clearing it here is what
                    # would let that run forever.
                    repeats = {k: v for k, v in repeats.items() if k == p["skey"]}
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
            # Between model calls, grow the token estimate by the appended text.
            ctx_tokens += len(body) // 4
            # Recompute after EVERY tool result. Trigger off REAL tokens when the
            # caller supplies the model window (max_context_tokens), else chars.
            if max_context_chars > 0 or max_context_tokens > 0:
                _ct = max_context_chars or max_context_tokens * 4
                _over = _context_chars(messages) > _ct or (
                    max_context_tokens > 0
                    and ctx_tokens > max_context_tokens * 4 // 5)
                if _over:
                    if compaction_fn is not None:
                        await _llm_compact(messages, compaction_fn)
                    _compact_context(messages, _ct)
                    while (max_context_chars > 0
                           and _context_chars(messages) > max_context_chars
                           and _elide_oldest_tool_output(messages)):
                        pass
                    ctx_tokens = _context_chars(messages) // 4
                if on_event is not None:
                    try:
                        on_event("context", {"chars": _context_chars(messages),
                                             "cap": max_context_chars,
                                             "tokens": ctx_tokens})
                    except Exception:
                        pass

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


async def _stream_wrap_up(*, messages, agent, session_id, provider_pin,
                          model=None, route_affinity=None,
                          max_tokens=2048, temperature=0.3,
                          on_event=None) -> dict:
    """One final, tool-less pass at the model — the honest end of a turn that
    was looping a call it had already completed.

    The model was being asked for one more thing when it had nothing left to do;
    the answer the user gets used to be the stall note. This issues the final
    pass with no tool schema at all, streams the deltas through the same
    `on_event("text", ...)` path the rest of the run uses, and returns the
    closing reply in the loop's own shape. If the caller passes `on_event`,
    the deltas go through it; either way the text is collected here.
    """
    parts: list[str] = []
    async for ev in LLM().stream_chat(
            messages=messages,
            tools=None,
            agent=agent, session=session_id,
            provider=provider_pin, model=model,
            route_affinity=route_affinity,
            max_tokens=max_tokens, temperature=temperature):
        kind = ev.get("type")
        if kind == "text":
            chunk = ev.get("text") or ""
            parts.append(chunk)
            if on_event is not None:
                try:
                    on_event("text", {"delta": chunk})
                except Exception:
                    pass
        elif kind == "done":
            return {
                "text": "".join(parts),
                "provider": ev.get("provider") or "",
                "input_tokens": ev.get("input_tokens") or 0,
                "output_tokens": ev.get("output_tokens")
                    or len("".join(parts)) // 4,
            }
    return {"text": "".join(parts), "provider": "",
            "input_tokens": 0,
            "output_tokens": len("".join(parts)) // 4}


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
                         max_context_tokens: int = 0,
                         max_tool_result_chars: int | None = None,
                         capture_transcript: bool = False,
                         stream: bool = False,
                         max_seconds: float = 0,
                         max_total_tokens: int = 0,
                         compaction_fn=None,
                         short_answers: bool = False) -> dict:
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
                    # Same reply shape as _chat, assembled from the stream. The
                    # gateway now forwards the provider's real usage on `done`;
                    # capture it so the loop's accounting is exact. Only fall
                    # back to a char estimate if a backend omits usage.
                    parts: list[str] = []
                    reasoning: list[str] = []
                    tool_calls: list[dict] = []
                    provider = ""
                    usage_in_hop = 0
                    usage_out_hop = 0
                    answered = ""
                    async for ev in LLM().stream_chat(
                            messages=messages, tools=tools_payload,
                            agent=agent, session=session_id,
                            provider=provider_pin, model=model,
                            max_tokens=max_tokens, temperature=temperature,
                            route_affinity=route_affinity):
                        kind = ev.get("type")
                        if kind == "text":
                            chunk = ev.get("text") or ""
                            parts.append(chunk)
                            if on_text and chunk:
                                on_text(chunk)
                        elif kind == "thinking":
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
                            answered = ev.get("model") or answered
                            usage_in_hop = int(ev.get("input_tokens") or 0)
                            usage_out_hop = int(ev.get("output_tokens") or 0)
                    text = "".join(parts)
                    return {"text": text, "tool_calls": tool_calls,
                            "provider": provider, "model": answered,
                            "reasoning": "".join(reasoning),
                            "input_tokens": usage_in_hop,
                            "output_tokens": usage_out_hop or len(text) // 4}

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
                    max_context_tokens=max_context_tokens,
                    max_tool_result_chars=max_tool_result_chars,
                    capture_transcript=capture_transcript,
                    max_seconds=max_seconds,
                    max_total_tokens=max_total_tokens,
                    compaction_fn=compaction_fn,
                    short_answers=short_answers)
                if loop_result and loop_result.get("needs_wrap_up"):
                    # The model repeated an already-successful call with no
                    # closing answer. Give it one final tool-less pass so the
                    # turn ends with a real answer rather than the stall
                    # note. Streams through the same event path as the rest of
                    # the turn; if it fails, the honest stall outcome stands.
                    instruction = {
                        "role": "user",
                        "content": ("Stop calling tools. You have everything you "
                                    "need: write your final answer now."),
                    }

                    try:
                        wrap = await _stream_wrap_up(
                            messages=list(loop_result.get("wrap_up_messages") or _messages)
                            + [instruction],
                            agent=agent, session_id=session_id,
                            provider_pin=provider_pin, model=model,
                            route_affinity=route_affinity,
                            max_tokens=max_tokens, temperature=temperature,
                            on_event=on_event)
                        loop_result = dict(loop_result)
                        loop_result["text"] = wrap.get("text") or ""
                        loop_result["provider"] = (wrap.get("provider")
                                                   or loop_result.get("provider", ""))
                        loop_result["input_tokens"] = (int(loop_result.get("input_tokens") or 0)
                                                       + int(wrap.get("input_tokens") or 0))
                        loop_result["output_tokens"] = (int(loop_result.get("output_tokens") or 0)
                                                        + int(wrap.get("output_tokens") or 0))
                        loop_result["wrap_up"] = True
                        loop_result.pop("needs_wrap_up", None)
                        loop_result.pop("wrap_up_messages", None)
                    except Exception as _e:
                        print(f"[mcp] wrap-up pass failed: "
                              f"{type(_e).__name__}: {_e}")
                        loop_result["wrap_up_failed"] = True
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
