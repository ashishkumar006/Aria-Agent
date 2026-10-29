# Known Bugs & Issues

Tracked from E2E testing (see `E2E_TEST_PLAN.md` §12).

## BUG-001: Parallel sibling nodes crash with `ExceptionGroup` (§8.2)
- **Severity:** High (breaks fan-out, a core routing feature)
- **Symptom:** When the planner fans out N parallel nodes (e.g. 3 `researcher`
  for a multi-subject compare), 1–2 siblings die with
  `ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)` during
  the parallel `asyncio.gather` in `flow.py`.
- **Evidence:** `e2e-82b` / `s8-096edb88` — planner emitted n:2/n:3/n:4
  (London/Paris/Berlin); n:3 & n:4 failed; recovery planner re-emitted as
  n:7/n:8. `graph.json` shows `failure_report: ... ExceptionGroup: unhandled
  errors in a TaskGroup`.
- **Root cause:** `flow.py` ~line 291 `asyncio.gather(*[_run_one(...) for nid
  in ready])` — a `TaskGroup` exception inside one `_run_one` (or the skill it
  calls) propagates out of `gather` and aborts the whole batch, instead of
  being isolated per-node.
- **Fix direction:** isolate per-sibling failure — either
  `asyncio.gather(*tasks, return_exceptions=True)` and mark each failed node
  individually, or wrap each `_run_one` call in try/except so one sibling's
  `ExceptionGroup` becomes that node's `AgentResult(success=False, error=...)`
  rather than killing the batch. Recovery path already handles a single failed
  node correctly; the bug is only the *parallel* propagation.
- **Fix applied (2026-08-21):** `flow.py` executor loop now uses
  `asyncio.gather(..., return_exceptions=True)`; any `BaseException` returned
  is converted to a failed `AgentResult` for that node (marked failed,
  individually recoverable) instead of aborting the batch.
- **Verified:** `e2e-131` / `s8-43e6447e` — 3 parallel researchers (London/
  Paris/Berlin) all `status=complete`, **0 recovery nodes** (previously 2 of 3
  died with `ExceptionGroup`). §1.3 now fully green.
- **Status:** **FIXED.**

## BUG-002: Local Qwen2.5-1.5B a11y unreliable on multi-turn (§10)
- **Severity:** Medium (only affects the optional local-a11y path; not wired in)
- **Symptom:** On a 5-turn browser task the local model drifted to prose
  (`thinking: ...`) with no JSON, then got stuck clicking the same mark.
- **Fix direction:** hardened a11y prompt + JSON normalizer (strip fences,
  coerce `mark`→int, drop unknown `type`) + no-op-retry (re-enumerate, confirm
  page changed). Needs GPU/Q4 quant to be fast enough on this laptop.
- **Status:** **WON'T FIX — user decided not to use the local Qwen model
  (2026-08-21).** Benchmark data in `E2E_TEST_PLAN.md` §10 kept for reference
  only. Browser skill stays on the gateway a11y path.

## BUG-003: Offline gated-shell inert without vision judge (§8.1)
- **Severity:** Low (graceful `error`, no 500)
- **Symptom:** `_shell_intent_offline` returns `None` when no vision model is
  configured, so the offline answer degrades to a graceful error rather than a
  shell answer.
- **Fix direction:** plug a local `Qwen2.5-VL-3B` in as the vision judge (note:
  separate from the text-only a11y use case in BUG-002).
- **Status:** Open.

## Skipped / unexercised
- §3 LIVE actions (Telegram/email/calendar/weather/GitHub) — no credentials.
- §8.3 (malformed skill JSON), §8.4 (MAX_NODES cap) — not exercised.
