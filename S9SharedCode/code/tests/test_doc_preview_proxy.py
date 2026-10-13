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
       request, and it is worse than sending none. The format now travels in
       the brief ("as a PDF"), and `_clean_doc_setup` still enforces a forced
       setup from any other caller.
       """
       src = open("console-frontend/src/views/Authoring.tsx", encoding="utf-8").read()
       assert "setupDirective" not in src, \
           "the setup directive is still built and sent"
       assert "docSetup" not in src, \
           "authoring still sends a doc_setup it no longer offers controls for"
       assert "SetupPanel" not in src, \
           "the setup panel is still mounted"
       # The brief is the only instruction now, so it has to say it can be
       # specific about the format.
       assert "as a PDF" in src, \
           "the brief placeholder should show how to ask for a format"


def test_the_viewer_shows_the_document_rather_than_text():
    src = open("console-frontend/src/views/Authoring.tsx", encoding="utf-8").read()
    # The four format branches each mount the real viewer.
    assert src.count("<DocumentViewer") >= 4, \
        "some formats still show only extracted text"

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