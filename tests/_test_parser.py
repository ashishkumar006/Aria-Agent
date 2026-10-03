import sys, json
sys.path.insert(0, ".")
from skills import _robust_json_parse, parse_skill_json

cases = {
    "bare": '{"a": 1, "b": "x"}',
    "prose_wrapped": 'Let me design the DAG:\n\n{"rationale": "do it", "nodes": [{"id": "n1"}]}',
    "fenced": '```json\n{"skill": "formatter", "inputs": ["n:2"]}\n```',
    "trailing_comma": '{"a": 1, "b": 2,}',
    "multi_fragment": 'Here is node 1: {"id": "n1"} and here is the plan: {"rationale": "r", "nodes": [{"id": "n1"}, {"id": "n2"}]}',
    "unbalanced_tail": '{"rationale": "r", "nodes": [{"id": "n1"}]}',
    "prose_only": 'The search results are still showing some odd content. Let me try a different approach.',
    "nested_quotes": '{"text": "he said \\"hello\\" to me", "n": 2}',
    "newline_in_string": '{"summary": "line1\\nline2", "ok": true}',
}

for name, c in cases.items():
    r = _robust_json_parse(c)
    status = "OK" if (r is not None) == (name != "prose_only") else "UNEXPECTED"
    print(f"[{status}] {name}: {json.dumps(r)[:80] if r else r}")

# parse_skill_json returns {} on failure
print("parse_skill_json(prose_only) ->", parse_skill_json("just prose"))
print("parse_skill_json(bare) ->", parse_skill_json('{"x": 1}'))
