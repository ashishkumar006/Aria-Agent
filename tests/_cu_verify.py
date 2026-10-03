"""Custom verification of the computer-use desktop layer (not the repo's tests).

Drives ComputerUseSkill.run() directly against the live desktop via cua-driver:
  - daemon reachable
  - Calculator arithmetic (L2a deterministic) — 3 expressions
  - Notepad write+save (L2a deterministic)
  - L0 gated-shell fallback path (read_file / open_app) — no daemon needed
  - dry-run mode produces a plan without touching the desktop
"""
from __future__ import annotations
import os, sys, time, tempfile, shutil
os.environ["COMPUTER_USE_ENABLED"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "S9SharedCode" / "code"))
from computer_use import daemon, engine, safety

def banner(t): print(f"\n{'='*60}\n{t}\n{'='*60}")

# 1. daemon reachable
banner("1. daemon reachable")
ok = daemon.ensure_daemon()
print(f"  ensure_daemon -> {ok}")
try:
    tools = daemon.call("list_tools", {}, timeout=10)
    names = [t.get("name") for t in (tools if isinstance(tools, list) else tools.get("tools", []))]
    print(f"  tools ({len(names)}): {names[:8]}")
except Exception as e:
    print(f"  list_tools: {e}")

# 2. Calculator arithmetic via the engine (L2a deterministic)
banner("2. Calculator arithmetic (L2a deterministic)")
sk = engine.ComputerUseSkill()
for expr, expected in [("2+2", "4"), ("234*567", "132678"), ("5*5", "25")]:
    r = sk.run(f"compute {expr} in Calculator", app_hint="Calculator", max_turns=6)
    disp = r.output.get("display", "").replace(",", "")
    ok = r.success and expected in disp
    print(f"  {expr}: layer={r.layer} display='{disp}' ok={ok}" + (f" err={r.error}" if not ok else ""))

# 3. Notepad write+save (L2a deterministic)
banner("3. Notepad write+save (L2a deterministic)")
tmp = tempfile.mkdtemp()
try:
    r = sk.run("write 'hello desktop' in Notepad and save as demo.txt",
               app_hint="Notepad", max_turns=8)
    print(f"  notepad: layer={r.layer} success={r.success} desc={r.output.get('plan','')[:60]}")
    # verify the file landed
    for root, _, files in os.walk(tmp):
        for f in files:
            if f == "demo.txt":
                p = os.path.join(root, f)
                print(f"  file found: {p} content={open(p).read()!r}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 4. L0 gated-shell fallback (no daemon needed)
banner("4. L0 gated-shell fallback")
sk0 = engine.ComputerUseSkill()
res = sk0.shell_read_file("C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/README.md" if os.path.exists("C:/Users/AISHWARYA/Downloads/project3/S9SharedCode/code/README.md") else "C:/Windows/System32/notepad.exe")
print(f"  read_file -> {res['status']} ({res.get('bytes', len(res.get('content','')))} bytes)")
res2 = sk0.shell_open_app("notepad")
print(f"  open_app notepad -> {res2['status']}")

# 5. dry-run: plan produced, nothing executed
banner("5. dry-run mode")
sk_dry = engine.ComputerUseSkill(safety=safety.SafetyGates(mode="dry-run"))
r = sk_dry.run("compute 2+2 in Calculator", app_hint="Calculator", max_turns=6)
plan = r.output.get("dry_run_plan", [])
print(f"  dry-run success={r.success} plan_steps={len(plan)}")
for step in plan[:6]:
    print(f"    {step}")

print("\n" + "="*60)
print("COMPUTER-USE VERIFICATION COMPLETE")
print("="*60)