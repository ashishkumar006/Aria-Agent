"""Confirm the memory classifier's 503 path: the agent's memory.py calls
LLM().chat(auto_route='memory') via RouterPool. When every router-pool
provider is in cooldown, main.py raises HTTPException(503) BEFORE any
provider attempt — so nothing lands in the calls table. That matches:
DB shows 0 errors, but agent saw 503s. Also verify which providers were in
the router pool and their cooldown state at that time."""
import pathlib, re
main = (pathlib.Path("../..") / "llm_gatewayV9" / "main.py").read_text(encoding="utf-8")
# find where classify raises 503/HTTPException on total router failure
idx = main.find("async def _classify_tier")
seg = main[idx:idx+6000]
m = re.search(r"(for\s+\w+\s+in.*?router_pool.*?)(?=raise|return)", seg, re.DOTALL)
tail = seg[-1800:]
print(tail)
