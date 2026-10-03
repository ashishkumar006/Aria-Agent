"""Fundamental suite for keyed integrations (offline, no servers).

Contract under test: missing credentials fail SOFT ({"ok": False} naming
the key — never raise, never touch the network), and success paths parse
provider responses with httpx faked at the client seam.

Run: uv run pytest integrations -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit


class _FakeClient:
    """Stand-in for httpx.Client (context manager + get/post)."""

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

    def patch(self, url, **kw):
        return self._call("PATCH", url, **kw)


def _patch_client(monkeypatch, handler):
    import httpx

    box: dict = {}

    def _factory(*a, **k):
        box["client"] = _FakeClient(handler)
        return box["client"]

    monkeypatch.setattr(httpx, "Client", _factory)
    return box


# ── gmail ────────────────────────────────────────────────────────────────

class TestGmail:
    def test_missing_key_fail_soft(self, monkeypatch):
        import integrations.gmail as g

        monkeypatch.delenv("GMAIL_TOKEN", raising=False)
        r = g.send_email(to="a@b.c", subject="s", body="b")
        assert r["ok"] is False and "GMAIL_TOKEN" in r["error"]
        r = g.query(api_method="list")
        assert r["ok"] is False and "GMAIL_TOKEN" in r["error"]

    def test_send_success(self, monkeypatch):
        import integrations.gmail as g

        monkeypatch.setenv("GMAIL_TOKEN", "tok")
        box = _patch_client(
            monkeypatch,
            lambda m, url, **kw: kit.FakeResp(200, {"id": "m9"}))
        r = g.send_email(to="a@b.c", subject="s", body="b")
        assert r == {"ok": True, "to": "a@b.c", "subject": "s",
                     "id": "m9", "thread_id": None}
        assert box["client"].calls[0]["url"].endswith("messages/send")

    def test_query_list(self, monkeypatch):
        import integrations.gmail as g

        monkeypatch.setenv("GMAIL_TOKEN", "tok")
        _patch_client(
            monkeypatch,
            lambda m, url, **kw: kit.FakeResp(
                200, {"messages": [{"id": "m1"}, {"id": "m2"}]}))
        r = g.query(api_method="list", query="is:unread")
        assert r == {"ok": True, "message_ids": ["m1", "m2"]}

    def test_unknown_method(self, monkeypatch):
        import integrations.gmail as g

        monkeypatch.setenv("GMAIL_TOKEN", "tok")
        _patch_client(
            monkeypatch, lambda m, url, **kw: kit.FakeResp(200, {}))
        r = g.query(api_method="nope")
        assert r["ok"] is False and "nope" in r["error"]

    def test_refresh_updates_process_env(self, monkeypatch, tmp_path):
        """Refresh must take effect without a restart: the running process
        reads process env, not the file."""
        import os

        import integrations.gmail as g

        monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "rt")
        monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
        monkeypatch.setenv("GMAIL_CLIENT_SECRET", "csec")
        monkeypatch.setenv("GMAIL_TOKEN", "old-token")
        monkeypatch.setattr(g, "GW_ENV", tmp_path / ".env")
        _patch_client(
            monkeypatch,
            lambda m, url, **kw: kit.FakeResp(
                200, {"access_token": "new-token", "expires_in": 3600}))
        r = g.refresh()
        assert r["ok"] is True and r["access_token"] == "new-token"
        assert os.environ.get("GMAIL_TOKEN") == "new-token"
        assert "GMAIL_TOKEN=new-token" in (tmp_path / ".env").read_text()


# ── github ───────────────────────────────────────────────────────────────

class TestGithub:
    def test_missing_key_fail_soft(self, monkeypatch):
        import integrations.github as g

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        r = g.query(api_method="list_repos")
        assert r["ok"] is False and "GITHUB_TOKEN" in r["error"]

    def test_list_repos(self, monkeypatch):
        import integrations.github as g

        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        _patch_client(
            monkeypatch,
            lambda m, url, **kw: kit.FakeResp(
                200, [{"name": "r", "html_url": "u"}]))
        r = g.query(api_method="list_repos")
        assert r == {"ok": True, "repos": [{"name": "r", "url": "u"}]}


# ── notion ───────────────────────────────────────────────────────────────

class TestNotion:
    def test_missing_key_fail_soft(self, monkeypatch):
        import integrations.notion as n

        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        r = n.query(api_method="get_page", page_id="p")
        assert r["ok"] is False and "NOTION_TOKEN" in r["error"]

    def test_list_pages_posts(self, monkeypatch):
        """Notion /v1/search is POST-only — GET silently drops the body."""
        import integrations.notion as n

        monkeypatch.setenv("NOTION_TOKEN", "tok")
        box = _patch_client(
            monkeypatch,
            lambda m, url, **kw: kit.FakeResp(
                200, {"results": [{"id": "p1", "properties": {}}]}))
        r = n.query(api_method="list_pages")
        assert r == {"ok": True, "pages": [{"id": "p1", "title": ""}]}
        assert box["client"].calls[0]["method"] == "POST"
        assert box["client"].calls[0]["url"].endswith("/v1/search")

    def test_create_page_body_failure_is_loud(self, monkeypatch):
        import integrations.notion as n

        monkeypatch.setenv("NOTION_TOKEN", "tok")

        def _handler(method, url, **kw):
            if url.endswith("/v1/pages"):
                return kit.FakeResp(200, {"id": "new1"})
            raise RuntimeError("patch boom")

        _patch_client(monkeypatch, _handler)
        r = n.query(api_method="create_page", page_id="p",
                    title="t", body="hello")
        assert r["ok"] is True and r["id"] == "new1"
        assert r["body_appended"] is False
        assert "warning" in r

    def test_create_page_body_ok(self, monkeypatch):
        import integrations.notion as n

        monkeypatch.setenv("NOTION_TOKEN", "tok")
        _patch_client(
            monkeypatch, lambda m, url, **kw: kit.FakeResp(200, {"id": "n1"}))
        r = n.query(api_method="create_page", page_id="p",
                    title="t", body="hello")
        assert r["body_appended"] is True

    def test_page_title_helper(self):
        import integrations.notion as n

        page = {"properties": {"Name": {"type": "title",
                                       "title": [{"plain_text": "T"}]}}}
        assert n._page_title(page) == "T"
        assert n._page_title({}) == ""


# ── calendar ─────────────────────────────────────────────────────────────

class TestCalendar:
    def test_missing_key_fail_soft(self, monkeypatch):
        import integrations.calendar as c

        monkeypatch.delenv("GOOGLE_CALENDAR_TOKEN", raising=False)
        r = c.create_event(summary="s", start="2026-01-01T00:00:00Z",
                           end="2026-01-01T01:00:00Z")
        assert r["ok"] is False and "GOOGLE_CALENDAR_TOKEN" in r["error"]


# ── websearch (backends mocked, usage store mocked) ──────────────────────

class TestWebsearch:
    def test_tavily_path(self, monkeypatch):
        import integrations.websearch as w

        monkeypatch.setenv("TAVILY_API_KEY", "tok")
        monkeypatch.setattr(
            w, "_load_usage",
            lambda: {"tavily": {"count": 0, "errors": 0},
                     "duckduckgo": {"count": 0, "errors": 0}})
        bumped: list = []
        monkeypatch.setattr(w, "_bump", lambda *a, **k: bumped.append(a))
        monkeypatch.setattr(
            w, "_tavily_search",
            lambda q, n: [{"title": "T", "url": "u", "snippet": "s"}])
        out = w.search(query="q")
        assert out == [{"title": "T", "url": "u", "snippet": "s"}]
        assert ("tavily",) in bumped

    def test_ddg_fallback_without_key(self, monkeypatch):
        import integrations.websearch as w

        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.setattr(
            w, "_load_usage",
            lambda: {"tavily": {"count": 0, "errors": 0},
                     "duckduckgo": {"count": 0, "errors": 0}})
        monkeypatch.setattr(w, "_bump", lambda *a, **k: None)
        monkeypatch.setattr(
            w, "_ddg_search",
            lambda q, n: [{"title": "D", "url": "u", "snippet": "s"}])
        assert w.search(query="q")[0]["title"] == "D"

    def test_tavily_cap_falls_back(self, monkeypatch):
        import integrations.websearch as w

        monkeypatch.setenv("TAVILY_API_KEY", "tok")
        monkeypatch.setattr(
            w, "_load_usage",
            lambda: {"tavily": {"count": 10 ** 9, "errors": 0},
                     "duckduckgo": {"count": 0, "errors": 0}})
        monkeypatch.setattr(w, "_bump", lambda *a, **k: None)
        monkeypatch.setattr(
            w, "_tavily_search",
            lambda q, n: (_ for _ in ()).throw(Exception("must not run")))
        monkeypatch.setattr(
            w, "_ddg_search",
            lambda q, n: [{"title": "D", "url": "u", "snippet": "s"}])
        assert w.search(query="q")[0]["title"] == "D"


# ── calendar oauth ─────────────────────────────────────────────────

CAL_KEYS = ["GOOGLE_CALENDAR_TOKEN", "GOOGLE_CALENDAR_REFRESH_TOKEN",
            "GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET"]


class TestCalendar:
    def test_missing_token_fail_soft(self, monkeypatch):
        import integrations.calendar as c

        for k in CAL_KEYS:
            monkeypatch.delenv(k, raising=False)
        r = c.create_event(summary="s", start="2026-01-01T10:00:00",
                           end="2026-01-01T11:00:00")
        assert r["ok"] is False and "GOOGLE_CALENDAR_TOKEN" in r["error"]

    def test_missing_triple_fail_soft(self, monkeypatch):
        import integrations.calendar as c

        for k in CAL_KEYS:
            monkeypatch.delenv(k, raising=False)
        r = c.refresh()
        assert r["ok"] is False and "GOOGLE_CALENDAR_REFRESH_TOKEN" in r["error"]

    def test_refresh_rotates_token(self, monkeypatch, tmp_path):
        import os
        import integrations.calendar as c
        import _adaptor_kit as kit

        saved = {k: os.environ.get(k) for k in CAL_KEYS}
        try:
            for k, v in [("GOOGLE_CALENDAR_REFRESH_TOKEN", "rt-old"),
                         ("GOOGLE_CALENDAR_CLIENT_ID", "cid"),
                         ("GOOGLE_CALENDAR_CLIENT_SECRET", "csec")]:
                monkeypatch.setenv(k, v)
            monkeypatch.setattr(c, "GW_ENV", tmp_path / ".env")
            # calendar uses httpx.Client directly — fake that seam
            import httpx

            made = {}

            class _Resp:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return {"access_token": "at-new", "expires_in": 3600}

            class _Client:
                def __init__(self, *a, **k):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def post(self, url, **kw):
                    made["url"] = url
                    return _Resp()

            monkeypatch.setattr(httpx, "Client", _Client)
            r = c.refresh()
            assert r["ok"] is True and r["access_token"] == "at-new"
            assert made["url"].endswith("oauth2.googleapis.com/token")
            assert os.environ.get("GOOGLE_CALENDAR_TOKEN") == "at-new"
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_create_retries_once_on_401(self, monkeypatch, tmp_path):
        import os
        import integrations.calendar as c
        import httpx

        saved = {k: os.environ.get(k) for k in CAL_KEYS}
        try:
            for k, v in [("GOOGLE_CALENDAR_TOKEN", "at-old"),
                         ("GOOGLE_CALENDAR_REFRESH_TOKEN", "rt"),
                         ("GOOGLE_CALENDAR_CLIENT_ID", "cid"),
                         ("GOOGLE_CALENDAR_CLIENT_SECRET", "csec")]:
                monkeypatch.setenv(k, v)
            monkeypatch.setattr(c, "GW_ENV", tmp_path / ".env")
            seen = {"n": 0}

            class _Resp:
                def __init__(self, code, payload):
                    self.status_code = code
                    self._payload = payload

                def raise_for_status(self):
                    if self.status_code >= 400:
                        raise httpx.HTTPStatusError("e", request=None, response=self)

                def json(self):
                    return self._payload

            class _Client:
                def __init__(self, *a, **k):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def post(self, url, **kw):
                    if url.endswith("oauth2.googleapis.com/token"):
                        return _Resp(200, {"access_token": "at-new", "expires_in": 3600})
                    seen["n"] += 1
                    if seen["n"] == 1:
                        return _Resp(401, {"error": "expired"})
                    return _Resp(200, {"id": "ev1", "htmlLink": "http://x"})

            monkeypatch.setattr(httpx, "Client", _Client)
            r = c.create_event(summary="s", start="2026-01-01T10:00:00",
                               end="2026-01-01T11:00:00")
            assert r["ok"] is True and r["id"] == "ev1"
            assert seen["n"] == 2
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# ── slack rotation ───────────────────────────────────────────────────

class TestSlackRefresh:
    KEYS = ["SLACK_BOT_TOKEN", "SLACK_REFRESH_TOKEN",
            "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"]

    def test_missing_triple_fail_soft(self, monkeypatch, tmp_path):
        import integrations.slack as s

        for k in self.KEYS:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(s, "ENV_PATH", tmp_path / ".env")
        r = s.refresh()
        assert r["ok"] is False and "SLACK_REFRESH_TOKEN" in r["error"]

    def test_refresh_rotates_pair(self, monkeypatch, tmp_path):
        import os
        import integrations.slack as s
        import _adaptor_kit as kit

        saved = {k: os.environ.get(k) for k in self.KEYS}
        try:
            for k, v in [("SLACK_BOT_TOKEN", "at-old"),
                         ("SLACK_REFRESH_TOKEN", "rt-old"),
                         ("SLACK_CLIENT_ID", "cid"),
                         ("SLACK_CLIENT_SECRET", "csec")]:
                monkeypatch.setenv(k, v)
            monkeypatch.setattr(s, "ENV_PATH", tmp_path / ".env")
            (tmp_path / ".env").write_text(
                "SLACK_BOT_TOKEN=at-old\nSLACK_REFRESH_TOKEN=rt-old\n",
                encoding="utf-8")
            calls = kit.fake_post_factory(
                monkeypatch,
                lambda url, **kw: kit.FakeResp(200, {
                    "ok": True, "access_token": "at-new",
                    "refresh_token": "rt-new", "expires_in": 43200,
                    "token_type": "bot"}))
            r = s.refresh()
            assert r["ok"] is True and r["rotated_refresh"] is True
            assert r["expires_in"] == 43200
            text = (tmp_path / ".env").read_text(encoding="utf-8")
            assert "SLACK_BOT_TOKEN=at-new" in text
            assert "SLACK_REFRESH_TOKEN=rt-new" in text
            assert calls and calls[0]["url"].endswith("oauth.v2.access")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_refresh_rejected(self, monkeypatch, tmp_path):
        import integrations.slack as s
        import _adaptor_kit as kit

        for k, v in [("SLACK_REFRESH_TOKEN", "rt-bad"),
                     ("SLACK_CLIENT_ID", "cid"),
                     ("SLACK_CLIENT_SECRET", "csec")]:
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(s, "ENV_PATH", tmp_path / ".env")
        kit.fake_post_factory(
            monkeypatch,
            lambda url, **kw: kit.FakeResp(200, {"ok": False,
                                                "error": "invalid_auth"}))
        r = s.refresh()
        assert r["ok"] is False and "invalid_auth" in r["error"]
