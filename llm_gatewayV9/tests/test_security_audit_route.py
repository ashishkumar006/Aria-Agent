"""`GET /v1/security/audit` — the operator-only view of the hash-chained log.

`security_audit` itself has one unit test (`test_security_hardening.py`), but
the route that serves it had none: nothing proved that the view an operator
reads actually verifies the chain it prints, that a break shows up there rather
than being swallowed, or that a non-operator cannot read the record of their
own failed attempts.

The chain is the product here. Deleting, reordering or editing a row must make
`verify` fail at the first changed entry, and the dashboard must say so — an
audit log that silently reports "ok" after someone edited it is worse than no
log, because it is read as proof.

Everything writes into `tmp_path`: `AUDIT_PATH`, `KEY_PATH` and the cached
chain state are patched, so no test here appends a row to the real
`state/security_audit.jsonl` or creates a key file.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gateway_auth  # noqa: E402
import main as M  # noqa: E402
import redact  # noqa: E402
import security_audit  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

# Must equal tests/conftest.py's TEST_TOKEN.
TOKEN = "test-gateway-token"


@pytest.fixture
def client():
    return TestClient(M.app)


@pytest.fixture(autouse=True)
def isolated_audit(tmp_path, monkeypatch):
    """A private log file, a private key, a private chain state.

    `init()` is deliberately not called: it would `touch()` the log, and the
    first test below needs the genuinely missing-file case that a fresh install
    has. `record()` creates the file itself, so nothing here depends on init.
    """
    monkeypatch.setattr(security_audit, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(security_audit, "KEY_PATH", tmp_path / "audit.key")
    # A configured key means no key file is written anywhere, not even in tmp.
    monkeypatch.setenv("GATEWAY_AUDIT_KEY", "zz-fake-audit-key")
    monkeypatch.setattr(security_audit, "_key", None)
    monkeypatch.setattr(security_audit, "_last_hash", "")
    # `record()` suppresses identical auth_denied rows for 5s. That is a disk
    # guard in production and a flake here: each test needs its own refusal
    # recorded.
    monkeypatch.setattr(security_audit, "_dedupe", {})
    # Fake secrets must not leak into the process-wide redactor either.
    monkeypatch.setattr(redact, "_secrets", set(redact._secrets))
    yield tmp_path


def _lines():
    p = Path(security_audit.AUDIT_PATH)
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def _write(lines):
    Path(security_audit.AUDIT_PATH).write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def seeded_chain(isolated_audit):
    """Three linked events, the newest last."""
    for name, reason in (("login", "first"), ("admin_key_write", "second"),
                         ("ssrf_blocked", "third")):
        security_audit.record(name, actor="operator", reason=reason)
    assert len(_lines()) == 3
    return isolated_audit


# ── the view ─────────────────────────────────────────────────────────────────
def test_the_audit_view_reports_a_verifying_chain(seeded_chain, client):
    body = client.get("/v1/security/audit").json()
    assert body["verify"] == {"ok": True, "entries": 3, "first_bad": None}
    assert len(body["events"]) == 3


def test_the_events_keep_their_order_and_fields(seeded_chain, client):
    events = client.get("/v1/security/audit").json()["events"]
    assert [e["event"] for e in events] == ["login", "admin_key_write",
                                            "ssrf_blocked"]
    assert [e["reason"] for e in events] == ["first", "second", "third"]
    assert all(e["actor"] == "operator" for e in events)
    # The chain material stays server-side: `recent` strips each record's HMAC
    # so a page refresh cannot be used to harvest the chain for an offline
    # forgery attempt.
    assert all("hash" not in e for e in events)
    assert "hash" not in client.get("/v1/security/audit").text


def test_limit_bounds_the_view_but_never_the_verification(seeded_chain, client):
    """`limit` is a display window. Truncating it must not truncate the
    verification, or a short page looks like a clean short log."""
    body = client.get("/v1/security/audit", params={"limit": 1}).json()
    assert len(body["events"]) == 1
    assert body["events"][0]["event"] == "ssrf_blocked"
    assert body["verify"]["entries"] == 3 and body["verify"]["ok"] is True


def test_an_empty_log_is_reported_as_clean_not_as_an_error(client):
    """A fresh install has no audit file at all. The route must answer with an
    empty verified log rather than a 500, otherwise the panel reads as broken
    on exactly the machines that have nothing to show."""
    body = client.get("/v1/security/audit").json()
    assert body["events"] == []
    assert body["verify"]["ok"] is True and body["verify"]["entries"] == 0


# ── tamper evidence ──────────────────────────────────────────────────────────
def test_an_edited_record_breaks_the_chain_and_shows_up_in_the_view(
        seeded_chain, client):
    """The point of the HMAC per row. Editing a `reason` in place must be
    detected at that row — and the route must report the break instead of
    printing an "ok" the operator would rely on."""
    lines = _lines()
    rec = json.loads(lines[1])
    assert rec["reason"] == "second"
    rec["reason"] = "nothing to see here"
    lines[1] = json.dumps(rec)
    _write(lines)

    bad = security_audit.verify()
    assert bad["ok"] is False
    assert bad["first_bad"] == 2
    assert bad["error"] == "record hash mismatch"

    served = client.get("/v1/security/audit").json()
    assert served["verify"]["ok"] is False, "the view hid a broken chain"
    assert served["verify"]["first_bad"] == 2
    assert served["events"][1]["reason"] == "nothing to see here", (
        "the tampered row is still rendered as if it were trustworthy")


def test_deleting_a_record_breaks_the_link_below_it(seeded_chain, client):
    """Removing a row is not the same as never having had it: the surviving
    records still chain through it, so the next row's `prev` no longer matches
    and the deletion is visible at row 2."""
    lines = _lines()
    _write(lines[:1] + lines[2:])          # drop the middle event
    bad = security_audit.verify()
    assert bad["ok"] is False and bad["first_bad"] == 2
    assert bad["error"] == "broken previous-hash link"
    assert client.get("/v1/security/audit").json()["verify"]["ok"] is False


def test_an_unparseable_line_is_reported_not_skipped(seeded_chain, client):
    """A hand-truncated file must not verify by accident: `verify` reads lines
    in order and needs the broken row's index."""
    lines = _lines()
    lines[2] = "{not json"
    _write(lines)
    bad = security_audit.verify()
    assert bad["ok"] is False and bad["first_bad"] == 3
    assert bad["error"] == "unparseable line"


# ── what the log must never contain ─────────────────────────────────────────
def test_a_secret_named_in_an_event_is_redacted_before_it_is_written(
        seeded_chain, client):
    """Audit rows carry failure text from provider errors, and Gemini puts the
    key in the URL. The log is readable by whoever holds the operator token, so
    the redaction has to happen on the way in."""
    pasted = "zzfake-leaked-provider-key-991122"
    redact.register_secret(pasted)
    security_audit.record("provider_error",
                          reason=f"GET https://gen.ai/v1?key={pasted} failed")
    text = client.get("/v1/security/audit").text
    assert pasted not in text
    assert redact.REDACTED in text
    assert security_audit.verify()["ok"] is True, "redaction broke the chain"


def test_a_refused_call_is_itself_recorded_and_still_chains(client):
    """Two properties in one: the break-in attempt lands in the log, and adding
    it keeps the chain valid (a refused request must not corrupt the store an
    operator is about to read)."""
    anon = TestClient(M.app)
    anon.headers.pop("X-Gateway-Token", None)
    assert anon.get("/v1/security/audit").status_code == 401
    body = client.get("/v1/security/audit").json()
    assert body["verify"]["ok"] is True
    assert any(e["event"] == "auth_denied" for e in body["events"])
    assert TOKEN not in str(body["events"])


# ── who may read it ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/security/audit"),
    ("GET", "/v1/security/audit?limit=5"),
])
def test_the_view_refuses_a_caller_without_the_operator_token(
        seeded_chain, method, path):
    client = TestClient(M.app)
    client.headers.pop("X-Gateway-Token", None)
    r = client.request(method, path)
    assert r.status_code == 401, (path, r.text[:200])
    assert TOKEN not in r.text


def test_a_user_token_holder_is_not_shown_the_security_log(seeded_chain):
    """A per-user credential is an inference credential. The audit log names
    that user's own failed attempts and every other tenant's administration —
    reading it must require the operator context, not merely a valid token."""
    non_admin = gateway_auth.AuthContext(admin=False, user_id="u-1", via="user")
    req = SimpleNamespace(state=SimpleNamespace(auth=non_admin))
    with pytest.raises(HTTPException) as denied:
        asyncio.run(M.security_audit_view(request=req, limit=10))
    assert denied.value.status_code == 403
    assert "operator" in str(denied.value.detail)

    anonymous = SimpleNamespace(state=SimpleNamespace(auth=None))
    with pytest.raises(HTTPException) as denied2:
        asyncio.run(M.security_audit_view(request=anonymous, limit=10))
    assert denied2.value.status_code == 403


# ── the chain must survive more than one writer ─────────────────────────────
# Found in the LIVE log, not in a test: `state/security_audit.jsonl` had two
# `broken previous-hash link` breaks (lines 122 and 236 of 273) and `verify()`
# returned `{"ok": false}`. `_load_last()` cached the tail once and set
# `_last_loaded = True`, so every later record chained from THIS process's
# `_last_hash`. The gateway, a test client sharing the real state dir, and a
# second gateway launched by `ensure_gateway()` all append to one file — and
# each forked the chain at its own boundary.
#
# A tamper-evident log with a forked chain is not evidence of anything, and
# invariant 7 ("components must not be able to edit or delete their own audit
# logs") is the promise this module exists to keep.
def test_two_writers_on_one_log_still_form_one_chain(monkeypatch):
    """Simulated as two processes: the second starts with no memory of line 1.

    A fresh process has `_last_hash == ""`, which is exactly the state that
    forked the live log. Chaining must come from the file, not from memory.
    """
    security_audit.record("user_created", actor="operator", path="/v1/users")

    # "Restart": drop every bit of in-memory chain state, keep the file.
    monkeypatch.setattr(security_audit, "_last_hash", "")
    security_audit.record("auth_denied", ip="203.0.113.9",
                          path="/v1/control/telegram",
                          reason="missing or invalid credential")

    lines = [l for l in _lines() if l.strip()]
    assert len(lines) == 2, lines
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert second["prev"] == first["hash"], (
        "the second writer restarted the lineage instead of continuing it — "
        "this is the live fork")
    verdict = security_audit.verify()
    assert verdict["ok"] is True, verdict


def test_a_repeated_restart_never_forks_the_chain(monkeypatch):
    """Three sequential 'processes', eleven records, one unbroken lineage."""
    for i in range(3):
        for n in range(4):
            security_audit.record("key_added", actor="operator",
                                  path=f"/v1/users/u-{i}/keys#{n}")
        monkeypatch.setattr(security_audit, "_last_hash", "")
    verdict = security_audit.verify()
    assert verdict["ok"] is True, verdict
    assert verdict["entries"] == 12, verdict


def test_verify_reports_the_line_that_broke_not_just_that_something_did():
    """The diagnosis has to survive into the response, or the operator cannot
    act on it. `first_bad` is a 1-based line number in the log."""
    security_audit.record("user_created", actor="operator", path="/v1/users")
    lines = [l for l in _lines() if l.strip()]
    # Rewrite line 1's payload while keeping its old hash: content edited, chain
    # claim unchanged. verify() must reject the hash, not the link.
    rec = json.loads(lines[0])
    rec["reason"] = "REDACTED-BY-AN-ATTACKER"
    Path(security_audit.AUDIT_PATH).write_text(
        json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    verdict = security_audit.verify()
    assert verdict["ok"] is False, verdict
    assert verdict["first_bad"] == 1, verdict


def test_an_unreadable_tail_does_not_silently_restart_the_lineage(monkeypatch):
    """If the tail read fails, fall back to the in-memory hash.

    Returning "" there would append a record whose `prev` is empty — indistinguishable
    from a brand-new log, and the exact failure being fixed. Better to keep
    chaining from the last hash we did see than to fabricate a fresh start.
    """
    security_audit.record("user_created", actor="operator", path="/v1/users")
    first = json.loads([l for l in _lines() if l.strip()][-1])
    monkeypatch.setattr(security_audit, "_last_hash", first["hash"])

    real_open = Path.open

    def _fail(self, *a, **k):
        raise OSError("simulated disk fault")

    monkeypatch.setattr(Path, "open", _fail, raising=False)
    try:
        rec = security_audit.record("admin_key_write", actor="operator",
                                    path="/v1/control/keys")
    finally:
        monkeypatch.setattr(Path, "open", real_open, raising=False)
    assert rec["prev"] == first["hash"], rec
