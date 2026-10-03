"""Quick health check + computer-use wiring test."""
import json
import urllib.request

BASE = "http://localhost:8500"


def ask(query, conv="health-check"):
    body = json.dumps({"query": query, "conversation_id": conv}).encode()
    req = urllib.request.Request(f"{BASE}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    answer = ""
    with urllib.request.urlopen(req, timeout=180) as r:
        buf = r.read()
    for frame in buf.split(b"\n\n"):
        line = frame.strip()
        if line.startswith(b"data: "):
            try:
                p = json.loads(line[6:])
                if p.get("type") == "done":
                    answer = p.get("answer", "")
            except json.JSONDecodeError:
                continue
    return answer


# 1. Agent health check
print("=== Agent Health Check ===")
a = ask("What is 2+2? Just the number.")
print(f"Q: What is 2+2?\nA: {a[:120]}")
ok_simple = "4" in a
print(f"PASS: {ok_simple}")

# 2. Computer-use wiring check
print("\n=== Computer-Use Wiring Check ===")
a = ask("Open Calculator and compute 2 + 2.", "cu-wiring-check")
print(f"Q: Open Calculator and compute 2 + 2.\nA: {a[:200]}")
ok_cu = "4" in a.lower() or "calculator" in a.lower()
print(f"PASS: {ok_cu}")

print("\n=== Summary ===")
print(f"Agent health: {'PASS' if ok_simple else 'FAIL'}")
print(f"Computer-use wiring: {'PASS' if ok_cu else 'FAIL'}")
