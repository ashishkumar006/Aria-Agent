"""Third-party service integrations, owned by the gateway.

Moved here from the agent's mcp_server.py so credentials live in exactly
one place (llm_gatewayV9/.env). Behavior is verbatim: same fail-soft
{ok: ...} shapes, same APIs. The agent's MCP tools are thin HTTP callers
over POST /v1/integrations/{service}/{op}.

Services with their own channel adaptor (telegram, slack) live in
adaptors/ instead; gmail has BOTH an adaptor (envelopes) and integration
functions (raw tool shapes) sharing the same keys.
"""
from __future__ import annotations

import os


def _need(key: str) -> str:
    v = (os.getenv(key) or "").strip().strip('"').strip("'")
    if not v:
        raise _MissingKey(key)
    return v


class _MissingKey(Exception):
    def __init__(self, key: str):
        super().__init__(f"{key} not set")
        self.key = key


def _fail(key_or_msg: str) -> dict:
    return {"ok": False, "error": key_or_msg if " " in key_or_msg else f"{key_or_msg} not set"}
