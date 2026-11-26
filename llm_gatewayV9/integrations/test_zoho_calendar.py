"""Zoho Calendar integration tests (offline). Run: uv run pytest integrations/test_zoho_calendar.py -q"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import _adaptor_kit as kit


class _FakeClient:
    def __init__(self, handler): self._h = handler; self.calls = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def _c(self, m, u, **k): self.calls.append((m, u)); return self._h(m, u, **k)
    def get(self, u, **k): return self._c("GET", u, **k)
    def post(self, u, **k): return self._c("POST", u, **k)


def _patch(monkeypatch, handler):
    import httpx, adaptors.http as _h
    _h.reset_pools()
    box = {}
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: box.setdefault("c", _FakeClient(handler)))
    return box


def _handler(method, url, **kw):
    if url.endswith("/calendars"):
        return kit.FakeResp(200, {"calendars": [{"id": "1", "name": "Primary"}]})
    if "/events" in url:
        return kit.FakeResp(200, {"events": [{"eventId": "e1", "title": "Hi",
                                              "startDate": 1, "endDate": 2}]})
    return kit.FakeResp(404, {})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ZOHO_TOKEN", "tok")
    import integrations.zoho_calendar as zc
    yield


def test_list_calendars(monkeypatch):
    import integrations.zoho_calendar as zc
    _patch(monkeypatch, _handler)
    r = zc.list_calendars()
    assert r["ok"] is True and r["calendars"] == [{"id": "1", "name": "Primary"}], r


def test_list_events_skips_status_rows(monkeypatch):
    import integrations.zoho_calendar as zc
    _patch(monkeypatch, _handler)
    r = zc.list_events(calendar_id="primary")
    assert r["ok"] is True and len(r["events"]) == 1 and r["events"][0]["title"] == "Hi", r