# New functional defects in Aria (stateful flows)

This is a bug-hunter pass over the live two-service system (agent `:8500`, gateway `:8109`), looking for
off-by-one, wrong-operator, unreachable-branch, state-invariant and unhandled-edge-case defects with a
trigger I actually executed. It found eleven new defects that are not on the batch's already-known list,
concentrated in three places: the A2UI validator's advertised safety limits (three of them do not hold),
`/api/agui`'s AG-UI event contract, and conversation-preference state. Two of the eleven
(A2UI depth-limit blindness, A2UI text-budget blindness) I was able to execute end-to-end even though
`:8500` died mid-pass, by driving the pure validator module directly; the rest were confirmed against
the live service before the outage or are static analysis. The single most consequential finding is that
**`use_documents: false` is silently ignored on `/api/chat`** — the DAG path the Research page actually
uses — so the per-conversation document toggle is a no-op on exactly the surface where documents matter.
`:8500` and `:8109` both went down at ~05:30 local during this pass (see `_BRIEFING.md` rule 6 and the
sibling reports on load-induced saturation); I did not restart either service.

## Scope & method

Auth helper: the briefing's `aria_probe` from `%TEMP%\kilo`, wrapped in a retry shim (`%TEMP%\kilo\H.py`,
deleted after the run) because the agent went unresponsive twice. Scratch scripts lived in
`%TEMP%\kilo\` only. No repo file was read except source, and the only file written is this report.

Live requests executed against `:8500`, in order:

```
GET  /api/health                                  -> 200 {"agent":"ready","gateway_up":true,"spa_built":true}
GET  /api/documents                               -> 401 {"error":"missing or invalid x-gateway-token header","status_code":401}   (18575 ms)
GET  /api/documents/zznope                        -> 401 (same)
POST /api/documents/search {"query":"zzperfbh1"}  -> 401 (same)   (16474 ms)
GET  /api/tts/voices                              -> 200 (7 voices)  (6980 ms)
GET  /api/cost                                    -> 200 {"rows":[],"totals":{...0}}
GET  /api/cost/by_skill                           -> 200 (populated)
GET  /api/memory?limit=2                          -> 200
GET  /api/mcp/stats /api/config /api/audit?limit=2 -> 200

POST /api/agui {"query":"hi","messages":"notalist"}      -> 200, RUN_STARTED emitted, full run executed
POST /api/agui {"query":"hi","messages":{"role":"user"}}  -> 200, RUN_STARTED emitted, full run executed
POST /api/agui {"query":"hi","runId":"  "}               -> 200, RUN_STARTED emitted, full run executed
POST /api/agui {"query":"hi","messages":[{"role":"wizard","content":"x"}]} -> 400 {"error":"messages[0].role must be user/assistant/system/tool, got 'wizard'"}
POST /api/agui {"query":"hi","messages":["hello"]}        -> 400 {"error":"messages[0] must be an object"}
POST /api/agui {"threadId":"zzperfbh1"}                  -> 400 {"error":"query required (or a user message)"}
POST /api/agui {"query":"hi","state":"nope"}              -> 400 {"error":"state must be an object"}
POST /api/agui {"query":"<100001 chars>"}                -> 413 {"error":"query too long (max 100000 chars)"}
POST /api/agui {"query":"hi","messages":[{"role":"user","content":[{"type":"image","url":"x"}]}]} -> 400 {"error":"messages[0] has non-text content parts, which this endpoint does not support yet"}
POST /api/agui ["not","an","object"]                     -> 400 {"error":"body must be a JSON object"}
POST /api/agui {"query":"hi","threadId":12345}           -> 500 Internal Server Error   <-- FINDING 2

GET  /api/chat/threads/zzperfbh1/prefs                  -> 200 {"prefs":{"use_documents":true}}
GET  /api/chat/threads/zzperfbh1                        -> 404 {"error":"unknown thread"}
POST /api/chat/threads/zzperfbh1/prefs {"research":true} -> 400 {"error":"use_documents must be true or false"}   <-- FINDING 4
POST /api/chat/threads/zzperfbh1/prefs {"use_documents":false} -> 200 {"prefs":{"use_documents":false}}
GET  /api/chat/threads/zzperfbh1/prefs                  -> 200 {"prefs":{"use_documents":false}}
GET  /api/chat/threads?limit=100                        -> 200, 61 threads, contains
        {"conversation_id":"zzperfbh1","title":"(untitled)","updated":0,"turns":0}      <-- FINDING 5
DELETE /api/chat/threads/zzperfbh1                      -> 200 {"status":"ok"}   (my own entry, cleaned up)
```

Three of the AG-UI probes above were accepted and ran to completion, so **three of my six permitted
`/api/chat`-equivalent runs were consumed by the malformed-request matrix**. I stopped issuing AG-UI runs
after that.

A2UI validator behaviour was established by importing the pure module and calling the same entry point
the route calls (`agent_server.py:2522` → `a2ui_catalog.validate_surface(body)`), so accept/reject
transfers 1:1 to `POST /api/a2ui/validate`. Script: `%TEMP%\kilo\p7.py` (deleted). Output:

```
depth 60 via 2nd child (max 12)          ACCEPTED components=121 textchars=2168 bytes=7318
depth 60 via 1st child (control)         REJECTED nesting deeper than 12 levels near 'root'
literal text 20x3999 = 79980 chars       REJECTED surface text exceeds 60000 chars in total
DataList.rows 20x6000 chars in lists     ACCEPTED components=1 textchars=15 bytes=120651
FilterChips.options 20x6000 chars        ACCEPTED components=1 textchars=29 bytes=120642
Text bound via {path} to 60000 chars     ACCEPTED components=1 textchars=60019 bytes=60124
MAX_ACTIONS_PER_COMPONENT = 1
65 children on root (max 64)             REJECTED root has 65 children; the limit is 64
Row.gap = 'not-an-enum'                  ACCEPTED
Progress.value = 'banana'                ACCEPTED
Link.target = 'https://evil.example'     ACCEPTED
catalog limits advertised: {'maxComponents': 400, 'maxDepth': 12, 'maxNodesPerParent': 64, 'maxTextLen': 4000, 'maxTotalText': 60000}
limits declared in code : {... 'maxActionsPerComponent': 1}
catalog components: 17
```

Also measured: `GET /api/tts/voices` 6980 ms and `GET /api/documents` 18575 ms wall clock, both against
the same `_gw_json` helper — the cost is the `localhost` name-resolution penalty documented at
`gateway.py:26-31` and re-introduced by `agent_server.py:3531`. Per the coordinator's mandatory
methodology I cannot attribute an absolute figure without a same-minute zero-load baseline, and
`:8500` p50 was already 2.1 s / p95 10.5 s under the batch's load, so I report this as a delta-relative
observation only, not a latency finding.

## Findings

### 1. `use_documents: false` is ignored by `/api/chat` — the toggle is a no-op on the Research page (P1)

**Evidence.** `_conversation_doc_ids(conversation_id)` is the only function that reads the stored
preference (`agent_server.py:2700-2708`), and a grep of every call site returns exactly three, all on the
lightweight paths:

```
agent_server.py:2315, 2390   /api/agui
agent_server.py:4204, 4240   /api/chat/simple
agent_server.py:4501, 4531   /api/chat/simple/stream
```

`/api/chat` does not appear. The DAG path resolves documents itself, in `flow.py`:

```python
# flow.py:356-363
doc_ids = None
try:
    from agent_server import _gw_enabled_doc_ids as _enabled
    doc_ids = _enabled()
```

`_gw_enabled_doc_ids()` (`agent_server.py:2717-2722`) takes no `conversation_id` and returns every enabled
document in the gateway registry. The Research page uses this path — `console-frontend/src/views/Research.tsx:210`
calls `api.chat(...)`, and `api.ts:658` `fetch('/api/chat', …)`.

The prefs route's own docstring asserts the opposite. `agent_server.py:2685-2698`:

> "Which documents a conversation may draw on… **Docs-off is answered from the stored preference alone**
> and never consults the registry, so it holds even when the gateway is unreachable."

That guarantee holds only for the three callers that pass a `conversation_id`.

**Repro.**
1. `POST /api/chat/threads/zzperfbh1/prefs {"use_documents": false}` → `200 {"prefs":{"use_documents":false}}`.
2. `POST /api/chat {"query":"…", "conversation_id":"zzperfbh1", "research":true}` → the run starts.
3. `flow.py:359` calls `_gw_enabled_doc_ids()` with no id; the run's memory read
   (`flow.py:364-365 memory_svc.read(..., doc_ids=doc_ids)`) sees every enabled document.
4. `POST /api/chat/threads/zzperfbh1/prefs {"use_documents": false}` again → unchanged, no warning.

**Root cause.** `flow.py:356-363` bypasses the per-conversation filter; `_conversation_doc_ids` is never
consulted on the DAG path.

**Impact.** A user who turns documents off in a conversation still has their documents read, chunked and
injected into that run's memory drawer. It fails **open** where the design comment promises it fails
closed — the opposite of the stated invariant. Silent: the console renders the toggle faithfully, so the UI
says "documents off" while retrieval continues.

**Severity.** P1 — a privacy-relevant access-control toggle that reports success and does nothing, on the
primary research surface.

**Cheapest correct fix.** Thread the conversation id into the Executor (`Executor.run(..., conversation_id=…)`),
and in `flow.py:356-363` call `agent_server._conversation_doc_ids(conversation_id)` instead of
`_gw_enabled_doc_ids()`, falling back to `set()` when the id is unknown. ~5 lines; no schema change.

---

### 2. A2UI depth limit only counts the first child, so `maxDepth: 12` is unenforced (P1)

**Evidence (executed).** 121 components nested 60 levels deep were **accepted**; the identical 60-deep
chain threaded through the first child slot was **rejected**. Same validator, same call:

```
depth 60 via 2nd child (max 12)   ACCEPTED components=121
depth 60 via 1st child (control)  REJECTED nesting deeper than 12 levels near 'root'
```

**Repro.**
1. Build `root` with `children: ["leaf", "n1"]`; each `nK` has `children: ["leafK", "nK+1"]`; 60 levels.
2. `POST /api/a2ui/validate` with that payload → `200 {"ok": true}` (or, offline,
   `a2ui_catalog.validate_surface(payload)` returns the surface).
3. Move each chain link into `children[0]` → `422 {"ok": false, "error": "nesting deeper than 12 levels…"}`.

**Root cause.** `a2ui_catalog.py:389-390` walks only the first child:

```python
while node["_children"]:
    node = by_id.get(node["_children"][0])
```

`_check_depth` is called at `a2ui_catalog.py:364`. Any node whose *second-or-later* child is deep is
invisible to it. `MAX_DEPTH = 12` (`a2ui_catalog.py:49`) and the identical blindness applies to the cycle
check at `a2ui_catalog.py:394-395`.

**Impact.** `MAX_DEPTH` is advertised to every client in `catalog_document()["limits"]["maxDepth"]`
(`a2ui_catalog.py:193`) as a DoS mitigation — one of the four the module docstring
(`a2ui_catalog.py:29-33`) names against "DoS via pathological nesting". A client that trusts the
advertised limit and renders recursively will blow its own stack; a reviewer reading the returned surface
has no reason to suspect the structure is 5× over the stated bound.

**Severity.** P1 — a documented security limit that does not hold, on an untrusted-input boundary.

**Cheapest correct fix.** Replace the first-child walk with a full iterative DFS over *all* children
(push every kid, track `(id, depth)`, keep the existing `seen` set for cycles). ~10 lines, same
recursion-free shape the current code deliberately uses.

---

### 3. A2UI text budget counts only literal strings, so `maxTextLen`/`maxTotalText` are bypassed by list props and bindings (P1)

**Evidence (executed).**

```
literal text 20x3999 = 79980 chars    REJECTED surface text exceeds 60000 chars in total   (control)
DataList.rows 20x6000 chars in lists  ACCEPTED  bytes=120651   (120,000 chars of text)
FilterChips.options 20x6000 chars     ACCEPTED  bytes=120642   (120,000 chars of text)
Text bound via {path} to 60000 chars  ACCEPTED  textchars=60019
```

So 120,000 characters of agent-supplied text pass a validator whose advertised `maxTotalText` is 60,000,
and the last case shows `render_to_text` — described at `a2ui_catalog.py:404-408` as the thing that makes
a surface reviewable — itself emitting 60,019 characters.

**Repro.**
1. `{"surfaceId":"zzperf1","components":[{"id":"root","component":"DataList","rows":[{label,value} × 20 with 6000-char strings]}]}`
2. `POST /api/a2ui/validate` → accepted (422 only when the whole payload exceeds 256 KB,
   `a2ui_catalog.py:465-467`).
3. Control: the same characters spread over 20 `Text` components are rejected.

**Root cause.** `a2ui_catalog.py:246-252`:

```python
def _as_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        return None  # a binding, not literal text
    return None
```

The budget loop at `a2ui_catalog.py:309-322` does `text = _as_text(value)` and `continue`s on `None`, so
every non-literal-string property is uncounted. Three catalog properties are list-valued —
`StatusRow.items` (`a2ui_catalog.py:119`), `DataList.rows` (`:146`), `FilterChips.options` (`:163`) — and
every `text|literal` property can be swapped for a `{"path": …}` binding, which returns `None` by design.
`data` itself is capped only by the 256 KB payload guard.

**Impact.** Two of the five advertised limits (`maxTextLen`, `maxTotalText`) do not bound agent-supplied
text. The generated surface is returned to the client and rendered; a 120 KB text payload is a rendering
and memory cost the client was told could not happen, and the "reviewable" plain-text form is unbounded too.

**Severity.** P1 — same class as #2: a documented untrusted-input limit that does not hold.

**Cheapest correct fix.** In the budget loop, add a case for `list` (and `dict`) values that recurses into
elements and counts their string leaves against `MAX_TEXT_LEN`/`MAX_TOTAL_TEXT`, plus one pass over
`payload["data"]` charging it to the same budget.

---

### 4. A2UI enum-typed properties are never enforced, so `Link.target: "https://evil.example"` is accepted (P1)

**Evidence (executed).**

```
Row.gap = 'not-an-enum'                  ACCEPTED   (declared "enum:tight|normal|loose")
Progress.value = 'banana'                ACCEPTED   (declared "number")
Link.target = 'https://evil.example'     ACCEPTED   (declared "enum:console")
```

The catalog entry for `Link` states the guarantee in prose (`a2ui_catalog.py:167`):

> `"doc": "Navigates within the console. External URLs are rejected."`

**Repro.**
1. `POST /api/a2ui/validate {"surfaceId":"zzperf1","components":[{"id":"root","component":"Link","label":"click","target":"https://evil.example"}]}`
2. → `200 {"ok": true, "surface": {… "target": "https://evil.example"}}`.
3. Control: `{"component":"Link","href":"https://evil.example"}` → rejected, `href` is in
   `FORBIDDEN_PROPS` (`a2ui_catalog.py:176-179`).

**Root cause.** `a2ui_catalog.py:296-306` validates the property *name* against `spec["props"]` but never
interprets the type string. `"enum:console"` is a display convention carried through to the wire by
`catalog_document()` (`a2ui_catalog.py:203 dict(spec["props"])`), not a constraint. The forbidden-name
list is what stops `href`/`src`/`url`, and `Link` reaches navigation through `target`, which is not on it.

**Impact.** The prose security claim in the catalog is false and the catalogue is the only contract the
client gets — `catalog_document()` emits `"target": "enum:console"` as a bare string with no machine-readable
schema. A client that trusts `enum:console` renders an agent-supplied absolute URL as a navigable target:
phishing by spoofed control, which is the first attack `a2ui_catalog.py:29-33` claims to mitigate.

**Severity.** P1 — untrusted-input boundary, and the mitigation the code claims is absent.

**Cheapest correct fix.** Parse the existing type strings once at import (`"enum:a|b|c"` → set,
`"number"` → numeric check, `"text|literal"` → str-or-binding) and enforce them in the same loop that
checks property names. No payload-shape change; the type language is already in the data.

---

### 5. `POST /api/chat/threads/{id}/prefs` creates a phantom thread that is first to be evicted (P2)

**Evidence (executed).**

```
GET  /api/chat/threads/zzperfbh1                        -> 404 {"error":"unknown thread"}
POST /api/chat/threads/zzperfbh1/prefs {"use_documents":false} -> 200
GET  /api/chat/threads?limit=100 -> contains
      {"conversation_id":"zzperfbh1","title":"(untitled)","updated":0,"turns":0}
```

**Repro.** As above: `GET` the thread (404), `POST` prefs, then `GET /api/chat/threads` — the id is now
listed. `DELETE /api/chat/threads/zzperfbh1` removes it again.

**Root cause.** `agent_server.py:2667-2678 _chat_prefs` writes the bare dict it read:

```python
thread = data.get(conversation_id) or {}
prefs = thread.get("prefs") or {}
if use_documents is not None:
    prefs["use_documents"] = bool(use_documents)
    thread["prefs"] = prefs
    data[conversation_id] = thread
```

No `title`, and — decisively — no `updated`. The listing sorts by `updated`
(`agent_server.py:4102 items.sort(key=lambda x: x["updated"], reverse=True)`), and every prune picks
victims by the same key, oldest first: `agent_server.py:4277-4280`, `:4610-4613`, `:2466-2470`. A
prefs-only entry has `updated == 0`, so it sorts last *and* is the first thing deleted once
`_CHAT_MAX_THREADS` (50, `agent_server.py:4002`) is exceeded. The store already held **61** threads at
probe time, i.e. over the cap, so the prune was already armed.

**Impact.** Turning documents off for a conversation the user has not yet typed into silently inserts an
"(untitled)" row into the sidebar, and the very next turn from any conversation deletes it. The preference
and the thread entry are destroyed together, so the toggle quietly reverts to on. A ghost row and a
self-erasing setting, both with `200 OK`.

**Severity.** P2.

**Cheapest correct fix.** In `_chat_prefs`, when creating a new entry, seed
`{"title": query-less placeholder, "updated": time.time(), "messages": []}` — or store prefs in a separate
map keyed by conversation id so the preference store and the thread store stop sharing one dict.

---

### 6. `/api/chat/simple/stream` claims an idempotency key it never releases (P2)

**Evidence (static analysis, not executed — `:8500` was down).** `_inflight_run_release` is called at
exactly three places (`agent_server.py:4399`, `:4930`, `:4988`). Two of them are inside `_stream_run`,
which serves `/api/chat` and `/api/templates/{name}/run`. The third is inside `_dedup_stream`.
`chat_simple_stream` claims at `agent_server.py:4462` and never releases on any path — success, error, or
disconnect. Its `finally` (`:4576-4584`) records cost and releases nothing.

**Repro.**
1. `POST /api/chat/simple/stream {"query":"Q", "research": true}` → completes normally, `done` frame
   delivered, thread persisted.
2. `POST /api/chat/simple/stream {"query":"Q", "research": true}` again, within 600 s
   (`_IDEM_TTL_S = 600.0`, `agent_server.py:4333`).
3. → `_dedup_stream` (`:4436`) emits `started{deduplicated:true}` then
   `error{text:"an identical run is already in progress…"}`. No answer is ever produced.

**Root cause.** `agent_server.py:4462 _inflight_run_claim(idem_key, conversation_id)` with no matching
release, versus the `/api/chat` pair at `:5038` / `:5044`.

**Impact.** On the streaming chat endpoint, one research-flagged question poisons that exact question for
ten minutes: every repeat is swallowed with an error frame and no answer. The idempotency guard, added
specifically to stop double-billing, becomes a ten-minute denial of the endpoint. `research: true` is what
`console-frontend/src/views/Research.tsx:211` sends, so this is reachable from the product.

**Severity.** P2 (P1 if any client uses `idempotency_key` on this path — the console currently does not).

**Cheapest correct fix.** Wrap `gen()`'s tail in `try/finally: _inflight_run_release(idem_key)`, exactly
as `_stream_run` does at `:4988`, including the disconnect branch.

---

### 7. `POST /api/config/tools` does not type-check `enabled`, so `{"enabled":"false"}` disables a tool (P2)

**Evidence (static analysis, not executed).** `agent_server.py:1724-1739`:

```python
name = (body.get("tool") or "").strip()
enabled = body.get("enabled", True)
...
if enabled:
    disabled.discard(name)
else:
    disabled.add(name)
```

No `isinstance(enabled, bool)` check. Contrast the two sibling routes that do validate:
`/api/documents/{doc_id}/enabled` (`agent_server.py:2041`) and the chat prefs route
(`agent_server.py:2657`). The neighbouring `/api/apps/flags` goes further and coerces `"false"` → `False`
via `flags._coerce` (`flags.py:127-132`).

**Repro.**
1. `POST /api/config/tools {"tool":"web_search","enabled":"false"}` → `200`, and `"false"` is a
   non-empty string, so `if enabled:` is **true** → `disabled.discard("web_search")`. The response body
   echoes `{"enabled": "false"}` while the tool stays enabled.
2. `POST /api/config/tools {"tool":"web_search","enabled":0}` → `0` is falsy → the tool is **disabled**,
   and the response echoes `"enabled": 0`.
3. `POST /api/config/tools {"tool":123,"enabled":false}` → `(123 or "").strip()` raises `AttributeError`
   outside any `try` → **HTTP 500** `text/plain`, the exact class of bug `_conv_id`
   (`agent_server.py:3549-3567`) was written to eliminate.

Direction of failure is not even consistent: `"false"` enables, `0` disables.

**Impact.** The tool kill-switch can be inverted by a mistyped client, and a non-string `tool` produces an
unhandled 500 instead of the 400 the route's own docstring promises ("Unknown names 400",
`agent_server.py:1718`). `skills._disabled_tools()` is live-read by `tool_payload`
(`skills.py:1029-1049`), so a wrong write changes model-visible capability with no restart.

**Severity.** P2.

**Cheapest correct fix.** `if not isinstance(enabled, bool): return 400`, and route `name` through the
existing `_conv_id`-style str/number coercion.

---

### 8. `/api/agui` accepts a non-list `messages` and silently discards it (P2)

**Evidence (executed).**

```
POST /api/agui {"query":"hi","messages":"notalist"}     -> 200, RUN_STARTED, run executed
POST /api/agui {"query":"hi","messages":{"role":"user"}} -> 200, RUN_STARTED, run executed
POST /api/agui {"query":"hi","messages":[{"role":"wizard","content":"x"}]} -> 400 (contrast)
```

**Repro.** As above. A client sending AG-UI's `messages` field with the wrong JSON type gets a
**successful, billed, persisted run** in which its transcript played no part; the run is then appended to
the thread at `agent_server.py:2458-2461` as if it had been answered.

**Root cause.** `agui.py:258` — `if isinstance(raw_messages, list):` with no `else`. The module docstring
(`agui.py:240-243`) states the opposite intent:

> "What is NOT lenient: the message list must be well-formed, because a malformed one would otherwise
> reach the model and fail there, much later and much less legibly."

Element-level validation *is* strict (`:260-266`); only the top-level type is unchecked, which is the one
check that decides whether the list is examined at all.

**Impact.** A whole class of client bug (double-encoded `messages`, `null`, a bare string) is converted
from a clear 400 into a wrong answer that looks fine. The caller has no way to tell from the response
that its history was dropped.

**Severity.** P2.

**Cheapest correct fix.** `elif raw_messages is not None: raise AguiRequestError("messages must be an array")`
after the `isinstance` check at `agui.py:258`.

---

### 9. `POST /api/agui` with a numeric `threadId` returns HTTP 500 (P2)

**Evidence (executed).**

```
POST /api/agui {"query":"hi","threadId":12345} -> 500 Internal Server Error
```

**Repro.** As above; the response is `text/plain`, no JSON, no detail.

**Root cause.** `agui.py:248-250`:

```python
thread = body.get("threadId") or body.get("thread_id") or \
    body.get("conversation_id") or body.get("conversationId")
thread = (thread or "").strip() or None
```

`.strip()` on an `int`. The identical hazard was found and fixed elsewhere — `agent_server.py:3549-3561`
`_conv_id` exists specifically because `{"conversation_id": 12345}` used to raise `AttributeError` and
return an unparseable 500 — but AG-UI's `parse_request` was never given the same treatment. A JS client
storing ids as numbers hits this immediately, which is the scenario `_conv_id`'s docstring calls out.

**Severity.** P2.

**Cheapest fix.** Reuse `str()`-coercion for int/float and drop everything else, i.e. call the existing
`_conv_id`-style guard on each of the four accepted keys.

---

### 10. AG-UI tool-call correlation uses a single slot, so a second tool overwrites the first's id (P2)

**Evidence (static analysis, not executed — the tool-calling path fails before this in this build).**
`agent_server.py:2336`:

```python
open_call: dict[str, str] = {}
```

`on_tool_event` (`:2342-2368`) writes `open_call["id"] = call_id` on every `tool_call` (`:2359-2360`) and
`open_call.clear()` on every `tool_result` (`:2366`). Events are buffered in a FIFO `pending` list
(`:2329`, `:2339-2340`) and flushed in order (`:2410-2411`, `:2422-2423`), so a run with two tools emits
`TOOL_CALL_START(call-1)`, `TOOL_CALL_START(call-2)`, `TOOL_CALL_RESULT(…)`, `TOOL_CALL_RESULT(…)` — and
because `open_call` holds one id, the first result is stamped with **`call-2`** and the second finds an
empty dict and is stamped **`"call-0"`** (`agent_server.py:2364 open_call.get("id", "call-0")`). `call-0`
was never started, violating the linking invariant the module docstring asserts at `agui.py:55`
("tool calls are linked by `toolCallId`").

A single-slot variable cannot represent two open calls; it needs a FIFO.

**Severity.** P2 — the AG-UI contract's central promise, broken for any multi-tool turn.

**Cheapest correct fix.** Make `open_call` a `collections.deque`; `append` on `tool_call`, `popleft` on
`tool_result`, and keep `"call-0"` only for a result with no preceding start.

---

### 11. `/api/agui` discards a client-supplied `runId` and answers with a different one (P3)

**Evidence (static analysis, not executed).** `agui.py:252` reads `runId`/`run_id` into `parsed["run_id"]`,
then `new_ids()` (`agui.py:165-174`) mints a fresh `uuid4().hex`, and `agent_server.py:2276-2277` overwrites
the parsed value:

```python
thread_id, run_id, message_id = _agui.new_ids(parsed["thread_id"])
parsed["run_id"] = run_id
```

`run_id` is then used for `RUN_STARTED`/`RUN_FINISHED` (`:2371`, `:2477`) and for the `X-AG-UI-Run-Id`
header (`:2487`). A client that sent `runId: "abc"` and correlates its transcript on it can never match
anything it receives.

**Severity.** P3.

**Cheapest correct fix.** Thread the supplied id into `new_ids` and fall back to a generated one only when
absent — the same "a supplied conversation id becomes the thread id" contract the function already honours
for threads (`agui.py:168-172`).

---

### 12. `MAX_ACTIONS_PER_COMPONENT` is an unreachable check and is missing from the published catalog (P3)

**Evidence (executed).** `a2ui_catalog.py:347-349`:

```python
actions = [k for k in ("action",) if k in raw]
if len(actions) > MAX_ACTIONS_PER_COMPONENT:   # MAX_ACTIONS_PER_COMPONENT == 1
```

The list is built from a one-element literal, so `len(actions)` ∈ {0, 1} and the branch can never be
taken. Confirmed: `MAX_ACTIONS_PER_COMPONENT = 1` printed from the live module. Additionally
`catalog_document()` (`a2ui_catalog.py:190-197`) publishes `maxComponents`, `maxDepth`,
`maxNodesPerParent`, `maxTextLen` and `maxTotalText` but **not** `maxActionsPerComponent`, so the one limit
with a code constant has no wire representation.

**Severity.** P3 — dead code plus an under-documented contract.

**Cheapest correct fix.** Delete the constant and the branch (the `props` check at `:303-306` already caps
actions at one per component, since only `action` is an accepted key for `Action`/`FilterChips`), or count
real event-valued props and publish the limit.

## Verified correct

Checked, not broken — recorded so the next agent does not re-test it.

* **`/api/agui` request validation is otherwise strict and its status codes are right.** Executed: bad
  element role → 400 with the offending index and value; non-object message → 400; missing query →
  400; non-object `state` → 400; 100 001-char query → **413** (not 400); non-text content parts → 400;
  non-object body → 400. The `MAX_INPUT_CHARS` guard at `agui.py:220,288-290` and its 413 status are
  correct.
* **A2UI `maxComponents` (400) and `maxNodesPerParent` (64) both hold.** Executed: 401 components rejected,
  65 children on one parent rejected (`root has 65 children; the limit is 64`). These two limits are fine —
  it is specifically `maxDepth` (#2) and the two text limits (#3) that leak.
* **A2UI really is a closed catalogue, and markup is refused by name.** Executed: unknown component
  (`Iframe`) rejected; `onClick` rejected as a forbidden property; `href`/`src`/`url` in `FORBIDDEN_PROPS`
  (`a2ui_catalog.py:176-179`) rejected. Unknown *property names* are rejected too (`colours` etc.). The
  defect in #4 is that `target` reaches navigation, not that arbitrary props are accepted.
* **`render_to_text` never inlines markup.** `a2ui_catalog.py:415-422` only emits scalars and reprs, so the
  reviewer-facing text form cannot carry injected markup.
* **AG-UI ids are unique per run.** `new_ids` (`agui.py:174`) mints `uuid4().hex` for `runId` and
  `messageId` per request, and tool call ids are a monotonic `call-{n}` counter (`:2354`). No duplicate-id
  path found. `STEP_STARTED` *is* emitted (`agent_server.py:2404`) — I checked whether it appears in the
  module's documented subset list (`agui.py:32-43`) and **it does not**, so that docstring is out of date,
  but no `STEP_FINISHED` exists in the module at all, so I am not claiming a protocol violation I could
  not compare against the spec.
* **The three Apps view flags are honoured — client-side.** `apps.views.table` / `.cards` / `.prefab`
  have **no server-side consumer** (grep: definitions only at `flags.py:53-71`), but
  `console-frontend/src/views/Apps.tsx:252-259` reads all three and computes `effView` correctly, including
  the documented "if all views are off, table is used as fallback" (`flags.py:57`) via
  `tableOn || !cardsOn ? 'table' : 'cards'`. Not a dead flag. `chat.agui` (`agent_server.py:2258`) and
  `apps.a2ui` (`:2545`) *are* enforced server-side and return 404 when off.
* **`/api/tools` ignoring the guard is a listing inconsistency only.** `agent_server.py:1686-1703`
  iterates `_TOOL_CATALOG` without consulting `_disabled_tools()`, so a disabled tool still appears in the
  Tools panel — but `/api/config/tools` (`:1707-1711`) and `/api/capabilities` (`:2227-2230`, which calls
  `tool_payload` at request time) both do reflect it, and `skills.tool_payload` (`skills.py:1046-1057`)
  filters the model-visible list. I am recording this as **P3-consistent behaviour across two endpoints**,
  not a broken guard, and I did not flip a real tool to test it (briefing rule 3).
* **`_gw_auth_headers()` and `gateway._client()` agree on the token file path.** `agent_server.py:1730`
  writes `ROOT/"state"/"tools_disabled.json"` and `skills.py:1035-1036` reads
  `S9_STATE_DIR or code/state` — same file. No split-brain there. (The gateway-token cache-coherence split
  on `:8500` documents/* vs :8109 was found independently by agents 05/06/07 and is **not** re-reported
  here; I only observed its symptom, `GET /api/documents` → 401 in 18575 ms.)
* **Template storage is not path-traversable.** `templates.py` stores templates as keys in a JSON dict
  (`_TPL_PATH`, `_TEMPLATES[name]`), never as filenames, so a name containing `/` or `..` cannot escape
  `state/`. The 5 000-char name already in this instance is a payload-size issue (already known), not a
  traversal. `run_template`'s unfilled-placeholder guard (`agent_server.py:3510-3515`) and the
  12 000-char rendered-query cap (`:3519-3523`) are both correct.
* **`_conv_id` is properly hardened** (`agent_server.py:3549-3567`): `bool` → `None`, number →
  stringified, dict/list dropped. Findings #7 and #9 are the two places that were *not* given this
  treatment.

## Not completed

`:8500` and `:8109` both stopped listening at ~05:30 local (confirmed twice: `Test-NetConnection` false for
both ports, `GET /api/health` → `Unable to connect to the remote server`). I did not restart either
service, per the briefing. The following were planned, are **not** reported as findings, and remain open:

* **Conversation-continuity round trip (task area 1).** Two real chat runs (seed a passphrase on
  `conversation_id=zzperfbh1` via `/api/chat/simple`, then ask for it back via `/api/agui` on the same
  `threadId`) to prove cross-path context sharing, plus `GET /api/sessions/{sid}/graph` before/after. All
  three AG-UI runs in my malformed-request matrix were consumed before I got to this. Not attempted.
* **Does `use_documents` change the next run's DAG?** Finding #1 answers the `/api/chat` half from source
  (it does not). I never executed the `/api/chat/simple` half to confirm documents *are* injected there,
  because `/api/documents` was already 401ing, which makes `_gw_enabled_doc_ids()` raise and
  `_conversation_doc_ids()` return `set()` (`agent_server.py:2709-2714`) — so retrieval was dead on all
  paths during my window and the positive control was impossible.
* **`POST /api/conversations/adopt`** (`agent_server.py:5048-5081`) — read only. I did not adopt a foreign
  session, since that writes shared `conversations.json` state I did not create.
* **`DELETE /api/sessions/{id}` soft vs `?hard=true`** (task area 6). Note from source: **there is no
  `hard` parameter** — the signature at `agent_server.py:1618` is `async def delete_session(session_id: str)`
  only, and `agent_server.py:1635-1637` unconditionally `shutil.rmtree`s the session directory. FastAPI
  silently ignores the unknown query parameter, so `?hard=true` and `?hard=false` are the same irreversible
  operation. I did not execute a delete (briefing rule 3 — the only sessions available were other agents'
  and `zzqa-*`).
* **Chat-thread orphaning after a session delete.** Also unexecuted, and it depends on the above.
* **Cancellation divergence across the four chat paths** (task area 7). From source: only `_stream_run`
  calls `_register_run` (`agent_server.py:4875`), so `/api/chat/simple`, `/api/chat/simple/stream` and
  `/api/agui` runs are **not** cancellable by `POST /api/chat/cancel` — it would return
  `{"found": false, "active_runs": 0}` for their conversation ids. Stated as static analysis; not
  executed, because a live proof needs an in-flight lightweight run.
* **Cost-accounting divergence across paths** (task area 7). From source: the lightweight paths key the
  per-turn ledger by the *conversation id* (`agent_server.py:4286, 4582, 2436`) while the DAG path keys
  it by the mapped `s8-…` session id (`:4961`), and `on_outcome` passes `run_id=conversation_id`
  (`:2392-2396`, `:4241-4244`, `:4533-4536`) so every tool-outcome row's `run_id` equals its `session_id`.
  Both are real divergences in the ledger, but I could not produce gateway numbers (`:8109/v1/spend` and
  `/v1/calls` 401) so I am not claiming a measured discrepancy.
* **Live flag flip (`POST /api/apps/flags`)** and **live `/api/config/tools` disable/restore** — finding
  #7 is static analysis only. No flag or tool was modified, so nothing needed restoring.
* **Template lifecycle end-to-end** (`POST/GET/DELETE /api/templates/zzperf*`, run with and without vars).
  Source-level note I did not make into a finding: `GET /api/templates/{name}` returns **HTTP 200
  `{"status":"not_found"}`** for a missing template (`agent_server.py:3472-3478`) and `DELETE` likewise
  (`agent_server.py:3481-3486`), unlike `GET /api/chat/threads/{cid}` which 404s
  (`agent_server.py:4112-4113`) — a client testing `res.ok` believes a nonexistent template exists. Also
  `POST /api/templates/{name}/run` with a non-object JSON body hits `body.get("vars")`
  (`agent_server.py:3500`) outside any `try` → 500. Unverified live; I ran out of service.
* **Feedback rollup** (task area 8). Source-level note: `POST /api/feedback` does
  `nid = (body.get("node_id") or "").strip()` (`agent_server.py:666`) outside a `try`, so a numeric
  `node_id` → 500 (same class as #9), and `vote = int(body.get("vote") or 0)` (`:668`) truncates, so
  `{"vote": 1.9}` is recorded as an **up** vote and `{"vote": true}` as +1. Neither was executed.

## Appendix A — findings from the coordinator's parallel black-box sweep

*Written by the coordinator against the same live instance, hours before the service went down at
~05:30. These do not duplicate findings 1–12 above; they are additional defects found by a different
method (every `/api/*` route probed with malformed bodies, path traversal, hostile query parameters and
concurrent load). All evidence is real request/response pairs.*

### A1. `doc_ids` in document search bypasses the `enabled: false` gate — **P1**
```
enabled=true   POST /api/documents/search {"query":"LEAKCANARY-zzprobe629fbc"}                      -> 2 hits
enabled=false  POST /api/documents/search {"query":"LEAKCANARY-zzprobe629fbc"}                      -> 0 hits   correct
enabled=false  POST /api/documents/search {"query":"LEAKCANARY-zzprobe629fbc","doc_ids":["doc-…"]}  -> 2 hits   LEAK
```
`enabled: false` means "excluded from retrieval", but an explicit id list bypasses the filter and
returns the disabled document's chunk text. The console's search box does not send `doc_ids`
(`api.ts:598-600` sends `{query, top_k}`), so this is currently API-only — but it becomes a privacy
hole the moment any client adds a filter. **Fix:** apply the `enabled` predicate *after* the id filter,
intersecting rather than overriding.

### A2. Neither service validates `Host`, which makes the launch token remotely reachable — **P1**
```
GET /research            Host: evil.example:8500 -> 200, <meta name="aria-token" content="E2al…OynA">
GET /api/tools           Host: evil.example:8500 + token -> 200
GET :8109/v1/config/keys Host: evil.example     -> 200, credential inventory (which of ~15 vars are set)
```
The per-launch token exists specifically to make the console un-forgeable by another local process, and
it is handed to anyone who asks. With `Host` unvalidated, a DNS-rebinding page can obtain it and then
drive the whole API — `/api/code/file` for source, `/api/chat` to spend. (To be precise about scope: the
gateway's *authentication* is sound — `gateway_auth.py`, enforced at `main.py:283-304`, see report 12 —
this finding is about the agent's token being **disclosed**, not about the gateway being open.)
**Fix:** validate `Host` against `localhost`/`127.0.0.1` and `GATEWAY_HOST` at the ASGI layer, before any
token is injected.

### A3. Notifications accept any content, cannot be deleted, and are permanent — **P1**
```
POST /api/notifications {}            -> 200, appends an entry with empty text
POST /api/notifications {"text":None}  -> 200
DELETE /api/notifications             -> 405 (route does not exist)
```
The live feed already contained a 3 000-character junk entry and an XSS probe string from earlier runs.
Append-only, no cap, no TTL, no validation, no way for an operator to clear it — and it renders in the
Console/Mission triage surface. **Fix:** validate `text` (non-empty, capped), cap the file on write, add
`DELETE /api/notifications/{id}`.

### A4. `/api/code/files` with an out-of-workspace path silently returns the entire root listing — **P1**
```
GET /api/code/files?path=../../../../Windows/System32/drivers/etc/hosts -> 200, full 80 KB root listing
GET /api/code/tree?path=../../../../                                    -> 200 {"error":"path outside the code workspace"}
GET /api/code/file?path=../../.env                                      -> 403 correctly refused
GET /api/code/file?path=S9SharedCode/code/.env                         -> 403 correctly refused
```
`/api/code/file` *is* properly confined, so there is **no traversal read primitive** — but the list
endpoint neither rejects nor errors; it falls back to the workspace root and transfers 80 KB. A caller
cannot distinguish "path rejected" from "here is everything". **Fix:** return 400 with the same error
shape `/api/code/tree` uses.

### A5. Unknown ids return HTTP 200 with an error inside the body — **P2**
```
GET    /api/documents/does-not-exist     -> 200 {"error":"no such document …","status_code":404}
GET    /api/documents/%2e%2e%2f%2e%2e     -> 404 {"error":…}      <- same route, different handler
GET    /api/sessions/nope/graph          -> 200 {"nodes":[],"edges":[]}
GET    /api/sessions/nope/browser-shots  -> 200 {"shots":[]}
POST   /api/computer/approvals/nope      -> 200 {"status":"not_found"}
DELETE /api/documents/nope               -> 200 {"status":"ok"}
DELETE /api/schedule/nope                -> 200
DELETE /api/templates/nope               -> 200
```
The second line is the proof this is a defect rather than a design: the same route 404s for a
traversal-shaped id and 200s for an ordinary unknown one. Every client that checks `response.ok` renders
"deleted" for a document that never existed. This generalises the template note in finding set above —
it is systemic, not template-specific.

### A6. `POST /api/chat` returns HTTP 500 on a non-string `conversation_id` — **P2**
Reproduced 3/3: `{"query":"hi","conversation_id":123}` → 500; `true` → 500. Same class as finding #9
(numeric `threadId`), different route. The gateway's Pydantic layer returns a clean 422 for the same
class of mistake, so the agent layer simply has no request model for `/api/chat`.

### A7. `idempotency_key` is accepted on `/api/chat` and ignored — **P2**
```
POST /api/chat {"query":"…","idempotency_key":"zzprobe-idem-key-1"} -> session s8-ee40156f
POST /api/chat {"query":"…","idempotency_key":"zzprobe-idem-key-1"} -> session s8-d622d160
```
Two sessions, two billed runs, from the key the console sends to prevent exactly that. Distinct from
finding #6: that one is a key that is never *released*; this one is a key that is never *honoured*. A
double-click or an HTTP retry pays twice.

### A8. Cancel is not idempotent, and its result field is a decrementing counter — **P2**
First cancel → `{"found":true,"cancelled":2,"flagged":true}`. Second cancel, same ids, immediately
after → `{"cancelled":1}`. `cancelled` behaves as a running total, so the console Stop button reports a
different value each press. Return a per-request boolean plus a separate `active_runs` count.

### A9. A cancelled run's SSE log prints an empty answer while `done` carries the real one — **P2**
```
data: {"type":"log","text":"…\nFINAL: \n…"}                            <- empty
data: {"type":"done","answer":"Stopped by operator before the run finished.","cancelled":true}
```
Any client rendering the log — the legacy console and the `/api/events` rollup both do — shows a blank
answer, while a client reading `done.answer` shows the explanation. The two disagree about the same run.

### A10. Collection limits are accepted at any magnitude — **P2**
`?limit=99999999` on `/api/sessions`, `?limit=999999999` on `/api/memory`, `?limit=99999999` on
`/api/events` all return **200** with full results. `/api/memory?q=%00` (NUL byte) is accepted.
`min_votes=-1` returns 200 while `min_votes=abc` correctly 422s. `agent_server.py:1346-1349` documents
that `?limit=100000` on `/api/sessions` was a known "parse every session ever recorded, synchronously on
the event loop" hazard that was fixed — yet these three routes remain unclamped.

### A11. `/api/memory?q=` has no relevance floor — **P2**
Querying a string that does not exist (`?q=CANARYMEM-zzprobe6a34b3`) returned three unrelated facts
(`"User instruction to reply with exactly OK3"`, `"Reply with exactly the word IDEM2"`, `"Population of
Mars on 3 March 2027…"`). The Memory page returns confident unrelated rows for any query, including
nonsense, and gives the user no way to tell "no match" from "semantically near".

### A12. Run text and schedule text are persisted as durable long-lived memory — **P2**
After single planning-only queries, `GET /api/memory` showed `"User instruction to reply with exactly
OK3"`, `"Reply with exactly the word IDEM2"`, `"Population of Mars on 3 March 2027 at 14:05 GMT"`. A
`POST /api/schedule` call separately persisted its own text: `"Setting zzprobe629fbc to probe every 1s"`.
The user's entire chat history becomes long-term memory as a side effect, with no TTL on the
episode/fact drawers — and this is the substrate the break-test's memory-injection finding exploits:
stored text is later injected into model context and obeyed.

### A13. The memory list projection exposes no stored content — **P2**
`GET /api/memory` item keys are exactly `['descriptor','drawer','id','keywords','kind','source']` — no
`text`/`value` field, `descriptor` truncated to ~300 chars. Combined with A12, the store is effectively
write-only from the UI's point of view: the operator can neither audit nor correct what the agent
believes.

### A14. Cross-origin write guard honours `Origin` but ignores `Referer` — **P2**
```
POST /api/chat/cancel  Origin: http://evil.example   -> 403 "cross-origin POST refused"
POST /api/chat/cancel  Referer: http://evil.example/x -> 200, executed
```
Not directly exploitable from a normal page (browsers send `Origin` on all cross-origin POSTs), but the
guard's test is "did you remember one header" rather than "is this same-origin". Accept only if neither
header indicates a foreign origin.

### A15. Under concurrency one run received another run's answer — **P2, observed once, not reproduced**
Four concurrent `/api/chat` runs were in flight; a fifth run asking for "a 400 word essay about the
history of clocks" returned **`OK1`** — the exact token requested by one of the concurrent runs. A
controlled sequential pair (seed `BRAVO`, then an unrelated question) answered correctly, so it could not
be reproduced deterministically. Suspected mechanism is A12: the working drawer's 60-minute-TTL
scratchpads carry recent run text into the next run's context as instruction-shaped content. Worth
noting alongside the static cancellation-divergence analysis above — the same root cause, different
symptom.

### A16. `/api/tools` does not list the MCP tools that actually execute — **P3**
`GET /api/tools` returns the skill catalogue (planner, researcher, retriever, action, coder, formatter,
browser, vision_file, summarise, web_search …) and **none** of the MCP tools that the MCP layer exposes
(`read_file`, `write_file`, `list_dir`, `final_answer`, `get_current_time`, `web_search`,
`currency_convert` …). Two tool registries exist — the skill catalogue and the MCP tool list — with no
endpoint that unions them, so the console's tool-disable control operates on a different list from the
one that executes.

### A17. `/api/cost` is empty while `/api/cost/by_skill` is populated — **P2**
```
GET /api/cost          -> {"rows":[], "totals":{"in_tokens":0,"out_tokens":0,"dollars":0.0,"calls":0}}
GET /api/cost/by_skill -> 7 rows, 429 calls, 1.4 M tokens
GET :8109/v1/spend     -> populated
```
The React Ledger page uses `by_skill` and renders correctly; the legacy console (`web/ledger.js`,
`web/app.js` cost panel) uses `/api/cost` and shows a permanently empty table. Two cost endpoints on the
same service disagree. This is the measured counterpart to the static cost-keying divergence noted
above.

### A18. Minor
* A 404 body leaks an absolute filesystem path: `GET /api/code/file?path=llm_gatewayV9/state/flags.json`
  → `{"error":"cannot stat: [WinError 2] … 'C:\\Users\\<user>\\Downloads\\project3\\…'"}` (**P3**).
* Uploading the same CSV filename + content twice creates **two** indexed documents — no content dedupe
  (**P3**).
* `POST /api/documents` with an unsupported type returns `200 {"error": ".exe is not supported …"}`
  instead of 415 (**P3**).
* `state/threads/` holds **656 files** for 236 conversations, written per turn and never pruned; thread
  listings include a `nodes` field populated for only a handful, so most entries look empty (**P3**).

### Closing three items from "Not completed" above
* **Cancellation divergence (partially closed).** I verified the DAG path: `POST /api/chat/cancel
  {"session_id":…,"conversation_id":…}` on an in-flight `/api/chat` returned `{"found":true,
  "cancelled":2,"flagged":true}`, remaining nodes went `skipped`, the `done` frame carried
  `cancelled:true`, cost was still recorded and the graph persisted. So cancellation *does* work on the
  path that calls `_register_run` (`agent_server.py:4875`). I did not execute it against
  `/api/chat/simple`, `/api/chat/simple/stream` or `/api/agui`, so the static claim that those three are
  **not** cancellable stands as source analysis — and it is the more important half, because the
  Research page uses the lightweight paths.
* **Template lifecycle (partially closed).** `GET /api/templates` returned **~16 MB**; the instance
  contains a template with a **5 000-character name** (`state/templates.json` is 23.4 MB). Running an
  unknown template returns 200, and `POST /api/templates/%2e%2e/run` also returns 200 — consistent with
  the `{"status":"not_found"}`-in-a-200 finding above.
* **Cost divergence (partially closed).** `/api/cost` versus `/api/cost/by_skill` (A17) is measured; the
  conversation-id-versus-session-id keying divergence above is not, for the reason given (no gateway
  access).

---

## Recommended work, ordered

| fix | file(s) | effort | risk | blocks what |
|---|---|---|---|---|
| Enforce the A2UI enum/number/list type language already in `CATALOG` (`enum:…`, `number`, `text\|literal`); reject a non-`enum:console` `Link.target` | `S9SharedCode/code/a2ui_catalog.py:296-306` | 1 h | low — payload shapes unchanged | finding #4; the "external URLs are rejected" guarantee in `a2ui_catalog.py:167` |
| Count list-valued and bound property text against `MAX_TEXT_LEN`/`MAX_TOTAL_TEXT`, and charge `data` to the same budget | `a2ui_catalog.py:246-252, 309-322` | 1-2 h | low — may newly reject oversized model output; `MAX_RETRIES=1` absorbs one retry | finding #3 |
| Replace the first-child walk in `_check_depth` with a full iterative DFS over all children (keeps the cycle check and the no-recursion property) | `a2ui_catalog.py:378-400` | 45 min | low | finding #2; the `maxDepth: 12` in `catalog_document()` |
| Thread `conversation_id` into `Executor.run` and use `_conversation_doc_ids` instead of `_gw_enabled_doc_ids` on the DAG path | `flow.py:356-363`, `agent_server.py:2699-2714` | 2 h incl. test | medium — changes retrieval for every `/api/chat` run; needs the 3 lightweight paths checked for parity | finding #1 |
| Release the idempotency key in `chat_simple_stream`'s generator `finally`, mirroring `_stream_run:4988` | `agent_server.py:4462`, `:4576-4584` | 15 min | very low | finding #6 |
| Validate `isinstance(enabled, bool)` in the tool guard and coerce `tool` like `_conv_id`; make `parse_request` accept int/float `threadId` and reject a non-list `messages` | `agent_server.py:1714-1739`; `agui.py:248-258` | 45 min | very low | findings #7, #8, #9 — all three are the same missing-input-validation class |
| Make AG-UI `open_call` a deque so `TOOL_CALL_RESULT` correlates to the right `toolCallId` | `agent_server.py:2336, 2342-2368` | 30 min | low — needs a working multi-tool run to observe | finding #10; the `agui.py:55` linking invariant |
| Seed `title`/`updated` when `_chat_prefs` creates a thread, or split prefs into their own store | `agent_server.py:2667-2678` | 30 min | low | finding #5 |
| Honour a client-supplied `runId` in `new_ids`; add `STEP_STARTED` to the documented subset in `agui.py:32-43` | `agui.py:165-174, 32-43`; `agent_server.py:2276-2277` | 20 min | very low | finding #11 |
| Delete `MAX_ACTIONS_PER_COMPONENT` and its unreachable branch, or make it count and publish it in `catalog_document()["limits"]` | `a2ui_catalog.py:53, 190-197, 347-349` | 15 min | very low | finding #12 |
| Return 404 (not `200 {"status":"not_found"}`) from `GET`/`DELETE /api/templates/{name}`, and validate the body type in `/api/templates/{name}/run` | `agent_server.py:3472-3486, 3496-3502` | 30 min | low — a client may be relying on the 200 | template contract consistency |
| Harden `POST /api/feedback` (`node_id` type, reject non-int `vote`) and `POST /api/config/tools` `tool` type | `agent_server.py:657-672`, `:1719-1728` | 30 min | very low | the "unhandled 500 on a mistyped field" class, alongside #7/#9 |