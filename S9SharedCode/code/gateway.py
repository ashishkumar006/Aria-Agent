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
GATEWAY_URL = "http://localhost:8109"


def _is_up() -> bool:
    try:
        httpx.get(f"{GATEWAY_URL}/v1/routers", timeout=2.0)
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
               model: str = None, max_tokens=800, temperature=0.0,
               agent=None, session=None, response_format=None) -> dict:
        # The V9 client exposes vision via /v1/vision; let the gateway route
        # it naturally unless the caller pinned provider/model.
        provider = provider or _DEFAULT_PROVIDER
        model = model or _DEFAULT_MODEL
        body = {
            "image": image, "prompt": prompt, "max_tokens": max_tokens,
            "temperature": temperature, "agent": agent, "session": session,
            "provider": provider, "model": model,
        }
        if system:
            body["system"] = system
        if response_format:
            body["response_format"] = response_format
        import httpx as _httpx
        r = _httpx.post(f"{self._raw.base_url}/v1/vision", json=body,
                        timeout=self._raw.timeout)
        r.raise_for_status()
        return r.json()

    def embed(self, text: str, task_type: str = "retrieval_document",
              provider: str = None) -> dict:
        return self._raw.embed(text, task_type=task_type, provider=provider)

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

    def cost_by_agent(self, session=None) -> dict:
        return self._raw.cost_by_agent(session=session)


# Public handle used across S9: `from gateway import LLM; LLM().chat(...)`.
LLM = _RoutedLLM


def embed(text: str, task_type: str = "retrieval_document") -> dict:
    """Compute an embedding for `text` via the gateway's embed endpoint."""
    ensure_gateway()
    return LLM().embed(text, task_type=task_type)


__all__ = ["ensure_gateway", "LLM", "GATEWAY_URL", "GATEWAY_V9_DIR", "embed"]
