# Gateway Tests (`llm_gatewayV9/tests/`)

The gateway's Pytest suite. Run from `llm_gatewayV9` with **this** venv:

```
uv run python -m pytest -q          # ~1.5 min
```

> Run `pytest -q` from `llm_gatewayV9`, **not** `pytest tests/`. 14 of the 16
> adaptor suites (plus `policy/` and `integrations/`) live **outside** `tests/`, so
> `pytest tests/ -q` passes while skipping ~344 tests.

## Coverage (representative)
- **Providers / routing** — `test_all_providers`, `test_route_affinity`, `test_router_backoff`, `test_agent_routing_soft_pin`, `test_kilo_*`, `test_gemini_*`, `test_multi_keys`, `test_credential_skip_rule`.
- **Empty completion / resilience** — `test_empty_completion_failover`, `test_openai_empty_tool_result`, `test_client_error_detail`, `test_context_clamp`.
- **Documents / docgen** — `test_docgen*`, `test_document*`, `test_documents_*`, `test_pdf_*`, `test_docx_visual_content`, `test_deck_preview_fidelity`.
- **Memory** — `test_memory*`, `test_drawers`, `test_recall_embed_budget`, `test_memory_keyword_bm25`.
- **Channels / integrations** — `test_adaptor_contract`, `test_adaptors`, `test_channel_pollers`, `test_telegram_poller`, `test_integration_reads`, `test_notion_create_page`, `test_zoho_*`.
- **Policy / security / cost** — `test_policy_spend_approvals_routes`, `test_security_*`, `test_gateway_auth`, `test_tool_ledger`, `test_by_agent_session_scope`, `test_call_table_tps`.
- **Vision / voice** — `test_vision_endpoint`, `test_vision_smoke`, `test_voice_chunking`, `test_voice_status`.

## Notes
- `conftest.py` holds shared fixtures; `check_free_models.py` /
  `check_kilo_default_live.py` are live probes.
- The adaptor contract suites live under `adaptors/` and `integrations/` — run the
  whole `llm_gatewayV9` tree, not just `tests/`.