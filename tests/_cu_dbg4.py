import os, sys, time
os.environ["COMPUTER_USE_ENABLED"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "S9SharedCode" / "code"))
from computer_use import daemon, safety
from computer_use.engine import ComputerUseSkill
from computer_use.layers import deterministic as det

# Check fix is loaded
import inspect
src = inspect.getsource(det._find_calc_button)
print("memory guard present:", "memory" in src)
print('"add" still in want:', '"add"' in src)

safety.reset_shared_gates()
sk = ComputerUseSkill(session_id="dbg", llm_chat=None, llm_vision=None, safety=safety.shared_gates())
r = sk.run("compute 2+2 in Calculator", app_hint="Calculator")
print("\nRESULT:", r.output.get("display"), "layer=", r.layer)
print("TRACE:")
for t in r.trace:
    print("  ", t)
