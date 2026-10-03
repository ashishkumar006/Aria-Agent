import json, pathlib
base = pathlib.Path("state/sessions/s8-7c7dda55")
tc = json.loads((base / "turn_costs.json").read_text(encoding="utf-8"))
for entry in tc:
    q = entry.get("query", "")
    if "Artificial" in q or "artificial_intelligence" in q:
        print("QUERY:", q)
        print(json.dumps(entry, indent=1, default=str)[:2500])
        break
else:
    print("AI-summary turn not in turn_costs; listing all queries:")
    for i, entry in enumerate(tc):
        print(f"{i}: {entry.get('query','')[:80]}")
