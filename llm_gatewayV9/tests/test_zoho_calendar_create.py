"""Zoho Calendar create_event: the write path, mocked at the httpx boundary.

The read ops (list_calendars / list_events) are covered by
``test_integration_reads``-style tests; ``create_event`` had none. It is the
one Calendar verb that mutates the user's real calendar, and its body is the
undocumented ``eventdata`` contract (epoch-ms ``dateandtime`` + a ``duration``
in minutes, NOT startDate/endDate), so it is exactly the kind of code that
silently rots without a test pinning the wire shape.

No credentials or network: ``_access_token`` and ``pooled_http`` are replaced,
so these run in the normal (non-live) suite.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integrations import zoho_calendar as zc


def _fake_http(response=None, *, raises=None):
    """A pooled_http stand-in. Records the POST so a test can assert the body.

    Returns (client_factory, captured) where captured is a dict that fills in
    with the url/headers/data of the single post the code makes.
    """
    captured: dict = {}

    class _R:
        status_code = 200

        def __init__(self):
            self.status_code = (response or {}).get("status_code", 200)
            self._json = (response or {}).get("json", {"data": {}})
            self.text = (response or {}).get("text", "")

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._json

    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **k):
            if raises is not None:
                raise raises
            captured["url"] = url
            captured["headers"] = k.get("headers")
            captured["data"] = k.get("data")
            return _R()

    return (lambda _t: _C()), captured


def _patch_token(monkeypatch, token="fake-token"):
    monkeypatch.setattr(zc, "_access_token", lambda: token)
    monkeypatch.setattr(zc, "_dc", lambda: "in")


class TestZohoCalendarCreate:
    def test_missing_token_fail_soft(self, monkeypatch):
        def _boom():
            raise RuntimeError("ZOHO_TOKEN not set")

        monkeypatch.setattr(zc, "_access_token", _boom)
        out = zc.create_event(title="x", start="2026-10-01T09:00:00Z",
                              end="2026-10-01T09:30:00Z")
        assert out.get("ok") is False
        assert "ZOHO_TOKEN" in out["error"]

    def test_success_returns_event_id(self, monkeypatch):
        _patch_token(monkeypatch)
        factory, _ = _fake_http({"json": {"data": [{"eventId": "evt-123"}]}})
        monkeypatch.setattr(zc, "pooled_http", factory)
        out = zc.create_event(title="Standup", start="2026-10-01T09:00:00Z",
                              end="2026-10-01T09:30:00Z")
        assert out["ok"] is True
        assert out["id"] == "evt-123"
        assert out["title"] == "Standup"
        assert out["calendar"] == "primary"

    def test_eventdata_wire_shape(self, monkeypatch):
        """The whole point: dateandtime is epoch-ms, duration is minutes.

        A regression to startDate/endDate (the Google shape) would 400 live and
        no other test would catch it.
        """
        _patch_token(monkeypatch)
        factory, cap = _fake_http({"json": {"data": [{"eventId": "e"}]}})
        monkeypatch.setattr(zc, "pooled_http", factory)
        zc.create_event(title="Sync", start="2026-10-01T09:00:00Z",
                        end="2026-10-01T10:30:00Z", description="d",
                        location="Room")
        assert cap["url"].endswith("/calendars/primary/events")
        assert cap["headers"]["Authorization"] == "Bearer fake-token"
        # Zoho wants the event JSON-encoded under the `eventdata` form field.
        body = cap["data"]
        assert set(body) == {"eventdata"}
        ev = json.loads(body["eventdata"])
        assert ev["title"] == "Sync"
        assert isinstance(ev["dateandtime"], int)
        # 09:00 -> 10:30 is 90 minutes.
        assert ev["duration"] == 90
        assert ev["description"] == "d"
        assert ev["location"] == "Room"
        assert ev["isAllDayEvent"] is False

    def test_all_day_uses_1440_minute_duration(self, monkeypatch):
        _patch_token(monkeypatch)
        factory, cap = _fake_http({"json": {"data": [{"eventId": "e"}]}})
        monkeypatch.setattr(zc, "pooled_http", factory)
        zc.create_event(title="Holiday", start="2026-10-01T00:00:00Z",
                        end="2026-10-02T00:00:00Z", all_day=True)
        ev = json.loads(cap["data"]["eventdata"])
        assert ev["isAllDayEvent"] is True
        assert ev["duration"] == 1440

    def test_start_accepts_epoch_ms(self, monkeypatch):
        _patch_token(monkeypatch)
        factory, cap = _fake_http({"json": {"data": [{"eventId": "e"}]}})
        monkeypatch.setattr(zc, "pooled_http", factory)
        zc.create_event(title="Epoch", start="1727773200000",
                        end="1727776800000")
        ev = json.loads(cap["data"]["eventdata"])
        assert ev["dateandtime"] == 1727773200000
        assert ev["duration"] == 60  # 1h apart

    def test_scope_missing_is_actionable(self, monkeypatch):
        _patch_token(monkeypatch)
        factory, _ = _fake_http({"status_code": 401})
        monkeypatch.setattr(zc, "pooled_http", factory)
        out = zc.create_event(title="x", start="2026-10-01T09:00:00Z",
                              end="2026-10-01T09:30:00Z")
        assert out["ok"] is False
        assert "scope" in out["error"].lower()

    def test_server_error_fail_soft(self, monkeypatch):
        _patch_token(monkeypatch)
        factory, _ = _fake_http({"status_code": 500, "text": "boom"})
        monkeypatch.setattr(zc, "pooled_http", factory)
        out = zc.create_event(title="x", start="2026-10-01T09:00:00Z",
                              end="2026-10-01T09:30:00Z")
        assert out["ok"] is False
        assert "create failed" in out["error"]

    def test_network_exception_fail_soft(self, monkeypatch):
        _patch_token(monkeypatch)
        factory, _ = _fake_http(raises=RuntimeError("network down"))
        monkeypatch.setattr(zc, "pooled_http", factory)
        out = zc.create_event(title="x", start="2026-10-01T09:00:00Z",
                              end="2026-10-01T09:30:00Z")
        assert out["ok"] is False
        assert "network down" in out["error"]