"""Fixed-queries × N replay harness for the computer-use engine (P3.2).

BENCHMARKS.md numbers are trace-derived, not a controlled eval. This harness
runs a fixed set of canonical goals N times against a LIVE desktop and reports
a per-goal success matrix with layer + latency. It is marked `@pytest.mark.live`
so it does NOT run in normal CI (which has no desktop daemon) — run it
explicitly on a host with cua-driver installed and COMPUTER_USE_MODE=live:

  pytest tests/test_computer_use_replay.py -q -m live --live-runs 3

The Recorder/replay infra (charter §11) is exercised by recording each run so
a successful trajectory can later be replayed as a regression test.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from computer_use import get_computer_use
from computer_use.core.recording import Recorder


# ── canonical goal set (covers each reliable path) ──────────────────────────
CANONICAL_GOALS = [
    ("calc", "compute 42 * 18", "Calculator"),
    ("notepad_write", "write 'hello from replay harness' in notepad", "Notepad"),
    ("notepad_readback", "select all and copy", "Notepad"),
    ("storage", "check storage on my laptop", None),
    ("open_app", "open notepad", None),
    ("ip", "what is my ip address", None),
]


def pytest_addoption(parser):
    parser.addoption("--live-runs", action="store", default="3",
                     help="N repetitions per goal for the live replay harness")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: requires a live desktop daemon")


@pytest.fixture
def live_runs(request):
    return int(request.config.getoption("--live-runs"))


@pytest.mark.live
class TestComputerUseReplay:
    """Runs each canonical goal N times against the live desktop."""

    def _run_once(self, goal: str, app_hint: str | None, record: bool):
        cu = get_computer_use()
        rec = None
        if record:
            rec = Recorder(cu.skill.session_id)
            rec.start()
        t0 = time.time()
        try:
            res = cu.skill.run(goal, app_hint=app_hint, max_turns=12,
                               record=record)
        finally:
            if rec is not None:
                rec.stop()
        elapsed = time.time() - t0
        return res, elapsed

    @pytest.mark.parametrize("name,goal,app_hint", CANONICAL_GOALS)
    def test_goal_matrix(self, name, goal, app_hint, live_runs):
        # Requires a live desktop; skip cleanly if the daemon is unavailable.
        from computer_use.core import daemon as _d
        if not _d.ensure_daemon(timeout=5):
            pytest.skip("cua-driver daemon not available; live harness skipped")
        if os.getenv("COMPUTER_USE_MODE", "dry-run").lower() != "live":
            pytest.skip("COMPUTER_USE_MODE != live; live harness skipped")

        results = []
        for i in range(live_runs):
            res, elapsed = self._run_once(goal, app_hint, record=(i == 0))
            results.append({
                "run": i,
                "success": bool(getattr(res, "success", False)),
                "layer": getattr(res, "layer", None),
                "elapsed_s": round(elapsed, 2),
                "error": getattr(res, "error", None),
            })
        passed = sum(1 for r in results if r["success"])
        rate = passed / len(results)
        print(f"\n[{name}] {passed}/{len(results)} passed (rate={rate:.0%})")
        for r in results:
            print(f"  run {r['run']}: ok={r['success']} "
                  f"layer={r['layer']} {r['elapsed_s']}s")
        # A goal is "reliable" if it passes at least once AND the majority of
        # runs. This is a measurement harness, not a hard gate, so we assert
        # only that the daemon path was exercised (no silent no-ops).
        assert len(results) == live_runs
        assert any(r["success"] for r in results), (
            f"{name}: 0/{live_runs} runs succeeded — engine path broken")
