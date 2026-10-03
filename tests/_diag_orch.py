"""Quick diagnostic for the 4 failing orchestrator integration tests."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent / "S9SharedCode" / "code"
sys.path.insert(0, str(ROOT))

import flow


class _FakeLLM:
    def __init__(self, script):
        self._script = script
        self.calls = []

    def chat(self, prompt=None, *, messages=None, system=None, agent=None,
             session=None, **kw):
        skill = agent or "formatter"
        self.calls.append((skill, (prompt or "")[:60]))
        payload = self._script.get(skill, {"final_answer": f"answer from {skill}"})
        return {"text": json.dumps(payload), "provider": "fake",
                "input_tokens": 10, "output_tokens": 10}

    def vision(self, *a, **kw):
        return {"text": json.dumps({"content": "vision ok"}), "provider": "fake"}


def run(query, script, sid="t-diag"):
    fake = _FakeLLM(script)
    with mock.patch.object(flow, "ensure_gateway", lambda: None), \
         mock.patch("skills.LLM", lambda: fake):
        ans = asyncio.run(flow.Executor().run(query, session_id=sid))
    return ans, fake


print("=" * 70)
print("TEST A: fanout_three_researchers")
ans, fake = run(
    "Compare populations",
    {
        "planner": {"nodes": [
            {"skill": "researcher", "inputs": [], "metadata": {"label": "r1", "question": "London"}},
            {"skill": "researcher", "inputs": [], "metadata": {"label": "r2", "question": "Paris"}},
            {"skill": "researcher", "inputs": [], "metadata": {"label": "r3", "question": "Berlin"}},
            {"skill": "formatter", "inputs": ["n:r1", "n:r2", "n:r3"], "metadata": {}},
        ]},
        "researcher": {"final_answer": "research result"},
        "formatter": {"final_answer": "London, Paris and Berlin compared."},
    },
    sid="t-fanout-diag",
)
print(f"  answer={ans[:100]!r}")
print(f"  calls={[c[0] for c in fake.calls]}")

print("=" * 70)
print("TEST B: planner_short_circuit")
ans, fake = run(
    "capital of France?",
    {"planner": {"answer": "The capital of France is Paris."}},
    sid="t-sc-diag",
)
print(f"  answer={ans[:120]!r}")
print(f"  calls={[c[0] for c in fake.calls]}")

print("=" * 70)
print("TEST C: critic_auto_insert")
ans, fake = run(
    "extract something",
    {
        "planner": {"nodes": [
            {"skill": "distiller", "inputs": ["USER_QUERY"], "metadata": {}},
            {"skill": "formatter", "inputs": ["n:distiller"], "metadata": {}},
        ]},
        "distiller": {"final_answer": "extracted fields"},
        "critic": {"verdict": "pass", "rationale": "looks good"},
        "formatter": {"final_answer": "Final formatted answer."},
    },
    sid="t-critic-diag",
)
print(f"  answer={ans[:120]!r}")
print(f"  calls={[c[0] for c in fake.calls]}")
