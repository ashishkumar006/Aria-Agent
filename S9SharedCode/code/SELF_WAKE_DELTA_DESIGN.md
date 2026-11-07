# Self-wake and the change delta — design

**Status:** delta and self-wake implemented and verified live (§9). Actions are
**not** — v1 is read-only, and the feature ships off.
**Scope:** the *main* agent (Chat section, and the DAG/flow runs it starts). Discuss, Code
and the other section agents are secondary surfaces and are explicitly out of scope for v1.

---

## 1. The idea in one paragraph

Today the agent only ever acts when the user types. The scheduler can already fire a
stored query later, but the wake is a replay: the same text, no idea what has happened
since. This feature gives the agent two things. **It can schedule its own wake-up**,
choosing both the time and — more importantly — *why*, in its own words, so a task it
cannot finish now ("watch for the reply from that reviewer and then tell me what it
means") survives the end of the conversation. And **the delta is a tool it calls after
waking**, `whats_new`, which answers "what has changed since I last looked" in one
bounded, pointer-shaped result.

The delta being a *tool* rather than a block injected at wake time is deliberate and it
is the better half of the design: the agent can call it mid-turn, call it again with a
narrower question after reading the first result (`surface: "apps"`, `match: "from:
reviewer@x.com"`), and use it in an ordinary chat turn — "what changed since this
morning" — with no wake machinery involved at all. It also means the wake turn pays for
the delta only when the agent asks for it, and asks once.

## 1a. What a wake actually is: a matcher, not a timer

"Wake up when an email arrives from this person" is not a cron job. There is no push
path, so a wake is a **poll with a predicate**: the row stores how often to look and
what it is looking for; at each tick something cheap and model-free checks whether the
condition is met, and only then does the agent get a turn.

```json
{"kind": "wake", "reason": "reply from reviews@journal.org, then summarise it",
 "watch": {"surface": "mail", "match": {"from": "reviews@journal.org"}},
 "every": "15m", "conversation_id": "ct-144c285e", "wakes_left": 3}
```

Two consequences, both load-bearing:

- **The pre-check must not spend a model call.** An unmatched tick re-arms and is
  invisible. This is the same rule as an empty delta, generalised: a wake that finds
  nothing costs nothing.
- **The pre-check is an external read, and that is its own cost.** Checking a mailbox
  every 15 minutes is ~96 gateway/tool calls a day with zero tokens. Cheap, but not
  free, and it is a *poll* — so the minimum interval floor (§5) is what keeps a
  "check every minute" wish from turning into a rate-limit problem on someone else's
  API. The floor applies to the poll, not to the model turn.
- A time-only wake (`every: "2h"`, no `watch`) is the degenerate case: the predicate is
  "is the delta non-empty", and it is evaluated with the same cheap collectors.


## 2. v1 is read-only

A wake may **read, notice, and report**. It may not write, send, schedule, delete, run,
or edit — not because those are dangerous in the abstract, but because the set of
actions an autonomous turn should be allowed to take is a decision we make *after* we
have watched what the deltas look like in practice. So v1:

- delivers the delta to the agent,
- gives it a read-only tool set,
- lets it append a note (a memory observation, or a message in the conversation it was
  woken in) and nothing else,
- requires it to say what it wants to do next rather than doing it.

Actions come later as an explicit, policy-matched allowlist — see §7.

## 3. The delta

### 3.1 Shape

One small JSON object. It is a *change index*, not a content dump: every entry is a
pointer plus a one-line description, and the agent fetches what it cares about with the
read tools it already has.

```json
{
  "since": 1793790000.0,
  "until": 1793794896.0,
  "cursor": "v1:1793794896.05",
  "changed": {
    "documents": [
      {"id": "doc-6b240e605c", "filename": "Document.pdf", "what": "indexed",
       "detail": "139 chunks, was 0", "at": 1793793000.1}
    ],
    "memory":   [{"id": "mem:fact-…", "drawer": "fact", "what": "added",
                  "detail": "prefers terse answers", "at": …}],
    "runs":     [{"session": "s8-…", "what": "completed", "detail": "5 nodes, 2 warnings",
                  "at": …}],
    "apps":     [{"id": "…", "what": "items added", "detail": "3 new of 12", "at": …}],
    "artifacts":[{"id": "art:…", "what": "produced", "detail": "Grid-Scale… .pdf", "at": …}],
    "files":    [{"path": "notes/x.md", "what": "modified", "detail": "14:22, +3/-1", "at": …}]
  },
  "counts": {"documents": 1, "memory": 4, "runs": 1, "apps": 1, "artifacts": 2, "files": 0},
  "truncated": {"memory": 40}
}
```

Rules that keep it a *delta* and not a data dump:

- **Bounded by construction.** A hard ceiling per category (default 25 entries) and on
  `detail` length (120 chars). Anything over the ceiling is counted and marked in
  `truncated`, so "there were 240 new chunks, showing 25" is a fact the agent can act
  on rather than a silent cut. This is the standing rule about no silent caps.
- **Pointers, not payloads.** An entry never carries file contents or a chunk body. The
  agent has `read_file`, `read_report`, `artifact read` for that.
- **One `detail` per entry, written for a reader who already knows the system.** The
  agent should not need a second call to know whether to make a second call.

### 3.2 The cursor

`state/wake_cursors.json`, one entry per conversation:

```json
{ "ct-144c285e": {"cursor": "v1:1793794896.05", "updated": 1793794896.1,
                  "wakes": 3, "last_wake": 1793794896.0} }
```

Written atomically (`_write_json_atomic`), advanced **only after the wake turn has been
delivered**, never when the delta is computed. A wake that crashes must re-see the same
delta; a wake that advanced the cursor first loses it. This is the only place the
feature keeps state, and it is the same pattern `telegram_poller.py` already uses for
its last-seen offset.

### 3.3 Where each change set comes from — and the one thing not to do

Every surface already records a timestamp:

| surface | source of truth | signal |
|---|---|---|
| documents | `state/documents/documents.json` | `status`, `chunk_count`, `updated_at`, `enabled` |
| memory | `state/memory/<drawer>/memory.json` | `created_at`, `superseded_by`, drawer |
| runs | `state/sessions/<sid>/` + `turn_logs.json` | node statuses, `turn_costs.json`, mtime |
| apps | `state/apps/<id>.json` + `.data.json` | `history[]`, `added`/`removed`, `error` |
| artifacts | `state/artifacts/<sha>.json` | file mtime, provenance |
| files | Code-workspace roots | mtime vs the stored watermark (size + mtime, no content hash) |

**Do not read the audit drawer for this.** `state/memory/audit/memory.json` is 138 MB
and `MemoryStore._save_locked` rewrites the entire file on every mutation — the exact
mechanism that made a document search take 35 seconds this week. A "what changed since
T" implemented as an audit scan would put that stall on a timer. The delta reads the
*registries and sidecars above*, which are small and already indexed by id, and it uses
mtime so an unchanged file is not parsed at all.

The same discipline applies everywhere: **check mtime + size before reading, and cache
per-file results across wakes.** The delta is computed on a background thread, never on
an event loop, and each category runs independently so one slow surface degrades to
`"error": "…"` in the payload rather than failing the whole delta.

## 4. Self-wake, on the scheduler that exists

No new engine. [`scheduler.py`](scheduler.py) already does persisted jobs, a heap
worker, cron parsing, conversation-bound firing, missed-slot skipping, idempotent
create, nudge-only zero-cost delivery, and cost recording into the Ledger. Added to a
schedule row:

```json
{ "kind": "wake", "reason": "checking whether the solar briefing changed",
  "conversation_id": "ct-144c285e", "wakes_left": 3 }
```

Behaviour:

- A `kind: "wake"` job carries **no query**. At fire time it builds the delta, and if
  the delta is empty it does nothing at all — no model call, no cost, no message. The
  nudge-only path (`_is_nudge_only`) is the precedent for "a scheduled row that must not
  spend money".
- If the delta is not empty, the wake runs as a chat turn in `conversation_id`, with the
  delta as a system block and the row's `reason` as the instruction. Same assembly path
  as Discuss, different tool set (§6).
- **Re-arming requires a stated reason.** The wake may create a follow-up wake only by
  passing a non-empty `reason` and a future time. A wake that ends with no follow-up
  ends the chain. This is the loop rule — and it costs nothing to enforce because the
  default is no re-arm.
- `wakes_left` is a per-chain depth counter, decremented on each wake, so "check every
  morning forever" requires the user to say so, not the agent.
- Fired wakes are recorded in the conversation as a normal assistant turn, so the user
  can read what it noticed and cancel it from the Scheduler page like any other job.

## 5. Guardrails, and where each one actually belongs

The gateway is the right home — but the current picture is uneven, and one piece that
looks like it exists does not gate model calls yet.

**Already real.** [`main.py:614`](../../llm_gatewayV9/main.py) calls `_budget.check(user_id)`
on every model request: 120 requests/min, 5,000/day, 5M tokens/day per user
([`budget.py`](../../llm_gatewayV9/budget.py)). A self-wake loop is a series of ordinary
model calls, so it is inside that ceiling from day one. This is the guardrail the design
relies on and it needs no work.

**Already real, and directly reusable.** [`policy/engine.py`](../../llm_gatewayV9/policy/engine.py)
matches on *any* key present in the request dict — `_match()` reads arbitrary fields,
which is why `trust` and `tool` work today. Adding `agent`, `session` and `trigger`
("chat" | "scheduled-wake" | "flow") is a caller-side change, not an engine change.
First-match-wins, default-deny, per-rule `enforce: true` to arm inside global dry-run,
and a third action `approve` that already routes to the pending-approvals flow.

**The gap to close.** `spend_caps_usd.per_session_per_day: 10.00` and
`per_agent_per_day: 5.00` are configured and *computed* by the engine — but the engine
is only consulted at the **channel-send** boundary
([`channels_api.py:118`](../../llm_gatewayV9/channels_api.py)), not on the model path,
and the `spend-cap` rule is dry-run like everything else in `policy.yaml`. So today a
runaway loop would be stopped by the *per-user daily* budget, not by a per-session
ceiling — and the per-session number is exactly the dimension that distinguishes "this
one conversation woke forty times" from "the user did a lot of work today".

Two changes close it, and both are prerequisites for turning self-wake on rather than
nice-to-haves:

1. **Evaluate the policy on model calls**, passing `agent`, `session`, `trigger`, and
   the spend rollups the engine already knows how to compute. Verdicts already have
   columns (`calls.policy_verdict`, `calls.policy_rule`), so this is observable from the
   first call without a schema change.
2. **Arm the cap rule** (`enforce: true` on `spend-cap`) — or arm it only for
   `trigger: scheduled-wake` first, which keeps dry-run everywhere else while making
   the new capability the strictly-governed one. That is the increment I would take:
   every new autonomy gets its own enforce-before-general rule.

**Plus the two cheap structural limits, because they cost nothing:**

- `wakes_left` per chain, default small (3).
- A minimum re-arm interval (floor of 10 minutes regardless of what the agent asks for).
- One wake per conversation at a time: reuse the existing per-conversation `turn_lock`
  so a wake cannot pile onto a turn the user is already having.

And the honest note on the *other* failure mode: the risk I raised earlier was not
really money, it was **a wake that re-arms itself to look at an empty world**. The
delta fixes that directly — an empty delta spends nothing at all — so the budget work
above is about a runaway, and the empty-delta rule is about the boring loop that would
otherwise run for a month saying nothing.

## 6. Read-only, enforced where it is cheap

Enforce at **composition**, not at dispatch: the wake turn builds its tool payload from
an explicit read-only allowlist rather than `_chat_tool_payload()`. Reusing the chat
payload and filtering it at dispatch time leaves one forgotten caller as the hole.

Starting set (reads and notices only):

`whats_new` (the delta, §3), `search_knowledge`, `read_report`, artifact/document
reads, `web_search`, `fetch_url`, `recall_preferences`, `list_scheduled`,
`check_channel`, `get_time`, plus the profile reads. Excluded outright in v1: every
write, `run_command`, `computer_action`, `write_file`/`edit_file`, `send_channel_message`,
`render_document`, `kill_app`, `enable_scheduled`, and both handoff tools.

**One deliberate exception: `schedule_wake`.** Without it the design contradicts itself
— the whole point is that the agent decides when to look again, and a watch that cannot
be extended dies at its first unmet tick. So a wake may create or extend **a wake and
nothing else**:

- `schedule_wake(reason, every, watch?)` — required non-empty `reason`; `every` clamped
  to the floor (§5); decrements `wakes_left` from the calling row and refuses at zero;
  can name the same conversation only.
- It cannot send a message, arm a lead notification, set `notify`, cancel another job,
  or create a job with a `query`. Those are what `schedule_task` can do, and
  `schedule_task` stays off the wake's list.

That keeps v1 read-only in the sense that matters — a wake can change *when it next
looks*, never the user's world — while still allowing "keep watching until it
arrives".

Defence in depth: the same list is a policy rule keyed on `trigger: scheduled-wake`, so
the gateway refuses the dangerous tools even if the composition side regresses.

## 7. Later, when actions come (noted now so the shape does not fight it)

Actions are a policy decision, not a code change, which is the point of routing tool
availability through the engine: a new rule

```yaml
- id: wake-can-note
  when: {trigger: scheduled-wake, tool: [remember_preference]}
  action: allow
  enforce: true
```

widens the wake one tool at a time, each with its own audit trail in
`calls.policy_rule`. Anything irreversible should start at `approve`, which already
exists end to end.

## 8. Flags

Three, per the standing rule that a new capability is one switch away from gone:

- `agent.whats_new` (bool, default **true**) — **shipped with the delta.** Off removes
  the tool from the chat payload *and* makes the route refuse with 503 naming the
  switch, so the model is never told about a capability it cannot use. It leaves the
  watermark file alone.
- `agent.self_wake` (bool, default **false**) — the only flag that turns the feature on.
  Off: no wake jobs can be created, any existing ones stay stored but never fire, and
  the wake's `schedule_wake` tool is not offered.
- `agent.delta_readonly` (bool, default **true**) — kept true through v1 and most of v2;
  setting it false is what the actions work will flip, and it is named so that the diff
  that widens autonomy is one obvious line.

## 9. Build order

1. ✅ `delta.py` — compute + bound + cache. Testable cold, with no wake machinery at all.
2. ✅ Cursor file, atomic write, advance-after-delivery.
3. ✅ **The delta as a tool.** `whats_new` in the MCP tool set, plus `GET
   /api/whats-new` for the console. Steps 1–3 are inert with respect to autonomy and
   were worth doing on their own: the agent now answers "what changed since this
   morning" today, and the shape got validated before any wake depends on it.
4. ⬜ **Not done — the gate.** Policy evaluated on the model-call path with
   `trigger`/`agent`/`session` + the armed cap rule. Measured 2026-11-04: 102,058 of
   107,738 ledger rows carry no `policy_verdict`, and every verdict that exists is on
   `channel:*` sends, so the wake's guardrails below are ours, not the gateway's.
   Steps 5–6 shipped behind `agent.self_wake` (default off) precisely so this can be
   landed before anyone leaves the feature on overnight.
5. ✅ `kind: "wake"` in the scheduler: the model-free predicate, the delta pointer, the
   read-only tool payload composed by allowlist, `schedule_wake` with `wakes_left`, the
   10-minute poll floor and the 24-hour watch horizon. Deviation from §5 as written:
   the wake's *first* tick is floored at its own arming time, because a fresh wake has
   no watermark and `since=0` would let its first tick match last Tuesday's document.
6. ✅ The wake turn's own visibility: the delivered turn is stored in the bound
   conversation as `kind: "wake"` with its reason and change counts, and the Scheduler
   page distinguishes an agent-armed watch from a user reminder, shows wakes left, what
   it is watching, and whether it ended, failed or is merely paused by the switch.

Verified live 2026-11-04 rather than only in tests: a real chat turn armed
`wake-fe84aa01`; a real upload matched it and nothing else; the delivered turn read the
file and reported the actual numbers; the cursor advanced; the row consumed one step and
did not re-arm; and the wake armed its own successor `wake-129b542a` with the chain
budget inherited downward (3 → 2), not reset. Three defects came out of that run that
25 passing unit tests had missed: two module-local imports referenced as globals, a
failed turn burning a chain step, and a request ending on a system block (Gemini 400).


## 10. What "working" looks like, verified

Not a unit test of the shape only. The standard is:

- Seed real change (upload a document, write a memory, finish a run, add app items) and
  assert the delta names exactly those, in bounded size, with a correct cursor round
  trip: wake → nothing new → empty.
- An empty delta spends **zero** gateway rows — verified from `calls`, not from the log.
- A wake cannot invoke a write tool: assert the composition allowlist *and* a policy
  deny for `run_command` under `trigger: scheduled-wake`.
- Re-arm without a stated reason is refused.
- Two wakes against one conversation serialise on the turn lock.
- The 138 MB audit drawer is never opened: assert it by counting reads, so a future
  "just scan the audit log" optimisation fails the test instead of reintroducing the
  stall.
- Live, at real scale: a wake armed on a real conversation with a real document upload,
  observed end to end in the console.

## 11. Decisions I want a steer on

1. **Which surfaces are in the first delta.** Documents + memory + runs + apps is the
   obvious four; `files` means picking the Code-workspace roots, and I would rather
   leave files out until the first four are trusted. Your call.
2. **Where a wake's finding lands.** In the bound conversation (visible, interruptible,
   and it grows a thread you did not start) versus a Notifications row that only shows
   the summary (quieter, easier to ignore). I lean conversation, because an agent that
   tells you something where you can immediately ask a follow-up is more useful than a
   log line — but the noise argument is real.
3. **Re-arm floor.** 10 minutes is my guess for the shortest interval a self-wake may
   ask for, independent of what the agent proposes.
4. **Arming the spend cap.** Arm `spend-cap` globally, or only for `trigger:
   scheduled-wake` first? The second keeps everything else in dry-run while making the
   new autonomy the governed one — which is the pattern I would like to keep using.
