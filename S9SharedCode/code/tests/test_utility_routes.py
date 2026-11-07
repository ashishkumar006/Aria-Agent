"""Five utility routes the audit found with no route-level test at all.

Each is small, and each had a specific way of lying:

  * `POST /api/memory/policy` — the operator's behavioural rules. A rejected
    body reached `write_policy` anyway, and a failed write must not answer with
    an id the caller would then try to supersede.
  * `POST /api/schedule/purge` — a hard delete. `max_age_days=-1` once clamped
    to 0, which made "older than 0 seconds" true for every past fire and deleted
    every disabled one-shot. An absurd cutoff has to stop before the store is
    opened, and a crash must not be swallowed into `{"status": "ok"}`.
  * `GET /api/cost/by_skill` — the Ledger page, polled every 15 s. A path-shaped
    `conversation_id` made `_turn_cost_path` raise and 500'd the whole rollup;
    so did one bad key already stored in a ledger. And the all-time rollup must
    not call the gateway once per session.
  * `GET /api/mcp/stats` — counters read off disk. A cold or corrupt cache file
    is not an error.
  * `GET /api/docgen/formats` — a plain gateway proxy, so its verb, path and
    credential have to survive the hop.

Nothing here touches the real state dir: the ledgers, the cache directory and
the policy write are all replaced per test.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))            # for test_auth_guard.token_on
sys.path.insert(0, str(HERE.parent))     # for agent_server / memory / scheduler

import agent_server  # noqa: E402
import auth  # noqa: E402
import memory  # noqa: E402
import persistence  # noqa: E402
import scheduler  # noqa: E402
from test_auth_guard import token_on  # noqa: E402

GW_TOKEN = "zzfake-gateway-token"


@pytest.fixture()
def client():
    guard = token_on()
    with guard as c:
        c.headers[auth.TOKEN_HEADER] = guard.token
        yield c


# ── POST /api/memory/policy ──────────────────────────────────────────────

@pytest.fixture()
def policy_store(monkeypatch):
    """The gateway-backed policy write, replaced by a recorder.

    `memory.write_policy` POSTs `/v1/memory/remember`, so leaving it live would
    both contact :8109 and create a real policy row."""
    seen = {"calls": []}

    def _write(text, *, source, run_id, supersedes=None):
        seen["calls"].append({"text": text, "source": source,
                              "run_id": run_id, "supersedes": supersedes})
        err = seen.get("error")
        if err:
            raise err
        return types.SimpleNamespace(id="mem-policy-1")

    monkeypatch.setattr(memory, "write_policy", _write)
    return seen


def test_a_policy_rule_is_written_and_answers_with_its_id(client, policy_store):
    r = client.post("/api/memory/policy", json={"text": "never email a client"})
    assert r.status_code == 200, r.text[:200]
    assert r.json() == {"status": "ok", "id": "mem-policy-1"}, r.json()
    call = policy_store["calls"][0]
    assert call["text"] == "never email a client"
    assert call["source"] == "dashboard", "the audit trail lost who wrote it"
    assert call["run_id"], "a policy row with no run id cannot be traced back"


def test_supersedes_is_forwarded_so_the_old_rule_stops_applying(client,
                                                                policy_store):
    """Without it the corrected rule is appended and the wrong one keeps being
    injected into behaviour, so "I fixed the policy" is not actually true."""
    r = client.post("/api/memory/policy",
                    json={"text": "email only on request",
                          "supersedes": "mem-policy-0"})
    assert r.status_code == 200, r.text[:200]
    assert policy_store["calls"][0]["supersedes"] == "mem-policy-0"


@pytest.mark.parametrize("body", [
    {},                                  # no rule at all
    {"text": ""},
    {"text": "   "},                     # whitespace-only: stored as a blank rule
    {"text": 123},                       # a number where a string belongs
    {"text": ["never", "email"]},        # a list
    {"text": {"nested": "object"}},      # a dict
])
def test_a_policy_body_that_cannot_be_a_rule_is_a_400(client, policy_store,
                                                      body):
    """`body.get("text").strip()` on a number or a list raises AttributeError,
    which surfaced as a bare 500 `text/plain` — and an empty string was accepted
    and written, producing a policy row that changes nothing and is
    indistinguishable from having no policy at all."""
    r = client.post("/api/memory/policy", json=body)
    assert r.status_code == 400, (body, r.status_code, r.text[:200])
    assert "application/json" in r.headers.get("content-type", "")
    assert r.json()["error"]
    assert policy_store["calls"] == [], "a rejected rule still reached the store"


@pytest.mark.parametrize("body", [[1, 2, 3], "a rule", 7, True])
def test_a_policy_body_that_is_not_an_object_is_a_400(client, policy_store,
                                                      body):
    r = client.post("/api/memory/policy", json=body)
    assert r.status_code == 400, (body, r.status_code, r.text[:200])
    assert "application/json" in r.headers.get("content-type", "")
    assert policy_store["calls"] == []


def test_an_unparseable_policy_body_is_a_400_not_a_500(client, policy_store):
    r = client.post("/api/memory/policy", content=b"{not json")
    assert r.status_code == 400, (r.status_code, r.text[:200])
    assert policy_store["calls"] == []


def test_a_failed_policy_write_says_so_in_the_body(client, policy_store):
    """The write failing is reported with the real reason, and never with an id.

    Fixed while this test was being written: the route returned a bare
    `{"status": "error", ...}` dict, so FastAPI sent HTTP 200 and the
    dashboard's `res.ok` check reported a refused rule as saved
    (agent_server.py:8260) — the same laundering `_gw_response` exists to stop.
    """
    policy_store["error"] = RuntimeError("gateway refused the drawer")
    r = client.post("/api/memory/policy", json={"text": "a rule"})
    body = r.json()
    assert r.status_code == 502, (
        f"a failed policy write answered HTTP {r.status_code}: {body}")
    assert body["status"] == "error", body
    assert "gateway refused the drawer" in body["message"], body
    assert "id" not in body, "a failed write answered with a fabricated id"


# ── POST /api/schedule/purge ─────────────────────────────────────────────

@pytest.fixture()
def purge(monkeypatch):
    """The real cutoff check, a fake delete.

    `scheduler.purge_disabled` is kept live so `-1` still raises where it is
    supposed to; only `_purge_locked`, the part that removes rows, is replaced.
    That is what lets a test prove an absurd cutoff stops BEFORE the store is
    opened rather than after."""
    calls = {"locked": [], "public": []}

    real = scheduler.purge_disabled

    def _load():
        return None

    def _purge_locked(max_age_s):
        calls["locked"].append(max_age_s)
        return 2

    def _public(max_age_days=7):
        calls["public"].append(max_age_days)
        return real(max_age_days)

    monkeypatch.setattr(scheduler, "_load", _load)
    monkeypatch.setattr(scheduler, "_purge_locked", _purge_locked)
    monkeypatch.setattr(scheduler, "purge_disabled", _public)
    return calls


def test_a_purge_deletes_and_reports_the_count(client, purge):
    r = client.post("/api/schedule/purge")
    assert r.status_code == 200, r.text[:200]
    assert r.json() == {"status": "ok", "purged": 2}, r.json()
    assert purge["public"] == [7], "the default cutoff was not the documented 7d"
    assert purge["locked"] == [7 * 86400], purge["locked"]


def test_the_cutoff_is_forwarded_in_days_and_converted_to_seconds(client, purge):
    r = client.post("/api/schedule/purge?max_age_days=30")
    assert r.status_code == 200, r.text[:200]
    assert purge["public"] == [30.0], purge["public"]
    assert purge["locked"] == [30 * 86400], (
        "a 30-day request must not purge on a 7-day cutoff")


@pytest.mark.parametrize("cutoff", [-1, -0.5, -1000])
def test_a_negative_cutoff_deletes_nothing(client, purge, cutoff):
    r = client.post(f"/api/schedule/purge?max_age_days={cutoff}")
    assert r.status_code == 400, (cutoff, r.status_code, r.text[:200])
    assert r.json()["error"], r.json()
    assert purge["locked"] == [], "a negative cutoff still reached the delete"


def test_a_non_numeric_cutoff_is_a_parseable_client_error(client, purge):
    r = client.post("/api/schedule/purge?max_age_days=last-week")
    assert r.status_code == 422, (r.status_code, r.text[:200])
    assert "application/json" in r.headers.get("content-type", "")
    assert purge["locked"] == []


def test_a_purge_that_crashes_is_not_reported_as_a_successful_delete(
        client, purge, monkeypatch):
    """A purge that fails must never be reported as a successful delete.

    Fixed while this test was being written: the route handled `ValueError` and
    nothing else, so any other failure escaped as Starlette's `text/plain` 500 —
    undiagnosable by a JSON client (agent_server.py:8448). It now has the same
    catch-all as its sibling routes, so the reason arrives as JSON.
    """
    def _boom(max_age_days=7):
        raise OSError("schedule store is locked")
    monkeypatch.setattr(scheduler, "purge_disabled", _boom)
    r = client.post("/api/schedule/purge")
    assert r.status_code == 500, (r.status_code, r.text[:200])
    assert "application/json" in r.headers.get("content-type", ""), r.text[:200]
    assert "schedule store is locked" in r.json()["error"], r.json()
    assert "purged" not in r.json(), "a failed purge reported a count"
    assert purge["locked"] == []


# ── GET /api/cost/by_skill ───────────────────────────────────────────────

def _turn(usd: float, agent: str = "planner", in_tok: int = 100,
          out_tok: int = 50, calls: int = 1, ts: float = 1.0) -> dict:
    return {"ts": ts, "query": f"q {ts}",
            "per_agent": {agent: {"usd": usd, "in_tok": in_tok,
                                  "out_tok": out_tok, "calls": calls}},
            "totals": {"usd": usd, "in_tok": in_tok, "out_tok": out_tok,
                       "calls": calls}}


@pytest.fixture()
def ledger(monkeypatch):
    """The turn ledgers the rollup reads, entirely in memory."""
    state = {"turns": {}, "resolved": [], "breakdown": []}

    def _read(session_id: str) -> list:
        if session_id.startswith("corrupt"):
            # A key stored before boundary validation that still fails the
            # shape check when read back.
            raise ValueError("unsafe session id")
        return list(state["turns"].get(session_id, []))

    def _resolve(conversation_id, *, create=True):
        state["resolved"].append((conversation_id, create))
        return None if str(conversation_id).startswith("nope") \
            else f"s8-{conversation_id}"

    def _breakdown(session_id):
        state["breakdown"].append(session_id)
        return {}

    monkeypatch.setattr(agent_server, "_read_turn_costs", _read)
    monkeypatch.setattr(agent_server, "_chat_threads_load", lambda: {})
    monkeypatch.setattr(agent_server, "resolve_session", _resolve)
    monkeypatch.setattr(agent_server, "_session_cost_breakdown", _breakdown)
    monkeypatch.setattr(persistence, "list_sessions",
                        lambda: list(state.get("sessions", [])))
    return state


def test_the_rollup_sums_per_agent_across_sessions(client, ledger):
    ledger["sessions"] = ["s8-one", "s8-two"]
    ledger["turns"] = {
        "s8-one": [_turn(1.0, "planner"), _turn(0.5, "researcher")],
        "s8-two": [_turn(2.0, "planner", in_tok=200, out_tok=80, calls=2)],
    }
    body = client.get("/api/cost/by_skill").json()
    by_skill = {r["skill"]: r for r in body["rows"]}
    assert by_skill["planner"]["dollars"] == 3.0, by_skill
    assert by_skill["planner"]["in_tokens"] == 300, by_skill
    assert by_skill["planner"]["calls"] == 3, by_skill
    assert by_skill["researcher"]["dollars"] == 0.5, by_skill
    assert [r["skill"] for r in body["rows"]] == ["planner", "researcher"], (
        "the ledger page sorts by spend; an unsorted rollup buries the costliest")
    assert body["totals"] == {"dollars": 3.5, "calls": 4, "skills": 2}, body


def test_a_scoped_rollup_reads_only_that_conversation(client, ledger):
    """Scoping has to exclude every other session's spend, and include both keys
    this conversation can be stored under: the resolved graph session (`s8-*`)
    and the lightweight chat thread, whose ledger is keyed by the conversation id
    itself because it has no graph dir."""
    ledger["sessions"] = ["s8-other"]
    ledger["turns"] = {"s8-other": [_turn(9.0)],
                       "s8-chat-1": [_turn(1.0)],
                       "chat-1": [_turn(0.25)]}
    body = client.get("/api/cost/by_skill?conversation_id=chat-1").json()
    assert body["totals"]["dollars"] == 1.25, body
    assert "s8-other" not in json.dumps(body["turns"]), body["turns"]
    assert {t["session"] for t in body["turns"]} == {"s8-chat-1", "chat-1"}


def test_a_get_does_not_mint_a_phantom_conversation(client, ledger):
    """`resolve_session(create=False)` is the read-only form. With `create=True`
    a typo'd id from a bookmarked URL added a permanent entry to
    conversations.json on every page load, and nothing prunes that map."""
    client.get("/api/cost/by_skill?conversation_id=nope-not-real")
    assert ledger["resolved"], "the conversation was never resolved at all"
    assert all(create is False for _, create in ledger["resolved"]), (
        ledger["resolved"])


def test_an_unknown_conversation_is_reported_not_invented(client, ledger):
    """The caller needs to know the id matched nothing; an empty rollup for a
    nonexistent conversation looks exactly like one that spent nothing."""
    body = client.get("/api/cost/by_skill?conversation_id=nope-x").json()
    assert body["unknown_conversation"] is True, body
    assert body["rows"] == [] and body["totals"]["dollars"] == 0.0, body


def test_a_path_shaped_conversation_id_is_a_400(client, ledger):
    """`../..` reached `Path()` and `_turn_cost_path` raised ValueError, which
    used to 500 the whole rollup — on a route the console polls every 15 s, so
    the Ledger page was dead until the state dir was edited by hand."""
    r = client.get("/api/cost/by_skill?conversation_id=..%2F..%2Fetc")
    assert r.status_code == 400, (r.status_code, r.text[:200])
    assert r.json()["error"] == "invalid conversation id", r.json()


def test_one_corrupt_stored_key_does_not_500_the_whole_rollup(client, ledger):
    """The same shape check fires when reading a ledger stored before the
    boundary validation existed. One bad key must skip, not kill the page."""
    ledger["sessions"] = ["corrupt-old", "s8-good"]
    ledger["turns"] = {"s8-good": [_turn(1.0)]}
    r = client.get("/api/cost/by_skill")
    assert r.status_code == 200, r.text[:200]
    assert r.json()["totals"]["dollars"] == 1.0, r.json()


def test_the_all_time_rollup_never_calls_the_gateway_per_session(client, ledger):
    """The per-session gateway fallback is scoped to `conversation_id` precisely
    because an all-time rollup would make one blocking round trip per session —
    which is how the Ledger page took seconds to render a table of zeros."""
    ledger["sessions"] = [f"s8-{i}" for i in range(12)]
    r = client.get("/api/cost/by_skill")
    assert r.status_code == 200, r.text[:200]
    assert ledger["breakdown"] == [], ledger["breakdown"]


def test_the_turn_list_is_capped_and_newest_first(client, ledger):
    """200 sessions x 50 turns is 10,000 rows the browser would have to build
    DOM for; the page only ever shows the recent tail."""
    ledger["sessions"] = [f"s8-{i}" for i in range(5)]
    ledger["turns"] = {f"s8-{i}": [_turn(0.1, ts=float(i * 100 + j))
                                   for j in range(60)] for i in range(5)}
    body = client.get("/api/cost/by_skill").json()
    assert len(body["turns"]) == 200, len(body["turns"])
    ts = [t["ts"] for t in body["turns"]]
    assert ts == sorted(ts, reverse=True), "the ledger listed spend oldest-first"


# ── GET /api/mcp/stats ───────────────────────────────────────────────────

@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """`ARIA_CACHE_DIR` is read at call time, so this moves the whole probe."""
    d = tmp_path / "fetch-cache"
    d.mkdir()
    monkeypatch.setenv("ARIA_CACHE_DIR", str(d))
    monkeypatch.delenv("ARIA_FETCH_CACHE", raising=False)
    return d


def _stats(cache_dir, hits, misses):
    (cache_dir / "_stats.json").write_text(
        json.dumps({"hits": hits, "misses": misses}), encoding="utf-8")


def test_the_fetch_cache_counters_are_computed_from_disk(client, cache_dir):
    _stats(cache_dir, 3, 1)
    (cache_dir / "a.json").write_text("{}", encoding="utf-8")
    (cache_dir / "b.json").write_text("{}", encoding="utf-8")
    (cache_dir / "notes.txt").write_text("not an entry", encoding="utf-8")
    body = client.get("/api/mcp/stats").json()
    c = body["cache"]
    assert c["hits"] == 3 and c["misses"] == 1, c
    assert c["hit_rate"] == 0.75, c
    assert c["entries"] == 2, (
        "the entry count counted non-cache files, so the operator sees a cache "
        "twice the size it is")
    assert c["enabled"] is True, c


def test_bookkeeping_files_are_not_counted_as_cache_entries(client, cache_dir):
    _stats(cache_dir, 0, 0)
    (cache_dir / "_stats.json").write_text(
        json.dumps({"hits": 0, "misses": 0}), encoding="utf-8")
    (cache_dir / "_lock.json").write_text("{}", encoding="utf-8")
    assert client.get("/api/mcp/stats").json()["cache"]["entries"] == 0


def test_a_cold_cache_with_no_stats_file_is_not_an_error(client, cache_dir):
    """The first request an agent ever makes has no `_stats.json` at all. A page
    reporting counters must not 500 because there is nothing to count yet."""
    r = client.get("/api/mcp/stats")
    assert r.status_code == 200, r.text[:200]
    c = r.json()["cache"]
    assert c["hits"] == 0 and c["misses"] == 0 and c["entries"] == 0, c
    assert c["hit_rate"] == 0.0, c


def test_a_corrupt_stats_file_falls_back_to_zeroes(client, cache_dir):
    """`_stats.json` is written by a different process (the MCP subprocess per
    skill invocation), so a torn write is expected, not exceptional."""
    _stats(cache_dir, 3, 1)
    (cache_dir / "_stats.json").write_text("{trunc", encoding="utf-8")
    r = client.get("/api/mcp/stats")
    assert r.status_code == 200, r.text[:200]
    assert r.json()["cache"]["hits"] == 0, r.json()


def test_non_integer_counters_do_not_crash_the_report(client, cache_dir):
    _stats(cache_dir, "three", None)
    r = client.get("/api/mcp/stats")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert r.json()["cache"]["hits"] == 0, r.json()


@pytest.mark.parametrize("flag", ["0", "false", "no"])
def test_the_cache_disable_flag_is_reported_as_disabled(client, cache_dir,
                                                        monkeypatch, flag):
    """The operator has to be able to tell "the cache is off" from "the cache
    has seen nothing", which the zeroed counters alone cannot say."""
    monkeypatch.setenv("ARIA_FETCH_CACHE", flag)
    _stats(cache_dir, 1, 1)
    assert client.get("/api/mcp/stats").json()["cache"]["enabled"] is False


def test_the_other_counters_are_present_even_when_their_owner_is_unavailable(
        client, cache_dir):
    """Read from disk rather than by importing the MCP server — that module is a
    different process, so importing it here would report this process's zeroed
    counters as if they were the truth."""
    body = client.get("/api/mcp/stats").json()
    assert set(body) >= {"cache", "tool_outcomes", "mcp_breaker"}, body
    assert body["tool_outcomes"] is None or isinstance(body["tool_outcomes"], dict)
    assert body["mcp_breaker"] is None or "open" in body["mcp_breaker"]


# ── GET /api/docgen/formats ──────────────────────────────────────────────

class _Gateway:
    """A scripted gateway, and a record of every request that reached it."""

    def __init__(self, responses):
        self._responses = [responses] if callable(responses) else list(responses)
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        nxt = self._responses[min(len(self.seen) - 1, len(self._responses) - 1)]
        if callable(nxt):
            return nxt(request)
        return httpx.Response(nxt.status_code, content=nxt.content,
                              headers=dict(nxt.headers))

    @property
    def methods(self):
        return [r.method for r in self.seen]

    @property
    def paths(self):
        return [r.url.path for r in self.seen]

    @property
    def tokens(self):
        return [r.headers.get("X-Gateway-Token") for r in self.seen]


@pytest.fixture()
def gw(monkeypatch):
    """Mount a MockTransport on the module's pooled async gateway client.

    `_gw_auth_headers` is pinned so nothing here reads the real
    `llm_gatewayV9/state/gateway.token`, and `ensure_gateway` is neutered so a
    failing proxy can never escalate into launching a gateway process.
    `tests/conftest.py` empties the pooled clients per test, which is what makes
    the patch apply to THIS test's connection."""
    monkeypatch.setattr(agent_server, "_gw_auth_headers",
                        lambda extra=None: {"X-Gateway-Token": GW_TOKEN})
    import gateway
    monkeypatch.setattr(gateway, "ensure_gateway", lambda: None)
    holder = {}

    def _mount(responses):
        stub = _Gateway(responses)
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient",
                            lambda *a, **k: real(*a,
                                                 transport=httpx.MockTransport(stub),
                                                 **k))
        holder["stub"] = stub
        return stub

    return _mount


def test_the_schema_request_is_a_get_on_the_gateway_path(client, gw):
    schema = {"formats": ["pdf", "pptx", "docx"], "content_model": {"blocks": []}}
    stub = gw([httpx.Response(200, json=schema)])
    r = client.get("/api/docgen/formats")
    assert r.status_code == 200, r.text[:200]
    assert r.json()["formats"] == ["pdf", "pptx", "docx"], r.json()
    assert stub.methods == ["GET"], stub.methods
    assert stub.paths == ["/v1/docgen/schema"], stub.paths


def test_the_format_call_presents_the_gateway_token(client, gw):
    """The gateway requires `X-Gateway-Token` on every `/v1/*` route, and the
    pooled client deliberately carries no headers — so the request-level header
    is the only credential on this path."""
    stub = gw([httpx.Response(200, json={"formats": []})])
    client.get("/api/docgen/formats")
    assert stub.tokens == [GW_TOKEN], stub.tokens


def test_a_gateway_refusal_reaches_the_console_as_a_reason(client, gw):
    """The picker must not fall back to an empty format list: an empty list is
    what "this gateway renders nothing" means to the UI, so an outage would be
    reported as a feature the user does not have.

    Fixed while this test was being written: the route returned the raw
    `_gw_json` dict instead of wrapping it in `_gw_response`, so the status line
    stayed 200 even when the gateway said 500 (agent_server.py:8648). The
    payload was already honest; the status line was the lie, and `if (res.ok)`
    in any client reads the status line.
    """
    stub = gw([httpx.Response(500, json={"error": "generator registry broken"})])
    r = client.get("/api/docgen/formats")
    body = r.json()
    assert r.status_code == 500, (
        f"gateway 500 laundered into HTTP {r.status_code}: {body}")
    assert "formats" not in body, (
        f"a gateway 500 was laundered into an empty success: {body}")
    assert "generator registry broken" in body["error"], body
    assert stub.methods == ["GET"]


def test_an_unreachable_gateway_is_reported_as_such(client, gw):
    def _refuse(request):
        raise httpx.ConnectError("connection refused", request=request)
    gw([_refuse])
    r = client.get("/api/docgen/formats")
    body = r.json()
    # A gateway we could not reach is a 5xx, never an invented 4xx and never a
    # 200. `_gw_json` labels it 503, which is the honest code for "upstream gone".
    assert r.status_code == 503, (r.status_code, body)
    assert "unreachable" in str(body.get("error", "")).lower(), body
    assert "formats" not in body, body


def test_a_schema_that_is_not_an_object_is_not_passed_on_as_success(client, gw):
    """`_gw_json` only ever hands back a dict; an array body means the gateway
    answered something else entirely, and rendering it as a schema would be a
    fabricated picker."""
    gw([httpx.Response(200, json=["pdf", "pptx"])])
    body = client.get("/api/docgen/formats").json()
    assert body.get("error"), body
    assert "formats" not in body, body


def test_a_401_is_retried_once_with_a_refreshed_token(client, gw, monkeypatch):
    issued = iter([GW_TOKEN, "zzfake-rotated"])
    monkeypatch.setattr(agent_server, "_gw_auth_headers",
                        lambda extra=None: {"X-Gateway-Token": next(issued)})
    stub = gw([httpx.Response(401, json={"error": "invalid token"}),
               httpx.Response(200, json={"formats": ["pdf"]})])
    r = client.get("/api/docgen/formats")
    assert r.status_code == 200, r.text[:200]
    assert stub.tokens == [GW_TOKEN, "zzfake-rotated"], stub.tokens
    assert stub.methods == ["GET", "GET"]


def test_an_empty_turn_ledger_falls_back_to_the_gateway_breakdown(
        client, ledger):
    """The fallback that used to be dead code.

    `GET /api/cost/by_skill?session=...` is what the Research page and the
    Ledger page read. A DAG run's spend only lands in
    `sessions/<sid>/turn_costs.json` when the run finishes cleanly, so the route
    carries a fallback that reads the gateway's per-session totals instead. It
    sat BELOW `rows = [...for ag, d in agg.items()]`, filling `agg` after `rows`
    and `totals` had already been derived from it — so a completed research
    conversation reported $0 while /api/cost showed real tokens, which is
    precisely the symptom its own comment claims was fixed.

    Asserted on the numbers, because `{"rows": [], "totals": {"dollars": 0}}` is
    indistinguishable from "this run was free" to anything reading it.
    """
    ledger["turns"] = {}                      # the turn ledger is empty
    ledger["breakdown"] = []
    ledger["sessions"] = ["s8-chat-1"]

    def _bd(session_id):
        return {"planner": {"usd": 0.02, "in_tok": 1200, "out_tok": 90,
                            "calls": 1},
                "researcher": {"usd": 0.11, "in_tok": 48000, "out_tok": 900,
                               "calls": 4}}

    import agent_server
    agent_server._session_cost_breakdown = _bd
    body = client.get("/api/cost/by_skill?conversation_id=chat-1").json()
    by_skill = {r["skill"]: r for r in body["rows"]}
    assert by_skill, "the gateway fallback produced no rows at all"
    assert by_skill["researcher"]["dollars"] == 0.11, by_skill
    assert by_skill["researcher"]["in_tokens"] == 48000, by_skill
    assert body["totals"]["dollars"] == 0.13, body["totals"]
    assert body["totals"]["skills"] == 2, body["totals"]


def test_the_fallback_is_not_called_for_the_all_time_rollup(client, ledger):
    """Cost, not correctness: the all-time view must not call the gateway once
    per session. `breakdown` stays empty when no conversation is scoped."""
    ledger["sessions"] = [f"s8-{i}" for i in range(5)]
    ledger["turns"] = {"s8-0": [_turn(1.0)]}
    body = client.get("/api/cost/by_skill").json()
    assert ledger["breakdown"] == [], ledger["breakdown"]
    assert body["totals"]["dollars"] == 1.0, body
