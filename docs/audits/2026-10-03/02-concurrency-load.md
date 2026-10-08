# Concurrency and load audit — the Aria agent has no admission control and its "stop" button is unreachable exactly when it is needed

The agent is a single uvicorn process with **one event loop and one thread pool**, and it runs a
blocking-heavy orchestrator on top of that loop. Under load it does not degrade gracefully — it
**bimodally stops**: I measured `/api/health` at a p50 of **7.8 ms** (baseline 9.1 ms) while **14 of
188 requests received zero bytes and no status line at all**, and two dark windows in one round
totalled ~120 s of the 199 s round. The `GET /assets/index-DFQaDsVD.js` static asset tracked
`/api/health` sample-for-sample, which is what makes the outage *total* rather than API-only. The
mechanism is not mysterious and I have it down to five line numbers: `POST /api/chat` performs
**two `Thread.join(timeout=60)` calls and a `ensure_gateway()` that `time.sleep(1)`s for up to 45 s,
all synchronously inside an async generator** (`agent_server.py:4772`, `:4776`, `:4779`), and the
comment above them asserts the opposite. Separately, every gateway-proxied route builds a fresh
`httpx.AsyncClient` per request (`agent_server.py:3616` and four siblings), which costs **230–459 ms
of synchronous work on the event loop each time** — 20 concurrent proxied requests blacked the agent
out for **10.2 s** with no chat involved at all. Cancellation is worse than absent: a run registers
itself in `_ACTIVE_RUNS` **seconds after** the client already holds its session id
(`agent_server.py:4846` yields `started`, `:4860` registers), so the operator's first "stop" returns
HTTP 200 `found: false` and does nothing — I watched **seven consecutive cancels** be dropped that
way. And during the worst window `POST /api/chat/cancel` itself **timed out at 25 s three times**,
so there is no manual escape hatch. Finally, `contextlib.redirect_stdout` at
`agent_server.py:4834` is process-global, so with two concurrent runs the SSE `log` frames of one
run are delivered into the *other* run's stream — I captured run A's banner inside run B's frames
while run A emitted no logs at all in 199 s.

---

## Scope & method

Read-only audit except for one accident, disclosed in full at the end of this section. No service was
started, stopped or killed. No `pytest`. `/api/chat` used **5 times total** (budget ~6). All scratch
scripts lived in `%TEMP%\kilo\` and are deleted.

### Why raw sockets

The reported symptom (`HTTP/1.1 200 OK` + `content-length: 52` and then nothing) cannot be diagnosed
with `urllib`/`Invoke-WebRequest`, because those raise a generic timeout that hides *which* part of
the response arrived. Every latency probe therefore used a raw `socket` so I could record
`connect()` separately from time-to-first-byte from total, and whether bytes arrived at all:

```python
# %TEMP%\kilo\zzconc_load.py  (abridged)
def raw_get(path, timeout=8.0, hdrs=None):
    t0 = time.perf_counter()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(timeout)
    s.connect(("127.0.0.1", 8500))
    t1 = time.perf_counter()                      # connect_ms
    s.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1:8500\r\nConnection: close\r\n\r\n"
               % path).encode())
    buf = b""
    while True:
        chunk = s.recv(65536)
        if not chunk: break
        if first_byte_ms is None: first_byte_ms = ...   # first_byte_ms
        buf += chunk
    ...
```

Two sampler threads ran at 400 ms cadence for the whole of every load round:

* `GET /api/health` (the only unauthenticated route — the liveness signal)
* `GET /assets/index-DFQaDsVD.js` — a real 266 KB hashed bundle from
  `console-frontend/dist/assets/`, chosen so "does the SPA still load?" is answered directly rather
  than through the JSON 404 path

A third thread polled `POST /api/chat/cancel {}` (empty body) as a free event-loop heartbeat, because
that response carries the `active_runs` counter. Chat clients were raw-socket `POST /api/chat` with
`Accept: text/event-stream`, recording time-to-first-byte and every SSE frame.

### Load rounds actually executed

| round | concurrency | agent pid | note |
|---|---|---|---|
| R0 quiet baseline | 0 | 16992 | 30 samples, no load of mine (instance already loaded by peers) |
| R1 "N=2" | 2 requests | 16992 | **only 1 run executed** — see Finding 7 |
| — | 0 | — | instance collapsed; pid 16992 gone; pid changed 29124 → 26676 on the gateway |
| R2 pre-baseline | 0 | 26812 | fresh process: p50 **9.1 ms** |
| R2 "N=4" | 4 requests | 26812 | 3 read to completion, 1 disconnected at t+5.7 s |
| R3 cancel series | 1 request | 26812 | 13 cancel probes around one live run |
| R4 leak probes | 0 | 26812 | 20+20 proxied requests, docs, process accounting |

### The instance is shared — read this before trusting any absolute number

Twelve audit subagents plus a `zzqa-*` harness were hitting the same process. Two consequences I
have tried to control for rather than hide:

1. **R0/R1 are contaminated.** `/api/health` was already at p50 **2137.7 ms** / max **11 113.3 ms**
   with *zero* load of mine, while `active_runs` read `0` on all 15 polls. So R1's numbers are
   reported as *shared-instance* evidence, not as my load's effect.
2. **R2 onward is clean.** After the restart (pid 26812) the pre-baseline was p50 9.1 ms /
   p95 13.7 ms / max 28.7 ms with no peers active, and the round's own numbers are reported against
   that same-minute baseline. This is the only round where a load/latency attribution is valid.

### Disclosure — I broke the read-only rule

While drafting the "cheapest correct fix" for Finding 1 I used the Edit tool on
`S9SharedCode/code/agent_server.py` **inside the repo** instead of writing the patch to my scratch
directory. I reverted it with `git checkout -- S9SharedCode/code/agent_server.py`. The revert was
clean against HEAD (`7a68b9f`), but the working tree was **not** identical to HEAD: it was ~864 bytes
and ~15 lines larger, and I destroyed that delta.

What was in it, verified by diffing against HEAD: a `def _gw_auth_headers() -> dict:` helper that
reads `gateway._gateway_token()` and returns `{"X-Gateway-Token": tok}`, plus a
`headers=_gw_auth_headers(),` kwarg on five `httpx.AsyncClient(...)` sites. The committed tree has
**zero** occurrences of `Gateway-Token` or `gw_auth`. **Someone must re-apply `_gw_auth_headers`** —
without it no agent-side gateway proxy route sends a credential at all. I checked
`%APPDATA%\Code\User\History` (50 snapshots, newest 2026-09-05 at 41 KB against a 231 KB file — none
usable) and there are no `.orig`/`.bak` files in the repo, so I cannot recover it myself. I also
checked and cleared one wrong guess of mine: both files contain **zero** U+FFFD replacement
characters, so the mojibake repair is intact.

No other repo file was modified by me. No `git add`/`commit`/`stash`. All `file:line` citations
below were **re-verified against the current on-disk file after the revert**, so they are correct for
the committed state a reader will see.

### Not verified

Stated plainly so the next person does not assume I checked:

* **The 20× reindex/DELETE document probe and the 20× `/api/memory/remember` + DELETE probe did not
  execute.** Every gateway-proxied route returned `401 {"error":"missing or invalid x-gateway-token
  header"}` (a peer had already reported the `state/gateway.token` clobber), so no document was
  created, no `zzperfc1` memory row exists, and nothing needed deleting. The *concurrency* half of
  those probes did run and is Finding 1.
* **Concurrent read+delete of one session's graph** — not executed; my probe-run sessions had already
  been reaped and the token 401 blocked re-creating one.
* **A scheduled job firing under chat load** — not executed. No job was due (Finding 9), and I
  created none. The scheduler concurrency claim is code-derived only.
* **The `_unregister_run` key-collision wedge** (Finding 5) is code-derived; I could not get two runs
  onto one `conversation_id` before the process collapsed.
* **"Cancel while the DAG is between nodes"** — every mid-run cancel I issued *was* between nodes
  (the runs spent ~99% of wall time between node boundaries), but I never landed one that the
  Executor actually observed, because the run I instrumented finished in 10.1 s.

---

## Findings

### 1. `POST /api/chat` hard-blocks the event loop for up to 165 s, synchronously, inside an async generator — and the comment says it does not

**Severity: P0**

**Evidence.** `agent_server.py:4772` and `:4776` are `Thread.join(timeout=60)` calls, and
`:4779` is a bare `ensure_gateway()`. All three are in the body of the `gen()` async generator that
`_stream_run` hands to `StreamingResponse` (`:4975`), so all three run **on the event loop**. The
comment directly above them claims the opposite:

> `# so we join it (non-blocking on the event loop — this runs in the`
> `# generator, not the event loop) rather than re-launching.`

An async generator's body runs *on* the loop, not beside it. Downstream, `gateway.py:118` is
`time.sleep(1)` inside a `for _ in range(45)` loop.

Measured, R1 (one run, pid 16992): the two `/api/chat` connections — separate TCP sockets, separate
client threads — received their response headers at **3884.1 ms** and **3885.4 ms**, 1.3 ms apart,
and the first in-round `/api/health` and static-asset probes were released in the same batch. Two
independent requests cannot land 1.3 ms apart by chance; they were flushed in one loop iteration
after the loop was released. The health/static probes at t+0.4 had already been served in
15.8/18.9 ms, so the loop was free, then blocked for ~3.5 s.

Measured, R2 (four runs, settled agent): TTFB **532.3 / 547.3 / 575.6 / 595.4 ms** — a 63 ms spread
across four connections, i.e. admitted together, against a 9.1 ms baseline. Against a single chat on
the same settled process, TTFB was **16.4 ms**.

And R1's own SSE log, verbatim from the run I was watching:

```
128.7s log  [gateway] launching llm_gatewayV9 from C:\...\llm_gatewayV9
129.3s log  [gateway] launching llm_gatewayV9 from C:\...\llm_gatewayV9
130.4s log  [gateway] up on http://127.0.0.1:8109
130.9s log  [gateway] up on http://127.0.0.1:8109
```

Two launches 0.6 s apart for one gateway that was already running — Finding 2.

**Repro.**

1. Start the agent. Do not make any request (so the import-time warmup threads
   `_GATEWAY_WARMUP_THREAD` / `_EMBEDDER_WARMUP_THREAD` are still alive).
2. From another process, `GET /api/health` in a loop and record `connect()` vs first-byte.
3. In parallel, send one `POST /api/chat {"query":"...","research":true}`.
4. Observe `connect()` succeeding in single-digit ms while first-byte is seconds to tens of seconds,
   and health/static probes returning zero bytes.

**Root cause.** `agent_server.py:4772`, `:4776`, `:4779` — `Thread.join(timeout=60)` ×2 and a
synchronous `ensure_gateway()` in an `async def` generator. Worst case per chat request:
60 + 60 + 45 = **165 s of hard loop block**, during which `/api/health`, `/api/events`, static
assets and every other chat are unreachable. It is one-shot per process (the second join on an
already-joined thread returns immediately), which is exactly why it presents as "the agent was fine,
then went dark for minutes, then self-recovered".

**Impact.** The total, minutes-long outage reported in the batch briefing. Single-process, no
admission control, no queueing, no `asyncio.Semaphore` anywhere on the chat path.

**Cheapest correct fix.** Wrap all three, and correct the comment:

```python
if _GATEWAY_WARMUP_THREAD is not None and _GATEWAY_WARMUP_THREAD.is_alive():
    await _aio.to_thread(_GATEWAY_WARMUP_THREAD.join, 60)
if _EMBEDDER_WARMUP_THREAD is not None and _EMBEDDER_WARMUP_THREAD.is_alive():
    await _aio.to_thread(_EMBEDDER_WARMUP_THREAD.join, 60)
from gateway import ensure_gateway
await _aio.to_thread(ensure_gateway)
```

---

### 2. `ensure_gateway()` is an unlocked thundering-herd launcher: it spawns duplicate gateways, and its liveness probe is starved by the very pool it shares

**Severity: P0**

**Evidence.** The duplicate launches are quoted in Finding 1. The trigger for them is in R1's log
too, 24 s earlier:

```
104.9s log  [memory.read] gateway unreachable (ReadTimeout('timed out')); continuing without hits
128.7s log  [gateway] launching llm_gatewayV9 ...
```

The gateway was up and serving throughout (it answered `401`, and the run's own planner call
completed with `HTTPStatusError ... 401`, not a connection error). The agent nevertheless concluded
the gateway was down and launched two more.

**Repro.** 1. Load the agent so the shared outbound pool is busy. 2. Send one `POST /api/chat`.
3. Watch the run's SSE `log` frames for `[gateway] launching`. 4. Count them — more than one means
duplicate `uv run main.py` processes raced for port 8109.

**Root cause.** `gateway.py:101-122`:

* `gateway.py:103` `if _is_up(): return` — then straight to `gateway.py:111`
  `subprocess.Popen(["uv","run","main.py"])`. There is **no lock, no singleton flag, no shared
  state** guarding this. Two concurrent callers that both fail `_is_up()` both spawn.
* `gateway.py:93` `_is_up()` probes `("/health", "/healthz", "/v1/routers")` **sequentially at
  `timeout=2.0` each** — up to 6 s of blocking per call, and `_is_up()` is called from
  `agent_server.py:4779` on the event loop.
* Those probes go through the **single process-wide pooled client**, `gateway.py:72`
  `httpx.Client(timeout=30.0, ...)` with `gateway.py:74` `max_connections=16`. Under load all 16
  slots are held by long LLM calls, so the liveness probe **queues behind real work**, hits its 2 s
  timeout, and reports "down" for a gateway that is up. That is the pool-exhaustion → false-negative
  → relaunch chain, and it is why this fires *under load specifically*.

**Impact.** Port-8109 contention between gateway processes; the agent can end up supervising a
gateway it just killed the healthy one to replace. This is my leading candidate for what escalated
R1 into the process disappearing (Finding 3).

**Cheapest correct fix.** A module-level `threading.Lock` plus a "launch in progress" timestamp so
only one caller ever spawns; drop `_is_up()` to a single `/health` probe at `timeout=1.0`; and give
the liveness probe its **own** `httpx.Client` so real work cannot starve it.

---

### 3. The outage signature: listener stays up, zero bytes are sent, and the cancel endpoint is unreachable for the duration

**Severity: P0**

**Evidence — what is observable from outside.** R2, `/api/health` and
`GET /assets/index-DFQaDsVD.js`, 400 ms cadence, 188 and 187 samples:

```
health  n=188 ok=174 starved=14  p50=7.8 ms  p95=84.1 ms  max=7726.4 ms
static  n=187 ok=173 starved=14  p50=16.5 ms p95=80.5 ms  max=5646.1 ms
```

The 14 starved health samples: `t+24.3, 32.3, 40.3, 48.3, 105.2, 121.0, 129.0, 137.0, 145.0, 153.0,
161.0, 169.0, 177.0, 185.0` — each **8010–8016 ms, 0 bytes**. The static sampler's starved timestamps
are the same to within 0.1 s. Two contiguous dark windows, ~32 s and ~88 s, ≈ **120 s of a 199 s
round** with no response of any kind.

Answers to the specific questions asked:

* **Does the TCP listener stay up?** Yes, always. `connect_ms` across every starved sample was
  **0.6–15.4 ms**. The socket is accepted into the backlog; nothing is ever refused, in R1 or R2.
* **Headers without a body?** I never got *any* bytes, not even the status line — so this is the
  same failure one step earlier in the response than the `HTTP/1.1 200 OK` + `content-length: 52`
  case in the briefing. `200 + content-length: 52` then nothing is what you see when the loop is
  blocked *after* FastAPI has flushed the response head and *before* the body iterator runs; I was
  always blocked before the head.
* **Does it correlate with concurrent chat load?** Yes, and it does **not** require chat at all —
  see Finding 4. Correlation across rounds: 1 run → 64 s dark; 4 runs → 120 s dark; 20 proxied
  non-chat requests → 10.2 s dark.

**It is bimodal, not gradual.** This is the finding that matters for operating the service:

| load | `/api/health` p50 | p95 | max | samples with 0 bytes |
|---|---|---|---|---|
| 0 (R2 pre-baseline) | 9.1 ms | 13.7 ms | 28.7 ms | 0 / 12 |
| 4 chats (R2) | **7.8 ms** | **84.1 ms** | **7726.4 ms** | **14 / 188** |
| 0 (R2 recovery) | 3.3–25.6 ms | — | 25.6 ms | 0 / 12 |

p50 did not move at all. The API does not get slower — it **stops**. A load test that only tracks
p50 will score this service as healthy.

**The stop button is down for the whole outage.** `POST /api/chat/cancel {}` from my heartbeat
thread: R1 returned `25009.6 / 25006.6 / 25014.7 ms` and no body (my 25 s client timeout), at
t+55.5 / t+80.5 / t+105.5. R2: `25017.2 ms` at t+24.18, `25007.2 ms` at t+120.96, `25007.7 ms` at
t+145.97. So for ~75 s in R1 and ~75 s in R2 the operator could not reach the one endpoint that
exists to stop a run.

**The client's own liveness signal dies too.** `_CHAT_STREAM_HEARTBEAT_S = 10.0`
(`agent_server.py:3996`) — a heartbeat is owed every 10 s. chat[0]'s actual `status` frames arrived
at 14.5, 61.3, 72.2, 82.3, 92.3, 102.4, 120.9, 194.4 s. Gaps of **46.8 s** and **73.5 s**: seven
missed heartbeats with no indication anything was wrong. The comment at `agent_server.py:4868-4870`
says the heartbeat exists so "the page looks dead rather than busy" — the block defeats it.

**Recovery is immediate and complete.** R2's recovery series: `22.9, 15.9, 4.3, 24.3, 5.4, 4.8, 4.4,
22.1, 4.6, 3.3, 25.6, 3.7 ms` — 3.3–25.6 ms, *faster* than the pre-round baseline. Nothing needs
restarting; the loop resumes the moment the blocking call returns. That is the "self-recovered"
half of the reported symptom.

**Severity rationale.** P0 because it is a full denial of service on a single-process service,
reproducible with one request, with no recovery action required or possible.

---

### 4. Every gateway-proxied route constructs a fresh `httpx.AsyncClient` per request — 230–459 ms of synchronous work on the event loop, each time

**Severity: P0**

This is the cheapest to trigger and the most damning, because **it needs no `/api/chat` at all** and
it is fully deterministic. It is also, I believe, the mechanism behind most of the outage the batch
briefing attributes to concurrent chat.

**Evidence — the construction cost**, measured in the agent's own virtualenv
(`.venv\Scripts\python.exe`, Python 3.11.13, httpx 0.28.1):

```
AsyncClient() construct ms: min=229.6 p50=281.2 max=459.1   series=[230,233,246,274,281,306,390,459]
Client()     construct ms: min=315.8 p50=512.1 max=581.3   series=[316,460,479,504,512,516,518,581]
ssl.create_default_context ms: min=53.3 p50=62.9 max=66.2
```

So constructing one client is **230–459 ms of synchronous work**, and it happens *inside the
handler*, on the loop, before a single byte goes to the gateway. The repo already knows this:
`gateway.py:33-37` says `httpx.get(...)` "constructs an SSL context and re-reads the CA bundle each
time — ~1s of pure setup on this machine".

**Evidence — the black-out**, on the settled agent (pid 26812, R4), each request answered by the
gateway with an instant 401:

```
1  × POST /api/memory/remember                       742.2 ms
20 × POST /api/memory/remember  (parallel)   p50 10228.8 ms   min 10198.2  max 10416.6
4  × POST /api/documents/search    (parallel)   2076.6 / 2332.4 / 2335.1 / 2336.7 ms
20 × POST /api/chat/cancel         (parallel)   49.7 … 101.5 ms   p50 91.2
```

Read the last line as the control. `POST /api/chat/cancel` does no I/O — it is pure in-process work —
and 20 concurrent copies complete in **91 ms p50**. The gateway-proxied routes do the *same amount of
concurrency* and take **10 229 ms p50**. The difference is entirely the per-request client
construction. The 20 memory requests all landed inside a **219 ms band** (10 198–10 417 ms) — the
signature of a convoy, not of 20 independent slow calls: they were serialised on the loop and
released together.

20 concurrent requests × ~500 ms of construction ≈ 10 s of total loop blockage. That *is* the total
outage, reproducible with a loop of `curl`.

**Repro.**

```python
import threading, aria_probe as a
b = {"kind":"scratchpad","descriptor":"zzperfc1 probe","value":{"a":1}}
ts = [threading.Thread(target=lambda: a.raw("POST","/api/memory/remember",b,timeout=90))
      for _ in range(20)]
[t.start() for t in ts]; [t.join() for t in ts]
# meanwhile, from another process:
#   GET /api/health  ->  0 bytes for ~10 s, connect() still ~1 ms
```

**Root cause.** Five sites each build a throwaway client inside an `async def`:

* `agent_server.py:2022` `async with httpx.AsyncClient(timeout=_GW_UPLOAD_TIMEOUT) as c:` (`documents_upload`, `_GW_UPLOAD_TIMEOUT = 300.0`)
* `agent_server.py:2111` `... timeout=_DOC_CTX_TIMEOUT_S ...`
* `agent_server.py:3541` (`_gw_bytes`)
* `agent_server.py:3616` (`_gw_json`) — the one behind `/api/memory/*`, `/api/documents/*`
* `agent_server.py:3912` `... timeout=180.0 ...`

`httpx.AsyncClient.__init__` is synchronous CPU/IO (SSL context + CA bundle). There is no shared
pool and no `to_thread`.

**Impact.** Any burst of gateway-backed traffic blacks out the whole agent for seconds-to-minutes.
The console polls several of these routes. This is the highest-leverage defect in the file because
the fix is mechanical and the payoff is ~500 ms of event-loop time per proxied request.

**Cheapest correct fix.** One module-level `httpx.AsyncClient` created at import with the right
`limits`, and pass it into `_gw_json`/`_gw_bytes`. Failing that, `await asyncio.to_thread(httpx.AsyncClient, ...)`. Do not leave a per-request construction in an async handler.

---

### 5. Cancel races: the run registers seconds after the client has its id, the count double-reports, and one id type is an unhandled 500

**Severity: P1**

Four distinct defects in one 20-line handler.

#### 5a. The registration gap — the operator's first "stop" is silently dropped

`_register_run` is called at `agent_server.py:4860`, but the client already has the session id at
`agent_server.py:4846` (`yield _sse("started", session_id=...)`), and between them sits
`agent_server.py:4857` `await _aio.to_thread(_session_cost_breakdown, session_id)` — a gateway round
trip. Registration is dead last.

R2, `active_runs` polled every 1.5 s across the four-run round:

```
t+0.02:0  t+1.53:0  t+3.03:0  t+4.55:2  t+6.05:4  t+7.56:4 ...
```

Four runs were connected and streaming from t≈0.6 s, yet the endpoint reported `active_runs: 0` for
~3 s and an undercount of 2 at t+4.55. R1 was worse: `started` frame at 3.9 s, `active_runs` still
`0` at t+9.93, first `1` at t+11.95 — an **~8 s window**.

R3 makes the consequence unambiguous. After the client had received `sid=s8-2c47cedb`, **seven
consecutive cancels were dropped**:

```
C valid session_id only   -> 200 {'found': False, 'cancelled': 0, 'active_runs': 0}
D twice #1                -> 200 {'found': False, 'cancelled': 0, 'active_runs': 0}
D twice #2                -> 200 {'found': False, 'cancelled': 0, 'active_runs': 0}
E two concurrent (a)      -> 200 {'found': False, 'cancelled': 0, 'active_runs': 0}   15.0 ms
E two concurrent (b)      -> 200 {'found': False, 'cancelled': 0, 'active_runs': 0}   15.7 ms
F same id in both keys    -> 200 {'found': False, 'cancelled': 0, 'active_runs': 0}
G by conversation_id      -> 200 {'found': False, 'cancelled': 0, 'active_runs': 0}
H (two requests later)    -> 200 {..., 'active_runs': 1}
```

Same endpoint, same run, `found:false` seven times and then `active_runs: 1`. HTTP 200 every time, so
the UI reports success.

**Root cause.** Ordering in `_stream_run`: yield the id at `:4846`, snapshot cost at `:4857`,
register at `:4860`. Also `cancel_chat` treats "not in `_ACTIVE_RUNS`" as "nothing is running" and
answers `found:false` rather than "not yet registered".

**Cheapest correct fix.** Call `_register_run` **before** the first `yield`, and have `cancel_chat`
distinguish *unknown id* from *known but not yet registered* — or simply return
`"registered": len(_ACTIVE_RUNS)` alongside `active_runs` so the UI can tell.

#### 5b. `cancelled` double-counts one run as two — the "2 → 1" step

A live run is registered under **both** keys (`agent_server.py:4860`
`_register_run(cancel_ev, session_id, conversation_id)`). `cancel_chat` builds
`agent_server.py:4728` `keys = [body.get("conversation_id"), body.get("session_id")]` with no dedup,
so when a caller supplies both — which the console does — `pairs` has two entries for one event and
`cancelled: len(pairs)` reports **2**. Meanwhile `_active_run_count()` at `agent_server.py:4707`
dedups with `len({id(v) for v in ...})` and correctly reports **1**.

**That is the 2 → 1 decrement in the briefing.** It cannot go negative — it is `len()` of a list —
but it over-reports every stop by 2×, and `{"session_id": X, "conversation_id": X}` makes it report 2
for a single id that appears twice in `keys`.

**Cheapest correct fix.** `keys = list(dict.fromkeys(k for k in (body.get("conversation_id"), body.get("session_id")) if k))`, and report `cancelled: len({id(ev) for _k, ev in pairs})`.

#### 5c. Unhashable id → unhandled 500

```
{"session_id": ["a"]}    -> HTTP 500  (509.2 ms)
{"session_id": {"a": 1}} -> HTTP 500  ( 44.8 ms)
{"session_id": 12345}    -> HTTP 200 {'found': false, ...}
null body                -> HTTP 200 {...}
{"conversation_id": ""}  -> HTTP 200 {'found': false, ...}
```

`agent_server.py:4730` `pairs = [(k, _ACTIVE_RUNS[k]) for k in keys if k and k in _ACTIVE_RUNS]` —
`k in dict` raises `TypeError: unhashable type` for a list or dict, and there is no `try` around it.
Note `session_id` is string-typed everywhere else in this file
(e.g. `agent_server.py:1624` validates with `^[A-Za-z0-9][A-Za-z0-9_-]{0,64}$`); this handler is the
one route that forgot to.

**Cheapest correct fix.** `if isinstance(k, str) and k in _ACTIVE_RUNS`, or coerce/reject non-strings with a 400.

#### 5d. The wedge: `_unregister_run` pops keys without checking identity — *not reproduced, code-derived*

`agent_server.py:4701` `_unregister_run` is `for k in keys: _ACTIVE_RUNS.pop(k, None)` — no check
that the stored event is the one being removed. `_register_run` (`:4694`) is
`_ACTIVE_RUNS[k] = cancel_ev`, an unconditional overwrite. Two requests that share a
`conversation_id` also share a `session_id`, because `agent_server.py:4993` resolves the id through
`resolve_session`. So: run B overwrites run A's entries; run A finishes; A's `finally` at
`agent_server.py:4841` calls `_unregister_run(session_id, conversation_id)` and pops **B's** entries
too. B is then still running, still burning tokens, still holding a thread — and reports
`found: false`, `active_runs: 0`, i.e. permanently uncancellable.

I could not force two runs onto one `conversation_id` before the process collapsed, so this is read
from the code, not measured. The reporting half of it I *did* observe: `active_runs: 0` while the
API was in a 64 s dark window (R1), and `found:false` seven times against a live run (R3).

**Cheapest correct fix.** Guard on identity: `_ACTIVE_RUNS.pop(k, None) if _ACTIVE_RUNS.get(k) is cancel_ev`. Pass the event into `_unregister_run` and keep a `refcount` per key.

---

### 6. Client disconnect mid-SSE: the run keeps going, keeps its registration, keeps its thread — and the log stream ends up on the wrong run

**Severity: P1**

**Evidence — executed once.** `chat[3]` read 3 frames, then I hard-RST the socket (`SO_LINGER 0`) at
t+5.7 s. Session **`s8-edbfcb92`**.

*Does the run keep going?* Yes. `active_runs` stayed at **4** from t+6.05 all the way through
t+102.88 — roughly **97 s after the disconnect** — while only three clients were still attached. The
counter only fell to 0 near the end of the round. `GET /api/sessions/s8-edbfcb92/graph?light=true`
confirms the session exists with `n:1 skipped`, `elapsed_s 2.6`.

*Is it still billed?* Yes, and by design: `agent_server.py:4904-4916` persists the spend delta on
the disconnect path and skips only the notification. That part is correct.

*Does it hold a thread?* Yes. The code says so —
`agent_server.py:4895-4897`: *"on disconnect the worker thread may still run to completion in the
background (threads can't be killed)"*. The cancel event is **never set** on disconnect, so the DAG
runs on to its node limit.

**The part I did not expect — the two runs swapped log streams.** chat[3]'s frames at t=5.5 s and
t=5.7 s were:

```
5.5s log  ===== session s8-22fbd...      <-- chat[0]'s session banner
5.7s log  ===== session s8-edbfc...
```

chat[3] received **chat[0]'s** banner. And chat[0] — which ran the full 199.4 s — emitted **zero
`log` frames** in that entire time: only `started`, eight `status` heartbeats, `meta` and `done`.

**Root cause.** `agent_server.py:4834` `with contextlib.redirect_stdout(_LiveLogCatcher()):`
redirects the **process-global** `sys.stdout`, while each catcher pushes into its **own** generator's
queue via `agent_server.py:4825` `loop.call_soon_threadsafe(log_q.put_nowait, s)`. With two or more
concurrent runs, the last-installed catcher receives every `print` from every thread. When it exits,
it restores stdout to the original and the other run's remaining logs go to the real stdout and are
lost from its SSE stream permanently.

**Impact.** Concurrent runs cross-contaminate each other's operator-visible logs — actively
misleading during exactly the incident you would be debugging — and one run's log output can vanish
completely. This is also a data-integrity problem for the on-disk session logs.

**Cheapest correct fix.** Don't redirect a global. Give each run a `contextvars.ContextVar` holding
its sink and have the logging calls target it; or serialise runs behind a queue.

---

### 7. A burst of identical "research" submits is silently merged, so the documented path cannot create N concurrent runs

**Severity: P2**

**Evidence.** R1 sent 2 identical `POST /api/chat {"query":"What is 2+2?…","research":true}`.
chat[1] never ran:

```
started  an identical run is already in progress - showing that one
status   joined the run already in flight
error    an identical run is already in progress, so this submit was not run again
```

`TTFB 3885.4 ms` — it still paid the full admission cost of Finding 1 for nothing. Only **one** run
executed, so my "N=2" round was really N=1. I had to give every request a distinct query suffix to
get genuine concurrency, which is itself the evidence that the guard is query-derived.

**Root cause.** `agent_server.py:5017` `idem_key = "research:" + _req_digest(query)`, checked at
`:5019`; a hit returns `_dedup_stream(existing)`. The window is `_IDEM_TTL_S = 600.0`
(`agent_server.py:4318`), and `agent_server.py:4911-4915` documents that a disconnect leaves the
key claimed for the **full 600 s** — after which a later *different* question under the same key is
swallowed as a duplicate.

**Impact.** Two users asking the same research question share one answer and one bill, with no
indication. A `POST` that starts a paid DAG run returns HTTP 200 with an `error` frame rather than a
`409`/`429`, so nothing in the transport tells the client it was refused.

**Cheapest correct fix.** Scope the derived key to the conversation id as well as the query, and
return `409` with the joined run's id instead of an SSE `error` frame.

---

### 8. Synchronous filesystem and graph work in `async def` handlers, on paths the console polls

**Severity: P2**

Not separately measured, but they are on the exact hot path the client polls during a run and they
are the plausible amplifiers of the 88 s dark window in Finding 3.

| site | what blocks the loop |
|---|---|
| `agent_server.py:1528` `async def session_graph` | `:1542` `store.read_all_nodes()` — reads **every** node state file of the session. The SPA polls this ~every 2 s during a run (per the comment at `:1536`). |
| `agent_server.py:~1380-1400` `GET /api/sessions` | walks the whole sessions root and rebuilds a networkx graph per session via `:1400` `node_link_graph` |
| `agent_server.py:1619` `async def delete_session` | `:1636` `shutil.rmtree(_sdir, ignore_errors=True)` — a full recursive directory wipe, on the loop |
| `agent_server.py:4957` `from skills import take_browser_artifacts` | filesystem walk inside the SSE generator, after the run |
| `agent_server.py:4936` `delta = _session_cost_delta(...)` | **not** wrapped in `to_thread`, unlike the pre-run snapshot at `:4857` which is. `_session_cost_breakdown` (`:3691`) is a blocking `httpx` GET at `timeout=5` to `:8109`, so this can block the loop ~5 s on every completed run. Same un-`to_thread` mistake at `:4905` on the disconnect path. |

`agent_server.py:4930` `notify_task_done(...)` is called inline too, but it returns immediately
unless `AGENT_TELEGRAM_NOTIFY=true` (`agent_server.py:334`) — **verified safe**, do not spend time
on it.

**Cheapest correct fix.** `await asyncio.to_thread(...)` around each; reuse one module-level
`AsyncClient` for the cost snapshots.

---

### 9. The scheduler is one thread with no admission control against live chats

**Severity: P2**

**Inventory at 23:47Z** (`GET /api/schedule`, 27 rows): **exactly 1 enabled** —
`sch-abf344ce`, `daily@09:00`, next_fire in **+13 517 s** (~3.75 h out), `last_fire` 72 879 s ago,
`last_error: null`. The other 26 are disabled, including 20 `zzqa-conc-sched *` one-shots all due in
+664…+815 s with `enabled: false` (so they will not fire), `sch-74f7aa6b` `every 0m`, and
`sch-b941136f` `in -5m`. **No `zz*` job was enabled and none was due during my window.** I created
no jobs.

**So: I did not observe a fire under load. The concurrency claim below is code-derived only.**

**Can a job fire while N chats are in flight? Yes, unconditionally.**

* `scheduler.py:336` `_worker` is a **single** thread walking a heap, `while not _STOP.wait(timeout=1.0)`.
* `scheduler.py:371` `_fire(sid, snap)` is called outside `_SCHED_LOCK` — good — but on **that same
  thread**. So jobs are strictly serialised: one 3-minute research job delays every other due job,
  which then all fire in a burst when it returns.
* `scheduler.py:303` `answer = asyncio.run(Executor().run(...))` — a **fresh event loop per fire**,
  on the scheduler thread. It is not the uvicorn loop, so it does not directly block serving, but it
  competes for the GIL and shares the process-global `sys.stdout` that Finding 6 is about.
* `_fire` **never registers in `_ACTIVE_RUNS` and never reads it.** There is no queue, no priority,
  no cap, and no way to stop a fired job from `POST /api/chat/cancel`. A due job landing during an
  N-chat burst adds a full extra DAG run to an already-saturated process.
* `scheduler.py:37` `_load()` replaces the module-global `_SCHEDULES`/`_HEAP` from disk, and
  `scheduler.py:202` `list_schedules()` calls it on **every request** under a `threading.Lock` — a
  synchronous file read + JSON parse per console poll, on the event loop.
* `scheduler.py:288` and `:321` call `_session_cost_breakdown`, the same blocking 5 s httpx GET —
  but on the scheduler thread, so not a loop block.

**Cheapest correct fix.** Give the worker a small thread pool (or hand each fire to a daemon thread)
with a bounded concurrency of 1 *shared with chat admission*, register fired runs in `_ACTIVE_RUNS`
so they are visible and cancellable, and cache `schedules.json` with an mtime check instead of
re-reading it per request.

---

## Verified correct

Checked, so nobody re-tests these.

* **No thread, handle, or socket leak from chat load or disconnects.** Across 6 `/api/chat` runs
  (1 with a hard-RST disconnect), 20 parallel `POST /api/memory/remember`, 20 parallel
  `POST /api/chat/cancel`, 4 parallel `POST /api/documents/search`: agent threads went 6 → 18 at
  peak and settled at a stable **14**; handles went 254 → 389 and settled at a stable **374–376**
  against a 376 baseline, sampled at 5 s intervals over 40 s with no upward trend. No growth.
* **Disconnect does not lose the spend.** `agent_server.py:4904-4916` persists the cost delta on the
  disconnect path and suppresses only the notification — deliberate and correct.
* **Cancel after a run has finished is clean.** Cancelling a completed session id and a completed
  conversation id both returned `200 {'found': false, 'cancelled': 0, 'active_runs': 0}` in 8.3 ms and
  5.1 ms, with no side effects. `found:false` for an idle run is the documented contract
  (`agent_server.py:4719-4723`), not a bug.
* **`cancelled` cannot go negative.** It is `len()` of a list (`agent_server.py:4734`). It
  over-reports (Finding 5b) but there is no decrement path.
* **Cancel with no ids, a null body, an empty string, an unknown id, and an integer id all return
  200 `found:false`** — 35+ such probes across three rounds, 7.5–42.4 ms, no exception. The
  `body = {}` fallback at `agent_server.py:4724-4727` works.
* **`_ACTIVE_RUNS` is properly locked.** `_ACTIVE_RUNS_LOCK` (`agent_server.py:4691`) is a real
  `threading.Lock`, held only for dict get/pop — short and correct. Two genuinely concurrent cancels
  (E) both returned cleanly with no torn read. No lock-ordering fault found between
  `_ACTIVE_RUNS_LOCK`, `_TURN_COST_LOCK` (`agent_server.py:3761`), `_CONV_LOCK`, and
  `scheduler._SCHED_LOCK`; none of them is ever held across an `await`.
* **The SSE log queue is correctly bounded and correctly off-loop.** `agent_server.py:4810`
  `asyncio.Queue(maxsize=2000)` with `call_soon_threadsafe` and a `QueueFull` drop
  (`:4826`) — the "don't use `threading.Queue` here" hazard called out at `:4816-4819` is genuinely
  avoided. No overflow observed.
* **Client-disconnect detection works.** `request.is_disconnected()` (`:4775`) did fire — the
  generator broke out on schedule, and the run was correctly not notified. The *policy* is wrong
  (Finding 6) but the detection is not.
* **`notify_task_done` does not block.** It returns at `agent_server.py:334` unless
  `AGENT_TELEGRAM_NOTIFY=true`, and the notification thread it may spawn is a daemon. No network I/O
  on the loop.
* **The gateway is up and `:8109` is reachable throughout** — every 401 in my logs came *from* the
  gateway, i.e. a working round trip, not a connectivity failure.
* **Recovery needs no intervention.** Every round self-recovered to 3.3–25.6 ms within ~2 s of the
  blocking call returning. Restarting the agent never fixed anything; nothing was stuck.

---

## Recommended work, ordered

| # | fix | file(s) | effort | risk | blocks what |
|---|---|---|---|---|---|
| 1 | One shared `httpx.AsyncClient` at import (or `to_thread` the construction) at all five sites | `agent_server.py:2022, 2111, 3541, 3616, 3912` | ~30 min | low | ~500 ms of loop per proxied request; the 10 s black-out from a 20-request burst |
| 2 | `await to_thread` the two `Thread.join(timeout=60)` and `ensure_gateway()`; fix the comment | `agent_server.py:4772, 4776, 4779` | ~15 min | low | the multi-minute total outage |
| 3 | Lock + singleton guard around gateway launch; dedicated 1 s `/health` client for liveness | `gateway.py:82-122` | ~45 min | med | duplicate gateway processes fighting for :8109 |
| 4 | `_register_run` before the first `yield`; dedup `keys`; type-validate ids; identity-check `_unregister_run` | `agent_server.py:4694-4734, 4846-4860` | ~1 h | low | "stop" silently doing nothing; the uncancellable-run wedge |
| 5 | Stop redirecting process-global stdout; per-run log sink | `agent_server.py:4825-4834` | ~2 h | med | concurrent runs cross-contaminating / losing logs |
| 6 | `to_thread` the graph/session/rmtree handlers and the post-run cost delta | `agent_server.py:1528, 1400, 1619, 4936, 4905` | ~1 h | low | loop stalls amplified by console polling |
| 7 | Set the cancel event on client disconnect | `agent_server.py:4895-4917` | ~20 min | med | abandoned runs burning tokens and threads for their full node limit |
| 8 | Bound chat concurrency with a semaphore; return 429 past the cap | `agent_server.py:4998`, `_stream_run` | ~1 h | low | any recursion of the above under real load |
| 9 | Scheduler: pool for fires, register in `_ACTIVE_RUNS`, cache `schedules.json` | `scheduler.py:37, 202, 303, 336-371` | ~2 h | med | job bursts colliding with chat bursts |
| 10 | Scope the `research:` idempotency key to the conversation; `409` on dedup | `agent_server.py:5017, 4318, 4911` | ~30 min | low | unintended answer/bill sharing |
| — | **Re-apply `_gw_auth_headers()`** (lost by my revert — see Scope) | `agent_server.py` (5 sites) | ~10 min | low | all agent→gateway credentialed calls |

Fix 1 before fix 2 in practice — it is smaller, mechanical, and it is the one that removes ~500 ms
of loop per request regardless of which code path issued it.