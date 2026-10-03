import os, sys, time
os.environ["COMPUTER_USE_ENABLED"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "S9SharedCode" / "code"))
from computer_use import daemon, safety
from computer_use.engine import ComputerUseSkill
from computer_use.layers import deterministic as det

desktop = daemon.call("get_accessibility_tree", {}, timeout=15)
calc = next((w for w in desktop.get("windows",[]) if "calc" in (w.get("title") or "").lower()), None)
pid, wid = calc["pid"], calc["window_id"]
try:
    daemon.call("bring_to_front", {"pid": pid, "window_id": wid}, timeout=10)
except Exception: pass
time.sleep(0.5)

# Replicate engine: scan, build plan, dispatch with re-resolution
state = daemon.call("get_window_state", {"pid": pid, "window_id": wid, "include_screenshot": True}, timeout=20)
snap0 = state.get("snapshot_id")
plan = det.try_deterministic("compute 2+2 in Calculator", "Calculator", state=state)

print("Plan actions:")
for a in plan.actions:
    print(f"  idx={a.get('element_index')} match={a.get('match')}")

print(f"\nDispatching (re-resolving per click like engine):")
for a in plan.actions:
    # _dispatch_action click path: fresh snapshot + re-resolve
    fresh = daemon.call("get_window_state", {"pid": pid, "window_id": wid, "include_screenshot": True}, timeout=20)
    snap = fresh.get("snapshot_id")
    elems = fresh.get("elements") or []
    idx = a.get("element_index")
    if a.get("match") and elems:
        new_idx = det.resolve_index(elems, a)
        if new_idx is not None:
            idx = new_idx
    # What label is at this idx?
    lbl = ""
    for e in elems:
        if e.get("element_index") == idx:
            lbl = e.get("label", "")
            break
    print(f"  click idx={idx} (match={a.get('match')}) -> label='{lbl}'")
    daemon.call("click", {"pid": pid, "window_id": wid, "element_index": idx, "snapshot_id": snap}, timeout=10)
    time.sleep(0.3)

time.sleep(0.5)
st = daemon.call("get_window_state", {"pid": pid, "window_id": wid}, timeout=20)
for line in (st.get("tree_markdown") or "").splitlines():
    if "Display is" in line:
        print(f"\nFINAL: {line.strip()}")
        break
