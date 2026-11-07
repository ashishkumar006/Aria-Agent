"""Gateway liveness semantics: slow is not dead.

Why this file exists
--------------------
The user's report was "when the gateway is under load, the agent fails to
connect to it sometimes, and I think some runs get skipped." They were right,
and the gateway was never down.

`_is_up()` counted ANY exception on a probe path as evidence to try the next
path, and falling off the end of three paths returned False. A read timeout or a
pool timeout — which is exactly what a loaded single-event-loop gateway produces,
since it also does its ledger's sqlite writes inline — therefore read as "nothing
is listening". `ensure_gateway()` then launched a second `main.py` on the
already-bound port, sent its output to DEVNULL so the bind error was invisible,
polled for 45 s, raised "Gateway V9 failed to start within 45s", and the caller
ended the turn with `gateway unavailable` before the graph existed. One slow
probe cost a whole run.

Measured on this box with the gateway IDLE: eight concurrent probes finished in
2.23-2.26 s against a 2.5 s budget. There was no headroom for load at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))


class _RaiseClient:
    def __init__(self, exc):
        self._exc = exc

    def get(self, url):
        raise self._exc


@pytest.fixture(autouse=True)
def _fresh_probe_client():
    """The probe client is a module singleton with real limits; each test needs
    to be able to stand in for it."""
    import gateway
    saved = gateway._PROBE_CLIENT
    yield
    gateway._PROBE_CLIENT = saved


def _stub_probe(monkeypatch, exc=None, status=None):
    import gateway
    if exc is not None:
        monkeypatch.setattr(gateway, "_probe_client",
                            lambda: _RaiseClient(exc))
    else:
        class _OK:
            def __init__(self, code):
                self.status_code = code

            def get(self, url):
                return _OK(status)
        monkeypatch.setattr(gateway, "_probe_client", lambda: _OK(status))


# ── the three-valued probe ───────────────────────────────────────────────
@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("slow"),
    httpx.PoolTimeout("pool full"),
    httpx.WriteTimeout("slow write"),
    httpx.RemoteProtocolError("bad frame"),
    ValueError("something odd"),
])
def test_a_slow_gateway_is_busy_not_down(monkeypatch, exc):
    """The whole bug, one line at a time.

    A timeout is evidence about SPEED. The only evidence about EXISTENCE is the
    connection layer. Treating the first as the second is what skipped runs.
    """
    import gateway
    _stub_probe(monkeypatch, exc=exc)
    assert gateway._probe() == "busy", (
        f"{type(exc).__name__} was read as the gateway being absent")
    # And the bool view must agree, or `/api/health` still reports death.
    assert gateway._is_up() is True


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refused"),
    httpx.ConnectTimeout("no SYN answer"),
])
def test_nothing_listening_is_down(monkeypatch, exc):
    """The genuine case must still be detected, or the agent waits forever on a
    gateway that is not there."""
    import gateway
    _stub_probe(monkeypatch, exc=exc)
    assert gateway._probe() == "down"
    assert gateway._is_up() is False


@pytest.mark.parametrize("status", [200, 401, 403, 503])
def test_answering_at_all_is_alive(monkeypatch, status):
    """401 and 503 are answers. A gateway that says "no" is running; one that
    says nothing is not. The 401 case has burned this codebase before."""
    import gateway
    _stub_probe(monkeypatch, status=status)
    assert gateway._probe() != "down"


def test_an_unimplemented_probe_path_falls_through(monkeypatch):
    """404 means this gateway build lacks the route, not that it is dead."""
    import gateway

    class _Mixed:
        """ONE client reused across the three paths, like the real singleton.
        Handing `_probe_client` the class instead of an instance gave every path
        a fresh counter, so each answered 404 and the test proved nothing."""

        def __init__(self):
            self.n = 0

        def get(self, url):
            self.n += 1
            code = 404 if self.n < 3 else 200
            return type("R", (), {"status_code": code})()

    shared = _Mixed()
    monkeypatch.setattr(gateway, "_probe_client", lambda: shared)
    assert gateway._probe() == "up"
    assert shared.n == 3, "did not walk the paths as intended"


# ── the thing that actually cost the runs ────────────────────────────────
def test_ensure_gateway_never_launches_on_a_slow_probe(monkeypatch):
    """Regression with the largest blast radius.

    "busy" used to fall into the launch branch: a duplicate process on a bound
    port, its error discarded, 45 s of polling, then an exception that ended the
    caller's turn. Asserting "no Popen" is the whole point — a fix that still
    spawns is no fix.
    """
    import gateway

    calls = []
    monkeypatch.setattr(gateway, "_probe", lambda: "busy")
    monkeypatch.setattr(gateway.subprocess, "Popen",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)
    gateway.ensure_gateway()
    assert calls == [], f"launched a duplicate gateway on a slow probe: {calls}"


def test_ensure_gateway_still_launches_when_nothing_listens(monkeypatch,
                                                             tmp_path):
    """The other direction: a genuinely absent gateway must still be started,
    or the first run after boot fails for everyone."""
    import gateway

    calls = []
    seq = ["down", "up"]

    def fake_probe():
        return seq.pop(0) if seq else "up"

    class _Dir:
        """`gateway.GATEWAY_V9_DIR` is a Path and `Path.exists` is not
        patchable per-instance; swapping the module attribute is."""

        @staticmethod
        def exists():
            return True

    monkeypatch.setattr(gateway, "_probe", fake_probe)
    monkeypatch.setattr(gateway, "GATEWAY_V9_DIR", _Dir())
    monkeypatch.setattr(
        gateway.subprocess, "Popen",
        lambda *a, **k: calls.append(k) or object())
    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)
    gateway.ensure_gateway()
    assert calls, "a genuinely absent gateway was never launched"


def test_launch_output_is_not_discarded(monkeypatch, tmp_path):
    """A child that dies on "address already in use" was previously invisible,
    which is why this took so long to find.

    Checked through what Popen actually receives, not by grepping the source —
    the function's own docstring quotes the word DEVNULL while explaining the
    bug, so a text search finds the prose and fails on correct code.
    """
    import gateway

    seen = {}

    def _capture(*a, **k):
        seen.update(k)
        return object()

    seq = ["down", "up"]
    monkeypatch.setattr(gateway, "_probe",
                        lambda: seq.pop(0) if seq else "up")

    class _Dir:
        @staticmethod
        def exists():
            return True

    monkeypatch.setattr(gateway, "GATEWAY_V9_DIR", _Dir())
    monkeypatch.setattr(gateway.subprocess, "Popen", _capture)
    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)
    gateway.ensure_gateway()
    assert seen.get("stdout") is not gateway.subprocess.DEVNULL, (
        "the child's output is still thrown away")
    assert seen.get("stderr") is not gateway.subprocess.DEVNULL, (
        "the child's errors are still thrown away")



# ── the swallowed None, which made everything undiagnosable ──────────────
def test_get_json_names_the_failure_it_swallows(monkeypatch, capsys):
    """The contract stays None — a listing the agent cannot read is a missing
    hint, not a failed document. But the author's figure list is one `get_json`,
    so a hiccup silently removed every picture from a run with no trace."""
    import gateway

    class _Boom:
        def get(self, url, timeout=None):
            raise httpx.PoolTimeout("saturated")

    monkeypatch.setattr(gateway, "_client", lambda: _Boom())
    gateway._swallow_notes["last"] = 0.0
    gateway._swallow_notes["counts"] = {}
    assert gateway.get_json("/v1/documents") is None
    out = capsys.readouterr().out
    assert "swallowed failure" in out and "/v1/documents" in out


def test_the_swallow_note_does_not_flood(monkeypatch, capsys):
    """30 s of rate limiting, because a saturated pool fails in a loop and a
    log line per call would bury the very signal it adds."""
    import gateway

    class _Boom:
        def get(self, url, timeout=None):
            raise httpx.PoolTimeout("saturated")

    monkeypatch.setattr(gateway, "_client", lambda: _Boom())
    gateway._swallow_notes["last"] = 0.0
    gateway._swallow_notes["counts"] = {}
    for _ in range(50):
        gateway.get_json("/v1/documents")
    out = capsys.readouterr().out
    assert out.count("swallowed failure") <= 2, out.count("swallowed failure")


def test_probe_budget_has_headroom_for_concurrent_pollers():
    """Measured: 8 concurrent probes against an IDLE gateway took 2.23-2.26 s
    under a flat 2.5 s timeout. The timeout must be split, not merely raised:
    a slow connect is death and stays short; a slow read is load and gets room.
    """
    import gateway
    c = gateway._probe_client()
    t = c.timeout
    assert t.connect is not None and t.connect <= 3.0, (
        f"connect budget {t.connect}s is too long — genuine absence must be "
        f"detected quickly or /api/health hangs")
    assert t.read >= 8.0, (
        f"read budget {t.read}s leaves no headroom over the measured 2.26s")
    assert c._transport._pool._max_connections >= 8, (
        "two probe slots means a third concurrent poller becomes a PoolTimeout")
