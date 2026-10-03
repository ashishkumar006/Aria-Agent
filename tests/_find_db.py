"""Find the CURRENT gateway call log (the v9.db is stale — from Aug 23).
The live gateway writes elsewhere; locate it by checking open file handles
or common paths."""
import pathlib, time

candidates = []
for pat in ("../../llm_gatewayV9/*.db", "../../llm_gatewayV9/**/*.db",
            "state/*.db", "*.db", "../**/*.db"):
    candidates.extend(pathlib.Path(".").glob(pat))
    candidates.extend(pathlib.Path("../..").glob(pat.replace("../", "", 1) if pat.startswith("..") else pat))

seen = set()
now = time.time()
for p in sorted(set(candidates)):
    if p.suffix != ".db" or str(p) in seen:
        continue
    seen.add(str(p))
    age_h = (now - p.stat().st_mtime) / 3600
    print(f"{str(p):60} size={p.stat().st_size:>10} age={age_h:.1f}h")
