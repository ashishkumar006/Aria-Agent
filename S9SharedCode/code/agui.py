"""AG-UI endpoint: the same chat turn, spoken in AG-UI's event vocabulary.

Why this exists
---------------
Aria already streams a typed, lifecycle-delimited event stream from
`/api/chat/simple/stream`. AG-UI is a standard vocabulary for exactly that
shape. Adopting it buys three things that matter and costs one endpoint:

  1. Our stream is validated against an external schema rather than our own
     opinion of it. That has already paid for itself in this codebase - every
     time we checked a shape against something external we found a real bug.
  2. Any AG-UI client can drive Aria. AG-UI is adopted by Google, Microsoft,
     Amazon and Oracle, and is the transport A2UI is designed to ride on.
  3. A capability document, so a client learns what we support before a run
     instead of discovering it by failing.

Why the event encoder is hand-rolled
------------------------------------
The official Python SDK (`ag-ui-protocol` on PyPI) describes "16 core event
types" while the AG-UI 1.0 spec defines 31 events across 8 categories - i.e.
the Python implementation lags the TypeScript one. Pinning to a lagging
dependency to emit the six events below would be a bad trade. So the subset we
emit is written out here, in full, and asserted by tests. That also keeps the
wire format auditable in one screen instead of hidden behind a version.

If the SDK reaches parity, this module is the only thing that changes: the
encoder swaps for `ag_ui.encoder.EventEncoder` and the event names are already
correct.

The subset
----------
Emitted, in this order:

    RUN_STARTED              once, first
    TEXT_MESSAGE_START       once, when the assistant begins
    TOOL_CALL_START          per tool invocation
    TOOL_CALL_ARGS           per tool invocation (streamed fragment)
    TOOL_CALL_END            per tool invocation
    TOOL_CALL_RESULT         per tool result
    ACTIVITY_SNAPSHOT        for "thinking…" / heartbeat status
    TEXT_MESSAGE_CONTENT     per answer chunk
    TEXT_MESSAGE_END         once, before the terminal event
    RUN_FINISHED | RUN_ERROR exactly one, last

Deliberately NOT emitted: STATE_SNAPSHOT / STATE_DELTA / MESSAGES_SNAPSHOT
(Aria's conversation history already lives behind
`GET /api/chat/threads/{id}`, and duplicating it into the event stream would
mean two sources of truth), REASONING_* (the chat path has no chain-of-thought
to expose), SUBAGENT_* (no delegated runs on this path), and RAW/CUSTOM (no
extension needs them yet - the activity snapshot covers status).

Invariants the spec is strict about, and which the tests assert:
  * a run starts with RUN_STARTED and ends with RUN_FINISHED or RUN_ERROR
  * TEXT_MESSAGE_CONTENT carries a non-empty `delta`
  * tool calls are linked by `toolCallId`
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator

# ── event constructors ───────────────────────────────────────────────────────
# Each returns a plain dict. `ts` is unix milliseconds, which the spec allows
# on any event; `rawEvent` is deliberately never set.


def _base(event_type: str) -> dict[str, Any]:
    return {"type": event_type, "timestamp": int(time.time() * 1000)}


def run_started(thread_id: str, run_id: str) -> dict[str, Any]:
    return {**_base("RUN_STARTED"), "threadId": thread_id, "runId": run_id}


def run_finished(thread_id: str, run_id: str,
                 result: Any = None) -> dict[str, Any]:
    out = {**_base("RUN_FINISHED"), "threadId": thread_id, "runId": run_id}
    # `result` is optional. Emitting it as null is noise, and the spec's
    # strict schema treats an absent field and an explicit null differently.
    if result is not None:
        out["result"] = result
    return out


def run_error(message: str, code: str = "AGENT_ERROR") -> dict[str, Any]:
    return {**_base("RUN_ERROR"), "message": str(message)[:2000], "code": code}


def step_started(step_name: str) -> dict[str, Any]:
    return {**_base("STEP_STARTED"), "stepName": step_name}


def step_finished(step_name: str) -> dict[str, Any]:
    """Pair for `step_started`.

    AG-UI pairs these - a client that renders progress from STEP_STARTED has
    nothing to remove the step on, so a started-without-finished stream leaves
    a spinner running forever. The encoder previously had no constructor for
    it at all, and the one emit site sent STEP_STARTED and never closed it.
    """
    return {**_base("STEP_FINISHED"), "stepName": step_name}


def text_start(message_id: str, role: str = "assistant") -> dict[str, Any]:
    return {**_base("TEXT_MESSAGE_START"), "messageId": message_id, "role": role}


def text_content(message_id: str, delta: str) -> dict[str, Any]:
    # The spec requires a non-empty delta. Emitting an empty one is a
    # validation error, so the caller must filter - `content()` does.
    return {**_base("TEXT_MESSAGE_CONTENT"), "messageId": message_id,
            "delta": delta}


def text_end(message_id: str) -> dict[str, Any]:
    return {**_base("TEXT_MESSAGE_END"), "messageId": message_id}


def tool_call_start(call_id: str, name: str, parent: str | None = None) -> dict[str, Any]:
    out = {**_base("TOOL_CALL_START"), "toolCallId": call_id,
           "toolCallName": name, "parentMessageId": parent or ""}
    return out


def tool_call_args(call_id: str, delta: str) -> dict[str, Any]:
    return {**_base("TOOL_CALL_ARGS"), "toolCallId": call_id, "delta": delta}


def tool_call_end(call_id: str, name: str) -> dict[str, Any]:
    return {**_base("TOOL_CALL_END"), "toolCallId": call_id, "toolCallName": name}


def tool_call_result(call_id: str, message_id: str, content: str,
                     role: str = "tool") -> dict[str, Any]:
    return {**_base("TOOL_CALL_RESULT"), "toolCallId": call_id,
            "messageId": message_id, "role": role, "content": content}


def activity_snapshot(message_id: str, text: str,
                      activity_type: str = "status") -> dict[str, Any]:
    return {**_base("ACTIVITY_SNAPSHOT"), "messageId": message_id,
            "activityType": activity_type, "content": {"text": text}}


# ── wire format ──────────────────────────────────────────────────────────────

def sse(event: dict[str, Any]) -> str:
    """One AG-UI frame.

    `EventEncoder` emits `data: {json}\\n\\n`. Kept byte-identical so a future
    swap to the SDK changes nothing observable to a client.
    """
    return "data: " + json.dumps(event, separators=(",", ":")) + "\n\n"


def content(event: dict[str, Any]) -> str | None:
    """Serialise a text delta, or None when there is nothing to send.

    Exists so the empty-delta rule is enforced in one place. The chunker can
    legitimately produce an empty final chunk, and emitting it would make the
    whole stream fail a client's schema validation - losing every event, not
    just the empty one.
    """
    if event.get("type") != "TEXT_MESSAGE_CONTENT":
        return sse(event)
    delta = event.get("delta")
    if not delta:
        return None
    return sse(event)


def new_ids(conversation_id: str | None) -> tuple[str, str, str]:
    """(threadId, runId, messageId).

    A supplied conversation id becomes the thread id so an AG-UI client and
    the console address the same conversation; a missing one is generated
    rather than defaulted to a constant, or every anonymous run would share
    one thread and their histories would interleave.
    """
    thread = conversation_id or f"agui-{uuid.uuid4().hex[:12]}"
    return thread, uuid.uuid4().hex, uuid.uuid4().hex


# ── capabilities ─────────────────────────────────────────────────────────────

def capabilities(*, streaming: bool = True, tools: list[str] | None = None,
                 message_persistence: bool = True) -> dict[str, Any]:
    """What this agent supports, for a client to read before a run.

    Mirrors the shape of AG-UI's capability document. Only flags that are TRUE
    are declared: the spec treats an omitted field as "not declared", and
    claiming a transport we do not implement (WebSocket, resumable streams)
    would send a client down a path that fails mid-run.

    `http_binary` is false and stays false until a protobuf encoder exists.
    """
    caps: dict[str, Any] = {
        "identity": {"agentName": "Aria", "agentVersion": "9"},
        "transport": {
            "streaming": streaming,
            "websocket": False,
            "http_binary": False,
            "resumable": False,
            "pushNotifications": False,
        },
        "tools": {
            "toolCalling": bool(tools),
            "tools": sorted(tools or []),
        },
        "output": {"streaming": True, "contextWindowSize": None},
        "state": {
            "sharedState": False,
            "agentState": False,
            "messagePersistence": message_persistence,
        },
        "multiAgent": {"delegation": False, "subAgents": False},
        "reasoning": {"reasoning": False},
        "multimodal": {"input": False, "output": False},
        "execution": {"codeExecution": False},
        "humanInTheLoop": {"approvalRequests": False},
    }
    return caps


# ── request parsing ──────────────────────────────────────────────────────────

MAX_INPUT_CHARS = 100_000


class AguiRequestError(ValueError):
    """A malformed AG-UI RunAgentInput. Carries the HTTP status to use."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def parse_request(body: Any) -> dict[str, Any]:
    """Validate a RunAgentInput-shaped body.

    Deliberately lenient about which conversation-shaped keys are accepted,
    because two clients want in: the console (which speaks
    `{query, conversation_id}`) and any AG-UI client (which sends
    `{threadId, runId, messages}`). Being strict about the spec but
    incompatible with our own frontend would make the endpoint dead on
    arrival.

    What is NOT lenient: the message list must be well-formed, because a
    malformed one would otherwise reach the model and fail there, much later
    and much less legibly.
    """
    if not isinstance(body, dict):
        raise AguiRequestError("body must be a JSON object")

    thread = body.get("threadId") or body.get("thread_id") or \
        body.get("conversation_id") or body.get("conversationId")
    # A non-string id (a number from LLM-generated JSON) used to
    # raise AttributeError on `.strip()` — a bare 500. Coerce
    # scalars, refuse everything else.
    if thread is not None and not isinstance(thread, str):
        if isinstance(thread, (int, float)) and not isinstance(thread, bool):
            thread = str(thread)
        else:
            raise AguiRequestError("thread_id must be a string")
    thread = (thread or "").strip() or None

    run_id = body.get("runId") or body.get("run_id") or ""
    if not isinstance(run_id, str):
        if isinstance(run_id, (int, float)) and not isinstance(run_id, bool):
            run_id = str(run_id)
        else:
            raise AguiRequestError("run_id must be a string")
    run_id = run_id.strip() or None

    raw_messages = body.get("messages")
    raw_query = body.get("query") or body.get("q") or ""
    if not isinstance(raw_query, str):
        raw_query = str(raw_query)
    explicit_query = raw_query.strip()
    query = explicit_query

    messages: list[dict] = []
    if isinstance(raw_messages, list):
        for i, m in enumerate(raw_messages):
            if not isinstance(m, dict):
                raise AguiRequestError(f"messages[{i}] must be an object")
            role = (m.get("role") or "").strip()
            if role not in ("user", "assistant", "system", "tool"):
                raise AguiRequestError(
                    f"messages[{i}].role must be user/assistant/system/tool, "
                    f"got {role!r}")
            content_val = m.get("content")
            # content may be a string or a list of content parts in the spec.
            # Only the string form is carried through today; a structured part
            # list is rejected loudly rather than silently dropped, because
            # silently dropping it would answer a question the user did not ask.
            if isinstance(content_val, list):
                text = "".join(
                    str(p.get("text", "")) for p in content_val
                    if isinstance(p, dict) and p.get("type") in (None, "text"))
                if not text.strip():
                    raise AguiRequestError(
                        f"messages[{i}] has non-text content parts, which this "
                        f"endpoint does not support yet")
            else:
                text = "" if content_val is None else str(content_val)
            messages.append({"role": role, "content": text})
            if role == "user" and not explicit_query and text.strip():
                # No explicit top-level query: the turn to
                # answer is the transcript's LAST user message.
                # Taking the first made a multi-turn transcript
                # answer its opening message. An EMPTY last
                # user message must not clear an earlier one —
                # the last NON-EMPTY user message is the ask.
                query = text.strip()

    if not query:
        raise AguiRequestError("query required (or a user message)")
    if len(query) > MAX_INPUT_CHARS:
        raise AguiRequestError(
            f"query too long (max {MAX_INPUT_CHARS} chars)", 413)

    tools = body.get("tools")
    tool_names: list[str] = []
    if isinstance(tools, list):
        for t in tools:
            name = None
            if isinstance(t, dict):
                # AG-UI nests the callable under `function`, mirroring OpenAI.
                fn = t.get("function")
                name = (fn or {}).get("name") if isinstance(fn, dict) else t.get("name")
            elif isinstance(t, str):
                name = t
            if name:
                tool_names.append(str(name))

    state = body.get("state")
    if state is not None and not isinstance(state, dict):
        raise AguiRequestError("state must be an object")

    return {
        "thread_id": thread,
        "run_id": run_id,
        "query": query,
        "messages": messages,
        "tool_names": tool_names,
        "state": state or {},
        # True when the query was derived from the caller's own
        # transcript rather than sent as a top-level field. The
        # endpoint must not append it to the history again, or
        # the turn reaches the model twice ("hello", "hello").
        "query_from_messages": not explicit_query and bool(messages),
    }


# ── streaming ────────────────────────────────────────────────────────────────

async def event_stream(producer: AsyncIterator[dict[str, Any]]
                      ) -> AsyncIterator[str]:
    """Serialise an event iterator to SSE frames, honouring the empty-delta rule.

    Any exception from the producer becomes a RUN_ERROR followed by nothing
    else. A stream that dies without a terminal event leaves the client waiting
    forever and, worse, renders a partial answer as a finished one.
    """
    try:
        async for event in producer:
            frame = content(event)
            if frame:
                yield frame
    except asyncio.CancelledError:
        # Client went away. Do not manufacture a terminal event for a stream
        # nobody is reading; just stop.
        raise
    except Exception as e:  # noqa: BLE001 - this is the boundary
        # Log the traceback, send a clean message. The client gets the type and
        # text and nothing about our internals; the operator gets the stack.
        # Without this, a boundary like this is where bugs go to hide.
        import traceback
        print(f"[agui] stream failed: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        yield sse(run_error(f"{type(e).__name__}: {e}"))
