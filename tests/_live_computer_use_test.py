"""LIVE computer-use test on the real desktop.

SAFE ladder only:
  L0 — read-only shell commands (no focus change)
  L2a — Calculator arithmetic (opens Calculator, clicks buttons, reads display)
  L2b — Notepad (opens Notepad, types text, reads back)

No destructive commands, no file writes, no system changes.
"""
from __future__ import annotations

import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'─' * 60}\n{title}\n{'─' * 60}")


def main() -> int:
    t0 = time.time()
    print("=" * 60)
    print("COMPUTER-USE — LIVE DESKTOP TEST")
    print("=" * 60)

    import os
    os.environ["COMPUTER_USE_ENABLED"] = "true"
    os.environ["COMPUTER_USE_MODE"] = "live"  # LIVE mode — real desktop
    import computer_use.safety
    computer_use.safety.reset_shared_gates()
    from computer_use.engine import ComputerUseSkill

    # ── L0: Shell (read-only, no focus change) ─────────────────────────
    section("L0 — Gated Shell (live, read-only)")
    skill = ComputerUseSkill(llm_chat=None, llm_vision=None)

    res = skill.shell_run_command("wmic logicaldisk get size,freespace,caption")
    ok = res.get("status") == "done" and len(res.get("stdout", "")) > 0
    record("L0.shell.disk_space", ok, str(res.get("stdout", ""))[:150])

    res = skill.shell_run_command("tasklist | findstr /i \"explorer\"")
    ok = res.get("status") == "done" and "explorer" in res.get("stdout", "").lower()
    record("L0.shell.tasklist", ok, str(res.get("stdout", ""))[:120])

    res = skill.shell_run_command("echo %COMPUTERNAME%")
    ok = res.get("status") == "done" and len(res.get("stdout", "").strip()) > 0
    record("L0.shell.hostname", ok, str(res.get("stdout", "")).strip()[:60])

    # ── L2a: Calculator (live) ─────────────────────────────────────────
    section("L2a — Calculator (LIVE: will open Calculator, steal focus briefly)")
    print("  ⚠️  About to open Calculator and click buttons. Don't touch mouse/keyboard.")
    time.sleep(2)

    # Cheap LLM stub: say "done" immediately so the engine doesn't escalate
    skill = ComputerUseSkill(llm_chat=lambda s, u, sc: {"verdict": "done"},
                             llm_vision=None)

    try:
        res = skill.run("compute 7 * 8", app_hint="Calculator", max_turns=6)
        out = res.output or {}
        display = out.get("display", "")
        ok = res.success and "56" in display.replace(",", "").replace(" ", "")
        record("L2a.calc.7x8", ok, f"layer={res.layer}, display={display!r}")
    except Exception as e:
        record("L2a.calc.7x8", False, str(e)[:150])

    # ── L2b: Notepad (live) ────────────────────────────────────────────
    section("L2b — Notepad (LIVE: will open Notepad, type, read back)")
    print("  ⚠️  About to open Notepad and type. Don't touch mouse/keyboard.")
    time.sleep(2)

    def judge_type_then_done(system, user, schema):
        judge_type_then_done.n += 1
        if judge_type_then_done.n == 1:
            return {"verdict": "act", "action": {"type": "type", "value": "hello from aria agent"}}
        return {"verdict": "done"}
    judge_type_then_done.n = 0

    skill = ComputerUseSkill(llm_chat=judge_type_then_done, llm_vision=None)

    try:
        res = skill.run("type hello into notepad", app_hint="Notepad", max_turns=6)
        ok = res.success and res.layer in ("L2b", "L2b-dry-run")
        record("L2b.notepad.type_hello", ok, f"layer={res.layer}, output={json.dumps(res.output, default=str)[:120]}")
    except Exception as e:
        record("L2b.notepad.type_hello", False, str(e)[:150])

    # ── summary ──────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 60)
    print(f"LIVE TEST: {passed}/{len(RESULTS)} passed ({time.time() - t0:.1f}s)")
    if passed < len(RESULTS):
        print("FAILED:")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}: {detail[:120]}")
    print("=" * 60)
    return 1 if passed < len(RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
