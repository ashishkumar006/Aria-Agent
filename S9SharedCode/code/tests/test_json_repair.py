"""A JSON-contract node gets exactly one repair call, not an instant failure.

Live failure: the planner answered `DAG_OK`. The word is not JSON, the node
failed, and the whole run degraded even though the identical prompt succeeds
on a retry. These tests pin the retry's three properties: it happens once, it
only happens for a JSON-contract skill, and a second miss still fails loudly.
"""
import asyncio
import json

import skills


PLAN = {"nodes": [{"skill": "researcher", "inputs": ["USER_QUERY"]},
                  {"skill": "formatter", "inputs": ["n:2"]}]}


def _run_planner(replies):
    class _FakeLLM:
        def __init__(self):
            self.prompts = []

        def chat(self, prompt=None, **_kw):
            self.prompts.append(prompt or "")
            text = replies[min(len(self.prompts) - 1, len(replies) - 1)]
            return {"text": text, "provider": "test"}

    skill = skills.SkillRegistry().get("planner")
    fake = _FakeLLM()
    orig = skills.LLM
    skills.LLM = lambda *a, **k: fake
    nodes = {"n:1": {"skill": "planner", "inputs": {}, "status": "running"}}
    try:
        result, _rendered = asyncio.run(skills.run_skill(
            skill, "n:1", nodes, "s9-test", "what happened", None))
    finally:
        skills.LLM = orig
    return result, fake


def test_a_non_json_reply_is_repaired_once():
    result, fake = _run_planner(["DAG_OK", json.dumps(PLAN)])
    assert result.success, result.error
    assert [n.skill for n in result.successors] == ["researcher", "formatter"]
    assert len(fake.prompts) == 2
    # The retry says what was wrong and demands the JSON, not a plan rewrite.
    assert "REPLY WITH THE REQUIRED JSON ONLY" in fake.prompts[1]
    assert "DAG_OK" in fake.prompts[1]
    # It quotes the skill's own output contract, not our paraphrase of it.
    assert "## Output" in fake.prompts[1]


def test_a_repaired_plan_is_the_one_that_runs():
    result, fake = _run_planner(["not json at all", json.dumps(PLAN)])
    assert result.success, result.error
    assert [n.skill for n in result.successors] == ["researcher", "formatter"], (
        "the repaired plan must reach the executor as successors")


def test_a_second_miss_still_fails_loudly():
    """One retry is the fix; an endless retry would hide a broken model."""
    result, fake = _run_planner(["DAG_OK", "still not json"])
    assert result.success is False
    assert "no usable JSON" in (result.error or "")
    assert len(fake.prompts) == 2


def test_a_malformed_node_is_repaired_with_the_validation_error():
    bad = {"nodes": [{"skill": "researcher", "inputs": "USER_QUERY"}]}
    result, fake = _run_planner([json.dumps(bad), json.dumps(PLAN)])
    assert result.success, result.error
    assert len(fake.prompts) == 2
    assert "malformed node" in fake.prompts[1]
    assert [n.skill for n in result.successors] == ["researcher", "formatter"]


def test_a_prose_skill_is_not_retried():
    """The repair is for JSON-contract skills. A skill that answers in prose
    (computer use) must not be billed for a second call it cannot satisfy."""
    skill = skills.SkillRegistry().get("computer")
    assert skill.name not in skills._JSON_CONTRACT_SKILLS
