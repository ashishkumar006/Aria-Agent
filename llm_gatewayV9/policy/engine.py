"""Declarative policy evaluator. Pure logic, no network, fully unit-tested.

Request shape (dict):
  {trust: owner|paired|untrusted, tool: <name>, channel: <name|None>,
   agent: <name|None>, session: <id|None>, spend_usd: <float>,
   over_spend_cap: <bool>, destructive: <bool>}

Evaluation: first rule whose `when` matches wins; no match -> default_action
(deny). In dry-run mode (global or per-rule enforce:false) the verdict is
reported but the caller is told allowed=true with dry_run=true, so operators
can observe before arming. Ties resolve to deny.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

POLICY_PATH = Path(__file__).resolve().parent / "policy.yaml"
_LOCK = threading.Lock()
_ENGINE = None
_MTIME = 0.0


@dataclass(frozen=True)
class PolicyVerdict:
    allowed: bool
    action: str  # allow | deny | approve
    rule_id: str | None = None
    reason: str = ""
    dry_run: bool = True


def _match(when: dict[str, Any], req: dict[str, Any]) -> bool:
    for k, want in (when or {}).items():
        got = req.get(k)
        if isinstance(want, list):
            if got not in want:
                return False
        elif isinstance(want, dict):
            if not isinstance(got, dict):
                return False
            for sk, sv in want.items():
                if got.get(sk) != sv:
                    return False
        else:
            if got != want:
                return False
    return True


class PolicyEngine:
    def __init__(self, doc: dict[str, Any]):
        self.doc = doc or {}
        self.defaults = self.doc.get("defaults", {}) or {}
        self.rules = self.doc.get("rules", []) or []
        self.spend_caps = self.doc.get("spend_caps_usd", {}) or {}

    @property
    def dry_run_global(self) -> bool:
        return bool(self.defaults.get("dry_run", True))

    def evaluate(self, req: dict[str, Any]) -> PolicyVerdict:
        req = dict(req or {})
        for rule in self.rules:
            if not isinstance(rule, dict):
                continue
            if _match(rule.get("when", {}), req):
                action = str(rule.get("action", "deny")).lower()
                if action not in ("allow", "deny", "approve"):
                    action = "deny"
                # A rule with `enforce: true` arms itself even while the
                # global mode is dry-run; without the flag the rule follows
                # the global mode.
                dry = self.dry_run_global and not bool(rule.get("enforce", False))
                if dry:
                    return PolicyVerdict(True, action, rule.get("id"),
                                         reason=rule.get("reason", "") or f"matched {rule.get('id')} (dry-run)",
                                         dry_run=True)
                return PolicyVerdict(action == "allow", action, rule.get("id"),
                                     reason=rule.get("reason", ""), dry_run=False)
        # No rule matched -> default (deny), still honouring dry-run.
        default = str(self.defaults.get("default_action", "deny")).lower()
        if default not in ("allow", "deny", "approve"):
            default = "deny"
        if self.dry_run_global:
            return PolicyVerdict(True, default, None,
                                 reason="no rule matched (dry-run default)", dry_run=True)
        return PolicyVerdict(default == "allow", default, None,
                             reason="no rule matched", dry_run=False)


def _load_doc() -> dict[str, Any]:
    try:
        import yaml
        return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"defaults": {"dry_run": True, "default_action": "deny"}, "rules": []}


def get_engine() -> PolicyEngine:
    """Singleton with mtime hot-reload (edits to policy.yaml apply live)."""
    global _ENGINE, _MTIME
    try:
        mtime = POLICY_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _LOCK:
        if _ENGINE is None or mtime != _MTIME:
            _ENGINE = PolicyEngine(_load_doc())
            _MTIME = mtime
        return _ENGINE


def reload_engine() -> PolicyEngine:
    global _ENGINE, _MTIME
    with _LOCK:
        _ENGINE = PolicyEngine(_load_doc())
        try:
            _MTIME = POLICY_PATH.stat().st_mtime
        except OSError:
            _MTIME = 0.0
        return _ENGINE


def spend_over_cap(*, per_call_usd: float = 0.0, agent_day_usd: float = 0.0,
                   session_day_usd: float = 0.0) -> bool:
    """Check a prospective spend against policy.yaml caps (pure function)."""
    caps = get_engine().spend_caps
    try:
        if per_call_usd > float(caps.get("per_call", 1e9)):
            return True
        if agent_day_usd > float(caps.get("per_agent_per_day", 1e9)):
            return True
        if session_day_usd > float(caps.get("per_session_per_day", 1e9)):
            return True
    except (TypeError, ValueError):
        return False
    return False
