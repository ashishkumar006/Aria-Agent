"""Slice 9 — the browser as tools the coding agent can actually use.

Aria has had a browser for a long time as a DAG *node* and never as something
an agent could *use*: no `@mcp.tool()` verbs, so no interactive surface could
reach one, and the dev loop the studio exists for was impossible on principle.
This file is mostly about the four layers a capability needs, because that is
where this project has been bitten three times: a tool that is registered,
catalogued, allowed and — for the coding agent — actually handed to the model
is a capability; anything less is a sentence in a prompt about a tool that
isn't there.

The integration half is real Chromium on a real page with a real JavaScript
error. That is the point of the slice: without a `page.on("console")` listener
the agent could not see the error a front-end change it just made produced, so
it edited the file again on a guess and reported success.
"""
from __future__ import annotations

import inspect
import os
import sys
import threading
import time
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import agent_server as A          # noqa: E402
import browser_session as B       # noqa: E402
import mcp_server as M            # noqa: E402
import skills                     # noqa: E402

SID = "s-browser-test"
BROWSER_NAMES = [n for n, _ in inspect.getmembers(M, inspect.isfunction)
                 if n.startswith("browser_")]


# ── the four layers ──────────────────────────────────────────────────
# Two verbs genuinely take nothing: they ask about state and act on it. The
# rule that matters is that everything ELSE is aimable by the model, so a
# growing list here should be read as a smell, not a to-do.
_NO_ARGS = {"browser_status", "browser_close"}


def test_every_browser_tool_is_registered_on_the_mcp_server():
    """Layer 1: it answers over stdio at all. `@mcp.tool()` is what makes it
    dispatchable; a function that exists but is not registered is prose."""
    assert len(BROWSER_NAMES) >= 16, BROWSER_NAMES
    for name in BROWSER_NAMES:
        fn = getattr(M, name)
        doc = (fn.__doc__ or "")
        assert doc.strip(), f"{name} has no description for the model"
        # The MCP decorator wraps the function; the schema comes from the
        # signature it unwrapped, so check the original.
        target = getattr(fn, "__wrapped__", fn)
        params = list(inspect.signature(target).parameters)
        if name not in _NO_ARGS:
            assert params, f"{name} takes nothing, so the model cannot aim it"


def test_every_browser_tool_is_in_the_catalog():
    """Layer 2: `_TOOL_CATALOG` is what `tool_payload` builds a schema from.
    Registered but uncatalogued is dispatchable and invisible."""
    missing = [n for n in BROWSER_NAMES if n not in skills._TOOL_CATALOG]
    assert not missing, f"registered on the child, absent from the catalog: {missing}"
    for name in BROWSER_NAMES:
        entry = skills._TOOL_CATALOG[name]
        assert entry["name"] == name
        assert entry["input_schema"]["type"] == "object", name


def test_the_grant_lists_name_only_tools_that_really_exist():
    """The anti-drift check. A name in `CODE_AGENT_TOOLS` that the child does
    not implement is a tool the model will call and the harness will fail to
    dispatch — which reads to the user as the agent giving up."""
    for name in A._CODE_BROWSER_READ_TOOLS + A._CODE_BROWSER_ACT_TOOLS:
        assert name in skills._TOOL_CATALOG, f"{name} is granted but not catalogued"
        assert hasattr(M, name), f"{name} is granted but the child has no such tool"


def test_the_browser_is_granted_only_when_its_switch_is_on():
    """The kill switch, checked both ways. Off must leave no residue: the
    coding agent's payload cannot contain a browser verb at all."""
    enabled = A._browser_tools_enabled()
    in_grant = "browser_verify" in A.CODE_AGENT_TOOLS
    assert in_grant == enabled, (
        f"browser.tools={enabled} but CODE_AGENT_TOOLS says {in_grant}")
    payload = [t["name"] for t in (skills.tool_payload(A.CODE_AGENT_TOOLS) or [])]
    assert ("browser_open" in payload) == enabled
    if not enabled:
        assert not any(p.startswith("browser_") for p in payload), (
            "the switch is off and the model can still see a browser tool")


def test_acting_on_a_page_is_gated_like_running_code():
    """Clicking Submit runs the site's handler with this machine's cookies.
    That is execution, so the mutating verbs belong in the exec gate — the
    level a session has to raise on purpose — not in the file-write gate."""
    mutating = set(A._CODE_BROWSER_MUTATING)
    assert {"browser_click", "browser_fill", "browser_press"} <= mutating
    # The gate the route applies is `_CODE_EXEC_TOOLS`; with the flag on it
    # must cover every mutating verb, or a "read-only" session could submit a
    # form while believing it was only looking.
    if A._browser_tools_enabled():
        assert mutating <= set(A._CODE_EXEC_TOOLS), (
            f"not gated: {sorted(mutating - set(A._CODE_EXEC_TOOLS))}")


def test_read_only_mode_withholds_the_browser_verbs_that_change_anything(
        monkeypatch):
    """`_code_allowed_tools(read_only=True)` is the read-only promise. Assert
    the shape that holds whichever way the flag is set."""
    monkeypatch.setattr(A, "_browser_tools_enabled", lambda: True)
    monkeypatch.setattr(A, "CODE_AGENT_TOOLS",
                        list(A.CODE_AGENT_TOOLS) + A._CODE_BROWSER_READ_TOOLS
                        + A._CODE_BROWSER_ACT_TOOLS)
    monkeypatch.setattr(A, "_CODE_EXEC_TOOLS",
                        set(A._CODE_EXEC_TOOLS) | A._CODE_BROWSER_MUTATING)
    denied = A._CODE_WRITE_TOOLS | A._CODE_EXEC_TOOLS
    allowed = set(A._code_allowed_tools(True))
    assert not (allowed & denied), "read-only mode is handing out a mutating tool"
    assert "browser_verify" in allowed or "browser_verify" in denied


def test_a_verify_that_navigates_is_gated_and_one_that_only_reads_is_not(
        monkeypatch):
    """`browser_verify` sits in the READ list because it reads a verdict, but
    with a `url` it navigates first — and a navigation runs the site's own code
    with this machine's cookies, which is the act the gate exists for. Without
    a url it only looks at the page that is already open, and gating that would
    break the read-only review mode it is meant to serve."""
    monkeypatch.setattr(A, "_browser_tools_enabled", lambda: True)
    assert A._is_exec_call("browser_verify", {"url": "http://127.0.0.1:5173"})
    assert not A._is_exec_call("browser_verify", {}), (
        "a verify that reads the open page still asked for permission")
    assert not A._is_exec_call("browser_verify", {"url": "   "})
    assert A._is_exec_call("browser_click", {"ref": "e3"})
    # Flag off: the verb is not in the payload at all, so the gate says nothing.
    monkeypatch.setattr(A, "_browser_tools_enabled", lambda: False)
    assert not A._is_exec_call("browser_verify", {"url": "http://x"})


def test_a_standing_verify_approval_is_per_url(monkeypatch):
    """"Approve and remember" must not become "approve every address the model
    later names". The signature is what a standing approval is keyed on."""
    a = A._code_sig("browser_verify", {"url": "http://localhost:5173"})
    b = A._code_sig("browser_verify", {"url": "http://example.com"})
    assert a != b, (
        f"both navigations sign as {a!r} — one approval authorises any site")
    assert "5173" in a and "example.com" in b


def test_goto_refuses_a_scheme_that_is_privileged_to_this_machine():
    """The old rule only asked "is there a colon", so `file://` navigated — and
    from a file:// page the state directory, `agent.token` and the gateway's
    loopback UI are all readable documents."""
    refused = ["file:///etc/passwd", "file:///C:/x/state/agent.token",
               "chrome://settings", "devtools://devtools/b/",
               "view-source:http://x", "javascript:alert(1)"]
    for bad in refused:
        ok, target, why = B._nav_url(bad)
        assert not ok and "scheme" in why, (bad, ok, target, why)
    # What still works: a real web page, a bare host, and a generated page.
    assert B._nav_url("http://127.0.0.1:5173")[0]
    assert B._nav_url("https://example.com")[0]
    assert B._nav_url("example.com") == (True, "https://example.com", "")
    assert B._nav_url("data:text/html,<p>hi</p>")[0], (
        "a data: page is how the dev loop shows Chromium something it just "
        "wrote; refusing it breaks the surface, not the threat")
    # And the refusal happens before a browser is needed at all.
    import asyncio
    b = B.Browser("st-schemetest")
    assert asyncio.run(b.goto("file:///etc/passwd"))["ok"] is False
    assert b.pages == [], "a refused navigation still opened a tab"


def test_the_session_id_a_browser_is_keyed_on_is_shaped_not_free_form():
    """An sid becomes a directory name for the artifacts. `/`, `..` and a
    colon must not get in at the door."""
    assert B._valid_sid("st-582daebe")
    assert B._valid_sid("s-slice9")
    for bad in ("", "../x", "a/b", "a\\b", "a:b", "-lead", "x" * 80, "a b"):
        assert not B._valid_sid(bad), bad
    with pytest.raises(ValueError):
        B.get("../escape")


# ── the ring buffers ─────────────────────────────────────────────────
def test_an_overflowed_ring_reports_how_much_it_ate():
    """A dev server that logs in a loop fills a ring in seconds. Without the
    dropped count a noisy app is read as a quiet one, which is the same class
    of lie as a truncated file that says nothing about truncating."""
    r = B.Ring(cap=3)
    for i in range(10):
        r.add({"n": i})
    assert len(r.items) == 3 and r.dropped == 7
    assert [x["n"] for x in r.items] == [7, 8, 9]


# ── real chromium ────────────────────────────────────────────────────
PAGE = """<!doctype html><html><head><title>Aria slice9</title></head>
<body>
  <h1 id="headline">Hello from the studio</h1>
  <button id="go" onclick="document.getElementById('out').textContent='pressed'">
    Press me</button>
  <p id="out">nothing yet</p>
  <input id="field" aria-label="Name">
  <a href="#section">to the section</a>
  <script>
    console.log("page booted");
    console.error("BOOM: the widget failed to mount");
    window.addEventListener('DOMContentLoaded', () => {
      setTimeout(() => { throw new Error("uncaught from a timer"); }, 10);
      fetch("/does-not-exist-404").catch(() => {});
    });
  </script>
</body></html>"""


@pytest.fixture(scope="module")
def browser():
    try:
        B.close(SID)
    except Exception:
        pass
    try:
        b = B.get(SID)
    except Exception as e:
        pytest.skip(f"chromium is not available here: {e}")
    yield b
    B.close(SID)


def _data_url(html: str) -> str:
    import base64
    return "data:text/html;base64," + base64.b64encode(html.encode()).decode()


@pytest.fixture(scope="module")
def local_server(tmp_path_factory):
    """A real loopback HTTP server, because reaching one is the capability.

    The dev loop the slice exists for is `http://localhost:5173`, and a
    relative `fetch()` from a `data:` page resolves to nothing — so a data URL
    cannot prove the network ring works. This does, and it proves the loopback
    claim in the tool description at the same time.
    """
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    root = tmp_path_factory.mktemp("webroot")
    (root / "index.html").write_text(PAGE, encoding="utf-8")

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(root))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    srv.shutdown()


def test_a_real_page_boots_and_its_javascript_error_is_captured(browser):
    """The listener that did not exist anywhere in Aria before this slice."""
    got = B.open_url(SID, _data_url(PAGE))
    assert got["ok"], got
    assert "Aria slice9" in got["title"], got
    time.sleep(0.4)
    events = B.events(SID, "console", errors_only=False)
    text = " ".join(str(e.get("text")) for e in events["events"])
    assert "page booted" in text, events
    errs = B.events(SID, "console", errors_only=True)
    kinds = " ".join(str(e.get("text")) for e in errs["events"])
    assert "BOOM: the widget failed to mount" in kinds, errs
    assert "uncaught from a timer" in kinds, errs


def test_the_network_ring_sees_a_real_request_and_its_404(browser, local_server):
    got = B.open_url(SID, local_server)
    assert got["ok"], got
    time.sleep(0.6)
    net = B.events(SID, "network")
    rows = net["events"]
    assert rows, net
    urls = " ".join(str(r.get("url")) for r in rows)
    assert "index.html" in urls, urls
    assert any("does-not-exist-404" in u for u in
               [str(r.get("url")) for r in rows]), rows
    statuses = {int(r["status"]) for r in rows if r.get("status")}
    assert 404 in statuses, rows


def test_snapshot_aim_a_click_and_read_the_result(browser):
    """The loop the studio needs: perceive, address, act, and read back what
    the page actually says after — not what the action was meant to do."""
    snap = B.snapshot(SID)
    assert snap["ok"] and snap["elements"] >= 3, snap
    assert "<button" in snap["legend"] and "Press me" in snap["legend"], snap["legend"]
    mark = None
    for line in snap["legend"].splitlines():
        if "Press me" in line:
            mark = int(line.split("]")[0].strip("["))
            break
    assert mark is not None
    clicked = B.act(SID, "click", mark=mark, epoch=snap["epoch"])
    assert clicked["ok"], clicked
    again = B.read(SID, "text", 4000)
    assert "pressed" in again["text"], again


def test_a_spent_epoch_is_refused_instead_of_clicking_whatever_moved(
        browser):
    """Coordinates are not identity. A mark from an old snapshot belongs to a
    page that has since rearranged itself, and clicking it does something
    nobody reviewed."""
    snap = B.snapshot(SID)
    first = snap["epoch"]
    B.act(SID, "scroll", x=0, y=10)          # bumps the epoch
    stale = B.act(SID, "click", mark=1, epoch=first)
    assert stale["ok"] is False and stale.get("stale"), stale
    assert "superseded" in stale["error"], stale


def test_a_mark_without_any_snapshot_is_refused_not_guessed(browser):
    """Same for a fresh page: no snapshot means no marks, and "click 3" with
    nothing to resolve against must not be silently turned into a coordinate
    click on whatever is at pixel 3."""
    B.tab_new(SID, _data_url("<html><body><p>plain</p></body></html>"))
    fresh = B.status(SID)
    idx = fresh["active"]
    B._SESSIONS[SID].snapshots[idx] = None
    out = B.act(SID, "click", mark=2)
    assert out["ok"] is False and "browser_snapshot" in out["error"], out


def test_an_action_with_no_target_is_refused(browser):
    out = B.act(SID, "click")
    assert out["ok"] is False and "mark" in out["error"], out


def test_fill_and_press_type_into_a_real_field(browser):
    B.open_url(SID, _data_url(PAGE))
    snap = B.snapshot(SID)
    out = B.act(SID, "fill", selector="#field", text="typed by aria")
    assert out["ok"], out
    val = B.evaluate(SID, "() => document.getElementById('field').value")
    assert val["ok"] and val["result"] == "typed by aria", val


def test_wait_resolves_a_condition_that_happens_and_one_that_does_not(
        browser):
    B.open_url(SID, _data_url(
        "<html><body><script>setTimeout(()=>{"
        "document.body.innerHTML='<p id=late>arrived</p>'},300)</script>"
        "</body></html>"))
    ok = B.wait(SID, selector="#late", state="visible", ms=4000)
    assert ok["ok"], ok
    late = B.wait(SID, selector="#never", state="visible", ms=600)
    assert not late["ok"] and "600ms" in late["error"], late


def test_screenshots_are_filed_where_the_artifact_route_can_serve_them(
        browser):
    """The pane shows these, so they must land in the session's browser
    artifact dir and come back as a served URL — not as a filesystem path a
    client could be talked into opening anywhere."""
    B.open_url(SID, _data_url(PAGE))
    shot = B.screenshot(SID)
    assert shot["ok"], shot
    assert shot["path"].endswith(".png") and Path(shot["path"]).is_file()
    assert f"/api/artifacts/{SID}/studio/" in shot["url"], shot["url"]
    assert "browser" in Path(shot["path"]).parts


def test_tabs_are_counted_bounded_and_not_emptied(browser):
    lst = B.tabs(SID)
    assert lst["ok"] and lst["tabs"], lst
    assert all(t["url"] for t in lst["tabs"])
    assert lst["limit"] == B.MAX_TABS
    # The last tab cannot be closed out from under the session.
    while len(B.tabs(SID)["tabs"]) > 1:
        B.tab_close(SID, len(B.tabs(SID)["tabs"]) - 1)
    refused = B.tab_close(SID, 0)
    assert refused["ok"] is False and "last tab" in refused["error"], refused


def test_the_tab_count_is_a_real_limit_not_an_aspiration(browser):
    """An unbounded tab list is an unbounded process, and a model that opens
    tabs in a loop finds out too late. Measured as a delta: a session always
    starts with one tab, and `MAX_TABS` is the total it may reach."""
    start = len(B.tabs(SID)["tabs"])
    opened = 0
    refused = None
    for _ in range(B.MAX_TABS + 3):
        out = B.tab_new(SID)
        if out.get("ok"):
            opened += 1
        else:
            refused = out
            break
    assert refused and "limit" in refused["error"], refused
    assert start + opened == B.MAX_TABS, (start, opened, B.MAX_TABS)
    while len(B.tabs(SID)["tabs"]) > 1:
        B.tab_close(SID, len(B.tabs(SID)["tabs"]) - 1)


def test_the_browser_survives_between_calls_and_not_between_sessions(browser):
    """Persistence is the slice: a context that dies with a run cannot hold a
    login. Two separate tool calls see the same page."""
    B.open_url(SID, _data_url("<html><title>pinned</title><body>x</body></html>"))
    first = B.status(SID)
    second = B.status(SID)
    assert first["running"] and second["running"]
    assert first["url"] == second["url"] and first["url"].startswith("data:")
    other = B.status(SID + "-nope")
    assert other["running"] is False, "a browser is not shared across sessions"


def test_close_frees_it_and_the_next_call_starts_a_new_one(browser):
    closed = B.close(SID)
    assert closed["ok"] and closed["closed"], closed
    assert B.status(SID)["running"] is False
    assert B.close(SID)["closed"] is False, "closing twice is not an error"
    B.get(SID)  # and it comes back


def test_evaluate_is_bounded_and_reports_its_own_cap(browser):
    B.get(SID)
    long = "x" * 4001
    out = B.evaluate(SID, long)
    assert out["ok"] is False and "4000" in out["error"], out
    small = B.evaluate(SID, "() => 1 + 1")
    assert small["ok"] and small["result"] == "2", small


def test_the_idle_sweep_closes_a_browser_nobody_is_using(monkeypatch):
    """An idle Chromium is a process, a network stack and a cookie jar."""
    B.close_all()
    b = B.get(SID)
    b.idle_after = time.time() - 1
    gone = B.sweep_idle()
    assert SID in gone, gone
    assert B.status(SID)["running"] is False
