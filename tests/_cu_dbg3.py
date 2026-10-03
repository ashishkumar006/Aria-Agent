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

# Turn scan (what the engine does)
state = daemon.call("get_window_state", {"pid": pid, "window_id": wid, "include_screenshot": True}, timeout=20)
elems = state.get("elements", [])
print(f"Turn scan: {len(elems)} elements, snapshot={state.get('snapshot_id')}")

plan = det.try_deterministic("compute 2+2 in Calculator", "Calculator", state=state)
print(f"\nPlan actions (resolved from turn scan):")
for a in plan.actions:
    print(f"  type={a['type']} idx={a.get('element_index')} match={a.get('match')}")

# Now simulate what _dispatch_action does: re-fetch fresh + re-resolve
print(f"\nSimulating _dispatch_action re-resolution:")
for a in plan.actions:
    fresh = daemon.call("get_window_state", {"pid": pid, "window_id": wid, "include_screenshot": True}, timeout=20)
    fresh_elems = fresh.get("elements", [])
    if a.get("match"):
        new_idx = det.resolve_index(fresh_elems, a)
        print(f"  match={a['match']} plan_idx={a.get('element_index')} -> resolved_idx={new_idx}")
    time.sleep(0.1)

# What does index 22 (clear) resolve to now?
print(f"\nIndex 22 in turn scan: ", end="")
for e in elems:
    if e.get("element_index") == 22:
        print(f"label='{e.get('label')}' role={e.get('role')}")
        break

# Show all digit/operator button indices in fresh scan
fresh = daemon.call("get_window_state", {"pid": pid, "window_id": wid, "include_screenshot": True}, timeout=20)
print(f"\nFresh scan buttons:")
for e in fresh.get("elements", []):
    lab = (e.get("label") or "").strip()
    if lab and e.get("role") == "Button" and any(k in lab.lower() for k in ("one","two","three","four","five","six","seven","eight","nine","zero","plus","minus","multiply","divide","equal","clear")):
        print(f"  idx={e.get('element_index'):3d} label='{lab}'")
