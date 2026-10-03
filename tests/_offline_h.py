"""OFFLINE Suite H: no-LLM API contract checks.
Uses only local HTTP endpoints that do not call /v1/chat or /v1/embed.
Does not call /api/chat or /api/tts.
"""
import urllib.request, json

BASE = "http://localhost:8500"
PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail if not cond else ""))

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return r.status, r.headers.get("Content-Type", ""), json.load(r)

for path in ("/api/health", "/api/audit", "/api/memory?limit=2", "/api/schedule", "/api/templates"):
    try:
        status, ct, data = get(path)
        check(f"H.GET[{path}]", status == 200 and "json" in ct.lower(), f"{status} {ct}")
    except Exception as e:
        check(f"H.GET[{path}]", False, f"{type(e).__name__}: {e}")

# health shape
try:
    _, _, health = get("/api/health")
    check("H.health.agent", health.get("agent") == "ready", str(health))
    check("H.health.gateway_field", "gateway_up" in health)
except Exception:
    pass

# schedule CRUD is local only; create then delete
try:
    payload = json.dumps({"query": "offline API test", "when": "in 1h", "conversation_id": "offline-test"}).encode()
    req = urllib.request.Request(BASE + "/api/schedule", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        status, data = r.status, json.load(r)
    sid = data.get("id") or data.get("schedule_id")
    check("H.schedule.create", status == 200 and bool(sid), str(data))
    if sid:
        req = urllib.request.Request(BASE + "/api/schedule/" + sid, method="DELETE")
        with urllib.request.urlopen(req, timeout=15) as r:
            deleted = json.load(r)
        check("H.schedule.delete", r.status == 200, str(deleted))
except Exception as e:
    check("H.schedule.crud", False, f"{type(e).__name__}: {e}")

# static endpoints
for path, expected in (("/", "text/html"), ("/app.js", "javascript"), ("/style.css", "text/css")):
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as r:
            body = r.read()
            check(f"H.static[{path}]", r.status == 200 and len(body) > 100, f"{r.status} {len(body)}")
    except Exception as e:
        check(f"H.static[{path}]", False, f"{type(e).__name__}: {e}")

print("\n=== SUITE H: no-LLM local API contracts ===")
print(f"pass={len(PASS)} fail={len(FAIL)}")
for name, err in FAIL:
    print(f"  FAIL {name}: {err}")
