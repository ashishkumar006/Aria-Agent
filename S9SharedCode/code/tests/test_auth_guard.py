"""The token guard, and the claims made about what it protects.

These tests are the security contract. A guard that is not tested is a comment,
and the properties asserted here are the ones that were previously open:

  * CSRF from a web page was live and paid for
  * every /api route was reachable by anything that could open a socket
  * SSRF-adjacent surface was open to any origin

What the control actually is, so the tests can be honest about it: a
per-launch token required as a custom header. That closes cross-origin
requests. It does NOT stop another process running as the same OS user, and
these tests say so rather than implying otherwise.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import agent_server  # noqa: E402


class token_on:
    """Context manager: enforcement on inside, guaranteed off after.

    Enforcement is process-global, so a test that switches it on and does not
    switch it off poisons every later test in the session. That is not
    hypothetical - the first version of this file returned a bare client and
    left the token set, and 32 unrelated tests failed with 403s. Hence the
    context manager and no other way to enable it.
    """

    def __init__(self, token: str = "t-" + "x" * 40):
        self.token = token

    def __enter__(self) -> TestClient:
        auth.configure(force=True)
        auth._token = self.token
        return TestClient(agent_server.app)

    def __exit__(self, *exc) -> bool:
        auth.disable()
        return False


# ── token mechanics ─────────────────────────────────────────────────────────

def test_a_token_is_long_and_unpredictable():
    tok = auth.configure(force=True)
    try:
        assert tok and len(tok) >= 32
        assert set(tok) != {"a"}, "a constant token is not a token"
    finally:
        auth.disable()


def test_each_launch_gets_a_different_token():
    a = auth.configure(force=True)
    b = auth.configure(force=True)
    try:
        assert a != b, "a reused token outlives the process that issued it"
    finally:
        auth.disable()


def test_enforcement_is_inert_until_configured():
    """Test clients and in-process use never launch a server, so they must not
    be gated. If they were, 600 tests would need a token header."""
    auth.disable()
    assert auth.is_enabled() is False
    assert auth.check_header_value("anything") is False
    assert auth.token_readable() is False if hasattr(auth, "token_readable") else True


def test_header_comparison_is_exact_and_whitespace_tolerant():
    auth.configure(force=True)
    tok = auth._token
    try:
        assert auth.check_header_value(tok) is True
        assert auth.check_header_value(f"  {tok}  ") is True
        assert auth.check_header_value(tok[:-1]) is False
        assert auth.check_header_value(tok + "x") is False
        assert auth.check_header_value("") is False
        assert auth.check_header_value(None) is False
        # A prefix must not pass: that would make it a public prefix.
        assert auth.check_header_value(tok[:8]) is False
    finally:
        auth.disable()


# ── the binding guard ────────────────────────────────────────────────────────

def test_loopback_binds_are_allowed():
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.5"):
        ok, why = auth.assert_safe_binding(host)
        assert ok, f"{host}: {why}"


def test_binding_every_interface_without_a_token_is_refused():
    """0.0.0.0 means every interface. With no token that turns the whole API
    into an open network endpoint, and it must not happen by accident."""
    auth.disable()
    for host in ("0.0.0.0", "::", "*"):
        ok, why = auth.assert_safe_binding(host)
        assert not ok, f"{host} was allowed with no token"
        assert "ARIA_ALLOW_REMOTE" in why, why


def test_binding_every_interface_with_a_token_is_allowed_but_noted():
    auth.configure(force=True)
    try:
        ok, why = auth.assert_safe_binding("0.0.0.0")
        assert ok, why
        assert "token configured" in why, why
    finally:
        auth.disable()


def test_a_missing_host_is_treated_as_not_loopback():
    """Defaulting to 'safe' on a parse failure would defeat the check."""
    assert auth.binding_is_loopback(None) is False
    assert auth.binding_is_loopback("") is False


# ── origin classification ────────────────────────────────────────────────────

def test_loopback_host_detection():
    assert auth.host_is_loopback("127.0.0.1") is True
    assert auth.host_is_loopback("127.5.5.5") is True
    assert auth.host_is_loopback("localhost") is True
    assert auth.host_is_loopback("[::1]:8500") is True
    assert auth.host_is_loopback("192.168.1.20") is False
    assert auth.host_is_loopback("evil.example") is False
    assert auth.host_is_loopback(None) is False


def test_safe_methods_are_never_blocked_by_the_origin_check():
    h = {"origin": "https://evil.example", "host": "127.0.0.1:8500"}
    assert auth.cross_origin_write(h, "GET") is None
    assert auth.cross_origin_write(h, "HEAD") is None


def test_a_cross_origin_write_is_refused():
    h = {"origin": "https://evil.example", "host": "127.0.0.1:8500"}
    reason = auth.cross_origin_write(h, "POST")
    assert reason and "cross-origin" in reason
    assert "evil.example" in reason


def test_a_same_origin_write_is_allowed():
    h = {"origin": "http://127.0.0.1:8500", "host": "127.0.0.1:8500"}
    assert auth.cross_origin_write(h, "POST") is None


def test_absent_origin_is_not_treated_as_cross_origin():
    """Our own SPA sends no Origin on same-origin fetches, and curl sends none
    at all. Absence is not evidence of attack."""
    assert auth.cross_origin_write({"host": "127.0.0.1:8500"}, "POST") is None


# ── the middleware, end to end ───────────────────────────

def test_api_calls_without_the_token_are_refused():
    with token_on() as c:
        r = c.get("/api/documents")
        assert r.status_code == 403, r.text
        assert "X-Aria-Token" in r.json()["error"]


def test_a_wrong_token_is_refused():
    with token_on() as c:
        assert c.get("/api/documents",
                     headers={"X-Aria-Token": "wrong"}).status_code == 403


def test_a_prefix_of_the_token_is_refused():
    """A prefix would make it a public prefix rather than a secret."""
    with token_on() as c:
        assert c.get("/api/documents",
                     headers={"X-Aria-Token": auth._token[:8]}).status_code == 403


def test_the_correct_token_is_accepted():
    with token_on() as c:
        r = c.get("/api/documents", headers={"X-Aria-Token": auth._token})
        assert r.status_code == 200, r.text


def test_a_cross_origin_post_with_a_valid_token_is_still_refused():
    """Defence in depth: if the token ever leaks (same-origin XSS, a logged
    console), a write arriving from someone else's Origin is still refused."""
    with token_on() as c:
        r = c.post("/api/chat/threads/x/prefs",
                   headers={"X-Aria-Token": auth._token,
                            "Origin": "https://evil.example"},
                   json={"use_documents": False})
        assert r.status_code == 403, r.text
        assert "cross-origin" in r.json()["error"]


def test_a_cross_origin_get_with_a_valid_token_is_allowed():
    """The origin check targets writes. A cross-origin GET cannot exfiltrate
    anything, because the response body is unreadable without CORS - so
    blocking it would only break things."""
    with token_on() as c:
        r = c.get("/api/documents", headers={"X-Aria-Token": auth._token,
                                             "Origin": "https://elsewhere"})
        assert r.status_code == 200, r.text


def test_health_stays_public_so_a_supervisor_can_probe():
    with token_on() as c:
        assert c.get("/api/health").status_code == 200


def test_non_api_routes_are_not_gated():
    """The shell itself must load, and static assets are not the API."""
    with token_on() as c:
        assert c.get("/docs").status_code in (200, 307)


def test_capabilities_requires_the_token_like_every_other_api_route():
    """It exposes the tool inventory, so it is not public information."""
    with token_on() as c:
        assert c.get("/api/capabilities").status_code == 403
        assert c.get("/api/capabilities",
                     headers={"X-Aria-Token": auth._token}).status_code == 200


def test_enforcement_does_not_leak_into_the_next_test():
    """The failure mode this file exists partly to prevent: a test enables the
    token, does not disable it, and every later test 403s."""
    with token_on():
        pass
    assert auth.is_enabled() is False
