"""Live computer-use test — real desktop automation."""
import json
import time
import urllib.request

BASE = "http://localhost:8500"


def ask(query, conv="live-cu-test", timeout=300.0):
    body = json.dumps({"query": query, "conversation_id": conv}).encode()
    req = urllib.request.Request(f"{BASE}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    answer, logs = "", []
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        buf = r.read()
    elapsed = time.time() - t0
    for frame in buf.split(b"\n\n"):
        line = frame.strip()
        if line.startswith(b"data: "):
            try:
                p = json.loads(line[6:])
                if p.get("type") == "done":
                    answer = p.get("answer", "")
                elif p.get("type") == "log":
                    logs.append(p.get("text", ""))
            except json.JSONDecodeError:
                continue
    return answer, logs, elapsed


def test(name, query, checker):
    print(f"\n--- {name} ---")
    print(f"Q: {query}")
    ans, logs, dt = ask(query)
    print(f"A: {ans[:200]}")
    ok = checker(ans)
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] ({dt:.0f}s)")
    return ok


def main():
    print("=" * 60)
    print("LIVE COMPUTER-USE TEST")
    print("=" * 60)

    results = []

    # L0: Shell
    results.append(test(
        "L0 Shell: disk space",
        "Check my disk space using PowerShell. Report the free space on C:.",
        lambda a: any(c.isdigit() for c in a)
    ))

    # L2a: Calculator
    results.append(test(
        "L2a Calculator: 2 + 2",
        "Open Calculator and compute 2 + 2. Tell me the result.",
        lambda a: "4" in a
    ))

    # L2b: Notepad
    results.append(test(
        "L2b Notepad: type hello",
        "Open Notepad and type 'Hello from Aria'. Tell me what you typed.",
        lambda a: "hello" in a.lower() or "notepad" in a.lower()
    ))

    passed = sum(1 for r in results if r)
    print(f"\n{'=' * 60}")
    print(f"RESULT: {passed}/{len(results)} passed")
    print(f"{'=' * 60}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
