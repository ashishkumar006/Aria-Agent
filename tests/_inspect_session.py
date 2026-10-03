import json, pathlib
base = pathlib.Path("state/sessions/s8-7c7dda55")
print("QUERY:", (base / "query.txt").read_text(encoding="utf-8"))
print()
for f in sorted((base / "nodes").glob("n_*.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    res = d.get("result") or {}
    err = str(res.get("error") or "")[:110]
    print(f"{f.name}: skill={d.get('skill')} status={d.get('status')} err={err}")
print()
# dump the summariser + critic + formatter outputs
for f in sorted((base / "nodes").glob("n_*.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    if d.get("skill") in ("summariser", "critic", "formatter"):
        out = (d.get("result") or {}).get("output") or {}
        print(f"--- {f.name} ({d.get('skill')}) output ---")
        print(json.dumps(out, indent=1, default=str)[:900])
        print()
