"""Two runs streaming at once must not read each other's log.

`/api/chat` renders live progress by capturing the orchestrator's `print()`
output. That capture used to be `contextlib.redirect_stdout(<this run's
catcher>)`, and `sys.stdout` is ONE process-global object — so with two runs in
flight the catcher installed last received every run's writes.

Reproduced against the live server: a research run's SSE stream carried another
run's `FINAL:` block and reported a node that took 134.4 s inside a run that had
been alive for 14.9 s. Behind that is a disclosure: another conversation's query
text, tool results and retrieved documents arriving inside your own run's log.

The answer itself was always safe (it lands in a per-request box). The LOG was
not, and the log is what the console shows.
"""
from __future__ import annotations

import io
import sys
import threading
import time


class _Sink:
    """One run's log queue, installed the way `_stream_run` installs its own."""

    def __init__(self):
        self.items: list[str] = []
        self._cm = None

    def push(self, s: str) -> None:
        self.items.append(s)

    def __enter__(self):
        import agent_server
        self._cm = agent_server.run_stdout_capture(self.push)
        self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)

    def text(self) -> str:
        return "".join(self.items)


def _worker(sink: _Sink, tag: str, n: int, barrier: threading.Barrier,
            err: list) -> None:
    try:
        barrier.wait(10)
        with sink:
            for i in range(n):
                print(f"{tag}-{i}")
                time.sleep(0.001)
    except Exception as e:  # pragma: no cover - surfaces as a test failure
        err.append(f"{tag}: {type(e).__name__}: {e}")


def test_a_run_only_sees_its_own_prints_when_two_runs_overlap():
    """The regression itself: A's writes must not arrive on B's stream."""
    n = 60
    a, b = _Sink(), _Sink()
    err: list[str] = []
    barrier = threading.Barrier(2)
    ta = threading.Thread(target=_worker, args=(a, "A", n, barrier, err), name="A")
    tb = threading.Thread(target=_worker, args=(b, "B", n, barrier, err), name="B")
    ta.start()
    tb.start()
    ta.join(30)
    tb.join(30)
    assert not err, err

    assert a.text(), "run A captured nothing at all"
    assert b.text(), "run B captured nothing at all"
    assert "B-" not in a.text(), "run A's stream carried run B's log lines"
    assert "A-" not in b.text(), "run B's stream carried run A's log lines"
    assert all(f"A-{i}" in a.text() for i in range(n))
    assert all(f"B-{i}" in b.text() for i in range(n))


def test_the_real_stdout_comes_back_when_the_last_run_finishes():
    """The router is shared, so it is reference-counted.

    Two runs install and one uninstalls would leave `sys.stdout` pointing at a
    router with no sink, and every later print — including another run's —
    would silently vanish from the console feed.
    """
    prior = sys.stdout
    outer, inner = _Sink(), _Sink()
    with outer:
        assert sys.stdout is not prior, "capture did not install the router"
        with inner:
            print("nested")
        assert sys.stdout is not prior, "inner exit stole the router from outer"
        print("still-captured")
    assert sys.stdout is prior, "stdout was not restored after the last capture"
    assert "nested" in inner.text()
    assert "still-captured" in outer.text()
    assert "still-captured" not in inner.text()


def test_prints_outside_a_run_are_never_swallowed():
    """Run-less output must reach the ordinary stdout, untouched.

    The router mirrors to `_LIVE_OUT` (the tee the Console's event feed tails)
    WHILE a run is capturing. Outside a capture nothing is routed at all, so a
    startup line or a test's own print keeps going to the real stream — a
    swallow there would be invisible in the terminal and only show up as
    missing server logs.
    """
    import agent_server

    buf = io.StringIO()
    real = sys.stdout
    real_live = agent_server._LIVE_OUT
    sink = _Sink()
    try:
        agent_server._LIVE_OUT = buf
        with sink:
            print("during-run")
        agent_server._LIVE_OUT = real_live
        print("after-run")
    finally:
        agent_server._LIVE_OUT = real_live
        sys.stdout = real
    assert "during-run" in sink.text(), "a run's own print missed its queue"
    assert "during-run" in buf.getvalue(), "the tee stopped mirroring run output"
    assert "after-run" not in buf.getvalue(), "a run-less print leaked into the tee"


def test_the_sink_is_context_scoped_not_a_module_global():
    """Pin the mechanism, because `redirect_stdout` would pass everything else.

    A future edit that reaches back for `contextlib.redirect_stdout` keeps the
    visible behaviour and reintroduces the leak, so the shape of the fix — a
    ContextVar, empty whenever no run is capturing — is asserted directly.
    """
    import agent_server

    var = agent_server._RUN_LOG_SINK
    assert var.get() is None, "no run should own the sink outside a capture"
    with agent_server.run_stdout_capture(lambda s: None):
        assert var.get() is not None
    assert var.get() is None


def test_a_failing_sink_cannot_break_the_run():
    """The queue is best-effort: a dead viewer must not kill a paid run."""
    import agent_server

    def boom(_s):
        raise RuntimeError("client already left")

    with agent_server.run_stdout_capture(boom):
        print("this must not raise")
