"""Regression + behaviour tests for the computer-use safety gates.

Covers:
  * enabled/mode defaults (charter §13)
  * approval-pattern tokenisation (whole-word, so Format-Table != format)
  * path / command denylists + allow-list
  * approval lifecycle (create / list / resolve)
  * AUDIT trail records BOTH approved and rejected outcomes
    (regresses the bug where approvals were never logged)
  * secret redaction on export
"""
from __future__ import annotations

import os

from computer_use.safety.gates import SafetyGates


def _fresh() -> SafetyGates:
    # Isolate from .env pollution (mcp_server loads .env with override=True
    # historically; even with override=False, prior test state can leak).
    for var in ("COMPUTER_USE_ENABLED", "COMPUTER_USE_MODE",
                "COMPUTER_USE_ALLOW", "COMPUTER_USE_DENY"):
        os.environ.pop(var, None)
    from computer_use.safety import gates as _gates
    _gates.reset_shared_gates()
    return SafetyGates()


def test_enabled_defaults_false():
    assert _fresh().enabled is False


def test_mode_defaults_dry_run():
    assert _fresh().mode == "dry-run"


def test_needs_approval_detects_destructive_token():
    g = _fresh()
    assert g.needs_approval("run_command", {"command": "rm -rf /tmp/x"}) is True
    assert g.needs_approval("run_command", {"command": "sudo reboot"}) is True


def test_needs_approval_ignores_substring_tokens():
    # 'format' inside 'format-table' must NOT trigger approval
    g = _fresh()
    assert g.needs_approval("run_command", {"command": "Get-PSDrive | Format-Table"}) is False
    # 'restart' inside 'restart-service' must NOT trigger approval
    assert g.needs_approval("run_command", {"command": "Restart-Service foo"}) is False


def test_path_blocked_defence_in_depth():
    g = _fresh()
    assert g.path_blocked(r"C:\Windows\system32\foo.exe") is True
    assert g.path_blocked(r"/etc/passwd") is True
    assert g.path_blocked(r"C:\Users\me\Desktop\notes.txt") is False


def test_cmd_blocked_denylist_and_allowlist():
    os.environ["COMPUTER_USE_ALLOW"] = "echo;ls"
    os.environ["COMPUTER_USE_DENY"] = "deltree"
    try:
        g = SafetyGates()
        assert g.cmd_blocked("deltree C:\\") is not None
        assert g.cmd_blocked("echo hello") is None
        # not on the allow-list -> blocked
        assert g.cmd_blocked("mkdir foo") is not None
    finally:
        os.environ.pop("COMPUTER_USE_ALLOW", None)
        os.environ.pop("COMPUTER_USE_DENY", None)


def test_create_and_list_approval():
    g = _fresh()
    aid = g.create_approval("run_command", {"command": "echo hi"})
    assert aid
    pending = g.list_approvals()
    assert any(p["id"] == aid for p in pending)
    assert all(p["status"] == "pending" for p in pending)


def test_resolve_unknown_returns_none():
    g = _fresh()
    assert g.resolve("does-not-exist", approve=True) is None


def test_resolve_approve_is_audited():
    g = _fresh()
    aid = g.create_approval("run_command", {"command": "echo hi"})
    g.resolve(aid, approve=True)
    lines = SafetyGates.export_audit(redact=True)
    assert any("approved" in line for line in lines), f"approved not in audit: {lines}"


def test_resolve_reject_is_audited():
    g = _fresh()
    aid = g.create_approval("run_command", {"command": "rm -rf /"})
    g.resolve(aid, approve=False)
    lines = SafetyGates.export_audit(redact=True)
    assert any("rejected" in line for line in lines), f"rejected not in audit: {lines}"


def test_redact_strips_secrets():
    sample = "token=sk-ABCDEFGH12345678secret api_key=abc123 Bearer xyz.abc.def --password hunter2"
    out = SafetyGates.redact(sample)
    assert "sk-ABCDEFGH" not in out
    assert "hunter2" not in out
    assert "***REDACTED***" in out


def test_export_audit_redaction_toggle():
    raw = "token=sk-ABCDEFGH12345678secret"
    # redact=True must mask; redact=False must keep the literal
    redacted = SafetyGates.export_audit(redact=True)
    assert all("sk-ABCDEFGH" not in ln for ln in redacted)
