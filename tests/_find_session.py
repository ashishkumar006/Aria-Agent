import json, pathlib
base = pathlib.Path("state/sessions")
target = "artificial_intelligence"
alt = "two sentences"
hits = []
for sid_dir in base.iterdir():
    if not sid_dir.is_dir():
        continue
    qf = sid_dir / "query.txt"
    if qf.exists():
        try:
            q = qf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if target in q.lower() or ("summarise" in q.lower() and alt in q.lower()):
            hits.append((sid_dir.name, q[:90]))
for name, q in hits:
    print(name, "|", q)
print(f"\n{len(hits)} matching session(s)")
