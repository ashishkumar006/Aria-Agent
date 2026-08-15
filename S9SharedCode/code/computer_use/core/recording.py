"""core/recording.py — trajectory recording & replay (charter §11).

cua-driver ships start_recording / stop_recording / replay_trajectory. Every
run records to a turn-numbered directory of (tool, args) pairs. When the
agent fails, the trajectory is the evidence; when it succeeds, it is a
regression test.

Usage:
    rec = Recorder(session_id="s8-abc")
    rec.start()
    try:
        run_agent(goal)
    finally:
        rec.stop()
    # later, to reproduce:
    rec.replay()
"""
from __future__ import annotations

import time
from pathlib import Path

from .daemon import call, DaemonError

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_RECORD_ROOT = ROOT / "state" / "computer_use_runs"


class Recorder:
    """Thin wrapper over cua-driver's recording primitives."""

    def __init__(self, session_id: str, output_root: Path | None = None):
        self.session_id = session_id
        self.output_root = Path(output_root) if output_root else DEFAULT_RECORD_ROOT
        self.output_dir = self.output_root / f"run-{session_id}-{int(time.time())}"
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> bool:
        """Begin recording. Returns True on success."""
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            call("start_recording", {"output_dir": str(self.output_dir)}, timeout=15)
            self._active = True
            return True
        except (DaemonError, Exception):
            self._active = False
            return False

    def stop(self) -> dict | None:
        """Stop recording and return the trajectory summary (or None)."""
        if not self._active:
            return None
        try:
            result = call("stop_recording", {}, timeout=15)
            self._active = False
            return result
        except (DaemonError, Exception):
            self._active = False
            return None

    def replay(self, trajectory_dir: str | None = None) -> dict | None:
        """Replay a recorded trajectory against the same starting UI state."""
        target = trajectory_dir or str(self.output_dir)
        try:
            return call("replay_trajectory", {"trajectory_dir": target}, timeout=60)
        except (DaemonError, Exception):
            return None

    def path(self) -> Path:
        return self.output_dir
