"""Tests for the web-search provider chain (Tavily -> Brave -> DDG ->
Marginalia). All providers mocked: asserts ORDER, skip-when-unset, monthly
caps, usage accounting, and the fail-soft tail (empty list, never raises)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from integrations import websearch as ws


def _hits(tag):
    return [{"title": f"{tag} title", "url": f"https://{tag}.io/1",
             "snippet": f"{tag} snippet"}]


class TestChainOrder:
    def test_tavily_first_when_keyed(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "k")
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        monkeypatch.setattr(ws, "_tavily_search",
                            lambda q, n: _hits("tavily"))
        monkeypatch.setattr(ws, "_ddg_search",
                            lambda q, n: (_ for _ in ()).throw(
                                AssertionError("must not reach ddg")))
        assert ws.search(query="x")[0]["url"].startswith("https://tavily")

    def test_brave_second_when_tavily_empty(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "k")
        monkeypatch.setenv("BRAVE_API_KEY", "k")
        monkeypatch.setattr(ws, "_tavily_search", lambda q, n: [])
        monkeypatch.setattr(ws, "_brave_search",
                            lambda q, n: _hits("brave"))
        monkeypatch.setattr(ws, "_ddg_search",
                            lambda q, n: (_ for _ in ()).throw(
                                AssertionError("must not reach ddg")))
        assert ws.search(query="x")[0]["url"].startswith("https://brave")

    def test_ddg_then_marginalia_when_no_keys(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        monkeypatch.setattr(ws, "_ddg_search", lambda q, n: [])
        monkeypatch.setattr(ws, "_marginalia_search",
                            lambda q, n: _hits("marg"))
        assert ws.search(query="x")[0]["url"].startswith("https://marg")

    def test_empty_tail_is_fail_soft(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        monkeypatch.setattr(ws, "_ddg_search", lambda q, n: [])
        monkeypatch.setattr(ws, "_marginalia_search", lambda q, n: [])
        assert ws.search(query="x") == []


class TestCapsAndUsage:
    def test_tavily_cap_skips_to_brave(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TAVILY_API_KEY", "k")
        monkeypatch.setenv("BRAVE_API_KEY", "k")
        monkeypatch.setattr(ws, "USAGE_PATH", tmp_path / "u.json")
        monkeypatch.setattr(ws, "MONTHLY_CAP", 0)  # exhausted
        monkeypatch.setattr(ws, "_tavily_search",
                            lambda q, n: (_ for _ in ()).throw(
                                AssertionError("capped tavily must not run")))
        monkeypatch.setattr(ws, "_brave_search",
                            lambda q, n: _hits("brave"))
        assert ws.search(query="x")[0]["url"].startswith("https://brave")

    def test_usage_bumped_per_winner(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ws, "USAGE_PATH", tmp_path / "u.json")
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        monkeypatch.setattr(ws, "_ddg_search", lambda q, n: _hits("ddg"))
        ws.search(query="x")
        data = ws._load_usage()
        assert data["duckduckgo"]["count"] == 1
        assert data["marginalia"]["count"] == 0


class TestProviderShapes:
    def test_brave_shape(self, monkeypatch):
        import httpx as _hx

        class _R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"web": {"results": [
                    {"title": "T", "url": "https://b.io",
                     "description": "D"}]}}

        class _C:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, **k):
                assert "api.search.brave.com" in url
                assert k["headers"]["X-Subscription-Token"] == "k"
                assert k["params"] == {"q": "x", "count": 5}
                return _R()

        monkeypatch.setattr(_hx, "Client", _C)
        monkeypatch.setenv("BRAVE_API_KEY", "k")
        out = ws._brave_search("x", 5)
        assert out == [{"title": "T", "url": "https://b.io", "snippet": "D"}]

    def test_brave_skipped_without_key(self, monkeypatch):
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        assert ws._brave_search("x", 5) == []

    def test_marginalia_shape_and_public_key(self, monkeypatch):
        import httpx as _hx
        seen = {}

        class _R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"results": [
                    {"title": "T", "url": "https://m.io",
                     "description": "D"}]}

        class _C:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, **k):
                seen.update({"url": url, "key": k["headers"]["API-Key"]})
                return _R()

        monkeypatch.setattr(_hx, "Client", _C)
        monkeypatch.delenv("MARGINALIA_API_KEY", raising=False)
        out = ws._marginalia_search("x", 5)
        assert seen["url"].startswith("https://api2.marginalia-search.com/search")
        assert seen["key"] == "public"  # shared key when unset
        assert out == [{"title": "T", "url": "https://m.io", "snippet": "D"}]
        monkeypatch.setenv("MARGINALIA_API_KEY", "personal")
        ws._marginalia_search("x", 5)
        assert seen["key"] == "personal"
