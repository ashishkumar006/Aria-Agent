import os, time, json
import sys
from pathlib import Path
from typing import Any, Optional
from contextlib import asynccontextmanager

# Stdio is UTF-8, unconditionally. Dozens of `print()` calls across the
# gateway log user-influenced content (descriptors, errors, doc ids); on a
# Windows cp1252 locale the FIRST non-ASCII character (an arrow, emoji,
# CJK) crashed the request with UnicodeEncodeError — e.g. POST
# /v1/memory/remember 503'd on a descriptor containing →. This one
# reconfigure removes the entire bug class (same fix as the MCP server).
for _s in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
del _s

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).parent
# Load the gateway's own .env first (it carries the provider keys: GEMINI_API_KEY,
# NVIDIA_API_KEY, etc.). Then load the parent project .env as a non-overriding
# fallback so any shared vars still apply without clobbering the local keys.
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env")

import db
import providers as P
from router import Router, RouterPool, DEFAULT_ROUTER_ORDER, LIMITS, SHORTCUTS, resolve, limits_for, expand_order
from cache import GeminiCache
from schemas import (ChatRequest, ChatResponse, ToolCall, RouterDecision,
                     EmbedRequest, EmbedResponse, EmbedBatchRequest,
                     BatchChatRequest, VisionRequest, ResponseFormat)
import embedders as E

# Document indexing batch width. 16 measured at ~3.2 chunks/s (311ms/chunk)
# against ~0.45 chunks/s one call at a time; 32 reaches ~4.0/s but leaves less
# headroom for memory recall to interleave, so 16 is the default and 32 is
# available by configuration.
_DOC_BATCH_DEFAULT = max(1, min(32, int(
    os.getenv("DOCUMENT_EMBED_BATCH", "16") or 16)))
_DOC_BATCH_MAX = 32
from channels_api import router as channels_router
from voice_api import router as voice_router
from integrations_api import router as integrations_router
from memory_api import router as memory_router

DEFAULT_ORDER = ["gemini35lite", "gemini", "nvidia", "groq", "cerebras", "openrouter", "github", "kilo"]
ORDER = [x.strip() for x in os.getenv("LLM_ORDER", ",".join(DEFAULT_ORDER)).split(",") if x.strip()]
ROUTER_ORDER = [x.strip() for x in os.getenv("ROUTER_ORDER", ",".join(DEFAULT_ROUTER_ORDER)).split(",") if x.strip()]
# NOTE (M1): only GATEWAY_V9_PORT is honoured. A stale GATEWAY_V3_PORT=8101
# line from the V3 era must NOT move V9 off 8109 (every client — agent
# gateway.py, dashboards, tests — targets :8109), so it is deliberately
# ignored here and should be deleted from llm_gatewayV9/.env.
PORT = int(os.getenv("GATEWAY_V9_PORT", "8109"))

# V8: agent_routing.yaml maps `agent="<name>"` to a preferred provider name.
# The caller's explicit `provider=` still wins. Loaded once at import; the
# file is small enough (~10 lines) that hot-reloading would be over-engineering.
import yaml
_AGENT_ROUTING_PATH = ROOT / "agent_routing.yaml"
AGENT_ROUTING: dict[str, str] = {}
if _AGENT_ROUTING_PATH.exists():
    try:
        AGENT_ROUTING = yaml.safe_load(_AGENT_ROUTING_PATH.read_text()) or {}
    except Exception as e:  # pragma: no cover - logged then ignored
        print(f"[gateway] failed to parse agent_routing.yaml: {e!r}")
        AGENT_ROUTING = {}

# Tier -> worker failover order. TINY prefers small fast workers; LARGE prefers
# long-context Gemini; HUGE is rejected (no summarizer agent exists yet).
TIER_TO_ORDER = {
    "TINY":  ["github", "openrouter", "groq", "nvidia", "cerebras", "gemini35lite", "gemini", "kilo"],
    "LARGE": ["gemini35lite", "gemini", "groq", "nvidia", "cerebras", "github", "openrouter", "kilo"],
}

# Router envelope: cap the sample at ~800 chars (first 400 + last 400).
# Keeps router input under 400 tokens regardless of worker payload size, so
# routing decisions never burn through router quota on big prompts.
ROUTER_SAMPLE_HEAD = 400
ROUTER_SAMPLE_TAIL = 400
ROUTER_PROMPT = (
    "You are a routing classifier. Given a token_count and a content sample, "
    "output exactly one of: TINY, LARGE, or HUGE.\n\n"
    "Rules:\n"
    "- TINY: token_count below 1000 with simple factual content.\n"
    "- LARGE: token_count between 1000 and 8000, OR token_count below 1000 "
    "but content is dense (code, base64, multilingual, technical).\n"
    "- HUGE: token_count above 8000.\n\n"
    "Output the single word and nothing else."
)


def _estimate_tokens(text: str) -> int:
    """words * 1.4 — deliberately rough. The router sample handles the cases
    where rough isn't good enough (code, CJK, base64)."""
    return int(len(text.split()) * 1.4)


def _build_sample(text: str) -> str:
    if len(text) <= ROUTER_SAMPLE_HEAD + ROUTER_SAMPLE_TAIL + 10:
        return text
    return text[:ROUTER_SAMPLE_HEAD] + "\n...\n" + text[-ROUTER_SAMPLE_TAIL:]


def _tier_from_count(tokens: int) -> str:
    """Deterministic fallback when the router LLM is unreachable or replies
    with garbage. Pure token-count rule, identical thresholds."""
    if tokens > 8000:
        return "HUGE"
    if tokens >= 1000:
        return "LARGE"
    return "TINY"


def _parse_tier(text: str) -> Optional[str]:
    up = (text or "").upper()
    for tier in ("HUGE", "LARGE", "TINY"):
        if tier in up:
            return tier
    return None


async def _classify_tier(req: ChatRequest, role: str, router_pool: RouterPool, prompt_text: str):
    """Run a router-LLM classification. Returns a RouterDecision (without
    chosen_worker_* fields, which are filled in by the caller after worker pick).

    Failover: try each router provider in order. Only fall back to the
    pure token-count rule when all routers in the pool have failed.
    """
    estimated = _estimate_tokens(prompt_text)

    # Short-circuit HUGE — no need to spend a router call.
    if estimated > 8000:
        return RouterDecision(
            role=role, tier="HUGE", estimated_tokens=estimated,
            router_provider="(skipped)", router_model="(skipped)",
            router_latency_ms=0, fallback_used=True,
        )

    sample = _build_sample(prompt_text)
    envelope = f"token_count: {estimated}\nsample:\n{sample}"
    call_role = f"router_{role}"

    last_provider = ""
    last_model = ""
    last_latency = 0

    for name in router_pool.candidates():
        ok, why = router_pool.state[name].can_use(limits_for(name), 400)
        if not ok:
            continue
        provider = router_pool.providers[name]
        t0 = time.time()
        router_pool.state[name].record(0)
        last_provider = name
        last_model = provider.model
        try:
            result = await provider.chat(
                messages=[{"role": "user", "content": envelope}],
                system_blocks=ROUTER_PROMPT,
                max_tokens=8, temperature=0,
                model=None, tools=None, tool_choice=None,
                reasoning="off", response_format=None,
                cache_system=False,
            )
            latency = int((time.time() - t0) * 1000)
            last_latency = latency
            tokens = (result.get("input_tokens") or 0) + (result.get("output_tokens") or 0)
            router_pool.state[name].tokens_today += tokens
            router_pool.state[name].tokens_minute.append((time.time(), tokens))
            tier = _parse_tier(result.get("text", ""))
            # Sanity clamp: HUGE is only valid when the deterministic count
            # agrees. Small router LLMs occasionally hallucinate HUGE on small
            # inputs that look "dense" (URLs, JSON brackets, code fragments).
            # The 8000-token ceiling is hard — override the LLM here to keep
            # the request servable.
            if tier == "HUGE" and estimated <= 8000:
                tier = "LARGE"
            if tier is None:
                # Router returned text we couldn't classify — try the next router
                # rather than giving up immediately. Log this attempt as a soft
                # failure with the actual response captured.
                db.log_call(provider=name, model=result.get("model", provider.model),
                            input_tokens=result.get("input_tokens", 0),
                            output_tokens=result.get("output_tokens", 0),
                            latency_ms=latency, status="error",
                            error=f"unparseable tier reply: {result.get('text','')[:100]}",
                            prompt_chars=len(envelope),
                            call_role=call_role, router_decision="unparseable",
                            agent=req.agent, session=req.session)
                continue
            db.log_call(provider=name, model=result.get("model", provider.model),
                        input_tokens=result.get("input_tokens", 0),
                        output_tokens=result.get("output_tokens", 0),
                        latency_ms=latency, status="ok",
                        prompt_chars=len(envelope), response_chars=len(result.get("text", "")),
                        call_role=call_role, router_decision=tier,
                        agent=req.agent, session=req.session)
            return RouterDecision(
                role=role, tier=tier, estimated_tokens=estimated,
                router_provider=name, router_model=result.get("model", provider.model),
                router_latency_ms=latency, fallback_used=False,
            )
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            last_latency = latency
            db.log_call(provider=name, model=provider.model,
                        status="error", error=str(e)[:500],
                        latency_ms=latency, call_role=call_role,
                        router_decision="error",
                        agent=req.agent, session=req.session)
            # Back off hard-failing routers (dead model, dead account):
            # without this a permanently-broken router is retried first on
            # EVERY classify (it's first in pool order). Transient blips
            # cost at most 5 minutes of pool absence; the pool has spares.
            router_pool.state[name].mark_unavailable(
                300, f"router error: {str(e)[:80]}")
            # Move on to the next router.
            continue

    # All routers in the pool failed — deterministic token-count fallback.
    return RouterDecision(
        role=role, tier=_tier_from_count(estimated), estimated_tokens=estimated,
        router_provider=last_provider or "(unavailable)",
        router_model=last_model or "(unavailable)",
        router_latency_ms=last_latency, fallback_used=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    app.state.cache = GeminiCache(ttl_seconds=300)
    app.state.providers = P.build_providers(app.state.cache)
    # Gemini-only mode (GATEWAY_GEMINI_ONLY=true): the gateway touches
    # Gemini keys and nothing else. Router pool empties → tier
    # classification uses the deterministic fallback (see _classify_tier).
    app.state.gemini_only = os.getenv("GATEWAY_GEMINI_ONLY", "false").lower() in ("1", "true", "yes")
    if app.state.gemini_only:
        app.state.providers = P.keep_gemini_only(app.state.providers)
        app.state.router_providers = {}
    else:
        app.state.router_providers = P.build_router_providers()
    # Key-pool siblings (gemini-2, …) slot in right after their canonical
    # member so one key's 429/cooldown fails over to the next key.
    app.state.router = Router(app.state.providers, expand_order(ORDER, app.state.providers))
    app.state.router_pool = RouterPool(app.state.router_providers,
                                       expand_order(ROUTER_ORDER, app.state.router_providers))
    app.state.embedders, app.state.embed_order = E.build_embedders()
    yield


app = FastAPI(title="LLM Gateway V9", lifespan=lifespan,
              redirect_slashes=False)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
# V10 adaptor plane (channels/hooks/policy/spend/control) + voice services
# + keyed third-party integrations (ex-agent Tier-1 tools).
app.include_router(channels_router)
app.include_router(voice_router)
app.include_router(integrations_router)
app.include_router(memory_router)
from deploy_api import router as deploy_router
app.include_router(deploy_router)


def _normalize_messages(req: ChatRequest):
    if req.messages:
        return list(req.messages)
    msgs = []
    msgs.append({"role": "user", "content": req.prompt or ""})
    return msgs


def _has_real_turn(messages) -> bool:
    """Does this conversation contain anything to answer?

    An empty `messages: []` (or a lone empty system turn) used to reach the
    provider and bill a real LLM call, which then confidently invented an
    answer to a question nobody asked — observed live answering "how do I set
    up a weekly review?" to an empty request. That is a cost bug and a
    correctness bug, so it is refused before routing.
    """
    for m in messages or []:
        role = (m.get("role") or "").lower()
        if role in ("system", "developer"):
            continue          # instructions, not something to answer
        content = m.get("content")
        if isinstance(content, list):
            has_text = any(
                isinstance(b, dict) and str(b.get("text") or "").strip()
                for b in content)
            if has_text:
                return True
            continue
        if str(content or "").strip():
            return True
    return False


def _system_blocks(req: ChatRequest):
    """Returns the system_blocks payload to hand to the provider adapter."""
    if req.system is None:
        return None
    if isinstance(req.system, str):
        if req.cache_system:
            return [{"text": req.system, "cache": True}]
        return req.system
    return [b.model_dump() if hasattr(b, "model_dump") else b for b in req.system]


def _est_tokens(messages, system_blocks, max_tokens):
    chars = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, list):
            chars += len(P._extract_text_blocks(c))
            # V9: image blocks count as ~258 tokens each on Gemini, ~85 base
            # tokens on OpenAI; use 300 chars per image as a coarse estimate
            # (gets multiplied by ~0.25 in chars→tokens below).
            chars += 1200 * sum(1 for b in c if isinstance(b, dict) and b.get("type") in ("image_url", "image", "input_image"))
        else:
            chars += len(str(c))
    if isinstance(system_blocks, str):
        chars += len(system_blocks)
    elif isinstance(system_blocks, list):
        for b in system_blocks:
            chars += len(b.get("text", "") if isinstance(b, dict) else "")
    return chars // 4 + min(max_tokens, 8192)


def _backoff_for(err: Exception, has_model_override: bool = False):
    msg = str(err).lower()
    status = getattr(err, "status", None)
    if status == 429:
        if "queue" in msg: return 15, "server queue full"
        if "quota" in msg or "rpm" in msg or "per minute" in msg: return 60, "RPM quota burned"
        if "rpd" in msg or "per day" in msg or "daily" in msg: return 3600, "RPD quota burned"
        return 30, "rate limited"
    if status and 500 <= status < 600: return 20, f"upstream {status}"
    if status == 408 or "timeout" in msg: return 10, "timeout"
    if status == 402:
        # Payment/quota required (observed live: entire Cerebras account
        # 402s on every model). Back off hard instead of burning a round
        # trip on every pick; billing fixes revive it with no code change.
        return 300, "payment required (quota/billing)"
    if status in (401, 403):
        # When the caller explicitly picked a model, 403/404 likely means
        # "this model not available to your account" rather than "key dead".
        # Don't blackball the whole provider for 10 minutes.
        if has_model_override:
            return 0, ""
        return 600, "auth error"
    if status == 404 and has_model_override:
        return 0, ""
    return 0, ""


def _attempts_str(attempts):
    return "; ".join(f"{a['provider']}:{a['reason']}" for a in attempts)


def _required_caps(req: ChatRequest):
    caps = []
    if req.tools: caps.append("tools")
    if req.reasoning and req.reasoning != "off": caps.append("reasoning")
    if req.response_format: caps.append("structured")
    # V9: auto-detect multimodal content. If any message carries image blocks,
    # only providers whose configured model supports vision are eligible.
    if req.messages:
        for m in req.messages:
            if P._content_has_image(m.get("content")):
                caps.append("vision")
                break
    return caps


async def _resolve_image_urls(messages: list[dict]) -> list[dict]:
    """V9: fetch any http(s) image URLs in message content and inline them as
    data: URLs. Providers downstream only ever see data: URLs, which keeps
    Gemini/Ollama translation paths simple. Mutates a copy; original is intact.
    """
    import base64
    import httpx as _httpx

    async def _fetch_to_data_url(url: str) -> str:
        # A real-browser UA — Wikimedia and many CDNs refuse python-default UAs.
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; LLMGatewayV9/0.1; +image-resolver)",
            "Accept": "image/*,*/*;q=0.8",
        }
        async with _httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as c:
            try:
                r = await c.get(url)
                r.raise_for_status()
            except _httpx.HTTPError as e:
                raise HTTPException(400, f"failed to fetch image url {url!r}: {e}")
            mt = (r.headers.get("content-type") or "image/png").split(";")[0].strip()
            b64 = base64.b64encode(r.content).decode()
            return f"data:{mt};base64,{b64}"

    out = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        new_blocks = []
        changed = False
        for b in content:
            if isinstance(b, dict) and b.get("type") == "image_url":
                iu = b.get("image_url")
                url = iu.get("url") if isinstance(iu, dict) else iu
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    data_url = await _fetch_to_data_url(url)
                    new_blocks.append({"type": "image_url", "image_url": {"url": data_url}})
                    changed = True
                    continue
            new_blocks.append(b)
        if changed:
            new_m = dict(m)
            new_m["content"] = new_blocks
            out.append(new_m)
        else:
            out.append(m)
    return out


def _validate_structured(text: str, schema: dict):
    try:
        obj = json.loads(text)
    except Exception as e:
        raise ValueError(f"output is not JSON: {e}")
    Draft202012Validator(schema).validate(obj)
    return obj


@app.post("/v1/chat")
async def chat(req: ChatRequest):
    router = app.state.router
    router_pool = app.state.router_pool
    messages = _normalize_messages(req)
    if not _has_real_turn(messages):
        raise HTTPException(400, "no user message to answer")
    # V9: pre-resolve any http(s) image URLs to data: URLs once, centrally.
    # Cheap when there are no images (function is a pass-through).
    if any(P._content_has_image(m.get("content")) for m in messages):
        messages = await _resolve_image_urls(messages)
    system_blocks = _system_blocks(req)
    prompt_text = "".join(
        (P._extract_text_blocks(m.get("content", "")) if isinstance(m.get("content"), list)
         else str(m.get("content", "")))
        for m in messages
    )
    est = _est_tokens(messages, system_blocks, req.max_tokens)
    explicit_override = bool(req.provider)
    required_caps = _required_caps(req)

    # V9: model-name routing. If the caller passes only `model=` (no `provider=`),
    # resolve the provider from the MODEL_ROUTES registry so the gateway uses that
    # specific backend. This lets any service call the gateway with just a model
    # name and have it routed to the right provider (e.g. Kilo-served models).
    if req.model and not req.provider:
        routed = P.MODEL_ROUTES.get(req.model)
        if routed:
            req.provider = routed
            explicit_override = True

    # V8: if the caller tagged the request with an agent name and did not
    # pin a provider explicitly, apply agent_routing.yaml's preferred provider.
    # This mutates req.provider so the rest of the function (router-pick,
    # candidate-narrowing, single-candidate-wait) sees the pin.
    if req.agent and not req.provider:
        pinned = AGENT_ROUTING.get(req.agent)
        if pinned and pinned in router.providers:
            req.provider = pinned
            explicit_override = True

    # V9: Kilo's free models (stepfun/step-3.7-flash:free, tencent/hy3:free)
    # reason by default, which adds a long thinking delay. Unless the caller
    # explicitly asks for reasoning, force it OFF for Kilo so calls return fast.
    # The caller can still pass reasoning="low"|"medium"|"high" to opt in.
    _kilo_resolved = (req.provider == "kilo") or (req.model and P.MODEL_ROUTES.get(req.model) == "kilo")
    if _kilo_resolved and req.reasoning is None:
        req.reasoning = "off"

    # V8: retry-on-5xx with `retries` surfaced in the response. The
    # per-provider failover loop below already rotates providers on
    # ProviderError; this counter exists for the single-provider retry case
    # (mostly meaningful when `provider=` is explicit). One retry, backoff
    # capped at 2s as the spec says. NOTE: `retries` counts same-provider
    # retries only — provider failovers are recorded in `attempted`, not here.
    retries = 0

    # V3: auto_route runs a router-LLM classifier first and uses tier-specific
    # failover order. Explicit `provider` overrides routing (caller knows best).
    router_decision: Optional[RouterDecision] = None
    if req.auto_route and not req.provider:
        router_decision = await _classify_tier(req, req.auto_route, router_pool, prompt_text)
        if router_decision.tier == "HUGE":
            raise HTTPException(
                503,
                {
                    "error": "input exceeds 8000 tokens",
                    "hint": "No summarizer agent exists yet; chunk the input or narrow the query. "
                            "For now, chunk the input or set provider=g explicitly to try Gemini anyway.",
                    "router_decision": router_decision.model_dump(),
                },
            )
        # Replace failover order with the tier-specific one, intersected with
        # what's actually wired in this gateway. Defensive .get: TIER_TO_ORDER
        # only defines TINY/LARGE and HUGE 503s above, but a future tier
        # string must degrade to the default order, never KeyError.
        tier_order = expand_order(TIER_TO_ORDER.get(router_decision.tier, ORDER), router.providers)
        candidates = [p for p in tier_order if p in router.providers]
    else:
        candidates = router.candidates(req.provider) if req.provider else list(router.order)

    if req.provider and not candidates:
        raise HTTPException(400, f"unknown provider '{req.provider}'. Try one of: {list(router.providers)} or shortcuts {list(SHORTCUTS)}")

    all_attempts = []
    last_err = None

    # When explicit provider is requested and the only blocker is cooldown,
    # wait briefly rather than 503-ing — this is what users intuitively expect.
    if explicit_override and len(candidates) == 1:
        import asyncio as _asyncio
        deadline = time.time() + 30
        while time.time() < deadline:
            name, _ = router.pick(est, candidates, required_caps=required_caps)
            if name is not None:
                break
            cd = router.state[candidates[0]].snapshot(limits_for(candidates[0]))["cooldown_remaining"]
            if cd <= 0 or cd > 30:
                break
            await _asyncio.sleep(min(cd + 0.05, 5))

    for _ in range(len(candidates) + 1):
        name, atts = router.pick(est, candidates, required_caps=required_caps)
        all_attempts.extend(atts)
        if name is None:
            break

        provider = router.providers[name]
        t0 = time.time()
        router.state[name].record(0)

        try:
            if req.stream:
                async def gen():
                    try:
                        agg = []
                        async for chunk in provider.stream(messages,
                                                          max_tokens=req.max_tokens,
                                                          temperature=req.temperature,
                                                          model=req.model,
                                                          tools=req.tools,
                                                          tool_choice=req.tool_choice,
                                                          reasoning=req.reasoning,
                                                          response_format=req.response_format,
                                                          system_blocks=system_blocks,
                                                          cache_system=bool(req.cache_system)):
                            agg.append(chunk)
                            if chunk.startswith("[[TOOL_CALL_DELTA]]"):
                                yield f"data: {json.dumps({'provider': name, 'tool_call_delta': chunk[len('[[TOOL_CALL_DELTA]] '):]})}\n\n"
                            else:
                                yield f"data: {json.dumps({'provider': name, 'delta': chunk})}\n\n"
                        text = "".join(agg)
                        latency = int((time.time() - t0) * 1000)
                        # Streaming providers return text only (no usage block),
                        # so estimate tokens the same way the pre-flight does
                        # (chars//4) instead of logging zeros that undercount
                        # cost in by_agent.
                        db.log_call(provider=name, model=req.model or provider.model,
                                    input_tokens=len(prompt_text) // 4,
                                    output_tokens=len(text) // 4,
                                    latency_ms=latency, status="ok",
                                    prompt_chars=len(prompt_text), response_chars=len(text),
                                    override=req.provider, attempted=_attempts_str(all_attempts),
                                    agent=req.agent, session=req.session, retries=retries)
                        yield f"data: {json.dumps({'done': True, 'provider': name})}\n\n"
                    except Exception as e:
                        db.log_call(provider=name, model=req.model or provider.model,
                                    status="error", error=str(e)[:500],
                                    latency_ms=int((time.time() - t0) * 1000),
                                    prompt_chars=len(prompt_text),
                                    override=req.provider, attempted=_attempts_str(all_attempts),
                                    agent=req.agent, session=req.session, retries=retries)
                        yield f"data: {json.dumps({'error': str(e)[:300]})}\n\n"
                return StreamingResponse(gen(), media_type="text/event-stream")

            # V8: one same-provider retry on transient 5xx / timeout before
            # we fall through to the failover loop. Exponential backoff capped
            # at 2s, exactly as the spec says. `retries` is surfaced in the
            # response so the orchestrator's replay can show it.
            try:
                result = await provider.chat(messages,
                                             max_tokens=req.max_tokens,
                                             temperature=req.temperature,
                                             model=req.model,
                                             tools=req.tools,
                                             tool_choice=req.tool_choice,
                                             reasoning=req.reasoning,
                                             response_format=req.response_format,
                                             system_blocks=system_blocks,
                                             cache_system=bool(req.cache_system))
            except (P.ProviderError, Exception) as transient:
                status = getattr(transient, "status", None)
                msg = str(transient).lower()
                retryable = (
                    (status is not None and 500 <= status < 600)
                    or status == 408
                    or "timeout" in msg
                )
                if not retryable:
                    raise
                import asyncio as _a
                await _a.sleep(min(2.0, 0.5 * (2 ** retries)))
                retries += 1
                result = await provider.chat(messages,
                                             max_tokens=req.max_tokens,
                                             temperature=req.temperature,
                                             model=req.model,
                                             tools=req.tools,
                                             tool_choice=req.tool_choice,
                                             reasoning=req.reasoning,
                                             response_format=req.response_format,
                                             system_blocks=system_blocks,
                                             cache_system=bool(req.cache_system))
            latency = int((time.time() - t0) * 1000)

            # Optional: validate structured output and (single) retry on failure.
            parsed = None
            if req.response_format and req.response_format.schema_ and not result["tool_calls"]:
                try:
                    parsed = _validate_structured(result["text"], req.response_format.schema_)
                except (ValueError, ValidationError) as ve:
                    # one corrective retry
                    fix_msgs = list(messages) + [
                        {"role": "assistant", "content": result["text"]},
                        {"role": "user", "content": f"Your previous reply did not match the required JSON schema: {ve}. Reply ONLY with valid JSON conforming to the schema."},
                    ]
                    result = await provider.chat(fix_msgs,
                                                 max_tokens=req.max_tokens,
                                                 temperature=0,
                                                 model=req.model,
                                                 response_format=req.response_format,
                                                 system_blocks=system_blocks,
                                                 cache_system=bool(req.cache_system))
                    try:
                        parsed = _validate_structured(result["text"], req.response_format.schema_)
                    except (ValueError, ValidationError) as ve2:
                        raise HTTPException(503, f"structured output failed validation: {ve2}")

            tokens = (result["input_tokens"] or 0) + (result["output_tokens"] or 0)
            router.state[name].tokens_today += tokens
            router.state[name].tokens_minute.append((time.time(), tokens))
            if router_decision is not None:
                router_decision.chosen_worker_provider = name
                router_decision.chosen_worker_model = result["model"]
            db.log_call(provider=name, model=result["model"],
                        input_tokens=result["input_tokens"], output_tokens=result["output_tokens"],
                        cache_create_tokens=result["cache_creation_input_tokens"],
                        cache_read_tokens=result["cache_read_input_tokens"],
                        latency_ms=latency, status="ok",
                        prompt_chars=len(prompt_text), response_chars=len(result["text"]),
                        override=req.provider, attempted=_attempts_str(all_attempts),
                        tool_calls=len(result["tool_calls"]),
                        reasoning_applied=result["reasoning_applied"],
                        tool_dialect=result["tool_call_dialect"],
                        call_role="worker",
                        router_decision=router_decision.tier if router_decision else None,
                        agent=req.agent, session=req.session, retries=retries)
            # Tool ledger: name every completed model tool call (batch and
            # vision funnel through here too). Names only — never arguments.
            for tc in (result["tool_calls"] or []):
                db.log_tool_use(str((tc or {}).get("name") or "tool"),
                                provider=name, model=result["model"],
                                agent=req.agent, session=req.session,
                                call_role="worker")
            return ChatResponse(
                provider=name,
                model=result["model"],
                text=result["text"],
                tool_calls=[ToolCall(**tc) for tc in result["tool_calls"]],
                stop_reason=result["stop_reason"],
                input_tokens=result["input_tokens"],
                output_tokens=result["output_tokens"],
                cache_creation_input_tokens=result["cache_creation_input_tokens"],
                cache_read_input_tokens=result["cache_read_input_tokens"],
                latency_ms=latency,
                tool_call_dialect=result["tool_call_dialect"],
                reasoning_applied=result["reasoning_applied"],
                parsed=parsed,
                attempted=all_attempts,
                router_decision=router_decision,
                retries=retries,
            ).model_dump()

        except P.ProviderError as e:
            last_err = str(e)
            secs, reason = _backoff_for(e, has_model_override=bool(req.model))
            if secs > 0:
                router.state[name].mark_unavailable(secs, reason)
            db.log_call(provider=name, model=req.model or provider.model,
                        status="error", error=str(e)[:500],
                        latency_ms=int((time.time() - t0) * 1000),
                        prompt_chars=len(prompt_text),
                        override=req.provider, attempted=_attempts_str(all_attempts),
                        agent=req.agent, session=req.session, retries=retries)
            tag = f"failed: {str(e)[:100]}"
            if secs > 0: tag += f" → backoff {secs:.0f}s ({reason})"
            all_attempts.append({"provider": name, "reason": tag})
            if explicit_override or not getattr(e, "retryable", True):
                raise HTTPException(502, f"{name} failed: {e}")
            candidates = [c for c in candidates if c != name]
            continue
        except HTTPException:
            raise
        except Exception as e:
            last_err = str(e)
            secs, reason = _backoff_for(e, has_model_override=bool(req.model))
            if secs > 0:
                router.state[name].mark_unavailable(secs, reason)
            db.log_call(provider=name, model=req.model or provider.model,
                        status="error", error=str(e)[:500],
                        latency_ms=int((time.time() - t0) * 1000),
                        prompt_chars=len(prompt_text),
                        override=req.provider, attempted=_attempts_str(all_attempts),
                        agent=req.agent, session=req.session, retries=retries)
            all_attempts.append({"provider": name, "reason": f"exception: {str(e)[:120]}"})
            if explicit_override:
                raise HTTPException(502, f"{name} failed: {e}")
            candidates = [c for c in candidates if c != name]
            continue

    raise HTTPException(503, f"all providers unavailable. attempts: {all_attempts}. last_error: {last_err}")


# ── V8 additions: batch endpoint and cost-by-agent ────────────────────────────

@app.post("/v1/chat/batch")
async def chat_batch(req: BatchChatRequest):
    """Run N chat requests concurrently with bounded parallelism. The gateway
    manages the rate-limit ladder centrally so callers do not need to open
    their own connection pools. Results are returned IN INPUT ORDER. Each
    inner call goes through the same `/v1/chat` pipeline (agent routing,
    retry, failover, db logging) — this endpoint is sugar on top."""
    import asyncio as _a
    sem = _a.Semaphore(max(1, req.max_concurrency))

    async def _one(call: ChatRequest):
        async with sem:
            try:
                return await chat(call)
            except HTTPException as he:
                return {"error": str(he.detail), "status_code": he.status_code}
            except Exception as e:
                return {"error": str(e)[:400], "status_code": 500}

    results = await _a.gather(*[_one(c) for c in req.calls])
    return {"results": results}


@app.post("/v1/vision")
async def vision(req: VisionRequest):
    """V9: single-image vision call. Thin shim over /v1/chat that:
      - packs `image` + `prompt` into a multimodal user message
      - forces routing to a vision-capable provider (via `vision` cap)
      - optionally enforces a JSON schema for structured output

    Returns the same ChatResponse shape as /v1/chat; if a schema was provided
    the parsed object is in `.parsed`.
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": req.prompt}]
    content.append({"type": "image_url", "image_url": {"url": req.image}})

    inner = ChatRequest(
        messages=[{"role": "user", "content": content}],
        system=req.system,
        provider=req.provider,
        model=req.model,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        response_format=(
            ResponseFormat(type="json_schema", schema=req.schema_, name=req.schema_name, strict=True)
            if req.schema_ else None
        ),
        agent=req.agent,
        session=req.session,
    )
    return await chat(inner)


@app.get("/v1/cost/by_agent")
async def cost_by_agent(session: Optional[str] = None, agent: Optional[str] = None):
    """Per-agent rollup. With ?session=<sid> the rollup is scoped to one
    flow-run; with ?agent=<name> the rollup is scoped to a single agent tag;
    without either, the calendar day. Used by the orchestrator's replay step
    to show how much each skill cost.

    V9: each row now carries a `dollars` field derived from `pricing.py`'s
    table.  $0 for free-tier providers (the course default); accurate-ish
    for paid providers.  Tokens remain the headline number.
    """
    import pricing as _pricing
    raw = db.by_agent(session=session)
    if agent:
        raw = {agent: raw.get(agent, [])}
    out: dict[str, list[dict]] = {}
    for ag, rows in raw.items():
        out[ag] = []
        for r in rows:
            r2 = dict(r)
            r2["dollars"] = _pricing.estimate_usd(
                r["provider"], r.get("in_tok") or 0, r.get("out_tok") or 0
            )
            out[ag].append(r2)
    return out


@app.post("/v1/embed")
async def embed(req: EmbedRequest):
    """Single embed endpoint. Failover ring runs Ollama → configured fallback.
    `provider` pins the choice (returns 502 on failure with no fallback).
    Rejects inputs over MAX_INPUT_CHARS with 413 — caller must chunk."""
    embedders = app.state.embedders
    if not embedders:
        raise HTTPException(503, "no embedding providers configured")

    if len(req.text) > E.MAX_INPUT_CHARS:
        raise HTTPException(
            413,
            f"text is {len(req.text)} chars; embed input is capped at "
            f"{E.MAX_INPUT_CHARS} chars (~{E.MAX_INPUT_CHARS // 4} tokens). "
            f"Chunk the input and embed each chunk.",
        )

    t0 = time.time()
    try:
        name, result, attempts, latency = await E.embed_with_failover(
            embedders, req.text, req.task_type, explicit=req.provider
        )
    except E.EmbedderError as e:
        latency = int((time.time() - t0) * 1000)
        db.log_call(
            provider=req.provider or "embed",
            model="embed",
            status="error",
            error=str(e)[:500],
            latency_ms=latency,
            prompt_chars=len(req.text),
            override=req.provider,
            call_role="embed",
            agent=req.agent, session=req.session,
        )
        if req.provider:
            # Pinned provider: surface upstream status faithfully.
            if e.status == 429:
                raise HTTPException(429, f"{req.provider} rate-limited: {e}")
            if e.status == 400:
                raise HTTPException(400, str(e))
            raise HTTPException(502, f"{req.provider} embed failed: {e}")
        raise HTTPException(503, str(e))

    db.log_call(
        provider=name,
        model=result["model"],
        status="ok",
        latency_ms=latency,
        prompt_chars=len(req.text),
        override=req.provider,
        attempted=_attempts_str(attempts),
        call_role="embed",
        embed_dim=result["dim"],
        agent=req.agent, session=req.session,
    )
    return EmbedResponse(
        provider=name,
        model=result["model"],
        embedding=result["embedding"],
        dim=result["dim"],
        latency_ms=latency,
        attempted=attempts,
    ).model_dump()


@app.post("/v1/embed/batch")
async def embed_batch(req: EmbedBatchRequest):
    """Batch embedding for document chunks.

    `/v1/embed` takes a single string, so indexing a document meant one HTTP
    round trip per chunk. Measured on this machine: one-at-a-time reached
    0.45 chunks/s, while Ollama's native array path (`/api/embed`, which takes
    a list) reached 3.2 chunks/s at batch 16 and 4.0 at 32 - roughly 3x the
    ceiling of plain concurrency, because it amortises per-request overhead
    instead of merely overlapping it.

    Batch size defaults to 16 and may be raised to 32 (DOCUMENT_EMBED_BATCH).
    The response repeats `embed_model` and `embed_dim` for every vector so a
    caller can persist provenance: vectors from different models must never
    be compared against each other.
    """
    embedders = app.state.embedders
    if not embedders:
        raise HTTPException(503, "no embedding providers configured")
    texts = req.texts
    if not texts:
        return {"embeddings": [], "model": "", "dim": 0, "latency_ms": 0,
                "batch_size": 0}

    # One oversized input would fail the whole batch, losing every other
    # chunk with it. Reject it here, naming the position, so the caller can fix
    # that chunk and retry the rest.
    for i, t in enumerate(texts):
        if len(t) > E.MAX_INPUT_CHARS:
            raise HTTPException(413, f"texts[{i}] is {len(t)} chars; the limit "
                                     f"is {E.MAX_INPUT_CHARS}. Split it.")
        if not t.strip():
            raise HTTPException(400, f"texts[{i}] is empty")

    # `0 or 16` would silently ignore an explicit 0, so check for None.
    want = (req.batch_size if req.batch_size is not None
            else _DOC_BATCH_DEFAULT)
    size = max(1, min(int(want), _DOC_BATCH_MAX))
    t0 = time.time()
    out: list[dict] = []
    model = ""
    dim = 0
    for start in range(0, len(texts), size):
        window = texts[start:start + size]
        try:
            result = await E.embed_batch_with_failover(
                embedders, window, req.task_type, explicit=req.provider)
            vecs = result["embeddings"]
            model = result.get("model") or model
            dim = result.get("dim") or dim or (len(vecs[0]) if vecs else 0)
        except E.EmbedderError as e:
            if req.provider:
                raise HTTPException(502, f"{req.provider} batch embed "
                                          f"failed: {e}")
            # Fall back to per-chunk so a single bad input costs one chunk
            # rather than the whole window. Report which index failed.
            vecs = []
            for j, t in enumerate(window):
                try:
                    one = await E.embed_with_failover(
                        embedders, t, req.task_type, explicit=req.provider)
                    vecs.append(one[1]["embedding"])
                    model = model or one[1].get("model", "")
                    dim = dim or one[1].get("dim", 0)
                except Exception:
                    vecs.append(None)          # noqa: E741
        for v in vecs:
            out.append({"embedding": v, "index": start + len(out)})

    latency = int((time.time() - t0) * 1000)
    db.log_call(
        provider="embed_batch",
        model=model or "embed",
        status="ok" if all(o["embedding"] is not None for o in out) else "partial",
        latency_ms=latency,
        prompt_chars=sum(len(t) for t in texts),
        call_role="embed",
        embed_dim=dim,
        agent=req.agent, session=req.session,
    )
    failed = [o["index"] for o in out if o["embedding"] is None]
    return {
        "embeddings": [o["embedding"] for o in out],
        "model": model,
        "embed_model": model,
        "dim": dim,
        "embed_dim": dim,
        "count": len(out),
        "failed_indices": failed,
        "batch_size": size,
        "latency_ms": latency,
    }


@app.get("/v1/embedders")
async def list_embedders():
    return {
        "order": app.state.embed_order,
        "models": {e.name: e.model for e in app.state.embedders},
        "fixed_dim": E.EMBED_DIM,
        "max_input_chars": E.MAX_INPUT_CHARS,
        "backoff_steps_s": E.BACKOFF_STEPS,
        "live": {e.name: e.state.snapshot() for e in app.state.embedders},
        "today": db.aggregate(call_role="embed"),
    }


@app.get("/v1/providers")
async def list_providers():
    r = app.state.router
    return {
        "order": r.order,
        "providers": list(r.providers.keys()),
        "shortcuts": SHORTCUTS,
        "limits": LIMITS,
        "models": {n: p.model for n, p in r.providers.items()},
    }


@app.get("/v1/capabilities")
async def capabilities():
    r = app.state.router
    out = {}
    for name, p in r.providers.items():
        caps = dict(getattr(p, "capabilities", {}))
        # per-model overrides
        caps = P.model_capabilities(name, p.model, caps)
        caps["model"] = p.model
        _lim = limits_for(name)
        caps.update({
            "max_ctx": _lim["max_ctx"],
            "rpm": _lim["rpm"],
            "rpd": _lim["rpd"],
        })
        out[name] = caps
    return out


@app.get("/v1/status")
async def status():
    r = app.state.router
    return {"order": r.order, "live": r.all_status(),
            "today": db.aggregate(call_role="worker"), "limits": LIMITS,
            "gemini_only": bool(getattr(app.state, "gemini_only", False))}


@app.get("/v1/routers")
async def routers():
    """V3: router pool — separate from the worker pool. Shows which router LLMs
    are wired, the failover order, and live rate-state."""
    rp = app.state.router_pool
    return {
        "order": rp.order,
        "providers": list(rp.providers.keys()),
        "models": {n: p.model for n, p in rp.providers.items()},
        "live": rp.all_status(),
        "today": db.aggregate(call_role="router"),
        "limits": {k: limits_for(k) for k in rp.providers},
        "tier_to_order": TIER_TO_ORDER,
    }


@app.get("/v1/calls")
async def calls(limit: int = 100, provider: Optional[str] = None, status: Optional[str] = None):
    return db.recent(limit=limit, provider=provider, status=status)


@app.get("/v1/tools/usage")
async def tools_usage():
    """Tool ledger: per-tool completions (uses, ok/errors, providers,
    agents, last use) since calendar day. Names only — arguments, which
    may carry secrets, are never stored."""
    tools = db.tool_usage()
    return {"tools": tools,
            "total_uses": sum(t["uses"] for t in tools),
            "total_tools": len(tools)}


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(str(ROOT / "static" / "dashboard.html"))


@app.get("/help", response_class=HTMLResponse)
async def help_page():
    return FileResponse(str(ROOT / "static" / "help.html"))


if __name__ == "__main__":
    import os as _os
    import uvicorn
    # Loopback by default (holds secrets + spend). Opt into LAN with
    # GATEWAY_HOST=0.0.0.0 — and then put bearer auth in front.
    _host = _os.environ.get("GATEWAY_HOST", "127.0.0.1").strip() or "127.0.0.1"
    uvicorn.run("main:app", host=_host, port=PORT, reload=False)
