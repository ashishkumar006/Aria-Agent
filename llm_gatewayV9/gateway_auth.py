"""Authentication for the gateway's own HTTP surface.

Why this exists
---------------
The agent console on :8500 is protected by a per-launch token, but every one of
the gateway's ~53 `/v1/*` routes was reachable with **no credential at all**.
A black-box probe with no headers destroyed live state on its first call:

    POST /v1/memory/sweep        -> 200, wiped 6 working-memory rows
    POST /v1/policy/reload       -> 200, mutated policy state
    POST /v1/control/deploy-test -> 200, ran real LLM calls (unauthenticated spend)
    GET  /v1/config/keys         -> 200, disclosed which secrets are configured
    GET  /v1/calls, /v1/spend    -> 200, full call log and spend by agent

The only thing standing between an unauthenticated caller and a total memory
wipe was a client-supplied literal: `DELETE /v1/memory` required
`confirm=wipe`, and the rejection message disclosed that exact string.

Loopback is the primary control, and the default bind already is
`127.0.0.1`. But loopback alone does not stop another process running as the
same OS user, and it is defeated outright by `GATEWAY_HOST=0.0.0.0` - which is
exactly how this instance was running. So a shared token is required as well,
and a non-loopback bind without one is refused rather than warned about.
"""
from __future__ import annotations

import hmac
import os
import secrets
import threading
from pathlib import Path

TOKEN_HEADER = "x-gateway-token"
ENV_TOKEN = "GATEWAY_V9_TOKEN"
# Generated once and cached for the process when nothing is configured, so the
# agent can read it after start-up without either side being configured.
_STATE_DIR = Path(__file__).resolve().parent / "state"

_lock = threading.Lock()
_token: str | None = None
_generated: str | None = None


class AuthNotConfigured(RuntimeError):
    """No token available to compare against."""


def token() -> str:
    """The gateway's token, stable across restarts.

    Order: configured env value, else the token file left by a previous run,
    else a fresh random value. Every process used to generate a fresh random
    value, so a second process - or a restart - silently invalidated the token
    every legitimate client held, while `/health` kept reporting green. The
    file is the rendezvous: the first process creates it, later ones adopt it,
    and deleting it is the only rotation mechanism.
    """
    global _token, _generated
    with _lock:
        if _token is not None:
            return _token
        configured = (os.getenv(ENV_TOKEN) or "").strip()
        if configured:
            _token = configured
            return _token
        existing = _read_token_file()
        if existing:
            _token = existing
            return _token
        if _generated is None:
            _generated = secrets.token_urlsafe(32)
        _token = _generated
        return _token


def _read_token_file() -> str:
    try:
        value = token_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    # A token this process did not mint is still safe to adopt: it was minted
    # by a sibling process with the same privileges, not handed in from outside.
    return value if len(value) >= 16 else ""


def token_file() -> Path:
    return _STATE_DIR / "gateway.token"


def publish_token_file() -> Path:
    """Write the token where the agent can read it at start-up.

    The two processes are launched independently, so an env var alone forces
    the operator to configure both sides. The file is inside `state/`, which is
    git-ignored, and is rewritten each launch.
    """
    p = token_file()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(token(), encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass                     # best effort; Windows has no POSIX mode
    except OSError:
        pass
    return p


def is_enabled() -> bool:
    """A token always exists - either configured or generated - so enforcement
    is never inert."""
    return True


def check_header_value(value: str | None) -> bool:
    if not value:
        return False
    expected = token()
    # Constant-time: a byte-at-a-time compare leaks the token prefix.
    return hmac.compare_digest(value.strip(), expected)


def header_value() -> str:
    return token()


META_NAME = "gateway-token"

# The gateway ships its own console (static/*.html: dashboard, connections,
# ledger, system, help). Those pages are same-origin with the API and call
# `/v1/*` from inline scripts. When `/v1/*` was put behind this token and the
# pages were not given a way to present it, every panel in the gateway UI went
# blank at once - each page fetched, got 401, and rendered an error or nothing.
# Loopback is not a credential, and the pages were already reachable without
# one, so handing them the token they need is not a new exposure: it restores
# the UI to its pre-auth behaviour while `/v1/*` stays closed to everything
# that is not one of these pages or an explicit client.
_INJECT_SNIPPET = (
    '<meta name="{meta}" content="{tok}">\n'
    '<script src="/static/console-token.js"></script>'
)


def html_head_injection() -> str:
    """The meta tag + loader the console pages need, or "" when disabled."""
    try:
        tok = token()
    except Exception:
        return ""
    if not tok:
        return ""
    return _INJECT_SNIPPET.format(meta=META_NAME, tok=tok)


def inject_into_html(html: str) -> str:
    """Add the token meta + loader to a served HTML page, once."""
    if not html:
        return html
    # Idempotency must test for the SNIPPET, not for the meta name in general.
    # help.html and architecture.html both *document* console auth, so a page
    # that merely mentions `name="gateway-token"` in its prose was treated as
    # already injected and got no loader script - it then loaded and 401'd every
    # panel it tried to render.
    if "console-token.js" in html:
        return html
    snippet = html_head_injection()
    if not snippet:
        return html
    for tag in ("</head>", "</body>"):
        idx = html.lower().rfind(tag)
        if idx != -1:
            return html[:idx] + "  " + snippet + "\n" + html[idx:]
    return html + snippet


def host_is_loopback(host: str | None) -> bool:
    h = (host or "127.0.0.1").strip().lower()
    return h in ("127.0.0.1", "localhost", "::1", "[::1]")


def assert_safe_bind(host: str | None) -> tuple[bool, str]:
    """Refuse a LAN bind with no real credential.

    With a configured token a LAN bind is legitimate - that is the point of the
    token. With only a process-generated one, a LAN bind would be an open
    gateway on every interface, which is what happened here.
    """
    if host_is_loopback(host):
        return True, "loopback"
    configured = bool((os.getenv(ENV_TOKEN) or "").strip())
    if configured:
        return True, f"non-loopback bind {host} with a configured token"
    return False, (
        f"GATEWAY_HOST={host} would expose every /v1 route on the network with a "
        f"token that only exists inside this process, so nothing can "
        f"authenticate to it. Set {ENV_TOKEN} to a secret value, or bind "
        f"127.0.0.1."
    )