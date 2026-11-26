"""POST /v1/integrations/{service}/{op} — keyed services the agent calls.

The agent's MCP tools are thin HTTP callers over this router; credentials
never leave the gateway process. Every call runs the policy engine first
(dry-run verdict recorded) and is ledger-logged with agent/session.
Response shapes are byte-identical to the old agent-local tools.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db

from scrub import scrub as _scrub

router = APIRouter()


class IntegrationBody(BaseModel):
    args: dict[str, Any] = {}
    agent: Optional[str] = None
    session: Optional[str] = None
    trust: Optional[str] = None


def _policy(req: dict[str, Any]) -> tuple[str, str | None, bool]:
    try:
        from policy import get_engine
        v = get_engine().evaluate(req)
        return v.action, v.rule_id, v.dry_run
    except Exception:
        return "deny", None, False



async def _run(service: str, op: str, args: dict[str, Any]):
    """Dispatch to the service module in a worker thread (blocking httpx)."""
    if service == "gmail":
        from integrations import gmail as g
        fn = {"send": g.send_email, "query": g.query, "refresh": g.refresh,
              "profile": g.profile}.get(op)
    elif service == "zoho_mail":
        from integrations import zoho_mail as z
        fn = {"send": z.send_email, "query": z.query, "refresh": z.refresh,
              "profile": z.profile, "folders": z.list_folders, "reply": z.reply,
              "mark": z.mark, "move": z.move, "delete": z.delete}.get(op)
    elif service == "zoho_calendar":
        from integrations import zoho_calendar as zc
        fn = {"list_calendars": zc.list_calendars, "list_events": zc.list_events,
              "create": zc.create_event}.get(op)
    elif service == "ntfy":
        from integrations import ntfy as n
        fn = {"send": n.send, "history": n.history}.get(op)
    elif service == "zoho_cliq":
        from integrations import cliq as c
        fn = {"send": c.send}.get(op)
    elif service == "bluesky":
        from integrations import bluesky as b
        fn = {"send": b.send}.get(op)
    elif service == "mastodon":
        from integrations import mastodon as m
        fn = {"send": m.send}.get(op)
    elif service == "google_chat":
        from integrations import google_chat as gc
        fn = {"send": gc.send}.get(op)
    elif service == "calendar":
        from integrations import calendar as c
        fn = {"create": c.create_event, "list": c.list_events, "refresh": c.refresh}.get(op)
    elif service == "github":
        from integrations import github as g
        fn = {"query": g.query, "call": g.call}.get(op)
    elif service == "notion":
        from integrations import notion as n
        fn = {"query": n.query, "call": n.call}.get(op)
    elif service == "telegram":
        from integrations import telegram_api as t
        fn = {"call": t.call}.get(op)
    elif service == "discord":
        from integrations import discord_api as d
        fn = {"call": d.call, "invite_url": d.invite_url}.get(op)
    elif service == "slack":
        from integrations import slack as s
        fn = {"history": s.history, "refresh": s.refresh}.get(op)
    elif service == "websearch":
        from integrations import websearch as ws
        fn = {"search": ws.search, "usage": lambda: ws.usage(),
              # Licensed pictures. Separate ops, because `search` returns a bare
              # list and these return a dict carrying a licence field the caller
              # must not lose on its way into a caption.
              "search_images": ws.search_images,
              "image_usage": ws.image_usage}.get(op)
    else:
        fn = None
    if fn is None:
        raise HTTPException(404, f"unknown integration {service}/{op}")
    try:
        return await asyncio.to_thread(fn, **(args or {}))
    except TypeError as e:
        raise HTTPException(400, f"bad args for {service}/{op}: {e}")


@router.post("/v1/integrations/{service}/{op}")
async def integration_call(service: str, op: str, body: IntegrationBody):
    service, op = service.lower(), op.lower()
    trust = (body.trust or "paired").lower()
    if trust not in ("owner", "paired", "untrusted"):
        trust = "paired"
    verdict, rule, dry = _policy({"trust": trust, "tool": f"{service}_{op}",
                                  "channel": service, "agent": body.agent,
                                  "session": body.session})
    if verdict == "deny" and not dry:
        db.log_call(provider=f"integration:{service}", model=op,
                    status="error", error=f"policy denied (rule {rule})",
                    call_role="integration", agent=body.agent,
                    session=body.session, channel=service,
                    trust_level=trust, policy_verdict=verdict,
                    policy_rule=rule)
        raise HTTPException(403, f"policy denied by rule '{rule}'")
    if verdict == "approve" and not dry:
        import approvals as _appr
        entry = _appr.create(tool=f"{service}_{op}", channel=service,
                             args={k: (v[:200] if isinstance(v, str) else v)
                                   for k, v in (body.args or {}).items()},
                             agent=body.agent, session=body.session,
                             rule_id=rule)
        db.log_call(provider=f"integration:{service}", model=op,
                    status="error", error=f"pending approval {entry['id']}",
                    call_role="integration", agent=body.agent,
                    session=body.session, channel=service,
                    trust_level=trust, policy_verdict=verdict,
                    policy_rule=rule)
        return {"ok": False, "status": "pending",
                "approval_id": entry["id"],
                "policy": {"verdict": verdict, "rule": rule}}
    t0 = time.time()
    try:
        result = await _run(service, op, body.args)
    except HTTPException:
        raise
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # websearch returns a bare list (not a dict) — wrap it, preserving shape.
    if isinstance(result, list):
        result = {"ok": True, "results": result}
    if not isinstance(result, dict):
        result = {"ok": False, "error": "integration returned non-dict"}
    if isinstance(result.get("error"), str):
        result = dict(result, error=_scrub(result["error"])[:300])
    db.log_call(provider=f"integration:{service}", model=op,
                status="ok" if result.get("ok") else "error",
                error=None if result.get("ok") else str(result.get("error", ""))[:300],
                latency_ms=int((time.time() - t0) * 1000),
                call_role="integration", agent=body.agent, session=body.session,
                channel=service, trust_level=trust,
                policy_verdict=verdict, policy_rule=rule)
    result = dict(result)
    result.setdefault("policy", {"verdict": verdict, "rule": rule, "dry_run": dry})
    return result


@router.get("/v1/integrations")
async def integration_inventory():
    """Which keyed services are configured (names only, never values)."""
    import os
    keys = {
        "gmail": ["GMAIL_TOKEN"],
        "zoho_mail": ["ZOHO_TOKEN"],
        "zoho_calendar": ["ZOHO_TOKEN"],
        "calendar": ["GOOGLE_CALENDAR_TOKEN"],
        "github": ["GITHUB_TOKEN"],
        "notion": ["NOTION_TOKEN"],
        "websearch": ["TAVILY_API_KEY"],
        "ntfy": [],
        "zoho_cliq": ["ZOHO_CLIQ_TOKEN"],
        "bluesky": ["BSKY_HANDLE", "BSKY_APP_PASSWORD"],
        "mastodon": ["MASTODON_TOKEN", "MASTODON_INSTANCE"],
        "google_chat": ["GOOGLE_CHAT_WEBHOOK"],
    }
    out = []
    for svc, ks in keys.items():
        # Websearch works keyless via the DDG fallback (Tavily only
        # upgrades quality), so it reports live with an explanatory note.
        if svc == "websearch":
            out.append({"service": svc, "configured": True,
                        "required_keys": ks,
                        "note": "works without a key (DDG fallback); "
                                "TAVILY_API_KEY upgrades quality"})
            continue
        out.append({"service": svc, "configured": all(os.getenv(k) for k in ks) if ks else True,
                    "required_keys": ks})
    return {"integrations": out}
