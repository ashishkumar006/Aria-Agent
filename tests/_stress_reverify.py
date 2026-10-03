"""Re-verify the two corrected stress cases (A.worker4 + D.empty_after_trim)."""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
from _stress_test import ask, server_alive  # noqa: E402

stamp = int(time.time())

# Case 1: Portuguese capital — accept English or Spanish answer
ans, dt, ok = ask("¿Cuál es la capital de Portugal?", f"st-fix1-{stamp}")
ok1 = ok and ("lisbon" in ans.lower() or "lisboa" in ans.lower())
mark = "PASS" if ok1 else "FAIL"
print(f"[{mark}] A.worker4_portuguese ({dt:.0f}s): {ans[:80]}")

# Case 2: empty query must be cleanly rejected; server stays healthy
ans, dt, ok = ask("   ", f"st-fix2-{stamp}")
clean = (not ok) and "empty query" in ans.lower()
mark = "PASS" if clean else "FAIL"
print(f"[{mark}] D.empty_after_trim ({dt:.1f}s): rejected={not ok}, msg={ans[:60]!r}")

alive = server_alive()
mark = "PASS" if alive else "FAIL"
print(f"[{mark}] D.server_still_alive")

total_ok = int(ok1) + int(clean) + int(alive)
print(f"\nRE-VERIFY: {total_ok}/3 passed")
sys.exit(0 if total_ok == 3 else 1)
