"""Tests for the calendar list + slack history integration ops added with
the tool expansion (26 -> 37). No credentials needed: the fail-soft paths
(missing token) are real behavior, and success paths are mocked at the
httpx boundary."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integrations import calendar as cal
from integrations import slack as sl


class TestCalendarList:
    def test_missing_token_fail_soft(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CALENDAR_TOKEN", raising=False)
        out = cal.list_events()
        assert out.get("ok") is False and "GOOGLE_CALENDAR_TOKEN" in out["error"]

    def test_shape(self, monkeypatch):
        import httpx as _hx

        class _R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"items": [
                    {"id": "e1", "summary": "Standup",
                     "start": {"dateTime": "2026-09-30T09:00:00+05:30"},
                     "end": {"dateTime": "2026-09-30T09:30:00+05:30"},
                     "location": "Room",
                     "htmlLink": "https://cal/e1"},
                    {"id": "e2", "summary": "All day",
                     "start": {"date": "2026-10-01"},
                     "end": {"date": "2026-10-02"}}]}

        class _C:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, **k):
                assert url.endswith("/events")
                assert k["params"]["singleEvents"] is True
                return _R()

        monkeypatch.setenv("GOOGLE_CALENDAR_TOKEN", "fake")
        monkeypatch.setattr(_hx, "Client", _C)
        out = cal.list_events()
        assert out["ok"] is True and len(out["events"]) == 2
        assert out["events"][0]["summary"] == "Standup"
        assert out["events"][0]["location"] == "Room"
        # all-day events use `date`, not `dateTime`
        assert out["events"][1]["start"] == "2026-10-01"


class TestSlackHistory:
    def test_missing_token_fail_soft(self, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        out = sl.history(channel="C123")
        assert out.get("ok") is False and "SLACK_BOT_TOKEN" in out["error"]

    def test_shape(self, monkeypatch):
        import httpx as _hx

        class _R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True, "messages": [
                    {"user": "U1", "text": "hello", "ts": "1.0"},
                    {"bot_id": "B1", "username": "deploybot",
                     "text": "done", "ts": "2.0", "subtype": "bot_message",
                     "thread_ts": "1.0"}]}

        class _C:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, **k):
                assert "conversations.history" in url
                assert k["params"]["channel"] == "C123"
                assert "Authorization" in k["headers"]
                return _R()

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
        monkeypatch.setattr(_hx, "Client", _C)
        out = sl.history(channel="C123", limit=5)
        assert out["ok"] is True and len(out["messages"]) == 2
        assert out["messages"][0]["user"] == "U1"
        assert out["messages"][1]["thread"] is True
