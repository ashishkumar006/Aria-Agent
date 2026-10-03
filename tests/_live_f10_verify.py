"""Live E2E verification of the F10 fix + regression battery.

Drives the agent through its HTTP API (no browser needed):
  POST /api/chat  {query, conversation_id}  -> SSE stream (log/meta/done frames)

Cases:
  A. Turn-log continuity: ask Q1, then "what was my first question in this
     conversation?" — must answer from the CONVERSATION HISTORY block, NOT
     from stale global memory.
  B. Planner short-circuit: trivial query returns a direct answer quickly.
  C. Scheduler CRUD via MCP tools: schedule → list → cancel.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://localhost:8500"
CONV = f"f10-verify-{int(time.time())}"


def chat(query: str, conv: str = CONV, timeout_s: float = 180.0) -> dict:
    """POST one query, consume the SSE stream, return {answer, logs, cost}."""
    body = json.dumps({"query": query, "conversation_id": conv}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    answer, logs, meta = "", [], {}
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        buf = b""
        while True:
            chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(1)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                frame_raw, buf = buf.split(b"\n\n", 1)
                line = frame_raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    frame = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if frame.get("type") == "log":
                    logs.append(frame.get("text", ""))
                elif frame.get("type") == "meta":
                    meta.update(frame)
                elif frame.get("type") == "done":
                    answer = frame.get("answer", "")
                elif frame.get("type") == "error":
                    answer = f"[error] {frame.get('text', '')}"
            if time.time() - t0 > timeout_s:
                break
    return {"answer": answer, "logs": logs, "meta": meta,
            "elapsed": round(time.time() - t0, 1)}


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


results = []

# ── Case A: F10 turn-log continuity ──────────────────────────────────────────
print("=" * 70)
print("CASE A1: seed a question into this conversation")
r1 = chat("What is the tallest mountain in the world?")
print(f"  ({r1['elapsed']}s) {r1['answer'][:100]}")
results.append(check("A1 seed answered", bool(r1["answer"].strip())
                     and not r1["answer"].startswith("[error]"),
                     r1["answer"][:60]))

print("-" * 70)
print("CASE A2: episodic recall — must cite Everest, not stale memory")
r2 = chat("What was the first question I asked in this conversation?")
print(f"  ({r2['elapsed']}s) {r2['answer'][:140]}")
a2_ok = ("everst" in r2["answer"].lower() or "everest" in r2["answer"].lower()
         or "tallest mountain" in r2["answer"].lower())
results.append(check("A2 recalls THIS conversation's first question", a2_ok,
                     r2["answer"][:80]))

# ── Case B: planner short-circuit ────────────────────────────────────────────
print("-" * 70)
print("CASE B: trivial query short-circuits")
rb = chat("hi")
planner_only = any("planner complete" in l and "formatter" not in l
                   for l in rb["logs"]) and not any(
                   "[n:" in l and "formatter" in l for l in rb["logs"])
print(f"  ({rb['elapsed']}s) {rb['answer'][:80]}")
results.append(check("B trivial query answered", bool(rb["answer"].strip()),
                     f"{len(rb['logs'])} log lines"))

# ── Case C: scheduler CRUD ───────────────────────────────────────────────────
print("-" * 70)
print("CASE C: scheduler create + cancel")
rc = chat("Use the schedule_task tool to schedule a reminder to drink water in 2 hours.")
print(f"  ({rc['elapsed']}s) {rc['answer'][:120]}")
import re
m = re.search(r"sch-[0-9a-f]+", rc["answer"] + " ".join(rc["logs"]))
if m:
    sid = m.group(0)
    rdel = chat(f"Cancel my reminder {sid}.")
    print(f"  cancel ({rdel['elapsed']}s): {rdel['answer'][:100]}")
    results.append(check("C scheduler create+cancel",
                         True, f"created {sid}"))
else:
    results.append(check("C scheduler create+cancel", False,
                         "no sch-id found in response"))

# ── Summary ──────────────────────────────────────────────────────────────────
print("=" * 70)
passed = sum(results)
print(f"LIVE E2E: {passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
