"""Loopback + per-launch token auth for the agent.

The situation this fixes
------------------------
The agent had no authentication of any kind. Every route - chat, research
submission, memory writes, document deletion, schedule creation - was reachable
by anything that could send an HTTP request to the port. Confirmed live: a
malicious web page could drive paid LLM calls and persist writes through the
user's browser, because there was no CSRF defence and nothing to forge.

Two facts make a full identity system unnecessary here:

  1. The agent runs on the user's own machine and binds to loopback.
  2. The realistic attacker is a *web page the user visits*, not a network
     peer. Such a page cannot read a cross-origin response body, and cannot
     send a custom header without a CORS preflight we never answer.

So the control that actually fits the threat is a **per-launch token**:
generated at startup, injected into the SPA shell, and required as a custom
header on every `/api/*` call. A remote page gets neither the token nor the
ability to send the header. This is not authentication of a user - there is no
user - it is origin enforcement, and it is honest to call it that.

What it does NOT protect against, stated plainly:
  * another process running as the same OS user (it can read the token file)
  * anything already on loopback
  * a browser extension with host permissions

What it DOES buy, and why it was worth doing before adding agent-generated UI:
  * CSRF from a web page is closed
  * the SSRF-adjacent surface shrinks to same-origin
  * a generated A2UI surface can no longer be aimed at the API from a page

Loopback binding guard
----------------------
Binding to 0.0.0.0 with no token turns every route into a network-exposed,
unauthenticated API. That is refused unless `ARIA_ALLOW_REMOTE=1` is set
explicitly, so it cannot happen by accident.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
from pathlib import Path

TOKEN_HEADER = "x-aria-token"
TOKEN_META = "aria-token"

# Endpoints reachable without a token. Deliberately tiny.
#   /api/health  - so a supervisor can tell "started" from "wedged", and so the
#                  console's connection indicator has something to call before it
#                  has a token. It exposes readiness and nothing else.
PUBLIC_PATHS = frozenset({"/api/health"})

_state_dir = Path(os.environ.get("S9_STATE_DIR")
                  or (Path(__file__).parent / "state"))
_token: str | None = None
_lock = threading.Lock()


class AuthNotConfigured(RuntimeError):
    """No token has been generated, so enforcement is inert."""


def state_dir() -> Path:
    return _state_dir


def is_enabled() -> bool:
    """True once a token exists.

    Inert until `configure()` runs, which happens on real startup. Test clients
    never launch the server, so they are unaffected - and the enforcement tests
    install a token explicitly rather than relying on that.
    """
    return _token is not None


def token() -> str:
    if _token is None:
        raise AuthNotConfigured("auth token not generated")
    return _token


def configure(force: bool | None = None) -> str | None:
    """Generate (or load) the per-launch token.

    A token from a previous run is NOT reused: it is regenerated every launch,
    so a token captured from an old process dies with that process. That is the
    point of "per-launch".
    """
    global _token
    with _lock:
        if _token is not None and not force:
            return _token
        if force is False:
            _token = None
            return None
        _token = secrets.token_urlsafe(32)
        _write_token_file(_token)
        return _token


def disable() -> None:
    """Remove enforcement. For tests, and for an explicit `ARIA_AUTH=off`."""
    global _token
    with _lock:
        _token = None
    try:
        (_state_dir / "agent.token").unlink()
    except OSError:
        pass


def _write_token_file(value: str) -> None:
    """Persist the token so a second Aria process (or a CLI) can find it.

    Written with owner-only permissions where the platform supports it. This is
    a convenience for local tools, not a secret store: anything running as the
    same user can read it.
    """
    try:
        _state_dir.mkdir(parents=True, exist_ok=True)
        path = _state_dir / "agent.token"
        path.write_text(value, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Windows ACLs are not POSIX modes; not worth failing over
    except OSError:
        pass


def check_header_value(value: str | None) -> bool:
    """Constant-time comparison, so a wrong token cannot be found byte by byte."""
    if not value or _token is None:
        return False
    return hmac.compare_digest(value.strip(), _token)


def is_public_path(path: str) -> bool:
    clean = path.rstrip("/") or "/"
    return clean in PUBLIC_PATHS


# ── origin enforcement ───────────────────────────────────────────────────────

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# `0.0.0.0` and `::` are deliberately absent: they mean "every interface", which
# is the opposite of loopback even though a loopback client still reaches them.
BIND_ALL = frozenset({"0.0.0.0", "::", "*", ""})

# Full forms of the IPv6 loopback address, before any port is stripped.
_IPV6_LOOPBACK = frozenset({"::1", "0:0:0:0:0:0:0:1"})


def host_is_loopback(host: str | None) -> bool:
    """Is this request Host / bind address the local machine?

    Handles the three shapes a host arrives in: bare IPv4, bracketed IPv6 with
    a port (`[::1]:8500`), and a bare IPv6 address. The bare-IPv6 case is the
    one that breaks a naive implementation - splitting `::1` on `:` yields an
    empty string, so `::1` stops being recognised as loopback and a legitimate
    local bind is refused.
    """
    if not host:
        return False
    h = host.strip().lower()
    if h.startswith("["):
        inner = h[1:h.index("]")] if "]" in h else h[1:]
        return host_is_loopback(inner)
    if ":" in h:
        return h in _IPV6_LOOPBACK
    if h in ("localhost", "127.0.0.1"):
        return True
    if h.startswith("127."):
        return True
    return False


def binding_is_loopback(host: str | None) -> bool:
    """Is the *bind address* local?

    `0.0.0.0` and `::` mean "every interface" and are NOT loopback, even though
    a loopback connection still works against them - which is exactly why this
    needs its own check rather than reusing `host_is_loopback`.
    """
    if not host:
        return False
    if host.strip().lower() in BIND_ALL:
        return False
    return host_is_loopback(host)


def assert_safe_binding(bind_host: str | None) -> tuple[bool, str]:
    """Refuse an unauthenticated network-exposed bind unless overridden.

    Returns `(ok, reason)`. The caller decides what to do, so this is testable
    without starting a server.
    """
    if os.getenv("ARIA_ALLOW_REMOTE") == "1":
        return True, "ARIA_ALLOW_REMOTE=1: remote bind explicitly allowed"
    if binding_is_loopback(bind_host):
        return True, "loopback bind"
    if not is_enabled():
        return False, (
            f"refusing to bind {bind_host!r}: that is every interface, and no "
            f"auth token is configured, so every /api route would be an "
            f"unauthenticated network endpoint. Bind 127.0.0.1, or set "
            f"ARIA_ALLOW_REMOTE=1 if you have a network boundary in front.")
    return True, f"non-loopback bind {bind_host!r} with a token configured"


def cross_origin_write(headers, method: str) -> str | None:
    """Return a rejection reason for a state-changing cross-origin request.

    The token is the primary control; this is defence in depth, and it catches
    the case where the token leaks (a same-origin XSS, a logged console) by
    refusing writes that arrive with someone else's `Origin`.
    """
    if method.upper() in SAFE_METHODS:
        return None
    origin = headers.get("origin")
    if not origin:
        # Same-origin fetches from our own SPA, and curl/CLI clients, send no
        # Origin at all. Absence is not evidence of cross-origin.
        return None
    host = headers.get("host")
    if not host:
        return None
    # Compare the origin's authority to the request's Host.
    tail = origin.split("://", 1)[-1].strip("/").lower()
    return None if tail == host.strip().lower() else (
        f"cross-origin {method} from {origin} refused")


def token_meta_snippet() -> str:
    """The meta tag injected into the SPA shell.

    The SPA must learn the token somehow, and the shell is the natural place:
    it is same-origin, and a cross-origin page cannot read it. This is why the
    token is not a defence against a local process.
    """
    if _token is None:
        return ""
    return (f'<meta name="{TOKEN_META}" content="{_token}">')
