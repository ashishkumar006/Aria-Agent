import json, pathlib
base = pathlib.Path("state/sessions")
# The browser node stores metadata.url, not the prompt. Scan graph.json files.
for sid_dir in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:15]:
    gf = sid_dir / "graph.json"
    if not gf.exists():
        continue
    try:
        g = json.loads(gf.read_text(encoding="utf-8"))
    except Exception:
        continue
    blob = json.dumps(g, default=str)
    if "artificial_intelligence" in blob:
        print("SESSION:", sid_dir.name)
        for nid, nd in g.get("nodes", {}).items():
            md = nd.get("metadata") or {}
            print(f"  {nid}: skill={nd.get('skill')} status={nd.get('status')} url={md.get('url','')}")
        break
