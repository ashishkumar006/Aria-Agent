"""The console reaches the preview through the agent, which is a proxy.

Two things have to hold: the agent must forward the call with the gateway
token, and previewing a DELIVERED file must send the bytes - the artifact
store lives here, not in the gateway, so a gateway-side lookup would find
nothing and the preview would silently fall back to a re-render.
"""
import base64
import contextlib
import json
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    import agent_server
    import auth
    # Do NOT call auth.configure(): it regenerates the per-launch token for the
    # whole process, which broke 32 unrelated tests the first time this was
    # written. Set the module value instead.
    monkeypatch.setattr(auth, "_token", "test-token", raising=False)
    return TestClient(agent_server.app), {"X-Aria-Token": "test-token"}


@contextlib.contextmanager
def tmp_session_root():
    """Point the session store at a temp dir for the duration of a block.

    SESSIONS_ROOT is imported *inside* the endpoint, so patching the module
    attribute of agent_server would do nothing; persistence is what has to be
    redirected.
    """
    import persistence
    root = Path(tempfile.mkdtemp(prefix="aria-sessions-"))
    old = persistence.SESSIONS_ROOT
    persistence.SESSIONS_ROOT = root
    try:
        yield root
    finally:
        persistence.SESSIONS_ROOT = old
        shutil.rmtree(root, ignore_errors=True)


def test_a_stored_document_preview_is_proxied_to_the_gateway(
        client, monkeypatch):
    """The Documents modal asks the agent, never the gateway directly."""
    c, hdr = client
    import agent_server

    seen = {}

    async def _fake(path, **kw):
        seen["path"] = path
        seen["timeout"] = kw.get("timeout")
        return {"mode": "pages", "format": "pdf", "pages": [],
                "page_count": 0}

    monkeypatch.setattr(agent_server, "_gw_json", _fake)
    r = c.get("/api/documents/doc-80c02d11c0/preview", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert seen["path"] == "/v1/documents/doc-80c02d11c0/preview"
    assert seen["timeout"] == 120.0
    assert r.json()["format"] == "pdf"


def test_a_spec_preview_is_proxied_to_the_gateway(client, monkeypatch):
    c, hdr = client
    seen = {}

    class _Resp:
        status_code = 200
        text = '{"mode":"pages","pages":[]}'
        content = b'{"mode":"pages","pages":[]}'

        def json(self):
            return {"mode": "pages", "pages": []}

    class _Client:
        def __init__(self, *a, **kw):
            seen["headers"] = dict(kw.get("headers") or {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, **kw):
            seen["url"] = url
            seen["body"] = json
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    r = c.post("/api/doc/preview",
               json={"format": "pdf", "spec": {"title": "T", "blocks": []}},
               headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert seen["url"].endswith("/v1/docgen/preview")
    assert "X-Gateway-Token" in seen["headers"], \
        "the gateway rejects /v1/* without its token"
    assert seen["body"]["spec"]["title"] == "T"


def test_previewing_an_artifact_sends_its_bytes(client, monkeypatch):
    c, hdr = client
    import artifacts as arts
    art = arts.put(b"%PDF-1.4 test", content_type="application/pdf",
                   source="author:pdf", descriptor="Probe (pdf)")
    seen = {}

    class _Resp:
        status_code = 200
        text = "{}"
        content = b"{}"

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, **kw):
            seen.update(json or {})
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    r = c.post("/api/doc/preview", json={"artifact": art}, headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert seen.get("blob_b64"), "the bytes did not travel with the request"
    assert base64.b64decode(seen["blob_b64"]).startswith(b"%PDF")
    assert seen.get("format") == "pdf", "the format was not inferred"


def test_a_malformed_handle_is_refused_before_any_call(client, monkeypatch):
    c, hdr = client

    def _boom(*a, **kw):
        raise AssertionError("the gateway was called for a malformed handle")

    import httpx
    monkeypatch.setattr(httpx, "Client", _boom)
    r = c.post("/api/doc/preview", json={"artifact": "art:../../etc/passwd"},
               headers=hdr)
    assert r.status_code == 400
    assert "malformed" in r.json()["error"].lower()


def test_an_unknown_artifact_is_a_404_not_a_500(client):
    c, hdr = client
    r = c.post("/api/doc/preview",
               json={"artifact": "art:ffffffffffffffff"}, headers=hdr)
    assert r.status_code == 404, r.text[:200]


def test_authoring_sends_no_setup_because_it_offers_none():
       """The Setup card is gone, so the page must not pretend otherwise.

       It used to send a full `doc_setup` of defaults while offering nothing to
       change them: the renderer was told "the user chose A4, portrait, Report,
       cover page" when the user had chosen nothing. That is a lie in the
       request, and it is worse than sending none. The page now offers exactly
       one setup control - the style - and that is the ONLY key it may send,
       only when the user picked something other than Auto. The format still
       travels in the brief ("as a PDF").
       """
       src = open("console-frontend/src/views/Authoring.tsx", encoding="utf-8").read()
       assert "setupDirective" not in src, \
           "the setup directive is still built and sent"
       assert "SetupPanel" not in src, \
           "the setup panel is still mounted"
       assert ("docSetup: docStyle === 'auto' ? undefined : { style: docStyle }"
               in src), "the style selector is not the only setup key sent"
       for key in ("page_size", "citation_style"):
           assert key not in src, \
               f"authoring sends a {key} default it offers no control for"
       # The brief is the only instruction now, so it has to say it can be
       # specific about the format.
       assert "as a PDF" in src, \
           "the brief placeholder should show how to ask for a format"


def test_the_viewer_shows_the_document_rather_than_text():
    src = open("console-frontend/src/views/Authoring.tsx", encoding="utf-8").read()
    # Every format branch mounts a real viewer. The count used to be of
    # `<DocumentViewer` alone; the PDF and discussion branches now mount
    # `<FilePreview`, which is the same three-way choice the document window
    # makes (pdf.js / the browser's own text view / the server pager) and which
    # a plain txt or html artifact must reach, because the pager endpoint
    # answers 415 for them.
    viewers = src.count("<DocumentViewer") + src.count("<FilePreview")
    assert viewers >= 4, \
        "some formats still show only extracted text"
    assert "<DocumentViewer source={discTarget}" not in src, \
        "the Discuss page routes every non-PDF straight to the raster pager " \
        "again, which is the 415 strip the user sees as 'the renderer is broken'"

# ── the setup panel must be a DECISION, not a suggestion ──────────────────
def test_doc_setup_is_whitelisted_at_the_boundary():
    """It reaches the renderer, so an unvalidated dict is not acceptable."""
    import agent_server
    raw = {
        "format": "docx", "page_size": "letter", "orientation": "landscape",
        "margins": "wide", "style": "academic", "citation_style": "ieee",
        "slide_size": "16:9", "columns": 99, "toc": True, "cover": True,
        "running_header": "Report", "length": "6 pages",
        # All of these must be dropped: they are not panel fields.
        "evil": "x", "nested": {"a": 1}, "blocks": [{"type": "heading"}],
    }
    out = agent_server._clean_doc_setup(raw)
    assert out["format"] == "docx"
    assert out["columns"] == 3, "columns must be clamped"
    assert out["toc"] is True and out["cover"] is True
    assert "evil" not in out and "nested" not in out and "blocks" not in out
    assert agent_server._clean_doc_setup(None) is None
    assert agent_server._clean_doc_setup({"format": "exe"}) is None
    assert agent_server._clean_doc_setup({"toc": "yes"}) is None, \
        "only a real boolean may set a flag"
    assert agent_server._clean_doc_setup({"columns": True}) is None


def test_the_setup_reaches_the_executor():
    import inspect

    import agent_server
    import flow

    assert "doc_setup" in inspect.signature(
        agent_server._run_orchestrator).parameters
    assert "doc_setup" in inspect.signature(
        flow.Executor.run).parameters
    assert "doc_setup" in inspect.signature(
        flow.Executor._run_one).parameters
    src = open("skills.py", encoding="utf-8").read()
    assert "applied the user's" in src, \
        "the setup must be forced onto the render, not only asked for"

def test_node_failures_group_into_a_fixed_vocabulary():
    """Free-text errors do not aggregate.

    65 author failures looked like 65 separate problems until they were
    bucketed, and they were three. The Ledger's "why" column is only useful
    because these map onto a stable set of labels.
    """
    import agent_server

    cases = [
        # Empty and prose replies stay distinct: skills.py:2413 separates them
        # because they need opposite fixes (upstream completion vs prompt
        # format), and one collapsed row hid a 52/13 split.
        ("researcher returned no usable JSON: empty reply",
         "provider returned an empty reply"),
        ("researcher returned no usable JSON: reply was not JSON "
         "(starts 'Here is my analysis')",
         "reply was prose, not the required JSON"),
        # A renderer crash is ours, not the model's. This exact string was a
        # live NameError being reported as model misbehaviour.
        ("author finished without calling render_document; "
         "render_document failed: spec fallback render failed: "
         "NameError: name 'title' is not defined - no file was produced",
         "document renderer failed (agent bug, not the model)"),
        ("author finished without calling render_document - no file was produced",
         "claimed a document but never called render_document"),
        ("author reported a document (x.pdf) but never called render_document",
         "claimed a document but never called render_document"),
        ("tool-use hop cap (6) reached without final text",
         "tool-use hop cap reached"),
        ("verifier stall: tool 'render_document' called with identical arguments 3x",
         "verifier stall: identical tool call repeated"),
        ("", "(no error text)"),
        ("some brand new failure nobody has seen",
         "some brand new failure nobody has seen"),
    ]
    for raw, want in cases:
        assert agent_server._classify_node_failure(raw) == want, (raw, want)


def test_node_health_reports_rate_against_each_skills_own_nodes():
    """`author failed 38% of its nodes` is actionable; "4 nodes failed" is not.

    This used to assert on the source TEXT of the function, which passes even
    if the code is unreachable or inside a comment - and worse, it pinned the
    literal expression of a line that was wrong: the denominator was every
    node, including nodes that were never dispatched. It now exercises the
    real endpoint with synthetic graphs and asserts the arithmetic.
    """
    import agent_server
    import networkx as nx
    from fastapi.testclient import TestClient

    def _graph(dirp, nodes):
        # Must be written in node_link form, which is a LIST of node objects,
        # because read_graph hands it to nx.node_link_graph. A bare dict of
        # id -> attrs parses fine as JSON and then fails to load as a graph,
        # which the endpoint swallows as "no nodes".
        g = nx.DiGraph()
        for nid, attrs in nodes.items():
            g.add_node(nid, **attrs)
        (dirp / "graph.json").write_text(
            json.dumps(nx.node_link_data(g)), encoding="utf-8")

    with tmp_session_root() as root:
        s1 = root / "s8-health-a"
        s1.mkdir(parents=True)
        _graph(s1, {
            "n1": {"skill": "author", "status": "failed",
                   "result": {"error": "boom"}},
            "n2": {"skill": "author", "status": "complete"},
            # never dispatched: must NOT dilute the rate
            "n3": {"skill": "author", "status": "pending"},
            "n4": {"skill": "formatter", "status": "pending"},
        })
        s2 = root / "s8-health-b"
        s2.mkdir(parents=True)
        _graph(s2, {
            "n1": {"skill": "author", "status": "complete"},
            "n2": {"skill": "author", "status": "complete"},
        })

        client = TestClient(agent_server.app)
        r = client.get("/api/nodes/health",
                       headers={"X-Aria-Token": "test-token"})
        assert r.status_code == 200, r.text
        body = r.json()
        rows = {row["skill"]: row for row in body["rows"]}

        author = rows["author"]
        # 2 attempted author nodes, 1 failed => 50%, NOT 20% (1/5)
        assert author["attempted"] == 4, author
        assert author["failed"] == 1, author
        assert author["fail_pct"] == 25.0, author
        assert author["never_run"] == 1, author
        assert author["nodes"] == 5, author

        # A skill whose only node never ran reports 0.0% and says so.
        fmt = rows["formatter"]
        assert fmt["attempted"] == 0, fmt
        assert fmt["never_run"] == 1, fmt
        assert fmt["fail_pct"] == 0.0, fmt

        # Worst skill sorts first.
        assert body["rows"][0]["skill"] == "author", body["rows"]
        assert body["totals"]["attempted"] == 4, body["totals"]

# --------------------------------------------------------------------------
# Caching. An artifact id is content-addressed, so a description of its bytes
# can never go stale — which makes it safe to keep the answer beside the file
# instead of recomputing it. Both preview routes used to recompute on every
# single open: the text extraction measured 0.45-0.80 s and the raster ~1.2 s.
# --------------------------------------------------------------------------

def _gateway_stub(seen):
    """An httpx.Client stand-in that counts how often the gateway is called."""
    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.content = json.dumps(payload).encode()
            self.text = self.content.decode()

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, **kw):
            seen["calls"] = seen.get("calls", 0) + 1
            n = int(json.get("max_pages") or 12)
            return _Resp({"mode": "pages", "format": "pdf",
                          "pages": [{"n": i} for i in range(n)],
                          "page_count": n})

    return _Client


def test_the_text_preview_is_cached_after_the_first_read(client):
    """Second read must be served from disk, byte-identically."""
    c, hdr = client
    import artifacts as arts
    art = arts.put(b"%PDF-1.4 caching-probe-A", content_type="application/pdf",
                   source="author:pdf", descriptor="Probe A (pdf)")

    r1 = c.get(f"/api/artifact/{art}/preview", headers=hdr)
    r2 = c.get(f"/api/artifact/{art}/preview", headers=hdr)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.headers.get("X-Artifact-Cache") == "miss", r1.headers
    assert r2.headers.get("X-Artifact-Cache") == "hit", r2.headers
    assert r1.json() == r2.json(), "a cache hit must not change the answer"


def test_the_artifact_bytes_are_marked_immutable(client):
    """Without these headers every open re-pulls the whole file over loopback.

    `immutable` is honest here because the URL *is* the content hash.
    """
    c, hdr = client
    import artifacts as arts
    art = arts.put(b"%PDF-1.4 caching-probe-B", content_type="application/pdf",
                   source="author:pdf", descriptor="Probe B (pdf)")
    r = c.get(f"/api/artifact/{art}?download=0", headers=hdr)
    assert r.status_code == 200
    assert r.headers.get("ETag") == f'"{art}"'
    assert "immutable" in (r.headers.get("Cache-Control") or "")


def test_the_raster_is_cached_and_both_parameter_sets_survive(
        client, monkeypatch):
    """The cache is keyed by the parameters, not just by the artifact.

    One slot per artifact passed this test when it only ever checked a repeat
    of the same request, and failed in practice: a default call followed by a
    4-page one overwrote the first, so alternating caps recomputed forever.
    """
    c, hdr = client
    import artifacts as arts
    import httpx
    art = arts.put(b"%PDF-1.4 caching-probe-C", content_type="application/pdf",
                   source="author:pdf", descriptor="Probe C (pdf)")
    seen = {}
    monkeypatch.setattr(httpx, "Client", _gateway_stub(seen))

    def ask(**extra):
        return c.post("/api/doc/preview",
                      json={"artifact": art, "format": "pdf", **extra},
                      headers=hdr)

    d1 = ask()
    n_default = len(d1.json()["pages"])
    d2 = ask()
    assert seen["calls"] == 1, "the second identical call must not hit the gateway"
    assert d2.headers.get("X-Raster-Cache") == "hit"

    d3 = ask(max_pages=4)
    assert seen["calls"] == 2, "a different page cap is a different answer"
    assert len(d3.json()["pages"]) == 4
    assert len(d3.json()["pages"]) != n_default

    # The original must STILL be cached: this is the alternation the single-slot
    # version got wrong.
    d4 = ask()
    assert seen["calls"] == 2, "the 4-page call must not have evicted the default"
    assert d4.headers.get("X-Raster-Cache") == "hit"
    assert len(d4.json()["pages"]) == n_default


def test_cache_roundtrip_and_unknown_name():
    import artifacts as arts
    art = arts.put(b"%PDF-1.4 caching-probe-D", content_type="application/pdf",
                   source="author:pdf", descriptor="Probe D (pdf)")
    assert arts.get_cache(art, "nothing") is None
    arts.put_cache(art, "nothing", {"a": 1})
    assert arts.get_cache(art, "nothing") == {"a": 1}
    assert arts.get_cache(art, "other") is None, "names must not share a slot"
