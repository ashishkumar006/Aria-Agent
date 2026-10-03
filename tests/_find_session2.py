import json, pathlib
base = pathlib.Path("state/sessions")
# The AI-summary query ran inside the shared UI thread session; find it by
# scanning node prompts in the most recently modified sessions.
candidates = sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
for sid_dir in candidates:
    nodes_dir = sid_dir / "nodes"
    if not nodes_dir.exists():
        continue
    for f in sorted(nodes_dir.glob("n_*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        prompt = str(d.get("prompt_sent") or "")
        if "artificial_intelligence" in prompt or ("Artificial intelligence" in prompt and d.get("skill") == "browser"):
            print(f"FOUND: {sid_dir.name}/{f.name} skill={d.get('skill')}")
            break
    else:
        continue
    break
