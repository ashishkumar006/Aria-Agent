"""Find where the memory classifier's request actually 503'd. The agent's
memory.py calls LLM().chat(auto_route='memory'). Check: does the gateway
raise HTTPException(503) when the ROUTER POOL has no available provider
BEFORE logging? Search main.py for that raise."""
import pathlib, re
main = (pathlib.Path("../..") / "llm_gatewayV9" / "main.py").read_text(encoding="utf-8")
# All HTTPException(503 raises with context
for m in re.finditer(r".{200}HTTPException\(503.{300}", main, re.DOTALL):
    txt = m.group(0)
    if "router" in txt.lower() or "classify" in txt.lower() or "fallback" in txt.lower():
        print(txt[-450:])
        print("=" * 70)
