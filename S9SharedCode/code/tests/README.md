# Tests (`tests/`)

The agent's Pytest suite. Run from `S9SharedCode/code` with the project interpreter
(**not** bare `uv run pytest`, which resolves the wrong interpreter and fails on
`numpy`):

```
uv run python -m pytest tests/ -q          # full suite ~5–6 min
uv run python -m pytest tests/test_recovery.py -q   # targeted file is fine
```

`TEST.md` (one level up) is the Studio verification checklist.

## What is covered (representative groups)
- **Orchestration / DAG** — `test_recovery*`, `test_recovery_splice`, `test_upstream_recovery`, `test_planner_*`, `test_stop_in_nodes`, `test_idempotency_release`, `test_sandbox_needs_coder`.
- **Skills / authoring** — `test_authoring_*`, `test_json_repair`, `test_sectioned_*`, `test_length_contract`, `test_no_hallucinated_artifacts`, `test_prompt_truth`, `test_mcp_tool_reachability`.
- **Channels / chat** — `test_channel_*`, `test_action_*`, `test_telegram_documents`, `test_chat_*`.
- **Computer use** — `test_computer_*`, `test_computer_open_app`, `test_computer_use*`, `test_private_desktop`.
- **Browser** — `test_browser_skill`, `test_natural_vision_search`.
- **Studio / code** — `test_studio_*`, `test_code_*`, `test_coding_agent_loop`.
- **Memory / documents** — `test_memory_*`, `test_document_*`, `test_documents_*`, `test_artifact_*`.
- **Scheduler / notify** — `test_scheduler*`, `test_self_wake`, `test_notify_*`, `test_reminder_*`.
- **Security / hardening** — `test_prompt_injection`, `test_auth_guard`, `test_audit_*`, `test_hardening_round`, `test_system_audit`.
- **E2E** — `test_e2e_*`, `e2e_harness.py`, `live_agent_probe.py`, `comprehensive/`.

## Notes
- `conftest.py` holds shared fixtures; `battle_test*.py` and `live_agent_probe.py`
  are live probes (need the services up).
- The suite is isolation-guarded (`test_suite_isolation_guard.py`) — keep new tests
  from leaking state between runs.