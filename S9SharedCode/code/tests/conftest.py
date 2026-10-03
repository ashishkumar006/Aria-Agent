"""pytest configuration for the S8/S9 test suites.

Two responsibilities:

1. Register the ``--live-runs`` option consumed by the live replay harness
   (``test_computer_use_replay.py``). ``pytest_addoption`` only runs for
   conftest.py and registered plugins, NOT for test modules — so the option is
   declared here, where pytest actually picks it up.

2. Skip ``@pytest.mark.live`` / ``@pytest.mark.stress`` tests unless the user
   explicitly selects them with ``-m live`` / ``-m stress``. Live tests drive
   real servers (agent :8500 + gateway :8109) or a desktop daemon and must
   never run as part of an unchecked full-suite invocation (CI). The live
   tests also self-skip via their ``live_env`` fixture when the servers are
   down, so they fail safe either way.
"""
import os
import shutil
import tempfile

import pytest

# ── state isolation ─────────────────────────────────────────────────────
# Every state-bearing module (persistence, scheduler, templates, turnlog,
# agent_server, skills) derives its paths from $S9_STATE_DIR at IMPORT time.
# Point it at a fresh temp dir HERE — conftest imports before any test
# module — so the whole suite runs against throwaway state and can never
# pollute or corrupt the live dashboard (the 39 phantom session dirs came
# from exactly this missing isolation). An explicitly-set S9_STATE_DIR is
# respected (CI can pin its own).
_TEST_STATE_DIR = os.environ.get("S9_STATE_DIR")
if not _TEST_STATE_DIR:
    _TEST_STATE_DIR = tempfile.mkdtemp(prefix="s9-test-state-")
    os.environ["S9_STATE_DIR"] = _TEST_STATE_DIR
    _OWN_TEST_STATE = True
else:
    _OWN_TEST_STATE = False


def pytest_sessionfinish(session, exitstatus):
    # Remove only the dir we created; a pinned S9_STATE_DIR is left alone.
    if _OWN_TEST_STATE:
        shutil.rmtree(_TEST_STATE_DIR, ignore_errors=True)


def pytest_addoption(parser):
    parser.addoption(
        "--live-runs", action="store", default="3",
        help="N repetitions per goal for the live replay harness",
    )


def pytest_configure(config):
    # Ensure the markers are registered even if pyproject.toml is ignored.
    config.addinivalue_line(
        "markers", "live: requires live servers (agent :8500 + gateway :8109)")
    config.addinivalue_line(
        "markers", "stress: slow soak/perf/canary run; opt-in only")


def pytest_collection_modifyitems(config, items):
    # Skip live/stress tests unless the user explicitly selects them with
    # `-m live` / `-m stress`. They need running servers or are slow soaks and
    # must never run as part of an unchecked full-suite invocation (CI).
    mfilter = (config.getoption("markexpr", default="")
               or config.getoption("-m", default=""))
    if "live" in mfilter or "stress" in mfilter:
        return  # user explicitly asked for these — keep them
    skip_live = pytest.mark.skip(reason="live test; run with '-m live'")
    skip_stress = pytest.mark.skip(reason="stress test; run with '-m stress'")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip_live)
        if item.get_closest_marker("stress"):
            item.add_marker(skip_stress)
