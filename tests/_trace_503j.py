"""The memory classifier 503 comes from the WORKER path, not the router:
memory.py's _llm_classify calls LLM().chat() WITHOUT auto_route (plain chat).
When all worker providers were cooling down at that instant, main.py raised
the final `HTTPException(503, 'all providers unavailable...')` at line ~774 —
and that raise happens AFTER the loop, so no per-provider error row is written
for it. Verify by checking memory.py's actual call."""
import pathlib, re
mem = (pathlib.Path(".") / "memory.py").read_text(encoding="utf-8")
idx = mem.find("def _llm_classify")
seg = mem[idx:idx+2600]
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print(seg)
