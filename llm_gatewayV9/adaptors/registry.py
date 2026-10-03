"""Adaptor registry: name -> instance, built lazily, env-gated.

Only adaptors whose credentials are present are "live"; the rest report
configured=false with their required keys (names only) so the dashboard
and check_keys can guide setup without leaking values.
"""
from __future__ import annotations

from typing import Any

from .base import BaseAdaptor

_BUILDERS: dict[str, str] = {
    # name -> "module:Class" (lazy import so one broken dep can't kill all).
    # Each channel is a package: adapter.py (transport) + schemas.py (wire
    # types) + verify.py / README.md where the channel warrants them.
    "telegram": "adaptors.telegram.adapter:TelegramAdaptor",
    "discord": "adaptors.discord.adapter:DiscordAdaptor",
    "matrix": "adaptors.matrix.adapter:MatrixAdaptor",
    "line": "adaptors.line.adapter:LineAdaptor",
    "webhook": "adaptors.webhook.adapter:GenericWebhookAdaptor",
    "webui": "adaptors.webui.adapter:WebUIAdaptor",
    "slack": "adaptors.slack.adapter:SlackAdaptor",
    "signal": "adaptors.signal.adapter:SignalAdaptor",
    "gmail": "adaptors.gmail.adapter:GmailAdaptor",
    "imap": "adaptors.imap.adapter:ImapSmtpAdaptor",
    "twilio_sms": "adaptors.twilio_sms.adapter:TwilioSmsAdaptor",
    "whatsapp_twilio": "adaptors.whatsapp_twilio.adapter:WhatsAppTwilioAdaptor",
    "local_mic": "adaptors.local_mic.adapter:LocalMicAdaptor",
    "teams": "adaptors.teams.adapter:TeamsAdaptor",
    "whatsapp_meta": "adaptors.whatsapp_meta.adapter:WhatsAppMetaAdaptor",
    "twilio_voice": "adaptors.twilio_voice.adapter:TwilioVoiceAdaptor",
}

_CACHE: dict[str, BaseAdaptor] = {}
_ERRORS: dict[str, str] = {}


def _build(name: str) -> BaseAdaptor | None:
    if name in _CACHE:
        return _CACHE[name]
    if name in _ERRORS:
        return None
    target = _BUILDERS.get(name)
    if not target:
        return None
    mod_name, cls_name = target.split(":")
    try:
        import importlib
        mod = importlib.import_module(mod_name, package=__package__)
        inst = getattr(mod, cls_name)()
        _CACHE[name] = inst
        return inst
    except Exception as e:  # one broken adaptor must not kill the registry
        _ERRORS[name] = f"{type(e).__name__}: {e}"
        return None


def get(name: str) -> BaseAdaptor | None:
    return _build((name or "").lower())


def names() -> list[str]:
    return sorted(_BUILDERS)


def inventory() -> list[dict[str, Any]]:
    """Full registry status for /v1/channels (no secret values, ever)."""
    out = []
    for name in names():
        inst = _build(name)
        if inst is None:
            out.append({"name": name, "available": False,
                        "configured": False, "required_keys": [],
                        "error": _ERRORS.get(name, "unknown")})
            continue
        try:
            out.append({"name": name, "available": True,
                        "configured": inst.is_configured(),
                        "required_keys": inst.required_keys(),
                        "capabilities": inst.capabilities.__dict__,
                        "cost_hint_usd": inst.cost_hint_usd()})
        except Exception as e:
            out.append({"name": name, "available": False,
                        "configured": False, "required_keys": [],
                        "error": f"{type(e).__name__}: {e}"})
    return out


def reset() -> None:  # tests only
    _CACHE.clear()
    _ERRORS.clear()
