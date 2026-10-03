"""CONFIRMED: memory classifier uses auto_route='memory' -> RouterPool.
When all router-pool providers are cooling down, _classify_tier's final
fallback path raises HTTPException(503) BEFORE any provider attempt is
logged with status='error' — that's why the DB shows 0 errors while the
agent saw 503s. Find the exact raise in the fallback tail of the function."""
import pathlib, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
main = (pathlib.Path("../..") / "llm_gatewayV9" / "main.py").read_text(encoding="utf-8")
idx = main.find("async def _classify_tier")
seg = main[idx:idx+9000]
# find fallback_used=True returns and any raise
for m in re.finditer(r"(fallback_used=True.*?|raise HTTPException[^\n]*})", seg, re.DOTALL):
    print(m.group(0)[:300])
    print("-" * 60)
