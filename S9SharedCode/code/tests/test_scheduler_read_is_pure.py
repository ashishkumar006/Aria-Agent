"""Listing schedules must not delete them.

`list_schedules()` purged aged disabled one-shots as a side effect, and the
console polls `GET /api/schedule` every 15 seconds. So opening the Scheduler
page destroyed the operator's execution history: a 30-day-old executed one-shot
was gone after a single page load, with nothing in the response reporting it.

`cancel()` had the same purge, which is worse in principle - a user cancelling
one job silently discarded every other aged disabled row.

The purge is a legitimate operation; it just has to be requested rather than
inferred from a read. `purge_disabled()` is that explicit path and it reports a
count, so these tests hold the read pure and confirm the explicit purge still
works.
"""

from __future__ import annotations

import time

import pytest

import scheduler


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """scheduler keeps module-global _SCHEDULES/_HEAP and a state path."""
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(scheduler, "_STATE_DIR", tmp_path / "state",
                        raising=False)
    monkeypatch.setattr(scheduler, "_SCHED_PATH", tmp_path / "state" / "schedules.json",
                        raising=False)
    old_sched, old_heap = scheduler._SCHEDULES, scheduler._HEAP
    scheduler._SCHEDULES, scheduler._HEAP = {}, []
    yield
    scheduler._SCHEDULES, scheduler._HEAP = old_sched, old_heap


def _age_one_shot(days: float) -> str:
    """Register a one-shot that looks `days` old, without waiting for it.

    "in 1h" makes a genuine one-shot; "daily@09:00" would be recurring, and a
    recurring job is deliberately never purged - so the fixture has to be the
    shape that actually gets swept.
    """
    sid = scheduler.schedule("an old executed reminder", "in 1h")
    spec = scheduler._SCHEDULES[sid]
    assert not spec.get("recurring"), "fixture must be a one-shot"
    spec["enabled"] = False
    spec["next_fire"] = time.time() - days * 86400
    spec["created"] = time.time() - days * 86400
    scheduler._save()
    return sid


def test_listing_does_not_purge_old_executed_rows():
    sid = _age_one_shot(30)

    rows = scheduler.list_schedules()

    assert any(r["id"] == sid for r in rows), (
        "GET /api/schedule deleted a 30-day-old executed schedule; the "
        "operator's execution history disappears on a page load")
    assert sid in scheduler._SCHEDULES


def test_listing_repeatedly_is_still_non_destructive():
    """The console polls; a leak that only shows on the Nth call is still a
    leak."""
    sid = _age_one_shot(30)
    for _ in range(5):
        scheduler.list_schedules()
    assert sid in scheduler._SCHEDULES


def test_cancelling_one_job_does_not_purge_the_others_history():
    old = _age_one_shot(30)
    live = scheduler.schedule("a job that is still active", "daily@09:00")

    assert scheduler.cancel(live) is True

    assert old in scheduler._SCHEDULES, (
        "cancelling one schedule purged an unrelated old one")


def test_the_explicit_purge_still_removes_them():
    """The capability is preserved - it is just no longer implicit."""
    old = _age_one_shot(30)
    assert old in scheduler._SCHEDULES

    removed = scheduler.purge_disabled(max_age_days=7)

    assert removed >= 1
    assert old not in scheduler._SCHEDULES


def test_a_recurring_job_is_never_purged():
    sid = scheduler.schedule("daily standup", "daily@09:00")
    spec = scheduler._SCHEDULES[sid]
    spec["enabled"] = False
    spec["created"] = time.time() - 400 * 86400
    scheduler._save()

    scheduler.purge_disabled(max_age_days=7)

    assert sid in scheduler._SCHEDULES, (
        "a disabled RECURRING job must survive the purge; it can be re-enabled")
