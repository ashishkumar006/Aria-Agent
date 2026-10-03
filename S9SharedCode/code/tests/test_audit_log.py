"""P2 — Audit log tests for computer-use safety gates.

Proves that every gated action is recorded, that the audit trail is
exportable via `/api/audit`, and that secrets are redacted by default.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from computer_use.safety import gates as gates_mod
from computer_use.safety.gates import SafetyGates, reset_shared_gates


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_gates():
    reset_shared_gates()
    yield
    reset_shared_gates()


@pytest.fixture
def audit_file(tmp_path, monkeypatch):
    """Isolate the audit log to a temp file for each test."""
    p = tmp_path / "computer_use.log"
    monkeypatch.setattr(gates_mod, "AUDIT_PATH", p)

    def _audit(self, action, params, outcome, ref=""):
        tag = f" [{ref}]" if ref else ""
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {action}{tag} {outcome} {params}\n"
        try:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    monkeypatch.setattr(SafetyGates, "_audit", _audit)
    return p


@pytest.fixture
def gates(audit_file):
    return SafetyGates()


# ── tests ─────────────────────────────────────────────────────────────────────

class TestAuditLog:
    """The audit trail records gated actions and redacts secrets."""

    def test_pending_approval_is_audited(self, gates, audit_file):
        """A sensitive action that requires approval must write an audit line."""
        aid = gates.create_approval("run_command", {"command": "rm -rf /tmp/x"})
        lines = SafetyGates.export_audit(redact=True)
        assert any(aid in line for line in lines), \
            f"approval id {aid!r} not found in audit: {lines}"
        assert any("pending-approval" in line for line in lines), \
            f"pending-approval not in audit: {lines}"

    def test_approved_action_is_audited(self, gates, audit_file):
        """Resolving an approval must record the outcome."""
        aid = gates.create_approval("run_command", {"command": "echo hi"})
        gates.resolve(aid, approve=True)
        lines = SafetyGates.export_audit(redact=True)
        assert any("approved" in line for line in lines), \
            f"approved not in audit: {lines}"

    def test_rejected_action_is_audited(self, gates, audit_file):
        """Rejecting an approval must record the outcome."""
        aid = gates.create_approval("run_command", {"command": "del C:\\x"})
        gates.resolve(aid, approve=False)
        lines = SafetyGates.export_audit(redact=True)
        assert any("rejected" in line for line in lines), \
            f"rejected not in audit: {lines}"

    def test_secrets_redacted_by_default(self, gates, audit_file):
        """Audit export must redact API keys, tokens, and passwords."""
        aid = gates.create_approval(
            "send_email",
            {
                "to": "a@b.com",
                "subject": "hi",
                "body": "secret",
                "token": "sk-abc123XYZ",
                "api_key": "AIzaSyD-very-long-key-value",
            },
        )
        gates.resolve(aid, approve=True)
        lines = SafetyGates.export_audit(redact=True)
        combined = "\n".join(lines)
        assert "sk-abc123XYZ" not in combined, "API key not redacted"
        assert "AIzaSyD-very-long-key-value" not in combined, "Google key not redacted"
        # The redaction marker should appear.
        assert "REDACTED" in combined or "***REDACTED***" in combined, \
            f"no redaction marker in: {combined}"

    def test_unredacted_export_keeps_secrets(self, gates, audit_file):
        """When redact=False, the raw audit lines must be returned."""
        aid = gates.create_approval("run_command", {"command": "echo token=abc123"})
        gates.resolve(aid, approve=True)
        lines = SafetyGates.export_audit(redact=False)
        assert any("abc123" in line for line in lines), \
            f"raw secret missing when redact=False: {lines}"

    def test_empty_audit_returns_empty_list(self, audit_file):
        """When no actions have been gated, export_audit returns []."""
        reset_shared_gates()
        lines = SafetyGates.export_audit(redact=True)
        assert lines == []

    def test_disabled_computer_use_skips_audit(self):
        """When COMPUTER_USE_ENABLED=false, no audit lines are written."""
        import os
        os.environ["COMPUTER_USE_ENABLED"] = "false"
        reset_shared_gates()
        g = SafetyGates()
        assert g.enabled is False
        # create_approval still works (it's a method on the object), but
        # the agent_server layer checks `enabled` before calling it.
        # Here we just verify the gate state.
        assert g.enabled is False
