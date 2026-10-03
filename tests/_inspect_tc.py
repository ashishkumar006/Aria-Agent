import json, pathlib
base = pathlib.Path("state/sessions/s8-7c7dda55")
tc = json.loads((base / "turn_costs.json").read_text(encoding="utf-8"))
print(type(tc).__name__, "| entries:", len(tc) if hasattr(tc, "__len__") else "?")
print(json.dumps(tc, indent=1, default=str)[:3000])
