# Persistence, state and clock audit — 05

*Written by the coordinator after the assigned subagent failed twice on upstream idle timeouts.
Scope: `S9SharedCode/code` state storage, write amplification, atomicity, cache coherence and time
handling. No live measurement was possible — the agent on `:8500` died at ~05:30 during this batch and
the gateway on `:8109` is token-mismatched (401). Everything below is from source and bounded
filesystem inspection.*

## Scope & method

* Source read: `persistence.py`, `scheduler.py`, `agent_server.py` (`/api/events`, `/api/sessions`,
  cost/notification writers, chat stream), `turnlog.py` referenced for discipline parity.
* Filesystem: directory-level counting only (`Get-ChildItem -Recurse | Measure-Object Length -Sum`) — no
  per-file reads, no content scans. 1 534 files / 32.5 MB under `state/`, 260 files / 18.8 MB under
  `tests/`, 125 files under `sandbox/`.
* Live state was **not** available. The write-amplification figure in F3 is therefore derived from code
  plus measured on-disk sizes, not from a before/after diff of one run. That is stated as an open item.

---

## Findings

### F1 — `/api/events` reports scheduler jobs as if they had already happened, with a timestamp 12 h away from everything else — **P1**

`S9SharedCode/code/agent_server.py:931` pushes each enabled job using **`next_fire`** as the event time:

```python
_push(j.get("next_fire") or 0, "sched", "scheduler",
      f"next: {(j.get('query') or '')[:120]} ({j.get('when') or j.get('recurring') or '?'})")
```

and `:886` renders that epoch as the clock string:

```python
"iso": _dt.fromtimestamp(t).strftime("%H:%M:%S")
```

`next_fire` is in the **future** by definition. Two consequences, both observed live in the break-test:

1. The Mission feed is sorted newest-first on `t`, so every enabled job floats to the **top** of the
   feed as the most recent thing that happened.
2. Its `iso` shows the job's wall-clock time. Observed alongside real runs:
   `{"src":"scheduler","t":1791084600.0,"iso":"09:00:00","msg":"next: Give me the morning briefing (daily@09:00)"}`
   sitting next to run events stamped `20:45:12`. The two look like they happened 11 h 45 m apart.

The feed never distinguishes "this already ran" from "this will run". The event carries a future
timestamp, a `next:` prefix buried in the message, and no state field.

**Impact:** the Console/Mission feed — the primary operator triage surface — misrepresents scheduling.
Anything reading it (including an agent, via `/api/events`) sees future events as recent history.

**Cheapest correct fix:** give the event an explicit `kind: "scheduled"` plus `at:` (the epoch) and
`fires_in_s:` (delta), key the sort on a separate `observed_at = time.time()` field rather than
`next_fire`, and drop `iso` in favour of a full ISO timestamp. That is a ~6-line change in
`/api/events` plus the client sort.

### F2 — every timestamp in the feed is time-of-day only, with no date and no zone — **P2**

`agent_server.py:886` formats `%H:%M:%S` and nothing else. A run at 23:50 yesterday and a run at 00:10
tonight are indistinguishable in the feed, and `count: 75` events spanning days are rendered as if they
were one session. There is no timezone or offset anywhere in the payload, so a client cannot normalise.

**Fix:** emit full ISO-8601 with offset (`datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")`)
and keep `t` as the epoch for sorting.

### F3 — the session graph is rewritten in full on every save — **P1 (growth × O(n²))**

`persistence.py:172`:

```python
payload = nx.node_link_data(h)
_atomic_write(self.graph_path, json.dumps(payload, indent=2, default=str))
```

The whole graph is serialised with `indent=2` and rewritten, not appended. The recovery-storm sessions
that `AGENT_BREAK_TEST_REPORT.md` documents reach 31–38 nodes; every save re-serialises all of them.
`agent_server.py:1346-1349` states the consequence explicitly for the read side: *"each row parses a
whole graph.json, so `?limit=100000` was a one-request way to make the server parse every session ever
recorded, synchronously on the event loop."* The same shape applies on the write side.

**Open item (not verified):** I did not confirm how many times `save_graph` is called per node event.
If it is once per node completion, bytes written per run are O(n²) in node count; if it is once at the
end, it is O(n). `flow.py` call sites need one grep to settle it. This is the single most valuable
follow-up in this report.

Measured cost of the current shape: `state/sessions` = **862 files / 9.12 MB**, with individual sessions
carrying browser artifacts of 691 KB, 277 KB, and several 169 KB PNGs.

### F4 — `state/templates.json` is 23.4 MB and is served whole to the browser — **P0 for that endpoint**

The single largest file in the repo state is `S9SharedCode/code/state/templates.json` at **23.4 MB**.
`GET /api/templates` returns ~16 MB of it (measured during the break-test). A template name of 5 000
characters exists in the live instance, and `POST /api/templates` appends without a length cap.

Three compounding costs: the file is re-read and re-parsed on every request, it is rewritten in full on
every create, and the console fetches all of it to render a list. Nothing prunes it.

**Cheapest correct fix:** cap `name` length (64 chars is ample) and query text; reject duplicates by
name; and return a summary (`name`, `vars`, `updated_at`) from the list route, fetching the body only
for a single template. That converts a 16 MB response into a few KB with a two-line handler change plus
a validation bound.

### F5 — MCP subprocess pipes are leaking, and the leak writes ~0.9 MB of stderr — **P1**

`S9SharedCode/code/agent.err` is **894 KB** and its tail is entirely:

```
ValueError: I/O operation on closed pipe
Exception ignored in: BaseSubprocessTransport.__del__
ResourceWarning: unclosed transport ... stdin=<Pipe ...>
```

from `asyncio/windows_utils.py:102` via `base_subprocess.py:70` and `proactor_events.py:80`. Every
skill invocation spawns a stdio MCP session (`mcp_runner.py`); these are being garbage-collected
unclosed. This is independent evidence for the resource-leak finding the concurrency audit was chasing,
and it is unbounded growth on disk.

**Fix:** wrap the subprocess teardown in `mcp_runner.py` so the transport is explicitly
`close()`d/`await wait_closed()` in a `finally`, and kill the process if it does not exit. The
`ResourceWarning` text names the exact objects.

### F6 — `logs/` is re-read and ANSI-stripped on every `/api/events` poll, and it grows — **P2**

`agent_server.py:946-947` tails `logs/agent.out` and `logs/agent.err` on **every poll** of
`/api/events`, which the console hits every 3 s. The comment at `:936-938` confirms the work per poll:
de-ANSI via regex, filter uvicorn noise, promote tracebacks. `logs/` is now **5.97 MB** and growing —
largely because of F5. So poll cost grows with log size, and log size grows with a leak.

**Fix:** tail by byte offset (the feed already has a `since` parameter) instead of re-reading whole
files; the log-tail buffer should be a ring, not a growing file.

### F7 — mixed naive/aware datetimes in the schedule parser — **P2**

`scheduler.py` builds fire times three different ways:

* `:105-107` `_dt.datetime.fromtimestamp(base)` → **naive local**, then `.timestamp()` re-interprets it
  as local. Self-consistent.
* `:124-125` `fromtimestamp(base) + timedelta(days=1)` then `.replace(...)` → naive local.
* `:127` `_dt.datetime.fromisoformat(cron).timestamp()` → **aware if the operator supplied an offset.**

`POST /api/schedule` accepts both `2026-10-04T09:00:00` and `2026-10-04T09:00:00+05:30` with the same
result envelope. The first is interpreted as host-local; the second as UTC+05:30. Two jobs that *look*
identical in the list view fire at different instants, and nothing in the response tells the operator
which interpretation was used.

**Fix:** normalise to aware UTC at parse time (`datetime.fromisoformat(...).replace(tzinfo=timezone.utc)`
when naive, documented as UTC), store `next_fire` as epoch, and echo the resolved timezone in the API
response.

### F8 — local-time day bucketing and DST-sensitive cron — **P2**

* `agent_server.py:776-778` `_dt_day(ts)` = `fromtimestamp(ts).strftime("%Y-%m-%d")` — **local** day
  boundary. Any per-day metric bucketed with it misattributes spend across midnight, and the bucket
  moves if the host timezone changes.
* `scheduler.py:103-107` computes `daily@HH:MM` with naive local `replace()`. Across a DST transition
  `target.timestamp()` shifts by an hour, so a "daily 09:00" job fires at 08:00 or 10:00 local twice a
  year.

**Fix:** bucket days in UTC; compute recurring local times with `zoneinfo` and re-resolve across
transitions.

### F9 — conversation threads grow one file per conversation, forever — **P3**

`state/threads/` = **656 files / 0.24 MB**, one per conversation id, written by
`agent_server.py:2462-2465` on every turn (`thread["updated"] = _time.time()`). Small individually, but
nothing prunes them and the count is already in the hundreds.

### F10 — agent-side "memory" does not exist on disk; every memory read is cross-process HTTP — **P3 (correctness note for the latency audit)**

`state/memory` does not exist. Memory lives in the gateway, so every drawer read the agent performs per
turn (`flow.py` calls `memory.read` once per run — the SSE log line `[memory.read] 8 hit(s) visible to
every skill this run`) is an HTTP round trip to `:8109`. That places memory recall on the critical path
of every run and behind the gateway's token. Flagging for report 01.

---

## Verified correct

* **`_atomic_write` is genuinely atomic and Windows-safe** — `persistence.py:58-74`: unique temp name
  per write (explicitly to avoid two concurrent writers clobbering one tmp file, and because
  `os.replace` fails on Windows when the destination is held open), text always written as UTF-8, then
  `os.replace`. This is the right discipline and it is applied consistently, including to `turnlog.py`
  and the gateway memory store.
* **No `fsync`** anywhere in `persistence.py`. Data reaches the OS page cache and the rename is atomic,
  so a *process* crash cannot leave a truncated file — but a power loss can. Acceptable for a cache of
  run artefacts; worth stating explicitly rather than leaving ambiguous.
* **Disabled one-shots are handled properly.** `scheduler.py:362-365` documents and implements
  disable-and-persist *before* executing, so a crash mid-fire cannot produce a re-fire on the next
  `_load()`. The comment records this was a real bug that was fixed.
* **`/api/sessions` and `/api/events` are documented as clamped**, and `agent_server.py:1346-1349`
  records that the `?limit=100000` amplification was a known, fixed hazard. The reasoning is sound.
* **`_inflight_runs` is TTL-swept** — `agent_server.py:4345-4369` prunes entries older than
  `_IDEM_TTL_S` under `_inflight_lock` on both the set and get paths, so the idempotency map cannot
  grow without bound.

---

## Recommended work, ordered

| # | Fix | Files | Effort | Risk | Blocks |
|---|-----|-------|--------|------|--------|
| 1 | Cap template `name`/`query` length; make `GET /api/templates` return summaries; dedupe by name | `templates.py`, `agent_server.py` list handler | 30 min | low | the 16 MB console fetch (report 04/06) |
| 2 | Fix the scheduler event timestamp: `observed_at` for ordering, `at`/`fires_in_s` for the schedule, ISO with offset | `agent_server.py:886, 931` | 20 min | low | Mission feed trust; anything reading `/api/events` |
| 3 | Close MCP subprocess transports in a `finally`; kill non-exiting children | `mcp_runner.py` | 1 h | med — must not break the tool path further | F5 leak, `agent.err` growth, agent stability (report 02) |
| 4 | Byte-offset log tailing + ring buffer instead of whole-file re-read per poll | `agent_server.py:936-960` | 1 h | low | `/api/events` poll cost |
| 5 | Confirm `save_graph` call frequency; if per-node, write node state to per-node files and assemble the graph on read | `persistence.py:172`, `flow.py` | 30 min to confirm, 2 h to fix | med | O(n²) write amplification |
| 6 | Normalise schedule datetimes to aware UTC; echo the zone in the response | `scheduler.py:99-130` | 45 min | med — changes firing times for existing jobs | correctness of every schedule |
| 7 | Bucket days in UTC; use `zoneinfo` for recurring local times | `agent_server.py:776`, `scheduler.py:103-125` | 45 min | low | DST/midnight misattribution |
| 8 | Retention job for `state/threads`, browser artifacts and `state/templates.json` | new | 1 h | med — deleting user data needs a policy decision first | unbounded disk growth |
| 9 | State the durability contract (no fsync = process-crash safe, not power-loss safe) in `persistence.py`'s docstring | `persistence.py` | 5 min | none | operator understanding |

**Do not** prune `state/templates.json` or browser artifacts without deciding what the retention policy
is — that is an operator decision, not a cleanup task.