import os, time, json
import sys
import asyncio
import re
import hashlib
from collections import OrderedDict
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
import logging

from fastapi import FastAPI, HTTPException, Request

log = logging.getLogger("gateway")
from fastapi.responses import (HTMLResponse, StreamingResponse, FileResponse,
                               JSONResponse)
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
from docgen_api import router as docgen_router
from users_api import router as users_router
from user_store import UserStore
from user_pools import UserPoolManager

# The failover ladder. nvidia/groq/cerebras/openrouter/github were removed at
# the operator's request (see providers.DISABLED_PROVIDERS), so the ladder is
# now Gemini first and Kilo as the cross-vendor backstop - two independent
# vendors, which is what makes a failover worth having.
#
# The filter matters as much as the list: LLM_ORDER may still name a disabled
# provider in the environment, and an entry here that cannot be constructed
# turns into a wasted failover attempt against a provider that was never there.
DEFAULT_ORDER = ["gemini35lite", "gemini", "kilo"]
_ORDER_RAW = [x.strip() for x in
              os.getenv("LLM_ORDER", ",".join(DEFAULT_ORDER)).split(",") if x.strip()]
ORDER = [x for x in _ORDER_RAW if x not in P.DISABLED_PROVIDERS] or list(DEFAULT_ORDER)
_ROUTER_RAW = [x.strip() for x in
               os.getenv("ROUTER_ORDER", ",".join(DEFAULT_ROUTER_ORDER)).split(",")
               if x.strip()]
ROUTER_ORDER = [x for x in _ROUTER_RAW
                if x not in P.DISABLED_PROVIDERS] or list(DEFAULT_ROUTER_ORDER)

# How often to re-read the Kilo catalogue so the model list and the pool's
# default track upstream without a restart. Deliberately slow: it is a UI
# convenience, and every poll is a third-party request.
_KILO_ROSTER_REFRESH_SECONDS = max(
    300.0, float(os.getenv("KILO_ROSTER_REFRESH_SECONDS", "1800") or 1800))
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

# Tier -> worker failover order. Both tiers now run Gemini then Kilo, so a tier
# classification no longer changes WHICH vendors answer - only the order within
# them. The removed providers were the fast/small half of both lists, and they
# are gone from the pool entirely (providers.DISABLED_PROVIDERS), so keeping
# them here would only add attempts that cannot succeed.
TIER_TO_ORDER = {
    "TINY":  ["gemini35lite", "kilo", "gemini"],
    "LARGE": ["gemini", "gemini35lite", "kilo"],
}

# Router envelope: cap the sample at ~800 chars (first 400 + last 400).
# Keeps router input under 400 tokens regardless of worker payload size, so
# routing decisions never burn through router quota on big prompts.
ROUTER_SAMPLE_HEAD = 400
ROUTER_SAMPLE_TAIL = 400
# Below this estimate the deterministic answer is already known and the LLM
# router can only ever agree with it. The router exists to catch the
# "small but dense" case (code, base64) - and the band where that matters
# starts well above this. Asking the router anyway was pure waste: the only
# wired router model (kilo-auto/free) does not follow the instruction, so
# every small request paid 1.5-4s for a reply that was discarded and the
# deterministic rule was used instead (217 failed router calls in 24h, all
# of them `router_memory` on the memory subsystem's small prompts).
ROUTER_MIN_TOKENS = int(os.getenv("GATEWAY_ROUTER_MIN_TOKENS", "512") or 512)
# How many replies a router may mangle before it is benched for a while.
ROUTER_PARSE_STRIKES = 3
ROUTER_BENCH_S = 300
ROUTER_PROMPT = (
    "Classify the size of a request.\n"
    "Reply with ONE word from this list and nothing else: TINY, LARGE, HUGE\n"
    "Do not repeat these instructions. Do not explain. Do not ask a question.\n"
    "Example reply: TINY\n\n"
    "Rules:\n"
    "- TINY: token_count below 1000 with simple factual content.\n"
    "- LARGE: token_count between 1000 and 8000, OR token_count below 1000 "
    "but content is dense (code, base64, multilingual, technical).\n"
    "- HUGE: token_count above 8000.\n\n"
    "Your answer:"
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
    """The tier word in a router reply, or None.

    Exactly one tier word must be present. A reply that names more than one
    ("TINY, LARGE, or HUGE" - the echo the free router model produced) is
    ambiguous, and answering "HUGE" for it was wrong: the old code took the
    first match in a fixed order, so an echoed list was read as HUGE and
    then clamped to LARGE, sending a small request to a big model.
    """
    up = (text or "").upper()
    found = [t for t in ("TINY", "LARGE", "HUGE")
             if re.search(rf"\b{t}\b", up)]
    if len(found) == 1:
        return found[0]
    return None


def _explicit_provider_for(model: str, agent, providers) -> str | None:
    """The provider an EXPLICIT model id should pin, or None to keep routing.

    An explicit id - the console's model picker passed it - is honoured
    through the global table first, then the code-only table even for a
    non-code agent: the user asked for that model, so it must not silently
    drop to the failover ladder. Returned only when that provider is wired,
    so an id whose key is missing falls back instead of 400-ing on an
    unknown provider.
    """
    if not model:
        return None
    routed = P.route_for_model(model, agent) or P.CODE_MODEL_ROUTES.get(model)
    return routed if routed in providers else None


def _red(value) -> str:
    """Redact provider keys/tokens from anything leaving over HTTP."""
    try:
        import redact
        return redact.redact(value)
    except Exception:
        return str(value)


def _bench_router(router_pool, name: str) -> None:
    """Bench a router that answered with something unparseable.

    The strike counter lives on the pool so it survives across calls. After
    `ROUTER_PARSE_STRIKES` mangled replies the provider is marked
    unavailable for `ROUTER_BENCH_S` seconds, and the deterministic
    token-count rule answers in the meantime. Without this a router that
    cannot follow the instruction was retried first on EVERY request.
    """
    strikes = getattr(router_pool, "parse_strikes", None)
    if strikes is None:
        return
    strikes[name] = int(strikes.get(name, 0)) + 1
    if strikes[name] >= ROUTER_PARSE_STRIKES:
        strikes[name] = 0
        try:
            router_pool.state[name].mark_unavailable(
                ROUTER_BENCH_S, "router reply was not parseable")
        except Exception:  # noqa: BLE001 - a bench must never break a call
            pass


async def _classify_tier(req: ChatRequest, role: str, router_pool: RouterPool, prompt_text: str, user_id: str | None = None):
    """Run a router-LLM classification. Returns a RouterDecision (without
    chosen_worker_* fields, which are filled in by the caller after worker pick).

    Failover: try each router provider in order. Only fall back to the
    pure token-count rule when all routers in the pool have failed.
    """
    estimated = _estimate_tokens(prompt_text)

    # Short-circuit HUGE - no need to spend a router call.
    if estimated > 8000:
        return RouterDecision(
            role=role, tier="HUGE", estimated_tokens=estimated,
            router_provider="(skipped)", router_model="(skipped)",
            router_latency_ms=0, fallback_used=True,
        )

    # Small enough that the deterministic rule already answers it. Spending a
    # router call here bought nothing: the reply was discarded and the
    # token-count rule decided anyway, at the cost of 1.5-4s per call.
    if estimated < ROUTER_MIN_TOKENS:
        return RouterDecision(
            role=role, tier=_tier_from_count(estimated),
            estimated_tokens=estimated,
            router_provider="(skipped: small)", router_model="(skipped: small)",
            router_latency_ms=0, fallback_used=True,
        )

    # Memoized tier: the same prompt text maps to the same tier, so skip the
    # router-LLM round-trip on repeat prompts.
    cached = _tier_cached(role, prompt_text)
    if cached:
        return RouterDecision(
            role=role, tier=cached, estimated_tokens=estimated,
            router_provider="(cache)", router_model="(cache)",
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
                            agent=req.agent, session=req.session, user_id=user_id)
                # A router that cannot follow the instruction must not be
                # asked again on the NEXT request: it was tried first (and
                # answered wrongly) on every single memory operation - 217
                # wasted calls in 24h, 1.5-4s each. After a few mangled
                # replies it is benched, and the deterministic rule answers
                # until the bench expires.
                _bench_router(router_pool, name)
                continue
            db.log_call(provider=name, model=result.get("model", provider.model),
                        input_tokens=result.get("input_tokens", 0),
                        output_tokens=result.get("output_tokens", 0),
                        latency_ms=latency, status="ok",
                        prompt_chars=len(envelope), response_chars=len(result.get("text", "")),
                        call_role=call_role, router_decision=tier,
                        agent=req.agent, session=req.session, user_id=user_id)
            _tier_store(role, prompt_text, tier)
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
                        agent=req.agent, session=req.session, user_id=user_id)
            # Back off hard-failing routers (dead model, dead account):
            # without this a permanently-broken router is retried first on
            # EVERY classify (it's first in pool order). Transient blips
            # cost at most 5 minutes of pool absence; the pool has spares.
            router_pool.state[name].mark_unavailable(
                300, f"router error: {str(e)[:80]}")
            # Move on to the next router.
            continue

    # All routers in the pool failed — deterministic token-count fallback.
    # Not cached: a fallback means the router is broken/benched, and caching it
    # would hide the bench/retry behaviour for this prompt.
    return RouterDecision(
        role=role, tier=_tier_from_count(estimated), estimated_tokens=estimated,
        router_provider=last_provider or "(unavailable)",
        router_model=last_model or "(unavailable)",
        router_latency_ms=last_latency, fallback_used=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    # Publish the owning loop so the document-indexing worker (a plain thread)
    # can submit embed coroutines back here instead of building a second event
    # loop and deadlocking the gateway. See memory_api.set_loop.
    try:
        import memory_api as _MA
        _MA.set_loop(asyncio.get_running_loop())
    except Exception:
        pass
    try:
        import security_audit as _audit
        _audit.init()
    except Exception:
        pass
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
    # Multi-user credentials: the operator pool above remains the default;
    # a request carrying a valid user token gets a lazily-built pool from that
    # user's own keys.
    app.state.user_store = UserStore()
    app.state.user_pools = UserPoolManager(
        ORDER, ROUTER_ORDER, gemini_only=app.state.gemini_only)
    # Warm Kokoro/Whisper off the request path. The first TTS call otherwise
    # pays the model load (measured 16.9s for the first chunk).
    #
    # NOT under pytest: warm_up loads native TTS models in a daemon thread that
    # nothing joins on shutdown. If the test process exits while that thread is
    # mid-native-load, the interpreter finalizes under it and Windows raises
    # 0xC0000409 (STATUS_STACK_BUFFER_OVERRUN) AFTER pytest has reported all
    # tests passed — a green run with a non-zero exit code. Tests never need the
    # model warm, so skip it there; production is unaffected (PYTEST_CURRENT_TEST
    # is only set by pytest).
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            import voice_api as _voice
            __import__("threading").Thread(
                target=_voice.warm_up, daemon=True, name="voice-warmup").start()
        except Exception:
            pass
    # Kilo model discovery. Registers every upstream model id so a caller can
    # pass `model="vendor/name"` and land on kilo without naming the provider.
    # Needs no key, so it runs even when kilo is unwired. Guarded and bounded:
    # a slow or dead roster endpoint must never hold up startup, and the
    # gateway keeps the static KILO_MODEL fallback either way.
    app.state.kilo_models = None
    app.state.kilo_roster_task = None
    try:
        import kilo_models as _KM

        async def _kilo_roster_loop():
            """Keep the catalogue current without a restart.

            Kilo adds, renames and retires models continuously - the hardcoded
            default rotted into a 404 that way. Polling means the picker and
            the pool's default track reality on their own. The interval is
            generous because this is a UI convenience, not correctness:
            correctness comes from the guard that ignores a route whose
            provider is not wired.
            """
            first = await _KM.prime_routes(app.state.providers)
            if first:
                log.info("kilo: %d model ids routable for coding agents", first)
            while True:
                await asyncio.sleep(_KILO_ROSTER_REFRESH_SECONDS)
                try:
                    await _KM.get_models(force=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - never kill the loop
                    log.warning("kilo catalogue refresh failed: %s", e)
        app.state.kilo_roster_task = asyncio.ensure_future(_kilo_roster_loop())
    except Exception as _e:  # noqa: BLE001 - discovery is strictly optional
        log.warning("kilo model discovery unavailable at startup: %s", _e)
    app.state.embedders, app.state.embed_order = E.build_embedders()
    # Capture channel credentials BEFORE the provider-key scrub — GitHub's
    # token is one of the scrubbed names — so the startup probe can still
    # verify each configured channel.
    try:
        import channel_probe as _CP
        _CP.capture_tokens()
    except Exception as _e:  # noqa: BLE001 - diagnostics only
        log.warning("channel credential capture failed: %s", _e)
    # Provider keys are no longer needed in the process environment once the
    # pools and embedders are built. Keeping them there lets any in-process
    # adapter read os.environ["GEMINI_API_KEY"] — the glc_v1 attack. Capture
    # them for the dashboard and the redactor, then remove them.
    try:
        import secret_vault as _vault
        _scrubbed = _vault.capture_and_scrub()
        if _scrubbed:
            log.info("provider secrets moved out of the environment (%d vars)",
                     _scrubbed)
    except Exception as _e:  # noqa: BLE001 - must not block startup
        log.warning("provider-key env scrub failed: %s", _e)
    # One line at boot: which configured channels are actually alive. A
    # revoked token leaves a channel looking "configured" while every send
    # fails; this is the only place that difference is said out loud.
    try:
        import channel_probe as _CP
        _CP.schedule_startup_report(app, log)
    except Exception as _e:  # noqa: BLE001 - diagnostics only
        log.warning("channel health probe unavailable: %s", _e)
    # Inbound Telegram. Long-polls in-process because a webhook needs a public
    # HTTPS URL (Telegram cannot reach 127.0.0.1) and no `setWebhook` call
    # exists anywhere in this repo — which left the inbound path, and therefore
    # pairing, unreachable on a local install. See telegram_poller.py.
    try:
        import telegram_poller as _TGP
        app.state.telegram_poller = _TGP.start(app)
    except Exception as _e:
        print(f"[telegram] poller not started: {type(_e).__name__}")
    # Inbound Gmail + Slack, same reasoning: a webhook needs a public HTTPS URL,
    # so these long-poll in-process. See gmail_poller.py / slack_poller.py.
    try:
        import gmail_poller as _GP
        app.state.gmail_poller = _GP.start(app)
    except Exception as _e:
        print(f"[gmail] poller not started: {type(_e).__name__}")
    try:
        import slack_poller as _SP
        app.state.slack_poller = _SP.start(app)
    except Exception as _e:
        print(f"[slack] poller not started: {type(_e).__name__}")
    yield
    try:
        import telegram_poller as _TGP
        _TGP.stop()
    except Exception:
        pass
    try:
        import gmail_poller as _GP
        _GP.stop()
    except Exception:
        pass
    try:
        import slack_poller as _SP
        _SP.stop()
    except Exception:
        pass
    # The roster poller sleeps for up to half an hour; cancel it explicitly or
    # shutdown blocks on it and uvicorn waits out the timeout.
    task = getattr(app.state, "kilo_roster_task", None)
    if task is not None and not task.done():
        task.cancel()
    try:
        import secret_vault as _vault
        _vault.restore()
    except Exception:
        pass


# Swagger/OpenAPI describe the whole attack surface and require no token in
# the default configuration. Off unless explicitly enabled for development.
_DOCS_PUBLIC = (os.getenv("GATEWAY_DOCS", "").strip().lower()
                in ("1", "true", "yes"))
app = FastAPI(title="LLM Gateway V9", lifespan=lifespan,
              redirect_slashes=False,
              docs_url="/docs" if _DOCS_PUBLIC else None,
              redoc_url="/redoc" if _DOCS_PUBLIC else None,
              openapi_url="/openapi.json" if _DOCS_PUBLIC else None)

@app.get("/v1/security/audit")
async def security_audit_view(request: Request, limit: int = 100):
    """Operator-only view of the tamper-evident security log."""
    auth = getattr(request.state, "auth", None)
    if auth is None or not auth.admin:
        raise HTTPException(403, "the security audit requires the operator token")
    import security_audit
    return {"verify": security_audit.verify(),
            "events": security_audit.recent(limit)}


# ── the gateway's own console pages ───────────────────────────────────────────
# StaticFiles served the HTML raw, so the pages had no way to present the token
# that /v1/* now requires: every panel fetched, got 401, and rendered empty.
# HTML goes through `_console_asset`, which injects the meta tag + loader;
# everything else (css, js, icons) is passed straight through.
_STATIC_DIR = ROOT / "static"


def _console_asset(name: str, request: Request | None = None):
    import gateway_auth as _ga
    # The console pages carry the operator token in a meta tag. Serving that
    # page to a remote peer would hand it the admin credential, making the
    # /v1 token gate decorative on a LAN or public bind. The pages are for
    # the local operator; remote operators should use a tunnel or reverse
    # proxy that authenticates first.
    if request is not None and not _ga.host_is_loopback(
            getattr(request.client, "host", None)):
        from fastapi.responses import JSONResponse as _JR
        return _JR(status_code=403, content={
            "error": "gateway console pages are loopback-only; use a tunnel "
                     "or an authenticating reverse proxy"})
    from fastapi.responses import FileResponse as _FR
    base = _STATIC_DIR.resolve()
    target = (_STATIC_DIR / name).resolve()
    # Containment, not a string prefix. `str(target).startswith(str(base))`
    # also accepts any sibling whose name *begins* with "static", so
    # /static/../static_secrets/creds.txt returned 200 with the file body.
    try:
        target.relative_to(base)
    except ValueError:
        return JSONResponse(status_code=404, content={"error": "not found"})
    if not target.exists() or not target.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    if target.suffix.lower() not in (".html", ".htm"):
        return _FR(str(target))
    try:
        html = target.read_text(encoding="utf-8")
    except OSError:
        return _FR(str(target))
    return HTMLResponse(_ga.inject_into_html(html))


@app.get("/static/{name:path}", include_in_schema=False)
async def console_static(name: str, request: Request):
    return _console_asset(name, request)

# ── auth on every /v1 route ───────────────────────────────────────────────────
# The agent console is token-protected; the gateway's own surface was not, so
# any process that could reach :8109 could wipe memory, reload policy, read the
# configured-key inventory and spend real money via /v1/control/deploy-test.
# See gateway_auth.py for the full account.
#
# Deliberately NOT exempt: /docs, /openapi.json and /redoc. They describe the
# surface but change nothing, and leaving them open keeps the docs usable from a
# browser while the routes themselves stay closed.
_OPEN_PATHS = {"/", "/health", "/healthz", "/openapi.json", "/docs",
               "/docs/oauth2-redirect", "/redoc", "/favicon.ico"}


@app.middleware("http")
async def _gateway_token_guard(request, call_next):
    import gateway_auth as _auth
    path = request.url.path
    # Only /v1/* is guarded. The `/static` clause in this condition used to read
    # `not path.startswith("/static")`, which required a path NOT to be static
    # in order to skip the check - so every stylesheet, script and console page
    # under /static was itself demanded a token and answered 401. The gateway's
    # own UI then loaded no CSS and no JS, which is what "the gateway UI is
    # broken" actually looked like: unstyled panels full of failed requests.
    if not path.startswith("/v1/") or path in _OPEN_PATHS:
        return await call_next(request)
    store = getattr(request.app.state, "user_store", None)
    ctx = _auth.authenticate(request.headers.get(_auth.TOKEN_HEADER),
                             request.headers.get(_auth.USER_TOKEN_HEADER),
                             store)
    if ctx is not None:
        # Route handlers read `auth` for key selection and admin checks.
        request.state.auth = ctx
        request.state.user_id = ctx.user_id
        if (ctx.user_id and not ctx.admin
                and not _auth.user_route_allowed(path)):
            try:
                import security_audit
                security_audit.record(
                    "authz_denied", user_id=ctx.user_id, path=path[:200],
                    ip=str(getattr(request.client, "host", "") or "")[:80],
                    reason="user token is inference-only")
            except Exception:
                pass
            from fastapi.responses import JSONResponse as _JR
            return _JR(status_code=403, content={
                "error": "this route requires the operator gateway token"})
        try:
            _max_body = max(1, int(os.getenv("GATEWAY_MAX_BODY_MB", "32"))) * 1024 * 1024
            _declared = request.headers.get("content-length")
            if _declared and _declared.isdigit() and int(_declared) > _max_body:
                try:
                    import security_audit
                    security_audit.record(
                        "body_too_large", path=path[:200],
                        ip=str(getattr(request.client, "host", "") or "")[:80],
                        reason=f"content-length {_declared} > {_max_body}")
                except Exception:
                    pass
                from fastapi.responses import JSONResponse as _JR
                return _JR(status_code=413, content={
                    "error": "request body too large",
                    "limit_bytes": _max_body})
        except (TypeError, ValueError):
            pass
        if (ctx.user_id and not ctx.admin and path != "/v1/users/me"):
            try:
                from budget import budget as _budget
                ok, retry, why = _budget.check(ctx.user_id)
            except Exception:
                ok, retry, why = True, 0, ""
            if not ok:
                try:
                    import security_audit
                    security_audit.record(
                        "rate_limited", user_id=ctx.user_id, path=path[:200],
                        ip=str(getattr(request.client, "host", "") or "")[:80],
                        reason=why)
                except Exception:
                    pass
                from fastapi.responses import JSONResponse as _JR
                return _JR(status_code=429,
                           headers={"Retry-After": str(retry)},
                           content={"error": why,
                                    "code": "user_budget_exceeded",
                                    "retry_after": retry})
        return await call_next(request)
    try:
        import security_audit
        security_audit.record(
            "auth_denied", path=path[:200],
            ip=str(getattr(request.client, "host", "") or "")[:80],
            reason="missing or invalid credential")
    except Exception:
        pass
    from fastapi.responses import JSONResponse as _JR
    return _JR(
        status_code=401,
        content={"error": (
            f"missing or invalid {_auth.TOKEN_HEADER} header, and no valid "
            f"{_auth.USER_TOKEN_HEADER}")},
        headers={"WWW-Authenticate": _auth.TOKEN_HEADER},
    )
# V10 adaptor plane (channels/hooks/policy/spend/control) + voice services
# + keyed third-party integrations (ex-agent Tier-1 tools).
app.include_router(channels_router)
app.include_router(voice_router)
app.include_router(integrations_router)
app.include_router(memory_router)
app.include_router(docgen_router)
app.include_router(users_router)
from deploy_api import router as deploy_router
app.include_router(deploy_router)
# Mesh validation / repair / analysis. `parts.api` imports only fastapi and its
# own package - never `main` - so there is no circular import; its own test
# suite asserts that in a subprocess. The routes are loopback-only and run the
# blocking numpy work in a threadpool.
from parts.api import router as parts_router
app.include_router(parts_router)

# Optimisation + geometry-for-viewer. Separate from `parts.api` because these
# endpoints return the triangle soup and the objective, which is what an
# agentic optimise/measure loop needs, rather than a validation report.
from parts.optimise_api import router as parts_optimise_router
app.include_router(parts_optimise_router)


def _normalize_messages(req: ChatRequest):
    if req.messages:
        return P.drop_empty_turns(list(req.messages))
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


def _est_input_tokens(messages, system_blocks, tools=None) -> int:
    """Rough prompt size in tokens. Deliberately coarse: it is used to keep a
    call inside a provider's window, and the safety margin below covers the
    drift between this estimate and a real tokenizer.

    Tool definitions are part of the prompt. Upstream spells this out when it
    refuses: "259371 tokens (194119 of text input, 1252 of tool input, 64000 in
    the output)" - an estimate that reads only the messages under-counts."""
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
    if tools:
        fp = (len(tools), tuple(
            str((t.get("function") or {}).get("name") or t.get("name") or "")
            for t in tools if isinstance(t, dict)))
        cc = _TOOLS_CHARS.get(fp)
        if cc is None:
            try:
                cc = len(json.dumps(tools))
            except (TypeError, ValueError):
                cc = len(str(tools))
            if len(_TOOLS_CHARS) > 256:
                _TOOLS_CHARS.clear()
            _TOOLS_CHARS[fp] = cc
        chars += cc
    return chars // 4


def _est_tokens(messages, system_blocks, max_tokens):
    return _est_input_tokens(messages, system_blocks) + min(max_tokens, 8192)


_CTX_SAFETY_TOKENS = 2048
_MIN_USEFUL_OUTPUT_TOKENS = 512
SLOT_WAIT_MAX_S = 20.0
SLOT_WAITS = 3


def _clamp_max_tokens(provider_name, est_in, requested):
    """Fit the call inside the provider's window by shrinking the OUTPUT.

    `max_ctx` has sat in router.LIMITS unread since it was written, while
    router.py's own docstring still claimed a context-window check happens. It
    did not: a ~194k-token prompt against a 256k provider went out with 64k
    reserved for the answer, upstream answered 400 "maximum context length is
    256000 tokens", and the node lost the minutes of research it had already
    paid for. Nothing here trims the prompt - the reservation gives way."""
    try:
        max_ctx = int(limits_for(provider_name).get("max_ctx") or 0)
    except Exception:
        max_ctx = 0
    req_mt = int(requested or 0)
    if not max_ctx or not req_mt:
        return req_mt, ""
    room = max_ctx - int(est_in) - _CTX_SAFETY_TOKENS
    if room < _MIN_USEFUL_OUTPUT_TOKENS:
        return None, (f"prompt of ~{int(est_in)} tokens leaves {room} in this "
                      f"provider's {max_ctx} window")
    if room < req_mt:
        return room, f"output clamped {req_mt}->{room} for {max_ctx} window"
    return req_mt, ""


def _soonest_wait(router, candidates):
    """Seconds until the first candidate could be usable, or None if nothing
    time-bounded is blocking it."""
    best = None
    for c in candidates:
        try:
            snap = router.state[c].snapshot(limits_for(c))
        except Exception:
            continue
        rem = max(float(snap.get("backoff_remaining") or 0.0),
                  float(snap.get("cooldown_remaining") or 0.0))
        if rem <= 0:
            return 0.0
        if best is None or rem < best:
            best = rem
    return best


def _backoff_for(err: Exception, has_model_override: bool = False,
                 provider: str = ""):
    # Kilo is deliberately exempt from the long backoffs below.
    #
    # Kilo fronts ~400 model ids behind ONE endpoint, so a 429/RPD on any single
    # id says nothing about the other 399 - and a 1-hour blackball on the only
    # free tool-calling provider we have is a self-inflicted outage. Observed
    # live: one 429 with "per day" in the message took the whole free tier
    # offline for 3600s, and every code-agent and tool-calling run after it had
    # nowhere to go. A few seconds is enough to stop a hot retry loop without
    # taking the catalogue down with it.
    if provider == "kilo":
        msg = str(err).lower()
        status = getattr(err, "status", None)
        if status == 429:
            if "queue" in msg:
                return 5, "kilo queue full"
            return 10, "kilo rate limited"
        if status and 500 <= status < 600:
            return 10, f"kilo upstream {status}"
        if status == 402:
            return 0, ""
        if status in (401, 403):
            return 0, ""
        return 0, ""
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


def _host_only(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        return (urlsplit(str(url or "")).hostname or "")[:120]
    except Exception:
        return ""


async def _resolve_image_urls(messages: list[dict]) -> list[dict]:
    """V9: fetch any http(s) image URLs in message content and inline them as
    data: URLs. Providers downstream only ever see data: URLs, which keeps
    Gemini/Ollama translation paths simple. Mutates a copy; original is intact.
    """
    import base64

    async def _fetch_to_data_url(url: str) -> str:
        # SSRF boundary: http(s) only, public addresses only, every redirect
        # revalidated, body and time capped. The URL is never echoed in an
        # error, because query strings can carry credentials.
        from safe_fetch import UnsafeURLError, fetch_bytes
        try:
            body, mt, _final = await fetch_bytes(url, image_only=True,
                                                 max_bytes=8 * 1024 * 1024,
                                                 timeout=15.0)
        except UnsafeURLError as e:
            try:
                import security_audit
                security_audit.record("ssrf_blocked", target=_host_only(url),
                                      reason=str(e)[:200])
            except Exception:
                pass
            raise HTTPException(400, f"image URL rejected: {e}")
        except Exception:
            raise HTTPException(400, "failed to fetch image URL")
        b64 = base64.b64encode(body).decode()
        return f"data:{mt or 'image/png'};base64,{b64}"

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


# ── latency caches ──────────────────────────────────────────────────────────
# Local response cache: identical deterministic (temperature=0, non-stream)
# requests return instantly instead of paying a provider round-trip. Bounded LRU
# with a short TTL so stale answers cannot linger.
_RESP_CACHE: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_RESP_TTL_S = 60.0
_RESP_MAX = 256


def _resp_key(req, messages, system_blocks) -> str:
    return hashlib.sha256(json.dumps(
        [req.model, req.provider, messages, system_blocks, req.tools, req.max_tokens],
        sort_keys=True, default=str).encode()).hexdigest()


def _resp_get(key):
    e = _RESP_CACHE.get(key)
    if not e:
        return None
    exp, val = e
    if time.time() > exp:
        _RESP_CACHE.pop(key, None)
        return None
    _RESP_CACHE.move_to_end(key)
    return val


def _resp_put(key, val):
    _RESP_CACHE[key] = (time.time() + _RESP_TTL_S, val)
    _RESP_CACHE.move_to_end(key)
    while len(_RESP_CACHE) > _RESP_MAX:
        _RESP_CACHE.popitem(last=False)


# Tier-classification cache: the router-LLM hop is a full extra round-trip; the
# same prompt text maps to the same tier, so memoize it briefly.
_TIER_CACHE: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_TIER_TTL_S = 300.0
_TIER_MAX = 512


def _tier_cached(role, prompt_text):
    key = hashlib.sha256(f"{role}\x00{prompt_text}".encode()).hexdigest()
    e = _TIER_CACHE.get(key)
    if e and time.time() <= e[0]:
        _TIER_CACHE.move_to_end(key)
        return e[1]
    return None


def _tier_store(role, prompt_text, tier):
    key = hashlib.sha256(f"{role}\x00{prompt_text}".encode()).hexdigest()
    _TIER_CACHE[key] = (time.time() + _TIER_TTL_S, tier)
    _TIER_CACHE.move_to_end(key)
    while len(_TIER_CACHE) > _TIER_MAX:
        _TIER_CACHE.popitem(last=False)


# Control-plane snapshot cache: /v1/status & /v1/routers recompute all_status()
# (db + provider state) per request (~200ms). Cache ~1s for polling UIs.
_STATUS_CACHE: dict = {}
_STATUS_TTL_S = 1.0


def _snap(key, build):
    now = time.time()
    e = _STATUS_CACHE.get(key)
    if e and now - e[0] < _STATUS_TTL_S:
        return e[1]
    val = build()
    _STATUS_CACHE[key] = (now, val)
    return val


# Tool-schema serialized size rarely changes; cache it by (count, names) so we
# don't json.dumps the whole schema on every request (estimate only).
_TOOLS_CHARS: dict = {}


@app.post("/v1/chat")
async def chat(req: ChatRequest, request: Request):
    auth = getattr(request.state, "auth", None)
    user_id = getattr(auth, "user_id", None)
    _is_admin = bool(getattr(auth, "admin", False))

    def _client_error(exc) -> str:
        """Detailed provider errors are operator-only; a user token gets a
        generic failure so it cannot read the provider roster/attempt chain."""
        return _red(str(exc)) if _is_admin else "the model provider failed"
    if user_id:
        pools = request.app.state.user_pools.get(
            request.app.state.user_store, user_id)
        if pools is None:
            raise HTTPException(
                503, detail={
                    "code": "user_has_no_keys",
                    "message": ("this user has no provider keys configured; "
                                "add one in the gateway's Users page"),
                    "user_id": user_id,
                })
        router = pools.router
        router_pool = pools.router_pool
    else:
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
    est_in = _est_input_tokens(messages, system_blocks, req.tools)
    # Local response cache: deterministic non-stream requests return instantly on
    # a repeat instead of paying a provider round-trip.
    _rc_key = None
    if not req.stream and (req.temperature or 0) == 0:
        _rc_key = _resp_key(req, messages, system_blocks)
        _hit = _resp_get(_rc_key)
        if _hit is not None:
            return _hit
    explicit_override = bool(req.provider)
    required_caps = _required_caps(req)

    # V9: model-name routing. If the caller passes only `model=` (no `provider=`),
    # resolve the provider from the model-route registry so the gateway uses that
    # specific backend. This lets any service call the gateway with just a model
    # name and have it routed to the right backend (e.g. Kilo-served models).
    #
    # Two tables, deliberately: MODEL_ROUTES is global, CODE_MODEL_ROUTES is
    # consulted only when the caller tagged the request as a coding agent.
    # Kilo's catalogue lives in the second one, so a Kilo id passed by the
    # document pipeline or chat is ignored instead of silently rerouting
    # authoring work onto a free coding model.
    if req.model and not req.provider:
        routed = _explicit_provider_for(req.model, req.agent, router.providers)
        if routed:
            req.provider = routed
            explicit_override = True

    # V8: if the caller tagged the request with an agent name and did not
    # pin a provider explicitly, apply agent_routing.yaml's preferred provider.
    # This mutates req.provider so the rest of the function (router-pick,
    # candidate-narrowing, single-candidate-wait) sees the pin.
    # Agent-routing is a SOFT preference, not a hard pin: a transient 5xx on
    # the preferred provider must fail over to the ladder, not 502 the whole
    # node. A caller's explicit provider= stays a hard binding below.
    _agent_pref = None
    if req.agent and not req.provider:
        pinned = AGENT_ROUTING.get(req.agent)
        if pinned and pinned in router.providers:
            _agent_pref = pinned

    # V9: Kilo's free models reason by default, which adds a long thinking
    # delay. Unless the caller explicitly asks for reasoning, force it OFF for
    # Kilo so calls return fast. The caller can still pass
    # reasoning="low"|"medium"|"high" to opt in.
    # route_for_model, not a raw MODEL_ROUTES lookup: the routing block above has
    # already resolved a code-scoped Kilo id into req.provider, and a direct
    # dict get would miss it for coding-agent calls.
    _kilo_resolved = (req.provider == "kilo") or (
        bool(req.model)
        and P.route_for_model(req.model, req.agent) == "kilo"
        and "kilo" in router.providers)
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
        router_decision = await _classify_tier(req, req.auto_route, router_pool, prompt_text, user_id)
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

    # Prompt-cache affinity: if this caller is mid-conversation on a provider
    # (one DAG node's next hop), start there instead of re-picking by load and
    # throwing away the provider's cache of the node's system prompt. A caller
    # that pinned `req.provider` already knows what it wants, and `candidates`
    # is a single entry there, so affinity is only consulted for routed calls.
    _preferred = (None if explicit_override
                  else (_agent_pref or router.provider_for(req.route_affinity)))

    # When explicit provider is requested and the only blocker is cooldown,
    # wait briefly rather than 503-ing — this is what users intuitively expect.
    if explicit_override and len(candidates) == 1:
        import asyncio as _asyncio
        deadline = time.time() + 30
        while time.time() < deadline:
            name, _ = router.pick(est, candidates, required_caps=required_caps,
                                  model=req.model)
            if name is not None:
                break
            cd = router.state[candidates[0]].snapshot(limits_for(candidates[0]))["cooldown_remaining"]
            if cd <= 0 or cd > 30:
                break
            await _asyncio.sleep(min(cd + 0.05, 2))

    _waits_left = SLOT_WAITS
    for _ in range(len(candidates) + 1 + SLOT_WAITS):
        name, atts = router.pick(est, candidates, required_caps=required_caps,
                                 preferred=_preferred, model=req.model)
        # Once the affinity provider has been tried (successfully or not), stop
        # preferring it: the next pass must fall back to the load-based pick so
        # a wedged provider cannot pin a node into an outage.
        _preferred = None
        all_attempts.extend(atts)
        if name is None:
            # Every worker was blocked. A wide fan-out burns the whole pool's
            # RPM in one wave, and the soonest slot is usually under a minute
            # away - so wait for it rather than 503. This node has already
            # spent minutes gathering what it is about to send; failing here
            # throws that away and hands the recovery planner the same hop to
            # redo, which then burns the same quota again.
            if _waits_left > 0 and candidates:
                wait = _soonest_wait(router, candidates)
                if wait is not None and 0 < wait <= SLOT_WAIT_MAX_S:
                    _waits_left -= 1
                    all_attempts.append({
                        "provider": "ladder",
                        "reason": f"waiting {wait:.1f}s for the next slot"})
                    import asyncio as _aio_wait
                    await _aio_wait.sleep(min(wait + 0.15, SLOT_WAIT_MAX_S))
                    continue
            break

        provider = router.providers[name]
        # Per-provider context window: shrink the output reservation so the
        # call fits, or move on to a provider that can take it.
        _mt, _clamp_why = _clamp_max_tokens(name, est_in, req.max_tokens)
        if _mt is None:
            all_attempts.append({"provider": name, "reason": f"too large: {_clamp_why}"})
            candidates = [c for c in candidates if c != name]
            continue
        if _clamp_why:
            all_attempts.append({"provider": name, "reason": _clamp_why})
        t0 = time.time()
        router.state[name].record(0)
        empty_retries = 0

        try:
            if req.stream:
                async def gen():
                    try:
                        agg = []
                        usage_blob = None
                        # Whether the stream produced anything that counts as
                        # an answer. A tool call is an answer; zero text and
                        # zero tool calls is not.
                        saw_tool_delta = False
                        async for chunk in provider.stream(messages,
                                                          max_tokens=_mt,
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
                                saw_tool_delta = True
                                yield f"data: {json.dumps({'provider': name, 'tool_call_delta': chunk[len('[[TOOL_CALL_DELTA]] '):]})}\n\n"
                            elif chunk.startswith("[[REASONING_DELTA]]"):
                                yield f"data: {json.dumps({'provider': name, 'reasoning_delta': json.loads(chunk[len('[[REASONING_DELTA]] '):])})}\n\n"
                            elif chunk.startswith("[[USAGE]]"):
                                # Consumed here — the client gets no usage
                                # frame; the ledger gets the real numbers.
                                usage_blob = chunk[len("[[USAGE]] "):]
                            else:
                                yield f"data: {json.dumps({'provider': name, 'delta': chunk})}\n\n"
                        text = "".join(agg)
                        latency = int((time.time() - t0) * 1000)
                        usage_out = None
                        if usage_blob is not None:
                            try:
                                _u = json.loads(usage_blob)
                                _det = _u.get("prompt_tokens_details") or {}
                                usage_out = {
                                    "in_tok": int(_u.get("prompt_tokens") or 0),
                                    "out_tok": int(_u.get("completion_tokens") or 0),
                                    "cache_read": max(int(_det.get("cached_tokens") or 0),
                                                      int(_u.get("cached_tokens") or 0)),
                                }
                            except Exception:
                                usage_out = None
                        # An empty stream is NOT a successful call, and this
                        # branch had no guard for it while the batched branch
                        # below does. Measured: 57 worker calls in seven days
                        # logged `ok` with zero output tokens, every one of them
                        # followed by a later non-empty call in the same session
                        # — i.e. the agent paid twice. It costs Discuss
                        # specifically because Discuss always streams: the
                        # gateway finished, returned 200 with nothing in it, the
                        # runner saw zero text and zero tool calls, switched
                        # itself off streaming for the rest of the turn, and
                        # re-asked. The user waits ~13 s and sees no tokens.
                        #
                        # Nothing has been yielded to the client if we get here
                        # with an empty `agg` and no tool deltas, so a retry is
                        # invisible to the caller: it just gets a slower first
                        # byte instead of a fake answer.
                        if not text.strip() and not saw_tool_delta:
                            agg = []
                            async for chunk in provider.stream(
                                    messages, max_tokens=_mt,
                                    temperature=req.temperature,
                                    model=req.model, tools=req.tools,
                                    tool_choice=req.tool_choice,
                                    reasoning=req.reasoning,
                                    response_format=req.response_format,
                                    system_blocks=system_blocks,
                                    cache_system=bool(req.cache_system)):
                                agg.append(chunk)
                                if chunk.startswith("[[TOOL_CALL_DELTA]]"):
                                    saw_tool_delta = True
                                    yield (f"data: {json.dumps({'provider': name, 'tool_call_delta': chunk[len('[[TOOL_CALL_DELTA]] '):]})}\n\n")
                                elif chunk.startswith("[[REASONING_DELTA]]"):
                                    yield (f"data: {json.dumps({'provider': name, 'reasoning_delta': json.loads(chunk[len('[[REASONING_DELTA]] '):])})}\n\n")
                                elif chunk:
                                    yield f"data: {json.dumps({'provider': name, 'delta': chunk})}\n\n"
                            text = "".join(agg)
                            latency = int((time.time() - t0) * 1000)
                            if not text.strip() and not saw_tool_delta:
                                # Say so, and log it as the failure it is. The
                                # runner's batched fallback then goes through the
                                # failover ladder, which is the path that knows
                                # how to change provider.
                                db.log_call(provider=name,
                                            model=req.model or provider.model,
                                            input_tokens=est_in, output_tokens=0,
                                            latency_ms=latency, status="error",
                                            error="empty completion (0 text, 0 tool calls) "
                                                  "on the streaming path",
                                            prompt_chars=len(prompt_text),
                                            response_chars=0,
                                            override=req.provider,
                                            attempted=_attempts_str(all_attempts),
                                            agent=req.agent, session=req.session,
                                            retries=retries, user_id=user_id)
                                yield f"data: {json.dumps({'error': f'{name} returned an empty completion (0 text, 0 tool calls)'})}\n\n"
                                return
                        # Streaming providers return text only (no usage block),
                        # so estimate tokens the same way the pre-flight does
                        # instead of logging zeros that undercount cost in
                        # by_agent. `len(prompt_text) // 4` counted MESSAGES
                        # only and left `req.tools` out, while the pre-flight
                        # and the provider both bill them: a chat turn logged
                        # 5,076 input tokens and actually sent ~9,300, because
                        # 27 tool schemas are 4,232 tokens of prompt that no
                        # row ever mentioned. Use the same estimate the router
                        # used, so the ledger and the invoice agree.
                        # Prefer the provider's own usage frame (the stream
                        # asked for it); fall back to the pre-flight estimate.
                        _log_in = usage_out["in_tok"] if usage_out else est_in
                        _log_out = usage_out["out_tok"] if usage_out else len(text) // 4
                        _log_cache = usage_out["cache_read"] if usage_out else 0
                        db.log_call(provider=name, model=req.model or provider.model,
                                    input_tokens=_log_in,
                                    output_tokens=_log_out,
                                    latency_ms=latency, status="ok",
                                    prompt_chars=len(prompt_text), response_chars=len(text),
                                    override=req.provider, attempted=_attempts_str(all_attempts),
                                    agent=req.agent, session=req.session, retries=retries,
                                    user_id=user_id, cache_read_tokens=_log_cache)
                        yield f"data: {json.dumps({'done': True, 'provider': name, 'model': req.model or provider.model, 'input_tokens': _log_in, 'output_tokens': _log_out, 'cache_read_tokens': _log_cache})}\n\n"
                    except Exception as e:
                        db.log_call(provider=name, model=req.model or provider.model,
                                    status="error", error=str(e)[:500],
                                    latency_ms=int((time.time() - t0) * 1000),
                                    prompt_chars=len(prompt_text),
                                    override=req.provider, attempted=_attempts_str(all_attempts),
                                    agent=req.agent, session=req.session, retries=retries,
                                    user_id=user_id)
                        yield f"data: {json.dumps({'error': _client_error(e)[:300]})}\n\n"
                return StreamingResponse(gen(), media_type="text/event-stream")

            # V8: one same-provider retry on transient 5xx / timeout before
            # we fall through to the failover loop. Exponential backoff capped
            # at 2s, exactly as the spec says. `retries` is surfaced in the
            # response so the orchestrator's replay can show it.
            try:
                result = await provider.chat(messages,
                                             max_tokens=_mt,
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
                                             max_tokens=_mt,
                                             temperature=req.temperature,
                                             model=req.model,
                                             tools=req.tools,
                                             tool_choice=req.tool_choice,
                                             reasoning=req.reasoning,
                                             response_format=req.response_format,
                                             system_blocks=system_blocks,
                                             cache_system=bool(req.cache_system))
            # An empty completion is NOT a successful call. Gemini's worker
            # occasionally finishes with zero text and zero tool calls (seen
            # live at ~50s, output_tokens=0); returning 200 made every skill
            # retry the same context and the recovery planner write a thinner
            # document. Retry once on the SAME provider - that is the retry
            # the agent used to perform - then treat it as a provider failure
            # so the failover ladder tries the next model.
            while not str(result.get("text") or "").strip() \
                    and not (result.get("tool_calls") or []):
                if empty_retries >= 1:
                    raise P.ProviderError(
                        f"{name} returned an empty completion "
                        f"(0 text, 0 tool calls)", retryable=True)
                empty_retries += 1
                import asyncio as _a
                await _a.sleep(0.4)
                result = await provider.chat(
                    messages, max_tokens=_mt,
                    temperature=req.temperature, model=req.model,
                    tools=req.tools, tool_choice=req.tool_choice,
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
                                                 max_tokens=_mt,
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
            # Affinity is recorded only on a SUCCESSFUL call. Recording the
            # attempt would pin a node to the provider that just failed it, and
            # the next hop would start from the outage instead of the load pick.
            router.note_affinity(req.route_affinity, name)
            if router_decision is not None:
                router_decision.chosen_worker_provider = name
                router_decision.chosen_worker_model = result["model"]
            db.log_call(provider=name, model=result["model"],
                        input_tokens=result["input_tokens"], output_tokens=result["output_tokens"],
                        thinking_tokens=result.get("thinking_tokens", 0),
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
                        agent=req.agent, session=req.session, retries=retries,
                                    user_id=user_id)
            # Tool ledger: name every completed model tool call (batch and
            # vision funnel through here too). Names only — never arguments.
            for tc in (result["tool_calls"] or []):
                db.log_tool_use(str((tc or {}).get("name") or "tool"),
                                provider=name, model=result["model"],
                                agent=req.agent, session=req.session,
                                call_role="worker")
            _resp = ChatResponse(
                provider=name,
                model=result["model"],
                text=result["text"],
                reasoning=result.get("reasoning", "") or "",
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
            if _rc_key:
                _resp_put(_rc_key, _resp)
            return _resp

        except P.ProviderError as e:
            last_err = str(e)
            secs, reason = _backoff_for(e, has_model_override=bool(req.model),
                                            provider=name)
            if secs > 0:
                router.state[name].mark_unavailable(secs, reason)
            db.log_call(provider=name, model=req.model or provider.model,
                        status="error", error=str(e)[:500],
                        latency_ms=int((time.time() - t0) * 1000),
                        prompt_chars=len(prompt_text),
                        override=req.provider, attempted=_attempts_str(all_attempts),
                        agent=req.agent, session=req.session, retries=retries,
                                    user_id=user_id)
            tag = f"failed: {str(e)[:100]}"
            if secs > 0: tag += f" → backoff {secs:.0f}s ({reason})"
            all_attempts.append({"provider": name, "reason": tag})
            if explicit_override or not getattr(e, "retryable", True):
                raise HTTPException(502, _client_error(f"{name} failed: {e}"))
            candidates = [c for c in candidates if c != name]
            continue
        except HTTPException:
            raise
        except Exception as e:
            last_err = str(e)
            secs, reason = _backoff_for(e, has_model_override=bool(req.model),
                                            provider=name)
            if secs > 0:
                router.state[name].mark_unavailable(secs, reason)
            db.log_call(provider=name, model=req.model or provider.model,
                        status="error", error=str(e)[:500],
                        latency_ms=int((time.time() - t0) * 1000),
                        prompt_chars=len(prompt_text),
                        override=req.provider, attempted=_attempts_str(all_attempts),
                        agent=req.agent, session=req.session, retries=retries,
                                    user_id=user_id)
            all_attempts.append({"provider": name, "reason": f"exception: {str(e)[:120]}"})
            if explicit_override:
                raise HTTPException(502, _client_error(f"{name} failed: {e}"))
            candidates = [c for c in candidates if c != name]
            continue

    raise HTTPException(503, _client_error(f"all providers unavailable. attempts: {all_attempts}. last_error: {last_err}"))


# ── V8 additions: batch endpoint and cost-by-agent ────────────────────────────

@app.post("/v1/chat/batch")
async def chat_batch(req: BatchChatRequest, request: Request):
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
                return await chat(call, request)
            except HTTPException as he:
                return {"error": str(he.detail), "status_code": he.status_code}
            except Exception as e:
                return {"error": str(e)[:400], "status_code": 500}

    results = await _a.gather(*[_one(c) for c in req.calls])
    return {"results": results}


@app.post("/v1/vision")
async def vision(req: VisionRequest, request: Request):
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
    return await chat(inner, request)


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
async def embed(req: EmbedRequest, request: Request):
    """Single embed endpoint. Failover ring runs Ollama → configured fallback.
    `provider` pins the choice (returns 502 on failure with no fallback).
    Rejects inputs over MAX_INPUT_CHARS with 413 — caller must chunk."""
    embedders = app.state.embedders
    if not embedders:
        raise HTTPException(503, "no embedding providers configured")
    _uid = getattr(getattr(request.state, "auth", None), "user_id", None)

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
            raise HTTPException(502, _red(f"{req.provider} embed failed: {e}"))
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
        agent=req.agent, session=req.session, user_id=_uid,
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
async def embed_batch(req: EmbedBatchRequest, request: Request):
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
    _uid = getattr(getattr(request.state, "auth", None), "user_id", None)
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
                raise HTTPException(
                    502, _red(f"{req.provider} batch embed failed: {e}"))
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
        agent=req.agent, session=req.session, user_id=_uid,
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
        # Kilo fronts ~400 models behind one provider entry, so the static
        # `models` map above shows only its default. This is a summary;
        # the full roster lives at /v1/providers/kilo/models.
        "kilo": {
            "wired": "kilo" in r.providers,
            "default_model": getattr(r.providers.get("kilo"), "model", None),
            "model_ids_discovered": len(P.KILO_MODEL_IDS),
            # Kilo ids route for agent="code" only. Reported separately from
            # the global table so it is visible that the catalogue is NOT
            # reachable from chat or the document pipeline.
            "code_routable": len(P.CODE_MODEL_ROUTES),
            "code_agents": sorted(P.CODE_AGENTS),
            # Any Kilo id that leaked into the global table would let chat and
            # the document pipeline reach the coding catalogue. Designed to be
            # 0; surfaced so a regression is visible rather than silent.
            "leaked_global": sum(
                1 for mid, p in P.MODEL_ROUTES.items() if p == "kilo"),
            "discovered": bool(getattr(app.state, "kilo_models", None)),
        },
    }


@app.get("/v1/providers/kilo/models")
async def list_kilo_models(refresh: bool = False, all: bool = False):
    """The live Kilo roster, for a model picker.

    Kilo's /models endpoint needs no auth, so this works before a key is
    saved -- which is the point: the picker should show what a key buys.
    `?refresh=true` bypasses the TTL cache. A failed fetch degrades to the
    last good roster with `stale: true`; it never 500s and never empties a
    roster we already had.
    """
    import kilo_models as KM
    roster = await KM.get_models(force=refresh)
    free = [m for m in roster["models"] if m["is_free"]]
    # The model the pool is actually configured with, so the picker can
    # pre-select it. Read from the wired provider rather than recomputed:
    # an explicit KILO_MODEL outranks the derived choice, and echoing the
    # recomputed value would hide that.
    wired = app.state.providers.get("kilo") if getattr(app.state, "providers", None) else None
    default = getattr(wired, "model", None) or KM.pick_default_model(roster["models"])
    return {
        "base_url": roster["base_url"],
        "count": roster["count"],
        "free_count": roster["free_count"],
        "stale": roster["stale"],
        "error": roster["error"],
        "fetched_at": roster["fetched_at"] or None,
        "default": default,
        "configured": bool(getattr(app.state, "providers", {}) and "kilo" in app.state.providers),
        # Free-first is the default ordering upstream: free, then largest
        # context. Callers that want everything send ?all=true.
        "free": free if not all else roster["models"],
        "models": roster["models"] if all else free,
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


@app.get("/v1/keys/info")
async def keys_info():
    """How to obtain and refresh every gateway credential, plus live presence.

    Returns names, purpose, where to get each key and how to renew it — never
    a value. `set` is the only thing read from the environment.
    """
    import key_info
    rows = key_info.collect()
    return {
        "configured": sum(1 for r in rows if r["set"]),
        "total": len(rows),
        "keys": [
            {k: r[k] for k in
             ("name", "env", "enables", "group", "get", "refresh", "script", "set")}
            for r in rows
        ],
    }


@app.get("/v1/channels/health")
async def channels_health():
    """Live, read-only probe of each configured channel's credential.

    "Configured" (a token exists) is not "working" (the token is accepted).
    This runs one cheap read per channel and reports ok/detail — never a send.
    """
    import channel_probe
    rows = await asyncio.to_thread(channel_probe.probe_all)
    configured = [r for r in rows if r["configured"]]
    return {
        "configured": len(configured),
        "healthy": sum(1 for r in configured if r["ok"]),
        "unhealthy": [r for r in configured if not r["ok"]],
        "channels": rows,
    }


@app.get("/v1/status")
async def status():
    r = app.state.router
    return _snap("status", lambda: {
        "order": r.order, "live": r.all_status(),
        "today": db.aggregate(call_role="worker"), "limits": LIMITS,
        "gemini_only": bool(getattr(app.state, "gemini_only", False))})


@app.get("/v1/routers")
async def routers():
    """V3: router pool — separate from the worker pool. Shows which router LLMs
    are wired, the failover order, and live rate-state."""
    rp = app.state.router_pool
    return _snap("routers", lambda: {
        "order": rp.order,
        "providers": list(rp.providers.keys()),
        "models": {n: p.model for n, p in rp.providers.items()},
        "live": rp.all_status(),
        "today": db.aggregate(call_role="router"),
        "limits": {k: limits_for(k) for k in rp.providers},
        "tier_to_order": TIER_TO_ORDER,
    })


@app.get("/v1/cache")
async def cache_health():
    """Are the two cache mechanisms actually running, or only configured?

    Added because `cache_read_tokens` was 0 in the ledger for 97.5% of calls and
    `cache_create_tokens` was 0 for ALL ~100k of them, and the only reason the
    second is not ambiguous is that nothing else could explain it: an unused
    explicit cache and a silently failing one produce identical numbers. This
    reports the mint failures the call path swallows, and how many nodes are
    currently pinned.
    """
    import cache as _cache_mod
    from router import AFFINITY_TTL_S
    store = getattr(app.state, "cache", None)
    routers = [app.state.router]
    rp = getattr(app.state, "router_pool", None)
    if rp is not None:
        routers.extend(getattr(rp, "routers", []) or [])
    affinity = sum(len(getattr(r, "affinity", {}) or {}) for r in routers)
    return {
        "explicit": {
            "enabled": store is not None,
            "ttl_seconds": getattr(store, "ttl", None),
            "live_entries": len(getattr(store, "_store", {}) or {}),
            "mint_failures": _cache_mod.failure_report(),
        },
        "implicit_prefix": {
            "note": "provider-side; only reports above the model's minimum "
                    "prompt size. Ledger floor for the gemini family is "
                    "~4.1k input tokens.",
        },
        "route_affinity": {"pinned_nodes": affinity, "ttl_seconds": AFFINITY_TTL_S},
    }


@app.get("/v1/calls")
async def calls(limit: int = 100, provider: Optional[str] = None, status: Optional[str] = None):
    # A negative or absurd limit used to mean unbounded: `?limit=-1` served
    # 20.7 MB in 5.5 s, and six concurrent callers moved 124 MB at 35 s each.
    # The ledger grows forever, so the read must be bounded and offset-capable
    # rather than trusting the client.
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))
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
async def index(request: Request):
    return _console_asset("dashboard.html", request)


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    return _console_asset("help.html", request)


@app.get("/health")
async def healthz():
    """Liveness, deliberately unauthenticated.

    The agent polls this to decide whether the gateway is up. It used to probe
    `/v1/routers`, which now sits behind the gateway token, so a token-path
    mistake made a perfectly healthy gateway look dead - and `ensure_gateway()`
    responded by launching a second copy onto an occupied port and reporting
    "failed to start within 45s". Liveness must never depend on authorisation.
    """
    return {"status": "ok", "service": "llm_gateway_v9", "port": PORT}


@app.get("/healthz")
async def healthz_alias():
    return await healthz()


if __name__ == "__main__":
    import os as _os
    import sys as _sys
    import uvicorn
    # Loopback by default (holds secrets + spend). A LAN bind needs a real,
    # configured token: the process-generated one cannot be known by a client,
    # so binding 0.0.0.0 with it would leave every /v1 route open to the network.
    # This instance WAS running on 0.0.0.0, which is how the unauthenticated
    # memory wipe in gateway_auth.py's docstring was reachable.
    _host = _os.environ.get("GATEWAY_HOST", "127.0.0.1").strip() or "127.0.0.1"
    import gateway_auth as _ga
    _safe, _why = _ga.assert_safe_bind(_host)
    if not _safe:
        print(f"[gateway] refusing to start: {_why}", file=_sys.stderr)
        raise SystemExit(2)
    _ga.publish_token_file()
    print(f"[gateway] auth enabled on /v1/* ({_why}); "
          f"token published to {_ga.token_file()}")
    # The app OBJECT, not the import string: the string made uvicorn re-import
    # this module while it was already running as __main__, so the module body
    # ran twice per boot. See the same fix in agent_server.py.
    uvicorn.run(app, host=_host, port=PORT, reload=False)
