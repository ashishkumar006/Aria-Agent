"""Channel + policy + spend + control routes (V10 adaptor plane).

Mounted from main.py as a single APIRouter. All handlers are defensive:
unknown channel -> 404, missing creds -> {ok:false}, scaffolded live
paths -> 501. Nothing here ever leaks a secret value.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import db
from adaptors import registry
from adaptors.base import BaseAdaptor, NotConfigured, NotIntegrated
from adaptors.envelope import TrustLevel
from scrub import scrub as _scrub

router = APIRouter()

# ── dashboard key placeholders ──────────────────────────────────────────
# Exact env names the dashboard Keys section may save. Anything not in this
# set is rejected with 400 — the endpoint can never write arbitrary vars.
ALLOWED_KEYS = frozenset({
    # messaging channels
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_PASSWORD",
    "LINE_CHANNEL_SECRET", "LINE_ACCESS_TOKEN",
    "WEBHOOK_SECRET", "WEBHOOK_OUT_URL",
    "SIGNAL_NUMBER", "SIGNAL_DATA_DIR",
    "IMAP_HOST", "IMAP_USER", "IMAP_PASS",
    "SMTP_HOST", "SMTP_USER", "SMTP_PASS",
    "TWILIO_SID", "TWILIO_AUTH", "TWILIO_FROM", "TWILIO_WEBHOOK_URL",
    "WHATSAPP_FROM",
    "TEAMS_APP_ID", "TEAMS_APP_PASSWORD", "TEAMS_TENANT",
    "WA_TOKEN", "WA_PHONE_ID", "WA_VERIFY_TOKEN",
    "TWILIO_VOICE_FROM", "VOICE_WS_URL",
    "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET",
    "SLACK_REFRESH_TOKEN", "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET",
    # LLM multi-key pools (SINGULAR holds key #1, PLURAL holds extras)
    "GEMINI_API_KEYS", "NVIDIA_API_KEYS", "GROQ_API_KEYS",
    "CEREBRAS_API_KEYS", "OPEN_ROUTER_API_KEYS", "GITHUB_ACCESS_TOKENS",
    "KILO_API_KEYS",
    # gateway mode switch (reversible, localhost-only like the rest)
    "GATEWAY_GEMINI_ONLY",
    # voice model paths (weights live in S9SharedCode, not the gateway dir)
    "KOKORO_MODEL", "KOKORO_VOICES", "STT_MODEL",
    # gateway-owned integrations
    "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
    "GOOGLE_CALENDAR_REFRESH_TOKEN", "GOOGLE_CALENDAR_CLIENT_ID",
    "GOOGLE_CALENDAR_CLIENT_SECRET",
    "GOOGLE_CALENDAR_TOKEN",
    "GITHUB_TOKEN",
    "NOTION_TOKEN",
    "TAVILY_API_KEY",
})
# GMAIL_TOKEN / GMAIL_REFRESH_TOKEN are deliberately excluded: they are minted
# by gmail_oauth_setup.py, not pasted by hand.

_ENV_PATH = Path(__file__).parent / ".env"
_MAX_KEY_LEN = 2000


def _adaptor_or_404(name: str) -> BaseAdaptor:
    inst = registry.get(name)
    if inst is None:
        raise HTTPException(404, f"unknown channel '{name}'. See GET /v1/channels")
    return inst


def _uses_custom_verify(inst: BaseAdaptor) -> bool:
    return type(inst).verify_webhook is not BaseAdaptor.verify_webhook


# ── inventory ─────────────────────────────────────────────────────────────
@router.get("/v1/channels")
async def list_channels():
    """Live adaptor inventory: configured flags are derived from env presence
    (values never leave the server)."""
    return {"channels": registry.inventory()}


class SendBody(BaseModel):
    to: str
    text: str = ""
    thread_id: Optional[str] = None
    media: Optional[list[dict[str, Any]]] = None
    agent: Optional[str] = None
    session: Optional[str] = None
    trust: Optional[str] = None  # owner|paired|untrusted (default paired for explicit sends)


@router.post("/v1/channels/{name}/send")
async def channel_send(name: str, body: SendBody):
    """Uniform send across all channels. Runs the policy engine first.

    Enforcement: a non-dry-run `deny` verdict returns 403 without sending;
    a non-dry-run `approve` verdict records a pending approval (202-style
    {ok:false, status:"pending"}) instead of sending. Dry-run verdicts are
    recorded on the ledger row and the send proceeds (observe-before-arm).
    """
    inst = _adaptor_or_404(name)
    trust = (body.trust or "paired").lower()
    if trust not in ("owner", "paired", "untrusted"):
        trust = "paired"
    verdict, rule, dry = _policy_check({"trust": trust, "tool": f"send_{name}",
                                         "channel": name, "agent": body.agent,
                                         "session": body.session})
    if verdict == "deny" and not dry:
        db.log_call(provider=f"channel:{name}", model=name, status="error",
                    error=f"policy denied (rule {rule})", call_role="channel",
                    agent=body.agent, session=body.session,
                    channel=name, trust_level=trust,
                    policy_verdict=verdict, policy_rule=rule)
        raise HTTPException(403, f"policy denied by rule '{rule}'")
    if verdict == "approve" and not dry:
        import approvals as _appr
        entry = _appr.create(tool=f"send_{name}", channel=name,
                             args={"to": body.to, "text": (body.text or "")[:500],
                                   "thread_id": body.thread_id},
                             agent=body.agent, session=body.session, rule_id=rule)
        db.log_call(provider=f"channel:{name}", model=name, status="error",
                    error=f"pending approval {entry['id']}",
                    call_role="channel", agent=body.agent, session=body.session,
                    channel=name, trust_level=trust,
                    policy_verdict=verdict, policy_rule=rule)
        return {"ok": False, "status": "pending", "approval_id": entry["id"],
                "channel": name,
                "policy": {"verdict": verdict, "rule": rule}}
    t0 = time.time()
    try:
        rep = await asyncio.to_thread(
            inst.send, to=body.to, text=body.text,
            thread_id=body.thread_id, media=body.media)
        db.log_call(provider=f"channel:{name}", model=name, status="ok",
                    latency_ms=int((time.time() - t0) * 1000),
                    prompt_chars=len(body.text or ""),
                    call_role="channel", agent=body.agent, session=body.session,
                    channel=name, trust_level=trust,
                    policy_verdict=verdict, policy_rule=rule)
        out: dict[str, Any] = {
            "ok": True, "channel": name, "msg_id": rep.msg_id,
            "cost_hint_usd": rep.cost_hint_usd,
            "policy": {"verdict": verdict, "rule": rule, "dry_run": dry},
        }
        # Adaptors stash non-fatal notes (e.g. skipped attachments) in
        # raw["warning"]; surface them top-level so callers actually see them.
        try:
            warning = (rep.raw or {}).get("warning")
        except Exception:
            warning = None
        if warning:
            out["warning"] = warning
        return out
    except NotConfigured:
        return {"ok": False, "channel": name,
                "error": f"channel '{name}' is not configured (see /v1/channels)"}
    except NotIntegrated as e:
        raise HTTPException(501, str(e))
    except PermissionError as e:
        err = _scrub(str(e))[:300]
        db.log_call(provider=f"channel:{name}", model=name, status="error",
                    error=err, latency_ms=int((time.time() - t0) * 1000),
                    call_role="channel", agent=body.agent, session=body.session,
                    channel=name, trust_level=trust,
                    policy_verdict=verdict, policy_rule=rule)
        return {"ok": False, "channel": name, "error": err,
                "hint": "credential expired — refresh via the channel's refresh path"}
    except Exception as e:
        err = _scrub(f"{type(e).__name__}: {e}")[:300]
        db.log_call(provider=f"channel:{name}", model=name, status="error",
                    error=err,
                    latency_ms=int((time.time() - t0) * 1000),
                    call_role="channel", agent=body.agent, session=body.session,
                    channel=name, trust_level=trust,
                    policy_verdict=verdict, policy_rule=rule)
        return {"ok": False, "channel": name, "error": err}


def _policy_check(req: dict[str, Any]) -> tuple[str, str | None, bool]:
    """Evaluate policy in dry-run-safe wrapper.
    Returns (verdict, rule_id, dry_run). Fail-closed on engine errors."""
    try:
        from policy import get_engine
        v = get_engine().evaluate(req)
        return v.action, v.rule_id, v.dry_run
    except Exception:
        return "deny", None, False


# ── inbound webhooks ──────────────────────────────────────────────────────
@router.get("/v1/hooks/{name}")
async def hook_verify(name: str, request: Request):
    """Verification handshakes (Meta hub.mode=subscribe, Slack url_verification)."""
    inst = _adaptor_or_404(name)
    params = dict(request.query_params)
    if name == "whatsapp_meta":
        import os
        from adaptors.whatsapp_meta import WhatsAppMetaAdaptor
        challenge = WhatsAppMetaAdaptor.verify_handshake(
            params, os.getenv("WA_VERIFY_TOKEN", ""))
        if challenge is None:
            raise HTTPException(403, "webhook verification failed")
        return PlainTextResponse(challenge)
    if params.get("type") == "url_verification" and "challenge" in params:
        return PlainTextResponse(params["challenge"])
    return {"ok": True, "channel": name}


@router.post("/v1/hooks/{name}")
async def hook_receive(name: str, request: Request):
    """Inbound channel traffic: verify -> normalize -> trust -> ledger.
    Returns the typed envelope (a future agent loop can poll this)."""
    inst = _adaptor_or_404(name)
    body = await request.body()
    # NOTE: Starlette lowercases header names on receipt, so passing the
    # raw headers to verify_webhook is safe; every verifier lowercases
    # defensively anyway.
    # Slack's url_verification handshake is answered BEFORE signature checks:
    # it carries no data (just echoes our challenge) and must succeed for
    # operators to complete Slack app setup at all.
    if name == "slack":
        try:
            import json as _json
            _probe = _json.loads(body.decode() or "{}")
            if _probe.get("type") == "url_verification" and "challenge" in _probe:
                return PlainTextResponse(str(_probe["challenge"]))
        except ValueError:
            pass
    if _uses_custom_verify(inst):
        ok = await asyncio.to_thread(inst.verify_webhook, dict(request.headers), body)
        if not ok:
            raise HTTPException(403, "webhook signature verification failed")
    ctype = request.headers.get("content-type", "")
    try:
        if "application/json" in ctype:
            import json
            payload = json.loads(body.decode() or "{}")
        elif "application/x-www-form-urlencoded" in ctype:
            from urllib.parse import parse_qsl
            payload = dict(parse_qsl(body.decode()))
        else:
            import json
            try:
                payload = json.loads(body.decode() or "{}")
            except ValueError:
                payload = {"text": body.decode(errors="replace")}
    except Exception:
        raise HTTPException(400, "unparseable webhook body")
    try:
        msg = await asyncio.to_thread(inst.normalize, payload)
    except Exception as e:
        raise HTTPException(422, f"normalize failed: {type(e).__name__}")
    verdict, rule, _dry = _policy_check({"trust": msg.trust_level.value,
                                          "tool": "inbound", "channel": name})
    db.log_call(provider=f"channel:{name}", model=name, status="ok",
                prompt_chars=len(msg.text or ""), call_role="channel",
                agent=f"channel:{name}", session=msg.chat_id or None,
                channel=name, trust_level=msg.trust_level.value,
                policy_verdict=verdict, policy_rule=rule)
    paired_as = _auto_pair_inbound(name, msg)
    return {"ok": True, "message": msg.model_dump(),
            "paired": paired_as,
            "policy": {"verdict": verdict, "rule": rule}}


def _auto_pair_inbound(name: str, msg) -> str:
    """Trust-on-first-contact, so a notification channel can be reached.

    Without this, the pairing store was only ever written by a loopback-only
    control call: a reminder had a configured bot and no destination, and no
    way for a human to supply one from the channel itself. First contact on an
    EMPTY store is recorded as `owner`.

    Deliberately only on an empty store. Any later sender is recorded as
    `paired`, never `owner`, and `resolve_notify_target` prefers `owner` - so
    a stranger who finds the bot cannot redirect your reminders to themselves.
    """
    sender = str(getattr(msg, "sender_id", "") or getattr(msg, "chat_id", "") or "")
    if not sender or not name:
        return ""
    try:
        from adaptors import trust as _trust
        first = not _trust.has_pairings(name)
        if first:
            _trust.pair(name, sender, "owner")
            return "owner"
        if not _trust.is_paired(name, sender):
            _trust.pair(name, sender, "paired")
            return "paired"
    except Exception:
        return ""
    return ""


# ── approvals (policy `approve` verdicts land here) ───────────────────────
@router.get("/v1/approvals")
async def approvals_list():
    """Pending policy approvals (resolve via POST below, then retry)."""
    import approvals as _appr
    return {"approvals": _appr.list_pending()}


class ApprovalResolveBody(BaseModel):
    approve: bool = False


@router.post("/v1/approvals/{approval_id}")
async def approvals_resolve(approval_id: str, body: ApprovalResolveBody):
    import approvals as _appr
    entry = _appr.resolve(approval_id, bool(body.approve))
    if entry is None:
        raise HTTPException(404, "unknown approval id")
    return {"status": entry["status"], "approval": entry}


# ── policy (dry-run observability) ────────────────────────────────────────
@router.get("/v1/policy")
async def policy_show():
    from policy import get_engine
    eng = get_engine()
    return {"dry_run": eng.dry_run_global,
            "defaults": eng.defaults,
            "spend_caps_usd": eng.spend_caps,
            "rules": eng.rules}


class PolicyEvalBody(BaseModel):
    trust: str = "untrusted"
    tool: str = ""
    channel: Optional[str] = None
    agent: Optional[str] = None
    session: Optional[str] = None
    over_spend_cap: bool = False


@router.post("/v1/policy/evaluate")
async def policy_evaluate(body: PolicyEvalBody):
    from policy import get_engine
    v = get_engine().evaluate(body.model_dump())
    return {"allowed": v.allowed, "action": v.action, "rule_id": v.rule_id,
            "reason": v.reason, "dry_run": v.dry_run}


@router.post("/v1/policy/reload")
async def policy_reload():
    from policy import reload_engine
    eng = reload_engine()
    return {"status": "ok", "rules": len(eng.rules), "dry_run": eng.dry_run_global}


# ── spend (unified ledger view) ───────────────────────────────────────────
@router.get("/v1/spend")
async def spend(session: Optional[str] = None, agent: Optional[str] = None):
    """Unified spend: by_agent rollup + policy caps + totals in one shape."""
    from policy import get_engine
    import pricing as _pricing
    raw = db.by_agent(session=session)
    if agent:
        raw = {agent: raw.get(agent, [])}
    rows: list[dict[str, Any]] = []
    for ag, rs in raw.items():
        for r in rs:
            r2 = dict(r)
            r2["dollars"] = _pricing.estimate_usd(
                r["provider"], r.get("in_tok") or 0, r.get("out_tok") or 0)
            rows.append(r2)
    rows.sort(key=lambda x: x.get("calls", 0), reverse=True)
    totals = {"calls": sum(r.get("calls", 0) for r in rows),
              "in_tok": sum(r.get("in_tok", 0) for r in rows),
              "out_tok": sum(r.get("out_tok", 0) for r in rows),
              "dollars": round(sum(float(r.get("dollars") or 0) for r in rows), 6)}
    caps = get_engine().spend_caps
    over_cap = False
    try:
        # Scoped rollups compare against the matching daily cap.
        cap = (caps.get("per_agent_per_day") if agent
               else caps.get("per_session_per_day") if session
               else None)
        over_cap = bool(cap is not None and totals["dollars"] > float(cap))
    except (TypeError, ValueError):
        over_cap = False
    return {"rows": rows, "totals": totals, "session": session, "agent": agent,
            "spend_caps_usd": caps, "over_cap": over_cap}


# ── control plane (out-of-band, localhost-first) ──────────────────────────
def _loopback_only(request: Request) -> None:
    import os
    # Trusts request.client.host, which is correct for direct localhost
    # binds. Behind a reverse proxy this sees the proxy's IP instead — do
    # not expose the control plane through a proxy without forwarding the
    # real client IP (X-Forwarded-For) and re-checking here.
    host = (request.client.host if request.client else "")
    if host in ("127.0.0.1", "::1", "localhost"):
        return
    if os.getenv("GLC_KILL_ALLOW_REMOTE") == "1":
        return
    raise HTTPException(403, "control plane is loopback-only")


@router.get("/v1/control/presence")
async def control_presence():
    inv = registry.inventory()
    live = sum(1 for c in inv if c.get("configured"))
    return {"up": True, "version": "v9", "ts": time.time(),
            "channels_total": len(inv), "channels_live": live}


@router.get("/v1/control/notify-target")
async def control_notify_target(request: Request):
    """Where a local notification should be delivered, unmasked.

    Loopback-only, for the same reason `pair` is: it hands out a real chat id.
    The display path (`/v1/control/presence` -> `trust.list_paired`) stays
    masked, and this exists so a scheduled task can find a destination instead
    of silently going nowhere.
    """
    _loopback_only(request)
    channel = request.query_params.get("channel") or "telegram"
    try:
        from adaptors import trust as _trust
        target = _trust.resolve_notify_target(channel)
    except Exception as e:
        raise HTTPException(500, f"trust store unavailable: {type(e).__name__}")
    inventory = registry.inventory()
    live = any(
        str(c.get("name") or "") == channel and c.get("configured")
        for c in (inventory or []) if isinstance(c, dict))
    return {"channel": channel, "target": target or "", "configured": bool(live)}


@router.post("/v1/control/pair")
async def control_pair(body: dict, request: Request):
    """Pair a sender: {channel, sender_id, role=paired}. Loopback-only."""
    _loopback_only(request)
    from adaptors import trust as _trust
    try:
        entry = _trust.pair(body.get("channel", ""), body.get("sender_id", ""),
                            body.get("role", "paired"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", "entry": entry}


@router.post("/v1/control/kill")
async def control_kill(request: Request):
    """Emergency stop. Disabled unless ARIA_ENABLE_KILL=1 (deliberate opt-in);
    always loopback-only. Stops the gateway process; supervisors restart it."""
    import os
    import signal
    _loopback_only(request)
    if os.getenv("ARIA_ENABLE_KILL") != "1":
        raise HTTPException(403, "kill switch disabled (set ARIA_ENABLE_KILL=1 to arm)")
    try:
        db.log_call(provider="control", model="kill", status="ok",
                    call_role="control", agent="operator")
    except Exception:
        pass
    os.kill(os.getpid(), signal.SIGTERM)
    return {"status": "stopping"}


# ── dashboard key placeholders (loopback-only writes, open reads) ──────────
@router.get("/v1/config/keys")
async def config_keys():
    """Names-only key status for the dashboard Keys section.

    Returns [{name, set}] — values never leave the server. Open (no loopback
    gate) because it leaks nothing beyond what check_keys.py already shows.
    """
    pools = {}
    for provider in PROVIDER_POOLS:
        try:
            members = _pool_members(provider)
            key_count = _pool_key_count(provider)
        except Exception:
            members = []
            key_count = 0
        pools[provider] = {"count": key_count, "members": members}
    return {"keys": [
        {"name": k, "set": bool(os.getenv(k))}
        for k in sorted(ALLOWED_KEYS)
    ], "pools": pools}


class KeysBody(BaseModel):
    keys: dict[str, str]


def _write_env_keys(pairs: dict[str, str | None]) -> None:
    """Upsert NAME=value lines in the gateway .env (same pattern as
    gmail_oauth_setup._write_tokens). Creates the file if missing.
    A None value REMOVES the line (used when a pool shrinks to one key)."""
    text = _ENV_PATH.read_text(encoding="utf-8") if _ENV_PATH.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    for key, val in pairs.items():
        if val is None:
            text = re.sub(rf"^\s*{re.escape(key)}=.*$\n?", "", text, flags=re.M)
            continue
        line = f"{key}={val}"
        if re.search(rf"^\s*{re.escape(key)}=", text, re.M):
            text = re.sub(rf"^\s*{re.escape(key)}=.*$", line, text, flags=re.M)
        else:
            text += line + "\n"
    _ENV_PATH.write_text(text, encoding="utf-8")


# provider → (SINGULAR var holding key #1, PLURAL var holding extras).
# Mirrors providers._key_list so the dashboard and the pool builder agree.
PROVIDER_POOLS: dict[str, tuple[str, str]] = {
    "gemini": ("GEMINI_API_KEY", "GEMINI_API_KEYS"),
    "nvidia": ("NVIDIA_API_KEY", "NVIDIA_API_KEYS"),
    "groq": ("GROQ_API_KEY", "GROQ_API_KEYS"),
    "cerebras": ("CEREBRAS_API_KEY", "CEREBRAS_API_KEYS"),
    "openrouter": ("OPEN_ROUTER_API_KEY", "OPEN_ROUTER_API_KEYS"),
    "github": ("GITHUB_ACCESS_TOKEN", "GITHUB_ACCESS_TOKENS"),
    "kilo": ("KILO_API_KEY", "KILO_API_KEYS"),
}


def _pool_key_count(provider: str) -> int:
    """Number of KEYS in a provider's pool (not members: one Gemini key
    yields two members, gemini + gemini35lite). Count only, never values."""
    import providers as _providers
    single, plural = PROVIDER_POOLS[provider]
    return len(_providers._key_list(single, plural))


def _pool_members(provider: str) -> list[str]:
    """Ordered pool member names for a provider (canonical + siblings),
    derived from env WITHOUT exposing values. Count only."""
    import providers as _providers
    single, plural = PROVIDER_POOLS[provider]
    keys = _providers._key_list(single, plural)
    members = [provider if i == 1 else f"{provider}-{i}" for i in range(1, len(keys) + 1)]
    if provider == "gemini":
        # Gemini keys yield a gemini + gemini35lite pair each.
        members = [m for pair in
                   ([g, g.replace("gemini", "gemini35lite")] for g in members)
                   for m in pair]
    return members


@router.post("/v1/control/keys")
async def control_keys(body: KeysBody, request: Request):
    """Save pasted keys from the dashboard placeholders to the gateway .env.

    Loopback-only. Accepts {keys: {NAME: value}}; every NAME must be in
    ALLOWED_KEYS and every value a non-empty string (cap 2000 chars).
    Values are applied to the live process env AND persisted to .env.
    Never echoes values back — response carries names only.
    Most channel keys take effect live; restart the gateway to be certain
    (worker keys are baked at startup).
    """
    _loopback_only(request)
    pairs = body.keys or {}
    if not pairs:
        raise HTTPException(400, "no keys supplied")
    bad = [k for k in pairs if k not in ALLOWED_KEYS]
    if bad:
        raise HTTPException(400, f"unknown keys: {', '.join(sorted(bad))}")
    clean: dict[str, str] = {}
    for k, v in pairs.items():
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(400, f"empty value for '{k}'")
        v = v.strip()
        if len(v) > _MAX_KEY_LEN:
            raise HTTPException(400, f"value too long for '{k}'")
        if "\n" in v or "\r" in v:
            raise HTTPException(400, f"value for '{k}' must be a single line")
        clean[k] = v
    try:
        _write_env_keys(clean)
    except Exception as e:
        raise HTTPException(500, f"failed to write .env: {type(e).__name__}")
    for k, v in clean.items():
        os.environ[k] = v
    try:
        db.log_call(provider="control", model="keys", status="ok",
                    call_role="control", agent="operator")
    except Exception:
        pass
    return {"status": "ok", "saved": sorted(clean.keys()),
            "note": "saved to gateway .env and live env; restart gateway to be certain all readers pick them up"}


class PoolBody(BaseModel):
    provider: str
    keys: list[str]


@router.post("/v1/control/pool")
async def control_pool(body: PoolBody, request: Request):
    """Save a provider's COMPLETE key set (ordered): keys[0] → SINGULAR var,
    keys[1:] → comma-joined PLURAL var (removed when only one key remains).

    Loopback-only. This REPLACES the provider's whole set — the UI shows
    the stored count first so pastes cover every slot. Values are validated
    (non-empty single lines, cap 2000 chars, max 20 keys) and never echoed;
    the response carries member names only. Restart the gateway afterwards.
    """
    _loopback_only(request)
    provider = (body.provider or "").lower()
    if provider not in PROVIDER_POOLS:
        raise HTTPException(400, f"unknown provider '{body.provider}'. Try one of: {sorted(PROVIDER_POOLS)}")
    seen: set[str] = set()
    clean: list[str] = []
    for v in body.keys or []:
        if not isinstance(v, str) or not v.strip():
            continue
        v = v.strip()
        if len(v) > _MAX_KEY_LEN:
            raise HTTPException(400, f"value too long for '{provider}' pool")
        if "\n" in v or "\r" in v:
            raise HTTPException(400, f"values for '{provider}' pool must be single-line")
        if v not in seen:
            seen.add(v)
            clean.append(v)
    if not clean:
        raise HTTPException(400, f"no usable keys for '{provider}' pool")
    if len(clean) > 20:
        raise HTTPException(400, f"too many keys for '{provider}' pool (max 20)")
    single, plural = PROVIDER_POOLS[provider]
    pairs: dict[str, str | None] = {single: clean[0],
                                    plural: ",".join(clean[1:]) if len(clean) > 1 else None}
    try:
        _write_env_keys(pairs)
    except Exception as e:
        raise HTTPException(500, f"failed to write .env: {type(e).__name__}")
    os.environ[single] = clean[0]
    if len(clean) > 1:
        os.environ[plural] = ",".join(clean[1:])
    else:
        os.environ.pop(plural, None)
    try:
        db.log_call(provider="control", model="pool", status="ok",
                    call_role="control", agent="operator")
    except Exception:
        pass
    return {"status": "ok", "provider": provider,
            "members": _pool_members(provider),
            "note": "pool saved to gateway .env and live env; restart gateway to activate"}
