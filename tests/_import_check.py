"""Phase 2 import-health harness. Import every module; report failures."""
from __future__ import annotations
import importlib, traceback, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "S9SharedCode" / "code"
sys.path.insert(0, str(ROOT))

# (module_name, is_package_import)
MODULES = [
    "schemas", "gateway", "memory", "perception", "decision", "flow",
    "recovery", "skills", "action", "artifacts", "persistence", "scheduler",
    "templates", "report", "replay", "vector_index", "mcp_runner",
    "mcp_server", "sandbox", "browser.skill", "browser.client", "browser.dom",
    "browser.driver", "browser.highlight",
    "computer_use", "computer_use.engine", "computer_use.safety.gates",
    "computer_use.safety.permissions", "computer_use.daemon",
    "computer_use.shell", "computer_use.apps.native", "computer_use.apps.electron",
    "computer_use.layers.goal", "computer_use.layers.perception",
    "computer_use.layers.sequencing", "computer_use.layers.recovery",
    "computer_use.layers.vision", "computer_use.layers.extract",
    "computer_use.layers.deterministic", "computer_use.core.recording",
]

ok, fail = [], []
for m in MODULES:
    try:
        importlib.import_module(m)
        ok.append(m)
    except Exception as e:
        fail.append((m, f"{type(e).__name__}: {e}"))

print(f"PHASE2: {len(ok)} ok, {len(fail)} failed, {len(MODULES)} total")
for m in ok:
    print(f"  ok: {m}")
for m, err in fail:
    print(f"  FAIL: {m}\n    {err}")
    # print short traceback tail for diagnosis
    tb = traceback.format_exc().strip().splitlines()[-4:]
    for line in tb:
        print(f"      {line}")
