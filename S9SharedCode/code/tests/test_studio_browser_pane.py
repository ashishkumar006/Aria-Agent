"""Slice 10 — the browser pane: a live view, the evidence, and takeover.

Aria's browser was invisible in three separate ways: `headless=True`
hardcoded, no surface anywhere that rendered a page, and screenshots that were
written, served and put on the `done` frame for a consumer this console never
had. A capability nobody can see is one nobody trusts, and "take over" is not
true of something you cannot look at. These tests are the pane's three jobs
plus the two rules that make them safe:

- **one master switch.** With `browser.tools` off, every route says so and
  does nothing — including the ones that would otherwise be read-only.
- **looking is not starting.** The pane polls status and frames; none of that
  may mint a Chromium as a side effect of being open, or every studio tab
  would carry a browser process whether it used one or not.
"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import agent_server as A          # noqa: E402
import browser_session as B       # noqa: E402
from agent_server import app      # noqa: E402

SID = "s-brow-pane"

PAGE = """<!doctype html><html><head><title>Pane page</title></head>
<body><h1 id="h">Pane is up</h1>
<script>
  console.log("pane booted");
  console.error("PANE-BOOM: widget failed");
  setTimeout(() => { document.getElementById('h').textContent = 'repainted'; },
             400);
</script></body></html>"""


def _data_url(html: str) -> str:
    return "data:text/html;base64," + base64.b64encode(html.encode()).decode()


@pytest.fixture(autouse=True)
def _grant(monkeypatch):
    """The pane is the tool grant's surface: with it off nothing answers."""
    monkeypatch.setattr(A, "_browser_tools_enabled", lambda: True)
    B.close(SID)
    yield
    B.close(SID)


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def chromium():
    try:
        b = B.get(SID)
    except Exception as e:
        pytest.skip(f"chromium is not available here: {e}")
    yield b
    B.close(SID)


# ── the switch ───────────────────────────────────────────────────────
def test_every_pane_route_is_inert_when_the_switch_is_off(client, monkeypatch):
    monkeypatch.setattr(A, "_browser_tools_enabled", lambda: False)
    st = client.get(f"/api/studio/browser/status?sid={SID}").json()
    assert st["disabled"] is True and st["running"] is False
    assert client.get(f"/api/studio/browser/events?sid={SID}").json()["disabled"]
    assert client.get(f"/api/studio/browser/shots?sid={SID}").json()["disabled"]
    assert client.get(f"/api/studio/browser/page?sid={SID}").json()["disabled"]
    nav = client.post("/api/studio/browser/navigate",
                      json={"sid": SID, "url": "https://example.com"})
    assert nav.status_code == 403, nav.text
    tabs = client.post("/api/studio/browser/tab", json={"sid": SID, "action": "new"})
    assert tabs.status_code == 403, tabs.text


# ── looking is not starting ──────────────────────────────────────────
def test_polling_the_pane_does_not_open_a_browser(client):
    B.close_all()
    for _ in range(3):
        got = client.get(f"/api/studio/browser/status?sid={SID}").json()
        assert got["running"] is False
        client.get(f"/api/studio/browser/events?sid={SID}&kind=console")
        client.get(f"/api/studio/browser/shots?sid={SID}")
    assert B._peek(SID) is None, (
        "a pane poll started Chromium; an idle tab now owns a process")


# ── navigate, log, evidence ──────────────────────────────────────────
def test_a_url_typed_in_the_pane_opens_in_the_agents_browser(client, chromium):
    got = client.post("/api/studio/browser/navigate",
                      json={"sid": SID, "url": _data_url(PAGE)}).json()
    assert got["ok"], got
    assert "Pane page" in got["title"], got
    assert client.get(f"/api/studio/browser/status?sid={SID}").json()["running"]


def test_the_console_route_returns_what_the_page_said(client, chromium):
    client.post("/api/studio/browser/navigate",
                json={"sid": SID, "url": _data_url(PAGE)})
    time.sleep(0.5)
    errs = client.get(f"/api/studio/browser/events?sid={SID}"
                      "&kind=console&all=true").json()
    text = " ".join(str(e.get("text")) for e in errs["events"])
    assert "PANE-BOOM: widget failed" in text, errs
    assert "pane booted" in text, errs
    assert errs["dropped"] == 0
    # The default is the error view, and it must not be mistaken for the whole
    # log: a `log`-level line is absent from it by design.
    quiet = client.get(f"/api/studio/browser/events?sid={SID}"
                       "&kind=console").json()
    assert "pane booted" not in " ".join(str(e.get("text"))
                                          for e in quiet["events"]), quiet


def test_an_observing_call_never_launches_a_browser(client):
    """`browser_console` on a session with nothing open answers "nothing is
    open" rather than starting Chromium to find out. A pane that polls every
    few seconds, or an agent that checks before it navigates, must not be able
    to leave a process behind."""
    B.close_all()
    assert B._peek(SID) is None
    got = client.get(f"/api/studio/browser/events?sid={SID}").json()
    assert got.get("not_open") or not got.get("events"), got
    assert B._peek(SID) is None, "the poll started a browser"
    snap = client.get(f"/api/studio/browser/page?sid={SID}").json()
    assert snap.get("not_open") or snap.get("ok") is False, snap
    assert B._peek(SID) is None


def test_the_shot_route_lists_screenshots_the_pane_can_render(client, chromium):
    client.post("/api/studio/browser/navigate",
                json={"sid": SID, "url": _data_url(PAGE)})
    shot = B.screenshot(SID)
    assert shot["ok"], shot
    listed = client.get(f"/api/studio/browser/shots?sid={SID}").json()
    names = [s["name"] for s in listed["shots"]]
    assert Path(shot["path"]).name in names, listed
    newest = listed["shots"][0]
    assert newest["url"] == f"/api/artifacts/{SID}/studio/{newest['name']}", newest
    got = client.get(newest["url"])
    assert got.status_code == 200 and got.content[:4] == b"\x89PNG", got.status_code


def test_the_artifacts_route_refuses_a_path_that_tries_to_leave(client, chromium):
    """The screenshot URLs the pane renders must not be a file reader.

    A literal `../` in the request line is normalised away by the client and
    never reaches the route at all, so the probe has to arrive percent-encoded
    — that is the form a hand-built fetch can send, and the form the server
    actually has to refuse.
    """
    B.open_url(SID, _data_url(PAGE))
    listed = client.get(f"/api/studio/browser/shots?sid={SID}").json()
    if not listed["shots"]:
        pytest.skip("no shots filed to probe")
    enc = ("/api/artifacts/" + SID + "/studio/"
           + "%2e%2e%2f" * 3 + "agent_config.yaml")
    got = client.get(enc)
    assert got.status_code in (400, 404), got.status_code
    assert b"browser:" not in got.content, "the route served the config file"


def test_the_page_route_gives_the_snapshot_a_human_can_read(client, chromium):
    client.post("/api/studio/browser/navigate",
                json={"sid": SID, "url": _data_url(PAGE)})
    snap = client.get(f"/api/studio/browser/page?sid={SID}").json()
    assert snap["ok"] and snap["epoch"], snap
    assert "h1" in snap["legend"] or "Pane is up" in str(snap["text_excerpt"])


# ── the live view ────────────────────────────────────────────────────
def test_the_view_stream_proves_it_is_streaming_and_delivers_a_frame(
        client, chromium):
    """The pane's central claim. Either frames arrive or the stream says why
    they cannot — an empty box that could be either is not a live view."""
    client.post("/api/studio/browser/navigate",
                json={"sid": SID, "url": _data_url(PAGE)})
    kinds = []
    image = ""
    with client.stream("GET", f"/api/studio/browser/view?sid={SID}"
                       "&every_ms=160&hold=6") as r:
        assert r.status_code == 200, r.status_code
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                line = [x for x in block.splitlines() if x.startswith("data:")]
                if not line:
                    continue
                import json as _j
                frame = _j.loads(line[0][5:].strip())
                kinds.append(frame.get("type"))
                if frame.get("type") == "frame" and frame.get("image"):
                    image = frame["image"]
                    break
            if image:
                break
    assert "cast" in kinds or "unsupported" in kinds, kinds
    if "unsupported" in kinds:
        pytest.skip(f"this runtime cannot screencast: {kinds}")
    assert image, kinds
    raw = base64.b64decode(image)
    assert raw[:3] == b"\xff\xd8\xff", "a jpeg frame or nothing"


def test_a_browsers_status_names_the_live_view_state(client, chromium):
    client.post("/api/studio/browser/navigate",
                json={"sid": SID, "url": _data_url(PAGE)})
    B.start_cast(SID)
    st = client.get(f"/api/studio/browser/status?sid={SID}").json()
    assert st["casting"] is True, st
    assert st["cast_fps"] > 0, st
    B.stop_cast(SID)
    after = client.get(f"/api/studio/browser/status?sid={SID}").json()
    assert after["casting"] is False, after


# ── tab strip ────────────────────────────────────────────────────────
def test_the_panes_tab_strip_and_the_agents_are_the_same_tabs(client, chromium):
    client.post("/api/studio/browser/navigate",
                json={"sid": SID, "url": _data_url(PAGE)})
    first = client.get(f"/api/studio/browser/status?sid={SID}").json()["url"]
    client.post("/api/studio/browser/tab", json={"sid": SID, "action": "new",
                                                 "url": "about:blank"})
    st = client.get(f"/api/studio/browser/status?sid={SID}").json()
    assert st["tabs"] == 2, st
    assert st["url"].startswith("about:blank") or st["active"] == 1, st
    # The first tab is still there and still where the agent left it.
    client.post("/api/studio/browser/tab", json={"sid": SID, "action": "select",
                                                 "index": 0})
    back = client.get(f"/api/studio/browser/status?sid={SID}").json()
    assert back["url"] == first, (back, first)


def test_navigating_with_an_empty_url_is_refused(client):
    got = client.post("/api/studio/browser/navigate",
                      json={"sid": SID, "url": "   "})
    assert got.status_code == 400, got.text


def test_an_absurd_url_is_refused_before_it_reaches_playwright(client):
    got = client.post("/api/studio/browser/navigate",
                      json={"sid": SID, "url": "x" * 4000})
    assert got.status_code == 400, got.text


def test_close_reports_whether_anything_was_actually_closed(client, chromium):
    client.post("/api/studio/browser/navigate",
                json={"sid": SID, "url": _data_url(PAGE)})
    got = client.post("/api/studio/browser/close", json={"sid": SID}).json()
    assert got["ok"] and got["closed"] is True, got
    assert client.get(f"/api/studio/browser/status?sid={SID}").json()["running"] is False
    again = client.post("/api/studio/browser/close", json={"sid": SID}).json()
    assert again["closed"] is False, "closing twice is not a failure"


# ── @browser as context ──────────────────────────────────────────────
def test_browser_page_context_reaches_the_prompt(client, monkeypatch, tmp_path):
    """`@browser` is a first-class context source, so the page has to arrive
    labelled as a page — with its URL, and with the excerpt declared when the
    text was cut. A pasted blob the model thinks is a file is worse than no
    paste."""
    import mcp_runner
    root = tmp_path / "proj"
    root.mkdir()
    state, why = __import__("studio_workspace").add_root(SID, str(root))
    assert state is not None, why
    seen: dict = {}

    async def fake_run(**kwargs):
        seen["messages"] = kwargs["messages"]
        return {"text": "ok", "provider": "test", "input_tokens": 1,
                "output_tokens": 1, "tool_calls": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    __import__("studio_workspace").bind(SID)
    page = {"url": "http://localhost:5173/cart", "title": "Cart",
            "text": "x" * 700_000, "errors": ["TypeError: cannot read q"]}
    with TestClient(app) as c:
        with c.stream("POST", "/api/code/agent", json={
                "question": "why is this broken", "path": "", "text": "",
                "sid": SID, "browser_page": page, "read_only": True}) as r:
            "".join(r.iter_text())
    blob = str(seen["messages"])
    assert "PAGE THE USER IS LOOKING AT: http://localhost:5173/cart — Cart" in blob
    assert "TypeError: cannot read q" in blob
    assert "the page text was cut here" in blob, "a 700k page must not read as whole"


def test_no_page_context_changes_nothing(client, monkeypatch, tmp_path):
    """The `@browser` path must not be able to alter a run that did not use
    it — no empty 'PAGE:' block for the model to wonder about."""
    import mcp_runner
    import studio_workspace
    root = tmp_path / "proj"
    root.mkdir()
    studio_workspace.add_root(SID, str(root))
    seen: dict = {}

    async def fake_run(**kwargs):
        seen["messages"] = kwargs["messages"]
        return {"text": "ok", "provider": "test", "input_tokens": 1,
                "output_tokens": 1, "tool_calls": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    studio_workspace.bind(SID)
    with TestClient(app) as c:
        with c.stream("POST", "/api/code/agent", json={
                "question": "hello", "path": "", "text": "", "sid": SID,
                "read_only": True}) as r:
            "".join(r.iter_text())
    assert "PAGE THE USER IS LOOKING AT" not in str(seen["messages"])
    studio_workspace.clear(SID)
