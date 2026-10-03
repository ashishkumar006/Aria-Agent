"""OFFLINE test suite — zero gateway/LLM calls.
Suite A: import & syntax validation of every module.
"""
import sys, io, importlib, traceback, json, os
sys.path.insert(0, ".")

os.environ.setdefault("S9_LLM_PROVIDER", "")
os.environ.setdefault("S9_LLM_MODEL", "")

PASS, FAIL = [], []

MODULES = [
    "schemas", "persistence", "memory", "scheduler", "recovery",
    "sandbox", "skills", "flow", "mcp_runner", "mcp_server",
    "gateway", "agent_server",
]

for mod in MODULES:
    try:
        importlib.import_module(mod)
        PASS.append(f"A.import:{mod}")
    except Exception as e:
        FAIL.append((f"A.import:{mod}", f"{type(e).__name__}: {e}"))

# browser subpackage
for bm in ("browser", "browser.skill", "browser.client"):
    try:
        importlib.import_module(bm)
        PASS.append(f"A.import:{bm}")
    except Exception as e:
        FAIL.append((f"A.import:{bm}", f"{type(e).__name__}: {e}"))

# computer_use (optional daemon deps — import only)
try:
    importlib.import_module("computer_use.core")
    PASS.append("A.import:computer_use.core")
except Exception as e:
    # acceptable if daemon-only deps missing; record but don't fail hard
    print(f"[note] computer_use.core import: {type(e).__name__}: {str(e)[:80]}")

print("\n=== SUITE A: imports ===")
print(f"pass={len(PASS)} fail={len(FAIL)}")
for name, err in FAIL:
    print(f"  FAIL {name}: {err}")
