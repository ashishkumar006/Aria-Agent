"""Observability contracts: ledger rows for memory/voice ops, dashboard
surface for the planes built after it.

Memory searches/writes and voice calls used to bypass db.log_call entirely
(only embeds were logged) — these tests pin the closed gap. DB writes go
to a tmp database (never the live 8MB ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def api_client(tmp_path_factory):
    import db
    import memory_api as MA

    tmp = tmp_path_factory.mktemp("memledger")
    os.environ["GATEWAY_MEMORY_STATE"] = str(tmp)
    MA._reset_cache()
    db.DB_PATH = str(tmp / "ledger.db")
    db.init()
    import main as M
    transport = httpx.ASGITransport(app=M.app)
    async with M.app.router.lifespan_context(M.app):
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test",
                                     timeout=60) as c:
            yield c
    MA._reset_cache()
    os.environ.pop("GATEWAY_MEMORY_STATE", None)
    import importlib as _il
    _il.reload(db)


def _rows(role):
    import db
    return [r for r in db.recent(limit=500) if r.get("call_role") == role]


@pytest.mark.asyncio
async def test_memory_search_logged(api_client):
    await api_client.post("/v1/memory/remember", json={
        "kind": "fact", "descriptor": "ledger probe fact",
        "source": "t", "run_id": "r1"})
    await api_client.post("/v1/memory/search", json={"query": "ledger"})
    mem = _rows("memory")
    ops = {r["model"] for r in mem}
    assert "remember" in ops and "search" in ops
    assert all(r["provider"] == "memory" for r in mem)


@pytest.mark.asyncio
async def test_memory_error_logged(api_client):
    await api_client.post("/v1/memory/remember", json={
        "kind": "fact", "descriptor": "x", "source": "t", "run_id": "r1",
        "principal_role": "runtime"})
    mem = [r for r in _rows("memory") if r.get("status") == "error"]
    assert any("may not write" in (r.get("error") or "") for r in mem)


@pytest.mark.asyncio
async def test_voice_errors_logged(api_client):
    r = await api_client.post(
        "/v1/stt", files={"file": ("empty.wav", b"", "audio/wav")})
    assert "error" in r.json()
    voice = _rows("voice")
    assert any("empty audio" in (v.get("error") or "") for v in voice)


@pytest.mark.asyncio
async def test_oversize_payload_is_413(api_client):
    big = {"k": "x" * 200_000}
    r = await api_client.post("/v1/memory/remember", json={
        "kind": "fact", "descriptor": "big value probe",
        "value": big, "source": "t", "run_id": "r1"})
    assert r.status_code == 413, r.text


@pytest.mark.asyncio
async def test_search_top_k_clamped(api_client):
    r = await api_client.post("/v1/memory/search", json={
        "query": "anything", "top_k": 1_000_000})
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) <= 100


def test_register_model_routes_unit():
    import providers as P

    P.MODEL_ROUTES.clear()
    try:
        P.register_model_routes({})
        assert P.MODEL_ROUTES == {}
        P.register_model_routes(
            {"kilo": type("K", (), {"model": "custom:free"})(),
             "gemini35lite": type("G", (), {"model": "m"})()})
        assert P.MODEL_ROUTES["custom:free"] == "kilo"
        assert P.MODEL_ROUTES["tencent/hy3:free"] == "kilo"
        assert P.MODEL_ROUTES["gemini-3.5-flash-lite"] == "gemini35lite"
    finally:
        P.MODEL_ROUTES.clear()


def test_integration_inventory_marks_fallback():
    import asyncio

    import integrations_api as IA

    out = asyncio.run(IA.integration_inventory())
    by_name = {s["service"]: s for s in out["integrations"]}
    assert by_name["websearch"]["configured"] is True
    assert "DDG" in by_name["websearch"].get("note", "")


def test_dashboard_covers_new_planes():
    static = HERE / "static"
    pages = {
        "dashboard.html": ["calls-table", "/v1/chat",
                           "/static/connections.html",
                           "/static/system.html"],
        "connections.html": ["conn-essential", "conn-advanced", "Essential",
                             "Advanced", "/v1/channels", "/v1/integrations",
                             "/v1/config/keys", "/v1/control/keys"],
        "system.html": ["memory-table", "spend-table", "policy-table",
                        "voice-text", "deploy-table", "/v1/memory/stats",
                        "/v1/spend", "/v1/policy", "/v1/tts",
                        "/v1/control/deploy-test"],
        "ledger.html": ["led-calls-table", "led-prov-table", "led-role-table",
                        "led-err-table", "led-tool-table", "exportLedgerCSV",
                        "/v1/calls", "/v1/spend", "/v1/cost/by_agent",
                        "/v1/tools/usage"],
    }
    for page, sections in pages.items():
        html = (static / page).read_text(encoding="utf-8")
        for section in sections:
            assert section in html, f"{page}: {section}"
