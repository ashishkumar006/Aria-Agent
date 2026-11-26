"""Key info + channel probe: presence only, never values."""
from __future__ import annotations


def test_key_info_records_are_complete_and_value_free():
    import key_info
    rows = key_info.collect()
    assert rows, "no credential records"
    for r in rows:
        assert isinstance(r["set"], bool)
        assert r.get("get"), f"{r['name']} has no 'how to get it'"
        assert "refresh" in r, f"{r['name']} has no refresh guidance"
        # A record must never carry the secret itself.
        assert "value" not in r and "token" not in {k.lower() for k in r}


def test_channel_probe_shape_without_network(monkeypatch):
    import channel_probe
    monkeypatch.setattr(channel_probe, "CAPTURED", {"notion": "dummy"})
    monkeypatch.setattr(channel_probe, "_request", lambda *a, **k: (200, '{"ok": true}'))
    rows = channel_probe.probe_all()
    assert len(rows) == len(channel_probe.TOKEN_ENVS)
    notion = next(r for r in rows if r["name"] == "notion")
    assert notion["ok"] is True and notion["configured"] is True
    telegram = next(r for r in rows if r["name"] == "telegram")
    assert telegram["configured"] is False  # not captured


def test_channel_probe_marks_unhealthy(monkeypatch):
    import channel_probe
    monkeypatch.setattr(channel_probe, "CAPTURED", {"slack": "dummy"})
    monkeypatch.setattr(channel_probe, "_request", lambda *a, **k: (200, '{"ok": false, "error": "invalid_auth"}'))
    rows = channel_probe.probe_all()
    slack = next(r for r in rows if r["name"] == "slack")
    assert slack["ok"] is False
    assert "invalid_auth" in slack["detail"]


def test_oauth_start_requires_client_credentials(monkeypatch):
    import oauth_flows
    monkeypatch.delenv("GMAIL_CLIENT_ID", raising=False)
    monkeypatch.delenv("GMAIL_CLIENT_SECRET", raising=False)
    r = oauth_flows.start("gmail")
    assert r["ok"] is False
    assert "GMAIL_CLIENT_ID" in r["error"]


def test_oauth_status_unknown_flow():
    import oauth_flows
    r = oauth_flows.status("does-not-exist")
    assert r["ok"] is False