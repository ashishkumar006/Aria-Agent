import os, sys, time
os.environ["COMPUTER_USE_ENABLED"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "S9SharedCode" / "code"))
from computer_use.engine import ComputerUseSkill
from computer_use import safety
safety.reset_shared_gates()
sk = ComputerUseSkill(session_id="t", llm_chat=None, llm_vision=None, safety=safety.shared_gates())
for expr, exp in [("2+2", "4"), ("234*567", "132678"), ("5*5", "25")]:
    r = sk.run(f"compute {expr} in Calculator", app_hint="Calculator")
    got = str(r.output.get("display", "")).replace(",", "")
    print(f"  {expr}: got='{got}' ok={exp in got} layer={r.layer}")
    time.sleep(0.3)
