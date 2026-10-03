"""V10 adaptor-plane contract tests. No network, no secrets, no gateway process.

Covers: registry shape (16 adaptors, no secret values), envelope defaults,
trust store isolation, per-adaptor normalize()/verify_webhook() fixtures,
NotConfigured/NotIntegrated behavior with keys stripped, policy engine
semantics, and the Tier-1 integration fail-soft shapes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adaptors import registry
from adaptors.base import BaseAdaptor, NotConfigured, NotIntegrated
from adaptors.envelope import ChannelMessage, TrustLevel

ALL_KEYS = [
    "TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "MATRIX_HOMESERVER",
    "MATRIX_USER", "MATRIX_PASSWORD", "LINE_CHANNEL_SECRET",
    "LINE_ACCESS_TOKEN", "WEBHOOK_SECRET", "WEBHOOK_OUT_URL",
    "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SIGNAL_NUMBER",
    "GMAIL_TOKEN", "GMAIL_REFRESH_TOKEN", "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET", "GOOGLE_CALENDAR_TOKEN", "GITHUB_TOKEN",
    "NOTION_TOKEN", "TAVILY_API_KEY", "IMAP_HOST", "IMAP_USER",
    "IMAP_PASS", "SMTP_HOST", "SMTP_USER", "SMTP_PASS", "TWILIO_SID",
    "TWILIO_AUTH", "TWILIO_FROM", "TWILIO_WEBHOOK_URL", "WHATSAPP_FROM",
    "TEAMS_APP_ID", "TEAMS_APP_PASSWORD", "TEAMS_TENANT", "WA_TOKEN",
    "WA_PHONE_ID", "WA_VERIFY_TOKEN", "TWILIO_VOICE_FROM", "VOICE_WS_URL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)
    registry.reset()
    yield
    registry.reset()


# ── registry ──────────────────────────────────────────────────────────────

def test_registry_has_16_adaptors():
    assert len(registry.names()) == 16
    for expected in ["telegram", "discord", "matrix", "line", "webhook",
                     "webui", "slack", "signal", "gmail", "imap",
                     "twilio_sms", "whatsapp_twilio", "local_mic", "teams",
                     "whatsapp_meta", "twilio_voice"]:
        assert expected in registry.names()


def test_inventory_never_leaks_values():
    import re
    os.environ["TELEGRAM_BOT_TOKEN"] = "SECRET-VALUE-12345"
    try:
        inv = registry.inventory()
    finally:
        del os.environ["TELEGRAM_BOT_TOKEN"]
    assert len(inv) == 16
    assert "SECRET-VALUE-12345" not in str(inv)  # names ok, values never
    for c in inv:
        assert set(c) >= {"name", "available", "configured", "required_keys"}
    assert re.search(r"\bSECRET-VALUE-12345\b", str(inv)) is None


def test_unknown_channel_is_none():
    assert registry.get("nope-not-a-channel") is None


# ── envelope ──────────────────────────────────────────────────────────────

def test_envelope_defaults_untrusted():
    m = ChannelMessage(channel="x")
    assert m.trust_level == TrustLevel.untrusted
    assert m.attachments == [] and m.raw == {}


# ── trust store (isolated) ────────────────────────────────────────────────

def test_trust_classify_pair_unpair(tmp_path, monkeypatch):
    from adaptors import trust as T
    monkeypatch.setattr(T, "STORE_PATH", tmp_path / "pairing.json")
    assert T.classify("telegram", "") == TrustLevel.untrusted
    assert T.classify("telegram", "999") == TrustLevel.untrusted
    T.pair("telegram", "999", "owner")
    assert T.classify("telegram", "999") == TrustLevel.owner
    T.pair("telegram", "111")
    assert T.classify("telegram", "111") == TrustLevel.paired
    assert T.unpair("telegram", "999") is True
    assert T.classify("telegram", "999") == TrustLevel.untrusted
    assert T.unpair("telegram", "nope") is False
    with pytest.raises(ValueError):
        T.pair("telegram", "1", "admin")


def test_list_masks_sender_ids(tmp_path, monkeypatch):
    from adaptors import trust as T
    monkeypatch.setattr(T, "STORE_PATH", tmp_path / "pairing.json")
    T.pair("gmail", "someone@example.com", "owner")
    shown = T.list_paired("gmail")
    assert "someone@example.com" not in str(shown)
    assert shown["gmail"]


# ── per-adaptor contracts ─────────────────────────────────────────────────

def _inst(name):
    inst = registry.get(name)
    assert inst is not None and isinstance(inst, BaseAdaptor)
    return inst


def test_keyless_adaptors_need_nothing():
    assert _inst("webhook").required_keys() == []
    assert _inst("webui").required_keys() == []
    assert _inst("local_mic").required_keys() == []


def test_keyed_adaptors_declare_keys():
    assert "TELEGRAM_BOT_TOKEN" in _inst("telegram").required_keys()
    assert "DISCORD_BOT_TOKEN" in _inst("discord").required_keys()
    assert "GMAIL_TOKEN" in _inst("gmail").required_keys()
    assert "WA_VERIFY_TOKEN" in _inst("whatsapp_meta").required_keys()
    assert "TWILIO_SID" in _inst("twilio_sms").required_keys()


def test_unconfigured_sends_raise_not_configured():
    for name in ["telegram", "slack", "discord", "gmail"]:
        inst = _inst(name)
        with pytest.raises((NotConfigured, NotIntegrated)):
            inst.send(to="x", text="hi")


def test_gmail_read_refresh_fail_soft_without_keys():
    inst = _inst("gmail")
    assert inst.read(query="is:unread")["ok"] is False
    assert inst.refresh()["ok"] is False


def test_no_scaffold_walls_remain():
    # Every adaptor now attempts live delivery once keys are present (and
    # maps provider errors). With fake keys each send must raise something
    # OTHER than NotIntegrated — and must return fast (timeouts, no hangs).
    import time
    for name in registry.names():
        if name in ("webui", "local_mic", "webhook"):
            continue  # local no-ops / needs OUT_URL; covered elsewhere
        inst = _inst(name)
        for k in inst.required_keys():
            os.environ[k] = "test-fake"
        try:
            t0 = time.time()
            try:
                inst.send(to="x", text="hi")
            except NotIntegrated as e:
                pytest.fail(f"{name} still scaffolded: {e}")
            except Exception:
                pass  # any other error = attempted delivery, good
            assert time.time() - t0 < 45, f"{name} hung"
        finally:
            for k in inst.required_keys():
                os.environ.pop(k, None)


def test_telegram_normalize():
    m = _inst("telegram").normalize({
        "message": {"message_id": 7, "date": 1700000000,
                    "from": {"id": 42}, "chat": {"id": 42}, "text": "hi"}})
    assert (m.sender_id, m.chat_id, m.text, m.msg_id) == ("42", "42", "hi", "7")
    assert m.trust_level == TrustLevel.untrusted  # unpaired by default


def test_discord_normalize_snowflake_ts():
    m = _inst("discord").normalize({
        "id": "1140000000000000000", "channel_id": "9",
        "author": {"id": "5"}, "content": "yo"})
    assert m.sender_id == "5" and m.chat_id == "9" and m.text == "yo"
    assert m.ts > 1_000_000_000


def test_line_verify_and_normalize():
    import hashlib
    import hmac
    import base64
    os.environ["LINE_CHANNEL_SECRET"] = "s3cret"
    inst = _inst("line")
    body = b'{"events":[]}'
    good = base64.b64encode(hmac.new(b"s3cret", body, hashlib.sha256).digest()).decode()
    assert inst.verify_webhook({"x-line-signature": good}, body) is True
    assert inst.verify_webhook({"x-line-signature": "bogus"}, body) is False
    assert inst.verify_webhook({}, body) is False
    m = inst.normalize({"events": [{"type": "message", "replyToken": "r",
                                    "source": {"userId": "U1"},
                                    "message": {"type": "text", "text": "hey"},
                                    "timestamp": 1700000000000}]})
    assert (m.sender_id, m.chat_id, m.text) == ("U1", "U1", "hey")


def test_slack_verify_rejects_without_secret():
    assert _inst("slack").verify_webhook({}, b"{}") is False


def test_whatsapp_meta_handshake_and_normalize():
    from adaptors.whatsapp_meta import WhatsAppMetaAdaptor
    assert WhatsAppMetaAdaptor.verify_handshake(
        {"hub.mode": "subscribe", "hub.verify_token": "tok",
         "hub.challenge": "CH"}, "tok") == "CH"
    assert WhatsAppMetaAdaptor.verify_handshake(
        {"hub.mode": "subscribe", "hub.verify_token": "nope",
         "hub.challenge": "CH"}, "tok") is None
    m = _inst("whatsapp_meta").normalize({
        "entry": [{"changes": [{"value": {"messages": [
            {"from": "1555", "id": "wamid.1", "timestamp": "1700000000",
             "type": "text", "text": {"body": "hola"}}]}}]}]})
    assert (m.sender_id, m.text, m.msg_id) == ("1555", "hola", "wamid.1")


def test_twilio_normalize_strips_whatsapp_prefix():
    m = _inst("whatsapp_twilio").normalize(
        {"From": "whatsapp:+1555", "Body": "hi", "MessageSid": "SM1"})
    assert m.sender_id == "+1555" and m.text == "hi"


def test_gmail_normalize():
    m = _inst("gmail").normalize(
        {"id": "abc", "thread_id": "t1", "from": "boss@x.com",
         "subject": "Q?", "snippet": "ping"})
    assert m.msg_id == "abc" and "ping" in m.text


# ── policy engine ─────────────────────────────────────────────────────────

def test_policy_owner_allow_untrusted_deny():
    from policy import PolicyEngine
    eng = PolicyEngine({"defaults": {"dry_run": False, "default_action": "deny"},
                        "rules": [
                            {"id": "o", "when": {"trust": "owner"}, "action": "allow"},
                            {"id": "u", "when": {"trust": "untrusted"}, "action": "deny"},
                        ]})
    assert eng.evaluate({"trust": "owner", "tool": "x"}).allowed is True
    v = eng.evaluate({"trust": "untrusted", "tool": "x"})
    assert v.allowed is False and v.rule_id == "u"


def test_policy_first_match_wins_and_dry_run():
    from policy import PolicyEngine
    eng = PolicyEngine({"defaults": {"dry_run": True, "default_action": "deny"},
                        "rules": [{"id": "r1", "when": {"tool": "x"}, "action": "deny"}]})
    v = eng.evaluate({"trust": "untrusted", "tool": "x"})
    assert v.allowed is True and v.dry_run is True and v.action == "deny"


def test_policy_default_deny_unknown():
    from policy import PolicyEngine
    eng = PolicyEngine({"defaults": {"dry_run": False, "default_action": "deny"},
                        "rules": []})
    assert eng.evaluate({"trust": "paired", "tool": "zzz"}).allowed is False


def test_policy_spend_caps():
    from policy import spend_over_cap
    # policy.yaml ships per_call: 0.50 — a $999999 call is over, $0.01 is not.
    assert spend_over_cap(per_call_usd=999999) is True
    assert spend_over_cap(per_call_usd=0.01) is False


def test_policy_yaml_loads():
    from policy import get_engine
    eng = get_engine()
    assert isinstance(eng.rules, list) and len(eng.rules) >= 4
    assert eng.dry_run_global is True  # ships report-only


# ── Tier-1 integration fail-soft (no keys in this env) ────────────────────

def test_integrations_fail_soft_without_keys():
    from integrations import gmail, calendar, github, notion
    assert gmail.send_email(to="a@b.com", subject="s", body="b")["ok"] is False
    assert gmail.query(api_method="list")["ok"] is False
    assert gmail.refresh()["ok"] is False
    assert calendar.create_event(summary="s", start="2026-01-01T10:00:00",
                                 end="2026-01-01T11:00:00")["ok"] is False
    assert github.query(api_method="list_repos")["ok"] is False
    assert notion.query(api_method="list_pages")["ok"] is False


def test_integrations_inventory_shape():
    import os
    assert os.path.exists(ROOT / ".env.example")


# ── live send paths (transport mocked — no network) ───────────────────────

class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        if isinstance(self._payload, Exception):
            raise ValueError("no json")
        return self._payload


def test_discord_send_ok_and_401(monkeypatch):
    import adaptors.http as H
    from adaptors.discord.adapter import DiscordAdaptor
    os.environ["DISCORD_BOT_TOKEN"] = "x"
    try:
        monkeypatch.setattr(H, "post", lambda *a, **k: _Resp(200, {"id": "m1"}))
        rep = DiscordAdaptor().send(to="9", text="hi")
        assert rep.ok is True and rep.msg_id == "m1"
        monkeypatch.setattr(H, "post", lambda *a, **k: _Resp(401, {"message": "bad"}))
        import pytest as _p
        with _p.raises(RuntimeError, match="401"):
            DiscordAdaptor().send(to="9", text="hi")
    finally:
        os.environ.pop("DISCORD_BOT_TOKEN", None)


def test_line_send_push_and_reply_paths(monkeypatch):
    import adaptors.http as H
    from adaptors.line.adapter import LineAdaptor
    os.environ["LINE_ACCESS_TOKEN"] = "x"
    seen = {}

    def _fake(url, **kw):
        seen["url"] = url
        seen["body"] = kw.get("json", {})
        return _Resp(200, {})

    try:
        monkeypatch.setattr(H, "post", _fake)
        LineAdaptor().send(to="U1", text="hi")
        assert seen["url"].endswith("/push")
        LineAdaptor().send(to="U1", text="hi", thread_id="reply:TOK")
        assert seen["url"].endswith("/reply")
        assert seen["body"]["replyToken"] == "TOK"
    finally:
        os.environ.pop("LINE_ACCESS_TOKEN", None)


def test_matrix_login_failure(monkeypatch):
    import adaptors.http as H
    from adaptors.matrix.adapter import MatrixAdaptor
    os.environ.update({"MATRIX_HOMESERVER": "https://x", "MATRIX_USER": "u",
                       "MATRIX_PASSWORD": "p"})
    try:
        monkeypatch.setattr(H, "post", lambda *a, **k: _Resp(403, {"error": "no"}))
        import pytest as _p
        with _p.raises(RuntimeError):
            MatrixAdaptor().send(to="!r:x", text="hi")
    finally:
        for k in ("MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_PASSWORD"):
            os.environ.pop(k, None)


def test_signal_send_and_poll(monkeypatch):
    import shutil
    import subprocess
    from adaptors.signal.adapter import SignalAdaptor
    os.environ["SIGNAL_NUMBER"] = "+1000"
    # deterministic: pretend the binary is absent regardless of host
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    try:
        def _ok(cmd, **kw):
            m = mock.MagicMock()
            m.returncode = 0
            m.stdout = '{"envelope":{"source":"+1","timestamp":1700000000000,"dataMessage":{"message":"yo"}}}\nnoise\n'
            m.stderr = ""
            return m

        monkeypatch.setattr(subprocess, "run", _ok)
        rep = SignalAdaptor().send(to="+2", text="hi")
        assert rep.ok is True
        envs = SignalAdaptor().poll(timeout=1)
        assert len(envs) == 1 and envs[0]["envelope"]["source"] == "+1"

        def _missing(cmd, **kw):
            raise FileNotFoundError("nope")

        monkeypatch.setattr(subprocess, "run", _missing)
        import pytest as _p
        with _p.raises(RuntimeError, match="binary not found"):
            SignalAdaptor().send(to="+2", text="hi")
    finally:
        os.environ.pop("SIGNAL_NUMBER", None)


def test_imap_send_and_poll(monkeypatch):
    import imaplib
    import smtplib
    from adaptors.imap.adapter import ImapSmtpAdaptor
    for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS", "SMTP_HOST",
              "SMTP_USER", "SMTP_PASS"):
        os.environ[k] = "x"
    # creds present but host unreachable -> RuntimeError, never hang/crash
    class _BadSMTP:
        def __init__(self, *a, **k):
            raise OSError("no route")

    monkeypatch.setattr(smtplib, "SMTP_SSL", _BadSMTP)
    import pytest as _p2
    with _p2.raises(RuntimeError, match="smtp error"):
        ImapSmtpAdaptor().send(to="a@b.c", text="hi")

    class _BadIMAP:
        def __init__(self, *a, **k):
            raise OSError("no route")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _BadIMAP)
    with _p2.raises(RuntimeError, match="imap login failed"):
        ImapSmtpAdaptor().poll()
    for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS", "SMTP_HOST",
              "SMTP_USER", "SMTP_PASS"):
        os.environ.pop(k, None)


def test_twilio_family_sends_mocked(monkeypatch):
    import adaptors.http as H
    from adaptors.twilio_sms.adapter import TwilioSmsAdaptor
    from adaptors.whatsapp_twilio.adapter import WhatsAppTwilioAdaptor
    from adaptors.twilio_voice.adapter import TwilioVoiceAdaptor
    os.environ.update({"TWILIO_SID": "ACx", "TWILIO_AUTH": "a",
                       "TWILIO_FROM": "+1000", "WHATSAPP_FROM": "whatsapp:+1000",
                       "TWILIO_VOICE_FROM": "+1000"})
    seen = {}

    def _ok(url, **kw):
        seen["url"] = url
        seen["data"] = kw.get("data", {})
        return _Resp(201, {"sid": "SM9"})

    try:
        monkeypatch.setattr(H, "post", _ok)
        r1 = TwilioSmsAdaptor().send(to="+2000", text="hi")
        assert r1.ok is True and r1.msg_id == "SM9"
        assert seen["data"]["From"] == "+1000"
        r2 = WhatsAppTwilioAdaptor().send(to="+2000", text="hi")
        assert r2.ok is True
        assert seen["data"]["From"] == "whatsapp:+1000"
        r3 = TwilioVoiceAdaptor().send(to="+2000", text="hello there")
        assert r3.ok is True and r3.msg_id == "SM9"
        assert "Calls.json" in seen["url"]
    finally:
        for k in ("TWILIO_SID", "TWILIO_AUTH", "TWILIO_FROM",
                  "WHATSAPP_FROM", "TWILIO_VOICE_FROM"):
            os.environ.pop(k, None)


def test_whatsapp_meta_send_mocked(monkeypatch):
    import adaptors.http as H
    from adaptors.whatsapp_meta.adapter import WhatsAppMetaAdaptor
    os.environ.update({"WA_TOKEN": "x", "WA_PHONE_ID": "123"})
    try:
        monkeypatch.setattr(
            H, "post",
            lambda *a, **k: _Resp(200, {"messages": [{"id": "wamid.1"}]}))
        rep = WhatsAppMetaAdaptor().send(to="1555", text="hi")
        assert rep.ok is True and rep.msg_id == "wamid.1"
    finally:
        os.environ.pop("WA_TOKEN", None)
        os.environ.pop("WA_PHONE_ID", None)


def test_teams_token_and_send_mocked(monkeypatch):
    import adaptors.http as H
    from adaptors.teams.adapter import TeamsAdaptor
    os.environ.update({"TEAMS_APP_ID": "a", "TEAMS_APP_PASSWORD": "p",
                       "TEAMS_TENANT": "t"})
    calls = []

    def _fake(url, **kw):
        calls.append(url)
        if "oauth2" in url:
            return _Resp(200, {"access_token": "tok", "expires_in": 3600})
        return _Resp(201, {"id": "act1"})

    try:
        monkeypatch.setattr(H, "post", _fake)
        rep = TeamsAdaptor().send(to="conv1", text="hi",
                                  service_url="https://svc/")
        assert rep.ok is True and rep.msg_id == "act1"
        assert any("oauth2" in u for u in calls)
    finally:
        for k in ("TEAMS_APP_ID", "TEAMS_APP_PASSWORD", "TEAMS_TENANT"):
            os.environ.pop(k, None)


def test_gmail_watch_needs_topic():
    from adaptors.gmail.adapter import GmailAdaptor
    import pytest as _p
    with _p.raises(NotConfigured):
        GmailAdaptor().watch()


# ── enforcement plumbing ────────────────────────────────────────────────

def test_policy_check_fail_closed(monkeypatch):
    import channels_api
    monkeypatch.setattr("policy.get_engine", lambda: 1 / 0)
    assert channels_api._policy_check({"trust": "owner"})[0] == "deny"


def test_approvals_store_roundtrip(tmp_path, monkeypatch):
    import approvals as A
    monkeypatch.setattr(A, "STORE_PATH", tmp_path / "appr.json")
    e = A.create(tool="send_x", channel="x", args={"to": "1"},
                 agent="a", session="s", rule_id="r")
    assert e["status"] == "pending" and e["id"].startswith("gw-")
    assert len(A.list_pending()) == 1
    assert A.resolve(e["id"], True)["status"] == "approved"
    assert A.list_pending() == []
    assert A.resolve("nope", True) is None
    # idempotent re-resolve keeps first verdict
    assert A.resolve(e["id"], False)["status"] == "approved"


def test_channel_send_enforces_deny(monkeypatch):
    import asyncio
    import db as _db
    import channels_api
    from fastapi import HTTPException
    import pytest as _p

    class _V:
        action, rule_id, dry_run = "deny", "r-deny", False

    class _Eng:
        def evaluate(self, req):
            return _V()

    monkeypatch.setattr("policy.get_engine", lambda: _Eng())
    monkeypatch.setattr(_db, "log_call", lambda *a, **k: None)

    dispatched = []

    class _Dummy:
        def send(self, **kw):
            dispatched.append(kw)
            raise AssertionError("must not dispatch")

    monkeypatch.setattr("adaptors.registry.get", lambda name: _Dummy())
    body = channels_api.SendBody(to="x", text="hi")
    with _p.raises(HTTPException) as ei:
        asyncio.run(channels_api.channel_send("telegram", body))
    assert ei.value.status_code == 403
    assert dispatched == []


def test_channel_send_approve_creates_pending(monkeypatch, tmp_path):
    import asyncio
    import db as _db
    import approvals as _A
    import channels_api
    monkeypatch.setattr(_A, "STORE_PATH", tmp_path / "appr.json")

    class _V:
        action, rule_id, dry_run = "approve", "r-appr", False

    class _Eng:
        def evaluate(self, req):
            return _V()

    monkeypatch.setattr("policy.get_engine", lambda: _Eng())
    monkeypatch.setattr(_db, "log_call", lambda *a, **k: None)
    body = channels_api.SendBody(to="x", text="hi")
    out = asyncio.run(channels_api.channel_send("telegram", body))
    assert out["ok"] is False and out["status"] == "pending"
    assert out["approval_id"].startswith("gw-")
    assert len(_A.list_pending()) == 1
