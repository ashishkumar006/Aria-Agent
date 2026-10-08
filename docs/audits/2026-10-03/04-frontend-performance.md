# Console frontend performance — 04

*Written by the coordinator. The assigned subagent timed out upstream. **This is browser evidence captured
during the break-test, plus source reading — not a fresh instrumented profile.** The agent went down
mid-batch, so per-view request counts over a 60 s idle window, MutationObserver render counts and heap/DOM
measurements could not be taken. Those gaps are listed explicitly in §6 rather than guessed at.*

## What was measured, in a real Chromium session

| Observation | Value |
|---|---|
| Static requests on first load | **24** |
| Entry chunk | `index-DFQaDsVD.js` = **266 521 bytes** |
| Views | 12, all `React.lazy` in `App.tsx` |
| Routes rendering at 390 px | 12/12, **zero horizontal overflow** (`scrollWidth == clientWidth` on every one) |
| Collapsed rail icon labels | non-empty `aria-label` on all 12 |
| Document title | **`Aria · Research` on all 12 routes** — never changes |
| `/runs` DAG | a 31-node / 10-failed session rendered; the header read "186 runs · 406 nodes · $0.0000" |
| Poll intervals observed | `/api/events` **3 s**; `/api/cost/by_skill` **15 s**; `/api/schedule` **15 s** |
| Fully broken routes | `/documents` and `/code` — 100 % of their API calls 403 |

---

## Findings

### F1 — Two routes in the navigation rail cannot function at all — **P0**

`/documents` and `/code` are the only two routes served through the SPA catch-all, and the catch-all
returns the **560-byte** raw `dist/index.html` instead of the **641-byte** shell the other ten routes get.
The difference is the `<meta name="aria-token">` element.

Consequence, reproduced in the browser and server-side three times:
* `/documents` → `couldn't load documents (403 missing or invalid X-Aria-Token …)`, counters read
  `0 uploaded / 0 ready`, while `GET /api/documents` with a token returns real documents.
* `/code` → hangs on `reading workspace…` forever, 0 files, **no error shown at all**; console shows
  `403 /api/code/roots` and `403 /api/code/files`.

And because the SPA does not do a full page load on navigation, **landing on either route leaves the whole
console session unauthenticated** until the user reloads from a working route. Both are in the left rail,
so both are one click away.

This is a frontend defect with a server cause: the shell builder injects the token for registered routes
and misses the fallback path. **Fix:** inject in the catch-all too, or register real routes.

### F2 — The Runs view transfers 202 KB to render one DAG, because the default is the *full* graph — **P1**

`GET /api/sessions/{id}/graph` returns **202 670 chars**; `?light=true` returns **5 593** for the same
session — a 36× difference. The client uses `light=true` (`api.ts:485`), so this is currently avoided, but
it is one forgotten parameter away from a 200 KB response, and the "186 runs · 406 nodes" dataset is
already large enough that the full graph for a busy session will keep growing.

**Fix:** make `light` the server-side default and require `full=1` to opt in. Removes the failure mode
rather than relying on every call site remembering.

### F3 — Three pollers run independently, on different intervals, over overlapping data — **P2**

`/mission` polls `/api/events` every **3 s**; `/ledger` polls `/api/cost/by_skill` every **15 s**;
/`/scheduler` polls `/api/schedule` every **15 s**. Each view mounts its own poller.

Two consequences:
* **Cost grows with time-on-page, not with work.** A console left open overnight polls `/api/events`
  ~28 800 times, and every one of those re-reads and ANSI-strips both log files server-side (report 05 F6),
  so server cost grows with *log size × poll count*.
* **No shared cache.** Three components interested in "what is happening" each hold their own copy and
  their own interval; there is no single subscriber. This is precisely the gap report 10 identifies as
  the strongest AG-UI adoption candidate.

**Fix:** one shared "live feed" store with a single 3 s poller, and views subscribe to it. Pause polling
when `document.hidden` is true — a console left in a background tab is the worst case.

### F4 — The entry chunk is 266 KB and the initial load costs 24 static requests — **P2**

`index-DFQaDsVD.js` = 266 521 bytes, plus a stylesheet, plus 12 lazy view chunks (react-flow for the DAG,
CodeMirror for the editor, `marked` for markdown). The 12 views *are* code-split via `React.lazy`, which
is correct. But 24 requests on first paint is high for a local app, and the built shell carries:

```html
<script type=speculationrules>
{"prerender":[{"e":"/"},{"where":{"href_matches":"/*"},"e":"/research"}]}
</script>
```

So Chrome prerenders `/` and, for any link matching `/*`, prerenders `/research`. That is a small,
bounded rule — not a prefetch-everything problem — but it does mean a second document render on load.

**Fix:** measure which of the 24 requests are on the critical path before optimising; the obvious win is
confirming the stylesheet and entry are the only blocking resources and that the 12 view chunks are truly
deferred. If `react-flow` and CodeMirror are in the entry rather than their view chunks, splitting them out
is worth ~100 KB on first paint.

### F5 — The document title never changes — **P3**

`<title>Aria · Research</title>` is baked into the shell. All twelve routes report the same title, so a
user with eleven tabs open cannot tell them apart. One `useEffect` per view setting `document.title`.

### F6 — An unescaped server string reached the notifications feed during testing — **P3, verified contained**

An earlier probe wrote `zzprobe-NOTIF <img src=x onerror="window.__pwned=1">` to `/api/notifications`, and
it persists in the live feed permanently (there is no delete route — report 08 A3). React escapes it, so
the console renders it as text. **The legacy `web/` bundle is the thing to check**: `web/app.js` is 64 KB of
vanilla DOM manipulation served unauthenticated, and its Mission/console pages predate the React app. If
any of them assigns server text via `innerHTML`, that payload executes. That check was not completed.

### F7 — The Code page hangs instead of erroring — **P2**

Distinct from F1's cause but the same class of outcome: `/code` shows `reading workspace…` indefinitely
with no error, no timeout, no retry. A failed fetch should render a failure state. Silent indefinite
pending is the worst state to debug and the easiest to fix.

---

## Verified correct

* **Responsive layout is genuinely solid.** All 12 routes at 390 × 844 produced zero horizontal overflow,
  and every collapsed rail icon kept a non-empty `aria-label`. This is better than most consoles and
  should not be regressed.
* **Views are properly code-split.** All 12 are `React.lazy`, so the 266 KB entry is not twelve views'
  worth of code.
* **The polling client is well built.** `api.ts` centralises the launch token, has AbortController support,
  timeout handling, and a `safe()` wrapper that distinguishes `{status:"error"}` bodies from transport
  failures — the contract problems in report 06 are server-side, not client-side.
* **The DAG renders at scale.** A 31-node session with 10 failed nodes rendered, and the inspector shows
  the exact underlying error (`UnboundLocalError: cannot access local variable '_os'`) with inputs,
  outputs and elapsed time. That is genuinely good observability for a failure.
* **Ledger renders correct data** via `/api/cost/by_skill` — 7 rows, 429 calls, 1.4 M tokens, per-skill
  breakdown. (The *agent's* `/api/cost` is the empty one; the client uses the working one.)
* **E2E coverage exists and is substantial** — `console-frontend/e2e/console.spec.ts` is 795 lines against
  a configured `testDir: './e2e'` and `baseURL: 'http://localhost:8500'`.

---

## Recommended work, ordered

| # | Fix | Finding | Effort |
|---|-----|---------|--------|
| 1 | Inject `aria-token` in the SPA catch-all | F1 | 30 min |
| 2 | Render a failure state on `/code` instead of hanging | F7 | 20 min |
| 3 | Default `graph` to `light` server-side | F2 | 10 min |
| 4 | One shared live-feed poller; pause on `document.hidden` | F3 | 3 h |
| 5 | Check `web/*.js` for `innerHTML` on server text; retire the legacy bundle | F6 | 2 h |
| 6 | Confirm react-flow/CodeMirror are in view chunks, not the entry | F4 | 1 h |
| 7 | Per-route `document.title` | F5 | 15 min |
| 8 | Add per-view request/byte instrumentation to the console | — | 2 h |

Item 1 is the same one-line fix as the P0 in the break-test report and is worth doing immediately: two of
twelve nav items are dead, and they break the session for everything else.

## Not measured

Per-view request counts and bytes over a 60 s idle window; MutationObserver render counts per SSE frame;
JS heap and DOM node counts over a 10-minute navigation; whether the DAG virtualises at 180 nodes; polling
overlap between components on the same view; focus order and error announcement on `/console`. All require
the live console, and each should be captured in one instrumented session rather than twelve.