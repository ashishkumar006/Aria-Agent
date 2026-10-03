"""One-click deployment self-test (drives the dashboard button).

POST /v1/control/deploy-test?full=false — loopback-only.
  quick (default): every area, zero LLM spend, zero side effects
    (telegram probes an invalid id, gmail only lists, writes skipped,
    memory uses a throwaway session that is wiped afterwards).
  full=true: quick + live chat per worker (max_tokens=5, tiny traceable
    spend) + vision + TTS synthesis.

Response: {results: [{area, name, status, detail}], summary: {...}} with
statuses WORKING | FAILED | UNKEYED | SKIPPED. Values never leave the
server — details carry counts and shapes only.
"""
from __future__ import annotations

import asyncio
import base64
import os
import struct
import time
import zlib

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

SESSION = "deploy-ui"


def _base_url() -> str:
    port = os.getenv("GATEWAY_V9_PORT", "8109")
    return f"http://127.0.0.1:{port}"


def _red_dot_url() -> str:
    raw = b"\x00\xff\x00\x00"

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


class DeployResult(BaseModel):
    area: str
    name: str
    status: str
    detail: str = ""


async def _run_quick(base: str, out: list[DeployResult]) -> None:
    def ok(area, name, detail=""):
        out.append(DeployResult(area=area, name=name, status="WORKING", detail=detail[:160]))

    def bad(area, name, detail=""):
        out.append(DeployResult(area=area, name=name, status="FAILED", detail=detail[:160]))

    async with httpx.AsyncClient(timeout=90) as c:
        # core
        try:
            p = (await c.get(f"{base}/v1/control/presence")).json()
            chans = (await c.get(f"{base}/v1/channels")).json()["channels"]
            st = (await c.get(f"{base}/v1/status")).json()
            ok("core", "presence", f"{p.get('channels_live')}/{p.get('channels_total')} channels live")
            ok("core", "status", f"{len(st['live'])} workers, gemini_only={st.get('gemini_only')}")
            workers = sorted(st["live"].keys())
        except Exception as e:
            bad("core", "presence/status", f"{type(e).__name__}: {e}")
            workers = []
        # embed (local, free)
        try:
            e = (await c.post(f"{base}/v1/embed", json={
                "text": "deployment probe", "task_type": "retrieval_query",
                "agent": "deploy-ui", "session": SESSION})).json()
            if e.get("dim") == 768:
                ok("workers", "embed", f"dim=768 via {e.get('provider')}")
            else:
                bad("workers", "embed", str(e)[:120])
        except Exception as e:
            bad("workers", "embed", f"{type(e).__name__}: {e}")
        # channels
        try:
            for ch in chans:
                if ch.get("configured"):
                    ok("channels", ch["name"], "keys present")
                else:
                    need = (ch.get("required_keys") or ["none"])[0]
                    out.append(DeployResult(area="channels", name=ch["name"],
                                            status="UNKEYED", detail=f"needs {need}"))
        except Exception as e:
            bad("channels", "inventory", f"{type(e).__name__}: {e}")
        # telegram zero-delivery probe (bad id proves key + path)
        try:
            t = (await c.post(f"{base}/v1/channels/telegram/send",
                              json={"to": "0", "text": "deploy probe"})).json()
            if t.get("ok") is False and "chat not found" in str(t.get("error", "")).lower():
                ok("channels", "telegram send-path", "bad-id rejected, key valid")
            else:
                bad("channels", "telegram send-path", str(t)[:120])
        except Exception as e:
            bad("channels", "telegram send-path", f"{type(e).__name__}: {e}")
        # integrations, read-only
        for svc, op, args, label in [
            ("gmail", "query", {"api_method": "list", "max_results": 1}, "gmail list"),
            ("github", "query", {"api_method": "list_repos"}, "github list_repos"),
            ("notion", "query", {"api_method": "list_pages"}, "notion list_pages"),
            ("websearch", "search", {"query": "python programming", "max_results": 1}, "websearch"),
        ]:
            try:
                d = (await c.post(f"{base}/v1/integrations/{svc}/{op}",
                                  json={"args": args, "agent": "deploy-ui",
                                        "session": SESSION})).json()
                if d.get("ok") is True and (svc not in ("websearch",) or d.get("results")):
                    ok("integrations", label, "read ok")
                else:
                    bad("integrations", label, str(d.get("error"))[:120])
            except Exception as e:
                bad("integrations", label, f"{type(e).__name__}: {e}")
        out.append(DeployResult(area="integrations", name="calendar create", status="SKIPPED",
                                detail="write op — would create a real event"))
        out.append(DeployResult(area="integrations", name="gmail send", status="SKIPPED",
                                detail="write op — would send a real email"))
        # memory roundtrip, self-cleaning
        marker = f"deploy-ui marker {int(time.time())}"
        try:
            r = await c.post(f"{base}/v1/memory/remember", json={
                "kind": "fact", "descriptor": marker, "keywords": ["deploy", "probe"],
                "value": {}, "source": "deploy-ui", "run_id": "deploy-ui",
                "session_id": SESSION})
            assert r.status_code == 200, r.text[:120]
            s = (await c.post(f"{base}/v1/memory/search", json={
                "query": "deploy-ui marker", "top_k": 5, "session_id": SESSION})).json()
            if any(marker in str(h) for h in s.get("items", [])):
                ok("memory", "remember+search", "self-recall ok")
            else:
                bad("memory", "remember+search", "written fact not recalled")
        except Exception as e:
            bad("memory", "remember+search", f"{type(e).__name__}: {e}")
        finally:
            try:
                await c.delete(f"{base}/v1/memory", params={"session_id": SESSION, "confirm": "wipe"})
            except Exception:
                pass
        # voice list, policy, observability
        try:
            v = (await c.get(f"{base}/v1/tts/voices")).json()
            if v.get("voices"):
                ok("voice", "voices", f"{len(v['voices'])} voices")
            else:
                bad("voice", "voices", "empty list")
        except Exception as e:
            bad("voice", "voices", f"{type(e).__name__}: {e}")
        try:
            p = (await c.post(f"{base}/v1/policy/evaluate",
                              json={"trust": "untrusted", "tool": "send_telegram"})).json()
            ok("system", "policy evaluate", f"verdict={p.get('action')}") if "action" in p else bad(
                "system", "policy evaluate", str(p)[:120])
        except Exception as e:
            bad("system", "policy evaluate", f"{type(e).__name__}: {e}")
        try:
            for u in ("/v1/spend", "/v1/cost/by_agent", "/v1/calls?limit=1"):
                r = await c.get(f"{base}{u}")
                assert r.status_code == 200, u
            ok("system", "spend/cost/calls", "ledger readable")
        except Exception as e:
            bad("system", "spend/cost/calls", f"{type(e).__name__}: {e}")
    # attach worker roster (no calls in quick mode)
    for w in workers:
        out.append(DeployResult(area="workers", name=f"chat:{w}", status="SKIPPED",
                                detail="live call only in full mode"))


async def _run_full(base: str, out: list[DeployResult]) -> None:
    await _run_quick(base, out)
    # drop the SKIPPED placeholders — real calls follow
    out[:] = [r for r in out if not (r.area == "workers" and r.name.startswith("chat:"))]
    async with httpx.AsyncClient(timeout=180) as c:
        try:
            st = (await c.get(f"{base}/v1/status")).json()
            workers = sorted(st["live"].keys())
        except Exception as e:
            out.append(DeployResult(area="workers", name="chat", status="FAILED",
                                    detail=f"status unreadable: {e}"))
            return
        for name in workers:
            try:
                r = await c.post(f"{base}/v1/chat", json={
                    "prompt": "Reply with the single word: ok", "provider": name,
                    "max_tokens": 5, "agent": "deploy-ui", "session": SESSION})
                d = r.json()
                if r.status_code == 200 and d.get("text"):
                    out.append(DeployResult(area="workers", name=f"chat:{name}",
                                            status="WORKING",
                                            detail=f"{d.get('latency_ms')}ms"))
                else:
                    out.append(DeployResult(area="workers", name=f"chat:{name}",
                                            status="FAILED",
                                            detail=f"HTTP {r.status_code}: {r.text[:100]}"))
            except Exception as e:
                out.append(DeployResult(area="workers", name=f"chat:{name}",
                                        status="FAILED", detail=f"{type(e).__name__}: {e}"))
        try:
            r = await c.post(f"{base}/v1/vision", json={
                "image": _red_dot_url(),
                "prompt": "What color is this single pixel? Answer one word.",
                "max_tokens": 10, "agent": "deploy-ui", "session": SESSION})
            d = r.json()
            if r.status_code == 200 and (d.get("text") or d.get("parsed")):
                out.append(DeployResult(area="workers", name="vision", status="WORKING",
                                        detail=str(d.get("text") or d.get("parsed"))[:60]))
            else:
                out.append(DeployResult(area="workers", name="vision", status="FAILED",
                                        detail=f"HTTP {r.status_code}: {r.text[:100]}"))
        except Exception as e:
            out.append(DeployResult(area="workers", name="vision", status="FAILED",
                                    detail=f"{type(e).__name__}: {e}"))
        try:
            r = await c.post(f"{base}/v1/tts", json={"text": "deployment probe"})
            if r.status_code == 200 and len(r.content) > 1000:
                out.append(DeployResult(area="voice", name="tts synth", status="WORKING",
                                        detail=f"{len(r.content)} wav bytes"))
            else:
                out.append(DeployResult(area="voice", name="tts synth", status="FAILED",
                                        detail=f"HTTP {r.status_code}: {r.text[:100]}"))
        except Exception as e:
            out.append(DeployResult(area="voice", name="tts synth", status="FAILED",
                                    detail=f"{type(e).__name__}: {e}"))


async def _run_system(base: str, out: list[DeployResult]) -> None:
    """System-section scope only: roster, memory, policy, spend, voices,
    key pools. No LLM calls, no channel/integration traffic."""

    def ok(name, detail=""):
        out.append(DeployResult(area="system", name=name, status="WORKING", detail=detail[:160]))

    def bad(name, detail=""):
        out.append(DeployResult(area="system", name=name, status="FAILED", detail=detail[:160]))

    async with httpx.AsyncClient(timeout=90) as c:
        try:
            st = (await c.get(f"{base}/v1/status")).json()
            ok("worker roster", f"{len(st['live'])} workers: {', '.join(sorted(st['live']))[:100]}")
        except Exception as e:
            bad("worker roster", f"{type(e).__name__}: {e}")
        try:
            m = (await c.get(f"{base}/v1/memory/stats")).json()
            total = (m.get("legacy") or {}).get("items", 0) + sum(
                (v or {}).get("items", 0) for v in (m.get("drawers") or {}).values())
            ok("memory stats", f"{total} records")
        except Exception as e:
            bad("memory stats", f"{type(e).__name__}: {e}")
        marker = f"deploy-ui marker {int(time.time())}"
        try:
            r = await c.post(f"{base}/v1/memory/remember", json={
                "kind": "fact", "descriptor": marker, "keywords": ["deploy", "probe"],
                "value": {}, "source": "deploy-ui", "run_id": "deploy-ui",
                "session_id": SESSION})
            assert r.status_code == 200, r.text[:120]
            s = (await c.post(f"{base}/v1/memory/search", json={
                "query": "deploy-ui marker", "top_k": 5, "session_id": SESSION})).json()
            if any(marker in str(h) for h in s.get("items", [])):
                ok("memory roundtrip", "self-recall ok")
            else:
                bad("memory roundtrip", "written fact not recalled")
        except Exception as e:
            bad("memory roundtrip", f"{type(e).__name__}: {e}")
        finally:
            try:
                await c.delete(f"{base}/v1/memory", params={"session_id": SESSION, "confirm": "wipe"})
            except Exception:
                pass
        try:
            p = (await c.post(f"{base}/v1/policy/evaluate",
                              json={"trust": "untrusted", "tool": "send_telegram"})).json()
            if "action" in p:
                ok("policy evaluate", f"verdict={p['action']}")
            else:
                bad("policy evaluate", str(p)[:120])
        except Exception as e:
            bad("policy evaluate", f"{type(e).__name__}: {e}")
        try:
            s = (await c.get(f"{base}/v1/spend")).json()
            ok("spend totals", f"{(s.get('totals') or {}).get('calls', 0)} calls logged")
        except Exception as e:
            bad("spend totals", f"{type(e).__name__}: {e}")
        try:
            v = (await c.get(f"{base}/v1/tts/voices")).json()
            if v.get("voices"):
                ok("voices", f"{len(v['voices'])} voices")
            else:
                bad("voices", "empty list")
        except Exception as e:
            bad("voices", f"{type(e).__name__}: {e}")
        try:
            k = (await c.get(f"{base}/v1/config/keys")).json()
            pools = k.get("pools", {})
            ok("key pools", ", ".join(f"{p}={v.get('count', 0)}" for p, v in sorted(pools.items()))[:140])
        except Exception as e:
            bad("key pools", f"{type(e).__name__}: {e}")


async def _run_ledger(base: str, out: list[DeployResult]) -> None:
    """Ledger-section scope only: the three read endpoints the page needs."""

    def ok(name, detail=""):
        out.append(DeployResult(area="ledger", name=name, status="WORKING", detail=detail[:160]))

    def bad(name, detail=""):
        out.append(DeployResult(area="ledger", name=name, status="FAILED", detail=detail[:160]))

    async with httpx.AsyncClient(timeout=30) as c:
        for name, url in [("calls", "/v1/calls?limit=1"),
                          ("spend", "/v1/spend"),
                          ("cost by agent", "/v1/cost/by_agent"),
                          ("tools usage", "/v1/tools/usage")]:
            try:
                r = await c.get(f"{base}{url}")
                if r.status_code == 200:
                    ok(name, "readable")
                else:
                    bad(name, f"HTTP {r.status_code}")
            except Exception as e:
                bad(name, f"{type(e).__name__}: {e}")


@router.post("/v1/control/deploy-test")
async def deploy_test(request: Request, full: bool = False, scope: str = "quick"):
    """Run the deployment self-test. Loopback-only (dashboard host).

    scope=quick (default): every area, no LLM spend, no side effects.
    scope=system: only System-section APIs (roster, memory, policy,
      spend, voices, key pools).
    scope=ledger: only the ledger reads (calls, spend, cost).
    full=true: everything + live chat/vision/TTS (tiny token spend).
    """
    from channels_api import _loopback_only
    _loopback_only(request)
    import db as _db
    out: list[DeployResult] = []
    try:
        if full:
            await _run_full(_base_url(), out)
        elif scope == "system":
            await _run_system(_base_url(), out)
        elif scope == "ledger":
            await _run_ledger(_base_url(), out)
        elif scope == "quick":
            await _run_quick(_base_url(), out)
        else:
            raise HTTPException(400, f"unknown scope '{scope}'. Try quick, system or ledger.")
    except HTTPException:
        raise
    except Exception as e:
        out.append(DeployResult(area="core", name="harness", status="FAILED",
                                detail=f"{type(e).__name__}: {e}"))
    rows = [r.model_dump() for r in out]
    counts = {"WORKING": 0, "FAILED": 0, "UNKEYED": 0, "SKIPPED": 0, "LIVE": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    try:
        _db.log_call(provider="control", model="deploy-test", status="ok",
                     call_role="control", agent="operator")
    except Exception:
        pass
    return {"results": rows, "summary": counts, "full": full}


@router.get("/v1/control/deploy-test/methods")
async def deploy_test_methods():
    return {"modes": {
        "quick": "no LLM spend, no side effects (~20-40s)",
        "full": "quick + live chat/vision/tts (tiny token spend, ~1-3 min)"}}


# ── per-channel safe probes ─────────────────────────────────────────────
# Every probe is read-only: credential checks, auth.test-style endpoints,
# logins without sends, or invalid-id sends that providers reject with
# zero delivery. Values never leave the server — only WORKING / FAILED /
# UNKEYED plus a scrubbed detail string.
class ChannelTestBody(BaseModel):
    channel: str
    rotate: bool = False


def _missing(*names: str) -> list[str]:
    return [n for n in names if not (os.getenv(n) or "").strip()]


# Integrations reachable by the same Test button (safe read-only ops).
# calendar has no read op — key presence only, create is never probed.
_INTEGRATION_PROBES: dict[str, tuple[str, str, dict]] = {
    "notion": ("notion", "query", {"api_method": "list_pages"}),
    "github": ("github", "query", {"api_method": "list_repos"}),
    "websearch": ("websearch", "search", {"query": "python programming", "max_results": 1}),
}


async def _probe_integration(name: str) -> DeployResult:
    from integrations_api import _run as _irun
    if name == "calendar":
        if _missing("GOOGLE_CALENDAR_TOKEN"):
            return DeployResult(area="integrations", name=name, status="UNKEYED",
                                detail="needs GOOGLE_CALENDAR_TOKEN")
        return DeployResult(area="integrations", name=name, status="WORKING",
                            detail="key present (create never probed — would book a real event)")
    svc, op, args = _INTEGRATION_PROBES[name]
    try:
        res = await asyncio.wait_for(_irun(svc, op, dict(args)), timeout=90)
    except Exception as e:
        return DeployResult(area="integrations", name=name, status="FAILED",
                            detail=f"{type(e).__name__}: {e}"[:120])
    if isinstance(res, list):
        res = {"ok": True, "results": res}
    if res.get("ok") is True:
        count = ""
        for k in ("pages", "repos", "results"):
            if isinstance(res.get(k), list):
                count = f" ({len(res[k])} found)"
                break
        if svc == "weather":
            count = ""
        return DeployResult(area="integrations", name=name, status="WORKING",
                            detail=f"read ok{count}")
    err = str(res.get("error", "unknown"))[:120]
    if "not set" in err:
        return DeployResult(area="integrations", name=name, status="UNKEYED", detail=err)
    return DeployResult(area="integrations", name=name, status="FAILED", detail=err)


async def _probe_channel(name: str, base: str, timeout: float = 15.0,
                         rotate: bool = False) -> DeployResult:
    miss = lambda *ns: _missing(*ns)  # noqa: E731
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            if name == "telegram":
                if miss("TELEGRAM_BOT_TOKEN"):
                    return DeployResult(area="channels", name=name, status="UNKEYED", detail="needs TELEGRAM_BOT_TOKEN")
                r = await c.post(f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage",
                                 json={"chat_id": 0, "text": "deploy probe"})
                if r.status_code == 400 and "chat not found" in r.text.lower():
                    return DeployResult(area="channels", name=name, status="WORKING", detail="bad-id rejected, key valid")
                if r.status_code == 401:
                    return DeployResult(area="channels", name=name, status="FAILED", detail="401 — token revoked, remake via @BotFather")
                return DeployResult(area="channels", name=name, status="FAILED", detail=f"HTTP {r.status_code}: {r.text[:100]}")
            if name == "discord":
                if miss("DISCORD_BOT_TOKEN"):
                    return DeployResult(area="channels", name=name, status="UNKEYED", detail="needs DISCORD_BOT_TOKEN")
                r = await c.get("https://discord.com/api/v10/users/@me",
                                headers={"Authorization": f"Bot {os.getenv('DISCORD_BOT_TOKEN')}"})
                if r.status_code == 200:
                    return DeployResult(area="channels", name=name, status="WORKING",
                                        detail=f"bot {r.json().get('username', '?')}")
                return DeployResult(area="channels", name=name, status="FAILED", detail=f"HTTP {r.status_code} — token invalid?")
            if name == "matrix":
                if miss("MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_PASSWORD"):
                    return DeployResult(area="channels", name=name, status="UNKEYED", detail="needs homeserver/user/password")
                r = await c.post(f"{os.getenv('MATRIX_HOMESERVER').rstrip('/')}/_matrix/client/v3/login",
                                 json={"type": "m.login.password",
                                       "identifier": {"type": "m.id.user", "user": os.getenv("MATRIX_USER")},
                                       "password": os.getenv("MATRIX_PASSWORD")})
                if r.status_code == 200:
                    return DeployResult(area="channels", name=name, status="WORKING", detail="login ok (token discarded)")
                return DeployResult(area="channels", name=name, status="FAILED", detail=f"HTTP {r.status_code}: {r.text[:100]}")
            if name == "line":
                if miss("LINE_CHANNEL_SECRET", "LINE_ACCESS_TOKEN"):
                    return DeployResult(area="channels", name=name, status="UNKEYED", detail="needs channel secret + access token")
                r = await c.get("https://api.line.me/v2/bot/info",
                                headers={"Authorization": f"Bearer {os.getenv('LINE_ACCESS_TOKEN')}"})
                if r.status_code == 200:
                    return DeployResult(area="channels", name=name, status="WORKING",
                                        detail=f"bot {r.json().get('displayName', '?')}")
                return DeployResult(area="channels", name=name, status="FAILED", detail=f"HTTP {r.status_code} — token invalid?")
            if name == "webhook":
                r = await c.post(f"{base}/v1/hooks/webhook",
                                 json={"sender_id": "deploy-probe", "text": "ping"})
                if r.status_code == 200:
                    return DeployResult(area="channels", name=name, status="WORKING", detail="ping normalized, trust untrusted")
                return DeployResult(area="channels", name=name, status="FAILED", detail=f"HTTP {r.status_code}")
            if name in ("webui", "local_mic"):
                return DeployResult(area="channels", name=name, status="WORKING", detail="keyless, always live")
            if name == "slack":
                if miss("SLACK_BOT_TOKEN"):
                    return DeployResult(area="channels", name=name, status="UNKEYED", detail="needs SLACK_BOT_TOKEN")
                async def _auth(token):
                    rr = await c.post("https://slack.com/api/auth.test", headers={
                        "Authorization": f"Bearer {token}"})
                    return rr.json()

                def _refresh_sync():
                    from integrations import slack as _slack
                    return _slack.refresh(write_env=True)

                async def _refresh_once():
                    import asyncio as _aio
                    return await _aio.to_thread(_refresh_sync)

                async def _scope_gaps(token):
                    """Which working scopes the token lacks (zero delivery):
                    bad-id post proves the write path, list proves read."""
                    gaps = []
                    r = await c.post(
                        "https://slack.com/api/chat.postMessage",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"channel": "C00000000", "text": "deploy probe"})
                    d = r.json()
                    if not (d.get("ok") is False and "channel_not_found" in str(d.get("error", ""))):
                        gaps.append("chat:write")
                    r = await c.get(
                        "https://slack.com/api/conversations.list",
                        headers={"Authorization": f"Bearer {token}"},
                        params={"limit": 1, "exclude_archived": True})
                    if r.json().get("ok") is not True:
                        gaps.append("channels:read")
                    return gaps

                if rotate:
                    # Explicit rotation test: consumes the single-use refresh
                    # token and persists the new pair. Only path that proves
                    # the whole triple without waiting 12h.
                    if _missing("SLACK_REFRESH_TOKEN", "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"):
                        return DeployResult(area="channels", name=name, status="FAILED",
                                            detail="rotation needs refresh token + client id + secret")
                    res = await _refresh_once()
                    if not res.get("ok"):
                        return DeployResult(area="channels", name=name, status="FAILED",
                                            detail=f"rotation failed: {res.get('error')}"[:120])
                    d = await _auth((os.getenv("SLACK_BOT_TOKEN") or "").strip())
                    if d.get("ok") is True:
                        return DeployResult(area="channels", name=name, status="WORKING",
                                            detail=f"rotated ok, new token live (expires {res.get('expires_in', 43200)}s)")
                    return DeployResult(area="channels", name=name, status="FAILED",
                                        detail="rotated but new token rejected")

                d = await _auth((os.getenv("SLACK_BOT_TOKEN") or "").strip())
                if d.get("ok") is True:
                    gaps = await _scope_gaps((os.getenv("SLACK_BOT_TOKEN") or "").strip())
                    base_detail = f"team {d.get('team', '?')}"
                    if not gaps:
                        return DeployResult(area="channels", name=name, status="WORKING",
                                            detail=base_detail + "; send-path + read ok")
                    return DeployResult(area="channels", name=name, status="FAILED",
                                        detail=base_detail + "; missing scopes: " + ", ".join(gaps))
                if d.get("error") == "token_expired" and os.getenv("SLACK_REFRESH_TOKEN"):
                    res = await _refresh_once()
                    if res.get("ok"):
                        d = await _auth((os.getenv("SLACK_BOT_TOKEN") or "").strip())
                        if d.get("ok") is True:
                            return DeployResult(area="channels", name=name, status="WORKING",
                                                detail=f"auto-refreshed; team {d.get('team', '?')}")
                    return DeployResult(area="channels", name=name, status="FAILED",
                                        detail=f"expired; refresh failed: {res.get('error')}"[:120])
                return DeployResult(area="channels", name=name, status="FAILED", detail=str(d.get("error", "auth failed"))[:120])
            if name == "signal":
                if miss("SIGNAL_NUMBER", "SIGNAL_DATA_DIR"):
                    return DeployResult(area="channels", name=name, status="UNKEYED", detail="needs number + data dir")
                import shutil
                if shutil.which("signal-cli"):
                    return DeployResult(area="channels", name=name, status="WORKING", detail="binary + number present (send not probed)")
                return DeployResult(area="channels", name=name, status="FAILED", detail="signal-cli binary not on PATH")
            if name == "gmail":
                if miss("GMAIL_TOKEN"):
                    return DeployResult(area="channels", name=name, status="UNKEYED", detail="needs GMAIL_TOKEN (run oauth setup)")
                import asyncio as _aio
                from integrations import gmail as _g
                res = await _aio.to_thread(_g.query, api_method="list", max_results=1)
                if res.get("ok"):
                    return DeployResult(area="channels", name=name, status="WORKING", detail="read-only list ok")
                return DeployResult(area="channels", name=name, status="FAILED", detail=str(res.get("error"))[:120])
            if name == "imap":
                if miss("IMAP_HOST", "IMAP_USER", "IMAP_PASS", "SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
                    return DeployResult(area="channels", name=name, status="UNKEYED", detail="needs IMAP_* + SMTP_* app passwords")
                import asyncio as _aio
                import imaplib as _imap
                import smtplib as _smtp

                def _check():
                    m = _imap.IMAP4_SSL(os.getenv("IMAP_HOST"), 993, timeout=10)
                    try:
                        m.login(os.getenv("IMAP_USER"), os.getenv("IMAP_PASS"))
                    finally:
                        try:
                            m.logout()
                        except Exception:
                            pass
                    s = _smtp.SMTP(os.getenv("SMTP_HOST"), 587, timeout=10)
                    try:
                        s.starttls()
                        s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
                    finally:
                        try:
                            s.quit()
                        except Exception:
                            pass
                    return True

                try:
                    await _aio.to_thread(_check)
                    return DeployResult(area="channels", name=name, status="WORKING", detail="IMAP+SMTP logins ok, nothing sent")
                except Exception as e:
                    return DeployResult(area="channels", name=name, status="FAILED", detail=f"{type(e).__name__}: {e}"[:120])
            if name in ("twilio_sms", "whatsapp_twilio", "twilio_voice"):
                if miss("TWILIO_SID", "TWILIO_AUTH"):
                    return DeployResult(area="channels", name=name, status="UNKEYED", detail="needs TWILIO_SID + TWILIO_AUTH")
                r = await c.get(f"https://api.twilio.com/2010-04-01/Accounts/{os.getenv('TWILIO_SID')}.json",
                                auth=(os.getenv("TWILIO_SID"), os.getenv("TWILIO_AUTH")))
                if r.status_code == 200:
                    return DeployResult(area="channels", name=name, status="WORKING",
                                        detail=f"account {r.json().get('friendly_name', '?')} (no message sent)")
                if r.status_code == 401:
                    return DeployResult(area="channels", name=name, status="FAILED", detail="401 — SID/auth rejected")
                return DeployResult(area="channels", name=name, status="FAILED", detail=f"HTTP {r.status_code}")
            if name == "teams":
                if miss("TEAMS_APP_ID", "TEAMS_APP_PASSWORD", "TEAMS_TENANT"):
                    return DeployResult(area="channels", name=name, status="UNKEYED", detail="needs Azure app id/secret/tenant")
                r = await c.post(f"https://login.microsoftonline.com/{os.getenv('TEAMS_TENANT')}/oauth2/v2.0/token",
                                 data={"client_id": os.getenv("TEAMS_APP_ID"),
                                       "client_secret": os.getenv("TEAMS_APP_PASSWORD"),
                                       "scope": "https://graph.microsoft.com/.default",
                                       "grant_type": "client_credentials"})
                if r.status_code == 200:
                    return DeployResult(area="channels", name=name, status="WORKING", detail="client-credentials token ok (discarded)")
                return DeployResult(area="channels", name=name, status="FAILED", detail=f"HTTP {r.status_code}: {r.text[:100]}")
            if name == "whatsapp_meta":
                if miss("WA_TOKEN", "WA_PHONE_ID"):
                    return DeployResult(area="channels", name=name, status="UNKEYED", detail="needs WA_TOKEN + WA_PHONE_ID")
                r = await c.get(f"https://graph.facebook.com/v21.0/{os.getenv('WA_PHONE_ID')}",
                                params={"fields": "verified_name,display_phone_number"},
                                headers={"Authorization": f"Bearer {os.getenv('WA_TOKEN')}"})
                if r.status_code == 200:
                    return DeployResult(area="channels", name=name, status="WORKING",
                                        detail=f"{r.json().get('display_phone_number', '?')} verified")
                return DeployResult(area="channels", name=name, status="FAILED", detail=f"HTTP {r.status_code}: {r.text[:100]}")
            return DeployResult(area="channels", name=name, status="FAILED", detail=f"unknown channel '{name}'")
    except Exception as e:
        return DeployResult(area="channels", name=name, status="FAILED", detail=f"{type(e).__name__}: {e}"[:120])


@router.post("/v1/control/channel-test")
async def channel_test(body: ChannelTestBody, request: Request):
    """Safe per-channel probe (read-only, zero delivery). Loopback-only.
    channel="all" runs all 16 sequentially."""
    from channels_api import _loopback_only
    _loopback_only(request)
    import db as _db
    name = (body.channel or "").lower()
    channels = ["telegram", "gmail", "discord", "matrix", "line", "webhook", "webui",
                "slack", "signal", "imap", "twilio_sms", "whatsapp_twilio",
                "local_mic", "teams", "whatsapp_meta", "twilio_voice"]
    rot = bool(getattr(body, "rotate", False))
    if name == "all":
        out = [await _probe_channel(n, _base_url()) for n in channels]
    elif name in _INTEGRATION_PROBES or name == "calendar":
        out = [await _probe_integration(name)]
    elif name in channels:
        out = [await _probe_channel(name, _base_url(), rotate=rot)]
    else:
        raise HTTPException(400, f"unknown channel '{body.channel}'. Try one of: {channels} "
                                 f"or integrations {sorted(list(_INTEGRATION_PROBES) + ['calendar'])}")
    rows = [r.model_dump() for r in out]
    try:
        _db.log_call(provider="control", model="channel-test", status="ok",
                     call_role="control", agent="operator")
    except Exception:
        pass
    return {"results": rows,
            "summary": {s: sum(1 for r in rows if r["status"] == s)
                        for s in ("WORKING", "FAILED", "UNKEYED")}}
