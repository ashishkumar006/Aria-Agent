#!/usr/bin/env python3
"""Comprehensive end-to-end test suite for the Aria agent.

Tests all major capabilities through the live /api/chat endpoint.
Run: uv run python tests/test_e2e_comprehensive.py
Run single tier: uv run python tests/test_e2e_comprehensive.py --tier 1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "http://localhost:8500"
GATEWAY = "http://localhost:8109"


@dataclass
class Result:
    name: str
    query: str
    tier: int
    ok: bool = False
    answer: str = ""
    elapsed_s: float = 0.0
    error: str = ""
    notes: str = ""
    dag: list[str] = field(default_factory=list)
    cost: dict = field(default_factory=dict)


def check_service(name: str, url: str) -> bool:
    """Check if a service is reachable."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def chat(query: str, conversation_id: str | None = None, timeout: int = 180) -> dict:
    """POST to /api/chat and collect SSE frames."""
    body = json.dumps({"query": query, "conversation_id": conversation_id or ""}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    out = {"answer": "", "elapsed_s": 0.0, "logs": [], "error": "", "session_id": "", "cost": {}}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buf = ""
            for raw in resp:
                buf += raw.decode("utf-8", "replace")
                while "\n\n" in buf:
                    chunk = buf[: buf.index("\n\n")]
                    buf = buf[buf.index("\n\n") + 2:]
                    if not chunk.startswith("data: "):
                        continue
                    try:
                        d = json.loads(chunk[6:])
                    except Exception:
                        continue
                    if d.get("type") == "log":
                        out["logs"].append(d.get("text", ""))
                    elif d.get("type") == "meta":
                        out["elapsed_s"] = d.get("elapsed_s", 0.0)
                        if "cost_usd" in d:
                            out["cost"] = d
                    elif d.get("type") == "done":
                        out["answer"] = d.get("answer", "")
                        out["session_id"] = d.get("session_id", "")
                    elif d.get("type") == "error":
                        out["error"] = d.get("text", "")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def get_session_dag(session_id: str) -> list[str]:
    """Read the DAG skill list from the session's graph.json."""
    graph_path = ROOT / "state" / "sessions" / session_id / "graph.json"
    if not graph_path.exists():
        return []
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        nodes = data.get("nodes", [])
        return [n.get("skill", "?") for n in nodes if n.get("status") == "complete"]
    except Exception:
        return []


def judge(answer: str, keywords: list[str]) -> tuple[bool, str]:
    """Loose pass check: non-empty answer containing at least one keyword."""
    a = (answer or "").lower()
    if not a.strip():
        return False, "empty answer"
    hits = [k for k in keywords if k.lower() in a]
    if hits:
        return True, f"matched: {', '.join(hits[:3])}"
    return False, f"no keyword match (saw {len(a)} chars)"


# ── Test definitions ────────────────────────────────────────────────────────

def t1_1_minimal_path(conv: str) -> Result:
    r = Result("t1_1_minimal", "hi", 1)
    t0 = time.time()
    resp = chat(r.query, conv)
    r.elapsed_s = time.time() - t0
    r.answer = resp["answer"]
    r.dag = get_session_dag(resp.get("session_id", ""))
    if resp["error"]:
        r.error = resp["error"]
        return r
    ok, note = judge(r.answer, ["hi", "hello", "hey", "greetings", "aria"])
    r.ok = ok
    r.notes = note
    return r


def t1_2_single_research(conv: str) -> Result:
    r = Result("t1_2_research", "What is the capital of France?", 1)
    t0 = time.time()
    resp = chat(r.query, conv)
    r.elapsed_s = time.time() - t0
    r.answer = resp["answer"]
    r.dag = get_session_dag(resp.get("session_id", ""))
    if resp["error"]:
        r.error = resp["error"]
        return r
    ok, note = judge(r.answer, ["paris"])
    r.ok = ok
    r.notes = note
    return r


def t1_3_fanout(conv: str) -> Result:
    r = Result("t1_3_fanout", "Compare the population of London, Paris, and Berlin; which two are closest?", 1)
    t0 = time.time()
    resp = chat(r.query, conv)
    r.elapsed_s = time.time() - t0
    r.answer = resp["answer"]
    r.dag = get_session_dag(resp.get("session_id", ""))
    if resp["error"]:
        r.error = resp["error"]
        return r
    ok, note = judge(r.answer, ["london", "paris", "berlin", "population", "closest"])
    r.ok = ok
    r.notes = note
    return r


def t1_5_critic(conv: str) -> Result:
    r = Result("t1_5_critic", "Write a haiku about the ocean, exactly 5-7-5 syllables.", 1)
    t0 = time.time()
    resp = chat(r.query, conv)
    r.elapsed_s = time.time() - t0
    r.answer = resp["answer"]
    r.dag = get_session_dag(resp.get("session_id", ""))
    if resp["error"]:
        r.error = resp["error"]
        return r
    # Check critic is in the DAG
    has_critic = "critic" in r.dag
    ok, note = judge(r.answer, ["ocean", "sea", "wave", "water", "tide"])
    r.ok = ok and has_critic
    r.notes = f"critic_in_dag={has_critic}; {note}"
    return r


def t2_1_browser_extract(conv: str) -> Result:
    r = Result("t2_1_browser_extract", "Summarise https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture) in two sentences.", 2)
    t0 = time.time()
    resp = chat(r.query, conv, timeout=120)
    r.elapsed_s = time.time() - t0
    r.answer = resp["answer"]
    r.dag = get_session_dag(resp.get("session_id", ""))
    if resp["error"]:
        r.error = resp["error"]
        return r
    ok, note = judge(r.answer, ["transformer", "attention", "paper", "vaswani", "neural"])
    r.ok = ok
    r.notes = note
    return r


def t3_1_weather(conv: str) -> Result:
    r = Result("t3_1_weather", "What's the weather in London?", 3)
    t0 = time.time()
    resp = chat(r.query, conv)
    r.elapsed_s = time.time() - t0
    r.answer = resp["answer"]
    r.dag = get_session_dag(resp.get("session_id", ""))
    if resp["error"]:
        r.error = resp["error"]
        return r
    ok, note = judge(r.answer, ["london", "°", "weather", "temp", "celsius", "fahrenheit"])
    r.ok = ok
    r.notes = note
    return r


def t3_2_currency(conv: str) -> Result:
    r = Result("t3_2_currency", "How much is 100 USD in EUR?", 3)
    t0 = time.time()
    resp = chat(r.query, conv)
    r.elapsed_s = time.time() - t0
    r.answer = resp["answer"]
    r.dag = get_session_dag(resp.get("session_id", ""))
    if resp["error"]:
        r.error = resp["error"]
        return r
    ok, note = judge(r.answer, ["eur", "usd", "€", "euro"])
    r.ok = ok
    r.notes = note
    return r


def t3_3_time(conv: str) -> Result:
    r = Result("t3_3_time", "What time is it in Tokyo?", 3)
    t0 = time.time()
    resp = chat(r.query, conv)
    r.elapsed_s = time.time() - t0
    r.answer = resp["answer"]
    r.dag = get_session_dag(resp.get("session_id", ""))
    if resp["error"]:
        r.error = resp["error"]
        return r
    ok, note = judge(r.answer, ["tokyo", "time", "am", "pm", ":"])
    r.ok = ok
    r.notes = note
    return r


def t4_1_disk_usage(conv: str) -> Result:
    r = Result("t4_1_disk_usage", "What is the current disk usage on drive C:?", 4)
    t0 = time.time()
    resp = chat(r.query, conv, timeout=120)
    r.elapsed_s = time.time() - t0
    r.answer = resp["answer"]
    r.dag = get_session_dag(resp.get("session_id", ""))
    if resp["error"]:
        r.error = resp["error"]
        return r
    ok, note = judge(r.answer, ["c:", "gb", "disk", "free", "used", "drive", "storage"])
    r.ok = ok
    r.notes = note
    return r


def t5_1_fibonacci(conv: str) -> Result:
    r = Result("t5_1_fibonacci", "Write Python to compute the first 10 Fibonacci numbers and run it.", 5)
    t0 = time.time()
    resp = chat(r.query, conv, timeout=120)
    r.elapsed_s = time.time() - t0
    r.answer = resp["answer"]
    r.dag = get_session_dag(resp.get("session_id", ""))
    if resp["error"]:
        r.error = resp["error"]
        return r
    ok, note = judge(r.answer, ["fibonacci", "0", "1", "1", "2", "3", "5", "8", "13", "21", "34"])
    r.ok = ok
    r.notes = note
    return r


def t5_2_math(conv: str) -> Result:
    r = Result("t5_2_math", "Calculate 2**100", 5)
    t0 = time.time()
    resp = chat(r.query, conv, timeout=120)
    r.elapsed_s = time.time() - t0
    r.answer = resp["answer"]
    r.dag = get_session_dag(resp.get("session_id", ""))
    if resp["error"]:
        r.error = resp["error"]
        return r
    # 2**100 = 1267650600228229401496703205376
    ok, note = judge(r.answer, ["1267650600228229401496703205376", "2^100", "power"])
    r.ok = ok
    r.notes = note
    return r


# ── Test registry ───────────────────────────────────────────────────────────

ALL_TESTS = [
    t1_1_minimal_path,
    t1_2_single_research,
    t1_3_fanout,
    t1_5_critic,
    t2_1_browser_extract,
    t3_1_weather,
    t3_2_currency,
    t3_3_time,
    t4_1_disk_usage,
    t5_1_fibonacci,
    t5_2_math,
]


def main():
    parser = argparse.ArgumentParser(description="Aria E2E test suite")
    parser.add_argument("--tier", type=int, default=None, help="Run only a specific tier (1-8)")
    parser.add_argument("--test", type=str, default=None, help="Run a specific test by name prefix")
    args = parser.parse_args()

    print("=" * 78)
    print("ARIA AGENT — END-TO-END TEST SUITE")
    print("=" * 78)

    # Pre-flight checks
    print("\n--- Pre-flight ---")
    gw_ok = check_service("Gateway", f"{GATEWAY}/v1/status")
    ag_ok = check_service("Agent", f"{BASE}/api/health")
    print(f"  Gateway (:8109): {'UP' if gw_ok else 'DOWN'}")
    print(f"  Agent   (:8500): {'UP' if ag_ok else 'DOWN'}")
    if not gw_ok or not ag_ok:
        print("\nFATAL: Start both services first:")
        print("  Gateway: cd llm_gatewayV9 && uv run python main.py")
        print("  Agent:   cd S9SharedCode/code && uv run python agent_server.py")
        sys.exit(1)

    # Select tests
    tests = ALL_TESTS
    if args.tier is not None:
        tests = [t for t in tests if t.__name__.startswith(f"t{args.tier}_")]
    if args.test:
        tests = [t for t in tests if args.test.lower() in t.__name__.lower()]

    if not tests:
        print("\nNo tests match the filter.")
        sys.exit(0)

    print(f"\n--- Running {len(tests)} tests ---\n")

    conv = f"e2e_{int(time.time())}"
    results: list[Result] = []
    for i, test_fn in enumerate(tests, 1):
        name = test_fn.__name__
        print(f"[{i}/{len(tests)}] {name} ...", end=" ", flush=True)
        try:
            r = test_fn(conv)
        except Exception as e:
            r = Result(name, "", 0, ok=False, error=f"{type(e).__name__}: {e}")
            r.elapsed_s = 0
        results.append(r)
        status = "PASS" if r.ok else "FAIL"
        print(f"{status} ({r.elapsed_s:.1f}s) {r.notes[:60]}")
        if r.error:
            print(f"       ERROR: {r.error[:80]}")
        if r.dag:
            print(f"       DAG: {' → '.join(r.dag)}")

    # Summary
    print("\n" + "=" * 78)
    passed = sum(1 for r in results if r.ok)
    failed = len(results) - passed
    print(f"RESULTS: {passed}/{len(results)} passed, {failed} failed")
    print("=" * 80)

    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"  [{status}] {r.name:30s} {r.elapsed_s:6.1f}s  {r.notes[:50]}")

    # Write report
    report_path = ROOT / "state" / "e2e_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "passed": passed,
        "total": len(results),
        "results": [
            {
                "name": r.name,
                "tier": r.tier,
                "ok": r.ok,
                "elapsed_s": round(r.elapsed_s, 1),
                "notes": r.notes,
                "dag": r.dag,
                "error": r.error,
                "answer_preview": (r.answer or "")[:200],
            }
            for r in results
        ],
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport written to {report_path}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
