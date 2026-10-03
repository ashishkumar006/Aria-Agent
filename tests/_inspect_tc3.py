import json, pathlib
base = pathlib.Path("state/sessions/s8-7c7dda55")
# The shared session overwrites nodes/ per turn. The AI-summary turn's node
# outputs are gone, but graph.json may retain the last DAG. Check it, then
# reconstruct what the formatter saw from the turn_costs agent list.
g = json.loads((base / "graph.json").read_text(encoding="utf-8"))
print("graph.json nodes:")
for nid in g.get("nodes", {}):
    nd = g["nodes"][nid]
    print(f"  {nid}: skill={nd.get('skill')} status={nd.get('status')}")
print()
# Reconstruct: 4 LLM calls = planner, browser(no LLM cost), summariser, critic, formatter
print("Reconstructed chain: planner -> browser -> summariser -> critic -> formatter")
print("browser has no entry in per_agent because its cascade layers bill separately")
print()
# The 503s: check gateway call log for this window (ts=1787601152 ≈ start)
import urllib.request
try:
    d = json.load(urllib.request.urlopen("http://localhost:8109/v1/calls?limit=200", timeout=10))
    calls = d if isinstance(d, list) else d.get("calls", [])
    errs = [c for c in calls if c.get("status") == "error"]
    print(f"gateway recent errors: {len(errs)}")
    for c in errs[:8]:
        print(f"  {c.get('provider')} | {str(c.get('error'))[:100]}")
except Exception as e:
    print("gateway log unavailable:", e)
