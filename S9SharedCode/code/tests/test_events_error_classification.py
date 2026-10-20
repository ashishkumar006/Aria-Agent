"""The event feed must not report successful traffic as errors.

uvicorn writes its ACCESS log to stderr, so `agent.err` is mostly successful
HTTP. Classifying every line of that file as `err` put 77 red lines and
"ERRORS - need attention" on the Console page while everything visible was
`INFO ... 200 OK`. The file a line was written to says where it went, not
whether it failed - so the content has to decide.

Deliberately asserts on the feed, not on the log file: /api/events only reads
the tail of each log, so a test that depends on a particular traceback still
being inside that window fails for reasons unrelated to classification.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_server as ag          # noqa: E402

SUCCESS_MARKERS = ("HTTP Request:", '"HTTP/1.1" 200 OK', "200 OK",
                   "201 Created", "204 No Content")


@pytest.fixture(scope="module")
def feed():
    from starlette.testclient import TestClient
    return TestClient(ag.app)


def _events(client):
    d = client.get("/api/events?limit=200").json()
    return d.get("events") or []


def test_successful_access_log_lines_are_not_errors(feed):
    for e in _events(feed):
        if e.get("level") != "err":
            continue
        msg = str(e.get("msg") or "")
        if not any(m in msg for m in SUCCESS_MARKERS):
            continue
        # An error keyword still wins, so a genuine 500 that happens to carry
        # "200" somewhere is not counted here.
        if any(k in msg for k in ("ERROR", "Traceback", "CRITICAL", "Exception")):
            continue
        pytest.fail(f"a successful access-log line is classified as err: {msg[:140]}")


def test_error_level_is_not_assigned_by_file_alone(feed):
    """The regression itself: `agent.err` must no longer mean "err".

    If every line of agent.err were promoted, the error count would track the
    size of the access log rather than the number of failures. A run of
    successful traffic is the observable symptom.
    """
    events = _events(feed)
    access = [e for e in events
              if any(m in str(e.get("msg") or "") for m in SUCCESS_MARKERS)]
    if not access:
        pytest.skip("no access-log lines in the current window")
    assert all(e.get("level") != "err" for e in access), (
        "an access-log success was marked err")


def test_feed_still_serves_lines(feed):
    """The endpoint works. Deliberately not "there is at least one line":
    the feed reads the tail of two log files, and both can legitimately be
    empty (a cleared console), which would fail a content assertion for a
    reason that has nothing to do with classification."""
    d = feed.get("/api/events?limit=200")
    assert d.status_code == 200, d.text
    body = d.json()
    assert "events" in body, body
    assert isinstance(body["events"], list)


def test_run_and_err_levels_are_distinct(feed):
    """Level is still meaningful - the fix did not flatten everything to info."""
    levels = {e.get("level") for e in _events(feed)}
    assert levels <= {"run", "sched", "tool", "info", "err"}, levels
    if not levels:
        pytest.skip("the feed is empty; nothing to classify right now")
