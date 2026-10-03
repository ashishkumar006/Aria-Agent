import os, sys, time
os.environ["COMPUTER_USE_ENABLED"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "S9SharedCode" / "code"))
from computer_use import daemon
from computer_use.layers import deterministic as det

desktop = daemon.call("get_accessibility_tree", {}, timeout=15)
calc = next((w for w in desktop.get("windows",[]) if "calc" in (w.get("title") or "").lower()), None)
pid, wid = calc["pid"], calc["window_id"]
try:
    daemon.call("bring_to_front", {"pid": pid, "window_id": wid}, timeout=10)
except Exception: pass
time.sleep(0.5)

def display_of(tree):
    for line in (tree or "").splitlines():
        if "Display is" in line:
            after = line.split("Display is", 1)[1].strip()
            if after.startswith('"'): after = after[1:]
            q = after.find('"')
            return after[:q].strip() if q >= 0 else after.strip()
    return None

def click_resolve(a, d=0.4):
    fresh = daemon.call("get_window_state", {"pid": pid, "window_id": wid, "include_screenshot": True}, timeout=20)
    snap = fresh.get("snapshot_id")
    elems = fresh.get("elements") or []
    idx = a.get("element_index")
    if a.get("match") and elems:
        new_idx = det.resolve_index(elems, a)
        if new_idx is not None: idx = new_idx
    daemon.call("click", {"pid": pid, "window_id": wid, "element_index": idx, "snapshot_id": snap}, timeout=10)
    time.sleep(d)

for expr, exp in [("2+2","4"), ("234*567","132678"), ("5*5","25")]:
    state = daemon.call("get_window_state", {"pid": pid, "window_id": wid, "include_screenshot": True}, timeout=20)
    plan = det.try_deterministic(f"compute {expr} in Calculator", "Calculator", state=state)
    for a in plan.actions:
        click_resolve(a, 0.5)   # slower clicks
    time.sleep(0.5)
    st = daemon.call("get_window_state", {"pid": pid, "window_id": wid}, timeout=20)
    got = display_of(st.get("tree_markdown",""))
    print(f"  {expr}: got='{got}' ok={exp in got}")
