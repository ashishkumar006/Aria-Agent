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


@pytest.fixture(autouse=True)
def _no_live_document_uploads(monkeypatch):
    """No test may write into the real Documents library.

    `S9_STATE_DIR` isolates every file the agent owns, but the gateway is a
    separate process on :8109 with its OWN state, and artifact filing posts to
    it. So the suites that exercise the filing path — a produced PDF, a spilled
    finding, a fetched page, a stored search list — uploaded their fixtures into
    the user's live library: `a.txt`, `b.txt`, `Path.txt`, and several
    "Findings with figures." documents, each with real chunks and embeddings.

    Returning None is exactly what `gateway.post_multipart` does when the
    gateway is away, and registration is fail-soft by contract, so this is not a
    special test mode: it is the offline branch. A test that wants to assert
    filing patches `gateway.post_multipart` itself, which overrides this.
    """
    try:
        import gateway

        def _away(*_a, **_k):
            return None

        monkeypatch.setattr(gateway, "post_multipart", _away, raising=False)
        # `None` means "could not ask", which is the answer that keeps an
        # existing link instead of re-filing — the safe branch, and the one that
        # keeps a test from probing the live plane for a document's status.
        monkeypatch.setattr(gateway, "document_exists", _away, raising=False)
        # The delete verb, or the guard only held the write half of a replace.
        # `_persist_persona_documents` lists the library, DELETEs the previous
        # "Persona — current.md" / "Persona — evolution.md" and then uploads the
        # new ones. With only the upload patched, a chat-turn persona worker
        # inside the suite removed both live copies and re-uploaded nothing: the
        # user's Documents section lost a 322-chunk evolution document to a test
        # run that "could not have written anything".
        monkeypatch.setattr(gateway, "delete_json", _away, raising=False)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _fresh_gateway_client_pools():
    """Empty the pooled gateway httpx clients for every test.

    `gateway.sync_pool` / `gateway.async_client` reuse one client per
    (loop, timeout) because building an httpx client costs 230-590 ms here.
    That is the point in production and a hazard in tests: the suites that
    patch `httpx.Client`/`httpx.AsyncClient` with a fake — to assert a proxy
    call carries the gateway token, or to simulate a dead gateway — only have
    an effect while the call site still constructs a client. A cached client
    would survive the patch, so `test_a_401_is_retried...` would be testing a
    connection made before it started.
    """
    try:
        import gateway
        gateway.reset_client_pools()
    except Exception:
        pass
    yield

