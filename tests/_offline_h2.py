"""Suite H2: in-process local API contracts; no gateway/LLM/network calls."""
import sys, os, json
sys.path.insert(0, ".")
os.environ["S9_LLM_PROVIDER"] = ""
os.environ["S9_LLM_MODEL"] = ""

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail if not cond else ""))

try:
    from fastapi.testclient import TestClient
    from agent_server import app
    client = TestClient(app)
    for path in ("/api/health", "/api/audit", "/api/memory?limit=2", "/api/schedule", "/api/templates"):
        response = client.get(path)
        check(f"H2.GET[{path}]", response.status_code == 200 and response.headers.get("content-type", "").startswith("application/json"), f"{response.status_code}")
    response = client.get("/api/health")
    data = response.json()
    check("H2.health.agent", data.get("agent") == "ready", str(data))
    check("H2.health.gateway_field", "gateway_up" in data)
    # local schedule CRUD (does not invoke Executor/gateway)
    response = client.post("/api/schedule", json={"query": "offline API test", "when": "in 1h", "conversation_id": "offline-test"})
    data = response.json()
    sid = data.get("id") or data.get("schedule_id")
    check("H2.schedule.create", response.status_code == 200 and bool(sid), str(data))
    if sid:
        response = client.delete(f"/api/schedule/{sid}")
        check("H2.schedule.delete", response.status_code == 200, response.text[:100])
    # static route functions through TestClient
    for path in ("/", "/app.js", "/style.css"):
        response = client.get(path)
        check(f"H2.static[{path}]", response.status_code == 200 and len(response.content) > 100, f"{response.status_code} {len(response.content)}")
except Exception as e:
    FAIL.append(("H2.setup", f"{type(e).__name__}: {e}"))

print("\n=== SUITE H2: in-process local API contracts ===")
print(f"pass={len(PASS)} fail={len(FAIL)}")
for name, err in FAIL:
    print(f"  FAIL {name}: {err}")
