"""Approvals, the policy dry-run contract and the spend rollup over HTTP.

Three `/v1` surfaces with no behavioural test before this file, each with one
property that is easy to lose silently:

* `/v1/policy/evaluate` answers `allowed: true` for a rule whose action is
  `deny` while the engine is in global dry-run. That is deliberate
  (observe-before-arm) and it is also the exact shape that turns into an
  unenforced policy the moment someone flips `dry_run: false` and their caller
  only reads one field. Both directions are pinned here, plus the per-rule
  `enforce: true` arm and the `approve` verdict — which is "not allowed" but is
  NOT a denial.
* `/v1/approvals` is where an armed `approve` verdict parks a send. Resolving
  must move it out of the pending list and must not re-open a decided one.
* `/v1/spend` is the money view the console's Ledger reads. It must not write
  anything: a rollup that also appends to the ledger inflates the next number
  it reports.

Offline; the ledger, the approvals store and the audit log are all redirected
to `tmp_path`, so nothing here touches the operator's real state.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import approvals as A  # noqa: E402
import db as gdb  # noqa: E402
import main as M  # noqa: E402
import security_audit  # noqa: E402
from policy import PolicyEngine  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

# Must equal tests/conftest.py's TEST_TOKEN.
TOKEN = "test-gateway-token"

# (method, path, body) — the routes this file covers, for the token gate.
ROUTES = [
    ("GET", "/v1/approvals", None),
    ("POST", "/v1/approvals/gw-nonexistent", {"approve": True}),
    ("GET", "/v1/policy", None),
    ("POST", "/v1/policy/evaluate", {"trust": "untrusted", "tool": "run_command"}),
    ("GET", "/v1/spend", None),
]

DENY_RULES = [{
    "id": "deny-destructive",
    "when": {"trust": "untrusted", "tool": "run_command"},
    "action": "deny",
    "reason": "untrusted sender: tools denied by default",
}]


@pytest.fixture
def client():
    return TestClient(M.app)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point the side-effecting stores at `tmp_path` and make policy answers
    independent of whatever `policy.yaml` the checkout currently ships.

    `approvals.STORE_PATH` and `db.DB_PATH` are read as module globals at call
    time, so patching the attribute is enough. The audit paths move because the
    auth middleware records every refused request — without this, a test of the
    401 path would append to the operator's real tamper-evident log.
    """
    monkeypatch.setattr(A, "STORE_PATH", tmp_path / "approvals.json")
    monkeypatch.setattr(gdb, "DB_PATH", str(tmp_path / "ledger.db"))
    gdb.init()
    monkeypatch.setattr(security_audit, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(security_audit, "KEY_PATH", tmp_path / "audit.key")
    monkeypatch.setenv("GATEWAY_AUDIT_KEY", "zz-fake-audit-key")
    monkeypatch.setattr(security_audit, "_key", None)
    monkeypatch.setattr(security_audit, "_last_hash", "")


def _engine(monkeypatch, *, dry_run: bool, rules=None, caps=None):
    """A deterministic engine behind the routes, in either mode.

    The handlers do `from policy import get_engine`, which resolves the module
    attribute at call time, so patching `policy.get_engine` drives the real
    handler with a real (pure, no-I/O) engine whose mode the test chose.
    """
    doc = {"defaults": {"dry_run": dry_run, "default_action": "deny"},
           "rules": DENY_RULES if rules is None else rules,
           "spend_caps_usd": caps if caps is not None
           else {"per_call": 0.50, "per_agent_per_day": 5.00,
                 "per_session_per_day": 10.00}}
    eng = PolicyEngine(doc)
    monkeypatch.setattr("policy.get_engine", lambda: eng)
    return eng


def _evaluate(client, **req):
    r = client.post("/v1/policy/evaluate", json=req)
    assert r.status_code == 200, r.text
    return r.json()


# ── the token gate ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("method,path,body", ROUTES)
def test_a_caller_without_a_token_is_refused(client, method, path, body):
    client.headers.pop("X-Gateway-Token", None)
    r = client.request(method, path, json=body)
    assert r.status_code == 401, (method, path, r.text[:200])
    assert "token" in r.text.lower(), r.text[:200]
    # Spend and the approvals queue are both money-shaped reads: an unauthenticated
    # caller gets nothing, and the refusal itself leaks no credential.
    assert TOKEN not in r.text


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_a_caller_with_the_wrong_token_is_refused(client, method, path, body):
    client.headers["X-Gateway-Token"] = "not-the-operator-token"
    r = client.request(method, path, json=body)
    assert r.status_code == 401, (method, path, r.text[:200])
    assert TOKEN not in r.text


# ── /v1/policy/evaluate: the dry-run contract ────────────────────────────────
def test_dry_run_reports_a_deny_verdict_as_allowed(client, monkeypatch):
    """The documented dry-run semantics, and the risk in one assertion.

    In global dry-run a matching `deny` rule is REPORTED (`action: "deny"`) but
    the call is allowed, so an operator can watch verdicts before arming them.
    A caller that reads only `allowed` therefore sees a pass on what is about to
    become a block — which is why `dry_run` must be true in the same payload.
    """
    _engine(monkeypatch, dry_run=True)
    out = _evaluate(client, trust="untrusted", tool="run_command")
    assert out["allowed"] is True
    assert out["action"] == "deny"
    assert out["dry_run"] is True
    assert out["rule_id"] == "deny-destructive"
    assert "untrusted" in out["reason"]


def test_armed_policy_actually_denies(client, monkeypatch):
    """The other direction, which is the one that matters once dry_run is off:
    a `deny` rule must produce `allowed: false`, not a polite suggestion."""
    _engine(monkeypatch, dry_run=False)
    out = _evaluate(client, trust="untrusted", tool="run_command")
    assert out["allowed"] is False
    assert out["action"] == "deny"
    assert out["dry_run"] is False
    assert out["rule_id"] == "deny-destructive"


def test_one_rule_can_arm_itself_inside_a_dry_run_policy(client, monkeypatch):
    """`enforce: true` is how a single rule goes live while the rest of the
    policy stays report-only; if that flag were ignored, the one guardrail an
    operator chose to arm would silently keep doing nothing."""
    rules = [dict(DENY_RULES[0], enforce=True)]
    _engine(monkeypatch, dry_run=True, rules=rules)
    out = _evaluate(client, trust="untrusted", tool="run_command")
    assert out["allowed"] is False and out["dry_run"] is False
    assert out["action"] == "deny"


def test_an_approve_verdict_is_not_allowed_and_is_not_a_deny(client, monkeypatch):
    """`approve` parks the call in the approvals queue instead of executing it,
    so `allowed` is false while `action` says something a caller must be able to
    tell apart from a hard denial — retrying after approval vs giving up."""
    rules = [{"id": "destructive-needs-approval",
              "when": {"trust": "paired", "tool": "run_command"},
              "action": "approve"}]
    _engine(monkeypatch, dry_run=False, rules=rules)
    out = _evaluate(client, trust="paired", tool="run_command")
    assert out["allowed"] is False
    assert out["action"] == "approve"
    assert out["rule_id"] == "destructive-needs-approval"


def test_nothing_matching_falls_back_to_the_default_deny(client, monkeypatch):
    """First-match-wins with a deny default: an unknown tool must not read as
    allowed simply because no rule named it."""
    _engine(monkeypatch, dry_run=False, rules=[])
    out = _evaluate(client, trust="paired", tool="totally_unknown_tool")
    assert out["allowed"] is False and out["action"] == "deny"
    assert out["rule_id"] is None


def test_evaluate_never_mutates_policy_state(client, monkeypatch):
    """Dry-run by name, dry-run in fact: the engine an evaluation consulted is
    still the engine afterwards, with the same mode and rules."""
    eng = _engine(monkeypatch, dry_run=False)
    before = (eng.dry_run_global, len(eng.rules))
    _evaluate(client, trust="untrusted", tool="run_command")
    _evaluate(client, trust="owner", tool="run_command")
    assert (eng.dry_run_global, len(eng.rules)) == before
    assert client.get("/v1/policy").json()["dry_run"] is False


def test_policy_show_reports_the_configuration_the_routes_use(client, monkeypatch):
    eng = _engine(monkeypatch, dry_run=True)
    body = client.get("/v1/policy").json()
    assert body["dry_run"] is eng.dry_run_global
    assert body["defaults"] == eng.defaults
    assert body["spend_caps_usd"] == eng.spend_caps
    assert [r["id"] for r in body["rules"]] == ["deny-destructive"]


# ── /v1/approvals ────────────────────────────────────────────────────────────
def _seed(tool="run_command", channel="telegram", **kw):
    return A.create(tool=tool, channel=channel,
                    args=kw.pop("args", {"to": "12345", "text": "hi"}),
                    agent=kw.pop("agent", "operator"),
                    session=kw.pop("session", "s-approval-1"),
                    rule_id=kw.pop("rule_id", "paired-destructive-needs-approval"))


def test_the_pending_list_shows_exactly_the_waiting_approvals(client):
    first, second = _seed(), _seed(args={"to": "6789"})
    body = client.get("/v1/approvals").json()
    ids = [e["id"] for e in body["approvals"]]
    assert set(ids) >= {first["id"], second["id"]}
    assert all(e["status"] == "pending" for e in body["approvals"])
    entry = next(e for e in body["approvals"] if e["id"] == first["id"])
    assert entry["tool"] == "run_command" and entry["channel"] == "telegram"
    assert entry["session"] == "s-approval-1"


def test_approving_an_entry_takes_it_out_of_the_queue(client):
    entry = _seed()
    r = client.post(f"/v1/approvals/{entry['id']}", json={"approve": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["approval"]["id"] == entry["id"]
    assert "resolved_ts" in r.json()["approval"]
    assert entry["id"] not in [e["id"] for e in client.get("/v1/approvals").json()["approvals"]]


def test_rejecting_an_entry_takes_it_out_of_the_queue(client):
    entry = _seed()
    r = client.post(f"/v1/approvals/{entry['id']}", json={"approve": False})
    assert r.json()["status"] == "rejected", r.text
    assert entry["id"] not in [e["id"] for e in client.get("/v1/approvals").json()["approvals"]]


def test_a_second_decision_does_not_reopen_the_first(client):
    """Resolution is one-way. A double-click, or a stale dashboard row, must
    not silently flip a rejection into an approval for a send that already
    happened — the store keeps the first verdict and the route reports it."""
    entry = _seed()
    assert client.post(f"/v1/approvals/{entry['id']}",
                       json={"approve": False}).json()["status"] == "rejected"
    again = client.post(f"/v1/approvals/{entry['id']}", json={"approve": True})
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "rejected"
    assert A.list_pending() == []


def test_an_unknown_approval_id_is_a_404(client):
    r = client.post("/v1/approvals/gw-does-not-exist", json={"approve": True})
    assert r.status_code == 404, r.text
    assert "unknown approval" in r.json()["detail"]


def test_an_omitted_approve_field_rejects_rather_than_guessing(client):
    """`ApprovalResolveBody.approve` defaults to False, so a body that forgets
    the field is a REJECTION. Pinned because it is surprising and because a
    dashboard that sends `{}` would otherwise deny work while reporting 200."""
    entry = _seed()
    r = client.post(f"/v1/approvals/{entry['id']}", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"


# ── /v1/spend ────────────────────────────────────────────────────────────────
@pytest.fixture
def ledger():
    """A seeded day of calls: one busy agent, one free one, one row from last
    week.

    Written through `db.conn()` with an explicit `ts`, which `db.log_call`
    cannot give (it always stamps now) and the day-floor behaviour under test
    needs. The columns are the ones `/v1/spend` reads, so a rollup that silently
    renames one shows up here as a KeyError rather than as a confident $0.
    """
    def add(agent, provider, in_tok, out_tok, calls=1, session="s-1", ts=None):
        for _ in range(calls):
            with gdb.conn() as c:
                c.execute(
                    "INSERT INTO calls (ts, provider, model, input_tokens, "
                    "output_tokens, latency_ms, status, agent, session) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (ts or time.time(), provider, "m", in_tok, out_tok,
                     40, "ok", agent, session))

    # groq is priced at 0.15/0.75 USD per Mtok, so 1M+1M is exactly $0.90.
    add("researcher", "groq", 500_000, 500_000, calls=2)
    add("researcher", "kilo", 1000, 1000)
    add("notes", "gemini", 9000, 900)
    add("lastweek", "groq", 5_000_000, 5_000_000,
        ts=time.time() - 14 * 86400)
    return None


def test_spend_totals_and_row_shape(ledger, client):
    """The unscoped view is today's rollup, sorted by calls, with `dollars`
    derived from the provider's rate — the number the Ledger page prints."""
    body = client.get("/v1/spend").json()
    by_agent = {r["agent"]: r for r in body["rows"]}
    assert set(by_agent) >= {"researcher", "notes"}
    assert "lastweek" not in by_agent, "the day floor disappeared from the unscoped view"
    res = [r for r in body["rows"] if r["agent"] == "researcher"]
    groq = next(r for r in res if r["provider"] == "groq")
    assert groq["calls"] == 2 and groq["in_tok"] == 1_000_000
    # 1M in + 1M out at groq's 0.15/0.75 per Mtok.
    assert groq["dollars"] == pytest.approx(0.90)
    assert body["totals"]["calls"] == sum(r["calls"] for r in body["rows"])
    assert body["totals"]["in_tok"] == 1_000_000 + 1000 + 9000
    assert body["totals"]["dollars"] == round(
        sum(float(r["dollars"]) for r in body["rows"]), 6)
    counts = [r["calls"] for r in body["rows"]]
    assert counts == sorted(counts, reverse=True), "rows are not sorted by calls"
    assert body["session"] is None and body["agent"] is None


def test_an_agent_scope_narrows_both_rows_and_totals(ledger, client):
    body = client.get("/v1/spend", params={"agent": "notes"}).json()
    assert {r["agent"] for r in body["rows"]} == {"notes"}
    assert body["totals"]["calls"] == 1
    assert body["totals"]["in_tok"] == 9000
    assert body["agent"] == "notes"
    # An agent with nothing today is an empty rollup, not a 404 and not the
    # whole ledger: the console renders "$0.00 for this agent", which is true.
    ghost = client.get("/v1/spend", params={"agent": "no-such-agent"}).json()
    assert ghost["rows"] == []
    assert ghost["totals"] == {"calls": 0, "in_tok": 0, "out_tok": 0,
                               "dollars": 0}


def test_over_cap_uses_the_cap_for_the_scope_it_was_asked_for(ledger, client,
                                                              monkeypatch):
    """`over_cap` compares the rollup against `per_agent_per_day` when an agent
    was named and `per_session_per_day` when a session was — comparing today's
    total against the wrong cap either cries wolf or hides a real overrun."""
    _engine(monkeypatch, dry_run=False, rules=[],
            caps={"per_call": 0.50, "per_agent_per_day": 0.20,
                  "per_session_per_day": 100.0})
    hot = client.get("/v1/spend", params={"agent": "researcher"}).json()
    assert hot["totals"]["dollars"] == pytest.approx(0.90)
    assert hot["over_cap"] is True
    cold = client.get("/v1/spend", params={"agent": "notes"}).json()
    assert cold["over_cap"] is False
    # A session scope with a generous cap is not flagged by the agent cap.
    scoped = client.get("/v1/spend", params={"session": "s-1"}).json()
    assert scoped["over_cap"] is False
    assert scoped["spend_caps_usd"]["per_session_per_day"] == 100.0


def test_spend_is_read_only_and_leaves_the_ledger_untouched(ledger, client):
    """The rollup must not write. `db.log_call` on this path would make the
    spend view feed on itself: every refresh would add a row that the next
    refresh counts."""
    def snapshot():
        with gdb.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM calls ORDER BY id").fetchall()]

    before = snapshot()
    assert before, "the fixture wrote no ledger rows"
    for params in ({}, {"agent": "researcher"}, {"session": "s-1"}):
        r = client.get("/v1/spend", params=params)
        assert r.status_code == 200, r.text
    after = snapshot()
    assert len(after) == len(before), "GET /v1/spend inserted ledger rows"
    assert after == before, "GET /v1/spend mutated an existing ledger row"
    # And the table files are the only state it reads.
    assert Path(gdb.DB_PATH).exists()
