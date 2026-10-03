import json, pathlib
base = pathlib.Path("state/sessions")
# The shared UI thread reuses one session; the AI-summary run's nodes are in
# the most recent session with a browser node. Scan ALL sessions from today.
found = []
for sid_dir in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:15]:
    nodes_dir = sid_dir / "nodes"
    if not nodes_dir.exists():
        continue
    for f in sorted(nodes_dir.glob("n_*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        blob = json.dumps(d, default=str)
        if "artificial_intelligence" in blob or "Artificial intelligence" in blob:
            found.append((sid_dir.name, f.name, d.get("skill")))
for s in found[:10]:
    print(s)
print(f"\n{len(found)} matching nodes")
