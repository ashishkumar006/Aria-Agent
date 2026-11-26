"""Notion create_page: a forgotten parent must not dead-end.

A create_page with no parent used to return a bare "requires a parent
page_id", so a model that had just listed pages had no way to recover in
the same run. It now returns candidate pages (id + title) the caller can
retry with. These tests cover that contract at the httpx boundary; no
credentials or network needed.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integrations import notion as n


def _fake_client(results):
    """A pooled_http stand-in whose /search returns `results`."""

    class _R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": results}

    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **k):
            assert url.endswith("/v1/search")
            return _R()

    return _C()


def _page(pid, title):
    # Shape Notion returns: properties.title[].plain_text
    return {
        "id": pid,
        "properties": {
            "title": {
                "id": "title",
                "type": "title",
                "title": [{"type": "text", "plain_text": title,
                           "text": {"content": title}}],
            }
        },
    }


class TestNotionCreatePage:
    def test_missing_token_fail_soft(self, monkeypatch):
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        out = n.query(api_method="create_page", title="x")
        assert out.get("ok") is False and "NOTION_TOKEN" in out["error"]

    def test_no_parent_returns_candidates(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "fake")
        monkeypatch.setattr(n, "pooled_http",
                            lambda _t: _fake_client([_page("p1", "Home"),
                                                     _page("p2", "Work")]))
        out = n.query(api_method="create_page", title="Aria briefing")
        assert out.get("ok") is False
        assert "parent" in out["error"].lower()
        cands = out.get("candidates") or []
        assert [c["id"] for c in cands] == ["p1", "p2"]
        assert cands[0]["title"] == "Home"
        assert "hint" in out

    def test_no_parent_returns_candidates_via_call(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "fake")
        monkeypatch.setattr(n, "pooled_http",
                            lambda _t: _fake_client([_page("p1", "Home")]))
        out = n.call(api_method="create_page", title="Aria briefing")
        assert out.get("ok") is False
        assert (out.get("candidates") or [{}])[0].get("id") == "p1"

    def test_candidate_lookup_failure_is_soft(self, monkeypatch):
        # If the search for candidates itself blows up, we still fail soft
        # with the clear error rather than raising.
        monkeypatch.setenv("NOTION_TOKEN", "fake")

        class _Boom:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, **k):
                raise RuntimeError("network down")

        monkeypatch.setattr(n, "pooled_http", lambda _t: _Boom())
        out = n.query(api_method="create_page", title="x")
        assert out.get("ok") is False
        assert "parent" in out["error"].lower()
        # no candidates key (or empty) — the point is it did not raise
        assert not out.get("candidates")