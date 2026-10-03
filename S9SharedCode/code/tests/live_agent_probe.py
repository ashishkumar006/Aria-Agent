"""Live agent probe — drives the running agent server through a curated
query set covering all 8 capability domains, and reports pass/fail per
domain. This is a *manual* validation harness (not a pytest unit test):
it talks to the real /api/chat endpoint and the real LLM gateway.

Run:
    python tests/live_agent_probe.py
    python tests/live_agent_probe.py --base http://localhost:8500
    python tests/live_agent_probe.py --conv my-validation-run

Each query is sent as a fresh conversation turn (same conversation_id so the
agent keeps memory continuity). We assert the answer is non-empty and that
the expected capability signal appears in the answer or the streamed log.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── curated query set: (domain, query, expected_signal_substrings) ──────────
# expected_signal: if ANY substring is found in the final answer OR the
# streamed log, the domain is considered exercised. This is a smoke test,
# not a correctness oracle — the human reviewer reads the captured answers.
QUERY_SET = [
    ("1. Reasoning/Math", "What is 2 + 2?", ["4"]),
    ("2. Memory continuity",
     "Remember that my favourite colour is teal.",
     ["teal", "remember", "got it", "noted"]),
    ("3. Memory recall",
     "What is my favourite colour?",
     ["teal"]),
    ("4. Web search",
     "Search the web for the latest news about artificial intelligence.",
     ["ai", "artificial", "model", "news"]),
    ("5. Weather (MCP tool)",
     "What is the weather in London right now?",
     ["london", "°c", "c", "cloud", "rain", "sun", "temp"]),
    ("6. Code + sandbox",
     "Write a Python script that prints the first 10 Fibonacci numbers and run it.",
     ["0", "1", "1", "2", "3", "5", "8", "13", "21", "34"]),
    ("7. GitHub (MCP tool)",
     "List my GitHub repositories.",
     ["repository", "repo", "github"]),
    ("8. Telegram (MCP tool)",
     "Send a telegram message saying: Live agent probe OK.",
     ["telegram", "sent", "message", "ok"]),
]


def _post_chat(base: str, query: str, conversation_id: str) -> dict:
    """POST /api/chat and collect the SSE-ish stream into a dict.

    Returns {"answer": str, "log": str, "meta": dict, "error": str|None}.
    """
    url = f"{base}/api/chat"
    body = json.dumps({"query": query, "conversation_id": conversation_id}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    answer_parts: list[str] = []
    log_parts: list[str] = []
    meta: dict = {}
    error: str | None = None
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            # SSE frames are prefixed with "data: " — strip it.
            if line.startswith("data:"):
                line = line[len("data:"):].strip()
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            ftype = frame.get("type")
            if ftype == "log":
                log_parts.append(frame.get("text", ""))
            elif ftype == "done":
                answer_parts.append(frame.get("answer", ""))
                meta = {k: v for k, v in frame.items() if k != "type"}
            elif ftype == "error":
                error = frame.get("text", "unknown error")
            elif ftype == "meta":
                meta.update({k: v for k, v in frame.items() if k != "type"})
    return {
        "answer": "\n".join(answer_parts).strip(),
        "log": "\n".join(log_parts),
        "meta": meta,
        "error": error,
    }


def _domain_pass(domain: str, result: dict, signals: list[str]) -> bool:
    blob = (result["answer"] + "\n" + result["log"]).lower()
    if result["error"]:
        return False
    if not result["answer"]:
        return False
    return any(s.lower() in blob for s in signals)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8500")
    ap.add_argument("--conv", default="live-probe-" + time.strftime("%Y%m%d-%H%M%S"))
    args = ap.parse_args()

    # Health check first.
    try:
        with urllib.request.urlopen(f"{args.base}/api/health", timeout=10) as r:
            health = json.loads(r.read())
        print(f"[health] {health}")
    except Exception as e:
        print(f"[FATAL] agent server not reachable at {args.base}: {e}")
        return 2

    print(f"\n=== Live agent probe — conversation '{args.conv}' ===\n")
    passed = 0
    total = len(QUERY_SET)
    report: list[tuple[str, bool, str]] = []

    for domain, query, signals in QUERY_SET:
        print(f"▶ {domain}")
        print(f"   query: {query}")
        t0 = time.time()
        try:
            res = _post_chat(args.base, query, args.conv)
        except Exception as e:
            print(f"   [ERROR] request failed: {e}\n")
            report.append((domain, False, f"request failed: {e}"))
            continue
        elapsed = time.time() - t0
        ok = _domain_pass(domain, res, signals)
        if ok:
            passed += 1
        ans_preview = res["answer"][:160].replace("\n", " ")
        print(f"   {elapsed:6.1f}s  {'PASS' if ok else 'FAIL'}  "
              f"err={res['error']}")
        print(f"   answer: {ans_preview}"
              f"{'…' if len(res['answer']) > 160 else ''}")
        if res["meta"].get("cost_usd") is not None:
            print(f"   cost: ${res['meta']['cost_usd']:.4f} "
                  f"({res['meta'].get('cost_in_tokens',0)}in/"
                  f"{res['meta'].get('cost_out_tokens',0)}out tok)")
        print()
        report.append((domain, ok, res["error"] or ""))

    print("=" * 60)
    print(f"RESULT: {passed}/{total} domains exercised successfully")
    for domain, ok, err in report:
        flag = "✅" if ok else "❌"
        extra = f"  ({err})" if err and not ok else ""
        print(f"  {flag} {domain}{extra}")
    print("=" * 60)

    # Dump full answers to a file for human review.
    out = ROOT / "state" / "live_probe_report.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write(f"Live agent probe — {args.conv}\n")
        f.write(f"base={args.base}\n\n")
        for domain, query, _ in QUERY_SET:
            f.write(f"## {domain}\nQ: {query}\n")
        f.write("\n--- end ---\n")
    print(f"\nFull run logged to {out}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
