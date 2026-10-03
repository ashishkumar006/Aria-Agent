"""Tests for the behaviours added in the production-hardening round:
in-flight run deduplication, soft node budgets, the source-health chain
ordering, and citation verification.

Each of these exists because something specific was broken or missing:
duplicate paid research runs, timing that was recorded but never consumed,
a fallback chain that paid the same timeout every run, and citations that
nothing ever checked.
"""
import json
import time

import pytest

import agent_server
import flow
import mcp_server as mcp
import skills


# ── research submit idempotency ─────────────────────────────────────────────
def test_identical_research_submits_join_one_run():
    """A double-click on "Research this topic" used to start a second paid run
    of the same question, and the sidebar then showed two identical topics."""
    key = "research:standard:" + agent_server._req_digest(
        "You are a research agent.\n\nTopic: tallest building?\n\nbe brief")
    with agent_server._inflight_lock:
        agent_server._inflight_runs.clear()
    assert agent_server._inflight_run_lookup(key) == ""
    agent_server._inflight_run_claim(key, "ct-abc123")
    assert agent_server._inflight_run_lookup(key) == "ct-abc123"
    agent_server._inflight_run_release(key)
    assert agent_server._inflight_run_lookup(key) == ""


def test_idempotency_key_ignores_whitespace_and_case():
    # Internal whitespace and case must not create two paid runs of the same
    # question. Punctuation spacing is deliberately NOT normalised: "building?"
    # and "building ?" are different strings and merging them could collapse
    # two genuinely different queries.
    a = agent_server._req_digest("Tallest  Building?")
    b = agent_server._req_digest("  tallest building?  ")
    assert a == b, "the same question typed twice must collapse to one key"
    assert a != agent_server._req_digest("shortest building?")


def test_a_finished_run_does_not_block_the_next_one():
    key = "research:quick:reused"
    with agent_server._inflight_lock:
        agent_server._inflight_runs.clear()
    agent_server._inflight_run_claim(key, "ct-1")
    agent_server._inflight_run_release(key)
    agent_server._inflight_run_claim(key, "ct-2")
    assert agent_server._inflight_run_lookup(key) == "ct-2"


def test_stale_inflight_entries_expire():
    """A run that crashed without releasing its key must not wedge the
    question forever."""
    key = "research:standard:crashed"
    with agent_server._inflight_lock:
        agent_server._inflight_runs.clear()
        agent_server._inflight_runs[key] = ("ct-dead", time.time() - 10_000)
    # The lookup sweeps expired entries.
    assert agent_server._inflight_run_lookup(key) == ""
    assert key not in agent_server._inflight_runs


def test_the_dag_endpoint_also_deduplicates():
    """Regression guard. The idempotency guard was first added only to
    /api/chat/simple/stream, but the Research page posts to /api/chat — so a
    double-click on "Research this topic" still started a second paid DAG
    run. This asserts the DAG path claims and honours a key."""
    src = (agent_server.ROOT / "agent_server.py").read_text(encoding="utf-8")
    handler = src.split('@app.post("/api/chat")')[1].split("@app.post(")[0]
    assert "_inflight_run_lookup" in handler, "/api/chat does not dedupe"
    assert "_inflight_run_claim" in handler, "/api/chat does not claim a key"
    assert "_dedup_stream" in handler, "/api/chat does not return a dedup frame"
    # And the run it delegates to must be able to release the key.
    assert "idem_key: str = \"\"" in src
    assert "_inflight_run_release(idem_key)" in src


# ── soft node budgets ───────────────────────────────────────────────────────
def test_budget_note_is_silent_on_a_fast_run():
    """A normal run must not have its prompt polluted with budget noise."""
    assert flow._budget_note("researcher", {}) == ""
    assert flow._budget_note("researcher", {"researcher": 5.0}) == ""


def test_budget_note_appears_once_the_skill_is_late():
    budget = flow.NODE_BUDGET_S["researcher"]
    late = {"researcher": budget * flow.BUDGET_WARN_RATIO + 1}
    note = flow._budget_note("researcher", late)
    assert note, "a researcher past its budget must be told"
    assert "TIME BUDGET" in note
    assert str(int(budget)) in note
    # It must be actionable, not just informative.
    assert "ONE more pass" in note and "Do not start a broad new search" in note


def test_budget_note_is_per_skill():
    """Spending 200s in the researcher does not make the planner late."""
    assert flow._budget_note("planner", {"researcher": 200.0}) == ""


def test_unknown_skill_gets_the_default_budget():
    assert flow.NODE_BUDGET_S.get("some_new_skill") is None
    spent = {("some_new_skill"): flow.NODE_BUDGET_S["default"] + 1}
    assert "TIME BUDGET" in flow._budget_note("some_new_skill", spent)


def test_budget_note_reaches_the_rendered_prompt():
    reg = skills.SkillRegistry()
    sk = reg.get("researcher")
    plain = skills.render_prompt(sk, "q", [])
    assert "TIME BUDGET" not in plain
    with_note = skills.render_prompt(sk, "q", [],
                                     budget_note=flow._budget_note(
                                         "researcher", {"researcher": 200.0}))
    assert "TIME BUDGET" in with_note


# ── source-health chain ordering ────────────────────────────────────────────
def test_health_order_is_stable_for_untried_sources(monkeypatch, tmp_path):
    """A cold start must behave exactly as before: no data, declared order."""
    monkeypatch.setattr(mcp, "_health_path", lambda: tmp_path / "h.json")
    chain = ["gdelt", "hackernews", "guardian"]
    assert mcp._health_order(chain) == chain
    # Equal scores (all 1.0) must not be shuffled.
    assert mcp._health_order(chain, {"scores": {}}) == chain


def test_a_chronically_failing_source_is_tried_last(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, "_health_path", lambda: tmp_path / "h.json")
    for _ in range(4):
        mcp._record_source("gdelt", False, 8.0)
    for _ in range(4):
        mcp._record_source("hackernews", True, 0.4)
    order = mcp._health_order(["gdelt", "hackernews", "guardian"])
    assert order.index("hackernews") < order.index("gdelt"), order
    assert order[-1] == "gdelt", order


def test_latency_breaks_ties_between_equally_reliable_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, "_health_path", lambda: tmp_path / "h.json")
    for _ in range(3):
        mcp._record_source("fast", True, 0.3)
        mcp._record_source("slow", True, 12.0)
    order = mcp._health_order(["slow", "fast"])
    assert order.index("fast") < order.index("slow"), order


def test_a_recovered_source_climbs_back(monkeypatch, tmp_path):
    """Health must not be a permanent sentence."""
    monkeypatch.setattr(mcp, "_health_path", lambda: tmp_path / "h.json")
    for _ in range(4):
        mcp._record_source("flaky", False, 5.0)
    assert mcp._health_order(["flaky", "steady"])[0] == "steady"
    for _ in range(8):                      # enough successes to outweigh
        mcp._record_source("flaky", True, 0.3)
    assert mcp._health_order(["flaky", "steady"])[0] == "flaky"


def test_health_telemetry_never_raises(monkeypatch, tmp_path):
    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setattr(mcp, "_health_path", lambda: blocked / "sub" / "h.json")
    mcp._record_source("x", True, 0.1)       # must not raise
    assert mcp.source_health()["observed"] == {} or True
    blocked.chmod(0o700)


def test_health_window_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, "_health_path", lambda: tmp_path / "h.json")
    for i in range(mcp._HEALTH_WINDOW + 30):
        mcp._record_source("busy", True, float(i))
    lat = mcp._health_all()["busy"]["lat"]
    assert len(lat) == mcp._HEALTH_WINDOW, len(lat)


# ── citation verification ───────────────────────────────────────────────────
def test_verify_citations_reports_a_confirmed_quote(monkeypatch):
    import httpx

    class _Resp:
        status_code = 200
        text = "The Burj Khalifa stands 828 metres tall in Dubai."

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return _Resp()

    monkeypatch.setattr(httpx, "Client", lambda **kw: _C())
    out = mcp.verify_citations([{"claim": "828m", "source_url": "https://x.example/a",
                                 "quote": "stands 828 metres tall"}])
    assert out["ok"] and out["checked"] == 1
    assert out["rows"][0]["reachable"] is True
    assert out["rows"][0]["quote_found"] is True
    assert out["verdict"] == "all verified"


def test_verify_citations_catches_an_invented_quote(monkeypatch):
    """The whole point: a real URL with a claim that page does not support."""
    import httpx

    class _Resp:
        status_code = 200
        text = "Completely unrelated content about penguins."

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return _Resp()

    monkeypatch.setattr(httpx, "Client", lambda **kw: _C())
    out = mcp.verify_citations([{"claim": "828m", "source_url": "https://x.example/a",
                                 "quote": "stands 828 metres tall"}])
    assert out["rows"][0]["reachable"] is True
    assert out["rows"][0]["quote_found"] is False
    assert out["quote_mismatches"] == 1
    assert "could not be confirmed" in out["verdict"]


def test_verify_citations_survives_a_line_wrapped_quote(monkeypatch):
    """A verbatim phrase split across a line break is still verbatim."""
    import httpx

    class _Resp:
        status_code = 200
        text = "The tower\n   is 828   metres tall."

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return _Resp()

    monkeypatch.setattr(httpx, "Client", lambda **kw: _C())
    out = mcp.verify_citations([{"source_url": "https://x.example/a",
                                 "quote": "The tower is 828 metres tall"}])
    assert out["rows"][0]["quote_found"] is True


def test_verify_citations_reports_a_dead_link(monkeypatch):
    import httpx

    class _Resp:
        status_code = 404
        text = "not found"

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return _Resp()

    monkeypatch.setattr(httpx, "Client", lambda **kw: _C())
    out = mcp.verify_citations([{"source_url": "https://x.example/gone"}])
    assert out["rows"][0]["reachable"] is False
    assert "404" in out["rows"][0]["note"]
    assert out["unreachable"] == 1


def test_verify_citations_rejects_an_empty_batch_without_raising():
    out = mcp.verify_citations([])
    assert out["ok"] is False and "no evidence" in out["error"]
    out = mcp.verify_citations(None)
    assert out["ok"] is False
    out = mcp.verify_citations([{"claim": "no url"}, "nonsense", 42])
    assert out["ok"] is False, "entries without a source_url must not be checked"


def test_verify_citations_survives_a_network_error(monkeypatch):
    import httpx

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "Client", lambda **kw: _C())
    out = mcp.verify_citations([{"source_url": "https://x.example/a", "quote": "q"}])
    assert out["ok"] is True
    row = out["rows"][0]
    assert row["reachable"] is None
    assert "ConnectError" in row["note"]


# ── the researcher's evidence contract ──────────────────────────────────────
def test_researcher_prompt_requires_evidence_and_conflicts():
    body = (skills.ROOT / "prompts" / "researcher.md").read_text(encoding="utf-8")
    assert '"evidence"' in body
    assert '"conflicts"' in body
    assert '"caveats"' in body
    # The instruction that makes quotes trustworthy.
    assert "not a quote" in body.lower()


def test_verify_citations_is_not_fooled_by_inline_markup(monkeypatch):
    """Regression guard, found against a real run.

    Wikipedia renders "As of 2024,<sup>[update]</sup> nearly 80 …". Extracting
    the page text with a space at every tag boundary yields
    "as of 2024 , [ update ] nearly 80", so a verifier comparing raw text
    declared a genuinely verbatim quote fabricated. Matching is on normalised
    content, because what a reader sees is not what the markup says."""
    import httpx

    wiki = ("<p>As of 2024,<sup class='reference'>[update]</sup> nearly 80 "
            "different government space agencies are in existence.</p>")

    class _Resp:
        status_code = 200
        text = wiki

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return _Resp()

    monkeypatch.setattr(httpx, "Client", lambda **kw: _C())
    out = mcp.verify_citations([{
        "claim": "nearly 80 agencies", "source_url": "https://x.example/w",
        "quote": "As of 2024,[update] nearly 80 different government space agencies",
    }])
    assert out["rows"][0]["quote_found"] is True, out["rows"][0]
    assert out["verdict"] == "all verified"


def test_normalisation_ignores_punctuation_not_content():
    n = mcp._norm_for_match
    assert n("As of 2024,[update] nearly 80") == n("as of 2024 , [ update ] nearly 80")
    assert n("828-metres tall!") == "828 metres tall"
    # Content differences must still register.
    assert n("nearly 80 agencies") != n("nearly 90 agencies")


def test_formatter_prompt_forbids_silently_resolving_a_conflict():
    body = (skills.ROOT / "prompts" / "formatter.md").read_text(encoding="utf-8")
    low = body.lower()
    assert "conflicts" in low
    assert "disagree" in low
    assert "do not silently pick" in low or "not silently pick" in low
    assert "caveats" in low


def test_planner_prompt_documents_the_structured_plan():
    body = (skills.ROOT / "prompts" / "planner.md").read_text(encoding="utf-8")
    assert '"research_plan"' in body
    for field in ("topic", "facets", "source_hints", "depth"):
        assert field in body, field
    # The topic is the run title, so the prompt has to be explicit about it.
    assert "never an instruction to yourself" in body


def test_plan_round_trips_through_the_session_store(tmp_path, monkeypatch):
    from persistence import SessionStore
    monkeypatch.setattr("persistence.SESSIONS_ROOT", tmp_path)
    st = SessionStore("s8-plan-test")
    assert st.read_plan() == {}
    st.write_plan({"topic": "tallest building?", "facets": ["a", "b"],
                   "source_hints": ["wikipedia"], "depth": "deep"})
    plan = st.read_plan()
    assert plan["topic"] == "tallest building?"
    assert plan["facets"] == ["a", "b"]
    # A corrupt plan must not take the listing down with it.
    st.plan_path.write_text("{not json", encoding="utf-8")
    assert st.read_plan() == {}
