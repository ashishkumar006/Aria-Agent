"""Rigid tests for the 11 tools added in the expansion round (26 -> 37).

Per tool: response SHAPE on success (mocked HTTP), FAIL-SOFT behavior
(network down -> {"ok": False}, never raises), and input clamping.
Plus cross-cutting: catalog input_schema matches the function signature,
and every tools_allowed name in agent_config.yaml resolves in the catalog
(so a typo can never silently disarm a skill).
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server as mcp
from skills import _TOOL_CATALOG

NEW_TOOLS = [
    "arxiv_search", "wikipedia_search", "openalex_search", "news_search",
    "fetch_pdf", "extract_tables", "calendar_query", "delete_file",
    "search_files", "slack_history", "wayback_fetch",
]


class _Resp:
    def __init__(self, payload=None, text="", status=200, headers=None):
        self._payload = payload
        self.text = text
        self.status_code = status
        self.headers = headers or {}
        self.content = text.encode()

    def json(self):
        if self._payload is not None:
            return self._payload
        import json as _j
        return _j.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _Client:
    """Drop-in for httpx.Client, scripted per test via `routes`."""
    routes: dict = {}
    seen: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _match(self, url, params=None):
        _Client.seen.append((url, params or {}))
        for key, resp in _Client.routes.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected URL in test: {url}")

    def get(self, url, **k):
        return self._match(url, k.get("params"))

    def head(self, url, **k):
        return self._match(url, k.get("params"))

    def post(self, url, **k):
        return self._match(url, k.get("params"))


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    import httpx as _hx
    _Client.routes = {}
    _Client.seen = []
    monkeypatch.setattr(_hx, "Client", _Client)
    yield
    _Client.routes = {}


ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Attention Is All You Need</title>
<id>http://arxiv.org/abs/1706.03762v7</id>
<published>2017-06-12T17:57:34Z</published>
<author><name>Ashish Vaswani</name></author>
<author><name>Noam Shazeer</name></author>
<summary>We propose the Transformer.</summary>
<link href="http://arxiv.org/abs/1706.03762v7" rel="alternate" type="text/html"/>
<link title="pdf" href="http://arxiv.org/pdf/1706.03762v7" rel="related" type="application/pdf"/>
</entry></feed>"""


class TestArxivSearch:
    def test_shape(self):
        _Client.routes = {"export.arxiv.org": _Resp(text=ARXIV_XML)}
        out = mcp.arxiv_search("transformer", 3)
        assert isinstance(out, list) and len(out) == 1
        p = out[0]
        assert p["title"] == "Attention Is All You Need"
        assert p["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
        assert p["published"] == "2017-06-12"
        assert "Transformer" in p["summary"]
        assert p["pdf_url"] == "http://arxiv.org/pdf/1706.03762v7"
        assert p["id"].endswith("1706.03762v7")

    def test_fail_soft(self):
        _Client.routes = {"export.arxiv.org": ConnectionError("down")}
        out = mcp.arxiv_search("x")
        assert out.get("ok") is False and "error" in out

    def test_clamps(self):
        _Client.routes = {"export.arxiv.org": _Resp(text=ARXIV_XML)}
        assert isinstance(mcp.arxiv_search("x", 999), list)
        assert isinstance(mcp.arxiv_search("x", "junk"), list)


class TestWikipediaSearch:
    def test_shape(self):
        _Client.routes = {
            "w/api.php": _Resp(payload={"query": {"search": [
                {"title": "Transistor",
                 "snippet": 'A <span class="searchmatch">transistor</span> is'}]}}),
            "rest_v1": _Resp(payload={"extract": "A transistor is a device."}),
        }
        out = mcp.wikipedia_search("transistor")
        assert len(out["results"]) == 1
        assert out["results"][0]["title"] == "Transistor"
        assert "searchmatch" not in out["results"][0]["snippet"]
        assert out["results"][0]["url"].endswith("/wiki/Transistor")
        assert out["top_summary"] == "A transistor is a device."

    def test_fail_soft(self):
        _Client.routes = {"wikipedia.org": ConnectionError("down")}
        assert mcp.wikipedia_search("x").get("ok") is False


class TestOpenAlexSearch:
    def test_shape(self):
        _Client.routes = {"api.openalex.org": _Resp(payload={"results": [{
            "title": "CRISPR screens", "publication_year": 2021,
            "cited_by_count": 42, "doi": "https://doi.org/10.1/abc",
            "id": "https://openalex.org/W1",
            "authorships": [{"author": {"display_name": "J. Doe"}}, {}],
            "primary_location": {"landing_page_url": "https://j.io",
                                 "pdf_url": "https://j.io/a.pdf"},
            "open_access": {"is_oa": True, "oa_url": "https://j.io/a.pdf"}}]})}
        out = mcp.openalex_search("crispr", 3)
        assert len(out) == 1
        w = out[0]
        assert (w["title"], w["year"], w["cited_by"]) == ("CRISPR screens", 2021, 42)
        assert w["authors"] == ["J. Doe"]  # empty author dict skipped
        assert w["doi"] == "10.1/abc"
        assert w["open_access_url"] == "https://j.io/a.pdf"
        assert w["is_open_access"] is True

    def test_fail_soft(self):
        _Client.routes = {"openalex": TimeoutError("slow")}
        assert mcp.openalex_search("x").get("ok") is False


class TestNewsSearch:
    def test_shape(self):
        _Client.routes = {"gdeltproject": _Resp(payload={"articles": [
            {"title": "Battery breakthrough", "url": "https://n.io/1",
             "sourceCommonName": "NewsOrg", "seendate": "20260929T000000Z"}]})}
        out = mcp.news_search("batteries", 7)
        assert len(out) == 1
        assert (out[0]["title"], out[0]["source"], out[0]["seen"]) == \
            ("Battery breakthrough", "NewsOrg", "20260929T000000Z")

    def test_fail_soft_and_clamps(self, monkeypatch):
        monkeypatch.delenv("GUARDIAN_API_KEY", raising=False)
        _Client.routes = {"gdeltproject": ConnectionError("down")}
        assert mcp.news_search("x").get("ok") is False
        # empty GDELT no longer returns [] directly — it falls through to
        # HN (unmocked here, so the unexpected-URL error is swallowed) and
        # then to the keyless fail-soft tail.
        _Client.routes = {"gdeltproject": _Resp(payload={"articles": []})}
        out = mcp.news_search("x", 9999, 999)
        assert out.get("ok") is False
        # timespan for a single day uses hours
        _Client.routes = {"gdeltproject": _Resp(payload={"articles": [
            {"title": "t", "url": "https://n.io", "sourceCommonName": "s",
             "seendate": "d"}]})}
        mcp.news_search("x", 1)
        assert _Client.seen[-1][1].get("timespan") == "24hours"

    def test_falls_through_to_hn_when_gdelt_empty(self, monkeypatch):
        monkeypatch.delenv("GUARDIAN_API_KEY", raising=False)
        _Client.routes = {
            "gdeltproject": _Resp(payload={"articles": []}),
            "hn.algolia": _Resp(payload={"hits": [
                {"title": "Show HN: fast DB", "url": "https://db.io",
                 "objectID": "1", "created_at": "2026-09-29T00:00:00Z"}]}),
        }
        out = mcp.news_search("database", 7, 3)
        assert len(out) == 1
        assert out[0]["source"] == "Hacker News"
        assert out[0]["url"] == "https://db.io"

    def test_falls_through_to_guardian_with_key(self, monkeypatch):
        monkeypatch.setenv("GUARDIAN_API_KEY", "test-key")
        _Client.routes = {
            "gdeltproject": _Resp(payload={"articles": []}),
            "hn.algolia": _Resp(payload={"hits": []}),
            "content.guardianapis": _Resp(payload={"response": {"results": [
                {"webTitle": "Budget passes", "webUrl": "https://g.uk/1",
                 "webPublicationDate": "2026-09-29"}]}}),
        }
        out = mcp.news_search("budget", 7, 3)
        assert len(out) == 1
        assert out[0]["source"] == "The Guardian"

    def test_all_empty_without_key_is_fail_soft(self, monkeypatch):
        monkeypatch.delenv("GUARDIAN_API_KEY", raising=False)
        _Client.routes = {
            "gdeltproject": _Resp(payload={"articles": []}),
            "hn.algolia": _Resp(payload={"hits": []}),
        }
        out = mcp.news_search("zzz-no-such-news", 7, 3)
        assert out.get("ok") is False and "GUARDIAN_API_KEY" in out["error"]


class TestFetchPdf:
    def test_passthrough(self, monkeypatch):
        import sys as _sys
        import types as _t

        class _Page:
            def __init__(self, text):
                self._text = text

            def extract_text(self):
                return self._text

        class _Reader:
            def __init__(self, buf):
                assert buf.getvalue() == b"%PDF-fake"
                self.pages = [_Page("paper text")]
                self.metadata = None
                self.is_encrypted = False

        fake = _t.ModuleType("pypdf")
        fake.PdfReader = _Reader
        monkeypatch.setitem(_sys.modules, "pypdf", fake)
        _Client.routes = {"arxiv.org": _Resp(
            text="", headers={"content-type": "application/pdf",
                              "content-length": "9"})}
        # HEAD and GET both hit the route; GET needs the body
        orig_match = _Client._match

        def _match(self, url, params=None):
            r = orig_match(self, url, params)
            if url.endswith(".pdf") or "/pdf/" in url:
                object.__setattr__(r, "content", b"%PDF-fake")
            return r

        monkeypatch.setattr(_Client, "_match", _match, raising=False)
        out = asyncio.run(mcp.fetch_pdf("https://arxiv.org/pdf/1"))
        # A PDF is attacker-controllable like any remote page, so the text now
        # arrives inside the untrusted envelope (see test_prompt_injection.py).
        # The extracted content itself must be untouched.
        assert "paper text" in out["text"]
        assert "<<<UNTRUSTED_WEB_CONTENT url=https://arxiv.org/pdf/1>>>" in out["text"]
        assert out["untrusted"] is True
        assert out["injection_flags"] == []
        assert out["pages"] == 1

    def test_refuses_html_landing_page(self):
        _Client.routes = {"x": _Resp(
            text="<html>", headers={"content-type": "text/html"})}
        out = asyncio.run(mcp.fetch_pdf("https://example.com/abs/1"))
        assert out.get("ok") is False and "not a PDF" in out["error"]

    def test_refuses_oversize(self):
        _Client.routes = {"x": _Resp(
            text="", headers={"content-type": "application/pdf",
                              "content-length": "999999999"})}
        out = asyncio.run(mcp.fetch_pdf("https://example.com/big.pdf"))
        assert out.get("ok") is False and "too large" in out["error"]


class TestExtractTables:
    HTML = ("<html><body><table><thead><tr><th>A</th><th>B</th></tr></thead>"
            "<tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td></tr></tbody>"
            "</table><table><tr><td>lonely</td></tr></table></body></html>")

    def test_shape(self):
        _Client.routes = {"x": _Resp(text=self.HTML)}
        out = mcp.extract_tables("https://example.com/t")
        assert out["n_tables"] == 2
        assert out["tables"][0]["headers"] == ["A", "B"]
        assert out["tables"][0]["rows"] == [["1", "2"], ["3"]]
        assert out["tables"][1]["rows"] == [["lonely"]]

    def test_no_tables(self):
        _Client.routes = {"x": _Resp(text="<html><body><p>hi</p></body></html>")}
        out = mcp.extract_tables("https://example.com/")
        assert out["tables"] == [] and out["n_tables"] == 0

    def test_fail_soft(self):
        _Client.routes = {"x": ConnectionError("down")}
        assert mcp.extract_tables("https://example.com/").get("ok") is False


class TestCalendarQuery:
    def test_passthrough(self, monkeypatch):
        seen = {}

        def _fake(service, op, args):
            seen.update({"service": service, "op": op, "args": args})
            return {"ok": True, "events": []}

        monkeypatch.setattr(mcp, "_gw_integration", _fake)
        out = mcp.calendar_query("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", 5)
        assert out == {"ok": True, "events": []}
        assert (seen["service"], seen["op"]) == ("calendar", "list")
        assert seen["args"]["max_results"] == 5


class TestDeleteFile:
    def test_file_and_empty_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        (tmp_path / "a.txt").write_text("x")
        assert mcp.delete_file("a.txt") == {"ok": True, "path": "a.txt", "deleted": "file"}
        assert not (tmp_path / "a.txt").exists()
        (tmp_path / "d").mkdir()
        assert mcp.delete_file("d")["deleted"] == "dir"

    def test_refuses_nonempty_dir_and_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        (tmp_path / "d2").mkdir()
        (tmp_path / "d2" / "f").write_text("x")
        with pytest.raises(ValueError, match="not empty"):
            mcp.delete_file("d2")
        with pytest.raises(ValueError, match="does not exist"):
            mcp.delete_file("nope.txt")

    def test_cannot_escape_sandbox(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        with pytest.raises(ValueError, match="escapes the sandbox"):
            mcp.delete_file("../outside.txt")


class TestSearchFiles:
    def test_finds_with_line_numbers(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        (tmp_path / "a.py").write_text("def fetch_url():\n    pass\n")
        (tmp_path / "b.txt").write_text("nothing here\n")
        out = mcp.search_files("def fetch_", ".", 10)
        assert out["n_hits"] == 1
        assert out["hits"][0]["file"] == "a.py"
        assert out["hits"][0]["line"] == 1
        assert "def fetch_" in out["hits"][0]["text"]
        assert out["files_scanned"] == 2

    def test_bad_regex_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        with pytest.raises(ValueError, match="bad regex"):
            mcp.search_files("([", ".")

    def test_skips_binary_and_caps(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02needle\xff")
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("needle\n" * 3)
        out = mcp.search_files("needle", ".", max_hits=4)
        assert out["n_hits"] == 4 and out["truncated"] is True
        assert all(h["file"] != "bin.dat" for h in out["hits"])


class TestSlackHistory:
    def test_passthrough(self, monkeypatch):
        seen = {}

        def _fake(service, op, args):
            seen.update({"service": service, "op": op, "args": args})
            return {"ok": True, "messages": []}

        monkeypatch.setattr(mcp, "_gw_integration", _fake)
        assert mcp.slack_history("C123", 10) == {"ok": True, "messages": []}
        assert (seen["service"], seen["op"]) == ("slack", "history")


class TestExtractPage:
    """The fetch engine itself: trafilatura extraction, bs4 fallback,
    truncation, and the outer timeout cap."""

    def test_extract_with_tables(self, monkeypatch):
        import sys as _sys
        import types as _t
        fake = _t.ModuleType("trafilatura")
        fake.fetch_url = lambda url: "<html><body><article><h1>T</h1><p>Body text here.</p></article></body></html>"
        fake.extract = lambda html, **k: "# T\n\nBody text here." if "<h1>" in html else None
        monkeypatch.setitem(_sys.modules, "trafilatura", fake)
        out = asyncio.run(mcp._extract_page("https://example.com/x"))
        assert out["status"] == 200
        assert "Body text here." in out["text"]
        assert out["truncated"] is False

    def test_bs4_fallback_when_no_article(self, monkeypatch):
        import sys as _sys
        import types as _t
        fake = _t.ModuleType("trafilatura")
        fake.fetch_url = lambda url: "<html><body><div>loose text</div><script>var x=1;</script></body></html>"
        fake.extract = lambda html, **k: None
        monkeypatch.setitem(_sys.modules, "trafilatura", fake)
        out = asyncio.run(mcp._extract_page("https://example.com/y"))
        assert "loose text" in out["text"]
        assert "var x=1" not in out["text"]  # scripts stripped

    def test_empty_fetch(self, monkeypatch):
        import sys as _sys
        import types as _t
        fake = _t.ModuleType("trafilatura")
        fake.fetch_url = lambda url: None
        fake.extract = lambda html, **k: None
        monkeypatch.setitem(_sys.modules, "trafilatura", fake)
        out = asyncio.run(mcp._extract_page("https://example.com/z"))
        assert out["status"] == 404

    def test_truncation(self, monkeypatch):
        import sys as _sys
        import types as _t
        fake = _t.ModuleType("trafilatura")
        fake.fetch_url = lambda url: "<html></html>"
        fake.extract = lambda html, **k: "w" * 100
        monkeypatch.setitem(_sys.modules, "trafilatura", fake)
        out = asyncio.run(mcp._extract_page("https://example.com/w", max_chars=10))
        assert out["truncated"] is True
        assert len(out["text"]) > 10  # cap + notice


class TestWaybackFetch:
    CDX = [["20240101120000", "https://example.com/old", "200", "abc123"]]

    def test_snapshot_url_and_text(self, monkeypatch):
        async def _fake(url, timeout_s=20, max_chars=20000):
            assert "web.archive.org/web/20240101120000id_" in url
            return {"status": 200, "text": "old content", "length_bytes": 11}

        monkeypatch.setattr(mcp, "_extract_page", _fake)
        _Client.routes = {"web.archive.org": _Resp(payload=self.CDX)}
        out = asyncio.run(mcp.wayback_fetch("https://example.com/old"))
        assert out["text"] == "old content"
        assert out["snapshot_timestamp"] == "20240101120000"
        assert out["snapshot_url"].startswith("https://web.archive.org/web/")

    def test_no_snapshot(self):
        _Client.routes = {"web.archive.org": _Resp(payload=[])}
        out = asyncio.run(mcp.wayback_fetch("https://example.com/nonexistent"))
        assert out.get("ok") is False and "no archived snapshot" in out["error"]

    def test_lookup_failure(self):
        _Client.routes = {"web.archive.org": ConnectionError("down")}
        out = asyncio.run(mcp.wayback_fetch("https://example.com/"))
        assert out.get("ok") is False


class TestHttpRetry:
    def test_retries_once_on_429_then_succeeds(self):
        calls = []

        class _Flaky(_Client):
            def get(self, url, **k):
                calls.append(url)
                if len(calls) == 1:
                    r = _Resp(text="throttled", status=429)
                    return r
                return _Resp(payload={"results": []})

        import httpx as _hx
        _hx.Client = _Flaky
        try:
            out = mcp.openalex_search("x")
        finally:
            _hx.Client = _Client
        assert out == []
        assert len(calls) == 2

    def test_gives_up_after_retry(self):
        class _Down(_Client):
            def get(self, url, **k):
                return _Resp(text="throttled", status=429)

        import httpx as _hx
        _hx.Client = _Down
        try:
            out = mcp.openalex_search("x")
        finally:
            _hx.Client = _Client
        assert out.get("ok") is False


# ── cross-cutting ────────────────────────────────────────────────────────

def _sig_params(fn):
    return [p for p in inspect.signature(fn).parameters if p not in ("self",)]


class TestSchemaSync:
    """The catalog input_schema is what the model sees. If it names a param
    the function doesn't take (or misses a required one), the tool call
    fails at runtime — after burning an LLM round-trip. This pins them."""

    @pytest.mark.parametrize("name", NEW_TOOLS)
    def test_schema_matches_signature(self, name):
        fn = getattr(mcp, name)
        schema = _TOOL_CATALOG[name]["input_schema"]
        sig = _sig_params(fn)
        props = schema["properties"]
        required = schema["required"]
        for r in required:
            assert r in sig, f"{name}: required '{r}' not a function param"
            assert r in props, f"{name}: required '{r}' missing from properties"
        for p in sig:
            param = inspect.signature(fn).parameters[p]
            if param.default is inspect.Parameter.empty:
                assert p in required, f"{name}: '{p}' has no default but is not required"

    def test_all_new_tools_registered(self):
        tm = mcp.mcp._tool_manager
        registered = list(tm._tools.keys())
        for name in NEW_TOOLS:
            assert name in registered, f"tool '{name}' not registered"
            assert name in _TOOL_CATALOG, f"tool '{name}' missing from catalog"


class TestWiring:
    """Every tools_allowed name must resolve in the catalog, or the skill
    is silently disarmed (skills.py warns + skips)."""

    def test_allowed_names_resolve(self):
        cfg = yaml.safe_load(
            (ROOT / "agent_config.yaml").read_text(encoding="utf-8"))
        for skill, conf in cfg.items():
            for t in (conf or {}).get("tools_allowed") or []:
                assert t in _TOOL_CATALOG, f"skill '{skill}' allows unknown tool '{t}'"

    def test_new_tools_reachable_by_a_skill(self):
        cfg = yaml.safe_load(
            (ROOT / "agent_config.yaml").read_text(encoding="utf-8"))
        reachable = {t for conf in cfg.values()
                     for t in ((conf or {}).get("tools_allowed") or [])}
        for name in NEW_TOOLS:
            assert name in reachable, f"tool '{name}' wired to no skill"
