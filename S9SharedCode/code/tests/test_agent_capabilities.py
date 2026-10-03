"""End-to-end capability test for the Aria agent via the live /api/chat SSE
endpoint. Exercises a wide variety of task types and records pass/fail +
timing + token usage so we can see how the agent behaves across the range.

Run:  uv run python tests/test_agent_capabilities.py
(Requires agent_server.py running on :8500 and the V9 gateway on :8109.)
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field

BASE = "http://localhost:8500"


@dataclass
class Result:
    name: str
    query: str
    ok: bool = False
    answer: str = ""
    elapsed_s: float = 0.0
    error: str = ""
    notes: str = ""


def run_chat(query: str, conversation_id: str | None = None, timeout: int = 180) -> dict:
    """POST to /api/chat and collect the SSE frames. Returns a dict with
    answer, elapsed_s, logs (list), error."""
    body = json.dumps({"query": query, "conversation_id": conversation_id or ""}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    out = {"answer": "", "elapsed_s": 0.0, "logs": [], "error": ""}
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
                    elif d.get("type") == "done":
                        out["answer"] = d.get("answer", "")
                    elif d.get("type") == "error":
                        out["error"] = d.get("text", "")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def judge(answer: str, keywords: list[str]) -> tuple[bool, str]:
    """Loose pass check: non-empty answer that contains at least one of the
    expected keywords (case-insensitive)."""
    a = (answer or "").lower()
    if not a.strip():
        return False, "empty answer"
    hits = [k for k in keywords if k.lower() in a]
    if hits:
        return True, f"matched: {', '.join(hits[:3])}"
    return False, f"no keyword match (saw {len(a)} chars)"


TASKS = [
    # name, query, expected-keyword hints
    ("research", "Summarise the benefits of vector databases for RAG in 3 bullet points.",
     ["vector", "rag", "embed", "retrieval"]),
    ("web_fetch", "Fetch the page at https://example.com and tell me the page title and one sentence about it.",
     ["example", "title", "page"]),
    ("code_gen", "Write a Python function that computes the Fibonacci sequence up to n using memoization, and show an example call.",
     ["def ", "fib", "memo", "return"]),
    ("file_create", "Create a file at state/test_hello.txt containing the line 'hello from aria'.",
     ["created", "hello", "file"]),
    ("file_read", "Read the file state/test_hello.txt and tell me its contents.",
     ["hello from aria"]),
    ("weather", "What's the weather like in London right now?",
     ["london", "°", "weather", "temp"]),
    ("currency", "How much is 100 USD in EUR?",
     ["eur", "usd", "100", "€"]),
    ("math", "What is 17 * 23 + 4?",
     ["395", "395"]),
    ("explain", "Explain what a computer-use agent is in two sentences.",
     ["agent", "computer", "desktop", "automate"]),
    ("list_repos", "List my GitHub repositories.",
     ["repo", "github", "repository"]),
    ("vision_file", "Look at the image at state/test_image.png and describe what's in it.",
     ["image", "picture", "see", "shows"]),
    ("open_app", "Open Notepad.",
     ["notepad", "opened", "app"]),
    ("disk_usage", "What is the current disk usage on drive C:?",
     ["c:", "gb", "disk", "free", "used"]),
    ("multi_step", "Research the difference between SQL and NoSQL databases, then write a short summary comparing them.",
     ["sql", "nosql", "database", "compare"]),
]


def main():
    print(f"=== Aria capability sweep ({len(TASKS)} tasks) ===\n")
    results: list[Result] = []
    thread = f"test_{int(time.time())}"
    for i, (name, query, kws) in enumerate(TASKS, 1):
        print(f"[{i}/{len(TASKS)}] {name}: {query[:60]}…")
        t0 = time.time()
        r = run_chat(query, conversation_id=thread)
        dt = time.time() - t0
        ok, note = judge(r["answer"], kws)
        if r["error"]:
            ok, note = False, f"error: {r['error'][:80]}"
        res = Result(name=name, query=query, ok=ok, answer=r["answer"][:200],
                     elapsed_s=round(dt, 1), error=r["error"], notes=note)
        results.append(res)
        print(f"    -> {'PASS' if ok else 'FAIL'} ({dt:.1f}s) {note}")
        print(f"    ans: {r['answer'][:120].replace(chr(10), ' ')}")

    passed = sum(1 for r in results if r.ok)
    print(f"\n=== SUMMARY: {passed}/{len(results)} passed ===")
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        print(f"  [{mark}] {r.name:12s} {r.elapsed_s:6.1f}s  {r.notes}")
    # write a report
    report = {
        "passed": passed, "total": len(results),
        "results": [
            {"name": r.name, "ok": r.ok, "elapsed_s": r.elapsed_s,
             "notes": r.notes, "answer_len": len(r.answer)}
            for r in results
        ],
    }
    with open("state/capability_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print("\nReport written to state/capability_report.json")


if __name__ == "__main__":
    main()
