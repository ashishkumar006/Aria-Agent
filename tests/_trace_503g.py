"""The gateway DB shows ZERO errors in the window, yet the agent saw 503s on
its memory-classifier calls. Those classifier calls go through RouterPool with
auto_route='memory'. When ALL router-pool providers are cooling down, main.py
raises HTTPException(503) BEFORE any provider attempt is logged — so the 503
never appears in the calls table. Verify: check router pool order and whether
a 503 from that path is logged."""
import re, pathlib
main = (pathlib.Path("../..") / "llm_gatewayV9" / "main.py").read_text(encoding="utf-8")
# find where router_pool pick fails -> 503
idx = main.find("router_pool")
print("router_pool references:", len(re.findall(r"router_pool", main)))
# find the classify_tier fallback and error path
m = re.search(r"all router.*?failed|fallback_used", main)
print("fallback marker found:", bool(m))
# Check: does a failed _classify_tier get logged to db?
seg = main[main.find("async def _classify_tier"):main.find("async def _classify_tier")+3000]
print("\n_classify_tier excerpt:")
print(seg[:1500])
