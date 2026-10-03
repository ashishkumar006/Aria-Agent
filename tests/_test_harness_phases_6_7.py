"""Phase 6+7 test harness: recovery, graph logic, skills registry."""
import sys
import tempfile
from pathlib import Path

print('=== Phase 6: Recovery policy ===')
from recovery import classify_failure, plan_recovery, handle_critic_verdict, RecoveryDecision
from flow import Graph
from schemas import AgentResult

# classify_failure
cases = [
    ('503 gateway down', 'transient'),
    ('502 bad gateway', 'transient'),
    ('504 timeout', 'transient'),
    ('connection error', 'transient'),
    ('timeout after 30s', 'transient'),
    ('malformed NodeSpec', 'validation_error'),
    ('ValidationError in schema', 'validation_error'),
    ('computer-use is disabled', 'environmental'),
    ('daemon unavailable', 'environmental'),
    ('no suitable target app', 'environmental'),
    ('requires your approval', 'environmental'),
    ('screenshot capture failed', 'environmental'),
    ('some other upstream miss', 'upstream_failure'),
]
for text, expected in cases:
    got = classify_failure(text)
    assert got == expected, f'classify({text!r}) = {got!r}, expected {expected!r}'
print('  classify_failure: OK')

# plan_recovery decision table
plan_cases = [
    ('transient error 503', 'transient', 'retry'),
    ('validation error bad schema', 'validation_error', 'skip'),
    ('computer-use is disabled', 'environmental', 'skip'),
    ('planner failed: bad plan', 'upstream_failure', 'skip'),
    ('researcher failed: network', 'upstream_failure', 'replan'),
]
for err_text, reason, action in plan_cases:
    skill = 'planner' if 'planner' in err_text else 'researcher'
    d = plan_recovery(failed_skill=skill, error_text=err_text, failed_node_id='n:1')
    assert d.action == action, f'plan({skill},{err_text!r}) = {d.action!r}, expected {action!r}'
    assert d.reason == reason
    if action == 'replan':
        assert d.failure_report is not None
print('  plan_recovery: OK')

# handle_critic_verdict: fail -> splice recovery planner + skip child
g = Graph()
n1_id = g.add_node(skill='distiller', inputs=['n:0'])
g.mark(n1_id, 'complete')
cn = g.add_node(skill='critic', inputs=['USER_QUERY', n1_id],
                metadata={'target': n1_id, 'child': None})
n2 = g.add_node(skill='formatter', inputs=[cn])
g.g.add_edge(cn, n2)
# patch child into metadata now that we have the real id
g.g.nodes[cn]['metadata']['child'] = n2
result = AgentResult(success=False, agent_name='critic',
                     output={'verdict': 'fail', 'rationale': 'bad extraction'},
                     source='run1', run_id='r1')
recovered = {}
cap_hit = []
handled = handle_critic_verdict(cn, result, g, recovered, cap_hit)
assert handled is True
assert g.g.nodes[n2]['status'] == 'skipped'
assert n1_id in recovered
new_nodes = [n for n in g.g.nodes if g.g.nodes[n]['skill'] == 'planner'
             and g.g.nodes[n].get('metadata', {}).get('recovers') == n1_id]
assert len(new_nodes) >= 1, f'expected recovery planner, got {dict(g.g.nodes(data=True))}'
print('  handle_critic_verdict (fail): OK')

# handle_critic_verdict: pass -> returns False, no graph change
g2 = Graph()
n1b = g2.add_node(skill='distiller', inputs=['n:0'])
g2.mark(n1b, 'complete')
cn2 = g2.add_node(skill='critic', inputs=['USER_QUERY', n1b],
                  metadata={'target': n1b, 'child': None})
n2b = g2.add_node(skill='formatter', inputs=[cn2])
g2.g.add_edge(cn2, n2b)
g2.g.nodes[cn2]['metadata']['child'] = n2b
result_pass = AgentResult(success=True, agent_name='critic',
                          output={'verdict': 'pass'},
                          source='run1', run_id='r1')
handled2 = handle_critic_verdict(cn2, result_pass, g2, {}, [])
assert handled2 is False
assert g2.g.nodes[n2b]['status'] == 'pending'
print('  handle_critic_verdict (pass): OK')

# Per-target cap: second critic-fail on same target -> cap hit
g3 = Graph()
n1c = g3.add_node(skill='distiller', inputs=['n:0'])
g3.mark(n1c, 'complete')
cn3 = g3.add_node(skill='critic', inputs=['USER_QUERY', n1c],
                  metadata={'target': n1c, 'child': None})
n2c = g3.add_node(skill='formatter', inputs=[cn3])
g3.g.add_edge(cn3, n2c)
g3.g.nodes[cn3]['metadata']['child'] = n2c
result_fail = AgentResult(success=False, agent_name='critic',
                          output={'verdict': 'fail', 'rationale': 'x'},
                          source='run1', run_id='r1')
recovered3 = {n1c: True}
cap_hit3 = []
handle_critic_verdict(cn3, result_fail, g3, recovered3, cap_hit3)
assert n1c in cap_hit3, f'expected cap_hit, got {cap_hit3}'
print('  handle_critic_verdict (cap hit): OK')

print()
print('=== Phase 7: Skills registry & dispatch ===')
from skills import SkillRegistry, resolve_inputs, _extract_json, PLANNER_SHORTCIRCUIT

reg = SkillRegistry()
names = reg.names()
print(f'  skills loaded: {names}')
assert 'planner' in names
assert 'computer' in names
assert 'vision_file' in names

# Every prompt file exists
for name in names:
    sk = reg.get(name)
    assert sk.prompt_path.exists(), f'prompt missing for {name}: {sk.prompt_path}'
print('  all prompt files exist: OK')

# resolve_inputs: USER_QUERY, n:<i>, art:<sha>, literal
graph_nodes = {
    'n:1': {'result': AgentResult(success=True, agent_name='researcher',
                                  output={'text': 'research done', 'actions': [{'title': 'x'}]},
                                  source='run1', run_id='r1')},
}
inputs = ['USER_QUERY', 'n:1', 'art:abc123', 'freeform']
resolved = resolve_inputs(inputs, graph_nodes, 'what is AI?')
assert resolved[0]['kind'] == 'query'
assert resolved[0]['value'] == 'what is AI?'
assert resolved[1]['kind'] == 'upstream'
assert resolved[1]['skill'] == 'researcher'
# art:abc123 doesn't exist in any store -> artifact-missing
assert resolved[2]['kind'] == 'artifact-missing'
assert resolved[2]['id'] == 'art:abc123'
assert resolved[3] == {'id': 'freeform', 'kind': 'literal', 'value': 'freeform'}
print('  resolve_inputs: OK')

# _extract_json: bare, fenced, trailing prose
assert _extract_json('{"a":1}') == {'a': 1}
assert _extract_json('```json\n{"a":2}\n```') == {'a': 2}
assert _extract_json('some text {"a":3} more text') == {'a': 3}
assert _extract_json('no json here') is None
print('  _extract_json: OK')

# PLANNER_SHORTCIRCUIT is a bool
assert isinstance(PLANNER_SHORTCIRCUIT, bool)
print('  PLANNER_SHORTCIRCUIT: OK')

print()
print('PHASES 6+7 PASSED')
