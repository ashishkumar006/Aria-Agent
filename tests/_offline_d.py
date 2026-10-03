"""OFFLINE Suite D: skills JSON parser, tool catalog, prompt rendering (no LLM)."""
import sys, os, json
sys.path.insert(0, ".")
os.environ.setdefault("S9_LLM_PROVIDER", "")

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail if not cond else ""))

from skills import parse_skill_json, _robust_json_parse, _repair_json, tool_payload, _TOOL_CATALOG
from schemas import NodeSpec

# ── robust JSON parser: every shape the model has ever emitted ──
cases = [
    ("clean", '{"a": 1}', {"a": 1}),
    ("fenced", '```json\n{"a": 1}\n```', {"a": 1}),
    ("prose-wrapped", 'Here is my plan:\n{"a": 1}\nHope that helps!', {"a": 1}),
    ("trailing-comma", '{"a": [1, 2, 3,]}', {"a": [1, 2, 3]}),
    ("nested", '{"outer": {"inner": "val"}, "n": 2}', {"outer": {"inner": "val"}, "n": 2}),
    ("multi-fragment", '{"small": 1} some text {"big": 2, "more": [1,2,3]}', {"big": 2, "more": [1,2,3]}),
    ("unbalanced-tail", '{"a": 1}}}', {"a": 1}),
    ("unicode", '{"msg": "caf\\u00e9 \\u2192 done"}', {"msg": "café → done"}),
]
for name, src, expect in cases:
    got = _robust_json_parse(src)
    check(f"D.json[{name}]", got == expect, f"got {got!r}")

check("D.json.garbage", _robust_json_parse("no json here at all") is None)
check("D.json.empty", _robust_json_parse("") is None)

# repair path
rep = _repair_json('{"a": [1, 2,],}')
check("D.repair_trailing_comma", json.loads(rep) == {"a": [1, 2]})

# parse_skill_json with planner nodes
plan = parse_skill_json(json.dumps({
    "rationale": "r",
    "nodes": [
        {"skill": "researcher", "inputs": ["USER_QUERY"], "metadata": {"label": "r1", "question": "q?"}},
        {"skill": "formatter", "inputs": ["n:r1"]},
    ],
}))
check("D.plan.parses", plan is not None and plan.get("rationale") == "r")
check("D.plan.nodes_count", len(plan.get("nodes", [])) == 2)

# malformed successor rejected but valid ones kept
# NOTE: parse_skill_json returns the raw dict; NodeSpec validation happens in
# run_skill's dispatch. Validate at that boundary here.
from pydantic import ValidationError
mixed = parse_skill_json(json.dumps({
    "nodes": [
        {"skill": "formatter", "inputs": []},
        {"skill": 12345},           # invalid — no skill string
        {"inputs": ["USER_QUERY"]}, # invalid — missing skill
    ],
}))
kept, dropped = [], []
for s in mixed.get("nodes", []):
    try:
        kept.append(NodeSpec.model_validate(s))
    except ValidationError:
        dropped.append(s)
check("D.plan.mixed_kept", len(kept) == 1 and kept[0].skill == "formatter")
check("D.plan.mixed_dropped", len(dropped) == 2)

# ── tool catalog integrity ──
for t in ("web_search", "fetch_url", "get_time", "currency_convert",
          "send_telegram", "send_email", "schedule_task", "list_scheduled",
          "cancel_scheduled", "get_weather", "search_knowledge"):
    check(f"D.catalog[{t}]", t in _TOOL_CATALOG)

# every catalog entry well-formed
for name, spec in _TOOL_CATALOG.items():
    ok = (spec.get("name") == name and isinstance(spec.get("description"), str)
          and isinstance(spec.get("input_schema"), dict)
          and spec["input_schema"].get("type") == "object")
    check(f"D.catalog.formed[{name}]", ok)

# tools_allowed filtering: unknown names dropped, known kept
payload = tool_payload(["web_search", "not_a_tool", "schedule_task"])
names = [p["name"] for p in payload or []]
check("D.payload.filters_unknown", "not_a_tool" not in names)
check("D.payload.keeps_known", "web_search" in names and "schedule_task" in names)
check("D.payload.empty_none", tool_payload([]) is None)

# ── prompt rendering: memory scoping + question wiring ──
from skills import render_prompt, Skill

class FakeSkill:
    name = "formatter"
    def prompt_template(self):
        return "You are the Formatter skill."

fs = FakeSkill()
# formatter must NOT see memory hits (contamination fix)
rp = render_prompt(fs, "q", [], memory_hits=[type("H", (), {"kind": "fact", "descriptor": "x", "source": "", "value": {}})()])
check("D.render.formatter_no_memory", "MEMORY HITS" not in rp)

class FakePlanner:
    name = "planner"
    def prompt_template(self):
        return "You are the Planner."

pp = render_prompt(FakePlanner(), "q", [], memory_hits=[type("H", (), {"kind": "fact", "descriptor": "x", "source": "", "value": {}})()])
check("D.render.planner_has_memory", "MEMORY HITS" in pp)
check("D.render.memory_disclaimer", "Do NOT weave" in pp or "background" in pp.lower())

# QUESTION block only when provided
rq = render_prompt(fs, "user q", [], question="sub-question?")
check("D.render.question_block", "QUESTION: sub-question?" in rq)
rn = render_prompt(fs, "user q", [])
check("D.render.no_question_block", "QUESTION:" not in rn)

print("\n=== SUITE D: skills parser/catalog/render ===")
print(f"pass={len(PASS)} fail={len(FAIL)}")
for name, err in FAIL:
    print(f"  FAIL {name}: {err}")
