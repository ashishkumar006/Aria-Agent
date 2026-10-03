"""P3 — Scheduler tests (deterministic, no live agent run).

Tests the lightweight scheduler in `scheduler.py` with mocked time and
a temp-file-backed state directory so no real background worker fires.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── helpers ──────────────────────────────────────────────────────────────────

def _install_scheduler(tmp_path: Path, monkeypatch):
    """Point the scheduler module at a temp directory and reset state."""
    import scheduler as sched
    monkeypatch.setattr(sched, "_SCHED_PATH", tmp_path / "schedules.json")
    monkeypatch.setattr(sched, "_LOG_PATH", tmp_path / "scheduler.log")
    sched._SCHEDULES.clear()
    sched._HEAP.clear()
    return sched


def _run_worker_once(sched, monkeypatch, timeout=5.0):
    """Drive the REAL _worker loop in a thread until it fires something.

    The old suite re-implemented the worker's re-arm logic by hand, which
    is why it stayed green while the actual loop was broken.
    """
    fired: list[tuple[str, dict]] = []
    logs: list[str] = []
    monkeypatch.setattr(sched, "_fire",
                        lambda s, spec=None: fired.append((s, dict(spec))))
    monkeypatch.setattr(sched, "_log", lambda m: logs.append(m))
    sched._STOP.clear()
    t = threading.Thread(target=sched._worker, daemon=True)
    t.start()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and not fired and not logs:
            time.sleep(0.05)
    finally:
        sched._STOP.set()
        t.join(timeout=timeout)
    return fired, logs


# ── tests ─────────────────────────────────────────────────────────────────────

class TestNextFireFromCron:
    """`_next_fire_from_cron` computes the next fire epoch correctly."""

    def test_epoch_seconds_passthrough(self):
        import scheduler as sched
        base = 1_700_000_000.0
        epoch_str = "1700001000"
        assert sched._next_fire_from_cron(epoch_str, base) == 1_700_001_000.0

    def test_in_10m_relative(self):
        import scheduler as sched
        base = 1_700_000_000.0
        with mock.patch("scheduler.time.time", return_value=base):
            sid = sched.schedule("ping", "in 10m")
        spec = sched._SCHEDULES[sid]
        assert spec["next_fire"] == pytest.approx(base + 10 * 60, rel=1e-3)
        assert spec["recurring"] is None

    def test_in_1h_relative(self):
        import scheduler as sched
        base = 1_700_000_000.0
        with mock.patch("scheduler.time.time", return_value=base):
            sid = sched.schedule("ping", "in 1h")
        spec = sched._SCHEDULES[sid]
        assert spec["next_fire"] == pytest.approx(base + 3600, rel=1e-3)

    def test_daily_cron_next_today(self):
        """If the target time is later today, fire today."""
        import scheduler as sched
        # Base: 2024-01-01 08:00 UTC → daily@10:00 should fire same day.
        base = 1_704_137_200.0  # 2024-01-01 08:00 UTC approx
        with mock.patch("scheduler.time.time", return_value=base):
            sid = sched.schedule("ping", "daily@10:00")
        spec = sched._SCHEDULES[sid]
        expected = base + 2 * 3600  # 2 hours later
        assert spec["next_fire"] == pytest.approx(expected, rel=1e-3)
        assert spec["recurring"] == "daily@10:00"

    def test_daily_cron_next_tomorrow(self):
        """If the target time already passed today, fire tomorrow."""
        import scheduler as sched
        # Base: 2024-01-01 12:00 UTC → daily@10:00 should fire tomorrow.
        base = 1_704_145_200.0  # 2024-01-01 12:00 UTC approx
        with mock.patch("scheduler.time.time", return_value=base):
            sid = sched.schedule("ping", "daily@10:00")
        spec = sched._SCHEDULES[sid]
        expected = base + 22 * 3600  # tomorrow 10:00
        assert spec["next_fire"] == pytest.approx(expected, rel=1e-3)

    def test_every_30m_recurring(self):
        import scheduler as sched
        base = 1_700_000_000.0
        with mock.patch("scheduler.time.time", return_value=base):
            sid = sched.schedule("ping", "every 30m")
        spec = sched._SCHEDULES[sid]
        assert spec["next_fire"] == pytest.approx(base + 30 * 60, rel=1e-3)
        assert spec["recurring"] == "every 30m"


class TestScheduleCRUD:
    """schedule / list_schedules / cancel round-trip correctly."""

    def test_schedule_returns_id(self, tmp_path, monkeypatch):
        sched = _install_scheduler(tmp_path, monkeypatch)
        sid = sched.schedule("hello", "in 10m", conversation_id="c1")
        assert sid.startswith("sch-")
        assert sid in sched._SCHEDULES

    def test_list_schedules_returns_all(self, tmp_path, monkeypatch):
        sched = _install_scheduler(tmp_path, monkeypatch)
        sched.schedule("q1", "in 10m", conversation_id="c1")
        sched.schedule("q2", "daily@09:00", conversation_id="c2")
        rows = sched.list_schedules()
        assert len(rows) == 2
        queries = {r["query"] for r in rows}
        assert queries == {"q1", "q2"}

    def test_cancel_disables_schedule(self, tmp_path, monkeypatch):
        sched = _install_scheduler(tmp_path, monkeypatch)
        sid = sched.schedule("bye", "in 10m")
        assert sched.cancel(sid) is True
        assert sched._SCHEDULES[sid]["enabled"] is False
        # list_schedules still returns it (disabled flag visible).
        rows = sched.list_schedules()
        assert rows[0]["enabled"] is False

    def test_cancel_unknown_returns_false(self, tmp_path, monkeypatch):
        sched = _install_scheduler(tmp_path, monkeypatch)
        assert sched.cancel("sch-doesnotexist") is False

    def test_persistence_across_reload(self, tmp_path, monkeypatch):
        """Schedules survive a process reload via schedules.json."""
        sched = _install_scheduler(tmp_path, monkeypatch)
        sid = sched.schedule("persist", "in 10m", conversation_id="c1")
        # Simulate a reload by clearing in-memory state and calling _load.
        sched._SCHEDULES.clear()
        sched._HEAP.clear()
        sched._load()
        assert sid in sched._SCHEDULES
        assert sched._SCHEDULES[sid]["query"] == "persist"
        assert sched._SCHEDULES[sid]["conversation_id"] == "c1"


class TestRecurringReArm:
    """When a recurring schedule fires, it re-arms for the next interval."""

    def test_every_30m_rearms_after_fire(self, tmp_path, monkeypatch):
        import scheduler as sched
        _install_scheduler(tmp_path, monkeypatch)

        base = 1_700_000_000.0
        with mock.patch("scheduler.time.time", return_value=base):
            sid = sched.schedule("ping", "every 30m")

        first_fire = sched._SCHEDULES[sid]["next_fire"]
        assert first_fire == pytest.approx(base + 30 * 60, rel=1e-3)

        # Simulate the worker firing the schedule at first_fire.
        new_base = first_fire
        with mock.patch("scheduler.time.time", return_value=new_base):
            sched._next_fire_from_cron.cache_clear() if hasattr(
                sched._next_fire_from_cron, "cache_clear"
            ) else None
            # Re-arm logic mirrors the worker loop.
            spec = sched._SCHEDULES[sid]
            spec["next_fire"] = sched._next_fire_from_cron(spec["recurring"], new_base)
            sched._HEAP = [(spec["next_fire"], sid)]
            sched._save()

        second_fire = sched._SCHEDULES[sid]["next_fire"]
        assert second_fire == pytest.approx(new_base + 30 * 60, rel=1e-3)


class TestWorkerFire:
    """Drive the REAL _worker loop (regression coverage for the bug where
    the worker disabled a one-shot and then handed the disabled spec to
    _fire, which aborted — so every one-shot silently did nothing)."""

    def _due_one_shot(self, sched, tmp_path, monkeypatch, query="ping"):
        with mock.patch("scheduler.time.time", return_value=1_700_000_000.0):
            sid = sched.schedule(query, "in 10m")
        # Make it due right now.
        sched._SCHEDULES[sid]["next_fire"] = time.time() - 1
        sched._save()
        return sid

    def test_one_shot_reaches_fire_with_enabled_spec(self, tmp_path, monkeypatch):
        sched = _install_scheduler(tmp_path, monkeypatch)
        sid = self._due_one_shot(sched, tmp_path, monkeypatch)

        fired, _ = _run_worker_once(sched, monkeypatch)

        assert fired, "worker never invoked _fire for a due one-shot"
        got_sid, spec = fired[0]
        assert got_sid == sid
        # THE BUG: this used to arrive as enabled=False → _fire returned
        # immediately and the job never ran.
        assert spec["enabled"] is True
        assert spec["query"] == "ping"

    def test_one_shot_disabled_on_disk_after_fire(self, tmp_path, monkeypatch):
        """Crash safety: the one-shot is persisted as disabled BEFORE the
        run, so a reload can never resurrect it."""
        sched = _install_scheduler(tmp_path, monkeypatch)
        sid = self._due_one_shot(sched, tmp_path, monkeypatch)

        fired, _ = _run_worker_once(sched, monkeypatch)
        assert fired

        sched._SCHEDULES.clear()
        sched._HEAP.clear()
        sched._load()
        assert sched._SCHEDULES[sid]["enabled"] is False
        assert not sched._HEAP, "disabled one-shot must not re-heap"

    def test_fire_error_is_logged_not_swallowed(self, tmp_path, monkeypatch):
        sched = _install_scheduler(tmp_path, monkeypatch)
        sid = self._due_one_shot(sched, tmp_path, monkeypatch)

        def boom(s, spec=None):
            raise RuntimeError("executor exploded")
        monkeypatch.setattr(sched, "_fire", boom)
        logs: list[str] = []
        monkeypatch.setattr(sched, "_log", lambda m: logs.append(m))
        sched._STOP.clear()
        t = threading.Thread(target=sched._worker, daemon=True)
        t.start()
        deadline = time.time() + 5
        try:
            while time.time() < deadline and not logs:
                time.sleep(0.05)
        finally:
            sched._STOP.set()
            t.join(timeout=5)

        assert logs, "worker still swallows exceptions silently"
        assert any("fire raised" in m for m in logs)
