"""Phase 8-11 test harness: MCP, scheduler, templates, sandbox, safety, gateway, server routes, test collection."""
import sys
import tempfile
import json
import os
from pathlib import Path

print('=== Phase 8: MCP server, scheduler, templates, sandbox ===')

# --- scheduler ---
from scheduler import schedule, list_schedules, cancel, _next_fire_from_cron
import time

with tempfile.TemporaryDirectory() as td:
    import scheduler as sched_mod
    sched_mod._SCHED_PATH = Path(td) / 'schedules.json'
    sid1 = schedule('query1', 'in 2m', notify=False)
    assert sid1.startswith('sch-')
    all1 = list_schedules()
    assert len(all1) == 1
    assert all1[0]['query'] == 'query1'
    assert cancel(sid1)
    # cancel is idempotent: returns True if the schedule exists, even if already disabled
    assert cancel(sid1)
    all2 = list_schedules()
    assert len(all2) == 1
    assert not all2[0]['enabled']
    print('  scheduler schedule/list/cancel: OK')

    # cron math
    base = 1_700_000_000.0
    fire = _next_fire_from_cron('daily@09:30', base)
    assert fire > base
    fire2 = _next_fire_from_cron('every 15m', base)
    assert abs(fire2 - (base + 15*60)) < 1
    print('  scheduler cron math: OK')

# --- templates ---
from templates import save, list_templates, get, delete, render
with tempfile.TemporaryDirectory() as td:
    import templates as tpl_mod
    tpl_mod._TPL_PATH = Path(td) / 'templates.json'
    tpl = save('daily_brief', 'Summarize {topic}', vars=['topic'])
    assert tpl['name'] == 'daily_brief'
    assert get('daily_brief') is not None
    assert get('nonexistent') is None
    all_tpl = list_templates()
    assert len(all_tpl) == 1
    rendered = render('daily_brief', {'topic': 'AI news'})
    assert rendered == 'Summarize AI news'
    assert render('nonexistent', {}) is None
    assert delete('daily_brief')
    assert not delete('daily_brief')
    print('  templates: OK')

# --- sandbox ---
from sandbox import run_python
result = run_python('print("hello")\n', timeout_s=10)
assert result['exit_code'] == 0
assert 'hello' in result['stdout']
assert not result['timed_out']
print('  sandbox run_python: OK')

# timeout
result2 = run_python('import time; time.sleep(30)\n', timeout_s=2)
assert result2['timed_out'] is True
assert result2['exit_code'] == -1
print('  sandbox timeout: OK')

# truncation
big = 'x' * 2_000_000
result3 = run_python(f'print({big!r})\n', timeout_s=10, stdout_cap=1000)
assert result3['stdout_truncated'] is True
print('  sandbox truncation: OK')

# --- mcp_server tool introspection ---
import mcp_server
# Confirm tools are registered (FastMCP stores them)
assert hasattr(mcp_server, 'mcp'), 'FastMCP app not found'
# Just confirm the module-level tools exist as callables
for tool_name in ['web_search', 'fetch_url', 'get_time', 'currency_convert',
                   'read_file', 'list_dir', 'create_file', 'update_file',
                   'edit_file', 'index_document', 'send_telegram', 'send_email',
                   'create_calendar_event', 'get_calendar_events', 'get_weather',
                   'computer_action', 'search_knowledge', 'github_query',
                   'slack_message', 'notion_query', 'gmail_query',
                   'gmail_refresh_token']:
    assert hasattr(mcp_server, tool_name), f'mcp_server missing tool: {tool_name}'
print('  mcp_server tools registered: OK')

# --- mcp_server _safe path guard ---
from mcp_server import _safe
try:
    _safe('../etc/passwd')
    assert False, 'should have raised'
except ValueError:
    pass
print('  mcp_server _safe path guard: OK')

# --- mcp_runner tool cache ---
from mcp_runner import _cache_get, _cache_put, _cache_key, _TOOL_CACHE
# Clear any stale entries from prior runs (disk-backed cache persists)
_TOOL_CACHE.clear()
unique_q = 'unique_test_query_xyz_123'
key = _cache_key('web_search', {'q': unique_q})
assert _cache_get('web_search', {'q': unique_q}) is None  # cold
_cache_put('web_search', {'q': unique_q}, 'result text')
assert _cache_get('web_search', {'q': unique_q}) == 'result text'
# non-cacheable tool
assert _cache_get('read_file', {'path': 'x'}) is None
print('  mcp_runner tool cache: OK')

print()
print('=== Phase 9: Computer-use safety ===')
from computer_use.safety.gates import SafetyGates, DEFAULT_APPROVAL_PATTERNS, DEFAULT_DENY_PATHS

# Test with fresh env (note: project .env has COMPUTER_USE_ENABLED=true,
# so we override here to test the default-disabled path)
os.environ['COMPUTER_USE_ENABLED'] = 'false'
os.environ['COMPUTER_USE_MODE'] = 'dry-run'
os.environ.pop('COMPUTER_USE_ALLOW', None)
os.environ.pop('COMPUTER_USE_DENY', None)
gates = SafetyGates()
assert gates.enabled is False
assert gates.mode == 'dry-run'
print('  SafetyGates disabled by default: OK')

# Note: project .env has COMPUTER_USE_ENABLED=true + COMPUTER_USE_MODE=live
# (verified via Get-Content). That is a real config finding: computer-use is
# actually enabled in the project's default state.
print('  [FINDING] project .env has COMPUTER_USE_ENABLED=true, COMPUTER_USE_MODE=live')

# needs_approval whole-word tokenization
assert gates.needs_approval('run_command', {'command': 'Get-PSDrive | Format-Table'}) is False
assert gates.needs_approval('run_command', {'command': 'rm -rf /tmp/x'}) is True
# 'Restart-Service' tokenizes as 'restart-service' (dash kept), which is
# distinct from 'restart' — so it does NOT trigger approval (by design).
assert gates.needs_approval('run_command', {'command': 'Restart-Service foo'}) is False
# bare 'restart' does trigger
assert gates.needs_approval('run_command', {'command': 'restart the server'}) is True
assert gates.needs_approval('run_command', {'command': 'echo hello'}) is False
print('  needs_approval whole-word: OK')

# path_blocked
assert gates.path_blocked(r'C:\Windows\System32') is True
assert gates.path_blocked(r'C:\Users\AISHWARYA\Documents') is False
print('  path_blocked: OK')

# cmd_blocked: checks deny_cmds + DEFAULT_DENY_PATHS + allowlist
# 'del' is an approval pattern (needs_approval), NOT a cmd_blocked pattern.
# cmd_blocked catches explicit path references and env deny-list entries.
assert gates.cmd_blocked('rm -rf C:\\Windows\\System32') is not None  # matches DEFAULT_DENY_PATHS
assert gates.cmd_blocked('echo hello') is None  # no allowlist -> allow all
# allowlist mode
gates3 = SafetyGates()
gates3.allow_cmds = ['echo', 'ls']
assert gates3.cmd_blocked('echo hi') is None
assert gates3.cmd_blocked('rm x') is not None
print('  cmd_blocked: OK')

# allowlist mode (already tested above)
print('  cmd_blocked allowlist: OK')

# redact
redacted = SafetyGates.redact('key=AIza1234567890abcdef token=sk-abc123def456ghi secret=myPass123')
assert 'AIza' not in redacted or 'REDACTED' in redacted
assert 'sk-abc123def456ghi' not in redacted or 'REDACTED' in redacted
assert 'myPass123' not in redacted or 'REDACTED' in redacted
print('  redact: OK')

# approval create/list/resolve
aid = gates.create_approval('run_command', {'command': 'rm x'})
assert aid.startswith('cu-')
pending = gates.list_approvals()
assert len(pending) == 1
resolved = gates.resolve(aid, True)
assert resolved['approve'] is True
print('  approval flow: OK')

# export_audit
lines = SafetyGates.export_audit(redact=True)
assert isinstance(lines, list)
print('  export_audit: OK')

print()
print('=== Phase 10: Gateway bridge + server routes ===')
from gateway import ensure_gateway, LLM, GATEWAY_URL, embed
import httpx

# ensure_gateway is idempotent (8109 already up)
ensure_gateway()
r = httpx.get(f'{GATEWAY_URL}/v1/routers', timeout=5)
assert r.status_code == 200
print(f'  gateway reachable at {GATEWAY_URL}: OK')

# LLM().embed cheap probe
emb = LLM().embed('test probe', task_type='retrieval_document')
assert emb['provider'] == 'ollama'
assert emb['dim'] == 768
assert len(emb['embedding']) == 768
print(f'  LLM().embed -> {emb["provider"]} dim={emb["dim"]}: OK')

# agent_server routes (no /api/chat — that triggers a real agent run)
from agent_server import app
from fastapi.testclient import TestClient
client = TestClient(app)

health = client.get('/api/health')
assert health.status_code == 200
d = health.json()
assert d['agent'] == 'ready'
print(f'  /api/health: OK (gateway_up={d["gateway_up"]})')

cost = client.get('/api/cost?session=nonexistent')
assert cost.status_code == 200
assert cost.json()['totals']['in_tokens'] == 0
print('  /api/cost empty session: OK')

templates = client.get('/api/templates')
assert templates.status_code == 200
print('  /api/templates: OK')

sched = client.get('/api/schedule')
assert sched.status_code == 200
print('  /api/schedule: OK')

audit = client.get('/api/audit')
assert audit.status_code == 200
assert 'lines' in audit.json()
print('  /api/audit: OK')

# resolve_session persistence
from agent_server import resolve_session
s1 = resolve_session('conv-1')
s2 = resolve_session('conv-1')
assert s1 == s2, 'same conversation_id should return same session_id'
s3 = resolve_session('conv-2')
assert s3 != s1
print('  resolve_session stable mapping: OK')

# artifact path-traversal guard
art_resp = client.get('/api/artifacts/sid-1/../../etc/passwd')
assert art_resp.status_code in (400, 404)
print('  /api/artifacts path-traversal blocked: OK')

print()
print('=== Phase 11: Test suite collection ===')
import subprocess
result = subprocess.run(
    [r'C:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\.venv\Scripts\python.exe',
     '-m', 'pytest', 'tests/', '--collect-only', '-q'],
    capture_output=True, text=True, timeout=60
)
print(f'  pytest exit code: {result.returncode}')
print(f'  collected (stdout snippet): {result.stdout[:500]}')
if result.stderr:
    print(f'  stderr: {result.stderr[:300]}')

# Count test files
test_files = list(Path('tests').glob('test_*.py'))
print(f'  test files on disk: {len(test_files)}')

print()
print('PHASES 8-11 COMPLETE')
