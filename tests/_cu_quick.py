"""Quick check: Calculator deterministic fix (no LLM calls)."""
import os, sys, time
os.environ["COMPUTER_USE_ENABLED"] = "true"
os.environ["COMPUTER_USE_MODE"] = "live"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "S9SharedCode" / "code"))
from computer_use.engine import ComputerUseSkill
from computer_use import daemon, safety

def skill():
    safety.reset_shared_gates()
    return ComputerUseSkill(session_id="cu-quick", llm_chat=None, llm_vision=None,
                             safety=safety.shared_gates())

print("=== Calculator L2a (deterministic, no LLM) ===")
for expr, expected in [("2+2", "4"), ("234*567", "132678"), ("5*5", "25")]:
    r = skill().run(f"compute {expr} in Calculator", app_hint="Calculator")
    got = str(r.output.get("display", ""))
    ok = r.success and expected in got
    print(f"  {'PASS' if ok else 'FAIL'} {expr} == {expected}  got='{got}'  layer={r.layer}")
    if not ok:
        print(f"        trace: {' | '.join(r.trace[-4:])}")
    time.sleep(0.4)

print("\n=== capabilities screenshot key fix ===")
daemon.ensure_daemon()
caps = daemon.capabilities()
print(f"  screenshot_ok = {caps.get('screenshot_ok')}")