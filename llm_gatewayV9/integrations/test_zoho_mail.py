"""Zoho Mail integration tests (offline, httpx faked at the client seam).

Contract: missing creds fail soft; send/query/profile/refresh parse the Zoho
REST shapes. Run: uv run pytest integrations/test_zoho_mail.py -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _call(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self._handler(method, url, **kw)

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def put(self, url, **kw):
        return self._call("PUT", url, **kw)


def _patch_client(monkeypatch, handler):
    import httpx
    import adaptors.http as _h
    _h.reset_pools()
    box: dict = {}

    def _factory(*a, **k):
        box["client"] = _FakeClient(handler)
        return box["client"]

    monkeypatch.setattr(httpx, "Client", _factory)
    return box


def _zoho_handler(method, url, **kw):
    if url.endswith("/oauth/v2/token"):
        return kit.FakeResp(200, {"access_token": "newtok", "expires_in": 3600})
    if url.endswith("/api/accounts"):
        return kit.FakeResp(200, {"data": [
            {"accountId": "123", "emailAddress": "me@zoho.com",
             "primaryEmailAddress": "me@zoho.com"}]})
    if url.endswith("/messages/attachments"):
        return kit.FakeResp(200, {"data": {"attachmentName": "file.txt"}})
    if url.endswith("/messages/view"):
        return kit.FakeResp(200, {"data": [
            {"messageId": "m1", "fromAddress": "a@b.c", "subject": "Hi",
             "snippet": "x", "content": "body", "receivedTime": "t",
             "status": "received"}]})
    if url.endswith("/messages"):
        return kit.FakeResp(200, {"data": {"messageId": "m1"}})
    if url.endswith("/folders"):
        return kit.FakeResp(200, {"data": [{"folderId": "1", "folderName": "Inbox"}]})
    if url.endswith("/updatemessage"):
        return kit.FakeResp(200, {"status": {"code": 200}})
    return kit.FakeResp(404, {})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("ZOHO_TOKEN", "ZOHO_REFRESH_TOKEN", "ZOHO_CLIENT_ID",
              "ZOHO_CLIENT_SECRET", "ZOHO_ACCOUNT_ID", "ZOHO_USER", "ZOHO_DC"):
        monkeypatch.delenv(k, raising=False)
    import integrations.zoho_mail as z
    z._ACCT_CACHE.clear()
    yield
    z._ACCT_CACHE.clear()


class TestZohoMail:
    def test_missing_key_fail_soft(self, monkeypatch):
        import integrations.zoho_mail as z
        r = z.send_email(to="a@b.c", subject="s", body="b")
        assert r["ok"] is False and "ZOHO_TOKEN" in r["error"]
        r = z.query(api_method="list")
        assert r["ok"] is False and "ZOHO_TOKEN" in r["error"]

    def test_send_success(self, monkeypatch):
        import integrations.zoho_mail as z
        monkeypatch.setenv("ZOHO_TOKEN", "tok")
        box = _patch_client(monkeypatch, _zoho_handler)
        r = z.send_email(to="a@b.c", subject="s", body="b")
        assert r["ok"] is True and r["id"] == "m1" and r["from"] == "me@zoho.com", r
        send = [c for c in box["client"].calls
                if c["method"] == "POST" and c["url"].endswith("/messages")][0]
        assert send["json"]["toAddress"] == "a@b.c"
        assert send["json"]["fromAddress"] == "me@zoho.com"
        assert send["json"]["mailFormat"] == "html"

    def test_query_list_and_read(self, monkeypatch):
        import integrations.zoho_mail as z
        monkeypatch.setenv("ZOHO_TOKEN", "tok")
        _patch_client(monkeypatch, _zoho_handler)
        r = z.query(api_method="list", query="unread")
        assert r["ok"] is True and r["message_ids"] == ["m1"], r
        r2 = z.query(api_method="read", query="m1")
        assert r2["ok"] is True and r2["content"] == "body", r2

    def test_profile(self, monkeypatch):
        import integrations.zoho_mail as z
        monkeypatch.setenv("ZOHO_TOKEN", "tok")
        _patch_client(monkeypatch, _zoho_handler)
        r = z.profile()
        assert r["ok"] is True and r["email"] == "me@zoho.com", r
        assert r["account_id"] == "123"

    def test_mark_read(self, monkeypatch):
        import integrations.zoho_mail as z
        monkeypatch.setenv("ZOHO_TOKEN", "tok")
        _patch_client(monkeypatch, _zoho_handler)
        r = z.mark(message_id="m1", mode="read")
        assert r["ok"] is True and r["mode"] == "read", r

    def test_move_and_delete(self, monkeypatch):
        import integrations.zoho_mail as z
        monkeypatch.setenv("ZOHO_TOKEN", "tok")
        _patch_client(monkeypatch, _zoho_handler)
        r = z.move(message_id="m1", dest_folder_id="9000")
        assert r["ok"] is True and r["to_folder"] == "9000", r
        r2 = z.delete(message_id="m1")
        assert r2["ok"] is True and r2["moved_to"] == "Trash", r2

    def test_refresh_without_browser(self, monkeypatch):
        import integrations.zoho_mail as z
        monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "rtok")
        monkeypatch.setenv("ZOHO_CLIENT_ID", "cid")
        monkeypatch.setenv("ZOHO_CLIENT_SECRET", "csec")
        _patch_client(monkeypatch, _zoho_handler)
        r = z.refresh(write_env=False)
        assert r["ok"] is True and r["access_token"] == "newtok"