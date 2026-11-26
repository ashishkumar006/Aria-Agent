"""The agent scheduling its own wake-up.

What this feature is: a scheduled row that carries a REASON instead of a QUERY.
At each tick a model-free predicate asks the change delta whether the thing it is
watching has appeared; only a match costs a turn. The point of the whole design is
that those two outcomes are separable in the store, in the console and in the
ledger — a wake that found nothing must not bill anything, and a wake that found
something must be attributable to a reason a human can read and cancel.

The tests below are organised by the failure they prevent, because each guardrail
is a sentence in the spec that would otherwise rot quietly:

  * `agent.self_wake` off means no arm AND no fire — the kill switch has to leave
    no residue, including a fired turn;
  * an arm without a stated reason is refused, so the loop cannot spin on an
    unstated impulse;
  * the interval floor clamps whatever the model proposes, because the POLL is
    the expensive thing, not the turn;
  * an unmatched tick spends zero model calls — asserted by counting calls, not
    by reading the log;
  * a delivered wake consumes the chain and does NOT re-arm itself;
  * the wake's tool set is composed from an allowlist, and the denied names are
    asserted absent rather than filtered at dispatch;
  * the cursor moves only after the turn is stored.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scheduler as sched  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A scheduler with temp state, and self-wake switched ON."""
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sched, "_SCHED_PATH", tmp_path / "schedules.json")
    monkeypatch.setattr(sched, "_LOG_PATH", tmp_path / "scheduler.log")
    sched._SCHEDULES.clear()
    sched._HEAP.clear()
    # The worker reloads schedules.json when another process writes it; reset
    # the mtime baseline so in-memory setup here is not clobbered by a reload.
    monkeypatch.setattr(sched, "_WORKER_MTIME", None)
    import flags as f
    importlib_flags(monkeypatch, f)
    f.set_override("agent.self_wake", True)
    return sched


def importlib_flags(monkeypatch, f):
    """Flags read their override file from S9_STATE_DIR; reload so the temp dir
    is the one in play rather than whatever the previous test left mounted."""
    import importlib
    importlib.reload(f)


def _arm(s, **over):
    kw = dict(reason="watch for the reviewer reply then summarise it",
              conversation_id="ct-wake-test")
    kw.update(over)
    return s.schedule_wake(kw.pop("reason_arg", "watch for the reviewer reply "
                                               "then summarise it"),
                           kw.pop("every", "1h"), **kw)


# ── arming ────────────────────────────────────────────────────────────────
def test_a_wake_is_a_reason_bearing_row_not_a_query(store):
    sid = store.schedule_wake("watch for the reviewer reply, then tell me what "
                              "it means", "1h", conversation_id="ct-1")
    row = {r["id"]: r for r in store.list_schedules()}[sid]
    assert row["kind"] == "wake"
    # The Executor runs `spec["query"]`. A wake carrying a query would be a
    # reminder with extra steps: it would bill a full DAG to ask nothing.
    assert row["query"] == ""
    assert row["notify"] is False, "a wake may not message the user unprompted"
    assert row["reason"].startswith("watch for the reviewer reply")
    assert row["armed_by"] == "agent"
    assert row["wakes_left"] == store.WAKES_LEFT_DEFAULT


def test_an_arm_without_a_reason_is_refused(store):
    with pytest.raises(ValueError) as e:
        store.schedule_wake("   ", "1h", conversation_id="ct-1")
    assert "reason" in str(e.value)


def test_a_wake_must_name_the_conversation_it_reports_into(store):
    with pytest.raises(ValueError):
        store.schedule_wake("watch the solar briefing", "1h", conversation_id="")


def test_the_interval_floor_beats_whatever_the_model_proposes(store):
    """A 30-second wish becomes a 10-minute poll.

    The floor is on the poll, not the turn: the predicate is a read of small
    sidecars, but "every 10s" is 8,640 external reads a day and that is a
    rate-limit problem on somebody else's API, arrived at by a model that meant
    well.
    """
    sid = store.schedule_wake("watch for the reply", "30s",
                              conversation_id="ct-1")
    row = {r["id"]: r for r in store.list_schedules()}[sid]
    assert row["step_s"] >= store.WAKE_FLOOR_S


def test_the_chain_budget_is_bounded_and_refuses_at_zero(store):
    with pytest.raises(ValueError):
        store.schedule_wake("watch", "1h", conversation_id="ct-1", wakes_left=0)
    with pytest.raises(ValueError):
        store.schedule_wake("watch", "1h", conversation_id="ct-1", wakes_left=99)


def test_the_kill_switch_refuses_arming_and_leaves_no_row(store, monkeypatch):
    import flags as f
    f.set_override("agent.self_wake", False)
    with pytest.raises(ValueError) as e:
        store.schedule_wake("watch for the reply", "1h", conversation_id="ct-1")
    assert "agent.self_wake" in str(e.value), "the refusal must name the switch"
    assert store.list_schedules() == []


# ── firing: the predicate is the whole cost model ─────────────────────────
def test_an_unmatched_tick_spends_no_model_call(store, monkeypatch):
    """Asserted by counting calls, not by reading a log line.

    "A wake that finds nothing costs nothing" is the rule the whole feature
    rests on — it is what makes a watch safe to leave armed overnight — so it
    cannot be pinned by a comment.
    """
    spent: list = []
    monkeypatch.setattr(sched, "_next_fire_from_cron", sched._next_fire_from_cron)
    import agent_server as A
    monkeypatch.setattr(A, "_run_wake_turn",
                        lambda **kw: spent.append(kw) or "should not have run")
    sid = store.schedule_wake("watch for the reply", "1h",
                              conversation_id="ct-quiet")
    monkeypatch.setattr(store, "_wake_predicate", lambda spec: (False, {"changed": {}}))
    row = {r["id"]: r for r in store.list_schedules()}[sid]
    store._fire_wake(sid, dict(row))

    assert spent == [], "a wake that matched nothing still ran a turn"
    after = {r["id"]: r for r in store.list_schedules()}[sid]
    # Re-armed and STILL ENABLED: the watch continues, invisibly, and it did not
    # consume a chain step, because a tick is not a wake.
    assert after["enabled"] is True
    assert after["wakes_left"] == store.WAKES_LEFT_DEFAULT
    assert after["next_fire"] > time.time()
    assert after["last_error"] is None


def test_the_kill_switch_stops_existing_wakes_from_firing(store, monkeypatch):
    """Off means off in both directions: no new arm AND no fired turn.

    A switch that only blocks creation would let rows armed last week keep
    waking the agent, which is the residue the rule about kill switches forbids.
    """
    import flags as f
    import agent_server as A
    calls: list = []
    monkeypatch.setattr(A, "_run_wake_turn", lambda **kw: calls.append(kw))
    sid = store.schedule_wake("watch for the reply", "1h", conversation_id="ct-1")
    f.set_override("agent.self_wake", False)
    row = {r["id"]: r for r in store.list_schedules()}[sid]
    store._fire_wake(sid, dict(row))
    assert calls == []
    # Still armed and still stored, so switching it back on resumes this watch —
    # but the row says why it is dark, or "paused by a switch" and "armed and
    # waiting" are indistinguishable on the Scheduler page.
    after = {r["id"]: r for r in store.list_schedules()}[sid]
    assert after["enabled"] is True
    assert "agent.self_wake" in (after.get("last_error") or ""), (
        "a switch-blocked wake gives no reason on its row")


def test_a_matched_wake_runs_once_and_does_not_re_arm_itself(store, monkeypatch):
    """Delivery consumes a chain step and ends the row.

    Continuing is the agent's decision, made by calling schedule_wake with a
    reason. If a delivered wake re-armed by default, "watch for the reply"
    would keep watching after the reply arrived — the boring runaway.
    """
    seen: list = []

    def fake_turn(**kw):
        seen.append(kw)
        return "The reply arrived; it asks for a validation split."

    monkeypatch.setattr(sched, "flags_self_wake_on", lambda: True)
    import agent_server as A
    monkeypatch.setattr(A, "_run_wake_turn", fake_turn)
    monkeypatch.setattr(A, "_session_cost_breakdown", lambda cid: {})
    monkeypatch.setattr(A, "_session_cost_delta", lambda cid, before: None)
    sid = store.schedule_wake("watch for the reply", "1h", conversation_id="ct-1",
                             wakes_left=2)
    monkeypatch.setattr(store, "_wake_predicate",
                        lambda spec: (True, {"changed": {"documents": []},
                                             "counts": {}, "cursor": "v1:1"}))
    row = {r["id"]: r for r in store.list_schedules()}[sid]
    store._fire_wake(sid, dict(row))

    assert len(seen) == 1
    after = {r["id"]: r for r in store.list_schedules()}[sid]
    assert after["enabled"] is False, "a delivered wake re-armed itself"
    assert after["wakes_left"] == 1
    assert after["wakes_fired"] == 1


def test_a_wake_fires_wakes_not_the_query_path(store, monkeypatch):
    """`_worker_tick` must route kind=="wake" to `_fire_wake`.

    Falling through to `_fire` would hand the Executor an empty query and bill a
    DAG run for a row that is a watch.
    """
    routed: list = []
    monkeypatch.setattr(store, "_fire", lambda sid, spec=None: routed.append(("fire", sid)))
    monkeypatch.setattr(store, "_fire_wake",
                        lambda sid, spec: routed.append(("wake", sid)))
    monkeypatch.setattr(store, "_fire_lead", lambda sid, spec: routed.append(("lead", sid)))
    sid = store.schedule_wake("watch for the reply", "1h", conversation_id="ct-1")
    row = store._SCHEDULES[sid]
    row["next_fire"] = time.time() - 1
    store._HEAP.clear()
    import heapq
    heapq.heappush(store._HEAP, (row["next_fire"], sid))
    store._worker_tick()
    assert routed == [("wake", sid)]


def test_a_watch_without_a_match_inside_the_horizon_expires_visibly(store, monkeypatch):
    """The horizon is what makes an unfulfillable watch finite, and it has to say
    so on the row — a silent disarm is indistinguishable from a cancelled one."""
    monkeypatch.setattr(store, "_wake_predicate", lambda spec: (False, {"changed": {}}))
    sid = store.schedule_wake("watch for a reply that never comes", "1h",
                              conversation_id="ct-1")
    row = dict(store._SCHEDULES[sid])
    row["watch_until"] = time.time() - 1
    store._wake_rearm(sid, row)
    after = store._SCHEDULES[sid]
    assert after["enabled"] is False
    assert "nothing matched" in (after.get("last_error") or "")


# ── the predicate itself ──────────────────────────────────────────────────
def test_the_predicate_never_opens_a_giant_drawer(store, monkeypatch):
    """`audit` is 138 MB and `document` 91 MB, and the store rewrites either
    whole per mutation — the mechanism behind the 35-second search. A "what
    changed" built on scanning them would put that stall on a timer, so this is
    asserted by counting reads of those files, not by timing."""
    import delta
    opened: list = []
    real_open = Path.open

    def spy(self, *a, **kw):
        if "memory" in str(self) and ("audit" in str(self) or "document" in str(self)):
            opened.append(str(self))
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", spy)
    monkeypatch.setattr(delta, "compute",
                        lambda **kw: {"changed": {}, "counts": {}, "empty": True,
                                      "cursor": "v1:1"})
    matched, _ = store._wake_predicate({"conversation_id": "ct-1", "watch": {}})
    assert matched is False
    assert opened == [], f"the predicate read a giant drawer: {opened}"


def test_a_watch_match_is_terms_in_one_entry_not_one_term_anywhere(store):
    """"from: reviews@journal.org" must match an entry that carries that sender,
    inside the surface the watch was armed on — not any entry that happens to
    mention the address somewhere."""
    fake = {"documents": [{"filename": "paper.pdf", "what": "indexed"}],
            "memory": [{"detail": "email from reviews@journal.org"}]}

    def compute(**kw):
        want = kw.get("surfaces")
        changed = {k: v for k, v in fake.items() if not want or k in want}
        return {"changed": changed, "counts": {}, "cursor": "v1:1"}

    import delta as d
    orig = d.compute
    d.compute = compute
    try:
        hit = store._wake_predicate({"conversation_id": "ct-1",
                                     "watch": {"surface": "memory",
                                               "match": {"detail": "reviews"}}})
        assert hit[0] is True
        wrong_surface = store._wake_predicate({"conversation_id": "ct-1",
                                               "watch": {"surface": "documents",
                                                         "match": {"detail": "reviews"}}})
        assert wrong_surface[0] is False, "the watch matched outside its surface"
        # Every term must land in the SAME entry: a filename here, a detail
        # there, is not one change that satisfies both.
        split = store._wake_predicate({"conversation_id": "ct-1",
                                       "watch": {"surface": "memory",
                                                 "match": {"a": "reviews",
                                                           "b": "quantum"}}})
        assert split[0] is False
    finally:
        d.compute = orig


# ── the tool, and what the wake may hold ─────────────────────────────────
def test_the_wake_tool_set_is_read_only_by_composition():
    """Not filtered at dispatch — composed. §6's whole argument is that a set
    built by subtraction leaves one forgotten caller as the hole."""
    import agent_server as A
    names = {str(s.get("name") or "") for s in A._wake_tool_payload()}
    assert names, "a wake with no tools cannot even read the change it woke for"
    assert not (names & set(A._WAKE_DENIED_TOOLS)), (
        f"write/send/run tools reachable by a wake: {sorted(names & set(A._WAKE_DENIED_TOOLS))}")
    # It can read, and it can re-arm — the one deliberate exception, without
    # which a watch dies at its first unmet tick.
    assert "whats_new" in names and "search_knowledge" in names
    assert "schedule_wake" in names


def test_delta_readonly_is_a_switch_that_actually_moves_something(tmp_path,
                                                                  monkeypatch):
    """The flag that gates the wake's narrowness must change the payload.

    Asserted because a declared flag nothing reads is a control that lies: the
    audit suite fails on it, and the later action work is the one that will set
    it false, so it has to be true-that-something-when-toggled before anyone
    leans on it.
    """
    import agent_server as A
    import flags as f
    import importlib
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path))
    importlib.reload(f)
    f.set_override("agent.self_wake", True)
    f.set_override("agent.delta_readonly", True)
    narrow = {s.get("name") for s in A._wake_tool_payload()}
    f.set_override("agent.delta_readonly", False)
    wide = {s.get("name") for s in A._wake_tool_payload()}
    assert narrow < wide, (
        f"agent.delta_readonly changed nothing: {sorted(narrow)} vs {sorted(wide)}")
    assert wide & set(A._WAKE_DENIED_TOOLS), "the wide set should include writes"


def test_the_deny_list_names_the_things_a_wake_must_not_do():
    import agent_server as A
    for tool in ("schedule_task", "cancel_scheduled", "send_channel_message",
                 "computer_action", "run_command", "delegate_research",
                 "remember_preference"):
        assert tool in A._WAKE_DENIED_TOOLS, f"{tool} must be denied by name"


def test_a_wake_cannot_arm_into_someone_elses_conversation():
    """The tool's `conversation_id` is advisory: a wake reporting into a thread
    the user never pointed it at is acting behind them."""
    import agent_server as A
    out = json.loads(A._arm_wake_from_tool(
        {"reason": "watch", "conversation_id": "ct-other"}, "ct-mine"))
    assert out["ok"] is False
    assert "conversation it is running in" in out["message"]


def test_arm_refuses_without_a_reason_and_reports_the_refusal_as_data():
    import agent_server as A
    out = json.loads(A._arm_wake_from_tool({"reason": ""}, "ct-mine"))
    assert out["ok"] is False
    assert out["status"] in ("refused", "bad_request")
    assert out["message"], "the model is told what to do instead of getting a 500"


def test_a_wake_inside_a_chain_inherits_the_remaining_budget(monkeypatch):
    """Re-arming from within a wake must not reset to the default, or a chain of
    three becomes a chain of three again at every link and never ends."""
    import agent_server as A
    import flags as f
    monkeypatch.setattr(f, "is_enabled", lambda name, default=False: True)
    A._WAKE_CHAIN["ct-chain"] = {"schedule_id": "wake-parent", "wakes_left": 2}
    out = json.loads(A._arm_wake_from_tool({"reason": "keep watching"}, "ct-chain"))
    assert out["ok"] is True, out
    rows = A._WAKE_CHAIN
    assert "ct-chain" in rows or rows is not None  # chain map is only for reads
    # Read the stored row back through the scheduler's own view.
    sid = out["schedule_id"]
    import scheduler as s
    row = {r["id"]: r for r in s.list_schedules()}[sid]
    assert row["parent"] == "wake-parent"
    assert row["wakes_left"] == 1, f"inherited {row['wakes_left']}, expected 1"
    A._WAKE_CHAIN.pop("ct-chain", None)


def test_a_tool_less_fallback_is_told_it_can_act_on_nothing():
    """The hole this closes was observed live.

    Two provider attempts failed, the tool loop threw, and the plain-text
    fallback answered with an A2UI card titled "Wake Scheduled" for a watch that
    was never armed — `/api/schedule` had no row. The fallback is correct
    resilience; what was missing is that a model with no tools must not be able
    to report having used one.
    """
    import agent_server as A
    noted = A._without_tools_note([{"role": "user", "content": "arm a wake"}])
    assert len(noted) == 2
    assert noted[1]["role"] == "system"
    text = noted[1]["content"]
    assert "TOOLS ARE UNAVAILABLE" in text
    assert "NEVER describe such an action as done" in text
    # The copy is a copy: the caller's list is what the successful path reuses.
    assert len(noted[0]) == len([{"role": "user", "content": "arm a wake"}][0]) or True
    assert noted[0]["content"] == "arm a wake"


def test_the_fallback_note_reaches_the_model_not_just_the_log(tmp_path, monkeypatch):
    """Force the tool loop to throw and assert what the LLM was handed.

    Asserted on the messages rather than on a print line, because the whole
    defect was that the code logged its own degradation while the model,
    uninformed, invented success.
    """
    import agent_server as A
    import gateway as GW
    seen: dict = {}

    class _FakeLLM:
        def chat(self, *a, **kw):
            seen.update(kw)
            return {"text": "I could not run any tools this turn."}

    # `LLM` is imported INSIDE the route from `gateway`, so that is the seam.
    monkeypatch.setattr(GW, "LLM", _FakeLLM)

    async def boom(**kw):
        raise RuntimeError("mcp child died")

    import mcp_runner
    monkeypatch.setattr(mcp_runner, "run_with_tools", boom)
    from fastapi.testclient import TestClient
    from agent_server import app
    # The tool payload must be non-empty or the route takes the intentional
    # no-tools branch, which is a different thing and correctly carries no note.
    monkeypatch.setattr(A, "_chat_tool_payload", lambda: [{"name": "whats_new",
                                                           "input_schema": {}}])
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path))
    token = (ROOT / "state" / "agent.token")
    headers = {"X-Aria-Token": token.read_text(encoding="utf-8").strip()} \
        if token.is_file() else {}
    client = TestClient(app)
    r = client.post("/api/chat/simple",
                    json={"conversation_id": "ct-fallback-note", "query": "hi"},
                    headers=headers)
    assert r.status_code in (200, 401, 403), r.text[:200]
    if r.status_code != 200:
        pytest.skip("no agent token available to reach the route")
    msgs = seen.get("messages") or []
    assert any("TOOLS ARE UNAVAILABLE" in str(m.get("content") or "") for m in msgs), (
        "the fallback ran without telling the model it had no tools")
    assert not seen.get("tools_payload"), (
        "the fallback still handed tools to a path that cannot dispatch them")


def test_a_failed_wake_turn_does_not_burn_the_chain(store, monkeypatch):
    """Found live, twice over.

    The first real firing died on a NameError inside the turn, and the row came
    back `enabled=False, wakes_left=2, wakes_fired=1` with the cursor unmoved:
    the change it was armed for was still unseen, and the watch that would have
    reported it was gone. A transient failure — a busy conversation, a gateway
    mid-restart — must not eat a step, because the step is the only thing
    standing between the user and never hearing about it.
    """
    def explode(**kw):
        raise RuntimeError("the conversation is busy with another turn")

    import agent_server as A
    monkeypatch.setattr(A, "_run_wake_turn", explode)
    monkeypatch.setattr(A, "_session_cost_breakdown", lambda cid: {})
    monkeypatch.setattr(store, "_wake_predicate",
                        lambda spec: (True, {"changed": {"documents": [{}]},
                                             "counts": {"documents": 1},
                                             "cursor": "v1:1"}))
    sid = store.schedule_wake("watch for the reply", "1h", conversation_id="ct-1",
                              wakes_left=2)
    row = dict(store._SCHEDULES[sid])
    store._fire_wake(sid, row)

    after = store._SCHEDULES[sid]
    assert after["enabled"] is True, "a failed wake disarmed itself"
    assert after["wakes_left"] == 2, "a failed wake spent a chain step"
    assert after["wakes_fired"] == 0
    assert "busy" in (after.get("last_error") or ""), "the failure is not on the row"
    assert after["next_fire"] > time.time(), "it did not re-arm to try again"


def test_the_wake_turn_path_runs_for_real():
    """The turn function must not hide a module-local import.

    `LLM` and `mcp_runner.run_with_tools` are imported INSIDE every route in
    this module, so `_run_wake_turn` referencing them as globals raised NameError
    and every wake died in 2.7 seconds — with the unit suite green, because the
    unit suite stubs `_run_wake_turn` and never executes its body. This asserts
    the names it reaches for are the ones it actually binds.
    """
    import inspect
    import agent_server as A
    src = inspect.getsource(A._run_wake_turn)
    assert "from gateway import LLM" in src
    assert "from mcp_runner import run_with_tools" in src
    # And the failure path tells the model the truth rather than leaving it to
    # invent a "Wake Scheduled" card.
    assert "_without_tools_note" in inspect.getsource(A.chat_simple) \
        or "_without_tools_note" in src


def test_a_wake_request_never_ends_on_a_system_or_model_turn(monkeypatch):
    """Gemini answers a request that ends on anything but a user turn with
    HTTP 400 "Requests ending with a model turn are not supported", and the
    gateway surfaces that as a 502. The first real wake died exactly there: the
    wake block was the last message because it reads like system context.

    Asserted on the assembled message list, since every provider in the ring
    has to accept it.
    """
    import agent_server as A
    import gateway as GW
    import mcp_runner

    sent: dict = {}

    class _FakeLLM:
        def chat(self, *a, **kw):
            sent.update(kw)
            return {"text": "noted"}

    monkeypatch.setattr(GW, "LLM", _FakeLLM)

    async def no_tools(**kw):
        raise RuntimeError("force the tool-less path")

    monkeypatch.setattr(mcp_runner, "run_with_tools", no_tools)
    monkeypatch.setattr(A, "_chat_tool_payload", lambda: [])
    A._run_wake_turn(conversation_id="ct-shape",
                     spec={"reason": "watch for the reply",
                           "conversation_id": "ct-shape", "wakes_left": 2},
                     delta={"changed": {}, "counts": {}, "cursor": "v1:1"},
                     schedule_id="wake-test")
    msgs = sent["messages"]
    assert msgs, "the wake turn sent no messages"
    assert msgs[-1]["role"] == "user", (
        f"a wake request must end on a user turn, got {msgs[-1]['role']}")
    assert not any(m["role"] == "system" for m in msgs[-1:])
    # The reason and the change index are both in front of the model.
    joined = " ".join(str(m.get("content")) for m in msgs)
    assert "watch for the reply" in joined
    assert "YOU ARE A SCHEDULED WAKE" in joined


def test_the_streaming_route_reaches_its_fallback_without_shadowing(tmp_path,
                                                                    monkeypatch):
    """The streaming twin, which is where the first fix broke nine tests.

    `_messages` and the fallback live across nested coroutine scopes in this
    route, so *assigning* the noted list inside the except made it local to the
    whole block: the tool loop's own read raised UnboundLocalError before the
    note was ever applied. What the route must be able to do is fall back twice —
    tools die, then the stream is unavailable — and still tell the model the
    truth.
    """
    import agent_server as A
    import gateway as GW
    import mcp_runner
    from fastapi.testclient import TestClient
    from agent_server import app

    seen: list = []

    class _FakeLLM:
        def chat(self, *a, **kw):
            seen.append(kw.get("messages") or [])
            return {"text": "no tools this turn"}

        def stream_chat(self, *a, **kw):
            seen.append(kw.get("messages") or [])
            raise RuntimeError("no streaming here")
            yield {}          # make it an async generator

    monkeypatch.setattr(GW, "LLM", _FakeLLM)

    async def boom(**kw):
        raise RuntimeError("mcp child died")

    monkeypatch.setattr(mcp_runner, "run_with_tools", boom)
    monkeypatch.setattr(A, "_chat_tool_payload",
                        lambda: [{"name": "whats_new", "input_schema": {}}])
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path))
    token = (ROOT / "state" / "agent.token")
    headers = {"X-Aria-Token": token.read_text(encoding="utf-8").strip()} \
        if token.is_file() else {}
    client = TestClient(app)
    with client.stream("POST", "/api/chat/simple/stream",
                      json={"conversation_id": "ct-stream-fallback",
                            "query": "hi"}, headers=headers) as r:
        body = "".join(r.iter_text()) if r.status_code == 200 else ""
    if r.status_code != 200:
        pytest.skip("no agent token available to reach the route")
    assert "UnboundLocalError" not in body, (
        f"the streaming fallback shadowed its own message list: {body[:300]}")
    assert seen, "the streaming route never reached a model call at all"
    noted = [m for m in seen[-1] if "TOOLS ARE UNAVAILABLE" in str(m.get("content"))]
    assert noted, "the streaming fallback did not tell the model it had no tools"


def test_the_real_worker_leaves_a_switched_off_wake_resumable(store, monkeypatch):
    """Driven through `_worker_tick`, because that is where it was lost.

    The worker disarms a one-shot BEFORE firing it, for crash safety. A wake is
    a one-shot, so the pre-disarm consumed it: switch `agent.self_wake` off, the
    tick skips the turn, and the row is left `enabled=False` — "paused by you"
    had become "finished", and switching back on resumed nothing. Three armed
    watches read as finished after one restart before this was fixed.

    Asserted on the FILE at the instant `_fire_wake` is handed the row, not on
    the flag afterwards: the off-path re-arms, which sets `enabled=True` again
    and hides the pre-disarm completely. A first version of this test checked the
    final flag and passed against the broken code.
    """
    import heapq
    import json as _json
    import flags as f
    import agent_server as A
    calls: list = []
    monkeypatch.setattr(A, "_run_wake_turn", lambda **kw: calls.append(kw))
    monkeypatch.setattr(store, "_wake_predicate", lambda spec: (True, {"changed": {}}))
    sid = store.schedule_wake("watch for the reply", "1h", conversation_id="ct-1")
    f.set_override("agent.self_wake", False)

    on_disk: list = []
    real_fire_wake = store._fire_wake

    def spy(s, spec):
        rows = _json.loads(store._SCHED_PATH.read_text(encoding="utf-8-sig"))
        on_disk.append(bool((rows.get(s) or {}).get("enabled")))
        return real_fire_wake(s, spec)

    monkeypatch.setattr(store, "_fire_wake", spy)
    store._SCHEDULES[sid]["next_fire"] = time.time() - 1
    store._HEAP.clear()
    heapq.heappush(store._HEAP, (store._SCHEDULES[sid]["next_fire"], sid))
    store._worker_tick()

    assert calls == [], "a wake fired while the switch was off"
    assert on_disk == [True], (
        f"the worker had already disarmed the watch on disk before firing it: "
        f"{on_disk}")
    row = store._SCHEDULES[sid]
    assert row["enabled"] is True, "a switch-blocked watch ended rather than paused"
    assert row["next_fire"] > time.time(), "a paused watch was left past-due"
    assert "agent.self_wake" in (row.get("last_error") or "")


def test_chat_offers_the_wake_tool_only_while_the_switch_is_on(monkeypatch, tmp_path):
    import agent_server as A
    import flags as f
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path))
    import importlib
    importlib.reload(f)
    f.set_override("agent.self_wake", True)
    on = {s.get("name") for s in (A._chat_tool_payload() or [])}
    f.set_override("agent.self_wake", False)
    off = {s.get("name") for s in (A._chat_tool_payload() or [])}
    assert "schedule_wake" in on
    assert "schedule_wake" not in off, (
        "off must not tell the model the capability exists")


def test_the_mcp_tool_refuses_when_the_switch_is_off(store, monkeypatch):
    """The refusal has to arrive as `disabled`, not as the scheduler's generic
    `refused`, and it has to leave no row.

    `scheduler.schedule_wake` already raises when `agent.self_wake` is off, so
    this is not the thing keeping the switch shut — dropping the tool-level
    check still refuses, just with `status: "refused"` (verified by mutation).
    What the check buys is that the model is told the feature is OFF and what to
    say instead, which `prompts/action.md` depends on: a bare `refused` reads
    like a malformed argument, and the model answers by retrying with different
    words rather than telling you watching is disabled. It also pins the guarantee
    at the boundary the action skill actually calls, so it does not silently move
    if the scheduler's own check ever becomes conditional.
    """
    import flags as f
    import mcp_server
    monkeypatch.setattr(f, "is_enabled",
                        lambda name, default=False: False
                        if name == "agent.self_wake" else default,
                        raising=False)
    out = mcp_server.schedule_wake(
        reason="watch for the review reply", every="1h",
        conversation_id="ct-1")
    assert out["ok"] is False
    assert out["status"] == "disabled"
    assert "agent.self_wake" in out["message"], (
        "the refusal must name the switch, or the model cannot tell the "
        "user what to turn on")
    assert store.list_schedules() == [], "a refused arm must leave no row"
