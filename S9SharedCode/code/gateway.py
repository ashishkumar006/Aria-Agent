"""Bridge to llm_gatewayV9.

V9 is V8 plus two things: (1) `/v1/vision` — typed shim for single-image
vision calls that the Browser skill's Layer-3 driver hits; (2) per-agent
USD pricing on `/v1/cost/by_agent` so the ledger surfaces dollars in
addition to tokens. V8's `agent` tagging, `/v1/chat/batch`, and retry-
on-5xx carry forward unchanged.

The session-version mapping (V9 for Session 9) lets V8 stay frozen for
Session 8.

Auto-starts the gateway on port 8109 if it is not already up, then
re-exports the V9 `LLM` client and a module-level `embed()` helper.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx

GATEWAY_V9_DIR = Path(__file__).resolve().parents[2] / "llm_gatewayV9"
# 127.0.0.1, NOT "localhost". On this box `localhost` resolves to ::1 first
# and the gateway binds IPv4 only, so every call paid a refused IPv6 connect
# before falling back — measured 2591ms via httpx against 1034ms for the
# literal address. On a memory-panel request that is paid twice (liveness
# probe + the real call).
GATEWAY_URL = "http://127.0.0.1:8109"

# One pooled, keep-alive client for the whole process. `httpx.get(...)` builds
# a Client per call, which constructs an SSL context and re-reads the CA
# bundle each time — ~1s of pure setup on this machine, on top of a fresh TCP
# handshake. Measured: 1034ms for a pooled-client request against ~10ms from
# a client that already had a connection.
_CLIENT: "httpx.Client | None" = None


def _client() -> "httpx.Client":
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(timeout=30.0, follow_redirects=True,
                               limits=httpx.Limits(max_keepalive_connections=4,
                                                   max_connections=16))
    return _CLIENT


def _is_up() -> bool:
    try:
        r = _client().get(f"{GATEWAY_URL}/v1/routers", timeout=2.0)
        r.raise_for_status()
        return True
    except Exception:
        return False


def ensure_gateway() -> None:
    """Start V9 if it is not already running. Idempotent."""
    if _is_up():
        return
    if not GATEWAY_V9_DIR.exists():
        raise RuntimeError(
            f"Gateway V9 directory not found at {GATEWAY_V9_DIR}. "
            "Build llm_gatewayV9 (Session 9 prerequisite) before running S9 code."
        )
    print(f"[gateway] launching llm_gatewayV9 from {GATEWAY_V9_DIR}")
    subprocess.Popen(
        ["uv", "run", "main.py"],
        cwd=str(GATEWAY_V9_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(45):
        time.sleep(1)
        if _is_up():
            print(f"[gateway] up on {GATEWAY_URL}")
            return
    raise RuntimeError(f"Gateway V9 failed to start within 45s. Check {GATEWAY_V9_DIR}")


# Load V9's client.py without polluting sys.path. The gateway dir has its
# own `schemas.py`, which would shadow ours if we put it on the path.
import importlib.util as _importlib_util

_client_path = GATEWAY_V9_DIR / "client.py"
if _client_path.exists():
    _spec = _importlib_util.spec_from_file_location("llm_gatewayV9_client", _client_path)
    _mod = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _RawLLM = _mod.LLM
else:
    _RawLLM = None  # populated once V9 is built; importers should ensure_gateway() first


# ── Routing wrapper ─────────────────────────────────────────────────────────
# S9 no longer forces a single provider/model. Every call passes through to
# the gateway's NATURAL routing system (router.order / agent_routing.yaml /
# MODEL_ROUTES / auto_route). The gateway's failover ladder stays intact and
# picks the best available backend per request. Callers may still pin
# `provider`/`model`/`agent`/`auto_route` explicitly when they want to.
#
# Optional env knobs (only applied when set) let an operator bias the default
# without hard-coding it: S9_LLM_PROVIDER / S9_LLM_MODEL. When unset, the
# gateway decides.
_DEFAULT_PROVIDER = os.getenv("S9_LLM_PROVIDER") or None
_DEFAULT_MODEL = os.getenv("S9_LLM_MODEL") or None


class _RoutedLLM:
    """Thin proxy over the V9 LLM client. Does NOT force routing — it lets
    the gateway's natural routing decide unless the caller explicitly pins
    provider/model/agent/auto_route."""

    def __init__(self, base_url: str = GATEWAY_URL, timeout: float = 600):
        if _RawLLM is None:
            raise RuntimeError(
                "Gateway V9 client unavailable. Confirm llm_gatewayV9/client.py exists."
            )
        self._raw = _RawLLM(base_url=base_url, timeout=timeout)

    def chat(self, prompt: str = None, *, messages=None, system=None,
             provider: str = None, model: str = None, max_tokens=2048,
             temperature=0.7, tools=None, tool_choice=None, cache_system=None,
             reasoning=None, response_format=None, auto_route=None,
             agent=None, session=None) -> dict:
        # Only apply the optional env bias when the caller did not pin anything.
        provider = provider or _DEFAULT_PROVIDER
        model = model or _DEFAULT_MODEL
        return self._raw.chat(
            prompt=prompt, messages=messages, system=system,
            provider=provider, model=model, max_tokens=max_tokens,
            temperature=temperature, tools=tools, tool_choice=tool_choice,
            cache_system=cache_system, reasoning=reasoning,
            response_format=response_format, auto_route=auto_route,
            agent=agent, session=session,
        )

    def vision(self, image, prompt, *, system=None, provider: str = None,
               model: str = None, max_tokens=800, temperature: float = 0.0,
               agent=None, session=None, response_format=None) -> dict:
        # The V9 client exposes vision via /v1/vision; let the gateway route
        # it naturally unless the caller pinned provider/model.
        # VisionRequest has `schema`/`schema_name` (NOT `response_format`),
        # so translate a json_schema response_format instead of sending a
        # key the server silently drops.
        provider = provider or _DEFAULT_PROVIDER
        model = model or _DEFAULT_MODEL
        body = {
            "image": image, "prompt": prompt, "max_tokens": max_tokens,
            "temperature": temperature, "agent": agent, "session": session,
            "provider": provider, "model": model,
        }
        if system:
            body["system"] = system
        if isinstance(response_format, dict):
            _rf_schema = (response_format.get("schema")
                          or (response_format.get("json_schema") or {}).get("schema"))
            if _rf_schema:
                body["schema"] = _rf_schema
                body["schema_name"] = response_format.get("name", "out")
        import httpx as _httpx
        r = _httpx.post(f"{self._raw.base_url}/v1/vision", json=body,
                        timeout=self._raw.timeout)
        r.raise_for_status()
        return r.json()

    def embed(self, text: str, task_type: str = "retrieval_document",
              provider: str = None, agent: str = None, session: str = None) -> dict:
        return self._raw.embed(text, task_type=task_type, provider=provider,
                               agent=agent, session=session)

    def chat_batch(self, calls: list[dict], max_concurrency: int = 4) -> list[dict]:
        # Do not pin provider/model on batched calls — let the gateway route
        # each one naturally. Only apply the optional env bias if a call has
        # neither set.
        for c in calls:
            c.setdefault("provider", _DEFAULT_PROVIDER)
            c.setdefault("model", _DEFAULT_MODEL)
        return self._raw.chat_batch(calls, max_concurrency=max_concurrency)

    def capabilities(self):
        return self._raw.capabilities()

    def cost_by_agent(self, session=None, agent=None) -> dict:
        return self._raw.cost_by_agent(session=session, agent=agent)


# Public handle used across S9: `from gateway import LLM; LLM().chat(...)`.
LLM = _RoutedLLM


def embed(text: str, task_type: str = "retrieval_document",
          agent: str | None = None, session: str | None = None) -> dict:
    """Compute an embedding for `text` via the gateway's embed endpoint."""
    ensure_gateway()
    return LLM().embed(text, task_type=task_type, agent=agent, session=session)


__all__ = ["ensure_gateway", "LLM", "GATEWAY_URL", "GATEWAY_V9_DIR", "embed"]
