"""The system audit — a tiered sweep over the whole product, not one module.

Why this file exists
--------------------
Everything else in `tests/` checks one behaviour. This checks the *joints*: the
contract between the console and the agent, between the declared flag set and
the code that reads it, between the tool list a model is offered and the tools
that actually exist, between a route the SPA can navigate to and a route the
server serves. Those are the bugs that survive every unit test, because each
side is individually correct — the console asks for `/api/x` faithfully, the
server serves `/api/x` faithfully, and the two drift apart in a rename nobody
ran.

Tiers
-----
  A  Hermetic. Imports the app, reads source. No network, no model, always runs.
  B  Live service contract. Requires the agent (and where noted the gateway) to
     be running; skips when they are not. **Zero model calls** — every request
     here is a read or a negative path, so this tier costs nothing and can run
     on every change.
  C  Live agent behaviour. Opt-in with `ARIA_LIVE_AUDIT=1`, because each test
     spends a real turn. These are the only ones that prove the agent actually
     does something; A and B can only prove it is addressable.

A failure in A or B is always a real defect. A failure in C may be the gateway
being rate-limited — C therefore reports what it saw rather than asserting a
number.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[1]                    # S9SharedCode/code
ROOT = CODE.parents[1]                                        # repo root
SRC = CODE / "console-frontend" / "src"
AGENT = os.environ.get("ARIA_AGENT", "http://127.0.0.1:8500")
GATEWAY = os.environ.get("ARIA_GATEWAY", "http://127.0.0.1:8109")

LIVE_AUDIT = os.environ.get("ARIA_LIVE_AUDIT") == "1"


# ── tier A: hermetic joint checks ────────────────────────────────────────
def _frontend_endpoint_templates() -> dict[str, set[str]]:
    """Every `/api/...` or `/v1/...` literal in the console source, normalised so
    ``${id}`` becomes `{x}`. Comments are skipped: a doc comment naming an
    endpoint is not a call to it."""
    found: dict[str, set[str]] = {}
    for path in sorted(list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx"))):
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"/\*[\s\S]*?\*/", "\n", text)
        text = re.sub(r"^\s*//.*$", "\n", text, flags=re.M)
        for m in re.finditer(r"[`\"](/(?:api|v1)/[^`\"\n' ]*)[`\"]", text):
            raw = m.group(1)
            if "*" in raw or raw.endswith("/"):
                continue
            norm = re.sub(r"\$\{[^}]*\}", "{x}", raw)
            # A placeholder glued to the end of a segment without a "/" in
            # front of it is a query suffix (`?sid=` / `&sid=`), not a path
            # parameter — those always occupy their own segment. Cutting it
            # here keeps `/api/code/write${sidQs(sid)}` resolvable against the
            # real route instead of becoming a phantom `/api/code/write{x}`.
            norm = re.sub(r"(?<!/)(\{x\})+$", "", norm)
            if not re.fullmatch(r"/(?:api|v1)/[A-Za-z0-9_/{}.x-]+", norm):
                continue
            found.setdefault(norm, set()).add(path.name)
    return found


def _route_patterns():
    """The agent's own route table, from the imported app — no HTTP involved."""
    import agent_server
    out = []
    for r in agent_server.app.routes:
        p = getattr(r, "path", None)
        if not p:
            continue
        methods = getattr(r, "methods", None) or set()
        if not methods & {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            continue
        out.append((p, methods))
    return out


def _matches(route_path: str, client_path: str) -> bool:
    """`/api/apps/{app_id}/a2ui` matches `/api/apps/${id}/a2ui`."""
    a = route_path.split("/")
    b = client_path.split("/")
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x.startswith("{"):
            continue
        if y.startswith("{x}"):
            continue
        if x != y:
            return False
    return True


def test_every_endpoint_the_console_calls_exists_on_the_agent():
    """The drift guard. 65 templates today, zero missing — which is exactly why
    the test is cheap to keep and expensive to lose: one renamed route and the
    panel silently shows an empty state, with both sides looking correct."""
    templates = _frontend_endpoint_templates()
    routes = _route_patterns()
    assert templates, "no /api endpoints found in the console — did the scan break?"
    missing = []
    for client, files in sorted(templates.items()):
        if not client.startswith("/api/"):
            continue                      # /v1/* is the gateway's surface, checked below
        if not any(_matches(rp, client) for rp, _ in routes):
            missing.append(f"{client} (called by {', '.join(sorted(files))})")
    assert not missing, "console calls endpoints the agent does not serve:\n  " + "\n  ".join(missing)


def test_flags_are_declared_where_they_are_read_and_read_where_they_are_declared():
    """Two failures in one, each silent in the other direction.

    A declared flag that nothing reads is a control the user can toggle with no
    effect — worse than no control, because it teaches them the UI lies. A flag
    read but never declared cannot be toggled from the console at all, so it is
    a behaviour nobody can turn off.
    """
    import flags
    declared = set(flags.DEFINITIONS)
    py = {p: p.read_text(encoding="utf-8") for p in list(CODE.glob("*.py"))}
    ts = {p: p.read_text(encoding="utf-8")
          for p in list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx"))}

    # Where the flag is honoured. The definition itself must not count, so the
    # agent-side scan looks at code beside flags.py.
    readers = " ".join(t for p, t in py.items() if p.name != "flags.py")
    frontend = " ".join(ts.values())

    dead = sorted(n for n in declared
                  if f'"{n}"' not in readers and f"'{n}'" not in readers
                  and f'"{n}"' not in frontend and f"'{n}'" not in frontend)
    assert not dead, (f"flags declared that nothing reads: {dead} — a toggle the user "
                      f"can flip with no effect is worse than no toggle, it teaches "
                      f"them this UI lies. Wire it up or delete the definition.")

    undeclared = sorted({n for t in py.values()
                         for n in re.findall(r"is_enabled\(\s*[\"']([\w.]+)[\"']", t)}
                        - declared)
    assert not undeclared, f"behaviour gated on flags that are not declared: {undeclared}"


def test_every_flag_has_a_description_that_states_its_blast_radius():
    """A kill switch whose description does not say what it turns off is the one
    thing that makes an operator afraid to touch it."""
    import flags
    for name, spec in flags.DEFINITIONS.items():
        desc = str(spec.get("description") or "")
        assert len(desc) > 60, f"{name}: description is {len(desc)} chars"
        assert "type" in spec and "default" in spec, f"{name}: missing type/default"
        assert spec["type"] in ("bool", "int", "str"), f"{name}: odd type"


def test_chat_tool_names_all_resolve_to_a_real_implementation():
    """`_CHAT_TOOLS` is a list of names handed to `skills.tool_payload`, which
    drops anything it has no schema for *silently*. A dropped tool is not an
    error — it is a capability the model was told about in the prompt and does
    not have, which is the worst of the two failure directions."""
    import agent_server
    from skills import tool_payload
    specs = tool_payload(agent_server._CHAT_TOOLS) or []
    got = {s["name"] for s in specs}
    local = {s["name"] for s in agent_server._CHAT_LOCAL_TOOLS}
    handoff = {s["name"] for s in agent_server._CHAT_HANDOFF_TOOLS}
    absent = [n for n in agent_server._CHAT_TOOLS
              if n not in got | local | handoff]
    assert not absent, f"chat offers tools with no implementation: {absent}"
    refused = got & set(agent_server._CHAT_TOOLS_REFUSED)
    assert not refused, f"refused tools reachable from chat: {sorted(refused)}"


def test_mcp_names_the_tools_chat_claims():
    """Same list, checked against the other source of truth: the @mcp.tool()
    definitions in the tool child process."""
    import agent_server
    src = (CODE / "mcp_server.py").read_text(encoding="utf-8")
    defined = set(re.findall(r"@mcp\.tool\(\)\s*\n(?:async )?def (\w+)", src))
    assert defined, "no @mcp.tool() definitions parsed — the scan broke"
    mcp_side = {n for n in agent_server._CHAT_TOOLS
                if n in defined or n in {"read_file", "list_dir", "search_files"}}
    by_name = {n for n in agent_server._CHAT_TOOLS} - mcp_side
    from skills import tool_payload
    catalog = {s["name"] for s in (tool_payload(sorted(by_name)) or [])}
    unresolved = sorted(n for n in by_name if n not in catalog
                        and n not in {s["name"] for s in agent_server._CHAT_LOCAL_TOOLS}
                        and n not in {s["name"] for s in agent_server._CHAT_HANDOFF_TOOLS})
    assert not unresolved, f"chat tools defined nowhere: {unresolved}"


# ── tier B: live service contract, no model calls ────────────────────────
def _services_up() -> bool:
    try:
        return bool(_health())
    except Exception:
        return False


def _token() -> str:
    html = urllib.request.urlopen(AGENT + "/", timeout=8).read().decode("utf-8", "ignore")
    m = re.search(r'name="aria-token"\s+content="([^"]+)"', html)
    return m.group(1) if m else ""


_TOKEN: str | None = None


def _hdr(extra=None):
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = _token()
    h = {"X-Aria-Token": _TOKEN}
    h.update(extra or {})
    return h


def _health() -> dict | None:
    try:
        with urllib.request.urlopen(AGENT + "/api/health", timeout=4) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError:
        # 401 still proves the service is up and guarding the route.
        return {}
    except Exception:
        return None


def _get(path: str, timeout: int = 45):
    req = urllib.request.Request(AGENT + path, headers=_hdr())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode("utf-8", "ignore")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body}


def _live():
    if not _services_up():
        pytest.skip("agent not running on %s — start it with start_aria.ps1" % AGENT)


@pytest.mark.parametrize("path,keys", [
    ("/api/documents", {"documents"}),
    ("/api/apps", {"apps"}),
    ("/api/memory?limit=5", {"items", "has_more", "offset"}),
    ("/api/schedule", {"schedules"}),
    ("/api/notifications", set()),
    ("/api/models", set()),
    ("/api/apps/flags", {"flags"}),
    ("/api/capabilities", set()),
    ("/api/whats-new?limit=2", {"changed", "counts", "cursor", "empty", "summary"}),
    ("/api/documents/folders", {"folders"}),
    ("/api/code/roots", set()),
    ("/api/parts/capabilities", set()),
    ("/api/voice/status", set()),
])
def test_read_endpoints_answer_with_the_shape_the_console_expects(path, keys):
    """Not "is it 200" — "is it the shape the caller destructures". A 200 whose
    body is `{"error": ...}` is the exact bug this repo has been bitten by twice
    (status laundering), so the key check is the point."""
    _live()
    status, body = _get(path)
    assert status == 200, f"{path} -> {status}: {str(body)[:160]}"
    assert isinstance(body, dict), f"{path} returned {type(body).__name__}, not an object"
    if keys:
        assert keys <= set(body), f"{path} missing keys {sorted(keys - set(body))}"
    # `error` as a *field* is fine (several endpoints carry `error: null` in a
    # success envelope); `error` with a value in it is the status-laundering bug.
    err = body.get("error")
    assert err in (None, "", False), f"{path} answered 200 with an error body: {err}"


def test_gateway_reports_up_through_the_agent():
    """The flapping-unreachable report was this being false while the gateway
    was fine, so it is asserted rather than assumed."""
    _live()
    status, body = _get("/api/health")
    assert status == 200 and body.get("agent") == "ready"
    assert body.get("gateway_up") is True, "gateway reported unreachable"


def test_an_unauthenticated_call_is_refused():
    """Read-only probes are convenient and also the reason an unpaired process
    on the LAN can read every memory drawer if the gate slips. Asserted on a
    route that returns real data, not on /api/health."""
    _live()
    req = urllib.request.Request(AGENT + "/api/documents")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status in (401, 403), f"/api/documents answered {status} with no token"


@pytest.mark.parametrize("path,want", [
    ("/api/documents/doc-does-not-exist", 404),
    ("/api/memory/mem-not-real", 404),
    ("/api/schedule/sch-nope", (404, 200)),
    ("/api/whats-new?surfaces=documents&limit=99999", (200, 422)),
])
def test_a_missing_thing_is_a_status_code_not_a_200_with_an_error_body(path, want):
    """Status laundering: returning a dict makes FastAPI answer 200, so a client
    that checks the status believes the call worked. The upload route was fixed
    for exactly this; this guards the rest."""
    _live()
    status, _body = _get(path)
    assert status == want or (isinstance(want, tuple) and status in want), \
        f"{path} answered {status}, expected {want}"


def test_the_document_preview_endpoint_refuses_a_format_it_cannot_render():
    """415 is load-bearing: the document viewer decides whether to mount the
    server pager by it, and a 200-shaped refusal made txt files render a red
    error strip under a readable document."""
    _live()
    _status, listing = _get("/api/documents?limit=200")
    docs = (listing or {}).get("documents") or []
    txt = next((d for d in docs if (d.get("doc_type") or "").lower() == "txt"), None)
    if not txt:
        pytest.skip("no txt document in the library to test the refusal with")
    req = urllib.request.Request(
        GATEWAY + "/v1/documents/" + txt["id"] + "/preview",
        headers={"x-gateway-token": _gw_token()}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 415, f"a txt preview answered {status}; the viewer's 415 rule depends on it"


_GW_TOKEN: str | None = None


def _gw_token() -> str:
    global _GW_TOKEN
    if _GW_TOKEN:
        return _GW_TOKEN
    html = urllib.request.urlopen(GATEWAY + "/", timeout=10).read().decode("utf-8", "ignore")
    m = re.search(r'name="[^"]*token[^"]*"\s+content="([^"]+)"', html, re.I)
    _GW_TOKEN = m.group(1) if m else ""
    return _GW_TOKEN


def test_the_ledger_columns_the_console_renders_exist_and_are_written():
    """`output_tps` and `thinking_tokens` were added as a migration; if the
    migration had not run, every new write would raise and the ledger would
    stop recording while the page still looked populated."""
    _live()
    import sqlite3
    db = ROOT / "llm_gatewayV9" / "gateway_v8.db"
    if not db.exists():
        pytest.skip("no gateway ledger to inspect")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cols = {r[1] for r in con.execute("PRAGMA table_info(calls)")}
    con.close()
    assert {"output_tps", "thinking_tokens"} <= cols, f"missing: {sorted({'output_tps','thinking_tokens'} - cols)}"


# ── tier C: opt-in live agent behaviour ──────────────────────────────────
def _chat(query: str, conversation_id: str, **body) -> dict:
    payload = {"conversation_id": conversation_id, "query": query, **body}
    req = urllib.request.Request(AGENT + "/api/chat/simple",
                                 data=json.dumps(payload).encode(),
                                 headers=_hdr({"Content-Type": "application/json"}))
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read())
    out["_seconds"] = round(time.time() - t0, 2)
    return out


@pytest.mark.skipif(not LIVE_AUDIT, reason="set ARIA_LIVE_AUDIT=1 to spend real turns")
def test_a_greeting_does_not_pay_for_a_corpus_search():
    """The behaviour change with a user-visible cost: "hi" used to run a search
    over every drawer including 177 documents and take 35 seconds."""
    _live()
    cid = "ct-audit-greet-%d" % int(time.time())
    ans = _chat("hi", cid)
    assert (ans.get("answer") or "").strip(), "no answer to a greeting"
    print(f"\n  greeting answered in {ans['_seconds']}s: {ans['answer'][:60]!r}")


@pytest.mark.skipif(not LIVE_AUDIT, reason="set ARIA_LIVE_AUDIT=1 to spend real turns")
def test_a_bound_document_turn_returns_citable_quotes():
    """The Discuss contract: page-and-words markers, because the pane beside it
    can light the words up. If the model stops emitting them the highlights die
    quietly — nothing errors."""
    _live()
    _status, docs = _get("/api/documents?limit=50")
    ready = [d for d in (docs or {}).get("documents", [])
             if d.get("status") == "ready" and d.get("doc_type") in ("pdf", "txt")]
    if not ready:
        pytest.skip("no ready document to bind a turn to")
    doc = ready[0]
    cid = "ct-audit-cite-%d" % int(time.time())
    _post_json("/api/chat/threads/" + cid + "/prefs",
               {"use_documents": True, "doc_ids": [doc["id"]]})
    ans = _chat("What is this document about? Quote one sentence from it.",
                cid, cite_markers=True, doc_ids=[doc["id"]])
    text = ans.get("answer") or ""
    assert len(text) > 40, "a grounded turn answered with nothing"
    markers = re.findall(r"\[\[p:\s*\d*\s*\|", text)
    print(f"\n  doc={doc['id']} {ans['_seconds']}s markers={len(markers)} "
          f"answer={text[:90]!r}")


def _post_json(path: str, payload: dict):
    req = urllib.request.Request(AGENT + path, data=json.dumps(payload).encode(),
                                 headers=_hdr({"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read()[:200].decode("utf-8", "ignore")}


@pytest.mark.skipif(not LIVE_AUDIT, reason="set ARIA_LIVE_AUDIT=1 to spend real turns")
def test_the_agent_can_actually_call_the_delta_tool():
    """The tool being *listed* is proved by tier A. This proves the whole loop:
    schema -> model picks it -> dispatch -> result reaches the answer."""
    _live()
    ans = _chat("Call the whats_new tool with limit 3 and tell me the summary line "
                "it returned, verbatim.", "ct-audit-delta-%d" % int(time.time()))
    _status, expect = _get("/api/whats-new?limit=3")
    words = set(re.findall(r"\b(documents|memory|runs|apps|artifacts)\b",
                           (expect.get("summary") or "").lower()))
    got = (ans.get("answer") or "").lower()
    assert words & set(re.findall(r"\b(documents|memory|runs|apps|artifacts)\b", got)), \
        f"the answer does not look like a delta report: {got[:160]!r}"
