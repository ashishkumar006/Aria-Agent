"""Gateway hardening against the eight security invariants."""
import asyncio
import json
import socket

import httpx
import pytest

import budget
import gateway_auth
import redact
import safe_fetch
import secret_vault
import security_audit


# ── SSRF ──────────────────────────────────────────────────────────────────
def _fake_dns(monkeypatch, mapping):
    def fake(host, port, *a, **k):
        ips = mapping.get(host, ["93.184.216.34"])
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in ips]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://localhost/x", "http://[::1]/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::ffff:127.0.0.1]/x", "http://2130706433/",
    "file:///etc/passwd", "gopher://example.com/x",
    "http://user:pass@example.com/x", "https://example.com:8080/x",
    "http://metadata.google.internal/",
])
def test_ssrf_urls_are_rejected(url, monkeypatch):
    _fake_dns(monkeypatch, {"metadata.google.internal": ["169.254.169.254"]})
    with pytest.raises(safe_fetch.UnsafeURLError):
        safe_fetch.validate_url(url)


def test_a_public_url_and_private_dns_answer(monkeypatch):
    _fake_dns(monkeypatch, {"public.example": ["93.184.216.34"],
                            "private.example": ["10.0.0.7"]})
    assert safe_fetch.validate_url("https://public.example/a")
    with pytest.raises(safe_fetch.UnsafeURLError, match="private"):
        safe_fetch.validate_url("https://private.example/a")


def test_a_redirect_to_loopback_is_rejected_before_fetching(monkeypatch):
    _fake_dns(monkeypatch, {"public.example": ["93.184.216.34"]})
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/x"})

    with pytest.raises(safe_fetch.UnsafeURLError):
        asyncio.run(safe_fetch.fetch_bytes(
            "https://public.example/a",
            transport=httpx.MockTransport(handler)))
    assert seen == ["https://public.example/a"]


def test_the_body_cap_is_enforced(monkeypatch):
    _fake_dns(monkeypatch, {"public.example": ["93.184.216.34"]})

    def handler(request):
        return httpx.Response(200, content=b"x" * 100,
                              headers={"content-type": "image/png"})

    with pytest.raises(safe_fetch.UnsafeURLError, match="too large"):
        asyncio.run(safe_fetch.fetch_bytes(
            "https://public.example/a", max_bytes=10, image_only=True,
            transport=httpx.MockTransport(handler)))


# ── redaction ─────────────────────────────────────────────────────────────
def test_known_and_shaped_secrets_are_redacted():
    redact.register_secret("super-secret-provider-key-123")
    out = redact.redact(
        "boom key=super-secret-provider-key-123 "
        "https://api.example.com/v1?key=AIza" + "A" * 30 +
        " Authorization: Bearer ghp_" + "b" * 30)
    assert "super-secret-provider-key-123" not in out
    assert "AIza" + "A" * 30 not in out
    assert "ghp_" + "b" * 30 not in out
    assert out.count(redact.REDACTED) >= 3


# ── key vault ────────────────────────────────────────────────────────────
def test_provider_keys_leave_the_environment(monkeypatch):
    monkeypatch.setenv("FAKE_PROVIDER_KEY_SINGLE", "sk-test-key-123456")
    monkeypatch.setenv("FAKE_PROVIDER_KEY_MANY", "one-key,two-key")
    n = secret_vault.capture_and_scrub(
        ("FAKE_PROVIDER_KEY_SINGLE", "FAKE_PROVIDER_KEY_MANY"))
    assert n == 2
    import os
    assert "FAKE_PROVIDER_KEY_SINGLE" not in os.environ
    assert secret_vault.present("FAKE_PROVIDER_KEY_SINGLE")
    assert secret_vault.value("FAKE_PROVIDER_KEY_SINGLE") == "sk-test-key-123456"
    assert secret_vault.values_for("FAKE_PROVIDER_KEY_SINGLE",
                                   "FAKE_PROVIDER_KEY_MANY") == [
        "sk-test-key-123456", "one-key", "two-key"]


# ── audit chain ──────────────────────────────────────────────────────────
def test_security_audit_chain_detects_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(security_audit, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(security_audit, "KEY_PATH", tmp_path / "audit.key")
    monkeypatch.setattr(security_audit, "_key", None)
    monkeypatch.setattr(security_audit, "_last_hash", "")
    security_audit.init()
    security_audit.record("first", reason="one")
    security_audit.record("second", reason="two")
    assert security_audit.verify()["ok"] is True
    assert security_audit.verify()["entries"] == 2
    lines = security_audit.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["reason"] = "changed"
    lines[0] = json.dumps(rec)
    security_audit.AUDIT_PATH.write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")
    bad = security_audit.verify()
    assert bad["ok"] is False and bad["first_bad"] == 1


# ── budgets and scopes ───────────────────────────────────────────────────
def test_a_user_token_is_inference_only():
    for path in ("/v1/chat", "/v1/chat/batch", "/v1/vision", "/v1/embed",
                 "/v1/tts", "/v1/tts/stream", "/v1/stt", "/v1/users/me"):
        assert gateway_auth.user_route_allowed(path), path
    for path in ("/v1/calls", "/v1/spend", "/v1/config/keys", "/v1/status",
                 "/v1/memory", "/v1/documents", "/v1/control/keys",
                 "/v1/users", "/v1/deploy-test"):
        assert not gateway_auth.user_route_allowed(path), path


def test_the_request_budget_stops_a_burst():
    b = budget.UserBudget()
    b.rpm = 2
    b.daily_requests = 0
    b.daily_tokens = 0
    assert b.check("u-test")[0] is True
    assert b.check("u-test")[0] is True
    ok, retry, why = b.check("u-test")
    assert ok is False and retry >= 1 and "minute" in why


def test_the_token_bearing_console_page_is_loopback_only():
    from types import SimpleNamespace
    import main as M
    remote = SimpleNamespace(client=SimpleNamespace(host="203.0.113.9"))
    r = M._console_asset("dashboard.html", remote)
    assert r.status_code == 403
    local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    r2 = M._console_asset("dashboard.html", local)
    assert getattr(r2, "status_code", 200) != 403
