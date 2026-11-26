"""Zoho token self-heal: a missing/expired access token is minted from the
refresh token, written back, and used — without a browser or a human.

`_access_token()` is the single chokepoint every Zoho call (mail + calendar)
goes through. Its self-healing contract:

  * ZOHO_TOKEN present  -> returned as-is (no network);
  * ZOHO_TOKEN missing   -> refresh() exchanges ZOHO_REFRESH_TOKEN for a new
                            access token, writes it to the gateway .env, sets
                            os.environ, and returns it;
  * refresh() fails      -> a clear fail-soft error, never a raise into the
                            caller.

No real credentials and, crucially, no touch of the real gateway .env: GW_ENV
is redirected to a temp file, so these run in the normal suite.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integrations import zoho_mail as zm


def _fake_http(access_token="new-access-abc", *, status=200,
               json_body=None, raises=None):
    """pooled_http stand-in returning a token response from /oauth/v2/token."""
    captured: dict = {}

    class _R:
        status_code = status
        text = ""

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            if json_body is not None:
                return json_body
            return {"access_token": access_token, "expires_in": 3600}

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
            captured["data"] = k.get("data")
            return _R()

    return (lambda _t: _C()), captured


def _creds(monkeypatch, *, token=None):
    monkeypatch.setenv("ZOHO_CLIENT_ID", "cid")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "csec")
    monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "rtok")
    monkeypatch.setenv("ZOHO_DC", "in")
    # Protect the real process env: whatever refresh() writes to os.environ is
    # restored to this (absent) state on teardown.
    if token is None:
        monkeypatch.delenv("ZOHO_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ZOHO_TOKEN", token)


class TestZohoTokenRefresh:
    def test_present_token_is_returned_without_network(self, monkeypatch):
        _creds(monkeypatch, token="already-here")

        def _boom(_t):
            raise AssertionError("refresh() must not be called when a token exists")

        monkeypatch.setattr(zm, "pooled_http", _boom)
        assert zm._access_token() == "already-here"

    def test_missing_token_self_heals_and_writes_env(self, monkeypatch, tmp_path):
        _creds(monkeypatch)  # no ZOHO_TOKEN
        env = tmp_path / ".env"
        env.write_text("ZOHO_DC=in\n", encoding="utf-8")
        monkeypatch.setattr(zm, "GW_ENV", env)
        factory, cap = _fake_http("minted-123")
        monkeypatch.setattr(zm, "pooled_http", factory)

        tok = zm._access_token()

        assert tok == "minted-123"
        # It was exchanged against the refresh-token grant.
        assert cap["data"]["grant_type"] == "refresh_token"
        assert cap["data"]["refresh_token"] == "rtok"
        # Written back to .env AND promoted into the process env.
        assert "ZOHO_TOKEN=minted-123" in env.read_text(encoding="utf-8")
        import os
        assert os.environ["ZOHO_TOKEN"] == "minted-123"

    def test_refresh_replaces_an_existing_token_line(self, monkeypatch, tmp_path):
        _creds(monkeypatch)
        env = tmp_path / ".env"
        env.write_text("ZOHO_TOKEN=stale\nZOHO_DC=in\n", encoding="utf-8")
        monkeypatch.setattr(zm, "GW_ENV", env)
        factory, _ = _fake_http("fresh-999")
        monkeypatch.setattr(zm, "pooled_http", factory)

        out = zm.refresh(write_env=True)

        assert out["ok"] is True and out["access_token"] == "fresh-999"
        text = env.read_text(encoding="utf-8")
        assert "ZOHO_TOKEN=fresh-999" in text
        assert "ZOHO_TOKEN=stale" not in text

    def test_refresh_without_write_env_still_returns_token(self, monkeypatch,
                                                           tmp_path):
        _creds(monkeypatch)
        env = tmp_path / ".env"
        env.write_text("ZOHO_DC=in\n", encoding="utf-8")
        monkeypatch.setattr(zm, "GW_ENV", env)
        factory, _ = _fake_http("mem-777")
        monkeypatch.setattr(zm, "pooled_http", factory)

        out = zm.refresh(write_env=False)

        assert out["ok"] is True and out["access_token"] == "mem-777"
        # The on-disk .env is untouched when write_env is False.
        assert "ZOHO_TOKEN" not in env.read_text(encoding="utf-8")

    def test_missing_client_credentials_fail_soft(self, monkeypatch):
        _creds(monkeypatch)
        monkeypatch.delenv("ZOHO_CLIENT_ID", raising=False)
        monkeypatch.delenv("ZOHO_CLIENT_SECRET", raising=False)
        out = zm.refresh(write_env=False)
        assert out["ok"] is False and "ZOHO_CLIENT_ID" in out["error"]

    def test_missing_refresh_token_fail_soft(self, monkeypatch):
        _creds(monkeypatch)
        monkeypatch.delenv("ZOHO_REFRESH_TOKEN", raising=False)
        out = zm.refresh(write_env=False)
        assert out["ok"] is False and "ZOHO_REFRESH_TOKEN" in out["error"]

    def test_token_endpoint_error_fail_soft(self, monkeypatch, tmp_path):
        _creds(monkeypatch)
        monkeypatch.setattr(zm, "GW_ENV", tmp_path / ".env")
        factory, _ = _fake_http(raises=RuntimeError("network down"))
        monkeypatch.setattr(zm, "pooled_http", factory)
        out = zm.refresh(write_env=False)
        assert out["ok"] is False and "network down" in out["error"]

    def test_response_without_access_token_fail_soft(self, monkeypatch):
        _creds(monkeypatch)
        factory, _ = _fake_http(json_body={"expires_in": 3600})  # no token
        monkeypatch.setattr(zm, "pooled_http", factory)
        out = zm.refresh(write_env=False)
        assert out["ok"] is False and "access_token" in out["error"]

    def test_access_token_raises_when_refresh_fails(self, monkeypatch):
        _creds(monkeypatch)
        monkeypatch.delenv("ZOHO_REFRESH_TOKEN", raising=False)  # force failure
        import pytest
        with pytest.raises(zm._MissingKey):
            zm._access_token()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))