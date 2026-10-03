"""Phase 2 (L1, live) — E2E scenarios against the running servers.

These require ``agent_server`` on :8500 and ``gateway`` on :8109. They are
marked ``@pytest.mark.live`` and are skipped unless the suite is run with
``-m live``. Each test first verifies the servers are up and ``pytest.skip``s
gracefully otherwise.

Covers: EA-01 (simple → short-circuit, 1 node), EA-02 (complex → full DAG,
>1 node), EA-03 (multi-turn memory continuity), EA-04 (seeded-fact recall),
ED-01 (TTS returns WAV), EH-01 (cost endpoint scoping), EF-01 (scheduler
CRUD), EG-01 (artifact serving), EO-01 (SSE frame contract), EO-02 (replay
reproducibility), EO-03 (cost-by-agent attribution).
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request
import urllib.error
import json
from pathlib import Path

import pytest

from e2e_harness import chat_sse, tts, health

# Every test in this module is a live end-to-end scenario.
pytestmark = pytest.mark.live

AGENT = "http://127.0.0.1:8500"
ROOT = Path(__file__).resolve().parent.parent


def _get_json(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


@pytest.fixture(scope="module")
def live_env():
    try:
        h = health(AGENT)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"agent :8500 not reachable ({e})")
    if not h.get("gateway_up"):
        pytest.skip("gateway not reachable from agent")
    return h


def node_count(frames):
    return sum(1 for f in frames
               if f.get("type") == "log" and str(f.get("text", "")).startswith("[n:"))


# ── A. Conversational core (live) ───────────────────────────────────────────
def test_ea01_live_simple_shortcircuit(live_env):
    ans, frames, meta = chat_sse(AGENT, "What is 2 + 2? Reply with just the number.")
    assert "4" in ans
    # Short-circuit → exactly one node (planner) executed.
    assert node_count(frames) <= 1, node_count(frames)


def test_ea02_live_complex_full_dag(live_env):
    ans, frames, meta = chat_sse(
        AGENT, "Compare the population of London and Paris; which is larger?")
    assert len(ans) > 20
    # Complex query must run a real DAG with >1 node.
    assert node_count(frames) > 1, node_count(frames)


def test_ea03_live_multiturn_memory(live_env):
    cid = "e2e-ea03-" + __import__("time").strftime("%H%M%S")
    chat_sse(AGENT, "Please remember: my favorite color is green.", cid)
    ans2, _, _ = chat_sse(AGENT, "What is my favorite color?", cid)
    assert "green" in ans2.lower(), ans2


def test_ea04_live_seeded_recall(live_env):
    import memory as mem
    mem.add_fact("My lucky number is 42 and I love pizza.",
                 value={"raw": "My lucky number is 42 and I love pizza."},
                 source="seed", run_id="seed-ea04", goal_id=None)
    try:
        ans, _, _ = chat_sse(AGENT, "What is my lucky number?")
        assert "42" in ans, ans
    finally:
        mem.clear()


# ── D. TTS ──────────────────────────────────────────────────────────────────
def test_ed01_tts_returns_wav(live_env):
    audio = tts(AGENT, "Hello from the production readiness test.", "af_heart")
    assert len(audio) > 1000, "TTS returned suspiciously small payload"
    assert audio[:4] == b"RIFF", "expected a WAV container"


# ── H. Cost scoping ──────────────────────────────────────────────────────────
def test_eh01_cost_scoping(live_env):
    cid = "e2e-eh01-" + __import__("time").strftime("%H%M%S")
    chat_sse(AGENT, "Name one capital city in Europe.", cid)
    data = _get_json(f"{AGENT}/api/cost?conversation_id={cid}")
    assert isinstance(data, (dict, list)), type(data)


# ── F. Scheduler ─────────────────────────────────────────────────────────────
def test_ef01_scheduler_crud(live_env):
    payload = json.dumps({"query": "say hi", "when": "in 10m"}).encode()
    req = urllib.request.Request(
        f"{AGENT}/api/schedule", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        created = json.loads(r.read().decode())
    assert created.get("status") in ("ok", "scheduled", "created"), created
    listing = _get_json(f"{AGENT}/api/schedule")
    assert isinstance(listing, (dict, list))


# ── G. Artifact serving ──────────────────────────────────────────────────────
def test_eg01_artifact_serving(live_env):
    ans, frames, meta = chat_sse(
        AGENT, "Summarise https://example.com in one sentence.")
    # Pull session_id + artifacts from the done frame.
    sid = meta.get("session_id") if meta else None
    done = next((f for f in frames if f.get("type") == "done"), None)
    shots = (done or {}).get("browser_artifacts", []) if done else []
    assert isinstance(ans, str) and len(ans) > 0
    if shots:  # browser produced screenshots → endpoint must serve them
        url = AGENT + shots[0]
        with urllib.request.urlopen(url, timeout=30) as r:
            body = r.read()
            ctype = r.headers.get("content-type", "")
        assert r.status == 200 and len(body) > 0
        assert "image" in ctype, ctype


# ── O. Observability ────────────────────────────────────────────────────────
def test_eo01_sse_frame_contract(live_env):
    ans, frames, meta = chat_sse(AGENT, "What is 2 + 2? Just the number.")
    types = [f.get("type") for f in frames]
    assert "meta" in types and "done" in types
    # log frames must precede meta, meta must precede done.
    log_i = [i for i, t in enumerate(types) if t == "log"]
    meta_i = types.index("meta")
    done_i = types.index("done")
    assert max(log_i) < meta_i < done_i
    assert isinstance(meta.get("elapsed_s"), (int, float))
    assert isinstance(meta.get("cost_usd"), (int, float))
    assert isinstance(meta.get("cost_in_tokens"), int)
    assert isinstance(meta.get("cost_out_tokens"), int)
    assert ans


def test_eo02_replay_reproducible(live_env):
    _, frames, meta = chat_sse(AGENT, "What is the capital of Italy?")
    done = next((f for f in frames if f.get("type") == "done"), None)
    sid = (done or {}).get("session_id")
    if not sid:
        pytest.skip("no session_id in done frame")
    rep = ROOT / "replay.py"
    if not rep.exists():
        pytest.skip("replay.py not present")
    proc = subprocess.run(
        [sys.executable, str(rep), sid], cwd=str(ROOT),
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    assert "planner" in proc.stdout.lower() or "Planner" in proc.stdout


def test_eo03_cost_by_agent(live_env):
    _, frames, meta = chat_sse(AGENT, "What is 3 + 3? Just the number.")
    done = next((f for f in frames if f.get("type") == "done"), None)
    sid = (done or {}).get("session_id")
    if not sid:
        pytest.skip("no session_id in done frame")
    data = _get_json(f"{AGENT}/api/cost?session={sid}")
    assert isinstance(data, (dict, list)), type(data)


if __name__ == "__main__":
    import pytest as _p
    _p.main([__file__, "-q", "-m", "live"])
