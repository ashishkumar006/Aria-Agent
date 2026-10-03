import json, pathlib
base = pathlib.Path("state/sessions/s8-7c7dda55")
g = json.loads((base / "graph.json").read_text(encoding="utf-8"))
nodes = g.get("nodes", [])
print("graph.json nodes:", len(nodes))
for nd in (nodes if isinstance(nodes, list) else list(nodes.values())):
    if isinstance(nd, dict):
        print(f"  {nd.get('id','?')}: skill={nd.get('skill')} status={nd.get('status')}")
print()
print("Reconstructed chain: planner -> browser -> summariser -> critic -> formatter")
print("(browser bills via cascade layers, so it has no per_agent entry)")
